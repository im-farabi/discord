# cogs/findnew.py

import os
import json
import re
import difflib
import unicodedata
import asyncio
import discord

from discord.ext import commands


# ============================================================
# CONFIG
# ============================================================

DATA_FILE = "/data/channels.json"

PREFIX = ","


# ============================================================
# CHANNEL-NAME PATTERNS
# ============================================================

# Strong patterns based on the channels you actually use.
#
# IMPORTANT:
# The name alone is NOT enough anymore.
# A channel must ALSO pass permissions + message-history checks.
CHANNEL_PATTERNS = [
    "yours",
    "your",
    "urs",
    "hubs",
    "sell",
    "clbs",
    "clb",
    "cllbs",
    "collabs",
    "collab",
    "promos",
    "promo",
    "shop",
    "reseller",
    "rslr",
    "resellers",
    "adv",
    "advertise",
    "self",
    "sponsors",
    "sponsor",
    "sponsorship",
]


# ============================================================
# FUZZY MATCH SETTINGS
# ============================================================

# Higher = stricter.
#
# 0.86 helps avoid weak matches such as completely unrelated
# words while still catching small variations.
FUZZY_THRESHOLD = 0.86


# ============================================================
# MESSAGE-HISTORY SETTINGS
# ============================================================

# Check up to this many recent messages from a candidate channel.
RECENT_MESSAGE_LIMIT = 8


# At least this many meaningful recent messages must exist.
#
# This prevents an empty/random channel from being accepted
# because its name happened to contain "promo".
MIN_RECENT_MESSAGES = 4


# At least this many recent messages must contain a Discord
# server invite.
MIN_INVITE_MESSAGES = 3


# 0.70 = at least 70% of recent meaningful messages should
# look like actual server advertisements.
MIN_INVITE_RATIO = 0.70


# Small delay between HISTORY requests.
#
# This is not required for correctness, but scanning many
# channels can generate many Discord API requests.
HISTORY_DELAY = 0.20


# ============================================================
# OUTPUT
# ============================================================

MAX_COMMAND_LENGTH = 1850


# ============================================================
# DISCORD INVITE DETECTION
# ============================================================

# Examples accepted:
#
# https://discord.gg/abc123
# http://discord.gg/abc123
# discord.gg/abc123
#
# https://discord.com/invite/abc123
# https://www.discord.com/invite/abc123
#
# https://discordapp.com/invite/abc123

DISCORD_INVITE_REGEX = re.compile(
    r"(?:https?://)?"
    r"(?:www\.)?"
    r"(?:"
    r"discord\.gg/"
    r"|"
    r"discord(?:app)?\.com/invite/"
    r")"
    r"[A-Za-z0-9_-]+",
    flags=re.IGNORECASE
)


# ============================================================
# DATA
# ============================================================

def default_data():

    return {
        "message": "",
        "channels": [],
        "auto": False,
        "next_run": None,
        "log_channel": None,
        "reverse": False
    }


def load_data():

    if not os.path.exists(DATA_FILE):

        return default_data()


    try:

        with open(
            DATA_FILE,
            "r",
            encoding="utf8"
        ) as fp:

            data = json.load(fp)


        data.setdefault(
            "message",
            ""
        )

        data.setdefault(
            "channels",
            []
        )

        data.setdefault(
            "auto",
            False
        )

        data.setdefault(
            "next_run",
            None
        )

        data.setdefault(
            "log_channel",
            None
        )

        data.setdefault(
            "reverse",
            False
        )


        return data


    except Exception as e:

        print(
            "[findnew] Failed loading data:",
            type(e).__name__,
            e
        )

        return default_data()


# ============================================================
# NORMALIZE CHANNEL NAME
# ============================================================

def normalize_name(name: str) -> str:

    if not name:

        return ""


    try:

        text = unicodedata.normalize(
            "NFKC",
            str(name)
        ).casefold()


        # Decorative symbols become spaces
        text = re.sub(
            r"[^\w]+",
            " ",
            text,
            flags=re.UNICODE
        )


        text = text.replace(
            "_",
            " "
        )


        text = re.sub(
            r"\s+",
            " ",
            text
        ).strip()


        return text


    except Exception:

        return str(
            name
        ).lower().strip()


# ============================================================
# CHANNEL NAME MATCH
# ============================================================

