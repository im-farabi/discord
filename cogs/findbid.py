# cogs/findbid.py

import asyncio
import re
import unicodedata
from collections import defaultdict

import discord
from discord.ext import commands


# ============================================================
# CONFIG
# ============================================================

PREFIX = ","

# API pacing. Keep this small but non-zero.
HISTORY_DELAY = 0.20
INVITE_DELAY = 0.35

# History limits. The system is intentionally progressive:
# cheap structure checks first, targeted history second.
PRIMARY_HISTORY_LIMIT = 14
FALLBACK_HISTORY_LIMIT = 10
SLOT_HISTORY_LIMIT = 8
REQUEST_HISTORY_LIMIT = 18

# Limits per server so one huge server cannot consume the whole scan.
MAX_PRIMARY_CHANNELS = 4
MAX_FALLBACK_CHANNELS = 5
MAX_SLOT_CHANNELS = 6
MAX_REQUEST_CHANNELS = 4

# A server is accepted if it passes one of the hard verification gates
# near the bottom. Score is used for ranking/confidence, NOT as the only gate.
LIKELY_SCORE = 50
VERIFIED_SCORE = 70

# Discord output safety.
MESSAGE_LIMIT = 1850
RESULTS_PER_PAGE = 5

# Never use person-name detection as an elimination condition.
# These are only obvious infrastructure terms used to avoid wasting
# slot-history reads on channels that are very unlikely to be personal slots.
INFRASTRUCTURE_WORDS = {
    "rules", "rule", "welcome", "welc", "verify", "verification",
    "announcements", "announcement", "updates", "update",
    "general", "chat", "main", "lounge", "media", "memes",
    "bot", "bots", "commands", "roles", "role", "staff", "mod",
    "mods", "admin", "admins", "support", "help", "ask",
    "questions", "question", "request", "requests", "ticket",
    "tickets", "giveaway", "giveaways", "events", "event",
    "partnership", "partnerships", "partners", "partner",
    "advertise", "advertising", "ads", "ad", "promo", "promos",
    "selfpromo", "self", "information", "info", "faq",
    "boost", "boosts", "vouch", "vouches", "reviews", "review",
    "logs", "log", "voice", "vc", "music", "games", "game",
}

# ============================================================
# NORMALIZATION / TEXT EXTRACTION
# ============================================================

INVITE_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(?:discord\.gg/|discord(?:app)?\.com/invite/)"
    r"[A-Za-z0-9_-]+",
    re.IGNORECASE,
)

MONEY_REGEX = re.compile(
    r"(?:(?:[$€£]\s?\d+(?:\.\d+)?)|(?:\b\d+(?:\.\d+)?\s?(?:usd|eur|gbp|wt|robux|rbx)\b))",
    re.IGNORECASE,
)

DURATION_REGEX = re.compile(
    r"\b(?:\d+\s?(?:d|day|days|w|wk|week|weeks)|perm|permanent)\b",
    re.IGNORECASE,
)

EVERYONE_REGEX = re.compile(r"@everyone|<@&?\d+>", re.IGNORECASE)

BID_TOKEN_REGEX = re.compile(r"\bbids?\b|\bbidding\b", re.IGNORECASE)

# Strong phrases that describe the exact "bid slot / personal channel" system.
STRONG_BID_PHRASES = (
    "bid info",
    "bids info",
    "bid information",
    "bids information",
    "bid slot",
    "bid slots",
    "bids slot",
    "bids slots",
    "free bid",
    "free bids",
    "perm bid",
    "permanent bid",
    "open a ticket to bid",
    "ticket to bid",
    "bid is always available",
    "bids are always available",
    "bid channel",
    "bid channels",
    "bidding server",
    "bidding discord",
)

# Request/help-channel evidence.
REQUEST_PHRASES = (
    "bid named",
    "bid name",
    "bid channel pls",
    "bid channel please",
    "bid slot open",
    "bid slots open",
    "free bid slot",
    "free bid slots",
    "can i get a bid",
    "can i have a bid",
    "need a bid",
    "make me a bid",
    "get a bid named",
)

# Bid-slot/ad content signals.
SLOT_TERMS = (
    "sb", "hb", "ia", "offer", "offers", "offer below",
    "selling", "s-lling", "buying", "funding",
    "qjs", "quick js", "quickjs", "js", "mop", "budget",
    "looking for", "lf ", "bidding",
)

# Negative auction signals. These reduce confidence, but never alone
# eliminate a server because real bid servers may use "offer"/"highest".
AUCTION_TERMS = (
    "highest bidder",
    "bid increment",
    "auction closes",
    "auction ending",
    "auction ends",
    "item auction",
    "nft",
    "sold to",
    "highest bid wins",
)


