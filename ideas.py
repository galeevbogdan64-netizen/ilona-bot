#!/usr/bin/env python3
"""
Раз в неделю генерирует 20 новых бизнес-идей (10 физических + 10 с Claude/нейросетями)
через Claude API и кладёт их в docs/ideas.json — приложение «Ilona» подхватит их
в разделе «Бизнес-партнёр». Нужен секрет ANTHROPIC_API_KEY. Без него скрипт ничего не делает.
"""
import json, os, re, sys
from datetime import date
from pathlib import Path
import requests, yaml

import notify as notifier

ROOT = Path(__file__).resolve().parent
SCHEMA = '''{"id": "уникальная-строка", "t": "название", "k": "phys" или "ai",
 "cost": [мин, макс] вложения в рублях, "profit": [мин, макс] чистая прибыль в рублях, "per": "мес" или "сделка",
 "time": "сколько до первых денег", "risk": "низкий|средний|высокий", "reg": "npd" | "ip" | "tender",
 "buy": [["что купить", "где купить (конкретные площадки/магазины в России)", "примерная цена"]],
 "sell": ["где искать клиентов / продавать — конкретные площадки и действия"],
 "tools": ["сервисы и программы"], "plan": ["Неделя 1: ...", "Неделя 2: ...", "Неделя 3–4: ..."],
 "ai": "что конкретно можно поручить Claude", "risks": ["риск и как его снизить"]}'''


def weekly_message(ideas):
    """Понедельничное уведомление о новой подборке идей."""
    txt = "💡 <b>Новая неделя — 20 свежих идей в Ilona</b> → «Партнёр»\nПрошлые исчезли, кроме избранного ⭐"
    if ideas:
        top = sorted(ideas, key=lambda i: -(i.get("profit") or [0, 0])[-1])[:5]
        txt += "\n\nСамые прибыльные:\n" + "\n".join(
            f"• {notifier.esc(i['t'])} — до {int((i.get('profit') or [0, 0])[-1]):,} ₽/{i.get('per', 'мес')}".replace(",", " ") for i in top)
    print("Отправлено:", notifier.broadcast("ideas", txt))


def main():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY не задан — идеи из встроенной библиотеки, шлю только напоминание")
        weekly_message([])
        return
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    out = ROOT / cfg["output"]["ideas_file"]
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    old_titles = [i["t"] for i in old.get("ideas", [])]
    skills = "; ".join(s["name"] for s in cfg["skills"])
    prompt = f"""Ты бизнес-партнёр. Придумай 20 НОВЫХ реалистичных идей заработка в России на {date.today():%B %Y}:
ровно 10 физических (k="phys": производство, услуги, тендеры на zakupki.gov.ru, торговля) и ровно 10 с Claude и нейросетями (k="ai").
Навыки человека: {skills}. Он фрилансер-разработчик, может работать удалённо.
Не повторяй эти идеи: {'; '.join(old_titles[:40]) or 'нет'}.
Цифры — честные вилки, без завышения. Указывай конкретные площадки: где купить (Авито, Ozon, WB, Леруа Мерлен, металлобазы, 1688 через карго и т.п.),
где продавать и искать клиентов, как оформиться (самозанятость в «Мой налог» / ИП через Госуслуги / тендеры 44-ФЗ).
Ответь ТОЛЬКО JSON-массивом из 20 объектов по схеме: {SCHEMA}"""
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": os.environ.get("IDEAS_MODEL", "claude-sonnet-5-5"), "max_tokens": 16000,
                            "messages": [{"role": "user", "content": prompt}]}, timeout=300)
    r.raise_for_status()
    txt = "".join(b.get("text", "") for b in r.json()["content"])
    ideas = json.loads(re.search(r"\[.*\]", txt, re.S).group(0))
    week = date.today().isocalendar()
    for i, it in enumerate(ideas):
        it["id"] = f"w{week[0]}-{week[1]}-{i}"
    out.write_text(json.dumps({"week": f"{week[0]}-W{week[1]:02d}", "ideas": ideas}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Сохранено идей: {len(ideas)}")
    weekly_message(ideas)


if __name__ == "__main__":
    sys.exit(main())
