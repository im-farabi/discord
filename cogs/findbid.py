# cogs/findbid.py
#
# Fast / rate-limit-safe FindBid
#
# Design goals:
#   • Scan every guild locally first.
#   • Never guess whether an unknown channel name is a person's name.
#   • Use history only on high-value channels.
#   • Never call guild.vanity_invite() or guild.invites() because those
#     endpoints can trigger long discord.py 429 sleeps.
#   • Wrap every history/invite API call in a hard timeout. If discord.py
#     starts sleeping on a 429, FindBid cancels that operation and moves on.
#   • Circuit-break history requests after repeated timeouts.
#   • Send a full progress summary every 30 seconds while scanning.
#   • Sort final results by member count and paginate 5 at a time.

import asyncio
import re
import time
import unicodedata
from collections import defaultdict

import discord
from discord.ext import commands


# ============================================================
# CONFIG
# ============================================================

RESULTS_PER_PAGE = 5
MESSAGE_LIMIT = 1850

# Each channel.history(limit <= 100) is normally one REST request.
PRIMARY_HISTORY_LIMIT = 12
SLOT_HISTORY_LIMIT = 6
REQUEST_HISTORY_LIMIT = 8
ANCHORLESS_HISTORY_LIMIT = 6

# Maximum targeted channels per server.
MAX_PRIMARY_CHANNELS = 2
MAX_SLOT_CHANNELS = 3
MAX_REQUEST_CHANNELS = 1

# Hard safety limits.
API_CALL_TIMEOUT = 4.0
INVITE_CALL_TIMEOUT = 4.0
HISTORY_PACE = 0.10
INVITE_PACE = 0.15

# If several REST calls begin hanging (usually discord.py waiting on a 429),
# stop making MORE history calls and complete the scan using evidence already
# collected. This prevents a one-hour Cloudflare retry from freezing FindBid.
MAX_CONSECUTIVE_API_TIMEOUTS = 3

# Extra global guardrail. Local structure scanning still scans every server.
MAX_HISTORY_CALLS = 140

# Progress report cadence.
PROGRESS_INTERVAL = 30.0

LIKELY_SCORE = 50
VERIFIED_SCORE = 70


# ============================================================
# KEYWORDS / REGEX
# ============================================================

INVITE_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(?:discord\.gg/|discord(?:app)?\.com/invite/)"
    r"[A-Za-z0-9_-]+",
    re.IGNORECASE,
)

MONEY_REGEX = re.compile(
    r"(?:(?:[$€£]\s?\d+(?:\.\d+)?)|"
    r"(?:\b\d+(?:\.\d+)?\s?(?:usd|eur|gbp|wt|robux|rbx)\b))",
    re.IGNORECASE,
)

DURATION_REGEX = re.compile(
    r"\b(?:\d+\s?(?:d|day|days|w|wk|week|weeks)|perm|permanent)\b",
    re.IGNORECASE,
)

BID_TOKEN_REGEX = re.compile(
    r"\bbids?\b|\bbidding\b",
    re.IGNORECASE,
)

EVERYONE_REGEX = re.compile(
    r"@everyone|<@&?\d+>",
    re.IGNORECASE,
)

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

SLOT_TERMS = (
    "sb",
    "hb",
    "ia",
    "offer",
    "offers",
    "offer below",
    "selling",
    "s-lling",
    "buying",
    "funding",
    "qjs",
    "quick js",
    "quickjs",
    "mop",
    "budget",
    "looking for",
    "bidding",
)

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

# IMPORTANT:
# This is NOT a list of "non-person names".
# It only identifies obvious server infrastructure so we can spend our very
# limited history calls on more useful channels.
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

REQUEST_CHANNEL_WORDS = {
    "ask", "help", "request", "requests",
    "question", "questions", "support",
}

INFO_CHANNEL_WORDS = {
    "info", "information", "rules", "rule",
    "guide", "guides", "faq", "details", "detail",
}


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_name(value) -> str:
    if value is None:
        return ""

    try:
        text = unicodedata.normalize(
            "NFKC",
            str(value),
        ).casefold()
    except Exception:
        text = str(value).lower()

    text = re.sub(
        r"[^\w]+",
        " ",
        text,
        flags=re.UNICODE,
    )

    text = text.replace(
        "_",
        " ",
    )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def normalize_text(value) -> str:
    if value is None:
        return ""

    try:
        text = unicodedata.normalize(
            "NFKC",
            str(value),
        ).casefold()
    except Exception:
        text = str(value).lower()

    # Keep characters useful for money / URLs / pings.
    text = re.sub(
        r"[^\w@$/€£.\-]+",
        " ",
        text,
        flags=re.UNICODE,
    )

    text = text.replace(
        "_",
        " ",
    )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def message_search_text(message) -> str:
    pieces = []

    try:
        content = getattr(
            message,
            "content",
            None,
        )

        if content:
            pieces.append(
                str(content)
            )
    except Exception:
        pass

    try:
        for embed in (
            getattr(
                message,
                "embeds",
                [],
            )
            or []
        ):

            for attr in (
                "url",
                "title",
                "description",
            ):

                try:
                    value = getattr(
                        embed,
                        attr,
                        None,
                    )

                    if value:
                        pieces.append(
                            str(value)
                        )
                except Exception:
                    pass

            try:
                for field in embed.fields:

                    if getattr(
                        field,
                        "name",
                        None,
                    ):
                        pieces.append(
                            str(field.name)
                        )

                    if getattr(
                        field,
                        "value",
                        None,
                    ):
                        pieces.append(
                            str(field.value)
                        )

            except Exception:
                pass

    except Exception:
        pass

    return "\n".join(
        pieces
    )


# ============================================================
# CHANNEL / PERMISSION HELPERS
# ============================================================

def is_text_channel(channel):
    try:
        return isinstance(
            channel,
            discord.TextChannel,
        )
    except Exception:
        return False


def get_self_member(bot, guild):
    try:
        member = getattr(
            guild,
            "me",
            None,
        )

        if member is not None:
            return member
    except Exception:
        pass

    try:
        if bot.user:
            return guild.get_member(
                bot.user.id
            )
    except Exception:
        pass

    return None