def normalize_text(value) -> str:
    if value is None:
        return ""
    try:
        text = unicodedata.normalize("NFKC", str(value)).casefold()
    except Exception:
        text = str(value).lower()

    # Preserve @, $, €, £, / and . because they are useful evidence.
    text = re.sub(r"[^\w@$/€£.\-]+", " ", text, flags=re.UNICODE)
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_name(value) -> str:
    if value is None:
        return ""
    try:
        text = unicodedata.normalize("NFKC", str(value)).casefold()
    except Exception:
        text = str(value).lower()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def message_search_text(message) -> str:
    pieces = []

    try:
        if message.content:
            pieces.append(str(message.content))
    except Exception:
        pass

    try:
        for embed in getattr(message, "embeds", []) or []:
            for attr in ("url", "title", "description"):
                try:
                    value = getattr(embed, attr, None)
                    if value:
                        pieces.append(str(value))
                except Exception:
                    pass

            try:
                for field in embed.fields:
                    if getattr(field, "name", None):
                        pieces.append(str(field.name))
                    if getattr(field, "value", None):
                        pieces.append(str(field.value))
            except Exception:
                pass
    except Exception:
        pass

    return "\n".join(pieces)


# ============================================================
# CHANNEL / PERMISSIONS
# ============================================================

def is_text_channel(channel):
    try:
        return isinstance(channel, discord.TextChannel)
    except Exception:
        return False


def get_self_member(bot, guild):
    try:
        if getattr(guild, "me", None) is not None:
            return guild.me
    except Exception:
        pass

    try:
        if bot.user:
            return guild.get_member(bot.user.id)
    except Exception:
        pass

    return None


def can_read_channel(bot, guild, channel):
    """
    FindBid is discovery-only, so unlike an ad-posting finder,
    send_messages is NOT required.

    Only require:
      - view_channel
      - read_message_history
    """
    try:
        member = get_self_member(bot, guild)
        if member is None:
            return False

        perms = channel.permissions_for(member)
        return bool(
            getattr(perms, "view_channel", False)
            and getattr(perms, "read_message_history", False)
        )
    except Exception:
        return False


def can_create_invite(bot, guild, channel):
    try:
        member = get_self_member(bot, guild)
        if member is None:
            return False

        perms = channel.permissions_for(member)
        return bool(
            getattr(perms, "view_channel", False)
            and getattr(perms, "create_instant_invite", False)
        )
    except Exception:
        return False


def channel_url(guild_id, channel_id):
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


# ============================================================
# NAME / STRUCTURE SIGNALS
# ============================================================

def bid_name_strength(name: str):
    """
    Returns (score, reason).

    This is an entry signal only. A channel name never verifies a server.
    """
    n = normalize_name(name)
    if not n:
        return 0, None

    tokens = n.split()

    exact_strong = {
        "bid info": 24,
        "bids info": 24,
        "bid information": 24,
        "bids information": 24,
        "bid slots": 26,
        "bids slots": 26,
        "bid slot": 24,
        "bids slot": 24,
        "bid rules": 20,
        "bids rules": 20,
        "bidding info": 20,
    }

    if n in exact_strong:
        return exact_strong[n], f"strong-name:{n}"

    if n in {"bid", "bids", "bidding"}:
        return 16, f"exact-name:{n}"

    # Token-based, not raw substring matching.
    if "bid" in tokens or "bids" in tokens or "bidding" in tokens:
        return 12, f"bid-token:{n}"

    return 0, None


def category_bid_strength(category):
    if category is None:
        return 0, None
    return bid_name_strength(getattr(category, "name", ""))


def obvious_infrastructure_channel(name: str) -> bool:
    """
    IMPORTANT:
    This does NOT decide whether a name is a person's name.
    It only marks obvious infrastructure names.

    Any unknown name remains a possible slot candidate.
    """
    n = normalize_name(name)
    if not n:
        return True

    tokens = set(n.split())
    if tokens & INFRASTRUCTURE_WORDS:
        return True

    return False


def is_requestish_channel(name: str) -> bool:
    n = normalize_name(name)
    tokens = set(n.split())
    return bool(tokens & {
        "ask", "help", "request", "requests", "question", "questions",
        "ticket", "tickets", "support", "general", "chat",
    })


def is_infoish_channel(name: str) -> bool:
    n = normalize_name(name)
    tokens = set(n.split())
    return bool(tokens & {
        "info", "information", "rules", "rule", "guide", "guides",
        "faq", "details", "detail",
    })


# ============================================================
# HISTORY CACHE
# ============================================================

