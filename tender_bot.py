"""
Бот для мониторинга тендеров на zakupki.gov.kg.
Заходит на список объявлений, отбирает нужные по типу и сумме,
открывает страницу каждого тендера и присылает НОВЫЕ в Telegram.
"""

import html
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# ==================== НАСТРОЙКИ (меняйте только здесь) ====================

MIN_SUM = 50_000      # минимальная сумма, сом

# Ключевые слова. Пустой список = присылать ВСЕ товарные тендеры в диапазоне.
# Пример: ["мебель", "канцеляр", "компьютер", "принтер"]
# Пишите основу слова: "канцеляр" найдёт и "канцелярия", и "канцелярские".
KEYWORDS = []

# Слова-исключения: если есть в названии, тендер пропускается.
# Пример: ["продукты питания"]
EXCLUDE_WORDS = []

# Присылать ли подходящие тендеры при самом первом запуске (для проверки).
SEND_ON_FIRST_RUN = True

# --- Расчёт цены и баллы (работает, когда вы отвечаете на карточку ценой закупки) ---
TAX_PERCENT = 2          # налог, % от цены закупки
LOGISTICS_DEFAULT = 500  # логистика по умолчанию, сом (на весь заказ)
# Логистика по регионам: ищем слово в адресе поставки и в названии организации.
# Пример: {"Бишкек": 500, "Иссык-Куль": 1500, "Ош": 2500}
LOGISTICS_BY_REGION = {}
TARGET_MARKUP = 20       # желаемая наценка, % от себестоимости
# На сколько % цена победы обычно ниже плановой суммы. Цифра пока примерная,
# поставьте свою. 0 = считать маржу от плановой суммы.
WIN_DISCOUNT = 9
MARKUP_PER_POINT = 4     # сколько % наценки = 1 балл маржи (10 баллов = 40%)

# ==========================================================================

LIST_URL = "https://zakupki.gov.kg/popp/view/order/list.xhtml"
VIEW_URL = "https://zakupki.gov.kg/popp/view/order/view.xhtml?id={}"
SEEN_FILE = "seen.json"
MAX_SEEN = 3000
MAX_CARDS = 300
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
BISHKEK = timezone(timedelta(hours=6))

# Подписи на сайте (английская и русская версии)
RE_TYPE = r"(?:Type of procurement|Вид закупки)"
RE_NAME = r"(?:purchase Name|Наименование закупки)"
RE_COMPANY = r"(?:Name of company|Наименование организации|Закупающая организация)"
RE_AMOUNT = r"(?:Planned amount|Планируемая сумма|Запланированная сумма)"
RE_DEADLINE = r"(?:Bids Submission Deadline|Срок подачи[^\d]*)"

NUMBER = r"\d{1,3}(?:[ ,\u00a0]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
}


def to_number(s):
    s = s.replace("\u00a0", "").replace(" ", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(s):
    """Понимает '09.10.2026 14:23' и '09 October 2026 14:23'."""
    if not s:
        return None
    s = s.strip()
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if m:
        d, mo, y, h, mi = m.groups()
    else:
        m = re.search(r"(\d{1,2})\s+([A-Za-zА-Яа-я]+)\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", s)
        if not m:
            return None
        d, mon, y, h, mi = m.groups()
        mo = MONTHS.get(mon.lower())
        if not mo:
            return None
    try:
        return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0),
                        tzinfo=BISHKEK)
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


# ---------- страница самого тендера ----------

def after_label(text, label_regex):
    """Берёт значение после подписи: на той же строке или на следующей."""
    m = re.search(label_regex + r"[ \t]*\n?[ \t]*([^\n]+)", text, re.I)
    if not m:
        return ""
    value = m.group(1).strip()
    if value.startswith("???") or not value:
        return ""
    return value


def clean_delivery_term(s):
    """'в течении 3-х дней со дня подписания контракта' -> 'в течении 3-х дней'.
    Если после обрезки ничего не осталось ('после заключения контракта'),
    оставляем текст из тендера как есть."""
    cleaned = re.sub(
        r"\s*(?:со дня|с дня|с момента|с даты|после|от даты|от дня)\b.*$",
        "", s, flags=re.I).strip(" ,.;")
    return cleaned or s.strip(" ,.;")


