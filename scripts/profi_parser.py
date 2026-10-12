"""Server-side Profi order parser; no Windows or mouse dependencies."""
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup

PARSER_VERSION = "4.5-server"

def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def get_order_id(url: str, testid: str = "") -> str:
    match = re.search(r"(\d+)_order-snippet", testid or "")
    if match:
        return match.group(1)

    try:
        query = parse_qs(urlparse(url).query)
        values = query.get("o", [])
        if values:
            return str(values[0])
    except Exception:
        pass

    match = re.search(r"[?&]o=(\d+)", url or "")
    return match.group(1) if match else ""


AMOUNT = r"\d(?:[\d\s\u00a0\u202f]*\d)?"
MONEY = re.compile(rf"(?<!\d)(?:(от|до)\s+)?({AMOUNT})(?:\s*[-–—]\s*({AMOUNT})|\s+до\s+({AMOUNT}))?\s*(?:₽|руб(?:\.|лей|ля)?)(?![А-Яа-я])", re.I)


def budget_fields(header_text: str) -> dict:
    match = MONEY.search(clean_text(header_text))
    if not match:
        return {"budget": "", "budget_min_rub": None, "budget_max_rub": None, "budget_kind": "unspecified", "budget_unit": None}
    prefix, start, end1, end2 = match.groups()
    a = int(re.sub(r"\D", "", start))
    end = end1 or end2
    if end:
        b = int(re.sub(r"\D", "", end))
        lo, hi, kind = min(a, b), max(a, b), "range"
    elif (prefix or "").lower() == "до":
        lo, hi, kind = None, a, "upper_bound"
    elif (prefix or "").lower() == "от":
        lo, hi, kind = a, None, "lower_bound"
    else:
        lo, hi, kind = a, a, "exact"
    unit = re.match(r"\s*(?:/|за\s+|в\s+)(час|ч\b|услугу|проект|занятие|месяц|день)\b", clean_text(header_text)[match.end():], re.I)
    units = {"час": "hour", "ч": "hour", "услугу": "service", "проект": "project", "занятие": "lesson", "месяц": "month", "день": "day"}
    return {"budget": clean_text(match.group(0) + (unit.group(0) if unit else "")), "budget_min_rub": lo,
            "budget_max_rub": hi, "budget_kind": kind,
            "budget_unit": units[unit[1].lower()] if unit else None}


def parse_title_and_budget(header_text: str) -> tuple[str, str, int | None]:
    header = clean_text(re.sub(r"\bfalse\b", "", header_text or "", flags=re.I))
    money = MONEY.search(header)
    title = clean_text(header[:money.start()] if money else header).strip(" ·-")
    budget = budget_fields(header)
    return title, budget["budget"], budget["budget_max_rub"]


MONTH_ALIASES = {
    name: month for month, names in enumerate((
        ("янв", "январь", "января"), ("фев", "февр", "февраль", "февраля"),
        ("мар", "март", "марта"), ("апр", "апрель", "апреля"), ("май", "мая"),
        ("июн", "июнь", "июня"), ("июл", "июль", "июля"), ("авг", "август", "августа"),
        ("сен", "сент", "сентябрь", "сентября"), ("окт", "октябрь", "октября"),
        ("ноя", "нояб", "ноябрь", "ноября"), ("дек", "декабрь", "декабря")), 1)
    for name in names
}
def split_client_and_age(text: str) -> tuple[str, str]:
    value = clean_text(re.sub(r"(?:Клиент\s+дополнил\s+заказ|Топ-заказ)", "", text or "", flags=re.I))
    patterns = [
        r"(.+?)\s+(\d+\s+(?:минут[аыу]?|час(?:а|ов)?|день|дня|дней|недел[юьи])\s+назад)$",
        r"(.+?)\s+((?:Сегодня|Вчера)(?:\s*,?\s*(?:в\s*)?\d{1,2}:\d{2})?)$",
        r"(.+?)\s+(\d{1,2}\s+[А-Яа-яЁё]+\.?(?:\s+\d{4})?(?:\s*,?\s*(?:в\s*)?\d{1,2}:\d{2})?)$",
        r"(.+?)\s+(Только\s+что)$",
    ]
    for pattern in patterns:
        match = re.fullmatch(pattern, value, flags=re.I)
        if match:
            return clean_text(match[1]), clean_text(match[2])
    return value, ""