def channel_matches(name: str):

    normalized = normalize_name(
        name
    )


    if not normalized:

        return False, None


    tokens = [

        token

        for token in normalized.split()

        if len(token) >= 2

    ]


    # ========================================================
    # 1. EXACT TOKEN
    # ========================================================

    for pattern in CHANNEL_PATTERNS:

        if pattern in tokens:

            return (
                True,
                f"exact:{pattern}"
            )


    # ========================================================
    # 2. SUBSTRING
    # ========================================================

    compact = normalized.replace(
        " ",
        ""
    )


    for pattern in CHANNEL_PATTERNS:

        if pattern in compact:

            return (
                True,
                f"contains:{pattern}"
            )


    # ========================================================
    # 3. DIFFLIB FUZZY
    # ========================================================

    for token in tokens:

        if len(token) > 20:

            continue


        for pattern in CHANNEL_PATTERNS:

            # Don't compare wildly different lengths
            if abs(
                len(token)
                - len(pattern)
            ) > 3:

                continue


            ratio = difflib.SequenceMatcher(
                None,
                token,
                pattern
            ).ratio()


            if ratio >= FUZZY_THRESHOLD:

                return (
                    True,
                    (
                        f"fuzzy:"
                        f"{token}->{pattern}:"
                        f"{ratio:.2f}"
                    )
                )


    return False, None


# ============================================================
# TEXT CHANNEL CHECK
# ============================================================

def is_text_channel(channel):

    try:

        # We only scan normal Guild TextChannels here.
        #
        # This automatically avoids:
        #
        # categories
        # voice channels
        # stage channels
        # forums
        # etc.

        return isinstance(
            channel,
            discord.TextChannel
        )


    except Exception:

        return False


# ============================================================
# GET OUR MEMBER OBJECT
# ============================================================

def get_self_member(bot, guild):

    try:

        # Usually available
        member = getattr(
            guild,
            "me",
            None
        )


        if member is not None:

            return member


        # Fallback
        if bot.user:

            return guild.get_member(
                bot.user.id
            )


    except Exception:

        pass


    return None


# ============================================================
# PERMISSION CHECK
# ============================================================

def can_use_channel(bot, guild, channel):
    """
    Require:

    • view channel
    • read message history
    • send messages

    If ANY of these are unavailable:
        reject channel
    """

    try:

        member = get_self_member(
            bot,
            guild
        )


        if member is None:

            return (
                False,
                "member-not-found"
            )


        perms = channel.permissions_for(
            member
        )


        if not getattr(
            perms,
            "view_channel",
            False
        ):

            return (
                False,
                "cannot-view"
            )


        if not getattr(
            perms,
            "read_message_history",
            False
        ):

            return (
                False,
                "cannot-read-history"
            )


        if not getattr(
            perms,
            "send_messages",
            False
        ):

            return (
                False,
                "cannot-send"
            )


        return (
            True,
            "sendable"
        )


    except Exception as e:

        return (
            False,
            (
                f"permission-error:"
                f"{type(e).__name__}"
            )
        )


# ============================================================
# MESSAGE TEXT / EMBED EXTRACTION
# ============================================================

def get_message_search_text(message):
    """
    Build one searchable string from:

    • normal message content
    • embed URL
    • embed title
    • embed description
    • embed fields
    """

    pieces = []


    # ========================================================
    # NORMAL CONTENT
    # ========================================================

    try:

        if message.content:

            pieces.append(
                str(message.content)
            )

    except Exception:

        pass


    # ========================================================
    # EMBEDS
    # ========================================================

    try:

        for embed in message.embeds:

            # URL
            try:

                if embed.url:

                    pieces.append(
                        str(embed.url)
                    )

            except Exception:

                pass


            # Title
            try:

                if embed.title:

                    pieces.append(
                        str(embed.title)
                    )

            except Exception:

                pass


            # Description
            try:

                if embed.description:

                    pieces.append(
                        str(embed.description)
                    )

            except Exception:

                pass


            # Fields
            try:

                for field in embed.fields:

                    if field.name:

                        pieces.append(
                            str(field.name)
                        )

                    if field.value:

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
# INVITE CHECK
# ============================================================

def message_contains_discord_invite(message):

    try:

        text = get_message_search_text(
            message
        )


        if not text:

            return False


        return bool(
            DISCORD_INVITE_REGEX.search(
                text
            )
        )


    except Exception:

        return False


