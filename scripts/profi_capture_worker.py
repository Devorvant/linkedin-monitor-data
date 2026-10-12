from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import pyautogui
except ImportError:
    raise SystemExit(
        "Не установлен pyautogui.\n"
        "Установите один раз:\n"
        "  pip install pyautogui"
    )

# ---- Ниже подключаем проверенный парсер/GitHub код из этого же файла ----

import base64
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    from bs4 import BeautifulSoup
except ImportError:
    raise SystemExit(
        "Не установлен beautifulsoup4.\n"
        "Установите один раз:\n"
        "  pip install beautifulsoup4 lxml"
    )


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name.lower() == "cache" else SCRIPT_DIR
REPORTS_DIR = ROOT / "reports"
SAVED_PAGES_DIR = ROOT / "saved_pages"

OUTPUT_JSON = REPORTS_DIR / "profi_latest.json"

GITHUB_OWNER = "Devorvant"
GITHUB_REPO = "linkedin-monitor-data"
GITHUB_BRANCH = "main"
GITHUB_PATH = "profi_latest.json"
GITHUB_TOKEN_FILES = (
    ROOT / "github_token.txt",
    REPORTS_DIR / "github_token.txt",
)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def decode_html_bytes(payload: bytes, declared_charset: str = "") -> str:
    """
    MHTML Profi.ru может декларировать windows-1251, хотя фактические bytes
    сохранены как UTF-8. Поэтому UTF-8 проверяем первым.
    """
    if not payload:
        return ""

    candidates = ["utf-8", declared_charset, "windows-1251", "cp1251", "cp1252"]
    seen = set()
    best = ""
    best_score = None

    for enc in candidates:
        enc = (enc or "").strip()
        if not enc or enc.lower() in seen:
            continue
        seen.add(enc.lower())

        try:
            text = payload.decode(enc, errors="replace")
        except Exception:
            continue

        replacements = text.count("\ufffd")
        mojibake = sum(text.count(x) for x in ("Р", "С", "Ð", "Ñ"))
        score = replacements * 1000 + mojibake

        if best_score is None or score < best_score:
            best_score = score
            best = text

        if enc.lower() == "utf-8" and replacements == 0:
            return text

    return best or payload.decode("utf-8", errors="replace")


def read_html_or_mhtml(path: Path) -> str:
    if path.suffix.lower() in {".mhtml", ".mht"}:
        with path.open("rb") as fh:
            message = BytesParser(policy=policy.default).parse(fh)

        parts = []

        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() != "text/html":
                    continue

                payload = part.get_payload(decode=True) or b""
                parts.append(
                    decode_html_bytes(
                        payload,
                        part.get_content_charset() or "",
                    )
                )
        else:
            payload = message.get_payload(decode=True) or b""
            parts.append(
                decode_html_bytes(
                    payload,
                    message.get_content_charset() or "",
                )
            )

        return "\n".join(parts)

    return decode_html_bytes(path.read_bytes())


def looks_like_profi_file(path: Path) -> bool:
    try:
        html = read_html_or_mhtml(path)
    except Exception:
        return False

    low = html[:300000].lower()
    return (
        "profi.ru" in low
        and (
            "order-snippet" in low
            or "заказы" in low
            or "backoffice/n.php?o=" in low
        )
    )


def find_latest_profi_mhtml() -> Path:
    """
    Ищет только файлы заказов Profi.ru:
      Заказы*.mhtml
      Заказы*.mht
      Заказы*.html
      Заказы*.htm

    Берёт самый новый по времени изменения.
    Остальные HTML/MHTML в папке вообще не проверяются.
    """
    candidates = []

    patterns = (
        "Заказы*.mhtml",
        "Заказы*.mht",
        "Заказы*.html",
        "Заказы*.htm",
    )

    for folder in (ROOT, SAVED_PAGES_DIR):
        if not folder.exists():
            continue

        for pattern in patterns:
            for path in folder.glob(pattern):
                if not path.is_file():
                    continue

                try:
                    candidates.append((path.stat().st_mtime, path))
                except OSError:
                    pass

    if not candidates:
        raise FileNotFoundError(
            "Не найден файл Заказы*.mhtml / Заказы*.html.\n"
            "Положите файл рядом со скриптом или в папку saved_pages."
        )

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


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


