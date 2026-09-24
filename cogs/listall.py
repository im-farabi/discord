# cogs/listallservers.py

import os
import json
import asyncio

import discord
from discord.ext import commands


# ============================================================
# CONFIG
# ============================================================

DATA_FILE = "/data/channels.json"

# How many successful server invites to show before asking y/n
SERVERS_PER_PAGE = 10

# 1 second delay between each server/invite request
REQUEST_DELAY = 1.0

# Invite settings
#
# max_age=0   -> never expires
# max_uses=0  -> unlimited uses
# temporary=False
# unique=False -> Discord may reuse a suitable existing invite
INVITE_MAX_AGE = 0
INVITE_MAX_USES = 0


# ============================================================
# LOAD DATA
# ============================================================

def load_data():

    if not os.path.exists(DATA_FILE):

        return {
            "channels": []
        }

    try:

        with open(
            DATA_FILE,
            "r",
            encoding="utf8"
        ) as fp:

            data = json.load(fp)

        data.setdefault(
            "channels",
            []
        )

        return data

    except Exception as e:

        print(
            "[listallservers] Failed loading data:",
            type(e).__name__,
            e
        )

        return {
            "channels": []
        }


# ============================================================
# GET CHANNEL ID FROM JSON ENTRY
# ============================================================

def get_channel_id(entry):

    try:

        # Normal channels.json format:
        #
        # {
        #     "id": 123456789
        # }

        if isinstance(
            entry,
            dict
        ):

            return int(
                entry["id"]
            )

        # Also support plain IDs just in case

        return int(
            entry
        )

    except Exception:

        return None


# ============================================================
# GET CHANNEL SAFELY
# ============================================================

async def get_channel_safe(
    bot,
    channel_id
):

    # ========================================================
    # FIRST TRY CACHE
    # ========================================================

    try:

        channel = bot.get_channel(
            channel_id
        )

        if channel is not None:

            return channel

    except Exception:

        pass


    # ========================================================
    # THEN TRY DISCORD API
    # ========================================================

    try:

        channel = await bot.fetch_channel(
            channel_id
        )

        return channel

    except discord.Forbidden:

        return None

    except discord.NotFound:

        return None

    except discord.HTTPException as e:

        print(
            "[listallservers] fetch_channel HTTP error:",
            channel_id,
            getattr(
                e,
                "status",
                "unknown"
            )
        )

        return None

    except Exception as e:

        print(
            "[listallservers] fetch_channel error:",
            channel_id,
            type(e).__name__,
            e
        )

        return None


# ============================================================
# NORMALIZE INVITE URL
# ============================================================

def invite_to_url(invite):

    try:

        # discord.Invite usually stringifies to its URL

        url = str(
            invite
        ).strip()

        if not url:

            return None


        # Make sure we're returning an actual Discord invite

        if "discord.gg/" in url:

            # Convert anything weird to clean https://discord.gg/code

            code = url.split(
                "discord.gg/",
                1
            )[1]

            code = code.split(
                "?"
            )[0]

            code = code.split(
                "/"
            )[0]

            if code:

                return (
                    f"https://discord.gg/"
                    f"{code}"
                )


        # Discord may give discord.com/invite/code

        if "discord.com/invite/" in url:

            code = url.split(
                "discord.com/invite/",
                1
            )[1]

            code = code.split(
                "?"
            )[0]

            code = code.split(
                "/"
            )[0]

            if code:

                return (
                    f"https://discord.gg/"
                    f"{code}"
                )


        return None

    except Exception:

        return None


# ============================================================
# TRY VANITY INVITE
# ============================================================

async def get_vanity_invite(
    guild
):

    # ========================================================
    # METHOD 1:
    # guild.vanity_invite()
    # ========================================================

    try:

        vanity_method = getattr(
            guild,
            "vanity_invite",
            None
        )

        if callable(
            vanity_method
        ):

            invite = await vanity_method()

            url = invite_to_url(
                invite
            )

            if url:

                return url

    except discord.Forbidden:

        pass

    except discord.NotFound:

        pass

    except discord.HTTPException as e:

        print(
            "[listallservers] Vanity HTTP error:",
            getattr(
                guild,
                "name",
                guild.id
            ),
            getattr(
                e,
                "status",
                "unknown"
            )
        )

    except Exception:

        pass


    # ========================================================
    # METHOD 2:
    # cached vanity_url_code
    # ========================================================

    try:

        code = getattr(
            guild,
            "vanity_url_code",
            None
        )

        if code:

            return (
                f"https://discord.gg/"
                f"{code}"
            )

    except Exception:

        pass


    return None


# ============================================================
# CHECK WHETHER CHANNEL CAN CREATE INVITES
# ============================================================

