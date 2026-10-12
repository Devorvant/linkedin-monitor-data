"""Prepare a card-only HTML capture for the server, without parsing order fields.

The full MHTML remains local. Standard library only; the uploaded document
contains order cards, not the backoffice header, account controls or assets.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import hashlib
from html import escape
from html.parser import HTMLParser
from pathlib import Path
import re

MSK = timezone(timedelta(hours=3))
ALLOWED = {"a", "div", "span", "h2", "h3", "p", "ul", "ol", "li", "br", "b", "strong", "em", "i"}
BLOCKED = {"script", "style", "iframe", "button", "svg", "form", "object", "template", "noscript"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def decode_html_bytes(payload: bytes, declared_charset: str = "") -> str:
    for encoding in dict.fromkeys(("utf-8-sig", declared_charset, "windows-1251")):
        if encoding:
            try:
                return payload.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                pass
    return payload.decode("utf-8", errors="replace")


def read_main_html(path: Path) -> str:
    raw = path.read_bytes()
    if path.suffix.lower() not in {".mhtml", ".mht"}:
        return decode_html_bytes(raw)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    root = message.get("Snapshot-Content-Location", "")
    parts = []
    for index, part in enumerate(message.walk()):
        if part.get_content_type() == "text/html":
            location = part.get("Content-Location", "")
            html = decode_html_bytes(part.get_payload(decode=True) or b"", part.get_content_charset() or "")
            parts.append(((location == root and bool(root), "profi.ru/backoffice" in location, -index), html))
    if not parts:
        raise ValueError("В MHTML не найдена HTML-страница.")
    return max(parts, key=lambda x: x[0])[1]


def capture_time(path: Path) -> tuple[datetime, str]:
    if path.suffix.lower() in {".mhtml", ".mht"}:
        with path.open("rb") as file:
            message = BytesParser(policy=policy.default).parsebytes(file.read(65536), headersonly=True)
        try:
            value = parsedate_to_datetime(message.get("Date", ""))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(MSK), "mhtml_date"
        except (ValueError, TypeError, IndexError, AttributeError):
            pass
    return datetime.fromtimestamp(path.stat().st_mtime, MSK), "file_mtime"


class OrderCardHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.cards = []
        self.chunk = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if not self.stack:
            match = re.fullmatch(r"(\d+)_order-snippet", values.get("data-testid") or "")
            if tag != "a" or not match:
                return
            values = {"data-testid": match[0], "href": "https://profi.ru/backoffice/n.php?o=" + match[1]}
        blocked = (self.stack[-1][1] if self.stack else False) or tag in BLOCKED
        emitted = tag in ALLOWED and not blocked
        if emitted:
            safe = [(key, value) for key, value in values.items()
                    if key in {"data-testid", "aria-label", "aria-hidden"}
                    or key == "href" and not self.stack]
            self.chunk.append("<" + tag + "".join(" " + key + '="' + escape(value or "", quote=True) + '"' for key, value in safe) + ">")
        if tag not in VOID:
            self.stack.append((tag, blocked, emitted))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.stack:
            return
        found = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if found is None:
            return
        for name, _, emitted in reversed(self.stack[found:]):
            if emitted:
                self.chunk.append("</" + name + ">")
        del self.stack[found:]
        if not self.stack:
            self.cards.append("".join(self.chunk))
            self.chunk = []

    def handle_data(self, data):
        if self.stack and not self.stack[-1][1]:
            self.chunk.append(escape(data, quote=False))

    def handle_entityref(self, name):
        if self.stack and not self.stack[-1][1]:
            self.chunk.append("&" + name + ";")

    def handle_charref(self, name):
        if self.stack and not self.stack[-1][1]:
            self.chunk.append("&#" + name + ";")


def card_document(html: str) -> str:
    parser = OrderCardHTML()
    parser.feed(html)
    parser.close()
    if not parser.cards or parser.stack:
        raise ValueError("Не удалось выделить законченные карточки заказов; данные не отправлены.")
    return '<!doctype html><html lang="ru"><meta charset="utf-8"><body>' + "\n".join(parser.cards) + "</body></html>"


def prepare_capture(path: Path) -> dict:
    html = card_document(read_main_html(path))
    captured, time_source = capture_time(path)
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    ident = hashlib.sha256((path.name + "|" + captured.isoformat()).encode()).hexdigest()[:20]
    return {"schema_version": 1, "capture_id": ident,
            "captured_at": captured.isoformat(timespec="seconds"),
            "capture_time_source": time_source,
            "source_filename": path.name, "html_sha256": digest,
            "html": html, "transport_version": "1"}