def capture_time(path: Path) -> tuple[datetime, str]:
    """Дата снимка из MHTML; при её отсутствии — время изменения файла."""
    moscow = timezone(timedelta(hours=3))
    if path.suffix.lower() in {".mhtml", ".mht"}:
        with path.open("rb") as fh:
            headers = BytesParser(policy=policy.default).parsebytes(
                fh.read(65536), headersonly=True
            )
        try:
            value = parsedate_to_datetime(headers.get("Date", ""))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(moscow), "mhtml_date"
        except (TypeError, ValueError, IndexError, AttributeError):
            pass
    return datetime.fromtimestamp(path.stat().st_mtime, moscow), "file_mtime"


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


def save_output_json(source_path: Path, records: list[dict]) -> dict:
    if not records:
        raise RuntimeError("Заказы не найдены: прежний JSON сохранён, загрузка не выполнена.")
    REPORTS_DIR.mkdir(exist_ok=True)
    captured_at, capture_source = capture_time(source_path)

    result = {
        "schema_version": 1,
        "parser_version": "4.4",
        "source": {
            "filename": source_path.name,
            "captured_at": captured_at.isoformat(timespec="seconds"),
            "capture_time_source": capture_source,
            "full_path": str(source_path),
            "modified_at": datetime.fromtimestamp(
                source_path.stat().st_mtime
            ).isoformat(timespec="seconds"),
            "parsed_at": datetime.now().isoformat(timespec="seconds"),
        },
        "summary": {
            "orders": len(records),
            "remote_orders": sum(1 for x in records if x.get("remote")),
        },
        "orders": records,
    }

    OUTPUT_JSON.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return result


def get_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token

    for path in GITHUB_TOKEN_FILES:
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token

    raise RuntimeError(
        "GitHub token не найден. Создайте github_token.txt рядом со скриптом "
        "или задайте переменную окружения GITHUB_TOKEN."
    )


def github_api_request(url: str, method: str = "GET", data: dict | None = None):
    token = get_github_token()

    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "profi-parser",
    }

    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API error {exc.code}: {error_body}"
        ) from exc


def upload_to_github(local_json_path: Path) -> None:
    api_url = (
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/"
        f"{GITHUB_PATH}"
    )

    sha = None
    lookup_url = f"{api_url}?ref={GITHUB_BRANCH}"

    try:
        current = github_api_request(lookup_url, method="GET")
        sha = current.get("sha")
    except RuntimeError as exc:
        # 404 означает, что файла ещё нет — это нормально.
        if "GitHub API error 404:" not in str(exc):
            raise

    content_b64 = base64.b64encode(
        local_json_path.read_bytes()
    ).decode("ascii")

    payload = {
        "message": f"Update {GITHUB_PATH}",
        "content": content_b64,
        "branch": GITHUB_BRANCH,
    }

    if sha:
        payload["sha"] = sha

    github_api_request(api_url, method="PUT", data=payload)




# ======================================================================
# GUI AUTOMATION — повторяет рабочую цепочку LinkedIn
# ======================================================================

PROFI_URL = "https://profi.ru/backoffice/n.php"

CHROME_PATHS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]

PAGE_LOAD_WAIT_SECONDS = 8.0

# Profi.ru подгружает карточки по мере прокрутки.
# Делаем существенно больше прокруток, чем в первой версии.
SCROLL_COUNT = 35
SCROLL_DELAY_SECONDS = 1.0
SCROLL_METHOD = "pagedown"

# Координаты относительно ширины/высоты конкретного монитора.
# Можно настроить при запуске, не редактируя код.
SCROLL_X_RATIO = 0.90
SAVE_MENU_X_RATIO = 0.74
SAVE_MENU_Y_OFFSET_RATIO = 0.125
CLOSE_X_RATIO = 0.985
CLOSE_Y_RATIO = 0.015
PROFI_WINDOW_HANDLE = None

SAVE_DIALOG_WAIT_SECONDS = 2.5
SAVE_FINISH_WAIT_SECONDS = 6.0
SAVE_DETECT_TIMEOUT = 22.0


