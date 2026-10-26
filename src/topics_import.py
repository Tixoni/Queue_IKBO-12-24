"""Read (owner name, topic) pairs from a Google Sheets link for /import_topics."""
import csv
import io
import re

import httpx

from src.bot.formatting import clean_text

HEADER_WORDS = ("фио", "имя", "студент", "name")


def csv_export_url(sheet_url: str) -> str | None:
    match = re.search(r"docs\.google\.com/spreadsheets/d/([\w-]+)", sheet_url)
    if not match:
        return None
    gid = re.search(r"[#&?]gid=(\d+)", sheet_url)
    return f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=csv&gid={gid.group(1) if gid else 0}"


def parse_rows(text: str, max_topic_length: int = 80) -> list[tuple[str, str]]:
    """First column — name, second — topic. Skips the header, empty topics and unfinished ones
    ("Приготовление" when other rows say "Приготовление шурпы"); for a repeated name the last row wins."""
    rows = []
    for record in csv.reader(io.StringIO(text)):
        if len(record) < 2:
            continue
        name, topic = clean_text(record[0])[:60], clean_text(record[1])[:max_topic_length]
        if name and topic and not name.casefold().startswith(HEADER_WORDS):
            rows.append((name, topic))
    topics = {topic for _, topic in rows}
    by_owner: dict[str, tuple[str, str]] = {}
    for name, topic in rows:
        if any(other.startswith(topic + " ") for other in topics):
            continue  # a template that this person has not filled in yet
        by_owner[name.casefold()] = (name, topic)
    return list(by_owner.values())


async def fetch_rows(sheet_url: str) -> list[tuple[str, str]]:
    export_url = csv_export_url(sheet_url)
    if not export_url:
        raise ValueError("Это не ссылка на Google Таблицу")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(export_url)
    if response.status_code != 200 or "text/csv" not in response.headers.get("content-type", ""):
        raise ValueError("Таблица недоступна: откройте доступ «Все, у кого есть ссылка» (просмотр)")
    return parse_rows(response.text)