def can_read_channel(bot, guild, channel):
    try:
        member = get_self_member(
            bot,
            guild,
        )

        if member is None:
            return False

        perms = channel.permissions_for(
            member
        )

        return bool(
            getattr(
                perms,
                "view_channel",
                False,
            )
            and getattr(
                perms,
                "read_message_history",
                False,
            )
        )

    except Exception:
        return False


def can_create_invite(bot, guild, channel):
    try:
        member = get_self_member(
            bot,
            guild,
        )

        if member is None:
            return False

        perms = channel.permissions_for(
            member
        )

        return bool(
            getattr(
                perms,
                "view_channel",
                False,
            )
            and getattr(
                perms,
                "create_instant_invite",
                False,
            )
        )

    except Exception:
        return False


def channel_url(guild_id, channel_id):
    return (
        f"https://discord.com/channels/"
        f"{guild_id}/{channel_id}"
    )


# ============================================================
# CHANNEL-NAME / STRUCTURE SIGNALS
# ============================================================

def bid_name_strength(name: str):
    """
    Local-only signal.

    It NEVER verifies a server by itself.
    """

    n = normalize_name(
        name
    )

    if not n:
        return 0, None

    exact = {
        "bid info": 26,
        "bids info": 26,
        "bid information": 26,
        "bids information": 26,
        "bid slot": 26,
        "bid slots": 28,
        "bids slot": 26,
        "bids slots": 28,
        "bid rules": 22,
        "bids rules": 22,
        "bidding info": 22,
    }

    if n in exact:
        return (
            exact[n],
            f"strong-name:{n}",
        )

    if n in {
        "bid",
        "bids",
        "bidding",
    }:
        return (
            18,
            f"exact-name:{n}",
        )

    tokens = set(
        n.split()
    )

    if tokens & {
        "bid",
        "bids",
        "bidding",
    }:
        return (
            12,
            f"bid-token:{n}",
        )

    return 0, None


def is_requestish_channel(name: str):
    tokens = set(
        normalize_name(
            name
        ).split()
    )

    return bool(
        tokens
        & REQUEST_CHANNEL_WORDS
    )


def is_infoish_channel(name: str):
    tokens = set(
        normalize_name(
            name
        ).split()
    )

    return bool(
        tokens
        & INFO_CHANNEL_WORDS
    )


def obvious_infrastructure_channel(name: str):
    """
    UNKNOWN DOES NOT MEAN INVALID.

    If a channel is called:
        ariyan
        nini
        pibble
        abc123
        whatever

    we make NO attempt to decide whether that is a human name.

    We only avoid wasting slot-history reads on names that are obviously
    infrastructure such as #rules or #staff.
    """

    n = normalize_name(
        name
    )

    if not n:
        return True

    tokens = set(
        n.split()
    )

    return bool(
        tokens
        & INFRASTRUCTURE_WORDS
    )


# ============================================================
# MESSAGE ANALYSIS
# ============================================================

def analyze_text_blob(raw_text: str):
    normalized = normalize_text(
        raw_text
    )

    strong = []
    requests = []
    slot_terms = []
    negatives = []

    for phrase in STRONG_BID_PHRASES:
        if phrase in normalized:
            strong.append(
                phrase
            )

    for phrase in REQUEST_PHRASES:
        if phrase in normalized:
            requests.append(
                phrase
            )

    for phrase in SLOT_TERMS:
        if phrase in normalized:
            slot_terms.append(
                phrase
            )

    for phrase in AUCTION_TERMS:
        if phrase in normalized:
            negatives.append(
                phrase
            )

    return {
        "has_bid_token": bool(
            BID_TOKEN_REGEX.search(
                normalized
            )
        ),

        "has_invite": bool(
            INVITE_REGEX.search(
                raw_text
            )
        ),

        "has_money": bool(
            MONEY_REGEX.search(
                normalized
            )
        ),

        "has_duration": bool(
            DURATION_REGEX.search(
                normalized
            )
        ),

        "has_everyone": bool(
            EVERYONE_REGEX.search(
                raw_text
            )
        ),

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
        "ad_like_messages": 0,
        "strong_slot_messages": 0,
        "strong_phrases": set(),
        "request_phrases": set(),
        "slot_terms": set(),
        "negative_terms": set(),
    }

    for message in messages:

        raw = message_search_text(
            message
        ).strip()

        if not raw:
            continue

        result[
            "meaningful"
        ] += 1

        analysis = analyze_text_blob(
            raw
        )

        if analysis[
            "has_bid_token"
        ]:
            result[
                "bid_messages"
            ] += 1

        if analysis[
            "has_invite"
        ]:
            result[
                "invite_messages"
            ] += 1

        if analysis[
            "has_everyone"
        ]:
            result[
                "everyone_messages"
            ] += 1

        if analysis[
            "has_money"
        ]:
            result[
                "money_messages"
            ] += 1

        if analysis[
            "has_duration"
        ]:
            result[
                "duration_messages"
            ] += 1

        result[
            "strong_phrases"
        ].update(
            analysis[
                "strong_phrases"
            ]
        )

        result[
            "request_phrases"
        ].update(
            analysis[
                "request_phrases"
            ]
        )

        result[
            "slot_terms"
        ].update(
            analysis[
                "slot_terms"
            ]
        )

        result[
            "negative_terms"
        ].update(
            analysis[
                "negative_terms"
            ]
        )

        # Actual advertisement-style message.
        if (
            analysis["has_invite"]
            and (
                analysis["has_everyone"]
                or analysis["has_bid_token"]
                or analysis["slot_terms"]
            )
        ):
            result[
                "ad_like_messages"
            ] += 1

        # Bid behavior even when no invite exists.
        if (
            analysis["has_bid_token"]
            and (
                analysis["has_everyone"]
                or analysis["has_money"]
                or analysis["slot_terms"]
            )
        ):
            result[
                "strong_slot_messages"
            ] += 1

    for key in (
        "strong_phrases",
        "request_phrases",
        "slot_terms",
        "negative_terms",
    ):
        result[key] = sorted(
            result[key]
        )

    return result


# ============================================================
# API SAFETY STATE
# ============================================================

