#!/usr/bin/env python3
"""
Бот-поисковик заказов на фриланс-биржах.

Что делает за один запуск:
  1. Читает публичные ленты (RSS, открытые Telegram-каналы, страницы со списком заказов).
  2. Отбирает заказы под твои навыки (config.yaml), считает, насколько подходит и какую цену предложить.
  3. Сохраняет ленту в docs/orders.json — её читает приложение «Ilona» (раздел «Работа»).
  4. Пишет тебе в Telegram о самых подходящих новых заказах: ссылка + цена.

Запуск:
  python bot.py            — один проход (так его запускает GitHub Actions)
  python bot.py --loop 20  — крутиться у себя на компьютере, проверяя каждые 20 минут
  python bot.py --dry      — ничего не сохранять и не отправлять, только показать найденное

Секреты (переменные окружения):
  TELEGRAM_TOKEN — токен бота; кому писать — config.yaml → notify.users (TELEGRAM_CHAT_ID — необязательно, старый способ)
  ANTHROPIC_API_KEY                — умная оценка заказов через Claude (необязательно)
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib import robotparser

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import requests

import notify as notifier
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "seen.json"
UA = "PlanOrdersBot/1.0 (+personal use; reads public feeds)"
TIMEOUT = 20
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- загрузка источников
_robots = {}


def allowed(url: str) -> bool:
    """Уважаем robots.txt для страниц (RSS-ленты сделаны как раз для программ)."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            r = SESSION.get(base + "/robots.txt", timeout=TIMEOUT)
            rp.parse(r.text.splitlines() if r.ok else [])
        except requests.RequestException:
            rp.parse([])
        _robots[base] = rp
    return _robots[base].can_fetch(UA, url)


