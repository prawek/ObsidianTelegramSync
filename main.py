"""
Entry point for the Obsidian → Telegram Sync Bot.

Supports two run modes:
  - Polling   (local development, no public URL needed)
  - Webhook   (production on Render — set WEBHOOK_URL env var)
"""
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram.ext import Application

from bot import build_application
from config import Config
from database import Database

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    cfg = Config.from_env()
    db = Database(cfg.db_path)

    logger.info("Starting Obsidian Sync Bot")
    logger.info("DB path: %s", cfg.db_path)

    app = build_application(cfg, db)

    if cfg.webhook_url:
        # ── Webhook mode (Render / any HTTPS host) ─────────────────────────
        webhook_path = f"/webhook/{cfg.bot_token}"
        full_url = cfg.webhook_url.rstrip("/") + webhook_path

        logger.info("Starting webhook on port %d → %s", cfg.webhook_port, full_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=cfg.webhook_port,
            url_path=webhook_path,
            webhook_url=full_url,
            secret_token=cfg.webhook_secret,
            drop_pending_updates=True,
        )
    else:
        # ── Polling mode (local development) ──────────────────────────────
        logger.info("Starting in polling mode")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