class ApiSafety:
    def __init__(self):
        self.history_calls = 0
        self.history_successes = 0
        self.history_timeouts = 0
        self.history_errors = 0

        self.invite_attempts = 0
        self.invite_successes = 0
        self.invite_timeouts = 0
        self.invite_errors = 0

        self.consecutive_timeouts = 0

        self.history_disabled = False
        self.history_budget_hit = False

    def may_use_history(self):
        if self.history_disabled:
            return False

        if (
            self.history_calls
            >= MAX_HISTORY_CALLS
        ):
            self.history_budget_hit = True
            self.history_disabled = True
            return False

        return True

    def history_ok(self):
        self.history_successes += 1
        self.consecutive_timeouts = 0

    def history_timeout(self):
        self.history_timeouts += 1
        self.consecutive_timeouts += 1

        if (
            self.consecutive_timeouts
            >= MAX_CONSECUTIVE_API_TIMEOUTS
        ):
            self.history_disabled = True


# ============================================================
# RATE-LIMIT-SAFE HISTORY
# ============================================================

async def fetch_history_safe(
    channel,
    limit,
    api_safety,
):
    """
    Very important behavior:

    discord.py normally handles a 429 by sleeping until Discord tells it to
    retry. If Discord says 3600 seconds, a normal try/except can sit there for
    an hour.

    asyncio.wait_for() puts a hard wall around the WHOLE history operation.
    If discord.py starts sleeping inside its 429 handler, this coroutine is
    cancelled after API_CALL_TIMEOUT and FindBid moves on.
    """

    if not api_safety.may_use_history():
        return [], "disabled"

    api_safety.history_calls += 1

    async def collect():
        messages = []

        async for message in channel.history(
            limit=limit
        ):
            messages.append(
                message
            )

        return messages

    try:
        messages = await asyncio.wait_for(
            collect(),
            timeout=API_CALL_TIMEOUT,
        )

        api_safety.history_ok()

        await asyncio.sleep(
            HISTORY_PACE
        )

        return messages, "ok"

    except asyncio.TimeoutError:
        api_safety.history_timeout()

        print(
            "[findbid] history timed out; "
            "possible rate-limit wait:",
            getattr(
                channel,
                "id",
                "unknown",
            ),
        )

        return [], "timeout"

    except (
        discord.Forbidden,
        discord.NotFound,
    ):
        api_safety.history_errors += 1
        api_safety.consecutive_timeouts = 0
        return [], "forbidden"

    except discord.HTTPException as e:
        api_safety.history_errors += 1
        api_safety.consecutive_timeouts = 0

        status = getattr(
            e,
            "status",
            None,
        )

        if status == 429:
            # Some forks may expose the 429 instead of sleeping.
            return [], "rate-limited"

        return [], "http-error"

    except asyncio.CancelledError:
        raise

    except Exception as e:
        api_safety.history_errors += 1
        api_safety.consecutive_timeouts = 0

        print(
            "[findbid] history error:",
            type(e).__name__,
            e,
        )

        return [], "error"


# ============================================================
# INVITE RESOLUTION — NO VANITY REST GET
# ============================================================

def cached_vanity_invite(guild):
    """
    NO API REQUEST.

    discord.py normally stores vanity_url_code from the guild payload when
    Discord supplies it. If it is present, build the URL locally.

    We deliberately DO NOT call:
        await guild.vanity_invite()
        await guild.invites()

    Those were the expensive/problematic endpoints in the old version.
    """

    try:
        code = getattr(
            guild,
            "vanity_url_code",
            None,
        )

        if code:
            return (
                f"https://discord.gg/{code}"
            )
    except Exception:
        pass

    return None


async def create_invite_safe(
    bot,
    guild,
    preferred_channels,
    api_safety,
):
    """
    Try a tiny number of channels.

    Every create_invite is hard-timed-out, so even an internal discord.py
    3600-second 429 retry cannot freeze FindBid.
    """

    cached = cached_vanity_invite(
        guild
    )

    if cached:
        return cached, "cached-vanity"

    ordered = []
    seen = set()

    for channel in preferred_channels:
        if (
            channel is not None
            and getattr(
                channel,
                "id",
                None,
            ) not in seen
        ):
            ordered.append(
                channel
            )
            seen.add(
                channel.id
            )

    # Add only a few readable text channels as fallback.
    try:
        for channel in guild.text_channels:

            if (
                channel.id
                in seen
            ):
                continue

            if not can_create_invite(
                bot,
                guild,
                channel,
            ):
                continue

            ordered.append(
                channel
            )
            seen.add(
                channel.id
            )

            if len(
                ordered
            ) >= 4:
                break

    except Exception:
        pass

    attempts = 0

    for channel in ordered:

        if attempts >= 3:
            break

        if not can_create_invite(
            bot,
            guild,
            channel,
        ):
            continue

        attempts += 1
        api_safety.invite_attempts += 1

        try:
            invite = await asyncio.wait_for(
                channel.create_invite(
                    max_age=0,
                    max_uses=0,
                    unique=False,
                    reason="FindBid result link",
                ),
                timeout=INVITE_CALL_TIMEOUT,
            )

            if invite:
                api_safety.invite_successes += 1

                await asyncio.sleep(
                    INVITE_PACE
                )

                return (
                    str(invite),
                    "generated",
                )

        except asyncio.TimeoutError:
            api_safety.invite_timeouts += 1

            print(
                "[findbid] invite creation timed out; "
                "skipping channel/server:",
                getattr(
                    guild,
                    "id",
                    "unknown",
                ),
            )

            # One timeout is enough to stop hammering invite creation
            # for this guild.
            break

        except (
            discord.Forbidden,
            discord.NotFound,
        ):
            api_safety.invite_errors += 1
            continue

        except discord.HTTPException as e:
            api_safety.invite_errors += 1

            if getattr(
                e,
                "status",
                None,
            ) == 429:
                break

            continue

        except asyncio.CancelledError:
            raise

        except Exception:
            api_safety.invite_errors += 1
            continue

    return None, "unavailable"


# ============================================================
# LOCAL GUILD INDEX
# ============================================================