def parse_gopp(text):
    """Возвращает (есть_гопп: True/False/None, значение)."""
    m = re.search(
        r"(?:Guarantee provision of the tender bid|"
        r"(?:Гарантийное\s+)?обеспечени\w+\s+(?:конкурсной\s+)?заявки)",
        text, re.I)
    if not m:
        return None, ""
    window = text[m.end(): m.end() + 300]
    # обрезаем, когда начинается другой блок
    window = re.split(r"\?\?\?bidSecurityValidity|Official information|Официальное",
                      window, maxsplit=1)[0]
    v = re.search(r"\((?:GOKZ|ГОКЗ|ГОПП)\)\s*:?\s*(\d[\d\s.,]*\s*(?:%|сом|som|KGS)?)",
                  window, re.I)
    if v:
        return True, " ".join(v.group(1).split())
    if re.search(r"declaration|деклараци", window, re.I):
        return False, ""
    return None, ""


def parse_requirements(text):
    """Таблица требований к поставщику -> список (квалификация, требование) или None."""
    m = re.search(r"(?:job specifications|Квалификационные требования|Требования к поставщику)",
                  text, re.I)
    if not m:
        return None
    block = text[m.end():]
    end = re.search(r"specificRequirements|A pre-bid meeting|Предквалификационн|"
                    r"Criteria for evaluation|Критерии оценки", block, re.I)
    if end:
        block = block[:end.start()]
    rows, current = [], None
    for line in block.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.fullmatch(r"\d{1,2}", line):
            current = []
            rows.append(current)
        elif current is not None:
            current.append(line)
    result = []
    for cells in rows:
        qual = cells[0] if cells else ""
        req = " ".join(cells[1:])
        result.append((qual, req))
    return result


def fetch_details(tid):
    """Открывает страницу тендера и достаёт нужные поля. Не падает при ошибке."""
    d = {"published": None, "deadline": None, "address": "", "delivery": "",
         "gopp": None, "gopp_value": "", "req_rows": None, "pay_due": ""}
    try:
        r = requests.get(VIEW_URL.format(tid), headers=HEADERS, timeout=60)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text("\n", strip=True)

        d["published"] = parse_date(after_label(
            text, r"(?:Date of publication|Дата публикации)"))
        d["deadline"] = parse_date(after_label(
            text, r"(?:Bids Submission Deadline|Срок подачи заявок?)"))
        d["address"] = after_label(
            text, r"(?:Address and place of delivery|Адрес и место поставки)")
        d["delivery"] = clean_delivery_term(after_label(
            text, r"(?:Terms of delivery of goods|Срок поставки(?: товара| товаров)?)"))
        d["gopp"], d["gopp_value"] = parse_gopp(text)
        d["req_rows"] = parse_requirements(text)
        m = re.search(r"(?:Due date|Срок оплаты)\s*(\d{2}\.\d{2}\.\d{4})", text, re.I)
        d["pay_due"] = m.group(1) if m else ""
    except Exception as e:  # сайт не ответил, вёрстка другая и т.д.
        print("Не удалось прочитать страницу тендера {}: {}".format(tid, e))
    return d


# ---------- фильтр и сообщение ----------

def matches(t):
    if not t["is_goods"]:
        return False
    if t["amount"] is None or t["amount"] < MIN_SUM:
        return False
    name = t["name"].lower()
    if KEYWORDS and not any(k.lower() in name for k in KEYWORDS):
        return False
    if any(w.lower() in name for w in EXCLUDE_WORDS):
        return False
    return True


def plural_days(n):
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def left_text(deadline):
    if not deadline:
        return ""
    delta = deadline - datetime.now(BISHKEK)
    if delta.total_seconds() <= 0:
        return "срок истёк"
    if delta < timedelta(days=1):
        hours = max(1, math.ceil(delta.total_seconds() / 3600))
        return "осталось {} ч".format(hours)
    days = delta.days
    return "осталось {} {}".format(days, plural_days(days))