async def get_history_cached(cache, channel, limit):
    """
    Cache by channel id. If a later stage asks for MORE messages than the
    cached amount, fetch again with the larger limit.
    """
    key = int(channel.id)
    cached = cache.get(key)

    if cached and cached["limit"] >= limit:
        return cached["messages"][:limit]

    messages = []
    try:
        async for message in channel.history(limit=limit):
            messages.append(message)
    except (discord.Forbidden, discord.NotFound):
        messages = []
    except discord.HTTPException:
        messages = []
    except Exception:
        messages = []

    cache[key] = {
        "limit": limit,
        "messages": messages,
    }

    await asyncio.sleep(HISTORY_DELAY)
    return messages


# ============================================================
# CONTENT ANALYSIS
# ============================================================

def analyze_text_blob(raw_text: str):
    normalized = normalize_text(raw_text)

    strong = []
    requests = []
    slot_terms = []
    negatives = []

    for phrase in STRONG_BID_PHRASES:
        if phrase in normalized:
            strong.append(phrase)

    for phrase in REQUEST_PHRASES:
        if phrase in normalized:
            requests.append(phrase)

    for phrase in SLOT_TERMS:
        if phrase in normalized:
            slot_terms.append(phrase)

    for phrase in AUCTION_TERMS:
        if phrase in normalized:
            negatives.append(phrase)

    return {
        "normalized": normalized,
        "has_bid_token": bool(BID_TOKEN_REGEX.search(normalized)),
        "has_invite": bool(INVITE_REGEX.search(raw_text)),
        "has_money": bool(MONEY_REGEX.search(normalized)),
        "has_duration": bool(DURATION_REGEX.search(normalized)),
        "has_everyone": bool(EVERYONE_REGEX.search(raw_text)),
        "strong_phrases": strong,
        "request_phrases": requests,
        "slot_terms": slot_terms,
        "negative_terms": negatives,
    }


def analyze_messages(messages):
    result = {
        "meaningful": 0,
        "bid_messages": 0,
        "invite_messages": 0,
        "everyone_messages": 0,
        "money_messages": 0,
        "duration_messages": 0,
        "strong_phrases": set(),
        "request_phrases": set(),
        "slot_terms": set(),
        "negative_terms": set(),
        "ad_like_messages": 0,
        "strong_slot_messages": 0,
    }

    for message in messages:
        raw = message_search_text(message).strip()
        if not raw:
            continue

        result["meaningful"] += 1
        a = analyze_text_blob(raw)

        if a["has_bid_token"]:
            result["bid_messages"] += 1
        if a["has_invite"]:
            result["invite_messages"] += 1
        if a["has_everyone"]:
            result["everyone_messages"] += 1
        if a["has_money"]:
            result["money_messages"] += 1
        if a["has_duration"]:
            result["duration_messages"] += 1

        result["strong_phrases"].update(a["strong_phrases"])
        result["request_phrases"].update(a["request_phrases"])
        result["slot_terms"].update(a["slot_terms"])
        result["negative_terms"].update(a["negative_terms"])

        # "Ad-like" means an invite plus one of the usual broadcast/sales signals.
        if a["has_invite"] and (
            a["has_everyone"]
            or a["has_bid_token"]
            or len(a["slot_terms"]) > 0
        ):
            result["ad_like_messages"] += 1

        # Strong bid-slot behavior, even without a Discord invite.
        if a["has_bid_token"] and (
            a["has_everyone"]
            or a["has_money"]
            or len(a["slot_terms"]) > 0
        ):
            result["strong_slot_messages"] += 1

    # convert sets for easier serialization/output
    for key in ("strong_phrases", "request_phrases", "slot_terms", "negative_terms"):
        result[key] = sorted(result[key])

    return result


# ============================================================
# INVITE RESOLUTION
# ============================================================

async def resolve_server_invite(bot, guild, preferred_channels):
    """
    Order:
      1) Vanity invite, if Discord lets us fetch it.
      2) Existing invite created by this account, if guild.invites() is allowed.
      3) Create a fresh non-expiring invite from a usable channel.
      4) Return None if no method is available.

    Never crashes the scan.
    """

    # 1) Vanity
    try:
        vanity_method = getattr(guild, "vanity_invite", None)
        if callable(vanity_method):
            invite = await vanity_method()
            if invite:
                return str(invite), "vanity"
    except Exception:
        pass

    await asyncio.sleep(INVITE_DELAY)

    # 2) Existing invite created by us, if accessible
    try:
        invites_method = getattr(guild, "invites", None)
        if callable(invites_method):
            invites = await invites_method()

            # Prefer an invite created by the current account.
            if bot.user:
                for invite in invites:
                    inviter = getattr(invite, "inviter", None)
                    if inviter and getattr(inviter, "id", None) == bot.user.id:
                        return str(invite), "existing-own"

            # If none are ours, an existing permanent-ish invite is still useful.
            for invite in invites:
                max_age = getattr(invite, "max_age", None)
                max_uses = getattr(invite, "max_uses", None)
                if max_age in (0, None) and max_uses in (0, None):
                    return str(invite), "existing"
    except Exception:
        pass

    await asyncio.sleep(INVITE_DELAY)

    # 3) Create a fresh invite.
    ordered = []
    seen = set()

    for ch in preferred_channels:
        if ch and getattr(ch, "id", None) not in seen:
            ordered.append(ch)
            seen.add(ch.id)

    try:
        for ch in guild.text_channels:
            if ch.id not in seen:
                ordered.append(ch)
                seen.add(ch.id)
    except Exception:
        pass

    for channel in ordered:
        try:
            if not is_text_channel(channel):
                continue
            if not can_create_invite(bot, guild, channel):
                continue

            invite = await channel.create_invite(
                max_age=0,
                max_uses=0,
                unique=False,
                reason="FindBid result link",
            )
            if invite:
                return str(invite), "generated"
        except Exception:
            continue
        finally:
            await asyncio.sleep(INVITE_DELAY)

    return None, "unavailable"


