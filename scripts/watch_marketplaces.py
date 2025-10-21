# -*- coding: utf-8 -*-
"""
Мониторинг изменений комиссий/логистики для РФ (Ozon RU, Я.Маркет RU, WB RU).
Особенности:
- Для доменов Ozon сначала пробуем cloudscraper;
- Если получаем 403/5xx — автоматически используем запасной текстовый ридер r.jina.ai;
- Храним предыдущую версию текста по каждому URL (data/state/<slug>.txt);
- В Telegram уходит "живой" дайджест: короткий diff (добавлено/удалено).
"""

from __future__ import annotations
import json, os, re, time, hashlib, difflib, unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

import requests
import cloudscraper
from bs4 import BeautifulSoup

# ─────────────────────────── Paths & constants ───────────────────────────

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
STATE_DIR = DATA_DIR / "state"
DATA_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)

URLS_FILE = DATA_DIR / "urls.json"
HASHES_FILE = DATA_DIR / "last_hash.json"   # для совместимости/коммита

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/127.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://seller.ozon.ru/",
}
OZON_HOSTS = ("seller.ozon.ru", "seller-edu.ozon.ru")  # RU-источники

# ─────────────────────────── helpers ───────────────────────────

def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "item"

def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default

def save_json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    # типовые «хедеры/футеры»
    for sel in ["header", "footer", "nav", ".header", ".footer", ".menu", ".breadcrumbs"]:
        for node in soup.select(sel):
            node.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)
    return text

def fetch_via_jina(url: str) -> str:
    """
    Запасной путь: текстовый ридер r.jina.ai (отдаёт уже «раскрытый» текст страницы).
    Пробуем и http, и https префиксы.
    """
    bases = [f"https://r.jina.ai/http://{url}", f"https://r.jina.ai/{url}"]
    # Если url уже начинается с http/https — не удваиваем
    if url.startswith("http://") or url.startswith("https://"):
        bases = [f"https://r.jina.ai/{url}"]
    last_err = None
    for u in bases:
        try:
            r = requests.get(u, headers=HEADERS, timeout=60)
            r.raise_for_status()
            # r.jina.ai уже отдаёт текст; слегка нормализуем
            txt = r.text
            txt = re.sub(r"\n{2,}", "\n", txt).strip()
            if len(txt) > 0:
                return txt
        except Exception as e:
            last_err = e
            time.sleep(1)
    if last_err:
        raise last_err
    return ""  # на всякий случай

def fetch_page(url: str, selector: Optional[str] = None, retries: int = 3) -> str:
    """
    Скачиваем страницу.
    1) Для Ozon-доменов — cloudscraper (чтобы пройти 403/JS);
    2) Если получили 403/5xx — пробуем r.jina.ai.
    3) Для остальных — обычный requests, при ошибке тоже fallback на r.jina.ai.
    """
    use_cloud = any(h in url for h in OZON_HOSTS)
    session = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    ) if use_cloud else requests

    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
            html = r.text
            if selector:
                node = BeautifulSoup(html, "lxml").select_one(selector)
                html = str(node) if node else html
            return clean_text(html)
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            last_err = e
            # 403/5xx — сразу уходим на fallback
            if status in (401, 403, 429, 500, 502, 503, 504):
                try:
                    return fetch_via_jina(url)
                except Exception as e2:
                    last_err = e2
            time.sleep(1 + attempt)
        except Exception as e:
            last_err = e
            # общий fallback
            try:
                return fetch_via_jina(url)
            except Exception as e2:
                last_err = e2
            time.sleep(1 + attempt)
    raise last_err  # type: ignore[misc]

def tg_send(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("No TELEGRAM_* envs; skip Telegram send")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()

def diff_preview(old: str, new: str, max_lines: int = 12, max_line_len: int = 200) -> str:
    """
    Компактный diff-превью:
    - только изменённые строки,
    - ➕ добавления / ➖ удаления,
    - длинные строки обрезаем.
    """
    out: List[str] = []
    d = difflib.ndiff(old.splitlines(), new.splitlines())
    for line in d:
        if line.startswith("+ ") or line.startswith("- "):
            mark = "➕" if line.startswith("+ ") else "➖"
            txt = line[2:].strip()
            if len(txt) > max_line_len:
                txt = txt[:max_line_len - 1] + "…"
            out.append(f"{mark} {txt}")
        if len(out) >= max_lines:
            break
    if not out:
        return "— Изменения есть, но не попали в короткое превью (незначительные правки форматирования)."
    return "\n".join(out)

# ─────────────────────────── main ───────────────────────────

def main() -> None:
    urls: List[Dict[str, Any]] = load_json(URLS_FILE, [])
    hashes: Dict[str, str] = load_json(HASHES_FILE, {})

    if not urls:
        print("No URLs configured in data/urls.json")
        return

    changes_blocks: List[str] = []

    for item in urls:
        name = item.get("name", "Unnamed")
        url = item["url"]
        selector = item.get("selector")
        key = slugify(name)  # имя файла состояния

        state_file = STATE_DIR / f"{key}.txt"

        try:
            content = fetch_page(url, selector)
            digest = sha256(content)
        except Exception as e:
            changes_blocks.append(f"⚠️ <b>{name}</b>\n<i>Ошибка запроса:</i> {e.__class__.__name__}: {e}")
            continue

        prev_content = state_file.read_text(encoding="utf-8") if state_file.exists() else ""
        prev_digest = hashes.get(url)

        if prev_digest != digest:
            # сохраним новый контент
            state_file.write_text(content, encoding="utf-8")
            hashes[url] = digest

            # сформируем превью diff
            preview = diff_preview(prev_content, content, max_lines=12, max_line_len=200)
            block = (
                f"🔄 <b>{name}</b>\n"
                f"{preview}\n"
                f"<i>Источник:</i> {url}"
            )
            changes_blocks.append(block)

    # сохраняем хэши (для коммита и будущих сравнений)
    save_json(HASHES_FILE, hashes)

    # отправляем отчёт
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    header = f"🧭 Обновления комиссий/логистики (RU)\n<code>{ts}</code>\n"

    if changes_blocks:
        full = header + "\n\n".join(changes_blocks)
        for i in range(0, len(full), 4000):
            tg_send(full[i:i+4000])
        print("Sent changes")
    else:
        tg_send(header + "Изменений нет.")
        print("No changes")

if __name__ == "__main__":
    main()
