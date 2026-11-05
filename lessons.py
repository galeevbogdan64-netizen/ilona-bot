#!/usr/bin/env python3
"""
Каждый день генерирует 3 новых коротких урока для раздела «Саморазвитие» приложения «Ilona»
через Claude API и складывает их в docs/lessons.json (хранится последние 120).
Уроки не повторяются: в запрос передаются заголовки уже созданных.
Нужен секрет ANTHROPIC_API_KEY. Без него скрипт ничего не делает.
"""
import json, os, re
from datetime import date
from pathlib import Path
import requests

import notify as notifier

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "lessons.json"


def morning_message(new):
    """Утреннее уведомление: напоминание об уроке дня (+ заголовки новых уроков от Claude, если есть)."""
    txt = "🧠 <b>Доброе утро! Урок дня уже ждёт в Ilona</b> → «Саморазвитие»\nА ещё — головоломки и «В этот день»."
    if new:
        txt += "\n\nНовое сегодня:\n" + "\n".join(f"• {notifier.esc(l['t'])}" for l in new)
    print("Отправлено:", notifier.broadcast("lessons", txt))


def main():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY не задан — новые уроки не создаю, только напоминание")
        morning_message([])
        return
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"lessons": []}
    titles = [l["t"] for l in old["lessons"]][-150:]
    prompt = f"""Придумай 3 коротких урока для саморазвития (2 минуты чтения каждый) — проверенные интересные факты.
Темы чередуй: история, наука, психология, мышление, финансы, география, искусство, технологии. Только достоверные факты, без выдумок.
Не повторяй эти темы: {'; '.join(titles) or 'нет'}.
Ответь ТОЛЬКО JSON-массивом: [{{"cat":"История|Наука|Психология|Мышление|Финансы|География|Искусство|Технологии","t":"заголовок","p":["абзац","абзац","абзац"],"key":"главная мысль","q":"вопрос","o":["вариант","вариант","вариант"],"a":0}}]"""
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": os.environ.get("LESSONS_MODEL", "claude-sonnet-5-5"), "max_tokens": 4000,
                            "messages": [{"role": "user", "content": prompt}]}, timeout=180)
    r.raise_for_status()
    txt = "".join(b.get("text", "") for b in r.json()["content"])
    new = json.loads(re.search(r"\[.*\]", txt, re.S).group(0))
    d = date.today().isoformat()
    for i, l in enumerate(new):
        l["id"] = f"bot-{d}-{i}"
    lessons = (old["lessons"] + new)[-120:]
    OUT.write_text(json.dumps({"updated": d, "lessons": lessons}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Новых уроков:", len(new))
    morning_message(new)


if __name__ == "__main__":
    main()
