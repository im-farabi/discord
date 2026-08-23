# cogs/auto.py

import asyncio
import discord
from discord.ext import commands


CHANNEL_ID = 1533686357547946045

# Small delay before automatically continuing.
CONTINUE_DELAY = 2


class AutoContinue(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.processing = False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):

        # Only watch the specific channel.
        if message.channel.id != CHANNEL_ID:
            return

        content = message.content

        # Detect the batch-completed message.
        #
        # We intentionally don't require the ENTIRE message to be identical,
        # because numbers such as 9/68, 10-12, etc. will keep changing.
        batch_finished = (
            "Batch finished" in content
            and "process ended" in content
            and "Processed:" in content
            and "Remaining:" in content
            and "Next batch:" in content
            and "Nothing is waiting for the next command" in content
        )

        if not batch_finished:
            return

        # Prevent duplicate triggers if multiple matching messages
        # somehow arrive almost simultaneously.
        if self.processing:
            return

        self.processing = True

        try:
            await asyncio.sleep(CONTINUE_DELAY)

            await message.channel.send(",continue")

            print(
                f"[AUTO] Batch finished detected in {CHANNEL_ID}. "
                f"Sent ,continue."
            )

        except Exception as e:
            print(f"[AUTO] Failed to send ,continue: {e}")

        finally:
            # Give the next batch time to start before allowing
            # another finished message to trigger.
            await asyncio.sleep(2)
            self.processing = False


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoContinue(bot))
