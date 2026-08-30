from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pyautogui

ACTION_API = "https://linkedin-actions.devorvant.workers.dev/actions"

DEVICE_CONFIGS = {
    "1": {
        "name": "laptop",
        "root": Path(r"C:\Users\kusc\AppData\Local\Programs\Python\Python311\linkedin_desktop_automation"),
        "chrome": Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    },
    "2": {
        "name": "pc2",
        "root": Path(r"H:\Users\kusc\AppData\Local\Programs\Python\Python311\linkedin_desktop_automation"),
        "chrome": Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    },
}


def choose_device(explicit: str | None) -> dict:
    if explicit:
        key = explicit
    else:
        print("\nГде выполняем?")
        print("  1 - ноутбук")
        print("  2 - компьютер")
        key = input("Выбор [1/2]: ").strip()
    if key not in DEVICE_CONFIGS:
        raise SystemExit("Неверный выбор устройства")
    return DEVICE_CONFIGS[key]


def load_secret(root: Path) -> str:
    env = os.environ.get("APPROVAL_SECRET", "").strip()
    if env:
        return env
    for path in [root / "approval_secret.txt", Path(__file__).resolve().parent / "approval_secret.txt"]:
        if path.exists():
            secret = path.read_text(encoding="utf-8").strip()
            if secret:
                return secret
    raise SystemExit("APPROVAL_SECRET не найден. Создай локальный approval_secret.txt или задай APPROVAL_SECRET.")


def fetch_queue(secret: str) -> dict:
    req = Request(
        ACTION_API,
        headers={"User-Agent": "linkedin-monitor-action-executor", "X-Approval-Key": secret},
        method="GET",
    )
    try:
        with urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Cloudflare HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise SystemExit(f"Cloudflare connection error: {exc}") from exc


def approved_items(payload: dict) -> list[dict]:
    items = payload.get("items") or []
    return [
        x for x in items
        if isinstance(x, dict) and str(x.get("state", "APPROVED")).upper() == "APPROVED"
    ]


def item_url(item: dict) -> str:
    return str(item.get("profile_url") or item.get("source_url") or "").strip()


def item_label(item: dict) -> str:
    target = item.get("target") or {}
    name = target.get("name") or target.get("company") or "—"
    return (
        f"{name} | {item.get('action') or '—'} | "
        f"source={item.get('source_date') or '—'} | approved={item.get('approved_at') or '—'}"
    )


def print_queue(items: list[dict]) -> None:
    print(f"\nAPPROVED actions: {len(items)}")
    if not items:
        print("Очередь пуста.")
        return
    for i, item in enumerate(items, 1):
        print(f"  {i:>2}. {item_label(item)}")
        if item_url(item):
            print(f"      URL: {item_url(item)}")
        if item.get("reason"):
            print(f"      reason: {item['reason']}")


def choose_action(items: list[dict]) -> dict | None:
    if not items:
        return None
    if len(items) == 1:
        print("\nВ очереди одно действие — выбираю его автоматически.")
        return items[0]
    raw = input("\nВыбери действие [1..N], Enter = выход: ").strip()
    if not raw:
        return None
    try:
        index = int(raw)
    except ValueError:
        print("Нужно ввести номер действия.")
        return None
    if not 1 <= index <= len(items):
        print("Такого номера нет.")
        return None
    return items[index - 1]


def print_preview(item: dict) -> None:
    target = item.get("target") or {}
    print("\n=== PREVIEW ===")
    print(f"action: {item.get('action') or '—'}")
    print(f"target: {target.get('name') or target.get('company') or '—'}")
    print(f"url:    {item_url(item) or '—'}")
    print(f"id:     {item.get('action_id') or '—'}")