def build_local_index(
    bot,
    guild,
):
    """
    ZERO REST REQUESTS.

    This function is allowed to scan every channel because all of this data
    already exists in the client's guild cache.
    """

    try:
        text_channels = list(
            guild.text_channels
        )
    except Exception:
        text_channels = []

    bid_named = []
    bid_categories = []
    infoish = []
    requestish = []
    readable = []

    categories_seen = {}

    for channel in text_channels:

        if not is_text_channel(
            channel
        ):
            continue

        if can_read_channel(
            bot,
            guild,
            channel,
        ):
            readable.append(
                channel
            )

        strength, reason = bid_name_strength(
            getattr(
                channel,
                "name",
                "",
            )
        )

        if strength > 0:
            bid_named.append(
                (
                    strength,
                    channel,
                    reason,
                )
            )

        if is_infoish_channel(
            getattr(
                channel,
                "name",
                "",
            )
        ):
            infoish.append(
                channel
            )

        if is_requestish_channel(
            getattr(
                channel,
                "name",
                "",
            )
        ):
            requestish.append(
                channel
            )

        category = getattr(
            channel,
            "category",
            None,
        )

        if category is not None:
            categories_seen[
                getattr(
                    category,
                    "id",
                    id(category),
                )
            ] = category

    for category in categories_seen.values():

        strength, reason = bid_name_strength(
            getattr(
                category,
                "name",
                "",
            )
        )

        if strength > 0:
            bid_categories.append(
                (
                    strength,
                    category,
                    reason,
                )
            )

    bid_named.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    bid_categories.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    return {
        "text_channels": text_channels,
        "readable": readable,
        "bid_named": bid_named,
        "bid_categories": bid_categories,
        "infoish": infoish,
        "requestish": requestish,
    }


# ============================================================
# SLOT CANDIDATE BUILDER
# ============================================================

def build_slot_candidates(
    guild_index,
    verified_categories,
):
    """
    No person-name AI.

    Unknown/non-infrastructure channels become candidates only because of
    STRUCTURE:
      • under a bid category
      • in the same category as verified bid info
      • immediately after a bid anchor
    """

    candidates = []
    seen = set()

    readable = guild_index[
        "readable"
    ]

    text_channels = guild_index[
        "text_channels"
    ]

    bid_named_channels = [
        channel
        for _, channel, _ in guild_index[
            "bid_named"
        ]
    ]

    bid_category_ids = {
        getattr(
            category,
            "id",
            None,
        )
        for _, category, _ in guild_index[
            "bid_categories"
        ]
    }

    related_category_ids = (
        set(
            bid_category_ids
        )
        | set(
            verified_categories
        )
    )

    # Strongest: under bid/verified category.
    for channel in readable:

        if channel.id in seen:
            continue

        if bid_name_strength(
            getattr(
                channel,
                "name",
                "",
            )
        )[0] > 0:
            continue

        if is_infoish_channel(
            getattr(
                channel,
                "name",
                "",
            )
        ):
            continue

        if is_requestish_channel(
            getattr(
                channel,
                "name",
                "",
            )
        ):
            continue

        category = getattr(
            channel,
            "category",
            None,
        )

        category_id = getattr(
            category,
            "id",
            None,
        )

        if (
            category_id
            in related_category_ids
            and not obvious_infrastructure_channel(
                getattr(
                    channel,
                    "name",
                    "",
                )
            )
        ):
            candidates.append(
                (
                    100,
                    channel,
                    "bid/verified-category",
                )
            )

            seen.add(
                channel.id
            )

    # Secondary: channels immediately after a bid anchor in guild order.
    positions = {
        channel.id: index
        for index, channel in enumerate(
            text_channels
        )
    }

    for anchor in bid_named_channels:

        anchor_pos = positions.get(
            anchor.id
        )

        if anchor_pos is None:
            continue

        for offset in range(
            1,
            7,
        ):
            pos = (
                anchor_pos
                + offset
            )

            if pos >= len(
                text_channels
            ):
                break

            channel = text_channels[
                pos
            ]

            if channel.id in seen:
                continue

            if channel not in readable:
                continue

            if bid_name_strength(
                getattr(
                    channel,
                    "name",
                    "",
                )
            )[0] > 0:
                continue

            if is_infoish_channel(
                getattr(
                    channel,
                    "name",
                    "",
                )
            ):
                continue

            if is_requestish_channel(
                getattr(
                    channel,
                    "name",
                    "",
                )
            ):
                continue

            if obvious_infrastructure_channel(
                getattr(
                    channel,
                    "name",
                    "",
                )
            ):
                continue

            candidates.append(
                (
                    60,
                    channel,
                    "near-bid-anchor",
                )
            )

            seen.add(
                channel.id
            )

    candidates.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    return candidates


# ============================================================
# ANCHORLESS FALLBACK PICKER
# ============================================================

def choose_anchorless_probe(
    guild_index,
):
    """
    Servers with no obvious #bid are NOT declared impossible.

    But we also do NOT scan 5-9 random histories per server.

    We spend at most ONE tiny history request:
      1) prefer #ask/#requests/#help
      2) otherwise #info/#rules

    If that tiny probe contains explicit bid-request/info language, the server
    is promoted into the deeper pipeline.
    """

    readable_ids = {
        channel.id
        for channel in guild_index[
            "readable"
        ]
    }

    for channel in guild_index[
        "requestish"
    ]:
        if channel.id in readable_ids:
            return channel

    for channel in guild_index[
        "infoish"
    ]:
        if channel.id in readable_ids:
            return channel

    return None


# ============================================================
# GUILD ANALYZER
# ============================================================

