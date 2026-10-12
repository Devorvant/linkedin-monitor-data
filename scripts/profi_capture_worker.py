"""Windows capture worker: save, rename, close Chrome and upload card HTML.
Order fields are parsed by GitHub Actions, not by this worker.
"""
from __future__ import annotations
import argparse
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
import profi_capture_transport as transport

try:
    import pyautogui
except ImportError:
    pyautogui = None

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name.lower() == "cache" else SCRIPT_DIR
SAVED_PAGES_DIR = ROOT / "saved_pages"
REPORTS_DIR = ROOT / "reports"
GITHUB_OWNER = "Devorvant"
GITHUB_REPO = "linkedin-monitor-data"
GITHUB_BRANCH = "main"
PUBLIC_CAPTURE_UPLOAD_ENABLED = False
GITHUB_TOKEN_FILES = (ROOT / "github_token.txt", REPORTS_DIR / "github_token.txt")


def looks_like_profi_file(path: Path) -> bool:
    try:
        return "_order-snippet" in transport.read_main_html(path)
    except (OSError, ValueError):
        return False


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


def upload_capture(path: Path) -> str:
    if not PUBLIC_CAPTURE_UPLOAD_ENABLED:
        raise RuntimeError("Передача карточек в публичный GitHub отключена до подтверждения владельцем.")
    capture = transport.prepare_capture(path)
    remote = f"profi_inputs/{capture['captured_at'][:10]}/{capture['capture_id']}.json"
    api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{remote}"
    content = base64.b64encode(json.dumps(capture, ensure_ascii=False).encode("utf-8")).decode("ascii")
    for attempt in range(3):
        sha = None
        try:
            current = github_api_request(api_url + f"?ref={GITHUB_BRANCH}")
            # SHA is required when sending a previously saved capture again.
            sha = current.get("sha")
        except RuntimeError as exc:
            if "GitHub API error 404:" not in str(exc):
                raise
        body = {"message": "Upload Profi order cards for server parsing",
                "content": content, "branch": GITHUB_BRANCH}
        if sha:
            body["sha"] = sha
        try:
            github_api_request(api_url, method="PUT", data=body)
            print(f"Карточки отправлены: {remote}")
            print("Парсинг, история и метрики выполняются в GitHub Actions.")
            print("Результат: https://devorvant.ru/profi-monitor.html")
            return remote
        except RuntimeError as exc:
            if "GitHub API error 409:" not in str(exc) or attempt == 2:
                raise
            time.sleep(1 + attempt)
    raise RuntimeError("Не удалось отправить сохранённые карточки.")


def latest_saved_page() -> Path:
    files = [p for folder in (SAVED_PAGES_DIR, ROOT) if folder.exists()
             for p in folder.iterdir() if p.is_file()
             and p.suffix.lower() in {".mhtml", ".mht", ".html", ".htm"}
             and p.name.lower().startswith(("profi_orders_", "заказы"))]
    if not files:
        raise RuntimeError("Не найден сохранённый файл Profi.ru.")
    return max(files, key=lambda p: p.stat().st_mtime)


def main():
    global ROOT, SAVED_PAGES_DIR, REPORTS_DIR, GITHUB_TOKEN_FILES
    global SCROLL_X_RATIO, SAVE_MENU_X_RATIO, SAVE_MENU_Y_OFFSET_RATIO
    global CLOSE_X_RATIO, CLOSE_Y_RATIO
    parser = argparse.ArgumentParser(description="Profi.ru -> HTML карточек -> серверный парсер GitHub")
    parser.add_argument("file", nargs="?", help="Сохранённый MHTML/HTML для --upload-only.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("-a", "--auto", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--upload-only", "--parse-only", action="store_true",
                        help="Отправить сохранённый файл без открытия Chrome; парсинг на GitHub.")
    for option, default in (("scroll-x", SCROLL_X_RATIO), ("save-menu-x", SAVE_MENU_X_RATIO),
                            ("save-menu-offset", SAVE_MENU_Y_OFFSET_RATIO),
                            ("close-x", CLOSE_X_RATIO), ("close-y", CLOSE_Y_RATIO)):
        parser.add_argument("--" + option, type=float, default=default)
    args = parser.parse_args()
    for option in ("scroll_x", "save_menu_x", "save_menu_offset", "close_x", "close_y"):
        if not 0 < getattr(args, option) < 1:
            parser.error(option + ": требуется число больше 0 и меньше 1.")
    if 0.46 + args.save_menu_offset >= 1:
        parser.error("Точка сохранения выходит за нижнюю границу экрана.")
    ROOT = args.root.expanduser().resolve()
    SAVED_PAGES_DIR, REPORTS_DIR = ROOT / "saved_pages", ROOT / "reports"
    GITHUB_TOKEN_FILES = (ROOT / "github_token.txt", REPORTS_DIR / "github_token.txt")
    SCROLL_X_RATIO, SAVE_MENU_X_RATIO = args.scroll_x, args.save_menu_x
    SAVE_MENU_Y_OFFSET_RATIO = args.save_menu_offset
    CLOSE_X_RATIO, CLOSE_Y_RATIO = args.close_x, args.close_y
    if not PUBLIC_CAPTURE_UPLOAD_ENABLED:
        raise RuntimeError("Передача карточек в публичный GitHub пока отключена. Окна не изменены.")
    get_github_token()  # Check existing credentials before moving the mouse.
    if args.upload_only:
        source = Path(args.file).expanduser().resolve() if args.file else latest_saved_page()
    else:
        if pyautogui is None:
            raise RuntimeError("Не установлен pyautogui. Установите: pip install pyautogui")
        source = capture_profi_page()
        close_profi_window_by_mouse()
    upload_capture(source)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("ОШИБКА: " + str(error), file=sys.stderr)
        raise SystemExit(1)