def find_chrome() -> Path:
    for path in CHROME_PATHS:
        if path.exists():
            return path
    raise FileNotFoundError("Google Chrome не найден в стандартных папках.")


def windows_api():
    if sys.platform != "win32":
        raise RuntimeError("Управление окнами поддерживается только в Windows.")
    api = ctypes.windll.user32
    # HWND — указатель: типы нужны также для 64-битного Python.
    for name in ("IsWindow", "IsWindowVisible", "GetWindowTextLengthW",
                 "BringWindowToTop", "SetForegroundWindow"):
        getattr(api, name).argtypes = [wintypes.HWND]
    api.GetForegroundWindow.restype = wintypes.HWND
    api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
    api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    api.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
    api.GetDlgItem.restype = wintypes.HWND
    return api


def window_info(hwnd) -> tuple[str, str]:
    api = windows_api()
    title = ctypes.create_unicode_buffer(api.GetWindowTextLengthW(hwnd) + 1)
    kind = ctypes.create_unicode_buffer(256)
    api.GetWindowTextW(hwnd, title, len(title))
    api.GetClassNameW(hwnd, kind, len(kind))
    return title.value, kind.value


def chrome_windows() -> list:
    api = windows_api()
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, _):
        if api.IsWindowVisible(hwnd):
            title, kind = window_info(hwnd)
            if kind == "Chrome_WidgetWin_1" and "chrome" in title.lower():
                found.append(hwnd)
        return True

    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    if not api.EnumWindows(visit, 0):
        raise RuntimeError("Не удалось получить список окон Windows.")
    return found


def close_previous_chrome_windows() -> None:
    """Перед запуском закрывает все прежние окна Chrome обычным WM_CLOSE."""
    api = windows_api()
    handles = chrome_windows()
    print(f"Закрываю прежние окна Chrome: {len(handles)}")
    for hwnd in handles:
        if not api.PostMessageW(hwnd, 0x0010, 0, 0):
            raise RuntimeError("Не удалось отправить команду закрытия окна Chrome.")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not chrome_windows():
            return
        time.sleep(0.3)
    raise RuntimeError(
        "Прежние окна Chrome не закрылись. Закройте оставшиеся окна/диалоги "
        "и повторите запуск. Новое окно не открыто."
    )


def focus_profi_chrome(maximize: bool = False) -> bool:
    """Активирует одно окно Profi.ru, проверяя фактический фокус."""
    global PROFI_WINDOW_HANDLE
    api = windows_api()
    found = []
    for hwnd in chrome_windows():
        title, _ = window_info(hwnd)
        if any(word in title.lower() for word in ("профи", "profi", "заказы")):
            found.append(hwnd)
    if PROFI_WINDOW_HANDLE in found:
        hwnd = PROFI_WINDOW_HANDLE
    elif len(found) == 1:
        hwnd = found[0]
    else:
        print(f"Ожидалось одно окно Profi.ru; найдено: {len(found)}")
        return False
    api.ShowWindow(hwnd, 9)  # SW_RESTORE
    time.sleep(0.3)
    if maximize:
        api.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        time.sleep(0.5)
    api.BringWindowToTop(hwnd)
    api.SetForegroundWindow(hwnd)
    time.sleep(0.8)
    if api.GetForegroundWindow() != hwnd:
        print("Не удалось сделать окно Profi.ru активным.")
        return False
    PROFI_WINDOW_HANDLE = hwnd
    return True


def activate_and_open_profi(wait_seconds: float = PAGE_LOAD_WAIT_SECONDS) -> None:
    chrome = find_chrome()
    subprocess.Popen([str(chrome), "--new-window", PROFI_URL])

    print(f"Открываю обычный Chrome: {PROFI_URL}")
    print(f"Ожидание загрузки: {wait_seconds:.0f} с")
    time.sleep(wait_seconds)


