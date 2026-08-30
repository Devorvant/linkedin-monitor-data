from __future__ import annotations

import argparse
import json
import os
import sys
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

    candidates = [
        root / "approval_secret.txt",
        Path(__file__).resolve().parent / "approval_secret.txt",
    ]
    for path in candidates:
        if path.exists():
            secret = path.read_text(encoding="utf-8").strip()
            if secret:
                return secret

    raise SystemExit(
        "APPROVAL_SECRET не найден. Создай локальный approval_secret.txt в корне проекта "
        "или задай переменную окружения APPROVAL_SECRET. Не добавляй этот файл в GitHub."
    )


def fetch_queue(secret: str) -> dict:
    req = Request(
        ACTION_API,
        headers={
            "User-Agent": "linkedin-monitor-action-executor",
            "X-Approval-Key": secret,
        },
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
    return [x for x in items if isinstance(x, dict) and str(x.get("state", "APPROVED")).upper() == "APPROVED"]


def item_url(item: dict) -> str:
    return str(item.get("profile_url") or item.get("source_url") or "").strip()


def item_label(item: dict) -> str:
    target = item.get("target") or {}
    name = target.get("name") or target.get("company") or "—"
    action = item.get("action") or "—"
    source_date = item.get("source_date") or "—"
    approved_at = item.get("approved_at") or "—"
    return f"{name} | {action} | source={source_date} | approved={approved_at}"


def print_queue(items: list[dict]) -> None:
    print(f"\nAPPROVED actions: {len(items)}")
    if not items:
        print("Очередь пуста.")
        return
    for i, item in enumerate(items, 1):
        print(f"  {i:>2}. {item_label(item)}")
        url = item_url(item)
        if url:
            print(f"      URL: {url}")
        reason = item.get("reason")
        if reason:
            print(f"      reason: {reason}")


def choose_action(items: list[dict]) -> dict | None:
    if not items:
        return None
    raw = input("\nВыбери действие для PREVIEW [1..N], Enter = выход: ").strip()
    if not raw:
        return None
    try:
        index = int(raw)
    except ValueError:
        print("Нужно ввести номер действия.")
        return None
    if index < 1 or index > len(items):
        print("Такого номера нет.")
        return None
    return items[index - 1]


def print_preview(item: dict) -> None:
    action = str(item.get("action") or "—")
    url = item_url(item) or "—"
    target = item.get("target") or {}
    target_name = target.get("name") or target.get("company") or "—"

    print("\n=== PREVIEW ===")
    print(f"action: {action}")
    print(f"target: {target_name}")
    print(f"url:    {url}")
    print(f"id:     {item.get('action_id') or '—'}")

    print("\nПлан выполнения:")
    if action == "follow_company":
        print("  1. Открыть URL в обычном Chrome.")
        print("  2. Дождаться загрузки страницы компании.")
        print("  3. Проверить состояние Follow / Following.")
        print("  4. Если уже Following — ничего не нажимать.")
        print("  5. Если доступно Follow — нажать один раз.")
        print("  6. Зафиксировать результат и завершить действие.")
    elif action == "follow_person":
        print("  1. Открыть профиль в обычном Chrome.")
        print("  2. Проверить, подписаны ли уже на человека.")
        print("  3. Если уже подписаны — ничего не делать.")
        print("  4. Иначе выполнить Follow один раз.")
        print("  5. Зафиксировать результат.")
    elif action == "engage_with_post":
        print("  1. Открыть URL публикации.")
        print("  2. Найти целевую публикацию.")
        print("  3. Пока только определить доступные действия; без кликов.")
    else:
        print("  1. Открыть URL действия.")
        print("  2. Проверить текущую страницу и состояние цели.")
        print("  3. Реальное выполнение для этого action пока НЕ включено.")

    print("\nDRY-RUN: Chrome не открывается, мышь и клавиатура не используются.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe LinkedIn approved-action executor prototype")
    parser.add_argument("--device", choices=["1", "2"], help="1=laptop, 2=pc2")
    parser.add_argument("--list", action="store_true", help="Only list approved actions")
    args = parser.parse_args()

    device = choose_device(args.device)
    root = device["root"]

    print(f"\nDevice: {device['name']}")
    print(f"Project root: {root}")
    print("Mode: DRY-RUN (никаких действий в LinkedIn не выполняется)")

    secret = load_secret(root)
    payload = fetch_queue(secret)
    items = approved_items(payload)
    print_queue(items)

    print("\nOK: GitHub executor -> Cloudflare queue read successfully.")

    if args.list or not items:
        return 0

    selected = choose_action(items)
    if selected is None:
        print("Выход без выполнения.")
        return 0

    print_preview(selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
