"""FSM states.

The bot relies primarily on inline buttons + filters for game flow,
so FSM is intentionally minimal — used only to gate private-chat
prompts where we await a single callback from a specific user.
"""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class NightFlow(StatesGroup):
    """Awaiting a night-action callback from the user in private chat."""

    waiting_target = State()
