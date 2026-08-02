"""In-memory state of an active game.

This module is the single source of truth for runtime game state.
Database rows mirror final results, but per-second gameplay
(votes, night actions, phases) lives here to avoid hot-writing SQL.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from app.game.enums import (
    KILLER_MAFIA_ROLES,
    MAFIA_SIDE_ROLES,
    THIRD_PARTY_ROLES,
    GamePhase,
    Role,
    Winner,
)
from app.game.settings import GameSettings


@dataclass(slots=True)
class PlayerState:
    """Runtime view of a player inside an active game."""

    user_id: int
    full_name: str
    role: Role
    is_alive: bool = True

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        status = "alive" if self.is_alive else "dead"
        return f"<Player {self.full_name} ({self.role.value}, {status})>"


@dataclass(slots=True)
class NightActions:
    """Collected choices during a single night."""

    # mafia_target: a single user_id - the team must converge.
    mafia_target: Optional[int] = None
    detective_target: Optional[int] = None
    # The detective may shoot instead of checking (mutually exclusive).
    detective_shoot_target: Optional[int] = None
    doctor_target: Optional[int] = None
    # Whore/putana blocks this player's night action.
    whore_target: Optional[int] = None
    # Maniac's solo kill.
    maniac_target: Optional[int] = None
    # The lawyer's client reads "civilian" to the detective this night.
    lawyer_client: Optional[int] = None
    # The don's optional search for the detective (does not block the night).
    don_check_target: Optional[int] = None
    # Per-mafia votes to decide the team target.
    mafia_votes: dict[int, int] = field(default_factory=dict)
    # Track who has acted this night so we know when everyone is done.
    acted: set[int] = field(default_factory=set)

    def reset(self) -> None:
        """Clear all choices for the next night."""
        self.mafia_target = None
        self.detective_target = None
        self.detective_shoot_target = None
        self.doctor_target = None
        self.whore_target = None
        self.maniac_target = None
        self.lawyer_client = None
        self.don_check_target = None
        self.mafia_votes.clear()
        self.acted.clear()


@dataclass(slots=True)
class DayVotes:
    """Collected votes during the daytime lynching phase."""

    # voter_user_id -> target_user_id (or SKIP_VOTE_ID)
    votes: dict[int, int] = field(default_factory=dict)
    # Track who has voted.
    voted: set[int] = field(default_factory=set)
    # nominator_user_id -> nominated_user_id (nomination stage)
    nominations: dict[int, int] = field(default_factory=dict)

    def reset(self) -> None:
        self.votes.clear()
        self.voted.clear()

    def reset_nominations(self) -> None:
        self.nominations.clear()

    def cast(self, voter: int, target: int) -> None:
        self.votes[voter] = target
        self.voted.add(voter)

    def candidates(self) -> list[int]:
        """Distinct nominated user ids, in nomination order."""
        seen: list[int] = []
        for target in self.nominations.values():
            if target not in seen:
                seen.append(target)
        return seen


@dataclass(slots=True)
class GameSession:
    """All mutable state for a running game.

    Lives in a process-local registry keyed by chat_id; see
    ``app.services.lobby``.
    """

    game_id: int
    chat_id: int
    creator_id: int
    players: dict[int, PlayerState]
    phase: GamePhase = GamePhase.NIGHT
    round_number: int = 0
    night: NightActions = field(default_factory=NightActions)
    day: DayVotes = field(default_factory=DayVotes)
    last_healed: Optional[int] = None   # doctor cannot heal same target twice
    last_blocked: Optional[int] = None  # whore cannot block same target twice
    last_detective_check: Optional[tuple[int, bool]] = None
    # Result of the don's search: (target_id, is_detective).
    don_check_result: Optional[tuple[int, bool]] = None
    # The doctor may heal himself only once per game (True Mafia rule).
    doctor_self_heal_used: bool = False
    # Consecutive missed night actions per player (anti-AFK).
    afk_strikes: dict[int, int] = field(default_factory=dict)
    # Per-game configuration (timings + optional roles).
    settings: GameSettings = field(default_factory=GameSettings)
    # During the last-word window this holds the lynched player's id.
    awaiting_last_word_from: Optional[int] = None
    # True once the bot has successfully muted the chat for a night.
    mute_enabled: bool = False
    # User ids that were actually muted this night.
    muted_user_ids: set[int] = field(default_factory=set)
    # Eliminated players stay muted until the game is over.
    permanently_muted: set[int] = field(default_factory=set)
    # Message ids of transient group announcements, dropped on transition.
    phase_message_ids: dict[str, int] = field(default_factory=dict)
    # --- achievement counters (per game, never persisted) ---------------
    # user_id -> how many times they pulled it off this game.
    stat_kills: dict[int, int] = field(default_factory=dict)
    stat_heals: dict[int, int] = field(default_factory=dict)
    stat_blocks: dict[int, int] = field(default_factory=dict)
    stat_correct_checks: dict[int, int] = field(default_factory=dict)
    # Detectives whose very first check landed on a mafia member.
    first_check_hits: set[int] = field(default_factory=set)
    # Detectives who have already used their first check.
    checked_once: set[int] = field(default_factory=set)
    # Everyone the table lynched during the game.
    lynched_ids: set[int] = field(default_factory=set)
    # Role-card outcome for this game: user_id -> honoured (False = refunded).
    card_results: dict[int, bool] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # --- players helpers -------------------------------------------------

    @property
    def alive_players(self) -> list[PlayerState]:
        return [p for p in self.players.values() if p.is_alive]

    @property
    def dead_players(self) -> list[PlayerState]:
        return [p for p in self.players.values() if not p.is_alive]

    def get(self, user_id: int) -> Optional[PlayerState]:
        return self.players.get(user_id)

    def bump_stat(self, counter: dict[int, int], user_id: Optional[int]) -> None:
        """Increment a per-game achievement counter, ignoring ``None``."""
        if user_id is None:
            return
        counter[user_id] = counter.get(user_id, 0) + 1

    def alive_of(self, role: Role) -> list[PlayerState]:
        return [p for p in self.alive_players if p.role is role]

    def alive_mafia_side(self) -> list[PlayerState]:
        """Alive players counted for the mafia win condition (incl. lawyer)."""
        return [p for p in self.alive_players if p.role in MAFIA_SIDE_ROLES]

    def alive_mafia_killers(self) -> list[PlayerState]:
        """Alive mafia who vote for the nightly victim (mafia + don).

        The lawyer is excluded: he does not know the family.
        """
        return [p for p in self.alive_players if p.role in KILLER_MAFIA_ROLES]

    def alive_roles(self) -> set[Role]:
        return {p.role for p in self.alive_players}

    def count_alive(self, role: Role) -> int:
        return sum(
            1 for p in self.players.values() if p.is_alive and p.role is role
        )

    # --- role succession -------------------------------------------------

    def promote_don(self) -> Optional[PlayerState]:
        """Promote a mafia to don when the don dies (True Mafia rule).

        Only applies when the don role is enabled for this game. Returns the
        newly promoted player, or ``None`` if nothing changed.
        """
        # NOTE: we deliberately do NOT gate this on ``settings.enable_don``.
        # What matters is whether a don actually took part in THIS game
        # (checked below); relying on the flag silently broke succession
        # whenever the settings object was not carried over to the session.
        if any(p.role is Role.DON for p in self.alive_players):
            return None
        # Only promote if a don actually existed and has since died.
        if not any(p.role is Role.DON for p in self.players.values()):
            return None
        heir = next((p for p in self.alive_players if p.role is Role.MAFIA), None)
        if heir is None:
            return None
        heir.role = Role.DON
        return heir

    def promote_sergeant(self) -> Optional[PlayerState]:
        """Promote the sergeant to detective when the detective dies."""
        if any(p.role is Role.DETECTIVE for p in self.alive_players):
            return None
        heir = next(
            (p for p in self.alive_players if p.role is Role.SERGEANT), None
        )
        if heir is None:
            return None
        heir.role = Role.DETECTIVE
        return heir

    def apply_succession(self) -> list[tuple[PlayerState, Role]]:
        """Run every inheritance rule after deaths were applied.

        Returns a list of ``(player, new_role)`` pairs so the caller can
        notify the promoted players privately.
        """
        promoted: list[tuple[PlayerState, Role]] = []
        new_don = self.promote_don()
        if new_don is not None:
            promoted.append((new_don, Role.DON))
        new_detective = self.promote_sergeant()
        if new_detective is not None:
            promoted.append((new_detective, Role.DETECTIVE))
        return promoted

    # --- night / day reset helpers --------------------------------------

    def begin_night(self) -> None:
        self.round_number += 1
        self.phase = GamePhase.NIGHT
        self.night.reset()
        # Clear the previous night's results so a role that does not act is
        # not re-notified of a stale verdict.
        self.last_detective_check = None
        self.don_check_result = None

    def begin_nomination(self) -> None:
        self.phase = GamePhase.DAY_NOMINATION
        self.day.reset()
        self.day.reset_nominations()

    def begin_vote(self) -> None:
        self.phase = GamePhase.DAY_VOTE
        self.day.reset()

    # --- anti-AFK ---------------------------------------------------------

    def record_night_activity(self) -> list[PlayerState]:
        """Update AFK strike counters at the end of a night.

        Players who had something to do but did not act gain a strike;
        players who acted have their counter cleared. Returns the players
        who are now considered AFK.
        """
        from app.game.constants import AFK_STRIKES_LIMIT
        from app.game.enums import NIGHT_ACTOR_ROLES

        afk: list[PlayerState] = []
        for player in self.alive_players:
            if player.role not in NIGHT_ACTOR_ROLES:
                continue
            if player.user_id in self.night.acted:
                self.afk_strikes.pop(player.user_id, None)
                continue
            strikes = self.afk_strikes.get(player.user_id, 0) + 1
            self.afk_strikes[player.user_id] = strikes
            if strikes >= AFK_STRIKES_LIMIT:
                afk.append(player)
        return afk

    def is_afk(self, user_id: int) -> bool:
        """True if the player has missed too many actions in a row."""
        from app.game.constants import AFK_STRIKES_LIMIT

        if not self.settings.afk_autoskip:
            return False
        return self.afk_strikes.get(user_id, 0) >= AFK_STRIKES_LIMIT

    # --- win-condition check --------------------------------------------

    def evaluate_winner(self) -> Optional[Winner]:
        """Return the winner if the game has ended, else ``None``.

        Three factions can win:
          * CITY   - every mafia and third-party killer is dead.
          * MAFIA  - no third party left and mafia >= town.
          * MANIAC - the maniac outlives (or is guaranteed to outlive) all.
        """
        alive = self.alive_players
        mafia_alive = sum(1 for p in alive if p.role in MAFIA_SIDE_ROLES)
        third_alive = sum(1 for p in alive if p.role in THIRD_PARTY_ROLES)
        town_alive = len(alive) - mafia_alive - third_alive

        # Everybody is dead - nobody wins.
        if not alive:
            return Winner.NONE

        # Town cleared every threat.
        if mafia_alive == 0 and third_alive == 0:
            return Winner.CITY

        # Maniac is the last one standing, or is alone against a single
        # townsperson he is guaranteed to kill tonight.
        if third_alive > 0 and mafia_alive == 0 and town_alive <= 1:
            return Winner.MANIAC

        # Mafia reached parity - but only once no third party can spoil it.
        if third_alive == 0 and mafia_alive > 0 and mafia_alive >= town_alive:
            return Winner.MAFIA

        return None
