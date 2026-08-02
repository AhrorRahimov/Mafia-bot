"""Entry point: run the bot with long-polling.

Usage:
    python run.py
"""
import asyncio
import logging

from app.main import main
from app.config import settings


if __name__ == "__main__":
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    # Reduce verbosity of noisy libraries.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped by user.")
