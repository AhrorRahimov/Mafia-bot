"""SQLAlchemy 2.0 declarative models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Game(Base):
    """A single mafia game played in a chat."""

    __tablename__ = "games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    creator_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="lobby", index=True)
    winner: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    rounds_played: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    players: Mapped[list["Player"]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )


class Player(Base):
    """A participant of a game (transient records, scoped to a game)."""

    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    full_name: Mapped[str] = mapped_column(String(256))
    role: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    is_alive: Mapped[bool] = mapped_column(default=True)

    game: Mapped[Game] = relationship(back_populates="players")


class UserStats(Base):
    """Persistent per-user statistics across all games."""

    __tablename__ = "user_stats"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(256))
    games_played: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    language: Mapped[str] = mapped_column(String(8), default="ru")
    # Telegram @username (without the @), stored so admins can moderate by
    # handle instead of hunting for a numeric id.
    username: Mapped[str] = mapped_column(String(64), default="")
    # Consecutive wins; ``best_streak`` keeps the personal record.
    win_streak: Mapped[int] = mapped_column(Integer, default=0)
    best_streak: Mapped[int] = mapped_column(Integer, default=0)
    # In-game currency balance, earned by playing and spent in the shop.
    coins: Mapped[int] = mapped_column(Integer, default=0)
    # True once the user has opened the bot in a private chat (pressed
    # /start in DM). Required to join a lobby so the bot can deliver the
    # secret role and night prompts.
    has_dm: Mapped[bool] = mapped_column(default=False)


class UserPurchase(Base):
    """A cosmetic shop item owned by a user.

    One row per (user, item). The uniqueness is enforced in the repository
    layer rather than by a DB constraint so that older databases created
    before the shop existed keep working after a plain ``create_all``.
    """

    __tablename__ = "user_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    price_paid: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------


class BotAdmin(Base):
    """An admin granted rights at runtime (owners live in ``ADMIN_IDS``).

    Keeping these in the DB lets an owner promote a moderator without a
    redeploy. Owners always outrank DB admins and cannot be demoted here.
    """

    __tablename__ = "bot_admins"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    granted_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AdminAudit(Base):
    """Append-only log of every privileged action.

    The panel can hand out currency and bans, so each action records who
    did what to whom. Nothing here is ever updated or deleted by the bot.
    """

    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    target: Mapped[str] = mapped_column(String(64), default="")
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserBan(Base):
    """A play ban. ``until`` is ``None`` for a permanent ban."""

    __tablename__ = "user_bans"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reason: Mapped[str] = mapped_column(String(256), default="")
    banned_by: Mapped[int] = mapped_column(BigInteger, default=0)
    until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserWarning(Base):
    """A warning issued to a player. Rows are counted, never overwritten."""

    __tablename__ = "user_warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reason: Mapped[str] = mapped_column(String(256), default="")
    admin_id: Mapped[int] = mapped_column(BigInteger, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatBan(Base):
    """A blacklisted group: the bot refuses to run games there."""

    __tablename__ = "chat_bans"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    reason: Mapped[str] = mapped_column(String(256), default="")
    banned_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PromoCode(Base):
    """A redeemable code granting coins and/or a shop item."""

    __tablename__ = "promo_codes"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    coins: Mapped[int] = mapped_column(Integer, default=0)
    item_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PromoRedemption(Base):
    """One row per (code, user) so a code cannot be farmed twice."""

    __tablename__ = "promo_redemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BotConfig(Base):
    """Runtime key/value config: feature flags, maintenance, multipliers.

    Stored as text and parsed by ``app.services.admin.RuntimeConfig`` so
    new switches never require a schema migration.
    """

    __tablename__ = "bot_config"

    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Rating, seasons and achievements
# ---------------------------------------------------------------------------


class Season(Base):
    """A rating season. Exactly one row is active at any time.

    Seasons roll over automatically once a month: the previous one is
    closed, the top players are paid out and a fresh ladder begins.
    """

    __tablename__ = "seasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(32), default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class UserRating(Base):
    """Per-season MMR of a single player.

    Kept separate from ``user_stats`` so a season reset never touches
    lifetime statistics, coins or purchases.
    """

    __tablename__ = "user_ratings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    mmr: Mapped[int] = mapped_column(Integer, default=1000)
    games: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserRoleStat(Base):
    """Lifetime games/wins per role, powering ``/me`` and balance checks."""

    __tablename__ = "user_role_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(16), index=True)
    games: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)


class UserAchievement(Base):
    """An unlocked achievement. One row per (user, code), ever."""

    __tablename__ = "user_achievements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Inventory and role cards
# ---------------------------------------------------------------------------


class UserInventory(Base):
    """Stackable consumables owned by a player (currently role cards).

    Cosmetics stay in ``user_purchases`` because they are owned once and
    never spent; anything with a quantity lives here instead.
    """

    __tablename__ = "user_inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RoleClaim(Base):
    """An activated role card waiting to be used in the next game.

    The card leaves the inventory the moment it is activated, so a player
    cannot queue the same ticket in two lobbies at once. If the role is
    unavailable when the game starts, the claim is cancelled and the card
    is returned to the inventory.
    """

    __tablename__ = "role_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(16))
    item_id: Mapped[str] = mapped_column(String(32), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
