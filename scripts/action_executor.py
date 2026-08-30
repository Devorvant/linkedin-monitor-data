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
    req = Request(ACTION_API, headers={"User-Agent": "linkedin-monitor-action-executor", "X-Approval-Key": secret}, method="GET")
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
    return [x for x in items if isinstance(x, dict) and str(x.get("state", "APPROVED")).upper() == "APPROVED"]


def item_url(item: dict) -> str:
    return str(item.get("profile_url") or item.get("source_url") or "").strip()


def item_label(item: dict) -> str:
    target = item.get("target") or {}
    name = target.get("name") or target.get("company") or "—"
    return f"{name} | {item.get('action') or '—'} | source={item.get('source_date') or '—'} | approved={item.get('approved_at') or '—'}"


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


def safe_probe_follow_company(item: dict, device: dict) -> None:
    url = item_url(item)
    if not url:
        print("PROBE остановлен: у действия нет URL.")
        return
    chrome = device["chrome"]
    if not chrome.exists():
        print(f"PROBE остановлен: Chrome не найден: {chrome}")
        return

    print("\nSAFE PROBE до пункта 4:")
    print("  1. Открываем URL в обычном Chrome.")
    subprocess.Popen([str(chrome), url])
    print("  2. Ждём 8 секунд загрузки страницы...")
    time.sleep(8)
    print("  3. Проверь состояние кнопки на странице.")
    print("     1 = Following / Уже подписан")
    print("     2 = Follow / Можно подписаться")
    print("     Enter = не удалось определить")
    state = input("Состояние: ").strip()
    if state == "1":
        print("  4. Уже Following -> НИЧЕГО не нажимаем. SAFE DONE.")
    elif state == "2":
        print("  4. Видна Follow -> останавливаемся ДО клика. SAFE DONE.")
    else:
        print("  4. Состояние не подтверждено -> останавливаемся. SAFE DONE.")
    print("Никаких кликов по Follow executor не выполнял.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe LinkedIn approved-action executor prototype")
    parser.add_argument("--device", choices=["1", "2"], help="1=laptop, 2=pc2")
    parser.add_argument("--list", action="store_true", help="Only list approved actions")
    args = parser.parse_args()

    device = choose_device(args.device)
    root = device["root"]
    print(f"\nDevice: {device['name']}")
    print(f"Project root: {root}")
    print("Mode: SAFE PROBE (до проверки состояния; Follow не нажимается)")

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
        go = input("\nОткрыть Chrome и выполнить SAFE PROBE до пункта 4? [y/N]: ").strip().lower()
        if go in {"y", "yes", "д", "да"}:
            safe_probe_follow_company(selected, device)
        else:
            print("PROBE отменён. Никаких действий.")
    else:
        print("Для этого типа действия SAFE PROBE пока не включён.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
