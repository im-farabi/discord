# cogs/findadpost.py
#
# ,findadpost
#
# What it does:
#   • Scans ALL guilds the account/bot can see.
#   • Scans ALL readable text channels + cached threads.
#   • Reads only the latest 14 messages from each channel.
#   • Detects ad-posting / ad-poster language with normalization + patterns.
#   • Stores every matching message.
#   • For each match, outputs:
#       - server name
#       - server invite (cached vanity or safely generated)
#       - channel link
#       - message link
#       - username
#       - matched keyword/reason
#       - short message preview
#   • Sends/edits a full progress summary every 30 seconds.
#   • Every history/invite REST operation has a hard timeout.
#     If discord.py begins sleeping on a long 429 retry, that operation is
#     cancelled and FindAdPost moves to the next channel/server.
#
# Notes:
#   • This scans the latest 14 MESSAGES per channel, not the last 14 days.
#   • It does NOT call guild.vanity_invite() or guild.invites(), because those
#     endpoints can trigger long 429 waits.
#   • Use a normal Discord bot/account setup that complies with Discord's rules.

import asyncio
import re
import time
import unicodedata

import discord
from discord.ext import commands


# ============================================================
# CONFIG
# ============================================================

LATEST_MESSAGES_PER_CHANNEL = 14

# Hard wall around a channel history REST request.
# If discord.py starts sleeping internally on a 429 (for example 3600s),
# wait_for cancels that single operation and we continue to the next channel.
HISTORY_TIMEOUT = 4.0

# Same idea for invite creation.
INVITE_TIMEOUT = 4.0

# Small pacing. Increase slightly if your account is in a very large
# number of servers/channels.
CHANNEL_PACE = 0.08
INVITE_PACE = 0.15

PROGRESS_INTERVAL = 30.0

# Discord message safety.
DISCORD_SAFE_MESSAGE_LENGTH = 1850

# Number of match entries sent in one result message.
RESULTS_PER_CHUNK = 5

# Preview length for the matching message.
MESSAGE_PREVIEW_LENGTH = 220

# If the API starts repeatedly hanging, we do NOT stop the command.
# We simply keep skipping timed-out channels. This is only used in the
# progress display so you can see what is happening.
LONG_TIMEOUT_WARNING_COUNT = 3


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_text(value) -> str:
    """
    Unicode-safe lowercasing + punctuation cleanup.

    Examples:
        "LF AD-POSTERS!!!" -> "lf ad posters"
        "ＡＤＰ"           -> "adp"
    """

    if value is None:
        return ""

    try:
        text = unicodedata.normalize(
            "NFKC",
            str(value),
        ).casefold()
    except Exception:
        text = str(value).lower()

    # Turn most punctuation into spaces while keeping useful @ / $ characters.
    text = re.sub(
        r"[^\w@$]+",
        " ",
        text,
        flags=re.UNICODE,
    )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


# ============================================================
# AD-POSTING DETECTION
# ============================================================