def expand_and_scroll_profi() -> None:
    """
    Полностью прогружаем доску заказов.
    Клик даёт фокус странице, затем PageDown много раз.

    ВАЖНО:
    не возвращаемся Ctrl+Home перед сохранением —
    сохраняем уже полностью прогруженный DOM страницы.
    """
    width, height = pyautogui.size()

    x = int(width * SCROLL_X_RATIO)
    y = int(height * 0.55)

    print(f"Монитор: {width}x{height}. Точка прокрутки: x={x}, y={y}")
    pyautogui.moveTo(x, y, duration=0.4)
    pyautogui.click()
    time.sleep(0.7)

    print("Прокручиваю и полностью прогружаю заказы Profi.ru...")

    previous_pos = None
    same_count = 0

    for index in range(SCROLL_COUNT):
        if SCROLL_METHOD == "wheel":
            pyautogui.scroll(-60)
        else:
            pyautogui.press("pagedown")

        print(f"Прокрутка {index + 1}/{SCROLL_COUNT}")
        time.sleep(SCROLL_DELAY_SECONDS)

    # Дополнительная пауза — дать динамическим карточкам догрузиться.
    print("Жду завершения подгрузки карточек...")
    time.sleep(3.0)


def snapshot_saved_pages(folder: Path) -> dict[str, tuple[float, int]]:
    """
    Снимок MHTML/HTML файлов до Save As:
    путь -> (mtime, size).
    """
    result = {}

    if not folder.exists():
        return result

    for path in folder.iterdir():
        if not path.is_file():
            continue

        if path.suffix.lower() not in {".mhtml", ".mht", ".html", ".htm"}:
            continue

        try:
            st = path.stat()
            result[str(path.resolve())] = (st.st_mtime, st.st_size)
        except OSError:
            pass

    return result


def wait_for_saved_page_change(
    folder: Path,
    before: dict[str, tuple[float, int]],
    timeout: float = SAVE_DETECT_TIMEOUT,
) -> Path | None:
    """
    Ждёт новый либо изменившийся HTML/MHTML после Save As.
    Не зависит от стандартного имени файла Chrome.
    """
    deadline = time.time() + timeout
    candidate = None

    while time.time() < deadline:
        changed = []

        if not folder.exists():
            time.sleep(0.5)
            continue

        for path in folder.iterdir():
            if not path.is_file():
                continue

            if path.suffix.lower() not in {".mhtml", ".mht", ".html", ".htm"}:
                continue

            try:
                st = path.stat()
            except OSError:
                continue

            key = str(path.resolve())
            old = before.get(key)

            if old is None or old != (st.st_mtime, st.st_size):
                changed.append((st.st_mtime, st.st_size, path))

        if changed:
            changed.sort(
                reverse=True,
                key=lambda item: (item[0], item[1]),
            )

            newest = changed[0][2]

            # Убеждаемся, что Chrome закончил запись файла.
            try:
                size1 = newest.stat().st_size
                time.sleep(0.9)
                size2 = newest.stat().st_size

                if size1 == size2 and size2 > 0:
                    return newest

                candidate = newest
            except OSError:
                pass

        time.sleep(0.5)

    return None


def require_save_dialog():
    """Не вводим путь, если координаты открыли печать вместо Save As."""
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        title, kind = window_info(windows_api().GetForegroundWindow())
        if kind == "#32770" and any(
            word in title.lower() for word in ("сохран", "save")
        ):
            return windows_api().GetForegroundWindow()
        time.sleep(0.2)
    raise RuntimeError(
        "Не найден диалог 'Сохранить как'. Проверьте --save-menu-offset "
        "на вашем мониторе: возможно, выбран пункт печати."
    )


