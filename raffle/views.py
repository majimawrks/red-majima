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
    pass


class ResetConfirmView(discord.ui.View):
    pass
