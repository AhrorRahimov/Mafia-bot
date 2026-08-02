"""User-facing text builders parameterised by a translator.

Centralising copy in one module keeps handlers short. Every function
takes ``t`` (a ``Translator`` callable bound to the user's language)
as its first argument so the resulting string is in the right locale.
"""
from __future__ import annotations

from typing import Callable, Iterable

from app.db.models import UserStats
from app.game.constants import MAX_PLAYERS, MIN_PLAYERS
from app.game.enums import Role, Winner
from app.services.session import GameSession, PlayerState


# --- Translator type hint ---------------------------------------------

# A Translator is a callable: t(key: str, **kwargs) -> str
Translator = Callable[..., str]


# --- Lobby -------------------------------------------------------------

def lobby_opened(t: Translator, creator_name: str, players: Iterable[str]) -> str:
    player_list = "\n".join(f"• {name}" for name in players)
    if not player_list:
        player_list = t("lobby.players_empty")
    return t(
        "lobby.opened",
        creator=creator_name,
        players=player_list,
        min=MIN_PLAYERS,
        max=MAX_PLAYERS,
    )


def lobby_opened_countdown(
    t: Translator,
    creator_name: str,
    players: Iterable[str],
    remaining_seconds: int,
) -> str:
    """Lobby card with a live countdown to auto-start / dissolve.

    Used by the periodic card refresh so players always see how long they
    have left to gather. ``remaining_seconds`` is clamped to >=0 upstream.
    """
    player_list = "\n".join(f"• {name}" for name in players)
    if not player_list:
        player_list = t("lobby.players_empty")
    return t(
        "lobby.opened_countdown",
        creator=creator_name,
        players=player_list,
        min=MIN_PLAYERS,
        max=MAX_PLAYERS,
        seconds=remaining_seconds,
    )


# --- Roles -------------------------------------------------------------

def your_role(t: Translator, role: Role, extra: str = "") -> str:
    title = t(f"role.{role.value}.title")
    description = t(f"role.{role.value}.description")
    footer = t("role.your_role_footer")
    text = f"<b>{title}</b>\n\n{description}\n\n{footer}"
    if extra:
        text += f"\n\n{extra}"
    return text


def mafia_teammates(t: Translator, teammates: list[PlayerState]) -> str:
    if not teammates:
        return ""
    names = ", ".join(p.full_name for p in teammates)
    return t("role.mafia_teammates", names=names)


def mafia_extra_for(
    t: Translator, game: GameSession, user_id: int
) -> str:
    """Build the optional "your teammates" block for a mafia-side player.

    True-Mafia secrecy rules:
      * A plain mafia / don *knows* the rest of the kill-voting family
        (mafia + don), but **never** the lawyer.
      * The lawyer is mafia-aligned yet acts blind: he does **not** know
        the family, so he gets no teammates block at all.

    Returning ``""`` means "no extra block" — the caller appends nothing.
    """
    from app.game.enums import Role  # local import avoids a cycle at import time

    player = game.players.get(user_id)
    if player is None:
        return ""
    # The lawyer never learns the family.
    if player.role is Role.LAWYER:
        return ""
    teammates = [
        p
        for p in game.alive_mafia_killers()
        if p.user_id != user_id
    ]
    return mafia_teammates(t, teammates)


def role_title(t: Translator, role: Role) -> str:
    """Localised role title (used in lynching announcements, reveal)."""
    return t(f"role.{role.value}.title")


def role_reveal_line(t: Translator, name: str, role: Role) -> str:
    return t("role.reveal_line", name=name, role=role_title(t, role))


def role_reveal_header(t: Translator) -> str:
    return t("role.reveal_header")


# --- Detective ---------------------------------------------------------

def detective_result(t: Translator, target_name: str, is_mafia: bool) -> str:
    verdict_key = (
        "night.detective_verdict_mafia"
        if is_mafia
        else "night.detective_verdict_clean"
    )
    return t(
        "night.detective_result",
        name=target_name,
        verdict=t(verdict_key),
    )


# --- Day / vote --------------------------------------------------------

