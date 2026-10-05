"""
Бот для мониторинга тендеров на zakupki.gov.kg.
Заходит на список объявлений, отбирает нужные по типу и сумме,
и присылает НОВЫЕ в Telegram.
"""
import html
import json
import os
import re
import sys

import requests
from bs4 import BeautifulSoup

# ==================== НАСТРОЙКИ (меняйте только здесь) ====================

MIN_SUM = 50_000          # минимальная сумма, сом
MAX_SUM = 1_000_000       # максимальная сумма, сом

# Ключевые слова. Пустой список = присылать ВСЕ товарные тендеры в диапазоне.
# Пример: ["мебель", "канцеляр", "компьютер", "принтер"]
# Пишите основу слова: "канцеляр" найдёт и "канцелярия", и "канцелярские".
KEYWORDS = []

# Слова-исключения: если есть в названии, тендер пропускается.
# Пример: ["продукты питания"]
EXCLUDE_WORDS = []

# Присылать ли подходящие тендеры при самом первом запуске (для проверки).
SEND_ON_FIRST_RUN = True

# ==========================================================================

LIST_URL = "https://zakupki.gov.kg/popp/view/order/list.xhtml"
VIEW_URL = "https://zakupki.gov.kg/popp/view/order/view.xhtml?id={}"
SEEN_FILE = "seen.json"
MAX_SEEN = 3000

# Подписи на сайте (английская и русская версии)
RE_TYPE = r"(?:Type of procurement|Вид закупки)"
RE_NAME = r"(?:purchase Name|Наименование закупки)"
RE_COMPANY = r"(?:Name of company|Наименование организации|Закупающая организация)"
RE_AMOUNT = r"(?:Planned amount|Планируемая сумма|Запланированная сумма)"
RE_DEADLINE = r"(?:Bids Submission Deadline|Срок подачи[^\d]*)"
NUMBER = r"\d{1,3}(?:[ ,\u00a0]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"


def to_number(s):
    s = s.replace("\u00a0", "").replace(" ", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_row(tid, text):
    t = {"id": tid, "raw": text}

    # Тип закупки
    m = re.search(RE_TYPE + r"\s*([^\s]+)", text, re.I)
    if m:
        kind = m.group(1).lower()
    else:
        kind = text.lower()
    t["is_goods"] = any(w in kind for w in ("goods", "товар", "тауар"))

    # Название
    m = re.search(
        RE_NAME + r"\s*(.*?)\s*(?:view\.xhtml|\?\?\?openInNewTab\?\?\?|procurement method|Способ закупки|"
        + RE_AMOUNT + r"|$)",
        text, re.I)
    t["name"] = m.group(1).strip() if m and m.group(1).strip() else text[:200]

    # Организация
    m = re.search(RE_COMPANY + r"\s*(.*?)\s*" + RE_TYPE, text, re.I)
    t["company"] = m.group(1).strip() if m else ""

    # Сумма
    m = re.search(RE_AMOUNT + r"\s*(" + NUMBER + ")", text, re.I)
    if not m:
        m = re.search(r"(\d{1,3}(?:[ ,\u00a0]\d{3})+(?:\.\d+)?)", text)
    t["amount"] = to_number(m.group(1)) if m else None

    # Срок подачи
    m = re.search(RE_DEADLINE + r"\s*(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})", text, re.I)
    t["deadline"] = m.group(1) if m else ""

    return t


def parse(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    found = {}
    for a in soup.select('a[href*="view.xhtml?id="]'):
        m = re.search(r"id=(\d+)", a["href"])
        if not m:
            continue
        tid = m.group(1)
        row = a.find_parent("tr")
        if row is None or tid in found:
            continue
        text = " ".join(row.get_text(" ", strip=True).split())
        found[tid] = parse_row(tid, text)
    return list(found.values())


def matches(t):
    if not t["is_goods"]:
        return False
    if t["amount"] is None or not (MIN_SUM <= t["amount"] <= MAX_SUM):
        return False
    name = t["name"].lower()
    if KEYWORDS and not any(k.lower() in name for k in KEYWORDS):
        return False
    if any(w.lower() in name for w in EXCLUDE_WORDS):
        return False
    return True


def format_message(t):
    lines = ["🆕 <b>" + html.escape(t["name"]) + "</b>"]
    if t["company"]:
        lines.append("🏢 " + html.escape(t["company"]))
    lines.append("💰 {:,.0f} сом".format(t["amount"]).replace(",", " "))
    if t["deadline"]:
        lines.append("⏰ Подача до: " + t["deadline"])
    lines.append("🔗 " + VIEW_URL.format(t["id"]))
    return "\n".join(lines)


def send(token, chat_id, text):
    r = requests.post(
        "https://api.telegram.org/bot{}/sendMessage".format(token),
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
        timeout=30,
    )
    r.raise_for_status()


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return None
    with open(SEEN_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_seen(ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(ids[-MAX_SEEN:], f)


def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("Не заданы TELEGRAM_TOKEN и TELEGRAM_CHAT_ID")

    resp = requests.get(
        LIST_URL,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        timeout=60,
    )
    resp.raise_for_status()
    tenders = parse(resp.text)
    if not tenders:
        sys.exit("На странице не найдено тендеров: возможно, сайт изменил вёрстку")

    seen = load_seen()
    first_run = seen is None
    seen = seen or []
    seen_set = set(seen)

    new = [t for t in tenders if t["id"] not in seen_set]
    to_send = [t for t in new if matches(t)]
    print("На странице: {}, новых: {}, подходит: {}".format(
        len(tenders), len(new), len(to_send)))

    if first_run:
        send(token, chat_id, "✅ Бот запущен и следит за новыми тендерами.")
        if not SEND_ON_FIRST_RUN:
            to_send = []

    # самые старые отправляем первыми
    for t in reversed(to_send):
        send(token, chat_id, format_message(t))

    seen.extend(t["id"] for t in reversed(new))
    save_seen(seen)


if __name__ == "__main__":
    main()
