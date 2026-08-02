"""Domain enumerations for the game."""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """Roles a player can be assigned."""

    CITIZEN = "citizen"
    MAFIA = "mafia"
    DETECTIVE = "detective"
    DOCTOR = "doctor"
    # --- Optional roles (enabled via lobby settings) ---
    DON = "don"            # Mafia leader: decisive kill + may search for the detective.
    WHORE = "whore"        # "Putana": blocks one player's night action (town-aligned).
    SERGEANT = "sergeant"  # Detective's aide: sees checks, inherits the badge on death.
    MANIAC = "maniac"      # Third party: kills alone every night, wins alone.
    LAWYER = "lawyer"      # Mafia-aligned: his client reads "civilian" to the detective.


class GameStatus(StrEnum):
    """Lifecycle status of a Game row."""

    LOBBY = "lobby"
    RUNNING = "running"
    FINISHED = "finished"


class GamePhase(StrEnum):
    """In-memory phases for an active game (not persisted per row)."""

    NIGHT = "night"
    DAY_ANNOUNCE = "day_announce"
    DAY_DISCUSSION = "day_discussion"
    DAY_NOMINATION = "day_nomination"  # optional "who goes on trial" stage
    DAY_VOTE = "day_vote"
    DAY_LAST_WORD = "day_last_word"    # brief window for a lynched player's final message
    ENDED = "ended"


class Winner(StrEnum):
    """Who won the game."""

    MAFIA = "mafia"
    CITY = "city"
    MANIAC = "maniac"
    NONE = "none"


# Roles that belong to the mafia team. Used for win checks and for the
# detective's verdict. The LAWYER is mafia-aligned even though he does not
# take part in the nightly kill vote.
MAFIA_SIDE_ROLES: frozenset[Role] = frozenset(
    {Role.MAFIA, Role.DON, Role.LAWYER}
)

# Mafia-side roles that actually vote for the nightly victim and see each
# other as teammates. The lawyer is deliberately excluded: in True Mafia he
# does not know the family and picks his client blindly.
KILLER_MAFIA_ROLES: frozenset[Role] = frozenset({Role.MAFIA, Role.DON})

# Roles that win on their own, against everyone else.
THIRD_PARTY_ROLES: frozenset[Role] = frozenset({Role.MANIAC})

# Town-aligned roles.
TOWN_ROLES: frozenset[Role] = frozenset(
    {Role.CITIZEN, Role.DETECTIVE, Role.DOCTOR, Role.WHORE, Role.SERGEANT}
)

# Roles that must submit a night action before the night can resolve early.
# The SERGEANT is passive, and the DON's optional "search" is not required.
NIGHT_ACTOR_ROLES: frozenset[Role] = frozenset(
    {
        Role.MAFIA,
        Role.DON,
        Role.DETECTIVE,
        Role.DOCTOR,
        Role.WHORE,
        Role.MANIAC,
        Role.LAWYER,
    }
)