# Strong role words. These can match even without an "LF / need / hiring"
# word because "ad poster", "adposting", "adp" etc. are already specific.
STRONG_ROLE_PATTERNS = (
    (
        "ad poster",
        re.compile(
            r"\bad\s*posters?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "adposter",
        re.compile(
            r"\badposters?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ad posting",
        re.compile(
            r"\bad\s*posting\b",
            re.IGNORECASE,
        ),
    ),
    (
        "adposting",
        re.compile(
            r"\badposting\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ad post",
        re.compile(
            r"\bad\s*post\b",
            re.IGNORECASE,
        ),
    ),
    (
        "adp",
        re.compile(
            r"\badps?\b",
            re.IGNORECASE,
        ),
    ),
)

# "AP" is far too generic to trust by itself.
# We accept AP/APS only when it appears close to a recruitment/need word.
AP_ROLE_PATTERN = re.compile(
    r"\baps?\b",
    re.IGNORECASE,
)

INTENT_PATTERNS = (
    (
        "lf",
        re.compile(
            r"\blf\b",
            re.IGNORECASE,
        ),
    ),
    (
        "looking for",
        re.compile(
            r"\blooking\s+for\b",
            re.IGNORECASE,
        ),
    ),
    (
        "need",
        re.compile(
            r"\b(?:need|needs|needed|needing)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "hiring",
        re.compile(
            r"\b(?:hire|hiring|hired)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "wanted",
        re.compile(
            r"\b(?:want|wanted|wants)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "seeking",
        re.compile(
            r"\b(?:seek|seeking)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "recruiting",
        re.compile(
            r"\b(?:recruit|recruiting|recruitment)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "who can",
        re.compile(
            r"\bwho\s+can\b",
            re.IGNORECASE,
        ),
    ),
    (
        "anyone",
        re.compile(
            r"\b(?:anyone|someone|somebody)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "paid",
        re.compile(
            r"\b(?:paid|paying|payment)\b",
            re.IGNORECASE,
        ),
    ),
)

# Extra useful phrases that do not always fall neatly into the two groups.
EXTRA_PATTERNS = (
    (
        "do ad posting",
        re.compile(
            r"\b(?:do|doing|does)\s+ad\s*posting\b",
            re.IGNORECASE,
        ),
    ),
    (
        "can ad post",
        re.compile(
            r"\bcan\s+(?:you\s+|someone\s+|anyone\s+)?ad\s*post\b",
            re.IGNORECASE,
        ),
    ),
    (
        "people to ad post",
        re.compile(
            r"\b(?:people|person|someone|anyone)\s+to\s+ad\s*post\b",
            re.IGNORECASE,
        ),
    ),
    (
        "people for ad posting",
        re.compile(
            r"\b(?:people|person|someone|anyone)\s+for\s+ad\s*posting\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ad posting staff",
        re.compile(
            r"\bad\s*posting\s+(?:staff|team|worker|workers)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ad poster needed",
        re.compile(
            r"\bad\s*posters?\s+(?:needed|wanted|required)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "adp needed",
        re.compile(
            r"\badps?\s+(?:needed|wanted|required)\b",
            re.IGNORECASE,
        ),
    ),
)


def detect_adpost_keywords(raw_text: str):
    """
    Returns:
        (matched: bool, reasons: list[str])

    Detection philosophy:
      1) Strong ad-posting role words are enough to count.
      2) AP/APS is only counted with nearby intent because AP alone is noisy.
      3) Extra phrases catch natural wording like:
           "who can do ad posting"
           "need people to ad post"
    """

    normalized = normalize_text(
        raw_text
    )

    if not normalized:
        return False, []

    reasons = []

    # Strong role terms.
    for label, pattern in STRONG_ROLE_PATTERNS:
        if pattern.search(
            normalized
        ):
            reasons.append(
                label
            )

    # Extra phrases.
    for label, pattern in EXTRA_PATTERNS:
        if pattern.search(
            normalized
        ):
            reasons.append(
                label
            )

    # AP/APS needs recruitment/need context.
    ap_match = AP_ROLE_PATTERN.search(
        normalized
    )

    if ap_match:
        intent_found = []

        for intent_label, intent_pattern in INTENT_PATTERNS:
            match = intent_pattern.search(
                normalized
            )

            if match:
                # Require the intent and AP token to be fairly close.
                # Character distance is enough after normalization.
                distance = abs(
                    match.start()
                    - ap_match.start()
                )

                if distance <= 45:
                    intent_found.append(
                        intent_label
                    )

        if intent_found:
            reasons.append(
                "ap/aps + "
                + "/".join(
                    intent_found[:2]
                )
            )

    # De-duplicate while keeping order.
    seen = set()
    final_reasons = []

    for reason in reasons:
        if reason in seen:
            continue

        seen.add(
            reason
        )

        final_reasons.append(
            reason
        )

    return bool(
        final_reasons
    ), final_reasons


# ============================================================
# MESSAGE TEXT EXTRACTION
# ============================================================

def get_message_search_text(message) -> str:
    """
    Search both normal message text and embed text.
    """

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
        embeds = (
            getattr(
                message,
                "embeds",
                [],
            )
            or []
        )

        for embed in embeds:

            for attr in (
                "title",
                "description",
                "url",
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
                    name = getattr(
                        field,
                        "name",
                        None,
                    )

                    value = getattr(
                        field,
                        "value",
                        None,
                    )

                    if name:
                        pieces.append(
                            str(name)
                        )

                    if value:
                        pieces.append(
                            str(value)
                        )

            except Exception:
                pass

    except Exception:
        pass

    return "\n".join(
        pieces
    )


def make_preview(text: str) -> str:
    text = re.sub(
        r"\s+",
        " ",
        str(
            text
            or ""
        ),
    ).strip()

    if len(
        text
    ) <= MESSAGE_PREVIEW_LENGTH:
        return text

    return (
        text[
            :MESSAGE_PREVIEW_LENGTH
        ].rstrip()
        + "…"
    )


# ============================================================
# CHANNEL HELPERS
# ============================================================

def get_self_member(
    bot,
    guild,
):
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


def can_read_channel(
    bot,
    guild,
    channel,
):
    """
    Conservative permission pre-check.

    If we cannot determine the member object, return True and let the actual
    history request decide. This avoids accidentally skipping everything on
    unusual/self-bot-compatible discord.py forks.
    """

    try:
        member = get_self_member(
            bot,
            guild,
        )

        if member is None:
            return True

        permissions_for = getattr(
            channel,
            "permissions_for",
            None,
        )

        if not callable(
            permissions_for
        ):
            return True

        perms = permissions_for(
            member
        )

        view_ok = getattr(
            perms,
            "view_channel",
            True,
        )

        history_ok = getattr(
            perms,
            "read_message_history",
            True,
        )

        return bool(
            view_ok
            and history_ok
        )

    except Exception:
        # If permission inspection itself breaks, let the real API request
        # decide instead of eliminating the channel.
        return True


def can_create_invite(
    bot,
    guild,
    channel,
):
    try:
        member = get_self_member(
            bot,
            guild,
        )

        if member is None:
            # Unknown -> try once and let Discord decide.
            return True

        permissions_for = getattr(
            channel,
            "permissions_for",
            None,
        )

        if not callable(
            permissions_for
        ):
            return True

        perms = permissions_for(
            member
        )

        return bool(
            getattr(
                perms,
                "view_channel",
                True,
            )
            and getattr(
                perms,
                "create_instant_invite",
                False,
            )
        )

    except Exception:
        return True


def get_scannable_channels(
    guild,
):
    """
    Local/cache-only channel collection.

    Includes:
      • normal text/news channels from guild.text_channels
      • cached active threads from guild.threads (if the library exposes them)

    No REST call is made here.
    """

    channels = []
    seen = set()

    try:
        for channel in guild.text_channels:
            channel_id = getattr(
                channel,
                "id",
                None,
            )

            if channel_id is None:
                continue

            if channel_id in seen:
                continue

            seen.add(
                channel_id
            )

            channels.append(
                channel
            )
    except Exception:
        pass

    try:
        threads = (
            getattr(
                guild,
                "threads",
                [],
            )
            or []
        )

        for channel in threads:
            channel_id = getattr(
                channel,
                "id",
                None,
            )

            if channel_id is None:
                continue

            if channel_id in seen:
                continue

            seen.add(
                channel_id
            )

            channels.append(
                channel
            )
    except Exception:
        pass

    return channels


# ============================================================
# SAFE HISTORY FETCH
# ============================================================

async def fetch_latest_messages_safe(
    channel,
    stats,
):
    """
    Fetch latest 14 messages.

    IMPORTANT:
    discord.py can internally sleep when Discord returns 429.
    A normal try/except will not necessarily help because no exception may be
    raised until after the library finishes waiting.

    Wrapping the WHOLE history operation in asyncio.wait_for means a long
    429 sleep is cancelled after HISTORY_TIMEOUT and the command moves on.
    """

    stats[
        "history_attempts"
    ] += 1

    async def collect():
        result = []

        async for message in channel.history(
            limit=LATEST_MESSAGES_PER_CHANNEL
        ):
            result.append(
                message
            )

        return result

    try:
        messages = await asyncio.wait_for(
            collect(),
            timeout=HISTORY_TIMEOUT,
        )

        stats[
            "history_ok"
        ] += 1

        stats[
            "messages_checked"
        ] += len(
            messages
        )

        stats[
            "consecutive_timeouts"
        ] = 0

        if CHANNEL_PACE > 0:
            await asyncio.sleep(
                CHANNEL_PACE
            )

        return messages, "ok"

    except asyncio.TimeoutError:
        stats[
            "history_timeouts"
        ] += 1

        stats[
            "channels_skipped"
        ] += 1

        stats[
            "consecutive_timeouts"
        ] += 1

        print(
            "[findadpost] history timeout / probable rate-limit wait:",
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
        stats[
            "history_forbidden"
        ] += 1

        stats[
            "channels_skipped"
        ] += 1

        stats[
            "consecutive_timeouts"
        ] = 0

        return [], "forbidden"

    except discord.HTTPException as e:
        stats[
            "history_http_errors"
        ] += 1

        stats[
            "channels_skipped"
        ] += 1

        stats[
            "consecutive_timeouts"
        ] = 0

        status = getattr(
            e,
            "status",
            None,
        )

        if status == 429:
            stats[
                "explicit_429s"
            ] += 1

            return [], "rate-limited"

        return [], "http-error"

    except asyncio.CancelledError:
        raise

    except Exception as e:
        stats[
            "history_other_errors"
        ] += 1

        stats[
            "channels_skipped"
        ] += 1

        stats[
            "consecutive_timeouts"
        ] = 0

        print(
            "[findadpost] history error:",
            type(e).__name__,
            e,
        )

        return [], "error"


# ============================================================
# SAFE SERVER INVITE
# ============================================================

def cached_vanity_url(
    guild,
):
    """
    Uses cached guild data only. NO vanity-url REST request.
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


async def resolve_server_invite_safe(
    bot,
    guild,
    preferred_channel,
    invite_cache,
    stats,
):
    """
    Called only for guilds that actually produced a matching message.

    Order:
      1) cached vanity_url_code (zero REST)
      2) create one permanent invite on the matching channel if possible
      3) try up to two other text channels

    Never calls:
      guild.vanity_invite()
      guild.invites()

    Those can create extra rate-limit pressure.
    """

    guild_id = getattr(
        guild,
        "id",
        None,
    )

    if guild_id in invite_cache:
        return invite_cache[
            guild_id
        ]

    cached = cached_vanity_url(
        guild
    )

    if cached:
        result = (
            cached,
            "cached-vanity",
        )

        invite_cache[
            guild_id
        ] = result

        return result

    candidates = []
    seen = set()

    if preferred_channel is not None:
        channel_id = getattr(
            preferred_channel,
            "id",
            None,
        )

        if channel_id is not None:
            candidates.append(
                preferred_channel
            )

            seen.add(
                channel_id
            )

    try:
        for channel in guild.text_channels:
            channel_id = getattr(
                channel,
                "id",
                None,
            )

            if channel_id is None:
                continue

            if channel_id in seen:
                continue

            if not can_create_invite(
                bot,
                guild,
                channel,
            ):
                continue

            candidates.append(
                channel
            )

            seen.add(
                channel_id
            )

            if len(
                candidates
            ) >= 3:
                break
    except Exception:
        pass

    attempts = 0

    for channel in candidates:
        if attempts >= 3:
            break

        if not can_create_invite(
            bot,
            guild,
            channel,
        ):
            continue

        attempts += 1

        stats[
            "invite_attempts"
        ] += 1

        try:
            invite = await asyncio.wait_for(
                channel.create_invite(
                    max_age=0,
                    max_uses=0,
                    unique=False,
                    reason="FindAdPost result link",
                ),
                timeout=INVITE_TIMEOUT,
            )

            if invite:
                stats[
                    "invite_ok"
                ] += 1

                result = (
                    str(
                        invite
                    ),
                    "generated",
                )

                invite_cache[
                    guild_id
                ] = result

                if INVITE_PACE > 0:
                    await asyncio.sleep(
                        INVITE_PACE
                    )

                return result

        except asyncio.TimeoutError:
            stats[
                "invite_timeouts"
            ] += 1

            # Don't hammer more invite endpoints for the same server.
            break

        except (
            discord.Forbidden,
            discord.NotFound,
        ):
            stats[
                "invite_errors"
            ] += 1

            continue

        except discord.HTTPException as e:
            stats[
                "invite_errors"
            ] += 1

            if getattr(
                e,
                "status",
                None,
            ) == 429:
                stats[
                    "explicit_429s"
                ] += 1

                break

            continue

        except asyncio.CancelledError:
            raise

        except Exception:
            stats[
                "invite_errors"
            ] += 1

            continue

    result = (
        None,
        "unavailable",
    )

    invite_cache[
        guild_id
    ] = result

    return result


# ============================================================
# SAFE SEND / EDIT
# ============================================================

async def safe_send(
    ctx,
    text,
    timeout=8.0,
):
    """
    Hard timeout around ctx.send so progress/result output itself cannot sit
    in a giant hidden 429 sleep forever.
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
            "[findadpost] ctx.send timed out."
        )

        return None

    except asyncio.CancelledError:
        raise

    except Exception as e:
        print(
            "[findadpost] ctx.send error:",
            type(e).__name__,
            e,
        )

        return None


async def safe_edit(
    message,
    text,
    timeout=8.0,
):
    if message is None:
        return False

    try:
        await asyncio.wait_for(
            message.edit(
                content=text
            ),
            timeout=timeout,
        )

        return True

    except asyncio.TimeoutError:
        print(
            "[findadpost] progress edit timed out."
        )

        return False

    except asyncio.CancelledError:
        raise

    except Exception:
        return False


# ============================================================
# PROGRESS
# ============================================================

def build_progress_text(
    stats,
):
    elapsed = max(
        0,
        int(
            time.monotonic()
            - stats[
                "started_at"
            ]
        ),
    )

    if stats[
        "consecutive_timeouts"
    ] >= LONG_TIMEOUT_WARNING_COUNT:
        api_state = (
            "🟠 repeated channel timeouts detected; "
            "timed-out channels are being skipped"
        )
    else:
        api_state = (
            "🟢 scanning; failures/timeouts are skipped"
        )

    return (
        "🔎 **FindAdPost progress**\n"
        f"Elapsed: **{elapsed}s**\n"
        f"Servers: **{stats['guilds_done']}/{stats['guilds_total']}**\n"
        f"Current server: **{stats['current_guild']}**\n"
        f"Channels discovered: **{stats['channels_discovered']}**\n"
        f"Channels processed: **{stats['channels_done']}**\n"
        f"Channels skipped: **{stats['channels_skipped']}**\n"
        f"History calls: **{stats['history_attempts']}** "
        f"(ok {stats['history_ok']}, "
        f"timeouts {stats['history_timeouts']}, "
        f"forbidden {stats['history_forbidden']}, "
        f"http errors {stats['history_http_errors']})\n"
        f"Messages checked: **{stats['messages_checked']}**\n"
        f"Matching messages found: **{stats['matches_found']}**\n"
        f"Explicit 429s seen: **{stats['explicit_429s']}**\n"
        f"Errors skipped: **{stats['guild_errors'] + stats['history_other_errors']}**\n"
        f"API state: {api_state}"
    )


async def progress_worker(
    ctx,
    stats,
    done_event,
    progress_message_holder,
):
    """
    Every 30 seconds, EDIT the same progress message when possible.
    Editing one message is much lighter than spamming a new status message
    every 30 seconds.
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

        text = build_progress_text(
            stats
        )

        progress_message = progress_message_holder[
            "message"
        ]

        edited = await safe_edit(
            progress_message,
            text,
        )

        if not edited:
            # If edit is unsupported/fails, send a replacement and keep editing
            # that one in future.
            new_message = await safe_send(
                ctx,
                text,
            )

            if new_message is not None:
                progress_message_holder[
                    "message"
                ] = new_message


# ============================================================
# RESULT FORMAT
# ============================================================

def get_author_display(
    message,
):
    try:
        author = message.author

        display_name = getattr(
            author,
            "display_name",
            None,
        )

        name = getattr(
            author,
            "name",
            None,
        )

        discriminator = getattr(
            author,
            "discriminator",
            None,
        )

        author_id = getattr(
            author,
            "id",
            None,
        )

        if (
            discriminator
            and discriminator != "0"
            and name
        ):
            username = (
                f"{name}#{discriminator}"
            )

        else:
            username = (
                name
                or display_name
                or "Unknown"
            )

        if author_id is not None:
            return (
                f"{username} (`{author_id}`)"
            )

        return username

    except Exception:
        return "Unknown"


def get_message_link(
    message,
):
    try:
        jump_url = getattr(
            message,
            "jump_url",
            None,
        )

        if jump_url:
            return str(
                jump_url
            )
    except Exception:
        pass

    try:
        return (
            f"https://discord.com/channels/"
            f"{message.guild.id}/"
            f"{message.channel.id}/"
            f"{message.id}"
        )
    except Exception:
        return "Unavailable"


def get_channel_link(
    guild,
    channel,
):
    try:
        return (
            f"https://discord.com/channels/"
            f"{guild.id}/"
            f"{channel.id}"
        )
    except Exception:
        return "Unavailable"


def format_match(
    match,
    index,
):
    reasons = (
        ", ".join(
            f"`{reason}`"
            for reason in match[
                "reasons"
            ][:5]
        )
        or "`ad-posting keyword`"
    )

    server_invite = (
        match[
            "server_invite"
        ]
        or "⚠️ unavailable / invite creation skipped"
    )

    return (
        f"**{index}. {match['guild_name']}**\n"
        f"👤 User: **{match['username']}**\n"
        f"🎯 Matched: {reasons}\n"
        f"🔗 Server: {server_invite}\n"
        f"📌 Channel: {match['channel_link']}\n"
        f"💬 Message: {match['message_link']}\n"
        f"📝 Preview: {match['preview']}\n"
    )


# ============================================================
# COG
# ============================================================

class FindAdPost(
    commands.Cog
):

    def __init__(
        self,
        bot,
    ):
        self.bot = bot
        self.running = False

    @commands.command(
        name="findadpost",
        aliases=[
            "findadposter",
            "findadposters",
        ],
    )
    async def findadpost(
        self,
        ctx,
    ):
        """
        ,findadpost

        Scan every cached guild/channel and inspect only the latest 14 messages
        per channel for ad-posting/ad-poster language.
        """

        if self.running:
            return await safe_send(
                ctx,
                "⚠️ `,findadpost` is already running.",
            )

        self.running = True

        done_event = asyncio.Event()
        progress_task = None

        try:
            # ----------------------------------------------------
            # GUILDS
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # LOCAL CHANNEL INDEX
            # ----------------------------------------------------

            guild_channels = []

            total_channels = 0

            for guild in guilds:

                try:
                    channels = get_scannable_channels(
                        guild
                    )

                    total_channels += len(
                        channels
                    )

                    guild_channels.append(
                        (
                            guild,
                            channels,
                        )
                    )

                except Exception as e:
                    print(
                        "[findadpost] channel index error:",
                        getattr(
                            guild,
                            "name",
                            "Unknown",
                        ),
                        type(e).__name__,
                        e,
                    )

                    guild_channels.append(
                        (
                            guild,
                            [],
                        )
                    )

            # Larger guilds first only for usefulness.
            # It does NOT eliminate any guild.
            guild_channels.sort(
                key=lambda item: int(
                    getattr(
                        item[0],
                        "member_count",
                        0,
                    )
                    or 0
                ),
                reverse=True,
            )

            # ----------------------------------------------------
            # STATS / START MESSAGE
            # ----------------------------------------------------

            stats = {
                "started_at": time.monotonic(),

                "guilds_total": len(
                    guild_channels
                ),

                "guilds_done": 0,

                "current_guild": "starting",

                "channels_discovered": total_channels,

                "channels_done": 0,

                "channels_skipped": 0,

                "history_attempts": 0,

                "history_ok": 0,

                "history_timeouts": 0,

                "history_forbidden": 0,

                "history_http_errors": 0,

                "history_other_errors": 0,

                "messages_checked": 0,

                "matches_found": 0,

                "explicit_429s": 0,

                "guild_errors": 0,

                "consecutive_timeouts": 0,

                "invite_attempts": 0,

                "invite_ok": 0,

                "invite_timeouts": 0,

                "invite_errors": 0,
            }

            start_text = (
                "🔎 **FindAdPost started**\n"
                f"Servers: **{len(guild_channels)}**\n"
                f"Readable/cached text channels discovered: "
                f"**{total_channels}**\n"
                f"Checking latest **{LATEST_MESSAGES_PER_CHANNEL} messages** "
                "per channel.\n"
                "Looking for: `ad post`, `ad posting`, `ad poster`, "
                "`adposter`, `adp`, `lf adp`, `lf ap`, `hiring adposters`, "
                "`need ad posting`, and related wording.\n\n"
                "If one channel hits a long API/rate-limit wait, that history "
                "operation is timed out and skipped instead of intentionally "
                "waiting for the full retry period.\n"
                "Progress updates every **30 seconds**."
            )

            progress_message = await safe_send(
                ctx,
                start_text,
            )

            progress_message_holder = {
                "message": progress_message
            }

            progress_task = asyncio.create_task(
                progress_worker(
                    ctx,
                    stats,
                    done_event,
                    progress_message_holder,
                )
            )

            # ----------------------------------------------------
            # SCAN
            # ----------------------------------------------------

            raw_matches = []

            for guild, channels in guild_channels:

                stats[
                    "current_guild"
                ] = getattr(
                    guild,
                    "name",
                    "Unknown Server",
                )

                try:
                    for channel in channels:

                        # Local permission pre-check.
                        if not can_read_channel(
                            self.bot,
                            guild,
                            channel,
                        ):
                            stats[
                                "channels_done"
                            ] += 1

                            stats[
                                "channels_skipped"
                            ] += 1

                            continue

                        messages, status = await fetch_latest_messages_safe(
                            channel,
                            stats,
                        )

                        stats[
                            "channels_done"
                        ] += 1

                        if status != "ok":
                            # The fetch helper already counted skip/error.
                            continue

                        for message in messages:

                            try:
                                searchable_text = get_message_search_text(
                                    message
                                )

                                matched, reasons = detect_adpost_keywords(
                                    searchable_text
                                )

                                if not matched:
                                    continue

                                # Skip our own command/progress messages if they
                                # happen to contain examples like "ad posting".
                                try:
                                    if (
                                        self.bot.user
                                        and message.author.id
                                        == self.bot.user.id
                                    ):
                                        # We only skip our own messages if they
                                        # look like command/progress output.
                                        normalized = normalize_text(
                                            searchable_text
                                        )

                                        if (
                                            "findadpost"
                                            in normalized
                                            or "findadpost progress"
                                            in normalized
                                        ):
                                            continue
                                except Exception:
                                    pass

                                raw_matches.append(
                                    {
                                        "guild": guild,
                                        "guild_name": getattr(
                                            guild,
                                            "name",
                                            "Unknown Server",
                                        ),

                                        "channel": channel,

                                        "message": message,

                                        "username": get_author_display(
                                            message
                                        ),

                                        "reasons": reasons,

                                        "channel_link": get_channel_link(
                                            guild,
                                            channel,
                                        ),

                                        "message_link": get_message_link(
                                            message
                                        ),

                                        "preview": make_preview(
                                            searchable_text
                                        ),

                                        "server_invite": None,
                                    }
                                )

                                stats[
                                    "matches_found"
                                ] += 1

                            except Exception as e:
                                print(
                                    "[findadpost] message analysis error:",
                                    type(e).__name__,
                                    e,
                                )

                                continue

                except asyncio.CancelledError:
                    raise

                except Exception as e:
                    stats[
                        "guild_errors"
                    ] += 1

                    print(
                        "[findadpost] guild scan error:",
                        getattr(
                            guild,
                            "name",
                            "Unknown",
                        ),
                        type(e).__name__,
                        e,
                    )

                finally:
                    stats[
                        "guilds_done"
                    ] += 1

            # ----------------------------------------------------
            # SCAN FINISHED
            # ----------------------------------------------------

            done_event.set()

            if progress_task is not None:
                try:
                    await asyncio.wait_for(
                        progress_task,
                        timeout=2.0,
                    )

                except Exception:
                    progress_task.cancel()

            # ----------------------------------------------------
            # INVITES ONLY FOR MATCHED GUILDS
            # ----------------------------------------------------

            invite_cache = {}

            # Resolve once per guild.
            for match in raw_matches:

                guild = match[
                    "guild"
                ]

                guild_id = getattr(
                    guild,
                    "id",
                    None,
                )

                if guild_id in invite_cache:
                    match[
                        "server_invite"
                    ] = invite_cache[
                        guild_id
                    ][0]

                    continue

                invite_url, invite_type = await resolve_server_invite_safe(
                    self.bot,
                    guild,
                    match[
                        "channel"
                    ],
                    invite_cache,
                    stats,
                )

                match[
                    "server_invite"
                ] = invite_url

            # Fill invite for other matches in same guild after cache created.
            for match in raw_matches:

                guild_id = getattr(
                    match[
                        "guild"
                    ],
                    "id",
                    None,
                )

                cached = invite_cache.get(
                    guild_id
                )

                if cached:
                    match[
                        "server_invite"
                    ] = cached[
                        0
                    ]

            # ----------------------------------------------------
            # SORT MATCHES
            # ----------------------------------------------------
            #
            # Newest message first.
            # If timestamp is unavailable, use message ID fallback.
            # ----------------------------------------------------

            def match_sort_key(
                item,
            ):
                message = item[
                    "message"
                ]

                try:
                    created_at = getattr(
                        message,
                        "created_at",
                        None,
                    )

                    if created_at is not None:
                        return created_at.timestamp()
                except Exception:
                    pass

                try:
                    return int(
                        getattr(
                            message,
                            "id",
                            0,
                        )
                    )
                except Exception:
                    return 0

            raw_matches.sort(
                key=match_sort_key,
                reverse=True,
            )

            # ----------------------------------------------------
            # FINAL SUMMARY
            # ----------------------------------------------------

            elapsed = int(
                time.monotonic()
                - stats[
                    "started_at"
                ]
            )

            final_summary = (
                "✅ **FindAdPost finished**\n"
                f"Time: **{elapsed}s**\n"
                f"Servers processed: "
                f"**{stats['guilds_done']}/{stats['guilds_total']}**\n"
                f"Channels discovered: **{stats['channels_discovered']}**\n"
                f"Channels processed: **{stats['channels_done']}**\n"
                f"Channels skipped: **{stats['channels_skipped']}**\n"
                f"History calls: **{stats['history_attempts']}**\n"
                f"Successful histories: **{stats['history_ok']}**\n"
                f"History timeouts: **{stats['history_timeouts']}**\n"
                f"Forbidden histories: **{stats['history_forbidden']}**\n"
                f"HTTP history errors: **{stats['history_http_errors']}**\n"
                f"Messages checked: **{stats['messages_checked']}**\n"
                f"Explicit 429s seen: **{stats['explicit_429s']}**\n"
                f"Invite attempts: **{stats['invite_attempts']}**\n"
                f"Invite timeouts/errors: "
                f"**{stats['invite_timeouts'] + stats['invite_errors']}**\n"
                f"Matching messages found: **{len(raw_matches)}**"
            )

            # Replace progress message with final summary if possible.
            edited = await safe_edit(
                progress_message_holder[
                    "message"
                ],
                final_summary,
            )

            if not edited:
                await safe_send(
                    ctx,
                    final_summary,
                )

            if not raw_matches:
                return await safe_send(
                    ctx,
                    "No ad-poster/ad-posting keyword matches were found "
                    "in the latest 14 messages of the channels that were "
                    "successfully checked.",
                )

            # ----------------------------------------------------
            # RESULT OUTPUT
            # ----------------------------------------------------
            #
            # 5 matches per Discord message.
            # ----------------------------------------------------

            total = len(
                raw_matches
            )

            for start in range(
                0,
                total,
                RESULTS_PER_CHUNK,
            ):

                chunk = raw_matches[
                    start:
                    start
                    + RESULTS_PER_CHUNK
                ]

                pieces = []

                for offset, match in enumerate(
                    chunk
                ):

                    index = (
                        start
                        + offset
                        + 1
                    )

                    pieces.append(
                        format_match(
                            match,
                            index,
                        )
                    )

                text = (
                    f"📨 **Ad-posting matches "
                    f"{start + 1}-{min(start + len(chunk), total)} "
                    f"of {total}**\n\n"
                    + "\n".join(
                        pieces
                    )
                )

                # If 5 detailed matches exceed Discord's safe size,
                # split them one-by-one.
                if len(
                    text
                ) <= DISCORD_SAFE_MESSAGE_LENGTH:

                    await safe_send(
                        ctx,
                        text,
                    )

                else:
                    for offset, match in enumerate(
                        chunk
                    ):

                        index = (
                            start
                            + offset
                            + 1
                        )

                        individual = format_match(
                            match,
                            index,
                        )

                        if len(
                            individual
                        ) > DISCORD_SAFE_MESSAGE_LENGTH:

                            individual = (
                                individual[
                                    :DISCORD_SAFE_MESSAGE_LENGTH
                                    - 20
                                ]
                                + "\n…"
                            )

                        await safe_send(
                            ctx,
                            individual,
                        )

            await safe_send(
                ctx,
                "🏁 **All FindAdPost matches sent.**",
            )

        except asyncio.CancelledError:

            done_event.set()

            if (
                progress_task is not None
                and not progress_task.done()
            ):
                progress_task.cancel()

            try:
                await safe_send(
                    ctx,
                    "⚠️ `,findadpost` was cancelled.",
                )
            except Exception:
                pass

            raise

        except Exception as e:

            done_event.set()

            if (
                progress_task is not None
                and not progress_task.done()
            ):
                progress_task.cancel()

            print(
                "[findadpost] fatal error:",
                type(e).__name__,
                e,
            )

            try:
                await safe_send(
                    ctx,
                    "❌ **FindAdPost crashed:**\n"
                    f"`{type(e).__name__}: {e}`",
                )
            except Exception:
                pass

        finally:

            done_event.set()

            if (
                progress_task is not None
                and not progress_task.done()
            ):
                progress_task.cancel()

            self.running = False


# ============================================================
# SETUP
# ============================================================

async def setup(
    bot,
):
    await bot.add_cog(
        FindAdPost(
            bot
        )
    )