async def analyze_guild(
    bot,
    guild,
    guild_index,
    api_safety,
    progress,
):
    evidence = {
        "score": 0,
        "bid_name_channels": [],
        "bid_categories": [],
        "strong_phrases": set(),
        "request_phrases": set(),
        "negative_terms": set(),
        "verified_slots": [],
        "content_channels": [],
        "passes": [],
    }

    for strength, channel, _ in guild_index[
        "bid_named"
    ]:
        evidence[
            "score"
        ] += strength

        evidence[
            "bid_name_channels"
        ].append(
            channel
        )

    for strength, category, _ in guild_index[
        "bid_categories"
    ]:
        evidence[
            "score"
        ] += min(
            strength,
            18,
        )

        evidence[
            "bid_categories"
        ].append(
            category
        )

    has_anchor = bool(
        evidence[
            "bid_name_channels"
        ]
        or evidence[
            "bid_categories"
        ]
    )

    promoted_anchorless = False
    verified_category_ids = set()

    # --------------------------------------------------------
    # STAGE A — ONE-CALL ANCHORLESS FALLBACK
    # --------------------------------------------------------

    if not has_anchor:

        probe = choose_anchorless_probe(
            guild_index
        )

        if probe is None:
            progress[
                "local_only_rejected"
            ] += 1

            return None

        progress[
            "anchorless_probes"
        ] += 1

        messages, status = await fetch_history_safe(
            probe,
            ANCHORLESS_HISTORY_LIMIT,
            api_safety,
        )

        if status != "ok":
            progress[
                "api_skipped"
            ] += 1
            return None

        analysis = analyze_messages(
            messages
        )

        evidence[
            "strong_phrases"
        ].update(
            analysis[
                "strong_phrases"
            ]
        )

        evidence[
            "request_phrases"
        ].update(
            analysis[
                "request_phrases"
            ]
        )

        evidence[
            "negative_terms"
        ].update(
            analysis[
                "negative_terms"
            ]
        )

        if analysis[
            "strong_phrases"
        ]:
            evidence[
                "score"
            ] += 25

        if analysis[
            "request_phrases"
        ]:
            evidence[
                "score"
            ] += 30

        # Promotion requires explicit bid semantics from content.
        promoted_anchorless = bool(
            analysis[
                "strong_phrases"
            ]
            or analysis[
                "request_phrases"
            ]
        )

        if not promoted_anchorless:
            return None

        evidence[
            "content_channels"
        ].append(
            (
                probe,
                analysis,
            )
        )

        category = getattr(
            probe,
            "category",
            None,
        )

        category_id = getattr(
            category,
            "id",
            None,
        )

        if category_id is not None:
            verified_category_ids.add(
                category_id
            )

        evidence[
            "passes"
        ].append(
            "anchorless explicit bid language"
        )

        progress[
            "anchorless_promoted"
        ] += 1

    # --------------------------------------------------------
    # STAGE B — PRIMARY BID/INFO CONTENT
    # --------------------------------------------------------

    primary_channels = []

    for _, channel, _ in guild_index[
        "bid_named"
    ]:

        if not can_read_channel(
            bot,
            guild,
            channel,
        ):
            continue

        primary_channels.append(
            channel
        )

        if len(
            primary_channels
        ) >= MAX_PRIMARY_CHANNELS:
            break

    strong_content = bool(
        evidence[
            "strong_phrases"
        ]
    )

    for channel in primary_channels:

        progress[
            "deep_channels_checked"
        ] += 1

        messages, status = await fetch_history_safe(
            channel,
            PRIMARY_HISTORY_LIMIT,
            api_safety,
        )

        if status != "ok":
            progress[
                "api_skipped"
            ] += 1
            continue

        analysis = analyze_messages(
            messages
        )

        evidence[
            "content_channels"
        ].append(
            (
                channel,
                analysis,
            )
        )

        evidence[
            "strong_phrases"
        ].update(
            analysis[
                "strong_phrases"
            ]
        )

        evidence[
            "request_phrases"
        ].update(
            analysis[
                "request_phrases"
            ]
        )

        evidence[
            "negative_terms"
        ].update(
            analysis[
                "negative_terms"
            ]
        )

        if analysis[
            "strong_phrases"
        ]:
            evidence[
                "score"
            ] += min(
                34,
                18
                + 6
                * len(
                    analysis[
                        "strong_phrases"
                    ]
                ),
            )

            strong_content = True

        if (
            analysis[
                "bid_messages"
            ] >= 1
            and analysis[
                "everyone_messages"
            ] >= 1
        ):
            evidence[
                "score"
            ] += 12

        if (
            analysis[
                "bid_messages"
            ] >= 1
            and analysis[
                "duration_messages"
            ] >= 1
        ):
            evidence[
                "score"
            ] += 10

        if (
            analysis[
                "bid_messages"
            ] >= 1
            and analysis[
                "money_messages"
            ] >= 1
        ):
            evidence[
                "score"
            ] += 6

        if analysis[
            "negative_terms"
        ]:
            evidence[
                "score"
            ] -= min(
                15,
                5
                * len(
                    analysis[
                        "negative_terms"
                    ]
                ),
            )

        category = getattr(
            channel,
            "category",
            None,
        )

        category_id = getattr(
            category,
            "id",
            None,
        )

        if (
            category_id is not None
            and (
                analysis[
                    "strong_phrases"
                ]
                or analysis[
                    "bid_messages"
                ]
            )
        ):
            verified_category_ids.add(
                category_id
            )

        # Once one primary channel has strong content, a second primary history
        # is not always useful. But we allow at most 2 total.
        if (
            strong_content
            and len(
                evidence[
                    "content_channels"
                ]
            ) >= 2
        ):
            break

    # --------------------------------------------------------
    # STAGE C — VERIFY UNKNOWN SLOT CHANNELS
    # --------------------------------------------------------

    slot_candidates = build_slot_candidates(
        guild_index,
        verified_category_ids,
    )

    verified_slots = 0

    # If explicit strong bid-info already exists, one slot is enough extra
    # confirmation. Otherwise allow up to 2.
    target_slot_verifications = (
        1
        if strong_content
        else 2
    )

    for _, channel, reason in slot_candidates[
        :MAX_SLOT_CHANNELS
    ]:

        progress[
            "deep_channels_checked"
        ] += 1

        messages, status = await fetch_history_safe(
            channel,
            SLOT_HISTORY_LIMIT,
            api_safety,
        )

        if status != "ok":
            progress[
                "api_skipped"
            ] += 1
            continue

        analysis = analyze_messages(
            messages
        )

        slot_verified = bool(
            analysis[
                "ad_like_messages"
            ] >= 1
            or analysis[
                "strong_slot_messages"
            ] >= 1
        )

        if slot_verified:

            verified_slots += 1

            evidence[
                "verified_slots"
            ].append(
                (
                    channel,
                    analysis,
                    reason,
                )
            )

            evidence[
                "score"
            ] += 18

            if analysis[
                "invite_messages"
            ]:
                evidence[
                    "score"
                ] += 5

            if analysis[
                "everyone_messages"
            ]:
                evidence[
                    "score"
                ] += 5

        if (
            verified_slots
            >= target_slot_verifications
        ):
            break

    # --------------------------------------------------------
    # STAGE D — ONE REQUEST CHANNEL CONFIRMATION
    # --------------------------------------------------------

    should_check_request = bool(
        has_anchor
        and (
            strong_content
            or verified_slots >= 1
            or evidence[
                "score"
            ] >= 30
        )
    )

    if (
        should_check_request
        and guild_index[
            "requestish"
        ]
    ):

        readable_ids = {
            channel.id
            for channel in guild_index[
                "readable"
            ]
        }

        checked = 0

        for channel in guild_index[
            "requestish"
        ]:

            if checked >= MAX_REQUEST_CHANNELS:
                break

            if channel.id not in readable_ids:
                continue

            checked += 1

            progress[
                "deep_channels_checked"
            ] += 1

            messages, status = await fetch_history_safe(
                channel,
                REQUEST_HISTORY_LIMIT,
                api_safety,
            )

            if status != "ok":
                progress[
                    "api_skipped"
                ] += 1
                continue

            analysis = analyze_messages(
                messages
            )

            if analysis[
                "request_phrases"
            ]:

                evidence[
                    "request_phrases"
                ].update(
                    analysis[
                        "request_phrases"
                    ]
                )

                evidence[
                    "score"
                ] += min(
                    34,
                    22
                    + 6
                    * len(
                        analysis[
                            "request_phrases"
                        ]
                    ),
                )

                break

    # --------------------------------------------------------
    # STAGE E — HARD ACCEPTANCE
    # --------------------------------------------------------

    has_strong_content = bool(
        evidence[
            "strong_phrases"
        ]
    )

    has_request_proof = bool(
        evidence[
            "request_phrases"
        ]
    )

    gate_a = (
        has_anchor
        and has_strong_content
    )

    gate_b = (
        has_anchor
        and verified_slots >= 1
    )

    gate_c = (
        promoted_anchorless
        and has_request_proof
        and (
            has_strong_content
            or verified_slots >= 1
        )
    )

    gate_d = (
        has_anchor
        and has_request_proof
        and evidence[
            "score"
        ] >= 45
    )

    accepted = (
        gate_a
        or gate_b
        or gate_c
        or gate_d
    )

    if not accepted:
        return None

    # Anti-auction sanity check.
    if (
        evidence[
            "negative_terms"
        ]
        and not has_request_proof
        and verified_slots == 0
        and not has_strong_content
    ):
        return None

    # --------------------------------------------------------
    # STAGE F — BEST BID CHANNEL
    # --------------------------------------------------------

    best_bid_channel = None

    for channel, analysis in evidence[
        "content_channels"
    ]:

        if (
            analysis[
                "strong_phrases"
            ]
            or analysis[
                "bid_messages"
            ]
            or analysis[
                "request_phrases"
            ]
        ):
            best_bid_channel = channel
            break

    if (
        best_bid_channel is None
        and evidence[
            "bid_name_channels"
        ]
    ):
        best_bid_channel = evidence[
            "bid_name_channels"
        ][0]

    if (
        best_bid_channel is None
        and evidence[
            "verified_slots"
        ]
    ):
        best_bid_channel = evidence[
            "verified_slots"
        ][0][0]

    # --------------------------------------------------------
    # STAGE G — INVITE ONLY AFTER ACCEPTANCE
    # --------------------------------------------------------

    preferred_invite_channels = []

    if best_bid_channel is not None:
        preferred_invite_channels.append(
            best_bid_channel
        )

    for channel, _, _ in evidence[
        "verified_slots"
    ]:
        preferred_invite_channels.append(
            channel
        )

    invite_url, invite_type = await create_invite_safe(
        bot,
        guild,
        preferred_invite_channels,
        api_safety,
    )

    score = max(
        0,
        evidence[
            "score"
        ],
    )

    if score >= VERIFIED_SCORE:
        confidence = "🔥 Verified"

    elif score >= LIKELY_SCORE:
        confidence = "✅ Likely"

    else:
        confidence = "🟡 Passed"

    return {
        "guild": guild,
        "guild_id": guild.id,
        "name": getattr(
            guild,
            "name",
            "Unknown Server",
        ),
        "members": int(
            getattr(
                guild,
                "member_count",
                0,
            )
            or 0
        ),
        "score": score,
        "confidence": confidence,
        "invite_url": invite_url,
        "invite_type": invite_type,
        "best_bid_channel": best_bid_channel,
        "evidence": evidence,
    }