# ============================================================
# SERVER ANALYZER
# ============================================================

async def analyze_guild(bot, guild):
    """
    Progressive verifier.

    Critical design rule:
    - UNKNOWN channel names are NEVER treated as "not a person's name".
    - "personal-looking" channels are only prioritization candidates.
    - A server is never rejected merely because it lacks recognizable names.
    """

    evidence = {
        "bid_name_channels": [],
        "bid_categories": [],
        "primary_content_channels": [],
        "fallback_content_channels": [],
        "verified_slot_channels": [],
        "request_channels": [],
        "strong_phrases": set(),
        "request_phrases": set(),
        "negative_terms": set(),
        "score": 0,
        "passes": [],
        "stats": defaultdict(int),
    }

    history_cache = {}

    try:
        text_channels = list(guild.text_channels)
    except Exception:
        return None

    if not text_channels:
        return None

    # --------------------------------------------------------
    # STAGE 1 — FREE STRUCTURE SCAN
    # --------------------------------------------------------

    bid_named = []
    infoish = []
    requestish = []
    readable_channels = []

    categories_seen = {}

    for channel in text_channels:
        evidence["stats"]["channels_seen"] += 1

        if not is_text_channel(channel):
            continue

        if can_read_channel(bot, guild, channel):
            readable_channels.append(channel)

        score, reason = bid_name_strength(getattr(channel, "name", ""))
        if score > 0:
            bid_named.append((score, channel, reason))
            evidence["bid_name_channels"].append(channel)
            evidence["score"] += score

        if is_infoish_channel(getattr(channel, "name", "")):
            infoish.append(channel)

        if is_requestish_channel(getattr(channel, "name", "")):
            requestish.append(channel)

        category = getattr(channel, "category", None)
        if category is not None:
            categories_seen[getattr(category, "id", id(category))] = category

    for category in categories_seen.values():
        cscore, creason = category_bid_strength(category)
        if cscore > 0:
            evidence["bid_categories"].append(category)
            evidence["score"] += min(cscore, 18)

    if evidence["bid_name_channels"]:
        evidence["passes"].append("bid-named channel found")

    if evidence["bid_categories"]:
        evidence["passes"].append("bid-named category found")

    # --------------------------------------------------------
    # STAGE 2 — STRUCTURAL PRIORITIZATION
    # --------------------------------------------------------
    #
    # We do NOT ask "is ariyan a real name?"
    # We only say:
    #   "unknown/non-infrastructure channel under a bid category is worth
    #    sampling later."
    #
    # No elimination happens here.
    # --------------------------------------------------------

    slot_candidates = []
    slot_seen = set()

    bid_category_ids = {
        getattr(category, "id", None)
        for category in evidence["bid_categories"]
    }

    for channel in readable_channels:
        category = getattr(channel, "category", None)
        category_id = getattr(category, "id", None)

        if category_id in bid_category_ids:
            n = normalize_name(getattr(channel, "name", ""))

            # Skip the obvious bid info/header channel itself from "slot"
            # sampling, but UNKNOWN names remain candidates.
            bid_score, _ = bid_name_strength(n)
            if bid_score == 0 and not obvious_infrastructure_channel(n):
                if channel.id not in slot_seen:
                    slot_candidates.append((100, channel, "under-bid-category"))
                    slot_seen.add(channel.id)

    if len(slot_candidates) >= 3:
        evidence["score"] += 12
        evidence["passes"].append(
            f"{len(slot_candidates)} unknown/non-infrastructure channels under bid category"
        )

    # --------------------------------------------------------
    # STAGE 3 — PRIMARY CONTENT CHECK
    # --------------------------------------------------------
    #
    # If there are explicit bid channels, inspect those first.
    # This is the cheapest high-value content proof.
    # --------------------------------------------------------

    primary = sorted(bid_named, key=lambda x: x[0], reverse=True)
    primary = [
        channel
        for _, channel, _ in primary
        if can_read_channel(bot, guild, channel)
    ][:MAX_PRIMARY_CHANNELS]

    primary_verified = False
    bid_content_found = False

    for channel in primary:
        messages = await get_history_cached(
            history_cache,
            channel,
            PRIMARY_HISTORY_LIMIT,
        )
        a = analyze_messages(messages)
        evidence["primary_content_channels"].append((channel, a))

        if a["strong_phrases"]:
            evidence["strong_phrases"].update(a["strong_phrases"])
            evidence["score"] += min(35, 18 + 6 * len(a["strong_phrases"]))
            bid_content_found = True

        if a["bid_messages"] >= 1 and a["everyone_messages"] >= 1:
            evidence["score"] += 12
            bid_content_found = True

        if a["bid_messages"] >= 1 and a["duration_messages"] >= 1:
            evidence["score"] += 10
            bid_content_found = True

        if a["bid_messages"] >= 1 and a["money_messages"] >= 1:
            evidence["score"] += 6
            bid_content_found = True

        if a["negative_terms"]:
            evidence["negative_terms"].update(a["negative_terms"])
            evidence["score"] -= min(18, 6 * len(a["negative_terms"]))

        if (
            len(a["strong_phrases"]) >= 1
            and (
                a["everyone_messages"] >= 1
                or a["duration_messages"] >= 1
                or a["money_messages"] >= 1
            )
        ):
            primary_verified = True

    if primary_verified:
        evidence["passes"].append("bid-info/history behavior verified")

    # --------------------------------------------------------
    # STAGE 4 — FALLBACK CONTENT DISCOVERY
    # --------------------------------------------------------
    #
    # IMPORTANT: this prevents weak-stage elimination.
    #
    # If explicit #bid names were absent OR their history was inconclusive,
    # inspect a SMALL number of likely information channels.
    #
    # This lets us find:
    #   #info -> "open a ticket to bid"
    #   #rules -> "free bid slots"
    #
    # We intentionally do not scan every random chat in every server yet.
    # --------------------------------------------------------

    fallback_candidates = []
    fallback_seen = set()

    for channel in infoish:
        if channel.id in fallback_seen:
            continue
        if channel in primary:
            continue
        if not can_read_channel(bot, guild, channel):
            continue
        fallback_candidates.append(channel)
        fallback_seen.add(channel.id)

    # If there was NO explicit bid anchor at all, allow a few requestish
    # channels into fallback too. This is the "do not eliminate weakly" path.
    if not evidence["bid_name_channels"] and not evidence["bid_categories"]:
        for channel in requestish:
            if channel.id in fallback_seen:
                continue
            if not can_read_channel(bot, guild, channel):
                continue
            fallback_candidates.append(channel)
            fallback_seen.add(channel.id)

    fallback_candidates = fallback_candidates[:MAX_FALLBACK_CHANNELS]

    for channel in fallback_candidates:
        messages = await get_history_cached(
            history_cache,
            channel,
            FALLBACK_HISTORY_LIMIT,
        )
        a = analyze_messages(messages)
        evidence["fallback_content_channels"].append((channel, a))

        if a["strong_phrases"]:
            evidence["strong_phrases"].update(a["strong_phrases"])
            evidence["score"] += min(34, 16 + 6 * len(a["strong_phrases"]))
            bid_content_found = True

        if a["request_phrases"]:
            evidence["request_phrases"].update(a["request_phrases"])
            evidence["score"] += min(32, 18 + 7 * len(a["request_phrases"]))
            bid_content_found = True

        if a["negative_terms"]:
            evidence["negative_terms"].update(a["negative_terms"])
            evidence["score"] -= min(15, 5 * len(a["negative_terms"]))

    # --------------------------------------------------------
    # STAGE 5 — BUILD SLOT CANDIDATES WITHOUT "NAME AI"
    # --------------------------------------------------------
    #
    # Priority sources:
    #   A) unknown channels directly inside a bid category
    #   B) unknown channels in same category as a verified bid/info channel
    #   C) unknown channels near explicit bid anchors in channel order
    #
    # UNKNOWN means "not obviously infrastructure".
    # We NEVER claim it is a person's name.
    # --------------------------------------------------------

    related_category_ids = set(bid_category_ids)

    for channel, analysis in (
        evidence["primary_content_channels"]
        + evidence["fallback_content_channels"]
    ):
        if analysis["strong_phrases"] or analysis["request_phrases"]:
            category = getattr(channel, "category", None)
            cid = getattr(category, "id", None)
            if cid is not None:
                related_category_ids.add(cid)

    for channel in readable_channels:
        if channel.id in slot_seen:
            continue

        # Never treat explicit bid/info/request infrastructure channels as slots.
        if bid_name_strength(getattr(channel, "name", ""))[0] > 0:
            continue
        if is_infoish_channel(getattr(channel, "name", "")):
            continue
        if is_requestish_channel(getattr(channel, "name", "")):
            continue

        category = getattr(channel, "category", None)
        cid = getattr(category, "id", None)

        if cid in related_category_ids and not obvious_infrastructure_channel(
            getattr(channel, "name", "")
        ):
            slot_candidates.append((90, channel, "same-verified-category"))
            slot_seen.add(channel.id)

    # Position-neighbor fallback.
    # This is useful for layouts where #bid is followed by many slot channels
    # but Discord category naming is generic or duplicated.
    channel_positions = {
        channel.id: index
        for index, channel in enumerate(text_channels)
    }

    anchor_channels = list(evidence["bid_name_channels"])

    for anchor in anchor_channels:
        anchor_pos = channel_positions.get(anchor.id)
        if anchor_pos is None:
            continue

        # Sample a narrow window after each explicit bid anchor.
        for offset in range(1, 9):
            pos = anchor_pos + offset
            if pos >= len(text_channels):
                break

            channel = text_channels[pos]

            if channel.id in slot_seen:
                continue
            if not can_read_channel(bot, guild, channel):
                continue
            if bid_name_strength(getattr(channel, "name", ""))[0] > 0:
                continue
            if is_infoish_channel(getattr(channel, "name", "")):
                continue
            if is_requestish_channel(getattr(channel, "name", "")):
                continue
            if obvious_infrastructure_channel(getattr(channel, "name", "")):
                continue

            slot_candidates.append((60, channel, "near-bid-anchor"))
            slot_seen.add(channel.id)

    slot_candidates.sort(key=lambda x: x[0], reverse=True)

    # --------------------------------------------------------
    # STAGE 6 — VERIFY ACTUAL SLOT BEHAVIOR
    # --------------------------------------------------------

    verified_slots = 0

    for _, channel, reason in slot_candidates[:MAX_SLOT_CHANNELS]:
        messages = await get_history_cached(
            history_cache,
            channel,
            SLOT_HISTORY_LIMIT,
        )
        a = analyze_messages(messages)

        # Strong enough to count as a real ad/bid slot:
        # - invite + broadcast/sales signal, OR
        # - explicit bid + broadcast/money/sales signal.
        slot_verified = (
            a["ad_like_messages"] >= 1
            or a["strong_slot_messages"] >= 1
        )

        if slot_verified:
            verified_slots += 1
            evidence["verified_slot_channels"].append((channel, a, reason))
            evidence["score"] += 16

            if a["invite_messages"] >= 1:
                evidence["score"] += 5
            if a["everyone_messages"] >= 1:
                evidence["score"] += 5

        if a["negative_terms"]:
            evidence["negative_terms"].update(a["negative_terms"])

        # Early stop: two independently verified slot channels are already
        # extremely strong evidence. No need to hammer the API.
        if verified_slots >= 2:
            break

    if verified_slots >= 1:
        evidence["passes"].append(
            f"{verified_slots} actual bid/ad slot channel(s) verified"
        )

    # --------------------------------------------------------
    # STAGE 7 — REQUEST/HELP CONFIRMATION
    # --------------------------------------------------------
    #
    # Run when:
    #   - server already has some bid evidence, OR
    #   - there were no explicit bid names and fallback needs confirmation.
    #
    # This is where "bid named rina please" is caught.
    # --------------------------------------------------------

    should_check_requests = (
        bid_content_found
        or verified_slots >= 1
        or evidence["score"] >= 25
        or (
            not evidence["bid_name_channels"]
            and not evidence["bid_categories"]
        )
    )

    if should_check_requests:
        for channel in requestish[:MAX_REQUEST_CHANNELS]:
            if not can_read_channel(bot, guild, channel):
                continue

            messages = await get_history_cached(
                history_cache,
                channel,
                REQUEST_HISTORY_LIMIT,
            )
            a = analyze_messages(messages)

            if a["request_phrases"]:
                evidence["request_channels"].append((channel, a))
                evidence["request_phrases"].update(a["request_phrases"])
                evidence["score"] += min(
                    38,
                    22 + 8 * len(a["request_phrases"]),
                )

    if evidence["request_phrases"]:
        evidence["passes"].append("bid-request language verified")

    # --------------------------------------------------------
    # STAGE 8 — HARD VERIFICATION GATES
    # --------------------------------------------------------
    #
    # Score alone is NOT enough.
    #
    # Gate A:
    #   explicit bid anchor + real bid content
    #
    # Gate B:
    #   verified actual bid/ad slot channel + any bid anchor/content
    #
    # Gate C:
    #   strong request language + strong bid content
    #
    # Gate D:
    #   two actual slot channels + explicit bid/category anchor
    #
    # This makes false-positive auction/trading servers much harder to pass.
    # --------------------------------------------------------

    has_anchor = bool(
        evidence["bid_name_channels"]
        or evidence["bid_categories"]
    )
    has_strong_content = bool(evidence["strong_phrases"])
    has_request_proof = bool(evidence["request_phrases"])

    gate_a = has_anchor and has_strong_content
    gate_b = verified_slots >= 1 and (has_anchor or has_strong_content or has_request_proof)
    gate_c = has_request_proof and has_strong_content
    gate_d = verified_slots >= 2 and has_anchor

    accepted = gate_a or gate_b or gate_c or gate_d

    if not accepted:
        return None

    # Small final anti-auction sanity rule.
    # If auction-only language dominates and there is no slot/request proof,
    # reject. This does not affect normal bid servers.
    if (
        evidence["negative_terms"]
        and verified_slots == 0
        and not has_request_proof
        and len(evidence["negative_terms"]) >= 2
        and not has_strong_content
    ):
        return None

    # Confidence is descriptive, not the sole acceptance rule.
    score = max(0, evidence["score"])
    if score >= VERIFIED_SCORE:
        confidence = "🔥 Verified"
    elif score >= LIKELY_SCORE:
        confidence = "✅ Likely"
    else:
        confidence = "🟡 Passed"

    # Pick the best bid channel for output.
    best_bid_channel = None

    # Prefer a content-verified bid channel.
    for channel, analysis in evidence["primary_content_channels"]:
        if analysis["strong_phrases"] or analysis["bid_messages"]:
            best_bid_channel = channel
            break

    if best_bid_channel is None and evidence["bid_name_channels"]:
        best_bid_channel = evidence["bid_name_channels"][0]

    if best_bid_channel is None and evidence["fallback_content_channels"]:
        for channel, analysis in evidence["fallback_content_channels"]:
            if analysis["strong_phrases"] or analysis["request_phrases"]:
                best_bid_channel = channel
                break

    # Invite resolution happens only AFTER acceptance.
    preferred_invite_channels = []

    if best_bid_channel is not None:
        preferred_invite_channels.append(best_bid_channel)

    for channel, _, _ in evidence["verified_slot_channels"]:
        preferred_invite_channels.append(channel)

    invite_url, invite_type = await resolve_server_invite(
        bot,
        guild,
        preferred_invite_channels,
    )

    return {
        "guild": guild,
        "guild_id": guild.id,
        "name": getattr(guild, "name", "Unknown Server"),
        "members": int(getattr(guild, "member_count", 0) or 0),
        "score": score,
        "confidence": confidence,
        "invite_url": invite_url,
        "invite_type": invite_type,
        "best_bid_channel": best_bid_channel,
        "evidence": evidence,
    }


