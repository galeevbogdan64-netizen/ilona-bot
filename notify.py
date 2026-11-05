"""
Уведомления в Telegram для нескольких людей и их включение/выключение.

Кому писать — ники в config.yaml → notify.users (например GaleevBogdan).
Telegram не даёт ботам писать первыми, поэтому каждый человек один раз открывает бота
и нажимает «Start» — бот запоминает его чат. У каждого свои переключатели.

Команды (пишешь своему боту или жмёшь кнопки в приложении Ilona):
  /status            — что включено
  /orders_on  /orders_off    — новые заказы
  /lessons_on /lessons_off   — утреннее напоминание и урок дня
  /ideas_on   /ideas_off     — 20 идей недели по понедельникам
  /all_on     /all_off       — всё сразу
Бот проверяет команды при каждом запуске (раз в ~20 минут), поэтому ответ приходит не мгновенно.
"""
import html
import json
import os
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
PREFS_FILE = ROOT / "state" / "notify.json"
CONFIG_FILE = ROOT / "config.yaml"
KINDS = {"orders": "Новые заказы", "lessons": "Напоминание и урок дня", "ideas": "Идеи недели"}
TIMEOUT = 20


def _token():
    return os.environ.get("TELEGRAM_TOKEN")


def allowed_users():
    """Ники из config.yaml (без @, в нижнем регистре)."""
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        cfg = {}
    users = (cfg.get("notify") or {}).get("users") or []
    return {str(u).strip().lstrip("@").lower() for u in users if u}


def load_prefs():
    try:
        p = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        p = {}
    p.setdefault("offset", 0)
    p.setdefault("users", {})
    # старый формат (один чат из TELEGRAM_CHAT_ID)
    legacy = os.environ.get("TELEGRAM_CHAT_ID")
    if legacy and not any(str(u.get("chat")) == str(legacy) for u in p["users"].values()):
        p["users"]["_owner"] = {"chat": legacy, "on": p.pop("on", None) or {}}
    for u in p["users"].values():
        u["on"] = {k: True for k in KINDS} | (u.get("on") or {})
    return p


def save_prefs(p):
    PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")


def send_to(chat, text, session=None):
    token = _token()
    if not token or not chat:
        return False
    s = session or requests
    try:
        r = s.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                   timeout=TIMEOUT)
        time.sleep(0.4)
        return getattr(r, "ok", True)
    except requests.RequestException:
        return False


def broadcast(kind, text, session=None):
    """Отправляет всем подключённым, у кого включён этот вид уведомлений. Возвращает число получателей."""
    n = 0
    for name, u in load_prefs()["users"].items():
        if (name == "_owner" or name in allowed_users()) and u["on"].get(kind, True):
            n += bool(send_to(u["chat"], text, session))
    return n


def enabled(kind):
    """Хоть у кого-то включено."""
    return any(u["on"].get(kind, True) for u in load_prefs()["users"].values())


def status_text(u):
    return "🔔 <b>Уведомления Ilona</b>\n" + "\n".join(f"{'✅' if u['on'][k] else '⛔'} {n}" for k, n in KINDS.items()) + \
        "\n\nВключить/выключить: /orders_on /orders_off /lessons_on /lessons_off /ideas_on /ideas_off /all_on /all_off"


def process_commands(session=None):
    """Читает новые сообщения боту: подключает людей из списка и применяет команды вкл/выкл."""
    p = load_prefs()
    token = _token()
    if not token:
        return p
    s = session or requests
    try:
        r = s.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"offset": p["offset"], "timeout": 0}, timeout=TIMEOUT)
        updates = r.json().get("result", [])
    except Exception:
        return p
    allowed = allowed_users()
    for upd in updates:
        p["offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat = msg.get("chat", {}).get("id")
        nick = (msg.get("from", {}).get("username") or "").lower()
        if nick in allowed:
            key = nick
        elif "_owner" in p["users"] and str(p["users"]["_owner"]["chat"]) == str(chat):
            key = "_owner"
        else:
            continue  # чужие сообщения игнорируем
        new = key not in p["users"] or str(p["users"][key].get("chat")) != str(chat)
        u = p["users"].setdefault(key, {"on": {k: True for k in KINDS}})
        u["chat"] = chat
        text = (msg.get("text") or "").strip()
        parts = text.split()
        cmd = parts[1] if text.startswith("/start") and len(parts) > 1 else (parts[0].lstrip("/").split("@")[0] if parts else "")
        if "_" in cmd:
            kind, _, val = cmd.rpartition("_")
            if val in ("on", "off") and (kind in KINDS or kind == "all"):
                for k in (KINDS if kind == "all" else [kind]):
                    u["on"][k] = val == "on"
        if new:
            send_to(chat, "✅ <b>Готово!</b> Теперь уведомления Ilona будут приходить сюда.\n\n" + status_text(u), s)
        else:
            send_to(chat, status_text(u), s)
    save_prefs(p)
    return p


def esc(t):
    return html.escape(str(t))
