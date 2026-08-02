"""Night actions: mafia kill, detective check, doctor heal, whore block,
maniac kill, lawyer defence and the don's search for the detective."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.game.enums import (
    KILLER_MAFIA_ROLES,
    MAFIA_SIDE_ROLES,
    NIGHT_ACTOR_ROLES,
    Role,
)
from app.game.exceptions import RoleError, TargetError
from app.services.session import GameSession, PlayerState

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NightOutcome:
    """Resolved outcome of a single night."""

    # Everyone who died tonight (mafia victim and/or maniac victim).
    deaths: list[PlayerState] = field(default_factory=list)
    healed: Optional[PlayerState] = None      # who the doctor saved
    detective_suspect: Optional[PlayerState] = None
    detective_is_mafia: Optional[bool] = None
    blocked: Optional[PlayerState] = None     # who the whore blocked
    # Who the detective shot tonight (None if he checked instead).
    detective_shot: Optional[PlayerState] = None
    # (target, is_detective) - result of the don's search.
    don_check: Optional[tuple[PlayerState, bool]] = None

    @property
    def killed(self) -> Optional[PlayerState]:
        """Backwards-compatible accessor: the first victim, if any."""
        return self.deaths[0] if self.deaths else None


class NightService:
    """Validates and collects night actions for a game session."""

    def __init__(self, session: GameSession) -> None:
        self._s = session

    # --- eligibility checks ---------------------------------------------

    def _require_alive_actor(self, user_id: int, role: Role) -> PlayerState:
        player = self._s.get(user_id)
        if player is None:
            raise RoleError("errors.not_participant")
        if not player.is_alive:
            raise RoleError("errors.dead_no_speak")
        if player.role is not role:
            raise RoleError("errors.wrong_role")
        return player

    def _require_alive_killer_mafia(self, user_id: int) -> PlayerState:
        """Accepts mafia and don - the roles that vote for the night kill."""
        player = self._s.get(user_id)
        if player is None:
            raise RoleError("errors.not_participant")
        if not player.is_alive:
            raise RoleError("errors.dead_no_speak")
        if player.role not in KILLER_MAFIA_ROLES:
            raise RoleError("errors.wrong_role")
        return player

    def _require_alive_target(self, target_id: int) -> PlayerState:
        target = self._s.get(target_id)
        if target is None:
            raise TargetError("errors.target_not_found")
        if not target.is_alive:
            raise TargetError("errors.target_already_dead")
        return target

    def has_acted(self, user_id: int) -> bool:
        return user_id in self._s.night.acted

    # --- actions ---------------------------------------------------------

    def mafia_kill(self, actor_id: int, target_id: int) -> None:
        self._require_alive_killer_mafia(actor_id)
        target = self._require_alive_target(target_id)
        if target.role in MAFIA_SIDE_ROLES:
            raise TargetError("errors.mafia_cant_kill_self_team")
        if actor_id in self._s.night.acted:
            raise RoleError("errors.already_acted_night")

        # Team vote: the don (if any) decides; otherwise the most-voted
        # target wins (on a tie the first-chosen target is kept).
        self._s.night.mafia_votes[actor_id] = target_id
        self._s.night.mafia_target = self._resolve_mafia_target()
        self._s.night.acted.add(actor_id)

    def don_search(self, actor_id: int, target_id: int) -> None:
        """The don looks for the detective.

        This is a *secondary*, optional action: it does not mark the don as
        having acted, because he still owes the family a kill vote.
        """
        self._require_alive_actor(actor_id, Role.DON)
        self._require_alive_target(target_id)
        if target_id == actor_id:
            raise TargetError("errors.don_cant_search_self")
        if self._s.night.don_check_target is not None:
            raise RoleError("errors.already_acted_night")
        self._s.night.don_check_target = target_id

    def lawyer_defend(self, actor_id: int, target_id: int) -> None:
        """The lawyer picks a client who will read "civilian" tonight."""
        self._require_alive_actor(actor_id, Role.LAWYER)
        self._require_alive_target(target_id)
        if target_id == actor_id:
            raise TargetError("errors.lawyer_cant_defend_self")
        if actor_id in self._s.night.acted:
            raise RoleError("errors.already_acted_night")
        self._s.night.lawyer_client = target_id
        self._s.night.acted.add(actor_id)

    def maniac_kill(self, actor_id: int, target_id: int) -> None:
        """The maniac kills alone - he may target anyone, including mafia."""
        self._require_alive_actor(actor_id, Role.MANIAC)
        self._require_alive_target(target_id)
        if target_id == actor_id:
            raise TargetError("errors.maniac_cant_kill_self")
        if actor_id in self._s.night.acted:
            raise RoleError("errors.already_acted_night")
        self._s.night.maniac_target = target_id
        self._s.night.acted.add(actor_id)

    def detective_check(self, actor_id: int, target_id: int) -> None:
        """The detective checks a player.

        The verdict is deliberately NOT computed here: the lawyer may still
        pick his client later the same night, which can flip the answer.
        It is resolved in :meth:`resolve`.
        """
        self._require_alive_actor(actor_id, Role.DETECTIVE)
        self._require_alive_target(target_id)
        if target_id == actor_id:
            raise TargetError("errors.detective_cant_check_self")
        if actor_id in self._s.night.acted:
            raise RoleError("errors.already_checked_night")

        self._s.night.detective_target = target_id
        self._s.night.acted.add(actor_id)

    def detective_shoot(self, actor_id: int, target_id: int) -> None:
        """The detective spends the night shooting instead of checking.

        Mutually exclusive with :meth:`detective_check`: both mark the
        detective as having acted, so he gets exactly one of the two.
        The bullet can be stopped by the doctor and is nullified if the
        whore blocked him (resolved in :meth:`resolve`).
        """
        self._require_alive_actor(actor_id, Role.DETECTIVE)
        self._require_alive_target(target_id)
        if not self._s.settings.detective_can_shoot:
            raise RoleError("errors.detective_shoot_disabled")
        if target_id == actor_id:
            raise TargetError("errors.detective_cant_shoot_self")
        if actor_id in self._s.night.acted:
            raise RoleError("errors.already_acted_night")

        self._s.night.detective_shoot_target = target_id
        self._s.night.acted.add(actor_id)

    def doctor_heal(self, actor_id: int, target_id: int) -> None:
        self._require_alive_actor(actor_id, Role.DOCTOR)
        self._require_alive_target(target_id)
        if actor_id in self._s.night.acted:
            raise RoleError("errors.already_healed_night")
        # Doctor cannot heal the same player two nights in a row.
        if self._s.last_healed is not None and target_id == self._s.last_healed:
            raise TargetError("errors.doctor_same_target_twice")
        # The doctor may heal himself only once per game.
        if target_id == actor_id:
            if self._s.doctor_self_heal_used:
                raise TargetError("errors.doctor_self_heal_used")
            self._s.doctor_self_heal_used = True

        self._s.night.doctor_target = target_id
        self._s.night.acted.add(actor_id)

    def whore_block(self, actor_id: int, target_id: int) -> None:
        """Whore/putana blocks a player's night action for this night."""
        self._require_alive_actor(actor_id, Role.WHORE)
        self._require_alive_target(target_id)
        if target_id == actor_id:
            raise TargetError("errors.whore_cant_block_self")
        if actor_id in self._s.night.acted:
            raise RoleError("errors.already_acted_night")
        if self._s.last_blocked is not None and target_id == self._s.last_blocked:
            raise TargetError("errors.whore_same_target_twice")

        self._s.night.whore_target = target_id
        self._s.night.acted.add(actor_id)

    # --- resolution ------------------------------------------------------

    def _resolve_mafia_target(
        self, exclude: Optional[set[int]] = None
    ) -> Optional[int]:
        """Decide the family's victim from the collected votes.

        The don's vote is decisive. Otherwise the most-voted target wins;
        on a tie the target seen first (insertion order) is kept.
        ``exclude`` drops blocked voters.
        """
        exclude = exclude or set()
        votes = {
            voter: target
            for voter, target in self._s.night.mafia_votes.items()
            if voter not in exclude
        }
        if not votes:
            return None

        # The don decides for the family.
        for voter, target in votes.items():
            player = self._s.get(voter)
            if player is not None and player.role is Role.DON and player.is_alive:
                return target

        tally: dict[int, int] = {}
        for target in votes.values():
            tally[target] = tally.get(target, 0) + 1
        return max(tally, key=tally.get)

    def all_required_acted(self) -> bool:
        """True if every alive role-actor has acted this night.

        Players flagged as AFK are not waited for, so a single idle player
        can no longer stall the whole table until the timer expires.
        """
        required = {
            p.user_id
            for p in self._s.alive_players
            if p.role in NIGHT_ACTOR_ROLES and not self._s.is_afk(p.user_id)
        }
        return required.issubset(self._s.night.acted)

    def resolve(self) -> NightOutcome:
        """Resolve the night and return its outcome.

        Order of operations:
          1. The whore's block nullifies the target's action.
          2. The mafia target is computed (dropping a blocked mafia voter).
          3. The maniac's solo kill is computed (unless he was blocked).
          4. The doctor's save applies unless the doctor was blocked; it can
             save from the mafia and from the maniac alike.
          5. The detective's verdict is computed, honouring the lawyer's
             disguise, unless the detective was blocked.
          6. The don's search resolves unless he was blocked.
        """
        night = self._s.night

        blocked_id = night.whore_target
        blocked_player = (
            self._s.get(blocked_id) if blocked_id is not None else None
        )
        # Carry-over so the whore cannot repeat the same target next night.
        self._s.last_blocked = blocked_id

        def blocked_role(role: Role) -> bool:
            return blocked_player is not None and blocked_player.role is role

        # 2. Mafia kill - a blocked mafia voter does not count.
        exclude: set[int] = set()
        if blocked_player is not None and blocked_player.role in KILLER_MAFIA_ROLES:
            exclude.add(blocked_player.user_id)
        mafia_target_id = self._resolve_mafia_target(exclude=exclude)

        # 3. Maniac kill - suppressed if the maniac was blocked.
        maniac_target_id = None if blocked_role(Role.MANIAC) else night.maniac_target

        # 3b. The detective's bullet - suppressed if he was blocked.
        detective_shot_id = (
            None if blocked_role(Role.DETECTIVE) else night.detective_shoot_target
        )

        # 4. Doctor save - nullified if the doctor was blocked.
        doctor_target = None if blocked_role(Role.DOCTOR) else night.doctor_target

        deaths: list[PlayerState] = []
        healed: Optional[PlayerState] = None
        seen: set[int] = set()
        for target_id in (mafia_target_id, maniac_target_id, detective_shot_id):
            if target_id is None or target_id in seen:
                continue
            seen.add(target_id)
            victim = self._s.get(target_id)
            if victim is None or not victim.is_alive:
                continue
            if doctor_target is not None and doctor_target == target_id:
                healed = victim
                continue
            deaths.append(victim)

        # Achievement counters: who actually landed something tonight.
        for victim in deaths:
            if victim.user_id == mafia_target_id:
                for killer in self._s.alive_players:
                    if killer.role in KILLER_MAFIA_ROLES:
                        self._s.bump_stat(self._s.stat_kills, killer.user_id)
            if victim.user_id == maniac_target_id:
                maniacs = self._s.alive_of(Role.MANIAC)
                if maniacs:
                    self._s.bump_stat(self._s.stat_kills, maniacs[0].user_id)
        if healed is not None:
            doctors = self._s.alive_of(Role.DOCTOR)
            if doctors:
                self._s.bump_stat(self._s.stat_heals, doctors[0].user_id)
        if blocked_player is not None:
            whores = self._s.alive_of(Role.WHORE)
            if whores:
                self._s.bump_stat(self._s.stat_blocks, whores[0].user_id)

        # Doctor restriction carry-over for next night (based on their choice).
        self._s.last_healed = night.doctor_target

        detective_shot: Optional[PlayerState] = (
            self._s.get(detective_shot_id)
            if detective_shot_id is not None
            else None
        )

        # 5. Detective verdict - the lawyer's client always reads "civilian".
        detective_suspect: Optional[PlayerState] = None
        detective_is_mafia: Optional[bool] = None
        if night.detective_target is not None and not blocked_role(Role.DETECTIVE):
            target = self._s.get(night.detective_target)
            if target is not None:
                is_mafia = target.role in MAFIA_SIDE_ROLES
                lawyer_active = (
                    night.lawyer_client is not None
                    and not blocked_role(Role.LAWYER)
                )
                if lawyer_active and night.lawyer_client == target.user_id:
                    is_mafia = False
                detective_suspect = target
                detective_is_mafia = is_mafia
                self._s.last_detective_check = (target.user_id, is_mafia)
                detectives = self._s.alive_of(Role.DETECTIVE)
                if detectives:
                    sleuth = detectives[0].user_id
                    first_check = sleuth not in self._s.checked_once
                    self._s.checked_once.add(sleuth)
                    if is_mafia:
                        self._s.bump_stat(self._s.stat_correct_checks, sleuth)
                        if first_check:
                            self._s.first_check_hits.add(sleuth)

        # 6. Don's search for the detective.
        don_check: Optional[tuple[PlayerState, bool]] = None
        if night.don_check_target is not None and not blocked_role(Role.DON):
            target = self._s.get(night.don_check_target)
            if target is not None:
                found = target.role is Role.DETECTIVE
                don_check = (target, found)
                self._s.don_check_result = (target.user_id, found)

        logger.info(
            "Night resolved: chat=%s round=%s deaths=%s healed=%s",
            self._s.chat_id,
            self._s.round_number,
            [p.user_id for p in deaths],
            getattr(healed, "user_id", None),
        )
        return NightOutcome(
            deaths=deaths,
            healed=healed,
            detective_suspect=detective_suspect,
            detective_is_mafia=detective_is_mafia,
            blocked=blocked_player,
            detective_shot=detective_shot,
            don_check=don_check,
        )
