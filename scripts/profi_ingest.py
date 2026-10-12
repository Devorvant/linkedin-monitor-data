"""Parse uploaded card HTML in GitHub Actions, then keep reports and history."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import profi_parser as parser
from profi_monitor import date_time, read_json, store_snapshot, write_json


def parse_capture(capture: dict) -> dict:
    if capture.get("schema_version") != 1 or not isinstance(capture.get("html"), str):
        raise ValueError("Unknown capture format")
    html = capture["html"]
    if len(html.encode("utf-8")) > 8_000_000:
        raise ValueError("Capture exceeds the accepted size")
    if hashlib.sha256(html.encode("utf-8")).hexdigest() != capture.get("html_sha256"):
        raise ValueError("Capture HTML checksum does not match")
    ident = str(capture.get("capture_id", ""))
    filename = str(capture.get("source_filename", ""))
    if not re.fullmatch(r"[a-f0-9]{20}", ident) or not filename or "/" in filename or "\\" in filename:
        raise ValueError("Invalid capture identity")
    captured_at = date_time(capture["captured_at"])
    rows = parser.extract_orders(html, filename, captured_at)
    if not rows:
        raise ValueError("No order cards in capture: previous results are preserved")
    return {"schema_version": 1, "parser_version": parser.PARSER_VERSION,
            "parser_location": "github_actions",
            "source": {"filename": filename, "captured_at": captured_at.isoformat(timespec="seconds"),
                       "capture_time_source": capture.get("capture_time_source", "collector"), "capture_id": ident,
                       "parsed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
            "summary": {"orders": len(rows), "remote_orders": sum(x["remote"] for x in rows)},
            "orders": rows}


def ingest(root: Path, force=False):
    archive = root / "profi_inputs"
    state_path = root / "profi_input_state.json"
    state = read_json(state_path, {"inputs": {}})
    fingerprint = hashlib.sha256(Path(parser.__file__).read_bytes() + Path(__file__).read_bytes()).hexdigest()
    changed_parser = state.get("parser_fingerprint") != fingerprint
    parsed = 0
    for path in sorted(archive.glob("*/*.json")):
        relative = path.relative_to(root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if not force and not changed_parser and state["inputs"].get(relative) == digest:
            continue
        report = parse_capture(read_json(path))
        output = root / "history" / "profi_parsed" / report["source"]["captured_at"][:10] / (report["source"]["capture_id"] + ".json")
        write_json(output, report)
        store_snapshot(root, report)
        state["inputs"][relative] = digest
        parsed += 1
    reports = [read_json(path) for path in (root / "history" / "profi_parsed").glob("*/*.json")]
    if reports:
        newest = max(reports, key=lambda r: (r["source"]["captured_at"], r["source"]["capture_id"]))
        current = read_json(root / "profi_latest.json")
        source = (current or {}).get("source") or {}
        current_time = date_time(source.get("captured_at") or source.get("modified_at") or source.get("parsed_at")) if source else None
        if current_time is None or date_time(newest["source"]["captured_at"]) >= current_time:
            write_json(root / "profi_latest.json", newest)
    state.update(parser_fingerprint=fingerprint, parser_version=parser.PARSER_VERSION)
    write_json(state_path, state)
    print(f"Server parser {parser.PARSER_VERSION}: processed {parsed} captures")
    return parsed


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--root", type=Path, default=Path("."))
    args.add_argument("--force", action="store_true")
    options = args.parse_args()
    ingest(options.root.resolve(), options.force)


if __name__ == "__main__":
    main()