# ============================================================
# RESULT FORMATTER
# ============================================================

def compact_pass_reasons(result):
    e = result["evidence"]
    reasons = []

    if e["bid_name_channels"]:
        names = []
        for ch in e["bid_name_channels"][:2]:
            names.append(f"#{getattr(ch, 'name', 'bid')}")
        reasons.append("bid anchor " + ", ".join(names))

    if e["bid_categories"]:
        cat = e["bid_categories"][0]
        reasons.append(f"bid category `{getattr(cat, 'name', 'bid')}`")

    if e["strong_phrases"]:
        phrases = list(sorted(e["strong_phrases"]))[:3]
        reasons.append("info: " + ", ".join(f"`{p}`" for p in phrases))

    if e["verified_slot_channels"]:
        reasons.append(
            f"{len(e['verified_slot_channels'])} real ad/bid slot(s)"
        )

    if e["request_phrases"]:
        phrases = list(sorted(e["request_phrases"]))[:2]
        reasons.append(
            "requests: " + ", ".join(f"`{p}`" for p in phrases)
        )

    if not reasons:
        reasons.append("multi-stage bid verification")

    return reasons


def format_result(result, index):
    guild = result["guild"]
    members = result["members"]
    score = result["score"]
    confidence = result["confidence"]

    invite_url = result["invite_url"] or "⚠️ Could not get/create invite"

    bid_channel = result["best_bid_channel"]
    if bid_channel is not None:
        bid_link = channel_url(guild.id, bid_channel.id)
        bid_name = f"#{getattr(bid_channel, 'name', 'bid')}"
    else:
        bid_link = "Unavailable"
        bid_name = "Unknown"

    reasons = compact_pass_reasons(result)

    reason_text = "\n".join(
        f"   • {reason}"
        for reason in reasons
    )

    return (
        f"**{index}. {result['name']}**\n"
        f"👥 Members: **{members:,}**\n"
        f"🧠 Result: **{confidence}** — score **{score}**\n"
        f"✅ Passed because:\n{reason_text}\n"
        f"🔗 Server: {invite_url}\n"
        f"📌 Bid channel: {bid_name}\n"
        f"🔎 Channel link: {bid_link}\n"
    )