# ============================================================
# RESULT FORMATTING
# ============================================================

def compact_reasons(result):
    evidence = result[
        "evidence"
    ]

    reasons = []

    if evidence[
        "bid_name_channels"
    ]:

        names = [
            f"#{getattr(channel, 'name', 'bid')}"
            for channel in evidence[
                "bid_name_channels"
            ][:2]
        ]

        reasons.append(
            "bid anchor "
            + ", ".join(
                names
            )
        )

    if evidence[
        "bid_categories"
    ]:

        category = evidence[
            "bid_categories"
        ][0]

        reasons.append(
            "bid category "
            f"`{getattr(category, 'name', 'bid')}`"
        )

    if evidence[
        "strong_phrases"
    ]:

        phrases = sorted(
            evidence[
                "strong_phrases"
            ]
        )[:3]

        reasons.append(
            "info: "
            + ", ".join(
                f"`{phrase}`"
                for phrase in phrases
            )
        )

    if evidence[
        "verified_slots"
    ]:

        reasons.append(
            f"{len(evidence['verified_slots'])} "
            "real ad/bid slot(s)"
        )

    if evidence[
        "request_phrases"
    ]:

        phrases = sorted(
            evidence[
                "request_phrases"
            ]
        )[:2]

        reasons.append(
            "requests: "
            + ", ".join(
                f"`{phrase}`"
                for phrase in phrases
            )
        )

    return reasons


def format_result(
    result,
    index,
):
    guild = result[
        "guild"
    ]

    bid_channel = result[
        "best_bid_channel"
    ]

    if bid_channel is not None:

        bid_name = (
            f"#{getattr(bid_channel, 'name', 'bid')}"
        )

        bid_link = channel_url(
            guild.id,
            bid_channel.id,
        )

    else:
        bid_name = "Unknown"
        bid_link = "Unavailable"

    invite = (
        result[
            "invite_url"
        ]
        or "⚠️ invite unavailable / skipped"
    )

    reasons = compact_reasons(
        result
    )

    if reasons:
        reason_text = "\n".join(
            f"   • {reason}"
            for reason in reasons
        )
    else:
        reason_text = (
            "   • multi-stage bid verification"
        )

    return (
        f"**{index}. {result['name']}**\n"
        f"👥 Members: **{result['members']:,}**\n"
        f"🧠 Result: **{result['confidence']}** — "
        f"score **{result['score']}**\n"
        f"✅ Passed because:\n"
        f"{reason_text}\n"
        f"🔗 Server: {invite}\n"
        f"📌 Bid channel: {bid_name}\n"
        f"🔎 Channel link: {bid_link}\n"
    )


