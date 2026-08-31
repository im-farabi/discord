# cogs/system.py

import asyncio
import json
import os
import logging
import time
import discord

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from discord.ext import commands


# ============================================================
# CONFIG
# ============================================================

DATA_FILE = "/data/channels.json"

CYCLE_HOURS = 5

# Fixed daily schedule in Bangladesh / Dhaka time.
DHAKA_TZ = ZoneInfo("Asia/Dhaka")

SCHEDULE_TIMES = (
    (7, 30),
    (12, 30),
    (18, 30),
    (22, 30),
)

SEND_DELAY = 10

RETRY_DELAY = 5

MAX_RETRIES = 0


# ============================================================
# BATCH SYSTEM
# ============================================================

BATCH_SIZE = 3


# Only here can ANYONE use:
#
# ,continue
# ,recontinue
#
CONTROL_CHANNEL_ID = 1533686357547946045


# If discord.py gets stuck internally waiting on a huge
# 429 retry-after, stop this command instead of allowing
# the coroutine to remain alive for hours.
#
# This DOES NOT bypass Discord's rate limit.
# It simply abandons this send attempt.
SEND_TIMEOUT = 30


# If an HTTPException actually reaches us with a retry_after
# longer than this, don't sit and sleep for hours.
MAX_INLINE_RETRY_WAIT = 30


# ============================================================
# HELPERS
# ============================================================

def utc_now() -> datetime:

    return datetime.now(
        timezone.utc
    )


def dhaka_now() -> datetime:

    return datetime.now(
        DHAKA_TZ
    )


def next_schedule_slot(
    now: datetime | None = None
) -> datetime:

    """
    Return the next fixed Bangladesh-time schedule slot.

    Daily slots:
        07:30
        12:30
        18:30
        22:30
    """

    if now is None:

        now = dhaka_now()

    else:

        now = now.astimezone(
            DHAKA_TZ
        )


    for hour, minute in SCHEDULE_TIMES:

        candidate = now.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0
        )

        if candidate > now:

            return candidate


    # All four slots for today have passed.
    # Return tomorrow's first slot.
    tomorrow = (
        now
        +
        timedelta(
            days=1
        )
    )

    return tomorrow.replace(
        hour=SCHEDULE_TIMES[0][0],
        minute=SCHEDULE_TIMES[0][1],
        second=0,
        microsecond=0
    )


def schedule_slot_text(
    slot: datetime
) -> str:

    return slot.astimezone(
        DHAKA_TZ
    ).strftime(
        "%H:%M"
    )


def default_data():

    return {

        "message": "",

        "channels": [],

        "auto": False,

        "next_run": None,

        "log_channel": None,

        "reverse": False,


        # ====================================================
        # BATCH STATE
        # ====================================================

        "batch_active": False,

        # Exact next channel index
        "batch_index": 0,

        # Snapshot of channel IDs for THIS cycle
        "batch_channel_ids": [],

        # Snapshot of promo message for THIS cycle
        "batch_message": None,

        "batch_reverse": False,

        "batch_started_at": None,


        # ====================================================
        # RECONTINUE STATE
        # ====================================================

        # Beginning of current 3-channel block.
        #
        # Example:
        #
        # batch 4-6
        # batch_attempt_start = 3
        #
        # Legacy batch marker kept for backwards-compatible
        # channels.json files.  Current ,recontinue resumes from
        # the exact first unsent channel after reloading all cogs.
        #
        "batch_attempt_start": 0,

        "batch_last_error": None
    }


# ============================================================
# LOAD DATA
# ============================================================

def load_data() -> dict:

    if not os.path.exists(
        DATA_FILE
    ):

        return default_data()


    with open(
        DATA_FILE,
        encoding="utf8"
    ) as fp:

        data = json.load(
            fp
        )


    # Backwards compatibility with your old channels.json.
    defaults = default_data()


    for key, value in defaults.items():

        data.setdefault(
            key,
            value
        )


    return data


# ============================================================
# SAVE DATA
# ============================================================

def save_data(
    data: dict
):

    folder = os.path.dirname(
        DATA_FILE
    )


    if folder:

        os.makedirs(
            folder,
            exist_ok=True
        )


    with open(
        DATA_FILE,
        "w",
        encoding="utf8"
    ) as fp:

        json.dump(

            data,

            fp,

            indent=2,

            ensure_ascii=False
        )


# ============================================================
# DISCORD HTTP LOG FORWARDER
# ============================================================