def night_killed(t: Translator, name: str) -> str:
    return t("night.killed", name=name)


def vote_result_lynch(
    t: Translator, name: str, role: Role, reveal: bool = True
) -> str:
    """Announce the lynching.

    When ``reveal`` is False (the "blind" game mode) the victim's role is
    kept secret.
    """
    if not reveal:
        return t("day.vote_result_lynch_hidden", name=name)
    return t("day.vote_result_lynch", name=name, role=role_title(t, role))


def vote_result_no_lynch(t: Translator) -> str:
    return t("day.vote_result_no_lynch")


def vote_result_skipped(t: Translator) -> str:
    return t("day.vote_result_skipped")


# --- End of game -------------------------------------------------------

def game_over(t: Translator, winner: Winner) -> str:
    if winner is Winner.MAFIA:
        return t("game_over.mafia_wins")
    if winner is Winner.CITY:
        return t("game_over.city_wins")
    if winner is Winner.MANIAC:
        return t("game_over.maniac_wins")
    return t("game_over.none")


def don_search_result(t: Translator, name: str, is_detective: bool) -> str:
    """Private answer to the don's nightly search."""
    key = (
        "night.don_result_found" if is_detective
        else "night.don_result_not_found"
    )
    return t(key, name=name)


def sergeant_report(t: Translator, name: str, is_mafia: bool) -> str:
    """Copy of the detective's verdict, forwarded to the sergeant."""
    verdict_key = (
        "night.detective_verdict_mafia" if is_mafia
        else "night.detective_verdict_clean"
    )
    return t("night.sergeant_report", name=name, verdict=t(verdict_key))


def leaderboard_text(t: Translator, rows) -> str:
    """Render the /top leaderboard from a list of UserStats rows."""
    if not rows:
        return t("top.empty")
    lines = [t("top.header"), ""]
    for place, stats in enumerate(rows, start=1):
        winrate = (
            f"{(stats.wins / stats.games_played * 100):.0f}%"
            if stats.games_played
            else "-"
        )
        lines.append(
            t(
                "top.line",
                place=place,
                name=stats.full_name,
                wins=stats.wins,
                played=stats.games_played,
                winrate=winrate,
            )
        )
    return "\n".join(lines)


# --- Stats -------------------------------------------------------------

def stats_text(t: Translator, stats: UserStats | None) -> str:
    if stats is None:
        return t("stats.empty")
    winrate = (
        f"{(stats.wins / stats.games_played * 100):.0f}%"
        if stats.games_played
        else "—"
    )
    return t(
        "stats.text",
        played=stats.games_played,
        wins=stats.wins,
        losses=stats.losses,
        winrate=winrate,
    )


# --- Helpers -----------------------------------------------------------

def player_names(session: GameSession, *, alive_only: bool = False) -> list[str]:
    players = session.alive_players if alive_only else list(session.players.values())
    return [p.full_name for p in players]


# --- Leaderboards & profile -------------------------------------------

# Boards offered by ``/top``. "season" is the MMR ladder, the rest are
# all-time boards computed from ``user_stats``.
TOP_BOARDS = ("season", "wins", "coins", "winrate", "streak")

# In a group the table of that group comes first: players there care who
# is the best at *their* table before caring about the global ladder.
# "chat" is unavailable in a private chat, where there is no group.
CHAT_BOARD = "chat"
GROUP_TOP_BOARDS = (CHAT_BOARD,) + TOP_BOARDS


def _winrate(wins: int, played: int) -> str:
    return f"{wins / played * 100:.0f}%" if played else "-"


def season_top_text(t: Translator, season_name: str, rows) -> str:
    """MMR ladder for the running season."""
    if not rows:
        return t("top.empty")
    lines = [t("top.season_header", season=season_name), ""]
    for place, (rating, name) in enumerate(rows, start=1):
        lines.append(
            t(
                "top.season_line",
                place=place,
                medal=medal(place),
                name=name,
                mmr=int(rating.mmr or 0),
                games=int(rating.games or 0),
                wins=int(rating.wins or 0),
            )
        )
    return "\n".join(lines)