# ============================================================
# SAFE SEND
# ============================================================

async def safe_send(
    ctx,
    text,
    timeout=8.0,
):
    """
    Prevent a Discord send from becoming another hour-long hidden 429 sleep.
    """

    try:
        return await asyncio.wait_for(
            ctx.send(
                text
            ),
            timeout=timeout,
        )

    except asyncio.TimeoutError:
        print(
            "[findbid] ctx.send timed out; "
            "possible Discord/Cloudflare rate limit."
        )
        return None

    except asyncio.CancelledError:
        raise

    except Exception as e:
        print(
            "[findbid] ctx.send failed:",
            type(e).__name__,
            e,
        )
        return None


# ============================================================
# PROGRESS REPORTER
# ============================================================

def build_progress_summary(
    progress,
    api_safety,
):
    elapsed = max(
        0,
        int(
            time.monotonic()
            - progress[
                "started_at"
            ]
        ),
    )

    total = progress[
        "total_guilds"
    ]

    scanned = progress[
        "guilds_scanned"
    ]

    current = progress.get(
        "current_guild",
        "—",
    )

    if api_safety.history_disabled:

        if api_safety.history_budget_hit:
            api_state = (
                "🟡 history API safety budget reached; "
                "finishing with collected/local evidence"
            )
        else:
            api_state = (
                "🟠 repeated API timeouts/rate-limit waits detected; "
                "new history calls disabled"
            )

    else:
        api_state = "🟢 targeted API checks active"

    return (
        "⏱️ **FindBid 30s progress summary**\n"
        f"Elapsed: **{elapsed}s**\n"
        f"Servers scanned: **{scanned}/{total}**\n"
        f"Current server: **{current}**\n"
        f"Servers with bid anchor: "
        f"**{progress['anchor_candidates']}**\n"
        f"Anchorless tiny probes: "
        f"**{progress['anchorless_probes']}**\n"
        f"Anchorless promoted: "
        f"**{progress['anchorless_promoted']}**\n"
        f"Deep channels checked: "
        f"**{progress['deep_channels_checked']}**\n"
        f"Bid servers found so far: "
        f"**{progress['found']}**\n"
        f"History REST calls: "
        f"**{api_safety.history_calls}** "
        f"(ok {api_safety.history_successes}, "
        f"timeouts {api_safety.history_timeouts}, "
        f"errors {api_safety.history_errors})\n"
        f"Invite attempts: "
        f"**{api_safety.invite_attempts}** "
        f"(ok {api_safety.invite_successes}, "
        f"timeouts {api_safety.invite_timeouts}, "
        f"errors {api_safety.invite_errors})\n"
        f"API state: {api_state}"
    )


async def progress_reporter(
    ctx,
    progress,
    api_safety,
    done_event,
):
    """
    Sends one full summary every 30 seconds until the scan completes.
    """

    while not done_event.is_set():

        try:
            await asyncio.wait_for(
                done_event.wait(),
                timeout=PROGRESS_INTERVAL,
            )

            break

        except asyncio.TimeoutError:
            pass

        if done_event.is_set():
            break

        summary = build_progress_summary(
            progress,
            api_safety,
        )

        await safe_send(
            ctx,
            summary,
        )


# ============================================================
# COG
# ============================================================