# ============================================================
# DETERMINE WHETHER MESSAGE IS MEANINGFUL
# ============================================================

def is_meaningful_message(message):

    try:

        # Ignore Discord system messages when possible.
        message_type = getattr(
            message,
            "type",
            None
        )


        default_type = getattr(
            discord.MessageType,
            "default",
            None
        )


        reply_type = getattr(
            discord.MessageType,
            "reply",
            None
        )


        if message_type not in {
            default_type,
            reply_type,
            None
        }:

            return False


        text = get_message_search_text(
            message
        ).strip()


        # Ignore completely blank messages
        if not text:

            return False


        return True


    except Exception:

        return False


# ============================================================
# ANALYZE RECENT CHANNEL HISTORY
# ============================================================

async def analyze_channel_history(channel):
    """
    A real promo/ad channel should have many recent messages
    containing Discord server invites.

    Returns dictionary:

    {
        passed: True/False,
        checked: X,
        invites: Y,
        ratio: 0.XX,
        reason: "..."
    }
    """

    meaningful = 0

    invite_messages = 0


    try:

        async for message in channel.history(
            limit=RECENT_MESSAGE_LIMIT
        ):

            if not is_meaningful_message(
                message
            ):

                continue


            meaningful += 1


            if message_contains_discord_invite(
                message
            ):

                invite_messages += 1


    except discord.Forbidden:

        return {
            "passed": False,
            "checked": 0,
            "invites": 0,
            "ratio": 0.0,
            "reason": "history-forbidden"
        }


    except discord.HTTPException as e:

        return {
            "passed": False,
            "checked": 0,
            "invites": 0,
            "ratio": 0.0,
            "reason": (
                f"history-http:"
                f"{getattr(e, 'status', 'unknown')}"
            )
        }


    except Exception as e:

        return {
            "passed": False,
            "checked": 0,
            "invites": 0,
            "ratio": 0.0,
            "reason": (
                f"history-error:"
                f"{type(e).__name__}"
            )
        }


    # ========================================================
    # TOO FEW MESSAGES
    # ========================================================

    if meaningful < MIN_RECENT_MESSAGES:

        return {
            "passed": False,
            "checked": meaningful,
            "invites": invite_messages,
            "ratio": (
                invite_messages / meaningful
                if meaningful
                else 0.0
            ),
            "reason": "too-few-messages"
        }


    # ========================================================
    # RATIO
    # ========================================================

    ratio = (
        invite_messages / meaningful
    )


    # ========================================================
    # MINIMUM INVITES
    # ========================================================

    if invite_messages < MIN_INVITE_MESSAGES:

        return {
            "passed": False,
            "checked": meaningful,
            "invites": invite_messages,
            "ratio": ratio,
            "reason": "too-few-invites"
        }


    # ========================================================
    # MINIMUM INVITE RATIO
    # ========================================================

    if ratio < MIN_INVITE_RATIO:

        return {
            "passed": False,
            "checked": meaningful,
            "invites": invite_messages,
            "ratio": ratio,
            "reason": "low-invite-ratio"
        }


    # ========================================================
    # SUCCESS
    # ========================================================

    return {
        "passed": True,
        "checked": meaningful,
        "invites": invite_messages,
        "ratio": ratio,
        "reason": "promo-history"
    }


# ============================================================
# BUILD MASS-SETC COMMANDS
# ============================================================

def build_mass_commands(
    channel_ids
):

    commands_to_send = []

    current = (
        f"{PREFIX}mass-setc"
    )


    for channel_id in channel_ids:

        addition = (
            f" {channel_id}"
        )


        if (
            len(current)
            + len(addition)
            > MAX_COMMAND_LENGTH
        ):

            commands_to_send.append(
                current
            )


            current = (
                f"{PREFIX}"
                f"mass-setc "
                f"{channel_id}"
            )


        else:

            current += addition


    if current != (
        f"{PREFIX}mass-setc"
    ):

        commands_to_send.append(
            current
        )


    return commands_to_send


# ============================================================
# COG
# ============================================================