class DiscordRateLimitHandler(
    logging.Handler
):

    """
    Watches discord.http logs.

    When discord.py prints something such as:

        We are being rate limited...
        responded with 429...
        Retrying in 5793 seconds

    mirror that warning into the Discord control/log channel.
    """

    def __init__(
        self,
        cog
    ):

        super().__init__(
            level=logging.WARNING
        )

        self.cog = cog

        self.last_message = None

        self.last_time = 0


    def emit(
        self,
        record
    ):

        try:

            message = record.getMessage()


            lower = message.lower()


            if (
                "rate limit" not in lower
                and
                "429" not in lower
            ):

                return


            # Avoid recursive warning loops while we're
            # forwarding a rate-limit warning.
            if self.cog._forwarding_rate_log:

                return


            # Basic duplicate protection.
            now = time.monotonic()


            if (
                message == self.last_message
                and
                now - self.last_time < 5
            ):

                return


            self.last_message = message

            self.last_time = now


            loop = self.cog.bot.loop


            if loop.is_closed():

                return


            self.cog._forwarding_rate_log = True


            loop.call_soon_threadsafe(

                lambda: asyncio.create_task(

                    self.cog._forward_rate_warning(
                        message
                    )

                )

            )


        except Exception:

            pass


# ============================================================
# COG
# ============================================================

