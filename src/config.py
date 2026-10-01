import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "secrets" / ".env")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    group_name: str
    schedule_api_base: str
    database_path: Path
    schedule_proxy: str | None
    vless_url: str | None


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Заполните secrets/.env: BOT_TOKEN обязателен")
    admins = frozenset(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
    return Settings(token, admins, os.getenv("GROUP_NAME", "ИКБО-12-24"),
                    os.getenv("SCHEDULE_API_BASE", "https://schedule-of.mirea.ru").rstrip("/"),
                    ROOT / os.getenv("DATABASE_PATH", "data/queue.sqlite3"),
                    os.getenv("SCHEDULE_PROXY", "").strip() or None,
                    os.getenv("VLESS_URL", "").strip() or None)


settings = load_settings()