def can_create_invite(
    guild,
    channel
):

    try:

        member = getattr(
            guild,
            "me",
            None
        )


        if member is None:

            return True


        permissions_for = getattr(
            channel,
            "permissions_for",
            None
        )


        if not callable(
            permissions_for
        ):

            return True


        perms = permissions_for(
            member
        )


        # Administrator automatically qualifies

        if getattr(
            perms,
            "administrator",
            False
        ):

            return True


        # Create Instant Invite permission

        if getattr(
            perms,
            "create_instant_invite",
            False
        ):

            return True


        return False

    except Exception:

        # If permission checking itself fails,
        # allow Discord API to make final decision.

        return True


# ============================================================
# CREATE INVITE FROM CHANNEL
# ============================================================

async def create_channel_invite(
    guild,
    channel
):

    try:

        create_invite = getattr(
            channel,
            "create_invite",
            None
        )


        if not callable(
            create_invite
        ):

            return None


        # Skip obvious no-permission channels before making
        # unnecessary API calls.

        if not can_create_invite(
            guild,
            channel
        ):

            return None


        invite = await create_invite(
            max_age=INVITE_MAX_AGE,
            max_uses=INVITE_MAX_USES,
            temporary=False,
            unique=False,
            reason="listallservers command"
        )


        return invite_to_url(
            invite
        )


    # ========================================================
    # NO PERMISSION
    # ========================================================

    except discord.Forbidden:

        print(
            "[listallservers] Cannot create invite:",
            getattr(
                guild,
                "name",
                guild.id
            ),
            "/",
            getattr(
                channel,
                "name",
                channel.id
            ),
            "- Forbidden"
        )

        return None


    # ========================================================
    # CHANNEL DELETED / NOT FOUND
    # ========================================================

    except discord.NotFound:

        return None


    # ========================================================
    # RATE LIMIT / DISCORD HTTP ERROR
    # ========================================================

    except discord.HTTPException as e:

        status = getattr(
            e,
            "status",
            None
        )


        print(
            "[listallservers] Invite HTTP error:",
            getattr(
                guild,
                "name",
                guild.id
            ),
            "/",
            getattr(
                channel,
                "name",
                channel.id
            ),
            "status:",
            status
        )


        # User requested:
        #
        # rate limited -> SKIP
        # HTTP error   -> SKIP
        #
        # discord.py usually handles normal Discord rate limits
        # internally, but if one reaches here we simply skip.

        return None


    # ========================================================
    # ANY OTHER ERROR
    # ========================================================

    except Exception as e:

        print(
            "[listallservers] Invite creation error:",
            getattr(
                guild,
                "name",
                guild.id
            ),
            "/",
            getattr(
                channel,
                "name",
                channel.id
            ),
            type(e).__name__,
            e
        )

        return None


# ============================================================
# GET REAL DISCORD.GG LINK FOR SERVER
# ============================================================

async def get_server_invite(
    guild,
    channels
):

    # ========================================================
    # PRIORITY 1:
    # VANITY
    #
    # discord.gg/example
    # ========================================================

    vanity = await get_vanity_invite(
        guild
    )


    if vanity:

        return {
            "url": vanity,
            "type": "vanity"
        }


    # Small delay after vanity request

    await asyncio.sleep(
        REQUEST_DELAY
    )


    # ========================================================
    # PRIORITY 2:
    # CREATE INVITE
    #
    # Try every saved channel belonging to this server.
    #
    # If channel 1 cannot create:
    #     try channel 2
    #
    # etc.
    # ========================================================

    for channel in channels:

        invite_url = await create_channel_invite(
            guild,
            channel
        )


        if invite_url:

            return {
                "url": invite_url,
                "type": "generated"
            }


        # 1 second delay between failed attempts

        await asyncio.sleep(
            REQUEST_DELAY
        )


    # ========================================================
    # NOTHING WORKED
    # ========================================================

    return None


# ============================================================
# COG
# ============================================================

