"""Офлайн-проверка бота на примерах лент: python tests/test_bot.py"""
import sys, json, tempfile
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yaml, bot

FX = Path(__file__).parent / "fixtures"

# Даты в примерах лент записаны относительно 03.11.2026 11:33 UTC.
# Сдвигаем их к «сейчас», чтобы тест не зависел от дня запуска (бот отбрасывает заказы старше 48 ч).
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime, format_datetime
BASE = datetime(2026, 11, 3, 11, 33, 31, tzinfo=timezone.utc)
SHIFT = datetime.now(timezone.utc) - BASE


def fresh_dates(raw: bytes, enc: str) -> bytes:
    t = raw.decode(enc)
    t = re.sub(r"<pubDate>(.*?)</pubDate>", lambda m: "<pubDate>" + format_datetime(parsedate_to_datetime(m.group(1)) + SHIFT) + "</pubDate>", t)
    t = re.sub(r'datetime="([^"]+)"', lambda m: 'datetime="' + (datetime.fromisoformat(m.group(1)) + SHIFT).isoformat() + '"', t)
    return t.encode(enc)


class Resp:
    def __init__(self, content, text=None): self.content, self.text, self.ok = content, text or content.decode("utf-8", "ignore"), True
    def raise_for_status(self): pass


def fake_get(url, timeout=None):
    if "robots.txt" in url: return Resp(b"")
    if "fl.ru/rss" in url: return Resp(fresh_dates((FX / "fl.xml").read_bytes(), "windows-1251"))
    if "t.me/s/" in url: return Resp(fresh_dates((FX / "tg.html").read_bytes(), "utf-8"))
    raise bot.requests.ConnectionError("нет сети в тесте")


def main():
    cfg = yaml.safe_load((bot.ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["sources"] = [s for s in cfg["sources"] if s["id"] in ("fl-programming", "telegram", "habr")]
    for s in cfg["sources"]:
        if s["type"] == "telegram": s["channels"] = ["test"]
    with tempfile.TemporaryDirectory() as tmp:
        bot.STATE_FILE = Path(tmp) / "seen.json"
        cfg["output"]["orders_file"] = str(Path(tmp) / "orders.json")
        sent = []
        with mock.patch.object(bot.SESSION, "get", side_effect=fake_get), \
             mock.patch.object(bot.SESSION, "post", side_effect=lambda *a, **k: sent.append(k["json"]["text"])), \
             mock.patch.dict("os.environ", {"TELEGRAM_TOKEN": "x", "TELEGRAM_CHAT_ID": "1"}, clear=False):
            fresh = bot.run(cfg)
            again = bot.run(cfg)   # повторный запуск — дублей быть не должно
        titles = [o["title"] for o in fresh]
        assert any("лендинг" in t.lower() for t in titles), titles
        assert any("telegram-бот" in t.lower() for t in titles), titles
        assert any("логотип для барбершопа" in t.lower() for t in titles), titles
        assert not any("курсов" in t.lower() or "1с" in t.lower() or "гараж" in t.lower() for t in titles), titles
        assert not any("кофейни" in t for t in titles), "старый заказ (5 дней) должен отсеяться"
        land = next(o for o in fresh if "лендинг" in o["title"].lower())
        assert land["price"] == 14000 and land["budget"].startswith("15"), land
        tg = next(o for o in fresh if "барбершоп" in o["title"])
        assert tg["url"] == "https://kwork.ru/projects/999", tg["url"]
        assert tg["budget"].startswith("7 000"), tg
        assert again == [], "повторный запуск дал дубли"
        feed = json.loads(Path(cfg["output"]["orders_file"]).read_text(encoding="utf-8"))
        assert len(feed["orders"]) == len(fresh)
        assert sent, "уведомления в Telegram не отправлены"
        print("Найдено:", len(fresh), "| уведомлений:", len(sent))
        print("Пример уведомления:\n" + sent[0])
        print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")


if __name__ == "__main__":
    main()
