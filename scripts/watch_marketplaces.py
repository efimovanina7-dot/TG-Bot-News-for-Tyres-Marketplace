import hashlib, json, os, re
from datetime import datetime
from pathlib import Path
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"; DATA.mkdir(exist_ok=True)
STATE = DATA / "last_hash.json"
CFG = DATA / "urls.json"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
HEADERS = {"User-Agent": "Mozilla/5.0 (MarketplaceWatch/1.0)"}

def jload(p, default): return json.loads(p.read_text("utf-8")) if p.exists() else default
def jsave(p, obj): p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script","style","noscript"]): t.decompose()
    txt = soup.get_text("\n", strip=True)
    return re.sub(r"\n{2,}", "\n", txt)

def fetch(url, selector=None):
    r = requests.get(url, headers=HEADERS, timeout=45); r.raise_for_status()
    if selector:
        s = BeautifulSoup(r.text, "lxml").select_one(selector)
        return clean_text(str(s) if s else r.text)
    return clean_text(r.text)

def sha(s): return hashlib.sha256(s.encode("utf-8")).hexdigest()

def tg_send(msg):
    if not TOKEN or not CHAT_ID: return
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  json={"chat_id": CHAT_ID, "text": msg, "parse_mode":"HTML",
                        "disable_web_page_preview": True}, timeout=30).raise_for_status()

def main():
    urls = jload(CFG, [])
    state = jload(STATE, {})
    changes = []
    for it in urls:
        name, url, sel = it["name"], it["url"], it.get("selector")
        try:
            content = fetch(url, sel)
            digest = sha(content)
        except Exception as e:
            changes.append(f"⚠️ <b>{name}</b>: ошибка: {e.__class__.__name__}: {e}")
            continue
        prev = state.get(url)
        if prev != digest:
            changes.append(f"🔄 <b>Изменения</b> — {name}\n{url}\n<i>{prev or '—'} → {digest[:10]}…</i>")
            state[url] = digest
    jsave(STATE, state)
    if changes:
        dt = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        msg = "🧭 Обновления комиссий/логистики\n<code>%s</code>\n\n%s" % (dt, "\n\n".join(changes))
        for i in range(0, len(msg), 4000): tg_send(msg[i:i+4000])

if __name__ == "__main__": main()
