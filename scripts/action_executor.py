from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pyautogui

ACTION_API = "https://linkedin-actions.devorvant.workers.dev/actions"
PAGE_EXTENSIONS = {".mhtml", ".mht", ".html", ".htm"}

DEVICE_CONFIGS = {
    "1": {
        "name": "laptop",
        "root": Path(r"C:\Users\kusc\AppData\Local\Programs\Python\Python311\linkedin_desktop_automation"),
        "chrome": Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        "save_right_x": 0.88,
        "save_right_y": 0.46,
        "save_menu_x": 0.74,
        "save_menu_y_offset": 0.155,
        "close_tab_x": 0.343,
        "close_tab_y": 0.022,
        "follow_button_x": 0.151,
        "follow_button_y": 0.451,
    },
    "2": {
        "name": "pc2",
        "root": Path(r"H:\Users\kusc\AppData\Local\Programs\Python\Python311\linkedin_desktop_automation"),
        "chrome": Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        "save_right_x": 0.938,
        "save_right_y": 0.488,
        "save_menu_x": 0.798,
        "save_menu_y_offset": 0.130,
        "close_tab_x": 0.200,
        "close_tab_y": 0.020,
        "follow_button_x": 0.151,
        "follow_button_y": 0.451,
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


def focus_linkedin_chrome(maximize: bool = True) -> bool:
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    found: list[int] = []
    wndenumproc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @wndenumproc
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.lower()
        if "linkedin" in title and "chrome" in title:
            found.append(hwnd)
        return True

    user32.EnumWindows(enum_proc, 0)
    if not found:
        return False
    hwnd = found[-1]
    user32.ShowWindow(hwnd, 9)
    time.sleep(0.3)
    if maximize:
        user32.ShowWindow(hwnd, 3)
        time.sleep(0.5)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.8)
    return True


def snapshot_saved_pages(folder: Path) -> dict[str, tuple[float, int]]:
    result: dict[str, tuple[float, int]] = {}
    folder.mkdir(parents=True, exist_ok=True)
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.lower() not in PAGE_EXTENSIONS:
            continue
        try:
            stat = path.stat()
            result[str(path.resolve())] = (stat.st_mtime, stat.st_size)
        except OSError:
            pass
    return result


def wait_for_saved_page_change(folder: Path, before: dict[str, tuple[float, int]], timeout: float = 20.0) -> Path | None:
    deadline = time.time() + timeout
    candidate: Path | None = None
    while time.time() < deadline:
        changed: list[tuple[float, int, Path]] = []
        for path in folder.iterdir():
            if not path.is_file() or path.suffix.lower() not in PAGE_EXTENSIONS:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            old = before.get(str(path.resolve()))
            if old is None or old != (stat.st_mtime, stat.st_size):
                changed.append((stat.st_mtime, stat.st_size, path))
        if changed:
            changed.sort(reverse=True, key=lambda x: (x[0], x[1]))
            newest = changed[0][2]
            try:
                size1 = newest.stat().st_size
                time.sleep(0.8)
                size2 = newest.stat().st_size
                if size1 == size2 and size2 > 0:
                    return newest
                candidate = newest
            except OSError:
                pass
        time.sleep(0.5)
    return candidate


def save_page_with_context_menu(device: dict, step_label: str = "3") -> Path | None:
    saved_pages = device["root"] / "saved_pages"
    before = snapshot_saved_pages(saved_pages)
    width, height = pyautogui.size()
    right_x = int(width * device["save_right_x"])
    right_y = int(height * device["save_right_y"])
    menu_x = int(width * device["save_menu_x"])
    menu_y = int(right_y + height * device["save_menu_y_offset"])
    print(f"  {step_label}. Правый клик -> Сохранить как...")
    pyautogui.moveTo(right_x, right_y, duration=0.35)
    pyautogui.rightClick()
    time.sleep(0.7)
    pyautogui.moveTo(menu_x, menu_y, duration=0.25)
    pyautogui.click()
    time.sleep(0.7)
    pyautogui.press("enter")
    print("     Жду появления нового файла в saved_pages...")
    return wait_for_saved_page_change(saved_pages, before)


def read_html_or_mhtml(path: Path) -> str:
    if path.suffix.lower() in {".mhtml", ".mht"}:
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        chunks: list[str] = []
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() not in {"text/html", "text/plain"}:
                    continue
                try:
                    content = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    content = payload.decode(charset, errors="replace")
                if isinstance(content, str):
                    chunks.append(content)
        else:
            content = message.get_content()
            if isinstance(content, str):
                chunks.append(content)
        return "\n".join(chunks)
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-16", "cp1251", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def detect_follow_state(text: str) -> str:
    low = text.lower()
    following_markers = (
        "отслеживаете эту страницу",
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


def click_main_follow_button(device: dict) -> None:
    width, height = pyautogui.size()
    x = int(width * device["follow_button_x"])
    y = int(height * device["follow_button_y"])
    print(f"  5. Нажимаю основную кнопку Follow один раз: ({x}, {y})")
    pyautogui.moveTo(x, y, duration=0.4)
    pyautogui.click()
    time.sleep(4)


def close_opened_tab(device: dict) -> None:
    width, height = pyautogui.size()
    x = int(width * device["close_tab_x"])
    y = int(height * device["close_tab_y"])
    print("  8. Закрываю открытую вкладку LinkedIn.")
    pyautogui.moveTo(x, y, duration=0.3)
    pyautogui.click()
    time.sleep(0.8)


def execute_follow_company(item: dict, device: dict) -> None:
    url = item_url(item)
    if not url:
        print("EXECUTOR остановлен: у действия нет URL.")
        return
    chrome = device["chrome"]
    if not chrome.exists():
        print(f"EXECUTOR остановлен: Chrome не найден: {chrome}")
        return

    print("\nFOLLOW_COMPANY EXECUTION:")
    print("  1. Открываю URL в обычном Chrome.")
    subprocess.Popen([str(chrome), url])
    print("  2. Жду 8 секунд и разворачиваю Chrome на полный экран.")
    time.sleep(8)
    focus_linkedin_chrome(maximize=True)

    try:
        saved = save_page_with_context_menu(device, "3")
        if saved is None:
            print("  4. STATE = UNKNOWN: новый файл не найден. Ничего не нажимаю.")
            return
        state = detect_follow_state(read_html_or_mhtml(saved))
        print(f"  4. STATE = {state}")
        print(f"     source: {saved.name}")

        if state == "FOLLOWING":
            print("     Уже подписаны -> SKIP.")
            return
        if state != "FOLLOW_AVAILABLE":
            print("     Состояние не подтверждено -> никаких кликов.")
            return

        click_main_follow_button(device)
        focus_linkedin_chrome(maximize=True)
        print("  6. Повторно сохраняю страницу для проверки результата.")
        saved_after = save_page_with_context_menu(device, "6")
        if saved_after is None:
            print("  7. VERIFY = UNKNOWN: повторный файл не найден.")
            return
        state_after = detect_follow_state(read_html_or_mhtml(saved_after))
        print(f"  7. VERIFY = {state_after}")
        print(f"     source: {saved_after.name}")
        if state_after == "FOLLOWING":
            print("     SUCCESS: компания теперь отслеживается.")
        else:
            print("     FAILED/UNCERTAIN: FOLLOWING после клика не подтверждён.")
    finally:
        try:
            focus_linkedin_chrome(maximize=True)
            close_opened_tab(device)
        except Exception as exc:
            print(f"     Не удалось закрыть вкладку автоматически: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="LinkedIn approved-action executor")
    parser.add_argument("--device", choices=["1", "2"], help="1=laptop, 2=pc2")
    parser.add_argument("--list", action="store_true", help="Only list approved actions")
    args = parser.parse_args()

    device = choose_device(args.device)
    root = device["root"]
    print(f"\nDevice: {device['name']}")
    print(f"Project root: {root}")
    print("Mode: EXECUTE follow_company only; UNKNOWN never clicks")

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
        execute_follow_company(selected, device)
    else:
        print("Реальное выполнение для этого типа действия пока не включено.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
