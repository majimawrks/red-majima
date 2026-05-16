from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import discord
from redbot.core import Config, commands, data_manager
from redbot.core.bot import Red

from .utils import format_end_time, pick_winners
from .views import (
    RaffleCancelConfirmView,
    RaffleRepickSelectView,
    RaffleReviveDurationModal,
    RaffleReviveSelectView,
    RaffleSelectView,
    RaffleSetupModal,
    ResetConfirmView,
    TimezoneModal,
    _ModalTriggerView,
)

IDENTIFIER = 748392015


class Raffle(commands.Cog):
    """Reaction-based raffle system with wizard setup."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=IDENTIFIER, force_registration=True)
        self.config.register_guild(
            open=True,
            multi=True,
            allowed_roles=[],
            allowed_members=[],
            timezone="UTC",
            raffles={},
        )
        # G1: keyed by (guild_id, message_id) tuple
        self._draw_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._history_base: Optional[Path] = None

    async def cog_load(self):
        self._history_base = data_manager.cog_data_path(self) / "history"
        self._history_base.mkdir(parents=True, exist_ok=True)
        await self._reschedule_tasks()

    async def cog_unload(self):
        for task in self._draw_tasks.values():
            task.cancel()

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        # Purge from active raffles in Config
        all_guilds = await self.config.all_guilds()
        for guild_id, guild_data in all_guilds.items():
            raffles = guild_data.get("raffles", {})
            changed = False
            for raffle in raffles.values():
                if user_id in raffle.get("participants", []):
                    raffle["participants"].remove(user_id)
                    changed = True
                if user_id in raffle.get("winners", []):
                    raffle["winners"].remove(user_id)
                    changed = True
            if changed:
                guild = self.bot.get_guild(guild_id)
                if guild:
                    await self.config.guild(guild).raffles.set(raffles)

        # Purge from on-disk history archives
        history_base = self._history_base or data_manager.cog_data_path(self) / "history"
        if not history_base.exists():
            return
        for guild_dir in history_base.iterdir():
            if not guild_dir.is_dir():
                continue
            for archive_path in guild_dir.glob("*.json"):
                try:
                    history = json.loads(archive_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                changed = False
                for entry in history:
                    if user_id in entry.get("participants", []):
                        entry["participants"].remove(user_id)
                        changed = True
                    if user_id in entry.get("winners", []):
                        entry["winners"].remove(user_id)
                        changed = True
                if changed:
                    archive_path.write_text(
                        json.dumps(history, indent=2), encoding="utf-8"
                    )

    # ── History archival (G8) ─────────────────────────────────────────

    def _history_dir(self, guild_id: int) -> Path:
        d = self._history_base / str(guild_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _archive_raffle(self, guild_id: int, message_id: str, entry: dict):
        """Append a completed/cancelled raffle to the monthly history file."""
        now = datetime.now(timezone.utc)
        filename = now.strftime("%Y-%m") + ".json"
        path = self._history_dir(guild_id) / filename
        history = []
        if path.exists():
            try:
                history = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                history = []
        entry_copy = dict(entry)
        entry_copy["message_id"] = message_id
        entry_copy["archived_at"] = now.isoformat()
        history.append(entry_copy)
        path.write_text(json.dumps(history, indent=2), encoding="utf-8")

    def _update_archive_winners(self, guild_id: int, message_id_str: str, winner_ids: list):
        """Overwrite the winners field for a specific archived raffle entry."""
        history_dir = self._history_dir(guild_id)
        for path in history_dir.glob("*.json"):
            try:
                history = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            changed = False
            for entry in history:
                if entry.get("message_id") == message_id_str:
                    entry["winners"] = winner_ids
                    changed = True
                    break
            if changed:
                path.write_text(json.dumps(history, indent=2), encoding="utf-8")
                return

    def _load_month_history(self, guild_id: int, month_str: str) -> list:
        path = self._history_dir(guild_id) / f"{month_str}.json"
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def _find_archived_raffle(self, guild_id: int, message_id_str: str) -> Optional[dict]:
        """Search all monthly history files for a raffle by message ID."""
        history_dir = self._history_dir(guild_id)
        for path in sorted(history_dir.glob("*.json"), reverse=True):
            try:
                history = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for entry in history:
                if entry.get("message_id") == message_id_str:
                    return entry
        return None

    async def _get_bot_prefix(self, guild: discord.Guild) -> str:
        """Get the first valid prefix for DM messages (G6)."""
        prefixes = await self.bot.get_valid_prefixes(guild)
        return prefixes[0] if prefixes else "!"

    # ── setraffle ─────────────────────────────────────────────────────

    @commands.group(name="setraffle")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_channels=True, moderate_members=True)
    async def setraffle(self, ctx: commands.Context):
        """Configure the raffle system."""

    @setraffle.command(name="allconf")
    async def setraffle_allconf(self, ctx: commands.Context):
        """Show all current raffle settings."""
        cfg = await self.config.guild(ctx.guild).all()
        allowed_roles = [ctx.guild.get_role(r) for r in cfg["allowed_roles"]]
        allowed_members = [ctx.guild.get_member(m) for m in cfg["allowed_members"]]
        role_str = ", ".join(r.mention for r in allowed_roles if r) or "None"
        member_str = ", ".join(m.mention for m in allowed_members if m) or "None"
        active = sum(1 for r in cfg["raffles"].values() if r["status"] == "active")

        embed = discord.Embed(title="Raffle Settings", colour=await ctx.embed_colour())
        embed.add_field(name="Mode", value="Open" if cfg["open"] else "Closed", inline=True)
        embed.add_field(name="Multi-raffle", value="Enabled" if cfg["multi"] else "Disabled", inline=True)
        embed.add_field(name="Timezone", value=cfg["timezone"], inline=True)
        embed.add_field(name="Allowed Roles", value=role_str, inline=False)
        embed.add_field(name="Allowed Members", value=member_str, inline=False)
        embed.set_footer(text=f"{active} active raffle(s)")
        await ctx.send(embed=embed)

    @setraffle.command(name="baseconf")
    async def setraffle_baseconf(self, ctx: commands.Context):
        """Toggle open (anyone starts) vs closed (role/member list only) mode."""
        current = await self.config.guild(ctx.guild).open()
        await self.config.guild(ctx.guild).open.set(not current)
        mode = "**Open**" if not current else "**Closed**"
        await ctx.maybe_send_embed(f"Raffle mode set to {mode}.")

    @setraffle.command(name="multiconf")
    async def setraffle_multiconf(self, ctx: commands.Context):
        """Toggle whether multiple raffles can run concurrently in different channels."""
        current = await self.config.guild(ctx.guild).multi()
        await self.config.guild(ctx.guild).multi.set(not current)
        state = "**enabled**" if not current else "**disabled**"
        await ctx.maybe_send_embed(f"Multi-raffle {state}.")

    @setraffle.command(name="roleconf")
    async def setraffle_roleconf(self, ctx: commands.Context, *roles: discord.Role):
        """Append one or more roles to the allowed-starters list.

        Accepts role mentions, role names, or role IDs.
        Automatically switches to closed mode if the guild is currently open.
        Example: [p]setraffle roleconf @Mods 123456789 VIPs
        """
        if not roles:
            await ctx.maybe_send_embed("Provide at least one role (mention, name, or ID).")
            return
        cfg = self.config.guild(ctx.guild)
        was_open = await cfg.open()
        async with cfg.allowed_roles() as lst:
            for role in roles:
                if role.id not in lst:
                    lst.append(role.id)
        if was_open:
            await cfg.open.set(False)
        names = ", ".join(r.mention for r in roles)
        suffix = " Raffle mode switched to **Closed**." if was_open else ""
        await ctx.send(
            f"Added to allowed roles: {names}.{suffix}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @setraffle.command(name="memberconf")
    async def setraffle_memberconf(self, ctx: commands.Context, *members: discord.Member):
        """Append one or more members to the allowed-starters list.

        Accepts member mentions, usernames, or user IDs.
        Automatically switches to closed mode if the guild is currently open.
        Example: [p]setraffle memberconf @alice 123456789
        """
        if not members:
            await ctx.maybe_send_embed("Provide at least one member (mention, username, or ID).")
            return
        cfg = self.config.guild(ctx.guild)
        was_open = await cfg.open()
        async with cfg.allowed_members() as lst:
            for member in members:
                if member.id not in lst:
                    lst.append(member.id)
        if was_open:
            await cfg.open.set(False)
        names = ", ".join(m.mention for m in members)
        suffix = " Raffle mode switched to **Closed**." if was_open else ""
        await ctx.send(
            f"Added to allowed members: {names}.{suffix}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @setraffle.command(name="tzconf")
    async def setraffle_tzconf(self, ctx: commands.Context):
        """Set the guild timezone used for displaying raffle end times."""
        view = _ModalTriggerView(
            TimezoneModal(self, ctx),
            label="Set Timezone",
            author_id=ctx.author.id,
        )
        msg = await ctx.send("Click to set timezone:", view=view, ephemeral=True)
        view.message = msg

    _RESET_TARGETS = {"base", "multi", "role", "member", "tz", "all"}

    @setraffle.command(name="reset")
    async def setraffle_reset(self, ctx: commands.Context, target: str = "all"):
        """Reset a setting or all settings to defaults.

        Targets: base, multi, role, member, tz, all
        """
        target = target.lower()
        if target not in self._RESET_TARGETS:
            await ctx.maybe_send_embed(
                f"Unknown target `{target}`. Choose from: {', '.join(sorted(self._RESET_TARGETS))}"
            )
            return
        view = ResetConfirmView(self, ctx, target)
        msg = await ctx.send(f"Reset **{target}** to defaults. Are you sure?", view=view, ephemeral=True)
        view.message = msg

    # ── raffle ────────────────────────────────────────────────────────

    @commands.group(name="raffle", invoke_without_command=False)
    @commands.guild_only()
    async def raffle(self, ctx: commands.Context):
        """Raffle commands."""

    @raffle.command(name="start")
    async def raffle_start(self, ctx: commands.Context, channel: Optional[Union[discord.TextChannel, discord.Thread]] = None):
        """Open the raffle setup wizard.

        Optionally provide a target channel where the raffle will be posted.
        Example: [p]raffle start #giveaways
        """
        if not await self._can_start(ctx):
            await ctx.send("You don't have permission to start a raffle.", ephemeral=True)
            return
        if not await self._slot_available(ctx):
            await ctx.send(
                "A raffle is already running in this guild. "
                "Enable multi-raffle (`setraffle multiconf`) or wait for it to end.",
                ephemeral=True,
            )
            return
        if channel is not None:
            perms = channel.permissions_for(ctx.guild.me)
            missing = []
            if not perms.send_messages:
                missing.append("Send Messages")
            if not perms.embed_links:
                missing.append("Embed Links")
            if not perms.add_reactions:
                missing.append("Add Reactions")
            if not perms.read_message_history:
                missing.append("Read Message History")
            if missing:
                await ctx.send(
                    f"❌ I'm missing permissions in {channel.mention}: "
                    + ", ".join(f"**{p}**" for p in missing),
                    ephemeral=True,
                )
                return
        view = _ModalTriggerView(
            RaffleSetupModal(self, ctx, target_channel=channel),
            label="Set Up Raffle",
            author_id=ctx.author.id,
        )
        msg = await ctx.send("Click to start the raffle setup:", view=view, ephemeral=True)
        view.message = msg

    async def _can_start(self, ctx: commands.Context) -> bool:
        """Check if invoker is allowed to start a raffle."""
        cfg = await self.config.guild(ctx.guild).all()
        if cfg["open"]:
            return True
        # Closed mode: privileged Discord perms OR whitelist
        perms = ctx.author.guild_permissions
        if perms.administrator or perms.manage_channels or perms.moderate_members:
            return True
        if ctx.author.id in cfg["allowed_members"]:
            return True
        author_role_ids = {r.id for r in ctx.author.roles}
        if author_role_ids & set(cfg["allowed_roles"]):
            return True
        return False

    def _is_privileged(self, ctx: commands.Context) -> bool:
        perms = ctx.author.guild_permissions
        return perms.administrator or perms.manage_channels or perms.moderate_members

    async def _slot_available(self, ctx: commands.Context) -> bool:
        """Check whether a new raffle can start (respects multiconf)."""
        multi = await self.config.guild(ctx.guild).multi()
        if multi:
            return True
        raffles = await self.config.guild(ctx.guild).raffles()
        return not any(r["status"] == "active" for r in raffles.values())

    # ── Launch & scheduling ───────────────────────────────────────────

    async def _launch_raffle(
        self,
        ctx: commands.Context,
        name: str,
        emoji: str,
        duration,
        winner_count: int,
        draw_type: str,
        target_channel: Optional[Union[discord.TextChannel, discord.Thread]] = None,
    ):
        post_channel = target_channel or ctx.channel
        tz_name = await self.config.guild(ctx.guild).timezone()
        end_ts = time.time() + duration.total_seconds()
        end_str = format_end_time(end_ts, tz_name)
        method_str = "Auto-draw" if draw_type == "auto" else "Manual draw"

        colour = await ctx.embed_colour()
        embed = discord.Embed(
            title=f"{emoji} {name}",
            description=f"React with {emoji} to enter!",
            colour=colour,
        )
        embed.add_field(name="Ends", value=end_str, inline=True)
        embed.add_field(name="Winners", value=str(winner_count), inline=True)
        embed.add_field(name="Method", value=method_str, inline=True)
        embed.set_footer(text=f"Hosted by {ctx.author.display_name} · Participants: 0")

        msg = await post_channel.send(embed=embed)
        try:
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            pass

        entry = {
            "name": name,
            "emoji": emoji,
            "channel_id": post_channel.id,
            "creator_id": ctx.author.id,
            "end_time": end_ts,
            "winner_count": winner_count,
            "draw_type": draw_type,
            "status": "active",
            "participants": [],
            "winners": [],
        }
        async with self.config.guild(ctx.guild).raffles() as raffles:
            raffles[str(msg.id)] = entry

        delay = duration.total_seconds()
        if draw_type == "auto":
            self._schedule_auto_draw(ctx.guild, msg.id, delay)
        else:
            self._schedule_manual_notify(ctx.guild, msg.id, delay)

    # ── Task scheduling (G2, G3) ──────────────────────────────────────

    def _schedule_auto_draw(self, guild: discord.Guild, message_id: int, delay: float):
        """Schedule auto winner draw. Stored in _draw_tasks (G2)."""
        task_key = (guild.id, message_id)

        async def _run():
            await asyncio.sleep(max(0.0, delay))
            await self._execute_draw(guild, message_id)

        task = self.bot.loop.create_task(_run())
        self._draw_tasks[task_key] = task

    def _schedule_manual_notify(self, guild: discord.Guild, message_id: int, delay: float):
        """Schedule DM notification when manual raffle duration ends. Stored in _draw_tasks (G2)."""
        task_key = (guild.id, message_id)

        async def _run():
            await asyncio.sleep(max(0.0, delay))
            await self._notify_manual_end(guild, message_id)

        task = self.bot.loop.create_task(_run())
        self._draw_tasks[task_key] = task

    async def _reschedule_tasks(self):
        """Restore both auto-draw and manual-notify tasks after bot restart (G3)."""
        all_guilds = await self.config.all_guilds()
        for guild_id, data in all_guilds.items():
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            for msg_id_str, entry in data.get("raffles", {}).items():
                if entry["status"] != "active":
                    continue
                delay = entry["end_time"] - time.time()
                msg_id = int(msg_id_str)
                if entry["draw_type"] == "auto":
                    self._schedule_auto_draw(guild, msg_id, delay)
                else:
                    self._schedule_manual_notify(guild, msg_id, delay)

    # ── Reaction tracking ─────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id or not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        key = str(payload.message_id)
        # Hold the Config lock for the entire read-check-write to prevent
        # concurrent reaction handlers from overwriting each other's updates.
        async with self.config.guild(guild).raffles() as raffles:
            if key not in raffles:
                return
            entry = raffles[key]
            if entry["status"] != "active":
                return
            if str(payload.emoji) != entry["emoji"]:
                return
            if payload.user_id in entry["participants"]:
                return
            entry["participants"].append(payload.user_id)
            raffles[key] = entry
        # Update the embed footer outside the lock (network I/O)
        await self._update_participant_count(guild, payload.channel_id, payload.message_id, entry)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id or not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        key = str(payload.message_id)
        async with self.config.guild(guild).raffles() as raffles:
            if key not in raffles:
                return
            entry = raffles[key]
            if entry["status"] != "active":
                return
            if str(payload.emoji) != entry["emoji"]:
                return
            if payload.user_id not in entry["participants"]:
                return
            entry["participants"].remove(payload.user_id)
            raffles[key] = entry
        await self._update_participant_count(guild, payload.channel_id, payload.message_id, entry)

    async def _update_participant_count(
        self,
        guild: discord.Guild,
        channel_id: int,
        message_id: int,
        entry: dict,
    ):
        channel = guild.get_channel_or_thread(channel_id)
        if not channel:
            return
        try:
            msg = await channel.fetch_message(message_id)
        except discord.HTTPException:
            return
        if not msg.embeds:
            return
        embed = msg.embeds[0].copy()
        creator = guild.get_member(entry["creator_id"])
        host_name = creator.display_name if creator else "Unknown"
        embed.set_footer(
            text=f"Hosted by {host_name} · Participants: {len(entry['participants'])}"
        )
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            pass

    # ── Draw engine ───────────────────────────────────────────────────

    async def _execute_draw(
        self,
        guild: discord.Guild,
        message_id: int,
        result_channel: Optional[Union[discord.TextChannel, discord.Thread]] = None,
    ):
        """Run the winner draw, collapse the raffle embed, post result separately.

        result_channel: where to post the winner embed. Defaults to the
        raffle's own channel (used for auto-draw).
        """
        key = str(message_id)

        # Atomically read the entry and remove it from Config so concurrent
        # reaction handlers stop writing to it.
        async with self.config.guild(guild).raffles() as raffles:
            if key not in raffles or raffles[key]["status"] != "active":
                return
            entry = dict(raffles[key])  # snapshot for post-lock work
            entry["participants"] = list(raffles[key]["participants"])
            raffles.pop(key, None)

        participants = entry["participants"]
        winner_ids = pick_winners(participants, entry["winner_count"])
        creator = guild.get_member(entry["creator_id"])
        host_name = creator.display_name if creator else "Unknown"

        # Archive immediately — the raffle is already removed from Config above,
        # so this must happen before any network calls that could fail and leave
        # the entry unrecoverable (e.g. Discord-archived thread not in cache).
        entry["status"] = "ended"
        entry["winners"] = winner_ids
        self._archive_raffle(guild.id, key, entry)
        self._draw_tasks.pop((guild.id, message_id), None)

        # Resolve the raffle channel. get_channel_or_thread() only searches the
        # in-memory cache; Discord-archived threads fall out of cache but still
        # exist, so fall back to an API call for those.
        raffle_channel = guild.get_channel_or_thread(entry["channel_id"])
        if not raffle_channel:
            try:
                raffle_channel = await self.bot.fetch_channel(entry["channel_id"])
            except discord.HTTPException:
                raffle_channel = None

        # Fetch the original raffle message for animation + tombstone edit.
        msg = None
        if raffle_channel:
            try:
                msg = await raffle_channel.fetch_message(message_id)
            except discord.HTTPException:
                pass

        # Rolling animation (4 frames, 0.8s apart) on the original embed
        if msg and participants:
            colour = discord.Colour.gold()
            for _ in range(4):
                candidate = guild.get_member(random.choice(participants))
                roll_name = candidate.display_name if candidate else "???"
                anim_embed = discord.Embed(
                    title="🎰 Drawing winners...",
                    description=f"Rolling... **{roll_name}**",
                    colour=colour,
                )
                try:
                    await msg.edit(embed=anim_embed)
                except discord.HTTPException:
                    pass
                await asyncio.sleep(0.8)

        # Collapse original embed to a minimal tombstone
        if msg:
            ended_embed = discord.Embed(
                title=f"{entry['emoji']} {entry['name']}",
                colour=discord.Colour.greyple(),
            )
            ended_embed.set_footer(
                text=f"Raffle ended · Hosted by {host_name} · {len(participants)} participants"
            )
            try:
                await msg.edit(embed=ended_embed)
            except discord.HTTPException:
                pass

        # Build and post winner result as a new message
        post_to = result_channel or raffle_channel
        if post_to:
            if not winner_ids:
                description = "No one entered this raffle."
            elif len(winner_ids) < entry["winner_count"]:
                mentions = " ".join(f"<@{uid}>" for uid in winner_ids)
                description = f"All participants win!\n{mentions}"
            else:
                mentions = "\n".join(f"🥇 <@{uid}>" for uid in winner_ids)
                description = f"Congratulations to:\n{mentions}"

            result_embed = discord.Embed(
                title=f"🎉 {entry['name']} — Winners!",
                description=description,
                colour=discord.Colour.gold(),
            )
            result_embed.set_footer(
                text=f"Hosted by {host_name} · {len(participants)} participants"
            )
            try:
                await post_to.send(embed=result_embed)
            except discord.HTTPException:
                pass

    async def _notify_manual_end(self, guild: discord.Guild, message_id: int):
        """Edit announcement embed and DM creator when manual raffle duration ends."""
        raffles = await self.config.guild(guild).raffles()
        key = str(message_id)
        if key not in raffles or raffles[key]["status"] != "active":
            return
        entry = raffles[key]
        channel = guild.get_channel_or_thread(entry["channel_id"])
        creator = guild.get_member(entry["creator_id"])

        # Edit announcement embed
        if channel:
            try:
                ann_msg = await channel.fetch_message(message_id)
                if ann_msg.embeds:
                    embed = ann_msg.embeds[0].copy()
                    mention = creator.mention if creator else f"<@{entry['creator_id']}>"
                    embed.add_field(
                        name="⏰ Status",
                        value=f"Duration ended — awaiting draw by {mention}",
                        inline=False,
                    )
                    await ann_msg.edit(embed=embed)
            except discord.HTTPException:
                pass

        # DM creator (G6: real prefix)
        if creator:
            prefix = await self._get_bot_prefix(guild)
            ch_mention = channel.mention if channel else "#unknown"
            try:
                await creator.send(
                    f'Your raffle **"{entry["name"]}"** in {ch_mention} '
                    f"(**{guild.name}**) has ended.\n"
                    f"Run `{prefix}raffle end` in that channel to draw winners."
                )
            except discord.HTTPException:
                pass

        self._draw_tasks.pop((guild.id, message_id), None)

    # ── raffle end / cancel helpers ───────────────────────────────────

    async def _get_visible_raffles(
        self,
        ctx: commands.Context,
        *,
        draw_type: Optional[str] = None,
    ) -> dict:
        """Return active raffles the invoker can act on."""
        raffles = await self.config.guild(ctx.guild).raffles()
        is_privileged = (
            ctx.author.guild_permissions.administrator
            or ctx.author.guild_permissions.manage_channels
            or ctx.author.guild_permissions.moderate_members
        )
        result = {}
        for key, entry in raffles.items():
            if entry["status"] != "active":
                continue
            if draw_type and entry["draw_type"] != draw_type:
                continue
            if is_privileged or entry["creator_id"] == ctx.author.id:
                result[key] = entry
        return result

    async def _do_draw_ctx(
        self, ctx: commands.Context, guild: discord.Guild, message_id: int
    ):
        """Trigger draw from a prefix command context."""
        task = self._draw_tasks.pop((guild.id, message_id), None)
        if task:
            task.cancel()
        # Pass the command channel so the winner embed appears here
        await self._execute_draw(guild, message_id, result_channel=ctx.channel)

    async def _do_draw(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        message_id: int,
    ):
        """Trigger draw from a select-menu interaction."""
        task = self._draw_tasks.pop((guild.id, message_id), None)
        if task:
            task.cancel()
        await interaction.response.edit_message(content="🎰 Drawing winners...", view=None)
        await self._execute_draw(guild, message_id, result_channel=interaction.channel)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    async def _do_revive(
        self,
        interaction: discord.Interaction,
        ctx: commands.Context,
        entry: dict,
        duration,
    ):
        """Post a new raffle from an archived entry, restoring participants."""
        guild = ctx.guild
        await interaction.response.defer(ephemeral=True)

        if not await self._slot_available(ctx):
            await interaction.followup.send(
                "❌ A raffle is already running in this guild. "
                "Enable multi-raffle (`setraffle multiconf`) or wait for it to end.",
                ephemeral=True,
            )
            return

        channel = guild.get_channel_or_thread(entry["channel_id"])
        if not channel:
            await interaction.followup.send(
                "❌ The original channel no longer exists.", ephemeral=True
            )
            return

        tz_name = await self.config.guild(guild).timezone()
        end_ts = time.time() + duration.total_seconds()
        end_str = format_end_time(end_ts, tz_name)
        method_str = "Auto-draw" if entry["draw_type"] == "auto" else "Manual draw"

        colour = await ctx.embed_colour()
        embed = discord.Embed(
            title=f"{entry['emoji']} {entry['name']}",
            description=f"React with {entry['emoji']} to enter!",
            colour=colour,
        )
        embed.add_field(name="Ends", value=end_str, inline=True)
        embed.add_field(name="Winners", value=str(entry["winner_count"]), inline=True)
        embed.add_field(name="Method", value=method_str, inline=True)

        creator = guild.get_member(entry["creator_id"])
        host_name = creator.display_name if creator else "Unknown"
        participants = list(entry.get("participants", []))
        embed.set_footer(
            text=f"Hosted by {host_name} · Participants: {len(participants)}"
        )

        try:
            msg = await channel.send(embed=embed)
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Failed to post raffle: {exc}", ephemeral=True
            )
            return

        try:
            await msg.add_reaction(entry["emoji"])
        except discord.HTTPException:
            pass

        new_entry = {
            "name": entry["name"],
            "emoji": entry["emoji"],
            "channel_id": channel.id,
            "creator_id": entry["creator_id"],
            "end_time": end_ts,
            "winner_count": entry["winner_count"],
            "draw_type": entry["draw_type"],
            "status": "active",
            "participants": participants,
            "winners": [],
        }
        async with self.config.guild(guild).raffles() as raffles:
            raffles[str(msg.id)] = new_entry

        delay = duration.total_seconds()
        if entry["draw_type"] == "auto":
            self._schedule_auto_draw(guild, msg.id, delay)
        else:
            self._schedule_manual_notify(guild, msg.id, delay)

        await interaction.followup.send("✅ Raffle revived!", ephemeral=True)

    @raffle.command(name="end")
    async def raffle_end(self, ctx: commands.Context):
        """Trigger the winner draw for a raffle.

        Works on manual raffles waiting to be drawn, and can also
        force-end an auto raffle early.
        """
        visible = await self._get_visible_raffles(ctx)
        if not visible:
            await ctx.send("No active raffles you can draw.", ephemeral=True)
            return
        if len(visible) == 1:
            msg_id = int(next(iter(visible)))
            await self._do_draw_ctx(ctx, ctx.guild, msg_id)
        else:
            view = RaffleSelectView(self, ctx, visible, action="end")
            msg = await ctx.send("Which raffle do you want to draw?", view=view, ephemeral=True)
            view.message = msg

    @raffle.command(name="cancel")
    async def raffle_cancel(self, ctx: commands.Context):
        """Cancel an active raffle."""
        visible = await self._get_visible_raffles(ctx)
        if not visible:
            await ctx.send("No active raffles to cancel.", ephemeral=True)
            return
        if len(visible) == 1:
            msg_id = int(next(iter(visible)))
            entry = next(iter(visible.values()))
            view = RaffleCancelConfirmView(self, ctx, ctx.guild, msg_id)
            msg = await ctx.send(
                f"Cancel raffle **{entry['name']}**? This cannot be undone.",
                view=view, ephemeral=True,
            )
            view.message = msg
        else:
            view = RaffleSelectView(self, ctx, visible, action="cancel")
            msg = await ctx.send("Which raffle do you want to cancel?", view=view, ephemeral=True)
            view.message = msg

    @raffle.command(name="revive")
    async def raffle_revive(self, ctx: commands.Context, message_id: Optional[int] = None):
        """Revive an ended or cancelled raffle with its original settings and participants.

        Optionally provide the original raffle message ID to skip the selection menu.
        Example: [p]raffle revive 1234567890
        """
        if not await self._can_start(ctx):
            await ctx.send("You don't have permission to revive a raffle.", ephemeral=True)
            return

        privileged = self._is_privileged(ctx)

        if message_id is not None:
            entry = self._find_archived_raffle(ctx.guild.id, str(message_id))
            if entry is None or (not privileged and entry.get("creator_id") != ctx.author.id):
                await ctx.send(
                    f"❌ No archived raffle found with message ID `{message_id}`.",
                    ephemeral=True,
                )
                return
            participant_count = len(entry.get("participants", []))
            view = _ModalTriggerView(
                RaffleReviveDurationModal(self, ctx, entry),
                label="Revive Raffle",
                author_id=ctx.author.id,
            )
            msg = await ctx.send(
                f"Reviving **{entry['name']}** ({participant_count} participants restored). "
                "Click to set new duration:",
                view=view,
                ephemeral=True,
            )
            view.message = msg
        else:
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            history = self._load_month_history(ctx.guild.id, month)
            ended = [
                e for e in history
                if e.get("status") in ("ended", "cancelled")
                and (privileged or e.get("creator_id") == ctx.author.id)
            ]
            if not ended:
                await ctx.send(
                    f"No ended or cancelled raffles found for **{month}**.",
                    ephemeral=True,
                )
                return
            # Most recent first, capped at Discord's select menu limit of 25
            ended = list(reversed(ended))[:25]
            view = RaffleReviveSelectView(self, ctx, ended)
            msg = await ctx.send("Which raffle do you want to revive?", view=view, ephemeral=True)
            view.message = msg

    async def _do_repick(self, ctx: commands.Context, entry: dict):
        """Re-pick winners from an archived ended raffle and post the result publicly."""
        participants = entry.get("participants", [])
        winner_ids = pick_winners(participants, entry["winner_count"])

        creator = ctx.guild.get_member(entry["creator_id"])
        host_name = creator.display_name if creator else "Unknown"

        if not winner_ids:
            description = "No one entered this raffle."
        elif len(winner_ids) < entry["winner_count"]:
            mentions = " ".join(f"<@{uid}>" for uid in winner_ids)
            description = f"All participants win!\n{mentions}"
        else:
            mentions = "\n".join(f"🥇 <@{uid}>" for uid in winner_ids)
            description = f"Congratulations to:\n{mentions}"

        result_embed = discord.Embed(
            title=f"🎉 {entry['name']} — Winners (Repick)!",
            description=description,
            colour=discord.Colour.gold(),
        )
        result_embed.set_footer(
            text=f"Hosted by {host_name} · {len(participants)} participants"
        )
        try:
            await ctx.send(embed=result_embed)
        except discord.HTTPException:
            pass

        self._update_archive_winners(ctx.guild.id, entry["message_id"], winner_ids)

    @raffle.command(name="repick")
    async def raffle_repick(self, ctx: commands.Context, message_id: Optional[int] = None):
        """Re-pick winners for an ended raffle without reviving it.

        Optionally provide the original raffle message ID to skip the selection menu.
        Example: [p]raffle repick 1234567890
        """
        if not await self._can_start(ctx):
            await ctx.send("You don't have permission to repick raffle winners.", ephemeral=True)
            return

        privileged = self._is_privileged(ctx)

        if message_id is not None:
            entry = self._find_archived_raffle(ctx.guild.id, str(message_id))
            if (
                entry is None
                or entry.get("status") != "ended"
                or (not privileged and entry.get("creator_id") != ctx.author.id)
            ):
                await ctx.send(
                    f"❌ No ended raffle found with message ID `{message_id}`.",
                    ephemeral=True,
                )
                return
            await self._do_repick(ctx, entry)
        else:
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            history = self._load_month_history(ctx.guild.id, month)
            ended = [
                e for e in history
                if e.get("status") == "ended"
                and (privileged or e.get("creator_id") == ctx.author.id)
            ]
            if not ended:
                await ctx.send(f"No ended raffles found for **{month}**.", ephemeral=True)
                return
            ended = list(reversed(ended))[:25]
            view = RaffleRepickSelectView(self, ctx, ended)
            msg = await ctx.send("Which raffle do you want to repick winners for?", view=view, ephemeral=True)
            view.message = msg

    async def _do_cancel(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        message_id: int,
    ):
        """Execute cancellation: cancel task, edit embed, archive entry."""
        key = str(message_id)

        # Atomically read, mark cancelled, and remove — prevents a concurrent
        # reaction handler from writing to a raffle that's about to be popped.
        async with self.config.guild(guild).raffles() as raffles:
            if key not in raffles:
                await interaction.response.edit_message(
                    content="Raffle not found.", view=None
                )
                return
            entry = dict(raffles[key])  # snapshot for post-lock work
            entry["status"] = "cancelled"
            raffles.pop(key, None)

        await interaction.response.edit_message(content="✅ Raffle cancelled.", view=None)

        # Cancel any pending scheduled task (G2)
        task = self._draw_tasks.pop((guild.id, message_id), None)
        if task:
            task.cancel()

        # Edit announcement embed
        channel = guild.get_channel_or_thread(entry["channel_id"])
        if channel:
            try:
                msg = await channel.fetch_message(message_id)
                if msg.embeds:
                    embed = msg.embeds[0].copy()
                    embed.title = f"❌ {entry['name']} (Cancelled)"
                    embed.colour = discord.Colour.red()
                    await msg.edit(embed=embed)
            except discord.HTTPException:
                pass

        # Archive to disk (G8)
        self._archive_raffle(guild.id, key, entry)

    # ── raffle history ────────────────────────────────────────────────

    @raffle.command(name="history")
    async def raffle_history(self, ctx: commands.Context, month: Optional[str] = None):
        """Show past raffles from the monthly archive.

        Optionally specify a month in YYYY-MM format (e.g. 2026-05).
        Defaults to the current month.
        """
        if month is None:
            month = datetime.now(timezone.utc).strftime("%Y-%m")
        elif not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month):
            await ctx.maybe_send_embed(
                "❌ Invalid month format. Use `YYYY-MM` (e.g. `2026-05`)."
            )
            return
        path = self._history_dir(ctx.guild.id) / f"{month}.json"
        if not path.exists():
            await ctx.maybe_send_embed(f"No raffle history for **{month}**.")
            return
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            await ctx.maybe_send_embed("Failed to read history file.")
            return
        if not history:
            await ctx.maybe_send_embed(f"No raffles recorded for **{month}**.")
            return

        embed = discord.Embed(
            title=f"Raffle History — {month}",
            colour=await ctx.embed_colour(),
        )
        # Show last 10 entries
        for entry in history[-10:]:
            status_icon = "🎉" if entry["status"] == "ended" else "❌"
            winners = entry.get("winners", [])
            winners_str = ", ".join(f"<@{w}>" for w in winners) if winners else "None"
            embed.add_field(
                name=f"{status_icon} {entry['name']}",
                value=(
                    f"Winners: {winners_str}\n"
                    f"Participants: {len(entry.get('participants', []))}"
                ),
                inline=False,
            )
        if len(history) > 10:
            embed.set_footer(text=f"Showing last 10 of {len(history)} raffles")
        await ctx.send(embed=embed)
