"""
Configuration management for the Obsidian → Telegram sync bot.
All settings are read from environment variables.
"""
import os
from dataclasses import dataclass


@dataclass
class Config:
    # Telegram
    bot_token: str
    allowed_user_ids: frozenset[int]
    supergroup_id: int | None  # set once bot is added to the group

    # Storage
    db_path: str

    # Sync behaviour
    max_message_length: int  # Telegram cap is 4096 chars
    topic_icon_color: int    # default forum topic icon colour (RGB int)

    # Optional webhook (for Render deployment)
    webhook_url: str | None
    webhook_port: int
    webhook_secret: str | None

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("BOT_TOKEN")
        if not token:
            raise ValueError("BOT_TOKEN environment variable is required")

        raw_user_ids = os.environ.get("ALLOWED_USER_IDS", "")
        if not raw_user_ids.strip():
            raise ValueError("ALLOWED_USER_IDS environment variable is required")
        try:
            allowed_user_ids = frozenset(
                int(value.strip()) for value in raw_user_ids.split(",")
            )
        except ValueError as exc:
            raise ValueError(
                "ALLOWED_USER_IDS must contain positive Telegram user IDs separated by commas"
            ) from exc
        if any(user_id <= 0 for user_id in allowed_user_ids):
            raise ValueError("ALLOWED_USER_IDS must contain only positive Telegram user IDs")

        raw_group = os.environ.get("SUPERGROUP_ID")
        group_id = int(raw_group) if raw_group else None

        return cls(
            bot_token=token,
            allowed_user_ids=allowed_user_ids,
            supergroup_id=group_id,
            db_path=os.environ.get("DB_PATH", "data/sync.db"),
            max_message_length=int(os.environ.get("MAX_MESSAGE_LENGTH", "4000")),
            topic_icon_color=int(os.environ.get("TOPIC_ICON_COLOR", "7322096")),
            webhook_url=os.environ.get("WEBHOOK_URL"),
            webhook_port=int(os.environ.get("PORT", "8443")),
            webhook_secret=os.environ.get("WEBHOOK_SECRET"),
        )