def read_saved_page(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-16", "cp1251", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def detect_follow_state(text: str) -> str:
    low = text.lower()

    # Priority is important: a company page can contain sidebar buttons
    # "Отслеживать" even when the main company itself is already followed.
    following_markers = (
        "отслеживаете",
        ">following<",
        'aria-label="following"',
        "unfollow",
    )
    follow_markers = (
        ">отслеживать<",
        "+ отслеживать",
        ">follow<",
        'aria-label="follow"',
    )

    if any(marker in low for marker in following_markers):
        return "FOLLOWING"
    if any(marker in low for marker in follow_markers):
        return "FOLLOW_AVAILABLE"
    return "UNKNOWN"


def save_current_page(root: Path) -> Path | None:
    probe = root / "action_probe.html"
    resources = root / "action_probe_files"

    try:
        if probe.exists():
            probe.unlink()
    except OSError:
        pass

    print("  3. Сохраняю временную копию страницы для проверки состояния...")
    pyautogui.hotkey("ctrl", "s")
    time.sleep(1.5)
    pyautogui.hotkey("ctrl", "a")
    pyautogui.write(str(probe), interval=0.001)
    pyautogui.press("enter")
    time.sleep(4)

    if probe.exists():
        return probe

    # Chrome may alter the extension depending on the selected save type.
    candidates = sorted(
        root.glob("action_probe.*"),
        key=lambda p: p.stat().st_mtime if p.is_file() else 0,
        reverse=True,
    )
    for candidate in candidates:
        if candidate.is_file() and candidate.suffix.lower() in {".html", ".htm", ".mhtml", ".mht"}:
            return candidate
    return None


def safe_probe_follow_company(item: dict, device: dict) -> None:
    url = item_url(item)
    if not url:
        print("PROBE остановлен: у действия нет URL.")
        return

    chrome = device["chrome"]
    if not chrome.exists():
        print(f"PROBE остановлен: Chrome не найден: {chrome}")
        return

    print("\nSAFE PROBE:")
    print("  1. Открываю URL в обычном Chrome.")
    subprocess.Popen([str(chrome), url])
    print("  2. Жду 8 секунд загрузки страницы...")
    time.sleep(8)

    saved = save_current_page(device["root"])
    if saved is None:
        print("  4. STATE = UNKNOWN: временная страница не сохранилась.")
        print("Клик по Follow НЕ выполнялся.")
        return

    try:
        state = detect_follow_state(read_saved_page(saved))
    except Exception as exc:
        print(f"  4. STATE = UNKNOWN: ошибка чтения страницы: {exc}")
        print("Клик по Follow НЕ выполнялся.")
        return

    if state == "FOLLOWING":
        print("  4. STATE = FOLLOWING — компания уже отслеживается. Ничего не нажимаю.")
    elif state == "FOLLOW_AVAILABLE":
        print("  4. STATE = FOLLOW_AVAILABLE — подписка доступна. Останавливаюсь ДО клика.")
    else:
        print("  4. STATE = UNKNOWN — состояние надёжно не определено. Ничего не нажимаю.")

    print(f"     source: {saved.name}")
    print("Клик по Follow НЕ выполнялся.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe LinkedIn approved-action executor prototype")
    parser.add_argument("--device", choices=["1", "2"], help="1=laptop, 2=pc2")
    parser.add_argument("--list", action="store_true", help="Only list approved actions")
    args = parser.parse_args()

    device = choose_device(args.device)
    root = device["root"]
    print(f"\nDevice: {device['name']}")
    print(f"Project root: {root}")
    print("Mode: SAFE PROBE (состояние определяется автоматически; Follow не нажимается)")

    secret = load_secret(root)
    items = approved_items(fetch_queue(secret))
    print_queue(items)
    print("\nOK: GitHub executor -> Cloudflare queue read successfully.")

    if args.list or not items:
        return 0

    selected = choose_action(items)
    if selected is None:
        print("Выход без выполнения.")
        return 0

    print_preview(selected)
    if str(selected.get("action") or "") == "follow_company":
        safe_probe_follow_company(selected, device)
    else:
        print("Для этого типа действия SAFE PROBE пока не включён.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
