"""Проверка уведомлений для нескольких людей и кнопок вкл/выкл на поддельном Telegram: python tests/test_notify.py"""
import sys, tempfile, json
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notify, lessons, ideas, bot

CFG = "notify:\n  users:\n    - GaleevBogdan\n    - Girl_Test\n"


class FakeTG:
    """Подменяет api.telegram.org: копит отправленные сообщения (chat, text) и отдаёт входящие."""
    def __init__(self): self.sent, self.inbox, self.uid = [], [], 100
    def user(self, text, chat, nick):
        self.uid += 1; self.inbox.append({"update_id": self.uid, "message": {"chat": {"id": chat}, "from": {"username": nick}, "text": text}})
    def get(self, url, params=None, timeout=None):
        off = (params or {}).get("offset", 0)
        return mock.Mock(json=lambda: {"ok": True, "result": [u for u in self.inbox if u["update_id"] >= off]})
    def post(self, url, json=None, timeout=None):
        self.sent.append((json["chat_id"], json["text"])); return mock.Mock(ok=True)
    def to(self, chat): return [t for c, t in self.sent if c == chat]


def main():
    tg = FakeTG()
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.yaml"; cfg.write_text(CFG, encoding="utf-8")
        with mock.patch.object(notify, "PREFS_FILE", Path(tmp) / "notify.json"), mock.patch.object(notify, "CONFIG_FILE", cfg), \
             mock.patch.object(notify, "requests", tg), mock.patch.object(notify.time, "sleep"), \
             mock.patch.dict("os.environ", {"TELEGRAM_TOKEN": "x", "TELEGRAM_CHAT_ID": "", "ANTHROPIC_API_KEY": ""}):
            # пока никто не нажал Start — писать некуда
            lessons.main(); assert tg.sent == []

            # Богдан нажал «Подключить» в приложении → Start в боте; посторонний тоже пытается
            tg.user("/start hi", 111, "GaleevBogdan"); tg.user("/start", 666, "stranger")
            notify.process_commands(tg)
            assert "Готово" in tg.to(111)[-1] and tg.to(666) == [], tg.sent

            lessons.main(); ideas.main()
            assert any("Урок дня" in t for t in tg.to(111)) and any("20 свежих идей" in t for t in tg.to(111))

            # девушка подключилась (ник в любом регистре) и выключила заказы — только у себя
            tg.user("/start hi", 222, "girl_test"); notify.process_commands(tg)
            tg.user("/start orders_off", 222, "girl_test"); notify.process_commands(tg)
            p = json.loads(notify.PREFS_FILE.read_text())
            assert p["users"]["girl_test"]["on"]["orders"] is False and p["users"]["galeevbogdan"]["on"]["orders"] is True

            order = {"title": "Сделать лендинг", "price": 15000, "budget": "", "fit": 90, "source": "FL.ru", "why": "сайт", "url": "https://x"}
            n1, n2 = len(tg.to(111)), len(tg.to(222))
            with mock.patch.object(bot.SESSION, "post", side_effect=tg.post):
                bot.notify([order])
            assert len(tg.to(111)) == n1 + 1 and "15 000 ₽" in tg.to(111)[-1]
            assert len(tg.to(222)) == n2, "девушке заказы не должны приходить — она их выключила"

            # Богдан выключил уроки → урок приходит только девушке
            tg.user("/lessons_off", 111, "GaleevBogdan"); notify.process_commands(tg)
            n1, n2 = len(tg.to(111)), len(tg.to(222)); lessons.main()
            assert len(tg.to(111)) == n1 and len(tg.to(222)) == n2 + 1

            # повторный запуск не применяет старые команды заново; /all_on всё включает
            tg.user("/all_on", 111, "GaleevBogdan"); notify.process_commands(tg); notify.process_commands(tg)
            assert all(json.loads(notify.PREFS_FILE.read_text())["users"]["galeevbogdan"]["on"].values())
            tg.user("/status", 111, "GaleevBogdan"); notify.process_commands(tg)
            assert tg.to(111)[-1].count("✅") == 3

            # убрали ник из config.yaml → больше не пишем
            cfg.write_text("notify:\n  users: [GaleevBogdan]\n", encoding="utf-8")
            n2 = len(tg.to(222)); lessons.main(); assert len(tg.to(222)) == n2
    print("OK: уведомления по нику (ты + девушка), у каждого свои переключатели, посторонним бот не пишет")


if __name__ == "__main__":
    main()