# ============================================================
# COG
# ============================================================

class FindBid(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.running = False

    @commands.command(name="findbid")
    async def findbid(self, ctx):
        """
        ,findbid

        Scans every server, verifies true Discord-style bid servers,
        resolves an invite, sorts by member count, and paginates 5 at a time.
        """

        if self.running:
            return await ctx.send(
                "⚠️ `,findbid` is already running."
            )

        self.running = True

        try:
            await ctx.send(
                "🔎 **FindBid started**\n"
                "Scanning every server with progressive verification:\n"
                "**structure → bid/info history → slot behavior → request proof → invite**\n\n"
                "Unknown channel names are **never** rejected just because the "
                "system cannot know whether they are a person's name."
            )

            try:
                guilds = list(self.bot.guilds)
            except Exception as e:
                return await ctx.send(
                    "❌ Couldn't access server list:\n"
                    f"`{type(e).__name__}: {e}`"
                )

            if not guilds:
                return await ctx.send(
                    "⚠️ No servers found."
                )

            results = []
            scanned = 0
            errors = 0

            # Sequential on purpose: safer for Discord API pressure.
            for guild in guilds:
                scanned += 1

                try:
                    result = await analyze_guild(
                        self.bot,
                        guild,
                    )

                    if result is not None:
                        results.append(result)

                except asyncio.CancelledError:
                    raise

                except Exception as e:
                    errors += 1
                    print(
                        "[findbid] Guild error:",
                        getattr(guild, "name", "Unknown"),
                        type(e).__name__,
                        e,
                    )
                    continue

            # Largest servers first.
            results.sort(
                key=lambda item: (
                    item["members"],
                    item["score"],
                ),
                reverse=True,
            )

            if not results:
                return await ctx.send(
                    "✅ **FindBid finished**\n\n"
                    f"Servers scanned: **{scanned}**\n"
                    f"Verified bid servers: **0**\n"
                    f"Errors skipped: **{errors}**\n\n"
                    "No server passed the multi-stage bid verification."
                )

            await ctx.send(
                "✅ **FindBid finished**\n\n"
                f"Servers scanned: **{scanned}**\n"
                f"Verified/likely bid servers: **{len(results)}**\n"
                f"Errors skipped: **{errors}**\n"
                "Sorted: **most members → least members**"
            )

            # ----------------------------------------------------
            # PAGINATION — 5 RESULTS, THEN Y/N, NO TIMEOUT
            # ----------------------------------------------------

            page_start = 0

            while page_start < len(results):
                page = results[
                    page_start:
                    page_start + RESULTS_PER_PAGE
                ]

                # Send each result separately to avoid Discord's message limit
                # and keep links clean/clickable.
                for offset, result in enumerate(page):
                    absolute_index = page_start + offset + 1
                    text = format_result(
                        result,
                        absolute_index,
                    )

                    if len(text) > MESSAGE_LIMIT:
                        text = text[:MESSAGE_LIMIT - 20] + "\n…"

                    await ctx.send(text)

                page_start += len(page)

                if page_start >= len(results):
                    await ctx.send(
                        "🏁 **All FindBid results sent.**"
                    )
                    break

                await ctx.send(
                    f"📄 Showing **{page_start}/{len(results)}**.\n"
                    "Continue with the next 5? **y/n**"
                )

                def check(message):
                    if message.author.id != ctx.author.id:
                        return False

                    if message.channel.id != ctx.channel.id:
                        return False

                    return message.content.strip().lower() in {
                        "y", "yes", "n", "no"
                    }

                # No timeout, exactly as requested.
                reply = await self.bot.wait_for(
                    "message",
                    check=check,
                )

                answer = reply.content.strip().lower()

                if answer in {"n", "no"}:
                    await ctx.send(
                        "⏹️ **FindBid stopped.**"
                    )
                    break

        except asyncio.CancelledError:
            try:
                await ctx.send(
                    "⚠️ `,findbid` was cancelled."
                )
            except Exception:
                pass
            raise

        except Exception as e:
            print(
                "[findbid] Fatal error:",
                type(e).__name__,
                e,
            )

            try:
                await ctx.send(
                    "❌ **FindBid crashed:**\n"
                    f"`{type(e).__name__}: {e}`"
                )
            except Exception:
                pass

        finally:
            self.running = False


# ============================================================
# SETUP
# ============================================================

async def setup(bot):
    await bot.add_cog(
        FindBid(bot)
    )

