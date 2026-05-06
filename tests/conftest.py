"""Mock redbot and discord so pure-logic utils can be tested without a live bot."""
import sys
from unittest.mock import MagicMock

# Stub out all redbot/discord modules before any raffle imports
for mod in [
    "discord",
    "discord.ui",
    "redbot",
    "redbot.core",
    "redbot.core.bot",
    "redbot.core.commands",
    "redbot.core.config",
    "redbot.core.data_manager",
]:
    sys.modules.setdefault(mod, MagicMock())