def clean(text: str) -> str:
    text = BeautifulSoup(text or "", "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _date(txt):
    if not txt:
        return datetime.now(timezone.utc)
    try:
        d = parsedate_to_datetime(txt)          # RSS: Tue, 03 Nov 2026 10:00:00 +0300
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(txt.replace("Z", "+00:00"))  # Atom: 2026-11-03T10:00:00Z
        except ValueError:
            return datetime.now(timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(data: bytes):
    """RSS 2.0 и Atom без внешних библиотек; кодировку (в т.ч. windows-1251) берёт из XML-заголовка."""
    root = ET.fromstring(data)
    strip = lambda tag: tag.split("}")[-1]
    items = []
    for el in root.iter():
        if strip(el.tag) not in ("item", "entry"):
            continue
        f = {strip(c.tag): c for c in el}
        link = f.get("link")
        href = (link.text or link.get("href", "")).strip() if link is not None else ""
        desc = f.get("description") if f.get("description") is not None else (f.get("summary") if f.get("summary") is not None else f.get("content"))
        date = f.get("pubDate") if f.get("pubDate") is not None else (f.get("published") if f.get("published") is not None else f.get("updated"))
        items.append({
            "title": (f["title"].text or "") if "title" in f else "",
            "link": href,
            "summary": (desc.text or "") if desc is not None else "",
            "date": _date(date.text if date is not None else ""),
        })
    return items


def fetch_rss(src):
    urls = src.get("urls") or ([src["url"]] if src.get("url") else [])
    out = []
    for url in urls:
        r = SESSION.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        for e in parse_feed(r.content):
            out.append({
                "title": clean(e["title"]),
                "url": e["link"],
                "desc": clean(e["summary"])[:1500],
                "date": e["date"],
                "source": src.get("name", "RSS"),
            })
    return out


def fetch_telegram(src):
    out = []
    for ch in src.get("channels") or []:
        ch = ch.strip().lstrip("@").replace("https://t.me/", "")
        url = f"https://t.me/s/{ch}"
        if not allowed(url):
            log(f"  t.me/{ch}: запрещено robots.txt — пропускаю")
            continue
        r = SESSION.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for msg in soup.select(".tgme_widget_message_wrap"):
            body = msg.select_one(".tgme_widget_message_text")
            if not body:
                continue
            text = body.get_text("\n").strip()
            link_el = msg.select_one("a.tgme_widget_message_date")
            time_el = msg.select_one("time[datetime]")
            # если в посте есть ссылка на биржу — ведём сразу туда
            ext = next((a["href"] for a in body.select("a[href]") if "t.me" not in a["href"]), None)
            try:
                dt = datetime.fromisoformat(time_el["datetime"]) if time_el else datetime.now(timezone.utc)
            except ValueError:
                dt = datetime.now(timezone.utc)
            out.append({
                "title": clean(text.split("\n")[0])[:160],
                "url": ext or (link_el["href"] if link_el else url),
                "desc": clean(text)[:1500],
                "date": dt,
                "source": f"Telegram · {ch}",
            })
    return out


def fetch_html(src):
    url = src["url"]
    if not allowed(url):
        log(f"  {src['name']}: robots.txt запрещает автоматический доступ — пропускаю")
        return []
    r = SESSION.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for it in soup.select(src["item"]):
        t = it.select_one(src["title"])
        if not t:
            continue
        l = it.select_one(src.get("link", src["title"]))
        d = it.select_one(src["desc"]) if src.get("desc") else None
        b = it.select_one(src["budget"]) if src.get("budget") else None
        out.append({
            "title": clean(t.get_text()),
            "url": urljoin(url, l.get("href", "")) if l else url,
            "desc": clean(d.get_text() if d else "")[:1500],
            "budget_text": clean(b.get_text()) if b else "",
            "date": datetime.now(timezone.utc),
            "source": src["name"],
        })
    return out


FETCHERS = {"rss": fetch_rss, "telegram": fetch_telegram, "html": fetch_html}


# ---------------------------------------------------------------- оценка
MONEY_RE = re.compile(r"(\d[\d\s\u00a0]{2,8})\s*(?:₽|руб|р\.|rub)|(\d{1,3}(?:[.,]\d)?)\s*(?:тыс|k\b|к\b)", re.I)


num = lambda n: f"{n:,}".replace(",", " ")


def parse_budget(text: str):
    """Максимальная сумма, похожая на бюджет, в рублях."""
    best = None
    for m in MONEY_RE.finditer(text or ""):
        if m.group(1):
            n = int(re.sub(r"\D", "", m.group(1)) or 0)
        else:
            n = int(float(m.group(2).replace(",", ".")) * 1000)
        if 300 <= n <= 5_000_000:
            best = max(best or 0, n)
    return best


def score(order, cfg):
    prof = cfg["profile"]
    text = f"{order['title']} {order['desc']}".lower()
    if any(w in text for w in cfg.get("exclude", [])):
        return None
    best, hits_best = None, []
    for sk in cfg["skills"]:
        hits = sorted({k for k in sk["keywords"] if k in text})
        if len(hits) > len(hits_best):
            best, hits_best = sk, hits
    if not best:
        return None
    in_title = sum(1 for k in hits_best if k in order["title"].lower())
    fit = min(97, 45 + 12 * len(hits_best) + 10 * in_title)
    budget = parse_budget(order.get("budget_text", "") + " " + order["title"] + " " + order["desc"])
    hours = best["hours"]
    by_rate = max(prof["min_price"], round(hours * prof["rate"] / 500) * 500)
    if budget:
        price = max(prof["min_price"], round(budget * 0.95 / 500) * 500)
        if budget < prof["min_price"]:
            fit -= 25
        why = f"{best['name']}: совпало «{', '.join(hits_best[:4])}». Бюджет клиента ~{num(budget)} ₽"
    else:
        price = by_rate
        why = f"{best['name']}: совпало «{', '.join(hits_best[:4])}». Бюджет не указан — цена по ставке, ~{hours} ч"
    return {
        "fit": max(0, fit),
        "price": int(price),
        "hours": hours,
        "budget": f"{num(budget)} ₽" if budget else "",
        "why": why,
    }


def claude_refine(orders, cfg):
    key = os.environ.get("ANTHROPIC_API_KEY")
    c = cfg.get("claude", {})
    if not key or not c.get("enabled") or not orders:
        return
    prof = cfg["profile"]
    skills = "; ".join(s["name"] for s in cfg["skills"])
    for o in orders[: c.get("max_per_run", 15)]:
        prompt = (
            "Ты помогаешь фрилансеру выбирать заказы. Его навыки: " + skills +
            f". Ставка {prof['rate']} ₽/ч, минимальный заказ {prof['min_price']} ₽.\n"
            f"Заказ: {o['title']}\nОписание: {o['desc'][:1200]}\nБюджет клиента: {o.get('budget') or 'не указан'}\n\n"
            'Ответь ТОЛЬКО JSON: {"fit": 0-100, "price": рубли, "hours": число, "why": "одно предложение по-русски"}'
        )
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": c.get("model", "claude-haiku-5-5"), "max_tokens": 200,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=40,
            )
            r.raise_for_status()
            txt = "".join(b.get("text", "") for b in r.json().get("content", []))
            data = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
            o["fit"] = int(max(0, min(100, data.get("fit", o["fit"]))))
            o["price"] = int(max(prof["min_price"], data.get("price", o["price"])))
            o["hours"] = data.get("hours", o["hours"])
            o["why"] = str(data.get("why", o["why"]))[:300] + " · оценка Claude"
        except Exception as e:  # оценка по словам остаётся
            log(f"  Claude: не удалось оценить «{o['title'][:40]}»: {e}")


# ---------------------------------------------------------------- Telegram
def notify(orders):
    """Шлёт новые заказы в Telegram, если уведомления о заказах включены (/orders_on, /orders_off)."""
    if not orders:
        return
    for o in orders[:10]:
        text = (f"🆕 <b>{html.escape(o['title'][:150])}</b>\n"
                f"💰 Предложить: <b>{num(o['price'])} ₽</b>" +
                (f" (бюджет: {html.escape(o['budget'])})" if o.get("budget") else "") +
                f"\n🎯 Подходит: {o['fit']}% · {html.escape(o['source'])}\n"
                f"💡 {html.escape(o['why'][:200])}\n🔗 {o['url']}")
        if not notifier.broadcast("orders", text, SESSION):
            log("  Telegram: никому не отправлено (никто не подключён или заказы выключены)")


# ---------------------------------------------------------------- основной проход
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def run(cfg, dry=False, fetchers=FETCHERS):
    if not dry:
        notifier.process_commands(SESSION)  # команды /orders_off и т.п.
    prof = cfg["profile"]
    seen = set(load_json(STATE_FILE, []))
    feed_path = ROOT / cfg["output"]["orders_file"]
    feed = load_json(feed_path, {"orders": []})
    known = {o["id"] for o in feed.get("orders", [])}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=prof["max_age_hours"])
    report, fresh = [], []

    for src in cfg["sources"]:
        if not src.get("enabled", True):
            continue
        fn = fetchers.get(src["type"])
        try:
            items = fn(src)
            report.append(f"{src.get('name')} ({src['id']}): {len(items)}")
        except Exception as e:
            report.append(f"{src.get('name')} ({src['id']}): ошибка — {e.__class__.__name__}: {str(e)[:80]}")
            continue
        for it in items:
            if not it["title"] or not it["url"]:
                continue
            oid = hashlib.sha1(it["url"].encode()).hexdigest()[:12]
            if oid in seen or oid in known:
                continue
            if it["date"].tzinfo is None:
                it["date"] = it["date"].replace(tzinfo=timezone.utc)
            if it["date"] < cutoff:
                seen.add(oid)
                continue
            sc = score(it, cfg)
            seen.add(oid)
            if not sc or sc["fit"] < prof["min_fit"]:
                continue
            fresh.append({"id": oid, "title": it["title"], "source": it["source"], "url": it["url"],
                          "date": it["date"].isoformat(), **sc})

    fresh.sort(key=lambda o: o["fit"], reverse=True)
    claude_refine(fresh, cfg)
    fresh = [o for o in fresh if o["fit"] >= prof["min_fit"]]
    fresh.sort(key=lambda o: o["fit"], reverse=True)

    log("Источники:", " | ".join(report))
    log(f"Новых подходящих заказов: {len(fresh)}")
    for o in fresh[:15]:
        log(f"  {o['fit']:>3}% {o['price']:>7} ₽  {o['title'][:70]}  [{o['source']}]")
    if dry:
        return fresh

    orders = (fresh + feed.get("orders", []))[: cfg["output"]["keep"]]
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    feed_path.write_text(json.dumps({"updated": datetime.now(timezone.utc).isoformat(), "orders": orders},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(seen)[-20000:]), encoding="utf-8")
    notify([o for o in fresh if o["fit"] >= prof["notify_fit"]])
    return fresh


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="повторять каждые N минут")
    ap.add_argument("--dry", action="store_true", help="не сохранять и не отправлять")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    a = ap.parse_args()
    while True:
        cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
        try:
            run(cfg, dry=a.dry)
        except Exception as e:
            log("Сбой прохода:", e)
            if not a.loop:
                sys.exit(1)
        if not a.loop:
            break
        time.sleep(a.loop * 60)


if __name__ == "__main__":
    main()