def medal(place: int) -> str:
    """Medal for the first three places, a plain number otherwise."""
    return {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(
        place, f"{place}."
    )


def board_text(t: Translator, board: str, rows) -> str:
    """Render one of the all-time boards."""
    if not rows:
        return t("top.empty")
    lines = [t(f"top.header.{board}"), ""]
    for place, stats in enumerate(rows, start=1):
        lines.append(
            t(
                f"top.line.{board}",
                place=place,
                medal=medal(place),
                name=stats.full_name,
                wins=int(stats.wins or 0),
                played=int(stats.games_played or 0),
                coins=int(getattr(stats, "coins", 0) or 0),
                streak=int(getattr(stats, "best_streak", 0) or 0),
                winrate=_winrate(
                    int(stats.wins or 0), int(stats.games_played or 0)
                ),
            )
        )
    return "\n".join(lines)


def chat_board_text(t: Translator, rows, *, title: str) -> str:
    """Leaderboard of one group chat.

    ``rows`` are plain dicts from ``ChatLeaderboardRepo`` rather than
    ``UserStats``, because these numbers are counted from the games
    played in this chat only.
    """
    if not rows:
        return t("top.chat_empty")
    lines = [t("top.header.chat", chat=title), ""]
    for place, row in enumerate(rows, start=1):
        played = int(row["played"] or 0)
        wins = int(row["wins"] or 0)
        lines.append(
            t(
                "top.line.chat",
                place=place,
                medal=medal(place),
                name=row["full_name"],
                wins=wins,
                played=played,
                winrate=_winrate(wins, played),
            )
        )
    return "\n".join(lines)


def profile_text(t: Translator, snapshot, *, achievements_total: int) -> str:
    """The ``/me`` card: rating, streaks, roles, badges, achievements."""
    stats = snapshot["stats"]
    played = int(getattr(stats, "games_played", 0) or 0)
    wins = int(getattr(stats, "wins", 0) or 0)
    name = getattr(stats, "full_name", "") or t("profile.anonymous")

    # One figure per line, grouped under section headers. Cramming them
    # into a single separator-joined line made the card unreadable on a
    # phone, where it wrapped at arbitrary points.
    lines = [
        t("profile.header", name=name),
        "",
        t("profile.stats_header"),
        t("profile.played", played=played),
        t("profile.wins", wins=wins),
        t("profile.losses", losses=max(0, played - wins)),
        t("profile.winrate", winrate=_winrate(wins, played)),
        "",
        t("profile.rating_header"),
        t("profile.mmr", mmr=snapshot["mmr"]),
        t(
            "profile.season",
            season=getattr(snapshot["season"], "name", "-"),
        ),
        t("profile.rank", rank=snapshot["rank"] or "-"),
        "",
        t("profile.streak_header"),
        t(
            "profile.streak_current",
            current=int(getattr(stats, "win_streak", 0) or 0),
        ),
        t(
            "profile.streak_best",
            best=int(getattr(stats, "best_streak", 0) or 0),
        ),
        "",
        t("profile.coins", coins=int(getattr(stats, "coins", 0) or 0)),
    ]

    roles = list(snapshot.get("roles") or [])
    lines.append("")
    lines.append(t("profile.roles_header"))
    if roles:
        favourite = max(roles, key=lambda row: int(row.games or 0))
        for row in roles:
            lines.append(
                t(
                    "profile.role_line",
                    role=t(f"role.{row.role}.title"),
                    games=int(row.games or 0),
                    wins=int(row.wins or 0),
                    winrate=_winrate(int(row.wins or 0), int(row.games or 0)),
                )
            )
        lines.append(
            t("profile.favourite", role=t(f"role.{favourite.role}.title"))
        )
    else:
        lines.append(t("profile.roles_empty"))

    unlocked = snapshot.get("achievements") or set()
    lines.append("")
    lines.append(
        t(
            "profile.achievements",
            count=len(unlocked),
            total=achievements_total,
        )
    )
    recent = snapshot.get("recent") or []
    for row in recent:
        lines.append(
            t("profile.achievement_line", name=t(f"achv.{row.code}.name"))
        )
    return "\n".join(lines)