class ListAllServers(
    commands.Cog
):

    def __init__(
        self,
        bot
    ):

        self.bot = bot

        # Prevent multiple listallservers jobs running
        # simultaneously.

        self.running = False


    # ========================================================
    # ,listallservers
    # ========================================================

    @commands.command(
        name="listallservers",
        aliases=[
            "listservers",
            "las"
        ]
    )
    async def listallservers(
        self,
        ctx
    ):

        # ====================================================
        # PREVENT DUPLICATE RUNS
        # ====================================================

        if self.running:

            return await ctx.send(
                "⚠️ `,listallservers` is already running."
            )


        self.running = True


        try:

            # =================================================
            # LOAD JSON
            # =================================================

            data = load_data()


            saved_entries = data.get(
                "channels",
                []
            )


            if not saved_entries:

                return await ctx.send(
                    "⚠️ No saved channels found in "
                    "`channels.json`."
                )


            await ctx.send(
                "🔎 **ListAllServers started**\n"
                "Checking saved channels and finding "
                "real `discord.gg` server invites..."
            )


            # =================================================
            # BUILD:
            #
            # guild_id -> {
            #     guild,
            #     channels[]
            # }
            #
            # This deduplicates servers while keeping ALL
            # saved channels available for invite creation.
            # =================================================

            guild_map = {}

            invalid_entries = 0

            missing_channels = 0


            for entry in saved_entries:

                # =============================================
                # CHANNEL ID
                # =============================================

                channel_id = get_channel_id(
                    entry
                )


                if channel_id is None:

                    invalid_entries += 1

                    continue


                # =============================================
                # GET CHANNEL
                # =============================================

                channel = await get_channel_safe(
                    self.bot,
                    channel_id
                )


                if channel is None:

                    missing_channels += 1

                    continue


                # =============================================
                # GET GUILD
                # =============================================

                guild = getattr(
                    channel,
                    "guild",
                    None
                )


                if guild is None:

                    continue


                try:

                    guild_id = int(
                        guild.id
                    )

                except Exception:

                    continue


                # =============================================
                # CREATE SERVER ENTRY
                # =============================================

                if guild_id not in guild_map:

                    guild_map[
                        guild_id
                    ] = {
                        "guild": guild,
                        "channels": []
                    }


                # =============================================
                # SAVE CHANNEL
                # =============================================

                guild_map[
                    guild_id
                ][
                    "channels"
                ].append(
                    channel
                )


            # =================================================
            # NOTHING FOUND
            # =================================================

            if not guild_map:

                return await ctx.send(
                    "⚠️ No valid servers could be found from "
                    "`channels.json`."
                )


            # =================================================
            # SERVER LIST
            # =================================================

            server_entries = list(
                guild_map.values()
            )


            total_unique_servers = len(
                server_entries
            )


            await ctx.send(
                "✅ **Database checked**\n\n"
                f"Unique servers: "
                f"**{total_unique_servers}**\n"
                f"Missing/deleted channels: "
                f"**{missing_channels}**\n"
                f"Invalid JSON entries: "
                f"**{invalid_entries}**\n\n"
                "Now finding invites..."
            )


            # =================================================
            # RESULT STORAGE
            #
            # IMPORTANT:
            #
            # We DON'T generate every invite immediately.
            #
            # We process until we collect 10 SUCCESSFUL links,
            # display them, then ask y/n.
            # =================================================

            server_position = 0

            successful_count = 0

            skipped_count = 0

            vanity_count = 0

            generated_count = 0


            # =================================================
            # MAIN PAGINATION LOOP
            # =================================================

            while (
                server_position
                < total_unique_servers
            ):

                # =============================================
                # CURRENT PAGE RESULTS
                # =============================================

                page_results = []


                # =============================================
                # FIND UP TO 10 SUCCESSFUL LINKS
                # =============================================

                while (
                    server_position
                    < total_unique_servers
                    and
                    len(
                        page_results
                    )
                    < SERVERS_PER_PAGE
                ):

                    entry = server_entries[
                        server_position
                    ]


                    # Advance immediately so an error can
                    # never trap us on the same server.

                    server_position += 1


                    guild = entry[
                        "guild"
                    ]


                    channels = entry[
                        "channels"
                    ]


                    guild_name = getattr(
                        guild,
                        "name",
                        "Unknown Server"
                    )


                    guild_id = getattr(
                        guild,
                        "id",
                        "Unknown"
                    )


                    print(
                        "[listallservers] Checking:",
                        guild_name,
                        guild_id
                    )


                    # =========================================
                    # GET INVITE
                    # =========================================

                    try:

                        invite_result = (
                            await get_server_invite(
                                guild,
                                channels
                            )
                        )


                    # =========================================
                    # ANY FAILURE:
                    # SKIP AND CONTINUE
                    # =========================================

                    except Exception as e:

                        print(
                            "[listallservers] Server error:",
                            guild_name,
                            type(e).__name__,
                            e
                        )

                        skipped_count += 1


                        await asyncio.sleep(
                            REQUEST_DELAY
                        )


                        continue


                    # =========================================
                    # NO INVITE:
                    # SKIP
                    # =========================================

                    if not invite_result:

                        print(
                            "[listallservers] SKIPPED:",
                            guild_name,
                            "- no vanity/create-invite access"
                        )


                        skipped_count += 1


                        await asyncio.sleep(
                            REQUEST_DELAY
                        )


                        continue


                    # =========================================
                    # SUCCESS
                    # =========================================

                    successful_count += 1


                    invite_type = invite_result[
                        "type"
                    ]


                    if invite_type == "vanity":

                        vanity_count += 1

                    else:

                        generated_count += 1


                    page_results.append(
                        {
                            "number": successful_count,

                            "guild_name": guild_name,

                            "guild_id": guild_id,

                            "url": invite_result[
                                "url"
                            ],

                            "type": invite_type
                        }
                    )


                    # =========================================
                    # 1 SECOND COOLDOWN
                    # =========================================

                    await asyncio.sleep(
                        REQUEST_DELAY
                    )


                # =============================================
                # SEND CURRENT PAGE
                # =============================================

                if page_results:

                    for result in page_results:

                        type_text = (
                            "Vanity"
                            if result[
                                "type"
                            ] == "vanity"
                            else "Generated"
                        )


                        await ctx.send(
                            f"**{result['number']}. "
                            f"{result['guild_name']}**\n"
                            f"{result['url']}\n"
                            f"`{type_text}`"
                        )


                        # Requested 1-second cooldown
                        # between displayed servers too.

                        await asyncio.sleep(
                            REQUEST_DELAY
                        )


                # =============================================
                # CHECK WHETHER ALL SERVERS ARE DONE
                # =============================================

                if (
                    server_position
                    >= total_unique_servers
                ):

                    break


                # =============================================
                # ASK KEEP GOING
                # =============================================

                remaining_servers = (
                    total_unique_servers
                    - server_position
                )


                await ctx.send(
                    "➡️ **Keep going?**\n"
                    "Type **y** for another "
                    "**10 successful server links**.\n"
                    "Type **n** to stop.\n\n"
                    f"Servers still unchecked: "
                    f"**{remaining_servers}**\n"
                    f"Successful so far: "
                    f"**{successful_count}**\n"
                    f"Skipped so far: "
                    f"**{skipped_count}**"
                )


                # =============================================
                # WAIT FOREVER FOR y / n
                #
                # timeout=None
                # =============================================

                def check(
                    message
                ):

                    try:

                        return (
                            message.author.id
                            == ctx.author.id
                            and
                            message.channel.id
                            == ctx.channel.id
                            and
                            message.content
                            .strip()
                            .lower()
                            in {
                                "y",
                                "yes",
                                "n",
                                "no"
                            }
                        )

                    except Exception:

                        return False


                response = await self.bot.wait_for(
                    "message",
                    check=check,
                    timeout=None
                )


                answer = (
                    response.content
                    .strip()
                    .lower()
                )


                # =============================================
                # YES
                # =============================================

                if answer in {
                    "y",
                    "yes"
                }:

                    await ctx.send(
                        "✅ Continuing..."
                    )


                    continue


                # =============================================
                # NO
                # =============================================

                await ctx.send(
                    "🛑 **Stopped.**\n\n"
                    f"Successful links: "
                    f"**{successful_count}**\n"
                    f"Vanity: "
                    f"**{vanity_count}**\n"
                    f"Generated: "
                    f"**{generated_count}**\n"
                    f"Skipped: "
                    f"**{skipped_count}**"
                )


                return


            # =================================================
            # EVERYTHING FINISHED
            # =================================================

            await ctx.send(
                "🏁 **ListAllServers finished!**\n\n"
                f"Servers checked: "
                f"**{total_unique_servers}**\n"
                f"Successful `discord.gg` links: "
                f"**{successful_count}**\n"
                f"Vanity links: "
                f"**{vanity_count}**\n"
                f"Generated links: "
                f"**{generated_count}**\n"
                f"Skipped/errors/no permission: "
                f"**{skipped_count}**\n\n"
                "✅ No `discord.com/channels/...` links "
                "were used."
            )


        # ====================================================
        # CANCELLED
        # ====================================================

        except asyncio.CancelledError:

            try:

                await ctx.send(
                    "⚠️ `,listallservers` was cancelled."
                )

            except Exception:

                pass


            raise


        # ====================================================
        # FATAL ERROR
        # ====================================================

        except Exception as e:

            print(
                "[listallservers] Fatal error:",
                type(e).__name__,
                e
            )


            try:

                await ctx.send(
                    "❌ **ListAllServers crashed:**\n"
                    f"`{type(e).__name__}: {e}`"
                )

            except Exception:

                pass


        # ====================================================
        # ALWAYS RESET
        # ====================================================

        finally:

            self.running = False


# ============================================================
# COMMAND ERROR
# ============================================================

    @listallservers.error
    async def listallservers_error(
        self,
        ctx,
        error
    ):

        if isinstance(
            error,
            commands.CommandOnCooldown
        ):

            return


        print(
            "[listallservers] Command error:",
            type(error).__name__,
            error
        )


# ============================================================
# SETUP
# ============================================================

async def setup(
    bot
):

    await bot.add_cog(
        ListAllServers(
            bot
        )
    )
