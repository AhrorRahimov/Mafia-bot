"""Role composition and dealing.

The base table below defines the classic city/mafia balance for every
supported lobby size. Optional roles requested through ``GameSettings``
are then layered on top by :func:`_apply_settings`, which always *replaces*
an existing slot so the total headcount never changes.

Replacement rules (chosen so team sizes stay predictable):
  * DON      replaces a MAFIA   -> mafia side unchanged
  * LAWYER   replaces a MAFIA   -> mafia side unchanged
  * WHORE    replaces a CITIZEN -> town unchanged
  * SERGEANT replaces a CITIZEN -> town unchanged
  * MANIAC   replaces a CITIZEN -> town shrinks by one, third party appears
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional, Sequence

from app.game.constants import MAX_PLAYERS, MIN_PLAYERS
from app.game.enums import Role


@dataclass(frozen=True, slots=True)
class RoleSetup:
    """How many players of each role a game contains."""

    mafia: int
    detective: int
    doctor: int
    citizen: int
    don: int = 0
    whore: int = 0
    sergeant: int = 0
    maniac: int = 0
    lawyer: int = 0

    @property
    def total(self) -> int:
        return (
            self.mafia
            + self.detective
            + self.doctor
            + self.citizen
            + self.don
            + self.whore
            + self.sergeant
            + self.maniac
            + self.lawyer
        )

    @property
    def mafia_side(self) -> int:
        """Players counted for the mafia win condition."""
        return self.mafia + self.don + self.lawyer

    def to_list(self) -> list[Role]:
        """Expand the counts into a concrete, ordered list of roles."""
        roles: list[Role] = []
        roles += [Role.DON] * self.don
        roles += [Role.MAFIA] * self.mafia
        roles += [Role.LAWYER] * self.lawyer
        roles += [Role.DETECTIVE] * self.detective
        roles += [Role.SERGEANT] * self.sergeant
        roles += [Role.DOCTOR] * self.doctor
        roles += [Role.WHORE] * self.whore
        roles += [Role.MANIAC] * self.maniac
        roles += [Role.CITIZEN] * self.citizen
        return roles


# players -> (mafia, detective, doctor, citizen)
_BALANCE_TABLE: dict[int, tuple[int, int, int, int]] = {
    4:  (1, 1, 1, 1),
    5:  (1, 1, 1, 2),
    6:  (2, 1, 1, 2),
    7:  (2, 1, 1, 3),
    8:  (2, 1, 1, 4),
    9:  (3, 1, 1, 4),
    10: (3, 1, 1, 5),
}


def _max_mafia_for(total: int) -> int:
    """Largest mafia side that still leaves the town in the majority."""
    return max(1, (total - 1) // 2)


def _apply_settings(base: RoleSetup, settings) -> RoleSetup:
    """Layer the optional roles from ``settings`` onto a base setup.

    Every optional role replaces an existing slot, so ``base.total`` is
    preserved. When no suitable slot is free the role is silently skipped
    rather than corrupting the headcount.
    """
    mafia = base.mafia
    detective = base.detective
    doctor = base.doctor
    citizen = base.citizen
    don = whore = sergeant = maniac = lawyer = 0

    total = base.total

    # --- Flexible composition: explicit mafia head count ---
    desired = getattr(settings, "mafia_count", None)
    if desired is not None:
        target = max(1, min(int(desired), _max_mafia_for(total)))
        delta = target - mafia
        if delta > 0:
            take = min(delta, citizen)
            mafia += take
            citizen -= take
        elif delta < 0:
            mafia += delta
            citizen -= delta  # delta is negative -> citizens grow back

    # --- Mafia-side specialists (replace a plain mafia) ---
    if getattr(settings, "enable_don", False) and mafia >= 1:
        mafia -= 1
        don = 1
    if getattr(settings, "enable_lawyer", False) and mafia >= 1:
        mafia -= 1
        lawyer = 1

    # --- Town specialists (replace a citizen) ---
    if getattr(settings, "enable_whore", False) and citizen >= 1:
        citizen -= 1
        whore = 1
    # The sergeant only makes sense while there is a detective to inherit from.
    if getattr(settings, "enable_sergeant", False) and citizen >= 1 and detective >= 1:
        citizen -= 1
        sergeant = 1

    # --- Third party (replaces a citizen) ---
    if getattr(settings, "enable_maniac", False) and citizen >= 1:
        citizen -= 1
        maniac = 1

    setup = RoleSetup(
        mafia=mafia,
        detective=detective,
        doctor=doctor,
        citizen=citizen,
        don=don,
        whore=whore,
        sergeant=sergeant,
        maniac=maniac,
        lawyer=lawyer,
    )
    assert setup.total == total, "role composition must preserve headcount"
    return setup


def get_setup(player_count: int, settings=None) -> RoleSetup:
    """Return the role composition for ``player_count`` players.

    Raises:
        ValueError: if the player count is outside the supported range.
    """
    if player_count not in _BALANCE_TABLE:
        raise ValueError(
            f"Unsupported player count {player_count} "
            f"(expected {MIN_PLAYERS}..{MAX_PLAYERS})"
        )
    mafia, detective, doctor, citizen = _BALANCE_TABLE[player_count]
    base = RoleSetup(
        mafia=mafia, detective=detective, doctor=doctor, citizen=citizen
    )
    if settings is None:
        return base
    return _apply_settings(base, settings)


def shuffle_roles(
    setup: RoleSetup, rng: Optional[random.Random] = None
) -> list[Role]:
    """Return the setup's roles in random order."""
    roles = setup.to_list()
    (rng or random).shuffle(roles)
    return roles


def assign_roles(
    user_ids: Sequence[int],
    settings=None,
    rng: Optional[random.Random] = None,
    forced: Optional[dict[int, Role]] = None,
) -> dict[int, Role]:
    """Deal a role to every user id.

    ``forced`` holds role-card reservations (``user_id -> Role``). A
    reservation is honoured only when the composition actually contains
    that role and it has not been handed out to an earlier claimant; the
    rest of the table is dealt at random as usual. Callers can compare the
    result with ``forced`` to find out which cards were rejected and must
    be refunded.
    """
    setup = get_setup(len(user_ids), settings)
    roles = shuffle_roles(setup, rng)
    if not forced:
        return dict(zip(user_ids, roles, strict=True))

    assignments: dict[int, Role] = {}
    pool = list(roles)
    # Reservations first, in the order the cards were activated.
    for user_id, wanted in forced.items():
        if user_id not in user_ids:
            continue
        if wanted in pool:
            pool.remove(wanted)
            assignments[user_id] = wanted
    for user_id in user_ids:
        if user_id not in assignments:
            assignments[user_id] = pool.pop()
    return assignments