def estimate_posted_at(value: str, captured_at: datetime) -> tuple[str | None, str | None]:
    # Relative time is anchored to the SAVED FILE, never to a later parsing run.
    text = clean_text(value).lower()
    if text == "только что":
        return captured_at.isoformat(timespec="seconds"), "relative_estimate"
    match = re.fullmatch(r"(\d+)\s+(минут[аыу]?|час(?:а|ов)?|день|дня|дней|недел[юьи])\s+назад", text)
    if match:
        n, unit = int(match[1]), match[2]
        delta = (timedelta(minutes=n) if unit.startswith("минут") else timedelta(hours=n)
                 if unit.startswith("час") else timedelta(weeks=n) if unit.startswith("недел") else timedelta(days=n))
        return (captured_at - delta).isoformat(timespec="seconds"), "relative_estimate"
    match = re.fullmatch(r"(сегодня|вчера)(?:\s*,?\s*(?:в\s*)?(\d{1,2}):(\d{2}))?", text)
    if match:
        day = captured_at - timedelta(days=int(match[1] == "вчера"))
        if match[2] is None:
            return day.date().isoformat(), "day"
        try:
            day = day.replace(hour=int(match[2]), minute=int(match[3]), second=0, microsecond=0)
        except ValueError:
            return None, None
        return day.isoformat(timespec="seconds"), "minute"
    match = re.fullmatch(r"(\d{1,2})\s+([а-яё]+)\.?(?:\s+(\d{4}))?(?:\s*,?\s*(?:в\s*)?(\d{1,2}):(\d{2}))?", text)
    if match:
        month = MONTH_ALIASES.get(match[2])
        if month is None:
            return None, None
        try:
            dt = captured_at.replace(year=int(match[3] or captured_at.year), month=month, day=int(match[1]),
                                     hour=int(match[4] or 0), minute=int(match[5] or 0), second=0, microsecond=0)
            if not match[3] and dt.date() > captured_at.date():
                dt = dt.replace(year=dt.year - 1)
        except ValueError:
            return None, None
        return (dt.isoformat(timespec="seconds") if match[4] else dt.date().isoformat(),
                "inferred_year" if not match[3] else "minute" if match[4] else "day")
    return None, None


def split_details(details_text: str) -> tuple[str, bool, str, str]:
    """
    Возвращает:
      description, remote, location, schedule
    """
    details = clean_text(details_text)

    remote = bool(re.search(r"\bДистанционно\b", details, flags=re.I))

    location = ""
    schedule = ""

    remote_match = re.search(
        r"Дистанционно\s*·\s*(.+?)(?=\s+(?:Начать\s+)?"
        r"\d{1,2}\s+[А-Яа-яЁё]{3,}|"
        r"\s+\d{1,2}\s+[А-Яа-яЁё]{3,}\.\s*\(|$)",
        details,
        flags=re.I,
    )

    if remote_match:
        location = clean_text(remote_match.group(1))
        description = clean_text(details[:remote_match.start()])

        tail = clean_text(details[remote_match.end():])
        schedule = tail
    else:
        description = details

        # Более общий fallback для формата "· Москва"
        loc_match = re.search(
            r"(?:^|\s)·\s*([^·]{2,80}?)(?=\s+\d{1,2}\s+[А-Яа-яЁё]{3,}|$)",
            details,
        )
        if loc_match:
            location = clean_text(loc_match.group(1))

    return description, remote, location, schedule


def direct_card_block(node, card):
    """Поднимается до блока карточки без привязки к сгенерированным CSS-классам."""
    if node is None:
        return None
    while node.parent is not None and node.parent is not card:
        node = node.parent
    return node if node.parent is card else None


