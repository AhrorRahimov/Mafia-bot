"""Game-level constants: timings and lobby size limits.

All user-facing role copy now lives in ``app/locales/*.json`` and is
served through the i18n layer (``app.texts.role_title``, ``your_role``).
"""
from __future__ import annotations

# --- Lobby size ---
MIN_PLAYERS = 4
MAX_PLAYERS = 10

# --- Lobby gathering timer ---
# After /newgame, the lobby has this long to gather players. On expiry the
# game auto-starts if enough players joined, otherwise the lobby dissolves.
LOBBY_TIMEOUT = 60
# /extend and /shorten adjust the remaining gathering time by this step.
LOBBY_EXTEND_STEP = 30
# Hard ceiling on total gathering time so /extend cannot run forever.
LOBBY_MAX_TIMEOUT = 300
# Hard floor when /shorten is used, so players are not caught off guard.
LOBBY_MIN_TIMEOUT = 10
# How often the live lobby card is refreshed (seconds).
LOBBY_REFRESH_INTERVAL = 15

# --- Phase timings (seconds) ---
NIGHT_DURATION = 45
DAY_DISCUSSION_DURATION = 60
DAY_VOTE_DURATION = 60
# Window a lynched player gets to type a final message (0 disables the feature).
LAST_WORD_DURATION = 20

# --- Phase countdown reminders ---
# Seconds-remaining marks at which the bot posts a "time left" reminder.
# A reminder is only sent when the phase is comfortably longer than the mark.
PHASE_REMINDER_SECONDS = 10

# --- Configurable presets (used by the in-lobby settings menu) ---
# The settings menu cycles through these values on each tap.
NIGHT_DURATION_PRESETS = (30, 45, 60, 90)
DISCUSSION_DURATION_PRESETS = (30, 60, 90, 120)
VOTE_DURATION_PRESETS = (30, 60, 90, 120)

# --- Callback data limits ---
MAX_INLINE_BUTTON_LABEL = 64  # Telegram inline button text limit

# --- Nomination stage ---
# How long players have to nominate candidates for the trial (when the
# ``nomination_mode`` setting is on).
NOMINATION_DURATION = 30

# --- Day vote ---
# Pseudo user-id used as the target of a "Skip the vote" ballot. Real
# Telegram user ids are always positive, so 0 can never collide.
SKIP_VOTE_ID = 0

# --- Anti-AFK ---
# How many consecutive nights a player may miss their action before the bot
# stops waiting for them (they are skipped, never auto-killed).
AFK_STRIKES_LIMIT = 2

# --- Economy (coins earned for playing) ---
# Awarded in ``end_game`` to every participant.
COINS_PER_GAME = 10        # simply for finishing a game
COINS_PER_WIN = 25         # extra for being on the winning side
COINS_SURVIVOR_BONUS = 5   # extra for staying alive to the end


# --- Moderation ---
# Warnings are cumulative: the Nth active warning converts into a
# temporary play ban, after which the counter resets.
WARN_BAN_THRESHOLD = 3
WARN_BAN_DAYS = 7