def format_message(t, d):
    e = html.escape
    dash = "—"
    lines = ["🍥 " + e(t["name"])]
    lines.append("🏢 " + e(t["company"] or dash))
    lines.append("")
    lines.append("📍 " + e(d["address"] or dash))
    lines.append("")
    lines.append("🚛 Срок поставки: " + e(d["delivery"] or dash))
    lines.append("")
    if d["gopp"] is True:
        lines.append("🧾 ГОПП: ✅ " + e(d["gopp_value"]))
    elif d["gopp"] is False:
        lines.append("🧾 ГОПП: ❌")
    else:
        lines.append("🧾 ГОПП: " + dash)
    lines.append("")
    lines.append("💸 <b>{:,.0f} сом</b>".format(t["amount"]).replace(",", " "))
    lines.append("")

    deadline = d["deadline"] or parse_date(t["deadline"])
    start = d["published"].strftime("%d.%m") if d["published"] else dash
    end = deadline.strftime("%d.%m") if deadline else dash
    left = left_text(deadline)
    date_line = "📅 от {} — до {}".format(start, end)
    if left:
        date_line += " ({})".format(left)
    lines.append(date_line)
    lines.append("")
    req = requirements_score(d.get("req_rows"))
    lines.append("📋 Требования: " + ("{}/10".format(req) if req is not None else dash))
    lines.append("")
    lines.append("🔗 " + VIEW_URL.format(t["id"]))
    return "\n".join(lines)


def send(token, chat_id, text, reply_to=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true"}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    r = requests.post(
        "https://api.telegram.org/bot{}/sendMessage".format(token),
        data=data, timeout=30,
    )
    r.raise_for_status()
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return None


# ---------- память бота (всё хранится в seen.json) ----------

def load_state():
    if not os.path.exists(SEEN_FILE):
        return None
    with open(SEEN_FILE, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):  # старый формат: просто список id
        data = {"seen": data}
    data.setdefault("seen", [])
    data.setdefault("cards", {})
    data.setdefault("offset", 0)
    return data


def save_state(state):
    state["seen"] = state["seen"][-MAX_SEEN:]
    cards = state["cards"]
    if len(cards) > MAX_CARDS:
        for k in list(cards.keys())[:len(cards) - MAX_CARDS]:
            del cards[k]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ---------- расчёт цены и баллы ----------

def parse_price(text):
    """'62000', '62 000', '15к', '6200*10', '6200х10' -> число или None."""
    s = (text or "").lower().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    s = re.sub(r"(сом|som|kgs)$", "", s)
    for ch in ("х", "x", "×"):
        s = s.replace(ch, "*")
    if not s:
        return None
    total = 1.0
    for part in s.split("*"):
        m = re.fullmatch(r"(\d+(?:\.\d+)?)(к|k|тыс)?", part)
        if not m:
            return None
        total *= float(m.group(1)) * (1000 if m.group(2) else 1)
    return total


def requirements_score(rows):
    """10 баллов минус за сложные требования. rows = список (квалификация, требование)."""
    if rows is None:
        return None
    score = 10
    other = 0
    for qual, req in rows:
        low = (qual + " " + req).lower()
        if re.search(r"конфликт\w* интерес|аффилирован|страхов\w+ взнос|уплате налог|"
                     r"задолженност", low):
            continue  # стандартные требования есть у всех тендеров
        if "опыт" in low:
            score -= 3
        elif re.search(r"лиценз|сертификат|разрешени|аккредитац", low):
            score -= 2
        else:
            other += 1
    score -= min(other, 3)
    return max(score, 0)


def logistics_for(card):
    place = ((card.get("address") or "") + " " + (card.get("company") or "")).lower()
    for key, cost in LOGISTICS_BY_REGION.items():
        if key.lower() in place:
            return cost
    return LOGISTICS_DEFAULT


def money(x):
    return "{:,.0f}".format(x).replace(",", " ")


def num(x):
    x = round(x, 1)
    return str(int(x)) if x == int(x) else str(x)


