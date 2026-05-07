from __future__ import annotations

from typing import Optional

import discord


class _ModalTriggerView(discord.ui.View):
    """Single-button view that opens a Modal on click. Reused for tzconf and raffle start."""

    def __init__(self, modal: discord.ui.Modal, *, label: str = "Open", author_id: Optional[int] = None):
        super().__init__(timeout=60)
        self.modal = modal
        self.author_id = author_id
        self.message: Optional[discord.Message] = None
        # G5: set label on the button instance, not the descriptor
        self.children[0].label = label

    @discord.ui.button(label="Open", style=discord.ButtonStyle.primary)
    async def trigger(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.author_id and interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return
        await interaction.response.send_modal(self.modal)
        self.stop()

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class RaffleSetupModal(discord.ui.Modal, title="Start a Raffle"):
    name_input = discord.ui.TextInput(
        label="Raffle Name",
        placeholder="e.g. Summer Giveaway",
        min_length=1,
        max_length=80,
    )
    emoji_input = discord.ui.TextInput(
        label="Entry Emoji",
        placeholder="e.g. 🎉 or <:custom:123456>",
        min_length=1,
        max_length=30,
    )
    duration_input = discord.ui.TextInput(
        label="Duration",
        placeholder="e.g. 2h, 1d, 30m",
        min_length=2,
        max_length=6,
    )
    winners_input = discord.ui.TextInput(
        label="Number of Winners",
        placeholder="e.g. 1",
        min_length=1,
        max_length=3,
    )

    def __init__(self, cog, ctx):
        super().__init__()
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        from .utils import parse_duration, validate_emoji

        name = self.name_input.value.strip()
        emoji = self.emoji_input.value.strip()
        raw_duration = self.duration_input.value.strip()
        raw_winners = self.winners_input.value.strip()

        # G4: validate emoji
        if not validate_emoji(emoji):
            await interaction.response.send_message(
                "❌ Invalid emoji. Use a Unicode emoji (🎉) or custom emoji (<:name:id>).",
                ephemeral=True,
            )
            return

        duration = parse_duration(raw_duration)
        if duration is None:
            await interaction.response.send_message(
                "❌ Invalid duration. Use formats like `2h`, `1d`, `30m`.",
                ephemeral=True,
            )
            return

        try:
            winner_count = int(raw_winners)
            if winner_count < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Winners must be a positive integer.", ephemeral=True
            )
            return

        view = RaffleTypeView(self.cog, self.ctx, name, emoji, duration, winner_count)
        await interaction.response.send_message(
            "**Step 2 — Pick winner method:**", view=view, ephemeral=True
        )


class RaffleTypeView(discord.ui.View):
    def __init__(self, cog, ctx, name: str, emoji: str, duration, winner_count: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.name = name
        self.emoji = emoji
        self.duration = duration
        self.winner_count = winner_count

    @discord.ui.select(
        placeholder="Pick winner method...",
        options=[
            discord.SelectOption(
                label="Auto — bot draws on expiry",
                value="auto",
                emoji="⏰",
            ),
            discord.SelectOption(
                label="Manual — creator triggers draw",
                value="manual",
                emoji="🖐️",
            ),
        ],
    )
    async def select_type(self, interaction: discord.Interaction, select: discord.ui.Select):
        draw_type = select.values[0]
        view = RaffleConfirmView(
            self.cog, self.ctx, self.name, self.emoji,
            self.duration, self.winner_count, draw_type,
        )
        embed = await view.build_embed()
        await interaction.response.edit_message(content=None, embed=embed, view=view)
        self.stop()


class RaffleConfirmView(discord.ui.View):
    def __init__(self, cog, ctx, name: str, emoji: str, duration, winner_count: int, draw_type: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.name = name
        self.emoji = emoji
        self.duration = duration
        self.winner_count = winner_count
        self.draw_type = draw_type

    async def build_embed(self) -> discord.Embed:
        import time
        from .utils import format_end_time

        tz_name = await self.cog.config.guild(self.ctx.guild).timezone()
        end_ts = time.time() + self.duration.total_seconds()
        end_str = format_end_time(end_ts, tz_name)
        colour = await self.ctx.embed_colour()
        embed = discord.Embed(title="📋 Confirm Raffle", colour=colour)
        embed.add_field(name="Name", value=self.name, inline=True)
        embed.add_field(name="Emoji", value=self.emoji, inline=True)
        embed.add_field(name="Duration", value=f"Ends {end_str}", inline=False)
        embed.add_field(name="Winners", value=str(self.winner_count), inline=True)
        embed.add_field(name="Method", value=self.draw_type.capitalize(), inline=True)
        return embed

    @discord.ui.button(label="✅ Start", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Not your raffle.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Starting raffle...", embed=None, view=None)
        await self.cog._launch_raffle(
            self.ctx, self.name, self.emoji,
            self.duration, self.winner_count, self.draw_type,
        )
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Raffle setup cancelled.", embed=None, view=None
        )
        self.stop()


class RaffleSelectView(discord.ui.View):
    pass


class RaffleCancelConfirmView(discord.ui.View):
    pass


class TimezoneModal(discord.ui.Modal, title="Set Timezone"):
    tz_input = discord.ui.TextInput(
        label="Timezone",
        placeholder="e.g. Asia/Jakarta, US/Eastern, UTC",
        min_length=2,
        max_length=50,
    )

    def __init__(self, cog, ctx):
        super().__init__()
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        # Deferred import to avoid circular: views.py <- raffle.py <- views.py
        from .utils import validate_timezone

        value = self.tz_input.value.strip()
        if not validate_timezone(value):
            await interaction.response.send_message(
                f"❌ `{value}` is not a valid timezone. "
                "Use IANA format like `Asia/Jakarta` or `UTC`.",
                ephemeral=True,
            )
            return
        await self.cog.config.guild(self.ctx.guild).timezone.set(value)
        await interaction.response.send_message(
            f"✅ Timezone set to **{value}**.", ephemeral=True
        )


class ResetConfirmView(discord.ui.View):
    def __init__(self, cog, ctx, target: str):
        super().__init__(timeout=30)
        self.cog = cog
        self.ctx = ctx
        self.target = target
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Not your reset.", ephemeral=True)
            return
        cfg = self.cog.config.guild(self.ctx.guild)
        t = self.target
        if t in ("role", "all"):
            await cfg.allowed_roles.set([])
        if t in ("member", "all"):
            await cfg.allowed_members.set([])
        if t in ("tz", "all"):
            await cfg.timezone.set("UTC")
        if t in ("base", "all"):
            await cfg.open.set(True)
        if t in ("multi", "all"):
            await cfg.multi.set(True)
        await interaction.response.edit_message(
            content=f"✅ Reset **{self.target}** to defaults.", view=None
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Reset cancelled.", view=None)
        self.stop()

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="Reset timed out.", view=None)
            except discord.HTTPException:
                pass