class FindBid(commands.Cog):

    def __init__(
        self,
        bot,
    ):
        self.bot = bot
        self.running = False

    @commands.command(
        name="findbid"
    )
    async def findbid(
        self,
        ctx,
    ):
        """
        ,findbid
        """

        if self.running:

            return await safe_send(
                ctx,
                "⚠️ `,findbid` is already running.",
            )

        self.running = True

        done_event = asyncio.Event()
        reporter_task = None

        try:

            try:
                guilds = list(
                    self.bot.guilds
                )
            except Exception as e:

                return await safe_send(
                    ctx,
                    "❌ Couldn't access server list:\n"
                    f"`{type(e).__name__}: {e}`",
                )

            if not guilds:

                return await safe_send(
                    ctx,
                    "⚠️ No servers found.",
                )

            progress = {
                "started_at": time.monotonic(),
                "total_guilds": len(
                    guilds
                ),
                "guilds_scanned": 0,
                "current_guild": "starting",
                "anchor_candidates": 0,
                "anchorless_probes": 0,
                "anchorless_promoted": 0,
                "deep_channels_checked": 0,
                "local_only_rejected": 0,
                "api_skipped": 0,
                "found": 0,
                "errors": 0,
            }

            api_safety = ApiSafety()

            await safe_send(
                ctx,
                "🔎 **FindBid started**\n"
                "Fast pipeline: "
                "**local structure → tiny targeted history → "
                "slot/request proof → safe invite**\n"
                "• Every server is scanned locally.\n"
                "• Unknown names are never treated as invalid people names.\n"
                "• REST calls have hard timeouts, so a 3600s Discord 429 "
                "cannot intentionally hold FindBid for an hour.\n"
                "• Full progress summary will be sent every **30 seconds**.",
            )

            reporter_task = asyncio.create_task(
                progress_reporter(
                    ctx,
                    progress,
                    api_safety,
                    done_event,
                )
            )

            # ----------------------------------------------------
            # PASS 1 — LOCAL INDEX OF EVERY SERVER
            # ----------------------------------------------------
            #
            # This is virtually free and lets us prioritize explicit bid
            # servers before spending history requests on anchorless fallback.
            # ----------------------------------------------------

            indexed = []

            for guild in guilds:

                try:
                    index = build_local_index(
                        self.bot,
                        guild,
                    )

                    has_anchor = bool(
                        index[
                            "bid_named"
                        ]
                        or index[
                            "bid_categories"
                        ]
                    )

                    indexed.append(
                        (
                            1
                            if has_anchor
                            else 0,
                            guild,
                            index,
                        )
                    )

                    if has_anchor:
                        progress[
                            "anchor_candidates"
                        ] += 1

                except Exception as e:

                    progress[
                        "errors"
                    ] += 1

                    print(
                        "[findbid] local index error:",
                        getattr(
                            guild,
                            "name",
                            "Unknown",
                        ),
                        type(e).__name__,
                        e,
                    )

            # Explicit anchors first.
            # Within equal priority, larger guilds first so useful results
            # resolve earlier.
            indexed.sort(
                key=lambda item: (
                    item[0],
                    int(
                        getattr(
                            item[1],
                            "member_count",
                            0,
                        )
                        or 0
                    ),
                ),
                reverse=True,
            )

            # ----------------------------------------------------
            # PASS 2 — TARGETED VERIFICATION
            # ----------------------------------------------------

            results = []

            for _, guild, guild_index in indexed:

                progress[
                    "guilds_scanned"
                ] += 1

                progress[
                    "current_guild"
                ] = getattr(
                    guild,
                    "name",
                    "Unknown",
                )

                try:
                    result = await analyze_guild(
                        self.bot,
                        guild,
                        guild_index,
                        api_safety,
                        progress,
                    )

                    if result is not None:

                        results.append(
                            result
                        )

                        progress[
                            "found"
                        ] = len(
                            results
                        )

                except asyncio.CancelledError:
                    raise

                except Exception as e:

                    progress[
                        "errors"
                    ] += 1

                    print(
                        "[findbid] guild analysis error:",
                        getattr(
                            guild,
                            "name",
                            "Unknown",
                        ),
                        type(e).__name__,
                        e,
                    )

                    continue

            # ----------------------------------------------------
            # SCAN COMPLETE — STOP 30s REPORTER
            # ----------------------------------------------------

            done_event.set()

            if reporter_task is not None:

                try:
                    await asyncio.wait_for(
                        reporter_task,
                        timeout=2.0,
                    )
                except Exception:
                    reporter_task.cancel()

            progress[
                "current_guild"
            ] = "finished"

            # Largest -> smallest.
            results.sort(
                key=lambda item: (
                    item[
                        "members"
                    ],
                    item[
                        "score"
                    ],
                ),
                reverse=True,
            )

            elapsed = int(
                time.monotonic()
                - progress[
                    "started_at"
                ]
            )

            api_note = ""

            if api_safety.history_disabled:

                api_note = (
                    "\n⚠️ Some deep history checks were skipped by the "
                    "rate-limit safety circuit instead of waiting."
                )

            await safe_send(
                ctx,
                "✅ **FindBid scan finished**\n\n"
                f"Time: **{elapsed}s**\n"
                f"Servers structurally indexed: "
                f"**{len(indexed)}/{len(guilds)}**\n"
                f"Servers fully processed: "
                f"**{progress['guilds_scanned']}/{len(indexed)}**\n"
                f"Bid-anchor candidates: "
                f"**{progress['anchor_candidates']}**\n"
                f"Anchorless probes: "
                f"**{progress['anchorless_probes']}**\n"
                f"Anchorless promoted: "
                f"**{progress['anchorless_promoted']}**\n"
                f"History calls: "
                f"**{api_safety.history_calls}**\n"
                f"History timeouts: "
                f"**{api_safety.history_timeouts}**\n"
                f"API parts skipped: "
                f"**{progress['api_skipped']}**\n"
                f"Errors skipped: "
                f"**{progress['errors']}**\n"
                f"Bid servers found: "
                f"**{len(results)}**"
                f"{api_note}",
            )

            if not results:

                return await safe_send(
                    ctx,
                    "No server passed the multi-stage "
                    "bid-server verification.",
                )

            # ----------------------------------------------------
            # 5-BY-5 RESULT PAGINATION
            # ----------------------------------------------------

            page_start = 0

            while page_start < len(
                results
            ):

                page = results[
                    page_start:
                    page_start
                    + RESULTS_PER_PAGE
                ]

                for offset, result in enumerate(
                    page
                ):

                    absolute_index = (
                        page_start
                        + offset
                        + 1
                    )

                    text = format_result(
                        result,
                        absolute_index,
                    )

                    if len(
                        text
                    ) > MESSAGE_LIMIT:

                        text = (
                            text[
                                :MESSAGE_LIMIT
                                - 20
                            ]
                            + "\n…"
                        )

                    await safe_send(
                        ctx,
                        text,
                    )

                page_start += len(
                    page
                )

                if page_start >= len(
                    results
                ):

                    await safe_send(
                        ctx,
                        "🏁 **All FindBid results sent.**",
                    )

                    break

                await safe_send(
                    ctx,
                    f"📄 Showing "
                    f"**{page_start}/{len(results)}**.\n"
                    "Continue with the next 5? **y/n**",
                )

                def check(message):

                    try:
                        same_author = (
                            message.author.id
                            == ctx.author.id
                        )

                        same_channel = (
                            message.channel.id
                            == ctx.channel.id
                        )

                        valid_answer = (
                            message.content
                            .strip()
                            .lower()
                            in {
                                "y",
                                "yes",
                                "n",
                                "no",
                            }
                        )

                        return (
                            same_author
                            and same_channel
                            and valid_answer
                        )

                    except Exception:
                        return False

                # No timeout.
                reply = await self.bot.wait_for(
                    "message",
                    check=check,
                )

                answer = (
                    reply.content
                    .strip()
                    .lower()
                )

                if answer in {
                    "n",
                    "no",
                }:

                    await safe_send(
                        ctx,
                        "⏹️ **FindBid stopped.**",
                    )

                    break

        except asyncio.CancelledError:

            done_event.set()

            if reporter_task is not None:
                reporter_task.cancel()

            try:
                await safe_send(
                    ctx,
                    "⚠️ `,findbid` was cancelled.",
                )
            except Exception:
                pass

            raise

        except Exception as e:

            done_event.set()

            if reporter_task is not None:
                reporter_task.cancel()

            print(
                "[findbid] fatal error:",
                type(e).__name__,
                e,
            )

            try:
                await safe_send(
                    ctx,
                    "❌ **FindBid crashed:**\n"
                    f"`{type(e).__name__}: {e}`",
                )
            except Exception:
                pass

        finally:

            done_event.set()

            if (
                reporter_task is not None
                and not reporter_task.done()
            ):
                reporter_task.cancel()

            self.running = False


# ============================================================
# SETUP
# ============================================================

async def setup(bot):

    await bot.add_cog(
        FindBid(bot)
    )