def calc_reply(card, purchase):
    amount = card["amount"]
    tax = purchase * TAX_PERCENT / 100
    logistics = logistics_for(card)
    cost = purchase + tax + logistics
    min_price = cost * (1 + TARGET_MARKUP / 100)
    win_price = amount * (1 - WIN_DISCOUNT / 100)
    profit = win_price - cost
    markup = profit / cost * 100 if cost else 0
    margin_score = max(0.0, min(10.0, markup / MARKUP_PER_POINT))

    lines = ["🧮 <b>Расчёт</b>",
             "Закупка: {} сом".format(money(purchase)),
             "Налог {}%: {} сом".format(num(TAX_PERCENT), money(tax)),
             "Логистика: {} сом".format(money(logistics)),
             "Себестоимость: <b>{} сом</b>".format(money(cost)),
             "",
             "🎯 Минимальная цена для ставки (+{}%): {} сом".format(
                 num(TARGET_MARKUP), money(min_price))]
    if WIN_DISCOUNT:
        lines.append("🏆 Ожидаемая цена победы: ~{} сом (плановая −{}%)".format(
            money(win_price), num(WIN_DISCOUNT)))
    else:
        lines.append("🏆 Расчёт от плановой суммы: {} сом".format(money(win_price)))
    lines.append("")
    lines.append("💰 Прибыль: <b>~{} сом</b> (наценка {}%)".format(
        money(profit), num(markup)))
    lines.append("📈 Маржа: <b>{}/10</b>".format(num(margin_score)))
    req = card.get("req")
    lines.append("📋 Требования: <b>{}</b>".format(
        "{}/10".format(req) if req is not None else "—"))
    if win_price < min_price:
        lines.append("⚠️ Цена победы ниже вашей минимальной цены")

    extra = []
    frozen = frozen_amount(card)
    if frozen:
        extra.append("🔒 Заморозится: {} сом (ГОПП)".format(money(frozen)))
    if card.get("pay_due"):
        extra.append("💳 Оплата до: " + card["pay_due"])
    if extra:
        lines.append("")
        lines.extend(extra)
    return "\n".join(lines)


def frozen_amount(card):
    if card.get("gopp") is not True:
        return None
    v = card.get("gopp_value") or ""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", v)
    if m:
        return card["amount"] * float(m.group(1).replace(",", ".")) / 100
    n = to_number(re.sub(r"[^\d.,]", "", v)) if v else None
    return n


def process_replies(token, chat_id, state):
    """Читает ответы на карточки (цена закупки) и отвечает расчётом."""
    r = requests.get(
        "https://api.telegram.org/bot{}/getUpdates".format(token),
        params={"offset": state["offset"], "timeout": 0,
                "allowed_updates": json.dumps(["message"])},
        timeout=30,
    )
    r.raise_for_status()
    for upd in r.json().get("result", []):
        state["offset"] = max(state["offset"], upd["update_id"] + 1)
        msg = upd.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat_id):
            continue
        reply = msg.get("reply_to_message")
        if not reply:
            continue
        card = state["cards"].get(str(reply.get("message_id")))
        if not card:
            continue
        purchase = parse_price(msg.get("text", ""))
        if purchase is None or purchase <= 0:
            send(token, chat_id,
                 "Не понял цену. Пример: 62000, 15к или 6200*10 (цена × количество).",
                 reply_to=msg["message_id"])
            continue
        send(token, chat_id, calc_reply(card, purchase), reply_to=msg["message_id"])


def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("Не заданы TELEGRAM_TOKEN и TELEGRAM_CHAT_ID")

    state = load_state()
    first_run = state is None
    if first_run:
        state = {"seen": [], "cards": {}, "offset": 0}

    # 1. ответы на карточки (цена закупки) -> расчёт
    if not first_run:
        try:
            process_replies(token, chat_id, state)
            save_state(state)
        except Exception as e:
            print("Не удалось обработать ответы: {}".format(e))

    # 2. новые тендеры
    resp = requests.get(LIST_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    tenders = parse(resp.text)
    if not tenders:
        sys.exit("На странице не найдено тендеров: возможно, сайт изменил вёрстку")

    seen = state["seen"]
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
        details = fetch_details(t["id"])
        msg_id = send(token, chat_id, format_message(t, details))
        if msg_id:
            state["cards"][str(msg_id)] = {
                "amount": t["amount"],
                "company": t["company"],
                "address": details["address"],
                "gopp": details["gopp"],
                "gopp_value": details["gopp_value"],
                "pay_due": details["pay_due"],
                "req": requirements_score(details["req_rows"]),
            }

    seen.extend(t["id"] for t in reversed(new))
    save_state(state)


if __name__ == "__main__":
    main()
