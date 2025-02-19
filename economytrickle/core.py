# pylint: disable=not-async-context-manager
import asyncio
import contextlib
from typing import no_type_check, Union
from datetime import datetime, timedelta
from collections import defaultdict
import random

import discord
from redbot.core import commands, checks
from redbot.core.config import Config
from redbot.core import bank
from redbot.core.utils.chat_formatting import pagify, box
from typing import Literal

from .activity import RecordHandler
from .converters import configable_guild_defaults, settings_converter


class EconomyTrickle(commands.Cog):
    """
    Automatic Economy gains for active users
    """

    __version__ = "2.1.0"

    def __init__(self, bot, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot = bot
        self.config = Config.get_conf(self, identifier=78631113035100160, force_registration=True)
        self.config.register_guild(
            active=False,
            mode="blacklist",
            blacklist=[],
            whitelist=[],
            min_voice_members=2,
            **configable_guild_defaults,
            # custom_level_table={},  # TODO
        )
        self.config.register_member(xp=0, level=0)
        self.recordhandler = RecordHandler()

        self.main_loop_task = asyncio.create_task(self.main_loop())
        self.extra_tasks = []

    def cog_unload(self):
        self.main_loop_task.cancel()
        for t in self.extra_tasks:
            t.cancel()

    @commands.Cog.listener()
    async def on_message(self, message):
        if await self.bot.cog_disabled_in_guild(self, message.guild):
            return
        if message.guild and await self.config.guild(message.guild).active():
            self.recordhandler.proccess_message(message)

    async def main_loop(self):
        minutes = defaultdict(int)

        while self is self.bot.get_cog(self.__class__.__name__):
            await asyncio.sleep(60)

            data = await self.config.all_guilds()

            for g in self.bot.guilds:
                if g.id in data and data[g.id]["active"]:
                    minutes[g] += 1
                    if minutes[g] % data[g.id]["interval"] == 0:
                        minutes[g] = 0
                        now = datetime.utcnow()
                        tsk = asyncio.create_task(self.do_rewards_for(g, now, data[g.id]))
                        self.extra_tasks.append(tsk)

    async def do_rewards_for(self, guild: discord.Guild, now: datetime, data: dict):
        after = now - timedelta(minutes=data["interval"], seconds=10)
        voice_mem = await self.config.guild(guild).min_voice_members()
        if data["mode"] == "blacklist":

            def mpred(m: discord.Message):
                return m.channel.id not in data["blacklist"]

            def vpred(mem: discord.Member):
                with contextlib.suppress(AttributeError):
                    return (
                        len(mem.voice.channel.members) > voice_mem
                        and mem.voice.channel.id not in data["blacklist"]
                        and not mem.bot
                    )

        else:

            def mpred(m: discord.Message):
                return m.channel.id in data["whitelist"]

            def vpred(mem: discord.Member):
                with contextlib.suppress(AttributeError):
                    return (
                        len(mem.voice.channel.members) > voice_mem
                        and mem.voice.channel.id in data["whitelist"]
                        and not mem.bot
                    )

        has_active_message = set(self.recordhandler.get_active_for_guild(guild=guild, after=after, message_check=mpred))

        is_active_voice = {m for m in guild.members if vpred(m)}
        is_active = has_active_message | is_active_voice

        # take exp away from inactive users
        for member in guild.members:
            if member in is_active:
                continue

            # loose exp per interval
            xp = max(data["xp_per_interval"] * data["decay_rate"], 1)
            xp = await self.config.member(member).xp() - xp
            xp = max(xp, 0)
            await self.config.member(member).xp.set(xp)

            # update level on these users
            level, next_needed = 0, data["level_xp_base"]

            while xp >= next_needed:
                level += 1
                xp -= next_needed
                next_needed += data["xp_lv_increase"]

            if data["maximum_level"] is not None:
                level = min(data["maximum_level"], level)

            await self.config.member(member).level.set(level)

        for member in is_active:
            # failed for this member, skip
            if data["fail_rate"] > random.random():
                continue
            # xp processing first
            xp = data["xp_per_interval"]
            if member in has_active_message:
                xp += data["extra_message_xp"]
            if member in is_active_voice:
                xp += data["extra_voice_xp"]

            xp = xp + await self.config.member(member).xp()
            await self.config.member(member).xp.set(xp)

            # level up: new mode in future.
            level, next_needed = 0, data["level_xp_base"]

            while xp >= next_needed:
                level += 1
                xp -= next_needed
                next_needed += data["xp_lv_increase"]

            if data["maximum_level"] is not None:
                level = min(data["maximum_level"], level)

            await self.config.member(member).level.set(level)

            # give economy

            to_give = data["econ_per_interval"]
            bonus = data["bonus_per_level"] * level
            if data["maximum_bonus"] is not None:
                bonus = min(data["maximum_bonus"], bonus)

            to_give += bonus
            try:
                await bank.deposit_credits(member, to_give)
            except bank.errors.BalanceTooHigh:
                pass

        # cleanup old message objects
        self.recordhandler.clear_before(guild=guild, before=after)

    # Commands go here

    @checks.admin_or_permissions(manage_guild=True)
    @commands.group(name="trickleset")
    async def ect(self, ctx):
        """
        Settings for economy trickle
        """
        pass

    @ect.command()
    async def active(self, ctx, active: bool):
        """
        Sets this as active (or not)
        """
        await self.config.guild(ctx.guild).active.set(active)
        await ctx.tick()

    @ect.command()
    @no_type_check
    async def setstuff(self, ctx, *, data: settings_converter = None):
        """
        Set other variables

        format *example* for this (and defaults):

        ```yaml
        bonus_per_level: 5
        econ_per_interval: 20
        fail_rate: 0.2
        decay_rate: 0.5
        extra_message_xp: 0
        extra_voice_xp: 0
        interval: 5
        level_xp_base: 100
        maximum_bonus: null
        maximum_level: null
        xp_lv_increase: 50
        xp_per_interval: 10
        ```
        """
        if not data:
            data = await self.config.guild(ctx.guild).all()
            keys = list(configable_guild_defaults.keys())
            msg = "Current data: (run `help trickleset setstuff to set`)\n```yaml\n"
            for key in keys:
                msg += f"{key}: {data[key]}\n"
            msg += "```"

            await ctx.send(msg)
            return

        for k, v in data.items():
            await self.config.guild(ctx.guild).get_attr(k).set(v)
        await ctx.tick()

    @ect.command(name="mode")
    async def rset_set_mode(self, ctx, *, mode: str = ""):
        """
        Whether to operate on a `whitelist`, or a `blacklist`
        """

        mode = mode.lower()
        if mode not in ("whitelist", "blacklist"):
            return await ctx.send_help()

        await self.config.guild(ctx.guild).mode.set(mode)
        await ctx.tick()

    @ect.command(name="voice")
    async def rset_voicemem(self, ctx, min_voice_members: int = 0):
        """
        Minimum number of voice members needed to count as active.

        If users are in a voice chat, only trickle if there are at least min_voice_members
        in there.
        """

        if min_voice_members < 1:
            curr = await self.config.guild(ctx.guild).min_voice_members()
            await ctx.send(f"Current: {curr}")
            return

        await self.config.guild(ctx.guild).min_voice_members.set(min_voice_members)
        await ctx.tick()

    @ect.command(name="addchan")
    async def rset_add_chan(self, ctx, *channels: Union[discord.TextChannel, discord.VoiceChannel]):
        """
        Adds one or more channels to the current mode's settings
        """

        if not channels:
            return await ctx.send_help()

        gsets = await self.config.guild(ctx.guild).all()
        mode = gsets["mode"]
        if not mode:
            return await ctx.send(f"You need to set a mode using `{ctx.clean_prefix}trickleset mode` first")

        for channel in channels:
            if channel.id not in gsets[mode]:
                gsets[mode].append(channel.id)

        await self.config.guild(ctx.guild).set_raw(mode, value=gsets[mode])
        await ctx.tick()

    @ect.command(name="remchan")
    async def rset_rem_chan(self, ctx, *channels: Union[discord.TextChannel, discord.VoiceChannel]):
        """
        removes one or more channels from the current mode's settings
        """

        if not channels:
            return await ctx.send_help()

        gsets = await self.config.guild(ctx.guild).all()
        mode = gsets["mode"]
        if not mode:
            return await ctx.send(f"You need to set a mode using `{ctx.clean_prefix}trickleset mode` first")

        for channel in channels:
            while channel.id in gsets[mode]:
                gsets[mode].remove(channel.id)

        await self.config.guild(ctx.guild).set_raw(mode, value=gsets[mode])
        await ctx.tick()

    @ect.command(name="list")
    async def rset_list_chan(self, ctx):
        """
        List current channels.
        """
        gsets = await self.config.guild(ctx.guild).all()
        mode = gsets["mode"]
        if not mode:
            return await ctx.send(f"You need to set a mode using `{ctx.clean_prefix}trickleset mode` first")

        if not gsets[mode]:
            await ctx.send("No channels defined.")
            return

        msg = f"Mode: {mode}\n"
        for i, channel in enumerate(gsets[mode]):
            msg += f"{i+1}. {ctx.guild.get_channel(channel)}\n"

        pages = pagify(msg)
        for page in pages:
            await ctx.send(box(page))

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ):
        pass
