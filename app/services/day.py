"""Day nomination, voting and lynching."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.game.constants import SKIP_VOTE_ID
from app.game.exceptions import VoteError
from app.services.session import GameSession, PlayerState

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VoteResult:
    """Outcome of the daytime vote."""

    lynched: Optional[PlayerState] = None
    tally: dict[int, int] = field(default_factory=dict)
    total_votes: int = 0
    # True when the table explicitly voted to skip the lynching.
    skipped: bool = False
    # True when the vote was tied and therefore nobody was lynched.
    tied: bool = False


class DayService:
    """Collects and resolves daytime nominations and lynching votes."""

    def __init__(self, session: GameSession) -> None:
        self._s = session

    # --- nomination stage -------------------------------------------------

    def nominate(self, nominator_id: int, target_id: int) -> None:
        """Put a player on trial. Each player may nominate one candidate."""
        nominator = self._s.get(nominator_id)
        if nominator is None:
            raise VoteError("errors.not_participant")
        if not nominator.is_alive:
            raise VoteError("errors.dead_no_vote")
        target = self._s.get(target_id)
        if target is None or not target.is_alive:
            raise VoteError("errors.vote_alive_only")
        if target_id == nominator_id:
            raise VoteError("errors.no_self_nomination")

        self._s.day.nominations[nominator_id] = target_id

    def candidates(self) -> list[PlayerState]:
        """Alive nominated players; falls back to everyone when empty."""
        nominated = [
            player
            for player in (self._s.get(uid) for uid in self._s.day.candidates())
            if player is not None and player.is_alive
        ]
        return nominated or list(self._s.alive_players)

    def all_required_nominated(self) -> bool:
        required = {p.user_id for p in self._s.alive_players}
        return required.issubset(set(self._s.day.nominations))

    # --- voting -----------------------------------------------------------

    def cast_vote(self, voter_id: int, target_id: int) -> None:
        voter = self._s.get(voter_id)
        if voter is None:
            raise VoteError("errors.not_participant")
        if not voter.is_alive:
            raise VoteError("errors.dead_no_vote")

        # "Skip the lynching" ballot.
        if target_id == SKIP_VOTE_ID:
            if not self._s.settings.allow_skip_vote:
                raise VoteError("errors.skip_not_allowed")
            self._s.day.cast(voter_id, SKIP_VOTE_ID)
            return

        target = self._s.get(target_id)
        if target is None or not target.is_alive:
            raise VoteError("errors.vote_alive_only")
        if target_id == voter_id:
            raise VoteError("errors.no_self_vote")

        # In nomination mode only players on trial may be voted for.
        if self._s.settings.nomination_mode and self._s.day.nominations:
            allowed = {p.user_id for p in self.candidates()}
            if target_id not in allowed:
                raise VoteError("errors.not_nominated")

        # Allow changing the vote until the phase ends.
        self._s.day.cast(voter_id, target_id)
        logger.debug(
            "Vote: chat=%s voter=%s target=%s",
            self._s.chat_id, voter_id, target_id,
        )

    def has_voted(self, user_id: int) -> bool:
        return user_id in self._s.day.voted

    def all_required_voted(self) -> bool:
        required = {p.user_id for p in self._s.alive_players}
        return required.issubset(self._s.day.voted)

    def resolve(self) -> VoteResult:
        """Tally votes; the highest-voted player is lynched.

        Nobody is lynched when the vote is tied or when "skip" wins.
        """
        votes = self._s.day.votes
        if not votes:
            self._s.day.reset()
            return VoteResult(lynched=None, tally={}, total_votes=0)

        tally: dict[int, int] = {}
        for target_id in votes.values():
            tally[target_id] = tally.get(target_id, 0) + 1

        max_votes = max(tally.values())
        top = [uid for uid, n in tally.items() if n == max_votes]

        lynched: Optional[PlayerState] = None
        skipped = False
        tied = len(top) > 1
        if not tied:
            winner_id = top[0]
            if winner_id == SKIP_VOTE_ID:
                skipped = True
            else:
                lynched = self._s.get(winner_id)

        logger.info(
            "Vote resolved: chat=%s round=%s lynched=%s skipped=%s tied=%s",
            self._s.chat_id, self._s.round_number,
            getattr(lynched, "user_id", None), skipped, tied,
        )
        total = sum(tally.values())
        self._s.day.reset()
        return VoteResult(
            lynched=lynched,
            tally=tally,
            total_votes=total,
            skipped=skipped,
            tied=tied,
        )