def wait_for_saved_page_in_folders(
    folders: tuple[Path, ...],
    before: dict[str, tuple[float, int]],
    timeout: float = SAVE_DETECT_TIMEOUT,
) -> Path | None:
    """Ищет изменённый файл текущего запуска в корне и saved_pages."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = {}
        for folder in folders:
            current.update(snapshot_saved_pages(folder))
        changed = [
            (stat[0], Path(name), stat)
            for name, stat in current.items()
            if stat[1] > 0 and before.get(name) != stat
        ]
        for _, path, stat in sorted(changed, key=lambda item: item[0], reverse=True):
            time.sleep(0.9)
            try:
                after = path.stat()
                if (after.st_mtime, after.st_size) == stat and looks_like_profi_file(path):
                    return path
            except OSError:
                pass
        time.sleep(0.3)
    return None


def save_page_without_changing_name() -> None:
    """
    Открывает Chrome -> 'Сохранить как...'
    и НЕ МЕНЯЕТ стандартное имя файла.

    В окне Save As подтверждаем стандартное имя клавишей Enter.
    Перенос в saved_pages выполняется после обнаружения сохранённого файла.
    Chrome сохраняет как, например:
        Заказы.mhtml
        Заказы (1).mhtml
        Заказы(3).mhtml
    """
    if not focus_profi_chrome(maximize=True):
        raise RuntimeError("Не найдено активное окно Profi.ru для сохранения.")
    time.sleep(0.8)

    width, height = pyautogui.size()

    click_x = int(width * 0.88)
    click_y = int(height * 0.46)

    print(
        f"Открываю контекстное меню: "
        f"x={click_x}, y={click_y}"
    )

    pyautogui.moveTo(click_x, click_y, duration=0.35)
    pyautogui.rightClick()
    time.sleep(0.8)

    save_x = int(width * SAVE_MENU_X_RATIO)
    save_y = click_y + int(height * SAVE_MENU_Y_OFFSET_RATIO)

    print(
        f"Нажимаю 'Сохранить как...': "
        f"x={save_x}, y={save_y}"
    )

    pyautogui.moveTo(save_x, save_y, duration=0.3)
    pyautogui.click()

    time.sleep(SAVE_DIALOG_WAIT_SECONDS)
    require_save_dialog()

    print("Имя файла НЕ меняю. Подтверждаю стандартное имя Chrome...")
    pyautogui.press("enter")
    time.sleep(SAVE_FINISH_WAIT_SECONDS)

    # Если файл с таким стандартным именем уже существует,
    # возможен диалог подтверждения замены.
    # Сначала пробуем выбрать подтверждение.
    title, kind = window_info(windows_api().GetForegroundWindow())
    if kind == "#32770" and any(
        word in title.lower() for word in ("подтверж", "confirm")
    ):
        pyautogui.press("left")
        time.sleep(0.2)
        pyautogui.press("enter")
        time.sleep(1.0)


def rename_saved_profi_page(path: Path) -> Path:
    """
    Переименовывает уже сохранённый Chrome файл ПОСЛЕ сохранения.
    """
    if not path.exists():
        return path

    stamp = datetime.fromtimestamp(
        path.stat().st_mtime
    ).strftime("%Y-%m-%d_%H-%M-%S")

    suffix = path.suffix.lower() or ".mhtml"

    new_path = path.parent / f"profi_orders_{stamp}{suffix}"

    counter = 2
    while (
        new_path.exists()
        and new_path.resolve() != path.resolve()
    ):
        new_path = path.parent / (
            f"profi_orders_{stamp}_{counter}{suffix}"
        )
        counter += 1

    if new_path.resolve() == path.resolve():
        return path

    path.rename(new_path)

    print(
        "Переименован файл текущего запуска: "
        f"{path.name} -> {new_path.name}"
    )

    return new_path


def capture_profi_page() -> Path:
    """
    Полная рабочая цепочка:
      открыть Chrome
      -> прокрутить
      -> snapshot saved_pages
      -> Save As без смены имени
      -> найти реально сохранённый файл
      -> переименовать после сохранения
    """
    saved_dir = SAVED_PAGES_DIR
    saved_dir.mkdir(exist_ok=True)

    print(
        "\nЧерез 5 секунд начнётся управление клавиатурой и мышью.\n"
        "Не переключайте окна до завершения сохранения.\n"
        "Для аварийной остановки переместите мышь "
        "в левый верхний угол.\n"
    )

    time.sleep(5)

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.1

    find_chrome()  # Проверяем наличие браузера до закрытия прежних окон.
    close_previous_chrome_windows()
    activate_and_open_profi()

    print("Разворачиваю Profi.ru/Chrome на весь экран...")
    if not focus_profi_chrome(maximize=True):
        raise RuntimeError("Не удалось активировать новое окно Profi.ru.")

    expand_and_scroll_profi()

    # Очень важно: snapshot непосредственно перед сохранением.
    capture_folders = (saved_dir, ROOT)
    before_save = {}
    for folder in capture_folders:
        before_save.update(snapshot_saved_pages(folder))

    save_page_without_changing_name()

    print("Жду фактически сохранённый файл...")
    actual_saved = wait_for_saved_page_in_folders(
        capture_folders,
        before_save,
        timeout=SAVE_DETECT_TIMEOUT,
    )

    if actual_saved is None:
        raise RuntimeError(
            "Не найден новый MHTML/HTML в корне скрипта или saved_pages. "
            "Проверьте, в какую папку Chrome сохранил файл."
        )

    print(f"Chrome сохранил: {actual_saved.name}")

    renamed = rename_saved_profi_page(actual_saved)
    if renamed.parent.resolve() != saved_dir.resolve():
        destination = saved_dir / renamed.name
        counter = 2
        while destination.exists():
            destination = saved_dir / f"{renamed.stem}_{counter}{renamed.suffix}"
            counter += 1
        shutil.move(str(renamed), str(destination))
        renamed = destination
        print(f"Перенёс сохранённый файл в saved_pages: {renamed.name}")

    print(f"Файл текущего запуска: {renamed}")
    return renamed



def close_profi_window_by_mouse() -> None:
    """После сохранения закрывает окно через верхний правый крестик."""
    if not focus_profi_chrome(maximize=True):
        raise RuntimeError("Не удалось активировать окно Profi.ru для закрытия.")
    api = windows_api()
    hwnd = PROFI_WINDOW_HANDLE
    width, height = pyautogui.size()
    # Окно выше развёрнуто на весь экран. Берём верхний правый угол
    # монитора без невидимых рамок GetWindowRect.
    close_x = int(width * CLOSE_X_RATIO)
    close_y = int(height * CLOSE_Y_RATIO)
    if not (0 <= close_x < width and 0 <= close_y < height):
        raise RuntimeError("Крестик окна находится вне основного монитора.")
    print(f"Закрываю окно Profi.ru: x={close_x}, y={close_y}")
    pyautogui.moveTo(close_x, close_y, duration=0.3)
    if api.GetForegroundWindow() != hwnd:
        raise RuntimeError("Окно сменилось перед кликом закрытия.")
    pyautogui.click()
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if not api.IsWindow(hwnd):
            return
        time.sleep(0.2)
    raise RuntimeError(
        "Окно не закрылось. Проверьте --close-x/--close-y или открытый диалог. "
        "Сохранённый файл оставлен в saved_pages."
    )


def run_full_chain() -> None:
    source = capture_profi_page()

    print("\nЗакрываю окно Profi.ru...")
    close_profi_window_by_mouse()

    print("\nПарсинг сохранённого Profi.ru...")
    html = read_html_or_mhtml(source)
    captured_at, _ = capture_time(source)
    records = extract_orders(html, source.name, captured_at)

    save_output_json(source, records)

    print(f"Заказов найдено: {len(records)}")
    print(
        "Удалённых: "
        f"{sum(1 for item in records if item.get('remote'))}"
    )
    print(f"JSON: {OUTPUT_JSON}")

    print("\nЗагрузка profi_latest.json на GitHub...")
    upload_to_github(OUTPUT_JSON)

    print(
        "GitHub: "
        f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/blob/"
        f"{GITHUB_BRANCH}/{GITHUB_PATH}"
    )

    print("\nГОТОВО.")


def run_parse_only(file_arg: str | None = None) -> None:
    if file_arg:
        source = Path(file_arg).expanduser().resolve()
        if not source.exists():
            raise RuntimeError(f"Файл не найден: {source}")
    else:
        # После переименования файлы имеют profi_orders_...
        candidates = []

        for folder in (SAVED_PAGES_DIR, ROOT):
            if not folder.exists():
                continue

            for pattern in (
                "profi_orders_*.mhtml",
                "profi_orders_*.mht",
                "Заказы*.mhtml",
                "Заказы*.mht",
            ):
                for path in folder.glob(pattern):
                    if path.is_file():
                        candidates.append(
                            (path.stat().st_mtime, path)
                        )

        if not candidates:
            raise RuntimeError(
                "Не найден сохранённый файл Profi.ru."
            )

        candidates.sort(
            key=lambda item: item[0],
            reverse=True,
        )
        source = candidates[0][1]

    print(f"Источник: {source}")

    html = read_html_or_mhtml(source)
    captured_at, _ = capture_time(source)
    records = extract_orders(html, source.name, captured_at)

    save_output_json(source, records)
    upload_to_github(OUTPUT_JSON)

    print(f"Заказов: {len(records)}")
    print(f"JSON: {OUTPUT_JSON}")
    print("GitHub обновлён.")


def main():
    global ROOT, REPORTS_DIR, SAVED_PAGES_DIR, OUTPUT_JSON, GITHUB_TOKEN_FILES
    global SCROLL_X_RATIO, SAVE_MENU_X_RATIO, SAVE_MENU_Y_OFFSET_RATIO
    global CLOSE_X_RATIO, CLOSE_Y_RATIO

    parser = argparse.ArgumentParser(
        description=(
            "Profi.ru -> Chrome -> MHTML -> "
            "profi_latest.json -> GitHub"
        )
    )

    parser.add_argument(
        "file",
        nargs="?",
        help="Явный MHTML для режима --parse-only.",
    )

    parser.add_argument(
        "--parse-only",
        action="store_true",
        help="Не открывать Chrome; обработать последний сохранённый MHTML.",
    )

    parser.add_argument(
        "--scroll-x", type=float, default=SCROLL_X_RATIO,
        help="Точка прокрутки по ширине экрана, от 0 до 1 (по умолчанию 0.90).",
    )
    parser.add_argument(
        "--save-menu-x", type=float, default=SAVE_MENU_X_RATIO,
        help="Точка выбора пункта меню по ширине экрана (по умолчанию 0.74).",
    )
    parser.add_argument(
        "--save-menu-offset", type=float, default=SAVE_MENU_Y_OFFSET_RATIO,
        help=("Смещение пункта 'Сохранить как' вниз от точки правого клика "
              "в долях высоты экрана (по умолчанию 0.125). "
              "Меньшее значение поднимает точку."),
    )

    parser.add_argument(
        "--close-x", type=float, default=CLOSE_X_RATIO,
        help="Крестик по ширине окна, от 0 до 1 (по умолчанию 0.985).",
    )
    parser.add_argument(
        "--close-y", type=float, default=CLOSE_Y_RATIO,
        help="Крестик по высоте окна, от 0 до 1 (по умолчанию 0.015).",
    )

    parser.add_argument("--root", type=Path, default=ROOT,
                        help="Рабочая папка с saved_pages, reports и существующим GitHub token.")
    parser.add_argument("-a", "--auto", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    ROOT = args.root.expanduser().resolve()
    REPORTS_DIR = ROOT / "reports"
    SAVED_PAGES_DIR = ROOT / "saved_pages"
    OUTPUT_JSON = REPORTS_DIR / "profi_latest.json"
    GITHUB_TOKEN_FILES = (ROOT / "github_token.txt", REPORTS_DIR / "github_token.txt")
    get_github_token()  # Проверяем существующие настройки до управления мышью.
    for option, value in (
        ("--scroll-x", args.scroll_x),
        ("--close-x", args.close_x),
        ("--close-y", args.close_y),
        ("--save-menu-x", args.save_menu_x),
        ("--save-menu-offset", args.save_menu_offset),
    ):
        if not 0 < value < 1:
            parser.error(f"{option}: требуется число больше 0 и меньше 1.")
    if 0.46 + args.save_menu_offset >= 1:
        parser.error("--save-menu-offset: точка меню выходит за нижнюю границу экрана.")

    CLOSE_X_RATIO = args.close_x
    CLOSE_Y_RATIO = args.close_y
    SCROLL_X_RATIO = args.scroll_x
    SAVE_MENU_X_RATIO = args.save_menu_x
    SAVE_MENU_Y_OFFSET_RATIO = args.save_menu_offset

    print("=" * 72)
    print(
        " Profi.ru -> full scroll -> Save As -> rename "
        "-> JSON -> GitHub"
    )
    print("=" * 72)

    if args.parse_only:
        run_parse_only(args.file)
    else:
        run_full_chain()


if __name__ == "__main__":
    main()
