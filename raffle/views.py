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
    pass


class RaffleTypeView(discord.ui.View):
    pass


class RaffleConfirmView(discord.ui.View):
    pass


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
