from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from redbot.core import Config, commands, data_manager
from redbot.core.bot import Red

from .utils import format_end_time, parse_duration, pick_winners, validate_emoji, validate_timezone
from .views import (
    RaffleCancelConfirmView,
    RaffleConfirmView,
    RaffleSelectView,
    RaffleSetupModal,
    RaffleTypeView,
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

    # ── Task scheduling (G2, G3) ──────────────────────────────────────

    async def _reschedule_tasks(self):
        """Restore auto-draw and manual-notify tasks after bot restart."""
        pass  # Implemented in Task 8

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
        """Append one or more roles to the allowed-starters list."""
        if not roles:
            await ctx.maybe_send_embed("Provide at least one role mention.")
            return
        async with self.config.guild(ctx.guild).allowed_roles() as lst:
            for role in roles:
                if role.id not in lst:
                    lst.append(role.id)
        names = ", ".join(r.mention for r in roles)
        await ctx.send(
            f"Added to allowed roles: {names}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @setraffle.command(name="memberconf")
    async def setraffle_memberconf(self, ctx: commands.Context, *members: discord.Member):
        """Append one or more members to the allowed-starters list."""
        if not members:
            await ctx.maybe_send_embed("Provide at least one member mention.")
            return
        async with self.config.guild(ctx.guild).allowed_members() as lst:
            for member in members:
                if member.id not in lst:
                    lst.append(member.id)
        names = ", ".join(m.mention for m in members)
        await ctx.send(
            f"Added to allowed members: {names}",
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
        msg = await ctx.send("Click to set timezone:", view=view)
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
        msg = await ctx.send(f"Reset **{target}** to defaults. Are you sure?", view=view)
        view.message = msg
