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
    database_path: Path
    schedule_file: Path


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Заполните secrets/.env: BOT_TOKEN обязателен")
    admins = frozenset(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
    return Settings(
        bot_token=token,
        admin_ids=admins,
        group_name=os.getenv("GROUP_NAME", "ИКБО-12-24"),
        database_path=ROOT / os.getenv("DATABASE_PATH", "data/queue.sqlite3"),
        schedule_file=ROOT / os.getenv("SCHEDULE_FILE", "schedule/IKBO-12-24.ics"),
    )


settings = load_settings()
