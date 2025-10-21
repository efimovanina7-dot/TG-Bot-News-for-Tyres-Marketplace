# -*- coding: utf-8 -*-
"""
Ежедневный мониторинг изменений комиссий/логистики на Ozon / Яндекс.Маркет / Wildberries.
- Читает список страниц из data/urls.json
- Для docs.ozon.ru / seller.ozon.ru использует cloudscraper, чтобы обходить 403/JS-чек
- Считает SHA-256 очищенного текста; при изменении шлёт уведомление в Telegram
- Состояние храним в data/last_hash.json (коммитит workflow)
"""

from __future__ import annotations
import json
import os
import re
import time
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

import requests
import cloudscraper
from bs4 import BeautifulSoup

# ─────────────────────────── Paths & constants ───────────────────────────

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

URLS_FILE = DATA_DIR / "urls.json"
STATE_FILE = DATA_DIR / "last_hash.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# максимально «похожий на браузер» набор заголовков
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

OZON_HOST_HINTS = ("docs.ozon.ru", "seller.ozon.ru")

# ─────────────────────────── utils ───────────────────────────

def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default

def save_json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def clean_text(html: str) -> str:
    """Очищаем HTML от скриптов/стилей; получаем стабильный текст."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    # нормализуем множественные переводы строки
    text = re.sub(r"\n{2,}", "\n", text)
    return text

def fetch_page(url: str, selector: Optional[str] = None, retries: int = 3, sleep_base: float = 1.0) -> str:
    """
    Скачиваем страницу. Для доменов Ozon используем cloudscraper,
    иначе обычный requests. Делаем несколько попыток.
    """
    use_cloudscraper = any(h in url for h in OZON_HOST_HINTS)
    session = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    ) if use_cloudscraper else requests

    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = session.get(url, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            html = resp.text
            if selector:
                node = BeautifulSoup(html, "lxml").select_one(selector)
                html = str(node) if node else html
            return clean_text(html)
        except Exception as e:
            last_err = e
            # небольшая экспоненциальная пауза
            time.sleep(sleep_base + attempt)
    # если все попытки не удались — пробрасываем последнюю ошибку
    raise last_err  # type: ignore[misc]

def tg_send(message: str) -> None:
    """Шлём сообщение в Telegram, если заданы токен/чат."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("No TELEGRAM_* envs; skip Telegram send")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=30)
    # если ошибка — пусть воркфлоу упадёт, чтобы мы увидели её в логах
    r.raise_for_status()

# ─────────────────────────── main ───────────────────────────

def main() -> None:
    urls: List[Dict[str, Any]] = load_json(URLS_FILE, [])
    state: Dict[str, str] = load_json(STATE_FILE, {})
    if not urls:
        print("No URLs configured in data/urls.json")
        return

    changes: List[str] = []
    for item in urls:
        name = item.get("name", "Unnamed")
        url = item["url"]
        selector = item.get("selector")

        try:
            content = fetch_page(url, selector)
            digest = sha256(content)
        except Exception as e:
            changes.append(f"⚠️ <b>{name}</b>: ошибка: {e.__class__.__name__}: {e}")
            continue

        prev_digest = state.get(url)
        if prev_digest != digest:
            changes.append(
                f"🔄 <b>Изменения</b> — {name}\n"
                f"{url}\n"
                f"<i>{(prev_digest or '—')} → {digest[:10]}…</i>"
            )
            state[url] = digest

    # сохраняем состояние даже если нет изменений — для «первого запуска»
    save_json(STATE_FILE, state)

    # отправка отчёта
    if changes:
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        header = f"🧭 Обновления комиссий/логистики\n<code>{ts}</code>\n"
        msg = header + "\n\n".join(changes)
        # Telegram ограничивает размер сообщения — разобьём на куски
        for i in range(0, len(msg), 4000):
            tg_send(msg[i:i + 4000])
        print("Sent changes")
    else:
        print("No changes")

if __name__ == "__main__":
    main()
