"""Скачивает свежее расписание группы в schedule/*.ics (запускать на своём ПК, с российского IP).

    python tools/update_schedule.py              # группа и файл из настроек (GROUP_NAME, SCHEDULE_FILE)
    python tools/update_schedule.py ИКБО-12-24 schedule/IKBO-12-24.ics

Затем закоммитьте файл и запушьте — бот на сервере подхватит его после деплоя.
"""
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BOT_TOKEN", "unused")  # src.config requires it; the script never talks to Telegram

from src.config import settings  # noqa: E402
from src.schedule import download_calendar, parse_calendar  # noqa: E402


async def main() -> None:
    group = sys.argv[1] if len(sys.argv) > 1 else settings.group_name
    path = Path(sys.argv[2]) if len(sys.argv) > 2 else settings.schedule_file
    content = await download_calendar(group)
    lessons = parse_calendar(content, group, date.today())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    print(f"Сохранено: {path} ({len(content)} байт), пар в ближайшие 2 недели: {len(lessons)}")


if __name__ == "__main__":
    asyncio.run(main())