class FindNew(commands.Cog):

    def __init__(
        self,
        bot
    ):

        self.bot = bot

        self.running = False


    # ========================================================
    # ,findnew
    # ========================================================

    @commands.command(
        name="findnew"
    )
    async def findnew(
        self,
        ctx
    ):

        # ====================================================
        # PREVENT TWO FINDNEW SCANS AT ONCE
        # ====================================================

        if self.running:

            return await ctx.send(
                "⚠️ `,findnew` is already running."
            )


        self.running = True


        try:

            # =================================================
            # START
            # =================================================

            await ctx.send(
                "🔎 **FindNew started**\n"
                "Scanning servers using:\n"
                "**name → permissions → recent ad history**"
            )


            # =================================================
            # LOAD FRESH CHANNEL DATABASE
            # =================================================

            data = load_data()


            existing_ids = set()


            for entry in data.get(
                "channels",
                []
            ):

                try:

                    existing_ids.add(
                        int(
                            entry["id"]
                        )
                    )

                except Exception:

                    continue


            # =================================================
            # RESULTS
            # =================================================

            found_ids = []

            found_set = set()

            found_details = []


            # =================================================
            # STATS
            # =================================================

            servers_scanned = 0

            channels_scanned = 0

            name_matches = 0

            already_saved = 0

            permission_rejected = 0

            history_checked = 0

            history_rejected = 0

            new_matches = 0

            errors = 0


            # =================================================
            # GET SERVERS
            # =================================================

            try:

                guilds = list(
                    self.bot.guilds
                )

            except Exception as e:

                return await ctx.send(
                    "❌ Couldn't access server list:\n"
                    f"`{type(e).__name__}: {e}`"
                )


            if not guilds:

                return await ctx.send(
                    "⚠️ No servers found."
                )


            # =================================================
            # SCAN ALL SERVERS
            # =================================================

            for guild in guilds:

                servers_scanned += 1


                try:

                    channels = list(
                        guild.text_channels
                    )

                except Exception as e:

                    errors += 1

                    print(
                        "[findnew] Could not read channels:",
                        getattr(
                            guild,
                            "name",
                            "Unknown"
                        ),
                        type(e).__name__,
                        e
                    )

                    continue


                # =============================================
                # SCAN SERVER CHANNELS
                # =============================================

                for channel in channels:

                    try:

                        channels_scanned += 1


                        # =====================================
                        # STAGE 1:
                        # CHANNEL TYPE
                        # =====================================

                        if not is_text_channel(
                            channel
                        ):

                            continue


                        channel_name = getattr(
                            channel,
                            "name",
                            ""
                        )


                        if not channel_name:

                            continue


                        # =====================================
                        # STAGE 2:
                        # NAME PATTERN
                        # =====================================

                        matched, name_reason = (
                            channel_matches(
                                channel_name
                            )
                        )


                        if not matched:

                            continue


                        name_matches += 1


                        try:

                            channel_id = int(
                                channel.id
                            )

                        except Exception:

                            continue


                        # =====================================
                        # STAGE 3:
                        # ALREADY SAVED
                        #
                        # Do this BEFORE API history calls.
                        # Saves unnecessary requests.
                        # =====================================

                        if channel_id in existing_ids:

                            already_saved += 1

                            continue


                        if channel_id in found_set:

                            continue


                        # =====================================
                        # STAGE 4:
                        # PERMISSIONS
                        # =====================================

                        usable, permission_reason = (
                            can_use_channel(
                                self.bot,
                                guild,
                                channel
                            )
                        )


                        if not usable:

                            permission_rejected += 1

                            continue


                        # =====================================
                        # STAGE 5:
                        # ACTUAL MESSAGE HISTORY
                        # =====================================

                        history_checked += 1


                        analysis = (
                            await analyze_channel_history(
                                channel
                            )
                        )


                        if not analysis[
                            "passed"
                        ]:

                            history_rejected += 1

                            await asyncio.sleep(
                                HISTORY_DELAY
                            )

                            continue


                        # =====================================
                        # REAL MATCH
                        # =====================================

                        found_set.add(
                            channel_id
                        )


                        found_ids.append(
                            channel_id
                        )


                        new_matches += 1


                        found_details.append(
                            {
                                "id": channel_id,

                                "guild": getattr(
                                    guild,
                                    "name",
                                    "Unknown Server"
                                ),

                                "channel": channel_name,

                                "name_reason": (
                                    name_reason
                                ),

                                "messages": (
                                    analysis[
                                        "checked"
                                    ]
                                ),

                                "invites": (
                                    analysis[
                                        "invites"
                                    ]
                                ),

                                "ratio": (
                                    analysis[
                                        "ratio"
                                    ]
                                )
                            }
                        )


                        # Small pause between API calls
                        await asyncio.sleep(
                            HISTORY_DELAY
                        )


                    except Exception as e:

                        errors += 1

                        print(
                            "[findnew] Channel error:",
                            getattr(
                                guild,
                                "name",
                                "Unknown Server"
                            ),
                            getattr(
                                channel,
                                "name",
                                "Unknown Channel"
                            ),
                            type(e).__name__,
                            e
                        )

                        continue


            # =================================================
            # NOTHING FOUND
            # =================================================

            if not found_ids:

                return await ctx.send(

                    "✅ **FindNew finished**\n\n"

                    f"Servers scanned: "
                    f"**{servers_scanned}**\n"

                    f"Channels scanned: "
                    f"**{channels_scanned}**\n"

                    f"Name candidates: "
                    f"**{name_matches}**\n"

                    f"Already saved: "
                    f"**{already_saved}**\n"

                    f"Rejected — can't send/read: "
                    f"**{permission_rejected}**\n"

                    f"History checked: "
                    f"**{history_checked}**\n"

                    f"Rejected — not enough "
                    f"real invite posts: "
                    f"**{history_rejected}**\n"

                    f"New real promo channels: "
                    f"**0**\n\n"

                    "No new channels passed all filters."
                )


            # =================================================
            # CREATE MASS SETC
            # =================================================

            commands_to_send = (
                build_mass_commands(
                    found_ids
                )
            )


            # =================================================
            # SUMMARY
            # =================================================

            await ctx.send(

                "✅ **FindNew finished**\n\n"

                f"Servers scanned: "
                f"**{servers_scanned}**\n"

                f"Channels scanned: "
                f"**{channels_scanned}**\n"

                f"Name candidates: "
                f"**{name_matches}**\n"

                f"Already saved: "
                f"**{already_saved}**\n"

                f"Rejected — can't send/read: "
                f"**{permission_rejected}**\n"

                f"History checked: "
                f"**{history_checked}**\n"

                f"Rejected — not real "
                f"promo history: "
                f"**{history_rejected}**\n"

                f"🔥 New real promo channels: "
                f"**{new_matches}**\n"

                f"`mass-setc` commands: "
                f"**{len(commands_to_send)}**"
            )


            # =================================================
            # PREVIEW FIRST 20
            # =================================================

            preview = []


            for item in found_details[
                :20
            ]:

                percentage = int(
                    item["ratio"]
                    * 100
                )


                preview.append(

                    f"• **{item['guild']}** / "
                    f"`#{item['channel']}`\n"

                    f"  `{item['id']}` — "

                    f"**{item['invites']}/"
                    f"{item['messages']}** "

                    f"recent messages contain "
                    f"Discord invites "
                    f"(**{percentage}%**)"

                )


            if preview:

                # Split preview because fancy names
                # can become very long.
                current = (
                    "🔍 **Verified preview:**\n"
                )


                for line in preview:

                    addition = (
                        line
                        + "\n"
                    )


                    if (
                        len(current)
                        + len(addition)
                        > 1850
                    ):

                        await ctx.send(
                            current
                        )

                        current = (
                            "🔍 **Preview continued:**\n"
                            + addition
                        )

                    else:

                        current += addition


                if current.strip():

                    await ctx.send(
                        current
                    )


            if len(found_details) > 20:

                await ctx.send(

                    f"ℹ️ Preview shows first "
                    f"**20** of "
                    f"**{len(found_details)}** "
                    f"verified channels."

                )


            # =================================================
            # MASS-SETC OUTPUT
            # =================================================

            await ctx.send(
                "📋 **Ready to paste:**"
            )


            for command_text in commands_to_send:

                await ctx.send(
                    f"```text\n"
                    f"{command_text}\n"
                    f"```"
                )


        # ====================================================
        # GLOBAL ERROR
        # ====================================================

        except asyncio.CancelledError:

            try:

                await ctx.send(
                    "⚠️ `,findnew` was cancelled."
                )

            except Exception:

                pass

            raise


        except Exception as e:

            print(
                "[findnew] Fatal error:",
                type(e).__name__,
                e
            )


            try:

                await ctx.send(
                    "❌ **FindNew crashed:**\n"
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
        FindNew(bot)
    )