class AutoPromo(
    commands.Cog
):

    """
    Auto Promo System

    ,start
        start forward

    ,startb
        start reverse

    Each invocation sends maximum 3 channels.

    ,continue
        next batch

    ,recontinue
        replay the current/failed batch

    The command DOES NOT wait around for the next command.
    """

    def __init__(
        self,
        bot: commands.Bot
    ):

        self.bot = bot

        self.data = load_data()

        self.loop_task: (
            asyncio.Task | None
        ) = None


        # Prevent two batches from running simultaneously.
        self.batch_lock = asyncio.Lock()

        # The Discord command task currently executing a batch.
        # ,recontinue may cancel this task, reload all cogs, then
        # resume from the exact first unsent channel saved on disk.
        self.active_batch_task = None


        # Rate-limit logger recursion guard
        self._forwarding_rate_log = False


        # ====================================================
        # ATTACH RATE-LIMIT LOG MIRROR
        # ====================================================

        self.http_logger = logging.getLogger(
            "discord.http"
        )


        self.rate_handler = (
            DiscordRateLimitHandler(
                self
            )
        )


        self.http_logger.addHandler(
            self.rate_handler
        )


        # ====================================================
        # RESTART RECOVERY
        # ====================================================
        #
        # Case 1:
        #
        # Halfway through 70-channel cycle
        #
        # batch_active = true
        #
        # Railway restart
        #
        # DO NOTHING automatically.
        #
        # Wait for:
        #
        # ,continue
        #
        #
        # Case 2:
        #
        # Full cycle already completed
        # and next_run exists
        #
        # Restart scheduler.
        # ====================================================

        if (

            self.data.get(
                "auto"
            )

            and

            not self.data.get(
                "batch_active"
            )

            and

            self.data.get(
                "next_run"
            )

        ):

            self._schedule_loop()


    # ========================================================
    # RATE LIMIT FORWARDING
    # ========================================================

    async def _forward_rate_warning(
        self,
        warning
    ):

        try:

            # Keep Discord message under limit
            if len(warning) > 1500:

                warning = warning[:1500]


            await self._log(

                "🚨 **Discord rate limit detected**\n"
                f"```text\n{warning}\n```\n"
                "The active send may be stopped by the "
                "batch timeout instead of waiting for hours.\n"
                "Do **not** repeatedly retry immediately; "
                "Discord's rate limit still applies."

            )


        except Exception as e:

            print(
                "Rate warning forward failed:",
                type(e).__name__,
                e
            )


        finally:

            self._forwarding_rate_log = False


    # ========================================================
    # LOG CHANNEL
    # ========================================================

    def _get_log_channel(
        self
    ):

        # First try the saved log channel
        log_ch = self.bot.get_channel(

            self.data.get(
                "log_channel",
                0
            )

        )


        if log_ch is not None:

            return log_ch


        # Then try your dedicated control channel
        control = self.bot.get_channel(
            CONTROL_CHANNEL_ID
        )


        if control is not None:

            return control


        # Final fallback
        for guild in self.bot.guilds:

            if guild.text_channels:

                return guild.text_channels[
                    0
                ]


        return None


    async def _log(
        self,
        message
    ):

        log_ch = (
            self._get_log_channel()
        )


        if not log_ch:

            return


        try:

            await log_ch.send(
                message
            )


        except Exception as e:

            print(
                "AutoPromo log error:",
                type(e).__name__,
                e
            )


    # ========================================================
    # CONTROL CHANNEL CHECK
    # ========================================================

    def _in_control_channel(
        self,
        channel
    ):

        try:

            return (
                channel.id
                ==
                CONTROL_CHANNEL_ID
            )


        except Exception:

            return False


    # ========================================================
    # FIND CHANNEL ENTRY
    # ========================================================

    def _find_channel_entry(
        self,
        channel_id
    ):

        for entry in self.data.get(
            "channels",
            []
        ):

            try:

                if int(
                    entry["id"]
                ) == int(
                    channel_id
                ):

                    return entry


            except Exception:

                continue


        return None


    # ========================================================
    # PREPARE NEW CYCLE
    # ========================================================

    def _prepare_new_cycle(
        self,
        reverse: bool,
        preserve_next_run: bool = False
    ):

        self.data = load_data()


        order = list(

            self.data.get(
                "channels",
                []
            )

        )


        if reverse:

            order.reverse()


        channel_ids = []


        for entry in order:

            try:

                channel_ids.append(

                    int(
                        entry["id"]
                    )

                )


            except Exception:

                continue


        # ====================================================
        # SNAPSHOT THIS PARTICULAR CYCLE
        # ====================================================

        self.data[
            "batch_active"
        ] = True


        self.data[
            "batch_index"
        ] = 0


        self.data[
            "batch_attempt_start"
        ] = 0


        self.data[
            "batch_channel_ids"
        ] = channel_ids


        self.data[
            "batch_message"
        ] = self.data.get(
            "message",
            ""
        )


        self.data[
            "batch_reverse"
        ] = reverse


        self.data[
            "batch_started_at"
        ] = utc_now().isoformat()


        self.data[
            "batch_last_error"
        ] = None


        # For a normal scheduled cycle, there is no automatic
        # next_run while this cycle is between batches.
        #
        # For ",start now" we preserve the already-calculated
        # upcoming fixed Bangladesh-time slot so finishing this
        # immediate cycle can still use that exact scheduled time.
        if not preserve_next_run:

            self.data[
                "next_run"
            ] = None


        save_data(
            self.data
        )


    # ========================================================
    # START COMMON
    # ========================================================

    async def _common_start(
        self,
        ctx: commands.Context,
        reverse: bool
    ):

        self.data = load_data()


        if not self.data.get(
            "message"
        ):

            return await ctx.send(

                "⚠️ Set a message first "
                "with `,setm …`."

            )


        if not self.data.get(
            "channels"
        ):

            return await ctx.send(

                "⚠️ Add promo channels "
                "with `,setc …` first."

            )


        if self.data.get(
            "batch_active"
        ):

            current = int(

                self.data.get(
                    "batch_index",
                    0
                )

            )


            total = len(

                self.data.get(
                    "batch_channel_ids",
                    []
                )

            )


            return await ctx.send(

                "ℹ️ A promo cycle is already active.\n"

                f"Progress: **{current}/{total}**\n\n"

                f"Use `,continue` in "
                f"<#{CONTROL_CHANNEL_ID}>."

            )


        if self.data.get(
            "auto"
        ):

            return await ctx.send(

                "ℹ️ Auto-cycle is already running."

            )


        # ====================================================
        # FIXED BANGLADESH-TIME START CHOICE
        # ====================================================

        closest_slot = next_schedule_slot()

        closest_text = schedule_slot_text(
            closest_slot
        )


        await ctx.send(

            f"⏰ Your next scheduled start is "
            f"**{closest_text} Bangladesh time**.\n\n"
            f"Do you want to start **now** too?\n"
            f"`y` = start now, then continue on the fixed schedule\n"
            f"`n` = do not start now; wait for **{closest_text}**"

        )


        def answer_check(
            message
        ):

            return (

                message.author.id
                ==
                ctx.author.id

                and

                message.channel.id
                ==
                ctx.channel.id

                and

                (
                    message.content
                    or ""
                ).strip().lower()
                in {
                    "y",
                    "n"
                }

            )


        answer = await self.bot.wait_for(

            "message",

            check=answer_check

        )


        choice = (
            answer.content
            or ""
        ).strip().lower()


        # Recalculate after the answer in case the user happened
        # to answer after the previously displayed slot passed.
        if closest_slot <= dhaka_now():

            closest_slot = next_schedule_slot()

            closest_text = schedule_slot_text(
                closest_slot
            )


        self.data = load_data()


        # Re-check state after waiting for y/n so another command
        # cannot accidentally create a second active scheduler/cycle.
        if self.data.get(
            "batch_active"
        ):

            current = int(

                self.data.get(
                    "batch_index",
                    0
                )

            )

            total = len(

                self.data.get(
                    "batch_channel_ids",
                    []
                )

            )

            return await ctx.send(

                "ℹ️ A promo cycle became active while waiting "
                "for your answer.\n"
                f"Progress: **{current}/{total}**"

            )


        if self.data.get(
            "auto"
        ):

            return await ctx.send(

                "ℹ️ Auto-cycle became active while waiting "
                "for your answer."

            )


        self.data[
            "auto"
        ] = True


        self.data[
            "log_channel"
        ] = ctx.channel.id


        self.data[
            "reverse"
        ] = reverse


        # Store the upcoming fixed slot as UTC ISO, exactly like
        # the old scheduler stored timezone-aware next_run values.
        self.data[
            "next_run"
        ] = closest_slot.astimezone(
            timezone.utc
        ).isoformat()


        save_data(
            self.data
        )


        # ====================================================
        # n = WAIT FOR CLOSEST FIXED SLOT
        # ====================================================

        if choice == "n":

            await ctx.send(

                f"✅ Auto-cycle scheduled "
                f"({'reverse' if reverse else 'forward'}).\n"
                f"First cycle will start at "
                f"**{closest_text} Bangladesh time**."

            )


            self._schedule_loop()

            return


        # ====================================================
        # y = START NOW + KEEP CLOSEST FIXED SLOT
        # ====================================================

        self._prepare_new_cycle(

            reverse,

            preserve_next_run=True

        )


        await ctx.send(

            f"✅ Auto-cycle started "
            f"({'reverse' if reverse else 'forward'}) "
            f"— first **{BATCH_SIZE}** channels now.\n"
            f"Next fixed scheduled start: "
            f"**{closest_text} Bangladesh time**."

        )


        # ====================================================
        # IMPORTANT
        #
        # Executes first 3.
        #
        # _run_next_batch RETURNS afterward.
        #
        # There is NO wait_for() for ,continue.
        # There is NO command waiting for ,continue.
        # ====================================================

        await self._execute_batch()


    # ========================================================
    # ,start
    # ========================================================

    @commands.command()
    async def start(
        self,
        ctx
    ):

        """Process channels first→last."""

        await self._common_start(

            ctx,

            reverse=False

        )


    # ========================================================
    # ,startb
    # ========================================================

    @commands.command()
    async def startb(
        self,
        ctx
    ):

        """Process channels last→first."""

        await self._common_start(

            ctx,

            reverse=True

        )


    # ========================================================
    # EXECUTE / TRACK ONE BATCH COMMAND
    # ========================================================

    async def _execute_batch(
        self
    ):

        task = asyncio.current_task()

        self.active_batch_task = task

        try:

            await self._run_next_batch()

        finally:

            if self.active_batch_task is task:

                self.active_batch_task = None


    # ========================================================
    # RECONTINUE RELOAD SUPERVISOR
    # ========================================================

    async def _recontinue_reload_supervisor(
        self,
        control_channel_id
    ):

        """
        Reload every .py extension in ./cogs, with this system cog
        reloaded LAST.

        IMPORTANT:
        This method is launched as its own asyncio task before the
        old ,recontinue command returns.  It never resumes a batch
        through the old AutoPromo instance after cogs.system has
        been reloaded.

        Batch progress is already persisted in /data/channels.json.
        The brand-new AutoPromo cog reloads that state and resumes
        from the exact saved batch_index.
        """

        bot = self.bot

        # Let the old ,recontinue callback fully return before
        # unloading/reloading its cog.
        await asyncio.sleep(
            0.10
        )

        cogs_dir = os.path.dirname(
            os.path.abspath(
                __file__
            )
        )

        if not os.path.isdir(
            cogs_dir
        ):

            return

        extensions = []

        for filename in sorted(
            os.listdir(
                cogs_dir
            )
        ):

            if (
                not filename.endswith(
                    ".py"
                )
                or
                filename.startswith(
                    "_"
                )
            ):

                continue

            name = filename[:-3]

            extensions.append(
                f"cogs.{name}"
            )


        # Reload this extension LAST so the supervisor is not
        # replacing its own cog until every other cog is done.
        this_extension = __name__

        extensions = [
            ext
            for ext in extensions
            if ext != this_extension
        ]

        if this_extension in {
            f"cogs.{os.path.splitext(os.path.basename(__file__))[0]}",
            __name__
        }:

            extensions.append(
                this_extension
            )


        reload_errors = []

        for extension in extensions:

            try:

                if extension in bot.extensions:

                    await bot.reload_extension(
                        extension
                    )

                else:

                    await bot.load_extension(
                        extension
                    )

            except Exception as e:

                reload_errors.append(
                    (
                        extension,
                        type(e).__name__,
                        str(e)
                    )
                )

                print(
                    "Cog reload error:",
                    extension,
                    type(e).__name__,
                    e
                )


        # NEVER continue on the old self after system reload.
        new_cog = bot.get_cog(
            "AutoPromo"
        )

        control = bot.get_channel(
            control_channel_id
        )

        if new_cog is None:

            if control is not None:

                try:

                    await control.send(
                        "❌ Recontinue reloaded the cogs, but "
                        "the new AutoPromo cog could not be found."
                    )

                except Exception:

                    pass

            return


        # Load the exact persisted state created by the old cog.
        new_cog.data = load_data()

        if not new_cog.data.get(
            "auto"
        ):

            return

        if not new_cog.data.get(
            "batch_active"
        ):

            return


        if reload_errors and control is not None:

            try:

                await control.send(
                    "⚠️ Recontinue finished the reload, but "
                    f"**{len(reload_errors)}** cog(s) had reload "
                    "errors. AutoPromo will still resume from "
                    "the saved position."
                )

            except Exception:

                pass


        # Fresh cog instance + fresh lock + persisted batch_index.
        await new_cog._execute_batch()


    # ========================================================
    # INTERNAL CONTINUE
    # ========================================================

    async def _handle_continue(
        self,
        ctx,
        *,
        replay=False
    ):

        # ====================================================
        # ONLY THIS CHANNEL
        # ====================================================

        if not self._in_control_channel(
            ctx.channel
        ):

            return


        # ====================================================
        # RECONTINUE = FORCE CURRENT BATCH TO STOP
        # ====================================================

        if self.batch_lock.locked():

            if not replay:

                return await ctx.send(

                    "⚠️ A 3-channel batch is "
                    "already processing."

                )


            active = self.active_batch_task

            if (
                active
                and
                not active.done()
                and
                active is not asyncio.current_task()
            ):

                active.cancel()

                try:

                    await active

                except asyncio.CancelledError:

                    pass

                except Exception:

                    pass


            # Give the cancelled batch a chance to release its lock.
            for _ in range(20):

                if not self.batch_lock.locked():

                    break

                await asyncio.sleep(
                    0.05
                )


        self.data = load_data()


        if not self.data.get(
            "auto"
        ):

            return await ctx.send(

                "ℹ️ Auto-cycle isn't running."

            )


        if not self.data.get(
            "batch_active"
        ):

            return await ctx.send(

                "ℹ️ There is no unfinished cycle."

            )


        total = len(

            self.data.get(
                "batch_channel_ids",
                []
            )

        )


        current = int(

            self.data.get(
                "batch_index",
                0
            )

        )


        # ====================================================
        # RECONTINUE
        #
        # Different from ,continue:
        #
        # 1. Force-stop a currently running batch if needed.
        # 2. KEEP the exact first unsent channel.
        # 3. Reload ALL cogs.
        # 4. Resume from that exact saved channel.
        #
        # Successful channels are NEVER replayed.
        # ====================================================

        if replay:

            await ctx.send(

                "🔁 **Recontinue**\n"
                "Force-stopped the old batch if needed.\n"
                f"Saved next channel: **{current + 1}**.\n"
                "Reloading **all cogs** now, then the fresh "
                "AutoPromo cog will resume from that saved "
                "channel."

            )


            # IMPORTANT:
            # Do NOT reload cogs inline and then keep executing
            # through this old self.  Launch an independent
            # supervisor and RETURN from the old command first.
            #
            # All batch progress is already persisted in
            # /data/channels.json, so the new AutoPromo instance
            # can recover the exact batch_index after reload.
            asyncio.create_task(

                self._recontinue_reload_supervisor(
                    CONTROL_CHANNEL_ID
                )

            )

            return


        # ====================================================
        # NORMAL CONTINUE
        # ====================================================

        await ctx.send(

            "▶️ **Continue**\n"
            f"Resuming from channel "
            f"**{current + 1}**.\n"
            f"Sending maximum **{BATCH_SIZE}** channels."

        )


        # New command execution.
        await self._execute_batch()


    # ========================================================
    # ,continue
    # ========================================================

    @commands.command(
        name="continue"
    )
    async def continue_cycle(
        self,
        ctx
    ):

        """
        For your own message.

        Other users are handled by on_message below.
        """

        # Wrong channel = no action.
        if not self._in_control_channel(
            ctx.channel
        ):

            return


        # If message is from somebody else,
        # listener below handles it.
        #
        # This prevents duplicate execution when
        # commands.process_commands also sees it.
        if (

            self.bot.user

            and

            ctx.author.id
            !=
            self.bot.user.id

        ):

            return


        await self._handle_continue(

            ctx,

            replay=False

        )


    # ========================================================
    # ,recontinue
    # ========================================================

    @commands.command(
        name="recontinue"
    )
    async def recontinue_cycle(
        self,
        ctx
    ):

        """
        Replay current 3-channel block.
        """

        if not self._in_control_channel(
            ctx.channel
        ):

            return


        if (

            self.bot.user

            and

            ctx.author.id
            !=
            self.bot.user.id

        ):

            return


        await self._handle_continue(

            ctx,

            replay=True

        )


    # ========================================================
    # LISTENER
    #
    # This allows OTHER PEOPLE to trigger these two commands
    # in your dedicated channel even if main.py has your
    # normal "only respond to myself" global command check.
    # ========================================================

    @commands.Cog.listener()
    async def on_message(
        self,
        message: discord.Message
    ):

        try:

            # Only dedicated channel
            if message.channel.id != (
                CONTROL_CHANNEL_ID
            ):

                return


            # Your own messages are handled through
            # the normal command framework.
            if (

                self.bot.user

                and

                message.author.id
                ==
                self.bot.user.id

            ):

                return


            content = (
                message.content
                or ""
            ).strip().lower()


            if content not in {

                ",continue",

                ",recontinue"

            }:

                return


            # Build context manually.
            ctx = await self.bot.get_context(
                message
            )


            if content == ",continue":

                await self._handle_continue(

                    ctx,

                    replay=False

                )


            elif content == ",recontinue":

                await self._handle_continue(

                    ctx,

                    replay=True

                )


        except Exception as e:

            print(

                "External continue listener error:",

                type(e).__name__,

                e

            )


    # ========================================================
    # ,stop
    # ========================================================

    @commands.command()
    async def stop(
        self,
        ctx
    ):

        # Always reload newest JSON
        self.data = load_data()


        if not self.data.get(
            "auto"
        ):

            return await ctx.send(

                "ℹ️ Auto-cycle isn’t running."

            )


        self.data[
            "auto"
        ] = False


        self.data[
            "next_run"
        ] = None


        self.data[
            "batch_active"
        ] = False


        self.data[
            "batch_index"
        ] = 0


        self.data[
            "batch_attempt_start"
        ] = 0


        self.data[
            "batch_channel_ids"
        ] = []


        self.data[
            "batch_message"
        ] = None


        self.data[
            "batch_started_at"
        ] = None


        self.data[
            "batch_last_error"
        ] = None


        save_data(
            self.data
        )


        if (

            self.loop_task

            and

            not self.loop_task.done()

        ):

            self.loop_task.cancel()


        await ctx.send(

            "🛑 Auto-cycle stopped."

        )


    # ========================================================
    # SCHEDULING
    # ========================================================

    def _schedule_loop(
        self
    ):

        if (

            self.loop_task

            and

            not self.loop_task.done()

        ):

            self.loop_task.cancel()


        self.loop_task = (

            self.bot.loop.create_task(

                self._loop()

            )

        )


    # ========================================================
    # FIXED BANGLADESH-TIME SCHEDULER
    # ========================================================

    async def _loop(
        self
    ):

        try:

            self.data = load_data()


            if not self.data.get(
                "auto"
            ):

                return


            # Half-finished batch exists.
            #
            # Do NOT automatically continue.
            if self.data.get(
                "batch_active"
            ):

                return


            next_run = self.data.get(
                "next_run"
            )


            if not next_run:

                return


            delay = max(

                0,

                (

                    datetime.fromisoformat(
                        next_run
                    )

                    -

                    utc_now()

                ).total_seconds()

            )


            await asyncio.sleep(
                delay
            )


            # Fresh state after sleeping
            self.data = load_data()


            if not self.data.get(
                "auto"
            ):

                return


            if self.data.get(
                "batch_active"
            ):

                return


            reverse = bool(

                self.data.get(
                    "reverse",
                    False
                )

            )


            self._prepare_new_cycle(
                reverse
            )


            await self._log(

                f"⏰ Scheduled cycle started "
                f"({'reverse' if reverse else 'forward'}) "
                f"— sending first "
                f"**{BATCH_SIZE}** channels."

            )


            # =================================================
            # FIRST 3 ONLY
            #
            # Then _loop itself ends.
            # =================================================

            await self._execute_batch()


            return


        except asyncio.CancelledError:

            pass


        except Exception as e:

            print(

                "AutoPromo loop error:",

                type(e).__name__,

                e

            )


    # ========================================================
    # SAVE CURRENT ERROR WITHOUT DESTROYING NEWER JSON DATA
    # ========================================================

    def _save_batch_error(
        self,
        position,
        error_text
    ):

        self.data = load_data()

        # A failed channel is considered processed/skipped.
        #
        # Never trap ,continue on the same bad server.
        # Example:
        # channel 26 fails -> batch_index becomes 26 (zero-based
        # next position = human channel 27).
        self.data[
            "batch_index"
        ] = position + 1


        self.data[
            "batch_last_error"
        ] = error_text


        save_data(
            self.data
        )


    # ========================================================
    # RUN ONE 3-CHANNEL BATCH
    # ========================================================

    async def _run_next_batch(
        self
    ):

        """
        IMPORTANT ARCHITECTURE:

        ONE invocation
            ↓
        sends max 3
            ↓
        saves position
            ↓
        RETURNS

        There is NO:

            wait_for(",continue")

        There is NO:

            while waiting for user

        There is NO sleeping task waiting for the next batch.

        ,continue is an entirely new Discord command execution.
        """

        async with self.batch_lock:

            self.data = load_data()


            if not self.data.get(
                "auto"
            ):

                return


            if not self.data.get(
                "batch_active"
            ):

                return


            channel_ids = list(

                self.data.get(
                    "batch_channel_ids",
                    []
                )

            )


            total = len(
                channel_ids
            )


            index = int(

                self.data.get(
                    "batch_index",
                    0
                )

            )


            promo = (

                self.data.get(
                    "batch_message"
                )

                or

                self.data.get(
                    "message",
                    ""
                )

            )


            reverse = bool(

                self.data.get(
                    "batch_reverse",
                    False
                )

            )


            # =================================================
            # INVALID STATE
            # =================================================

            if total == 0:

                self.data[
                    "batch_active"
                ] = False


                self.data[
                    "batch_index"
                ] = 0


                self.data[
                    "batch_attempt_start"
                ] = 0


                self.data[
                    "batch_channel_ids"
                ] = []


                save_data(
                    self.data
                )


                await self._log(

                    "⚠️ Batch contained no channels."

                )


                return


            if index >= total:

                await self._finish_cycle(
                    total
                )

                return


            # =================================================
            # DEFINE THIS EXACT BLOCK
            # =================================================

            start_index = index


            end_index = min(

                start_index
                +
                BATCH_SIZE,

                total

            )


            # Store beginning of THIS command's batch.
            #
            # This is what ,recontinue will return to.
            self.data[
                "batch_attempt_start"
            ] = start_index


            self.data[
                "batch_last_error"
            ] = None


            save_data(
                self.data
            )


            await self._log(

                f"▶️ Batch started "
                f"({'reverse' if reverse else 'forward'})\n"
                f"Channels "
                f"**{start_index + 1}-{end_index}** "
                f"of **{total}**."

            )


            # =================================================
            # PROCESS MAXIMUM 3
            # =================================================

            for position in range(

                start_index,

                end_index

            ):

                channel_id = (
                    channel_ids[
                        position
                    ]
                )


                # Refresh disk before each channel
                self.data = load_data()


                chan = self.bot.get_channel(
                    channel_id
                )


                entry = (
                    self._find_channel_entry(
                        channel_id
                    )
                )


                # =============================================
                # MISSING CHANNEL
                # =============================================

                if chan is None:

                    channel_name = (

                        entry.get(
                            "channel_name",
                            str(channel_id)
                        )

                        if entry

                        else

                        str(channel_id)

                    )


                    await self._log(

                        f"⏩ `{channel_name}` missing"

                    )


                    # Missing = processed/skipped.
                    self.data[
                        "batch_index"
                    ] = position + 1


                    save_data(
                        self.data
                    )


                    continue


                # =============================================
                # SEND — EXACTLY ONE ATTEMPT
                # =============================================

                sent = False

                try:

                    # discord.py can internally wait on a 429.
                    # This timeout prevents one bad server from
                    # holding the command for hours.
                    await asyncio.wait_for(

                        chan.send(
                            promo
                        ),

                        timeout=SEND_TIMEOUT

                    )


                    # =========================================
                    # SUCCESS
                    # =========================================

                    self.data = load_data()


                    entry = (
                        self._find_channel_entry(
                            channel_id
                        )
                    )


                    if entry is not None:

                        entry[
                            "last_sent"
                        ] = (
                            utc_now().isoformat()
                        )


                    self.data[
                        "batch_index"
                    ] = position + 1


                    self.data[
                        "batch_last_error"
                    ] = None


                    save_data(
                        self.data
                    )


                    await self._log(

                        f"✅ [1] "
                        f"{chan.guild.name}/"
                        f"#{chan.name}"

                    )


                    sent = True


                # =============================================
                # SEND TIMEOUT / INTERNAL 429 WAIT
                # =============================================

                except asyncio.TimeoutError:

                    error_text = (

                        f"Send timed out after "
                        f"{SEND_TIMEOUT}s on "
                        f"{chan.guild.name}/"
                        f"#{chan.name}"

                    )


                    # IMPORTANT:
                    # Failed channel is SKIPPED, not retried.
                    self._save_batch_error(

                        position,

                        error_text

                    )


                    await self._log(

                        "⏩ **Skipped failed channel**\n"
                        f"Channel: **{position + 1}/{total}**\n"
                        f"Server: **{chan.guild.name}**\n"
                        f"Channel: `#{chan.name}`\n"
                        f"Reason: send timed out after "
                        f"**{SEND_TIMEOUT}s**.\n"
                        "No retry. Moving on."

                    )


                # =============================================
                # DISCORD HTTP ERROR
                # =============================================

                except discord.HTTPException as e:

                    status = getattr(
                        e,
                        "status",
                        None
                    )

                    retry_after = getattr(
                        e,
                        "retry_after",
                        None
                    )


                    if status == 429:

                        if retry_after is not None:

                            error_text = (

                                f"Discord 429 rate limit "
                                f"(retry_after={retry_after}) on "
                                f"{chan.guild.name}/"
                                f"#{chan.name}"

                            )

                        else:

                            error_text = (

                                f"Discord 429 rate limit on "
                                f"{chan.guild.name}/"
                                f"#{chan.name}"

                            )

                    else:

                        error_text = (

                            f"Discord HTTP {status}: {e}"

                        )


                    # Exactly one attempt. Skip this channel.
                    self._save_batch_error(

                        position,

                        error_text

                    )


                    if status == 429:

                        retry_text = (

                            f" Retry-after: **{retry_after}s**."
                            if retry_after is not None
                            else ""
                        )

                        await self._log(

                            "⏩ **Skipped rate-limited channel**\n"
                            f"Channel: **{position + 1}/{total}**\n"
                            f"Server: **{chan.guild.name}**\n"
                            f"Channel: `#{chan.name}`\n"
                            f"No retry.{retry_text}\n"
                            "Moving on."

                        )

                    else:

                        await self._log(

                            "⏩ **Skipped HTTP-error channel**\n"
                            f"Channel: **{position + 1}/{total}**\n"
                            f"Server: **{chan.guild.name}**\n"
                            f"Channel: `#{chan.name}`\n"
                            f"`{type(e).__name__}: {e}`\n"
                            "No retry. Moving on."

                        )


                # =============================================
                # GENERIC ERROR
                # =============================================

                except Exception as e:

                    error_text = (

                        f"{type(e).__name__}: {e}"

                    )


                    self._save_batch_error(

                        position,

                        error_text

                    )


                    await self._log(

                        "⏩ **Skipped errored channel**\n"
                        f"Channel: **{position + 1}/{total}**\n"
                        f"{chan.guild.name}/"
                        f"#{chan.name}\n"
                        f"`{type(e).__name__}: {e}`\n"
                        "No retry. Moving on."

                    )


                # =============================================
                # BETWEEN CHANNELS
                # =============================================

                if (

                    sent

                    and

                    position
                    <
                    end_index - 1

                ):

                    await asyncio.sleep(
                        SEND_DELAY
                    )


            # =================================================
            # BATCH IS DONE
            # =================================================

            self.data = load_data()


            current = int(

                self.data.get(
                    "batch_index",
                    0
                )

            )


            # =================================================
            # ALL CHANNELS FINISHED
            # =================================================

            if current >= total:

                await self._finish_cycle(
                    total
                )

                return


            # =================================================
            # PREPARE NEXT SEPARATE COMMAND
            # =================================================

            # Current next index becomes beginning of
            # next batch.
            self.data[
                "batch_attempt_start"
            ] = current


            self.data[
                "batch_last_error"
            ] = None


            save_data(
                self.data
            )


            remaining = (
                total - current
            )


            next_end = min(

                current
                +
                BATCH_SIZE,

                total

            )


            await self._log(

                "⏸️ **Batch finished — process ended**\n\n"

                f"Processed: "
                f"**{current}/{total}**\n"

                f"Remaining: "
                f"**{remaining}**\n\n"

                f"Next batch: "
                f"**{current + 1}-{next_end}**\n\n"

                f"Use `,continue` in "
                f"<#{CONTROL_CHANNEL_ID}>.\n\n"

                "This command is now completely finished. "
                "Nothing is waiting for the next command."

            )


            # =================================================
            # IMPORTANT:
            #
            # RETURN.
            #
            # No wait_for()
            # No pending continue task
            # No loop waiting for input
            #
            # "delivery guy disappears"
            # =================================================

            return


    # ========================================================
    # FINISH COMPLETE CYCLE
    # ========================================================

    async def _finish_cycle(
        self,
        total
    ):

        self.data = load_data()


        self.data[
            "batch_active"
        ] = False


        self.data[
            "batch_index"
        ] = 0


        self.data[
            "batch_attempt_start"
        ] = 0


        self.data[
            "batch_channel_ids"
        ] = []


        self.data[
            "batch_message"
        ] = None


        self.data[
            "batch_started_at"
        ] = None


        self.data[
            "batch_last_error"
        ] = None


        # Keep an already-saved future fixed slot when this
        # cycle was started immediately with ",start" + "y".
        #
        # Otherwise calculate the next one of:
        # 07:30, 12:30, 18:30, 22:30 Bangladesh time.
        existing_next_run = self.data.get(
            "next_run"
        )


        keep_existing = False


        if existing_next_run:

            try:

                existing_dt = datetime.fromisoformat(
                    existing_next_run
                )

                if existing_dt > utc_now():

                    keep_existing = True

            except Exception:

                keep_existing = False


        if not keep_existing:

            upcoming_slot = next_schedule_slot()

            self.data[
                "next_run"
            ] = upcoming_slot.astimezone(
                timezone.utc
            ).isoformat()


        save_data(
            self.data
        )


        next_run_dt = datetime.fromisoformat(
            self.data[
                "next_run"
            ]
        ).astimezone(
            DHAKA_TZ
        )


        await self._log(

            "🏁 **Cycle finished**\n\n"

            f"Processed: **{total}/{total}**\n"

            f"Next automatic cycle at "
            f"**{next_run_dt.strftime('%H:%M')} "
            f"Bangladesh time**."

        )


        # One lightweight scheduler is needed only
        # for the next fixed automatic cycle.
        self._schedule_loop()


    # ========================================================
    # UNLOAD
    # ========================================================

    def cog_unload(
        self
    ):

        # Stop scheduler
        if (

            self.loop_task

            and

            not self.loop_task.done()

        ):

            self.loop_task.cancel()


        # Remove our logger handler
        try:

            self.http_logger.removeHandler(
                self.rate_handler
            )


        except Exception:

            pass


# ============================================================
# SETUP
# ============================================================

async def setup(
    bot
):

    await bot.add_cog(

        AutoPromo(
            bot
        )

    )
