"""Per-game configurable settings, tuned from the in-lobby settings menu."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from app.game.constants import (
    DAY_DISCUSSION_DURATION,
    DAY_VOTE_DURATION,
    LAST_WORD_DURATION,
    NIGHT_DURATION,
    NOMINATION_DURATION,
)


@dataclass(slots=True)
class GameSettings:
    """Everything the lobby creator can tune before the game starts."""

    # --- Phase timings (seconds) ---
    night_duration: int = NIGHT_DURATION
    discussion_duration: int = DAY_DISCUSSION_DURATION
    vote_duration: int = DAY_VOTE_DURATION
    last_word_duration: int = LAST_WORD_DURATION
    nomination_duration: int = NOMINATION_DURATION

    # --- Optional roles ---
    enable_don: bool = False
    enable_whore: bool = False
    enable_sergeant: bool = False
    enable_maniac: bool = False
    enable_lawyer: bool = False

    # --- Role abilities ---
    # The detective may spend the night SHOOTING instead of checking
    # (True Mafia rule). The two actions are mutually exclusive.
    detective_can_shoot: bool = True

    # --- Flexible composition ---
    # Explicit number of mafia-side players. ``None`` keeps the balance table
    # default. Clamped in ``balance.get_setup`` so the town always outnumbers
    # the mafia at the start.
    mafia_count: Optional[int] = None

    # --- Game modes ---
    # When False, deaths are announced without revealing the victim's role
    # (a much harder, "blind" game).
    reveal_roles: bool = True
    # Allow an explicit "Skip" option during the day vote.
    allow_skip_vote: bool = True
    # Run a nomination stage before the vote: only nominated players can be
    # voted for. Falls back to "everyone" when nobody is nominated.
    nomination_mode: bool = False
    # Relay messages between dead players in their private chats.
    dead_chat: bool = True
    # Warn and auto-skip players who repeatedly miss their night action.
    afk_autoskip: bool = True

    def copy(self) -> "GameSettings":
        """Return an independent copy (per-game snapshot)."""
        return replace(self)