def extract_orders(html: str, source_file: str,
                   captured_at: datetime | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("a[data-testid*='_order-snippet']")
    records = {}
    ranks = {}
    parsed_at = datetime.now().astimezone().isoformat(timespec="seconds")

    for card in cards:
        url = card.get("href", "") or ""
        order_id = get_order_id(url, card.get("data-testid", "") or "")
        if not order_id:
            continue
        direct_divs = card.find_all("div", recursive=False)
        heading = card.find(["h3", "h2"])
        header = direct_card_block(heading, card)
        paragraph = card.find("p")
        details = direct_card_block(paragraph or card.find("ul"), card)
        full_text = clean_text(card.get_text(" ", strip=True))
        warnings = []

        if heading is not None:
            title = clean_text(heading.get_text(" ", strip=True))
            header_text = clean_text(header.get_text(" ", strip=True))
        else:
            warnings.append("unrecognized_header_structure")
            header_text = clean_text(direct_divs[1].get_text(" ", strip=True)) if len(direct_divs) >= 2 else full_text
            title = parse_title_and_budget(header_text)[0]
        budget = budget_fields(header_text)

        description = clean_text(paragraph.get_text(" ", strip=True)) if paragraph else ""
        budget["budget_unit_source"] = "header" if budget["budget_unit"] else None
        # В некоторых карточках почасовая ставка указана только в описании.
        # Учитываем её, когда сумма совпадает с ценой в заголовке.
        if not budget["budget_unit"]:
            hourly = re.search(r"(\d[\d\s\u00a0\u202f]*)\s*(?:₽\s*)?/\s*(?:час|ч)\b", description, flags=re.I)
            if hourly:
                amount = int(re.sub(r"\D", "", hourly.group(1)))
                if amount in {budget["budget_min_rub"], budget["budget_max_rub"]}:
                    budget["budget_unit"] = "hour"
                    budget["budget_unit_source"] = "description"
        remote = False
        location = ""
        schedule = ""
        travel_locations = ""
        labeled_items = card.select("li[aria-label]")
        for item in labeled_items:
            label = clean_text(item.get("aria-label", "")).lower().rstrip(":")
            text = clean_text(item.get_text(" ", strip=True))
            if label == "дистанционно":
                remote = True
                location = clean_text(re.sub(r"^Дистанционно\s*[·•:]?\s*", "", text, flags=re.I))
            elif label in {"адрес клиента", "местоположение", "адрес"}:
                location = text
            elif label == "удобное время":
                schedule = text
            elif label == "готов приехать":
                travel_locations = text
        if not labeled_items:
            detail_text = clean_text(details.get_text(" ", strip=True)) if details else ""
            if detail_text:
                fallback_description, remote, location, schedule = split_details(detail_text)
                description = description or fallback_description
            warnings.append("missing_labeled_details")
        elif not location and travel_locations:
            location = travel_locations

        # Основная карточка содержит отдельный нижний блок клиента/времени.
        # В карусели его нет: время находится над заголовком, клиент не показан.
        footer = next((block for block in reversed(direct_divs)
                       if block is not header and block is not details
                       and not block.find("button")
                       and clean_text(block.get_text(" ", strip=True))), None)
        client, posted = split_client_and_age(
            footer.get_text(" ", strip=True) if footer else ""
        )
        if not posted and header is not None:
            for text in header.stripped_strings:
                value = clean_text(text)
                _, candidate = split_client_and_age("_ " + value)
                if candidate:
                    posted = candidate
                    break
        try:
            analytics = json.loads(parse_qs(urlparse(url).query).get("analytics_data", ["{}"])[0])
            card_source = analytics.get("source", "unknown")
        except (ValueError, TypeError, AttributeError):
            card_source = "unknown"
        posted_at, precision = estimate_posted_at(posted, captured_at) if captured_at else (None, None)
        record = {
            "type": "profi_order", "order_id": order_id, "title": title,
            **budget, "remote": remote, "location": location,
            "schedule": schedule, "client": client, "posted": posted,
            "posted_at": posted_at, "posted_at_precision": precision,
            "description": description, "url": url, "source_file": source_file,
            "collected_at": parsed_at,
            "captured_at": captured_at.isoformat(timespec="seconds") if captured_at else None,
            "card_source": card_source, "travel_locations": travel_locations,
            "badges": ["Топ-заказ"] if footer and "Топ-заказ" in footer.get_text() else [],
            "parser_warnings": warnings, "text": full_text,
        }
        # Один ID может встретиться в карусели и на доске: сохраняем более полную
        # карточку, при равенстве отдаём предпочтение основной доске.
        rank = (sum(bool(record[k]) for k in ("title", "budget", "description", "client", "posted", "location", "schedule")), card_source == "new_board")
        if order_id not in records or rank > ranks[order_id]:
            records[order_id] = record
            ranks[order_id] = rank

    return list(records.values())


