"""Build Profi.ru snapshots, observed-order history and dashboard metrics.

Standard library only. Reprocessing one capture updates its snapshot rather than
counting another scan. Git backfill preserves uploaded captures between CI runs.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

MSK = timezone(timedelta(hours=3))
FIELDS = ("order_id", "title", "description", "remote", "location", "schedule",
          "client", "posted", "posted_at", "posted_at_precision", "budget",
          "budget_min_rub", "budget_max_rub", "budget_kind", "budget_unit",
          "budget_unit_source", "card_source", "badges", "parser_warnings")
MATCH_RULES = (
    ("controls", "Управление и моделирование", "high", (
        r"\bmatlab\b", r"\bsimulink\b", r"автопилот", r"динамик\w* пол[её]т",
        r"систем\w* управлен", r"теори\w* управлен", r"виброизол", r"вибростенд",
        r"стабилизац", r"\bgnc\b", r"flight control", r"математическ\w* модел",
        r"имитационн\w* модел", r"оптимизац\w* траектор")),
    ("embedded", "Электроника и робототехника", "high", (
        r"\bstm32\b", r"\bembedded\b", r"микроконтроллер", r"робототех",
        r"\bpx4\b", r"\bardupilot\b", r"\buav\b", r"бпла", r"беспилот",
        r"мехатрон", r"\bpid\b")),
    ("engineering", "Инженерные задачи", "medium", (
        r"конструктор", r"инженер", r"solidworks", r"autocad", r"компас",
        r"чертеж", r"чертёж", r"механик", r"расч[её]т\w* прочност", r"аэродинам")),
    ("ai_data", "AI и обработка данных", "medium", (
        r"\bpython\b", r"компьютерн\w* зрени", r"машинн\w* обучен",
        r"нейросет", r"обработк\w* данн", r"анализ\w* данн", r"\bcuda\b")),
)
TEACHING = re.compile(r"\b(?:[1-9]|1[01])\s+класс\b|подготовка к экзамен|репетитор|повышение успеваемости|\bкурс\b.*\bвуз", re.I)


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def date_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=MSK) if parsed.tzinfo is None else parsed).astimezone(MSK)


def classify(row: dict) -> dict:
    title = str(row.get("title") or "")
    body = title + " " + str(row.get("description") or "")
    matches = []
    # Rules describe technical overlap, not a probability of getting an order.
    for code, label, level, patterns in MATCH_RULES:
        terms = [m.group(0) for p in patterns if (m := re.search(p, body, re.I))]
        if terms:
            matches.append((code, label, level, list(dict.fromkeys(terms))))
    if matches:
        chosen = next((m for m in matches if m[2] == "high"), matches[0])
        code, label, level, terms = chosen
        if TEACHING.search(body):
            level = "medium"
        return {"category": code, "category_label": label, "profile_fit": level,
                "fit_label": "Близко к профилю" if level == "high" else "Смежная задача",
                "match_terms": terms,
                "fit_reason": "Совпадения по ключевым словам: " + ", ".join(terms)}
    if TEACHING.search(body) or re.search(r"математик|физик|репетитор", title, re.I):
        code, label = "education", "Обучение"
    elif re.search(r"сайт|лендинг|веб|frontend|backend|разработк\w* игр|tilda|flutter", body, re.I):
        code, label = "software", "Сайты и приложения"
    else:
        code, label = "other", "Другие задачи"
    return {"category": code, "category_label": label, "profile_fit": "other",
            "fit_label": "Другие задачи", "match_terms": [],
            "fit_reason": "Прямые совпадения с инженерным профилем не найдены."}


def snapshot_from(raw: dict) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("orders"), list):
        raise ValueError("Expected an orders array in Profi report")
    source = raw.get("source") or {}
    captured = date_time(source.get("captured_at") or source.get("modified_at") or source.get("parsed_at"))
    parser = str(raw.get("parser_version") or "legacy")
    filename = str(source.get("filename") or "")
    # The collector's timestamped filename is stable when an older capture is
    # reparsed with a more accurate MHTML date.
    key = filename if re.match(r"^profi_orders_\d{4}-\d{2}-\d{2}_", filename) else captured.isoformat() + "|" + filename
    scan_id = hashlib.sha256(key.encode()).hexdigest()[:16]
    rows = {}
    for item in raw["orders"]:
        if not isinstance(item, dict):
            continue
        ident = str(item.get("order_id") or "")
        if not re.fullmatch(r"\d+", ident):
            continue
        row = {key: item.get(key) for key in FIELDS}
        row.update(order_id=ident, remote=item.get("remote") is True,
                   url=f"https://profi.ru/backoffice/n.php?o={ident}",
                   parser_warnings=item.get("parser_warnings") or [],
                   badges=item.get("badges") or [])
        row.update(classify(row))
        row["parser_version"] = parser
        row["capture_day"] = captured.date().isoformat()
        rows[ident] = row
    if not rows:
        raise ValueError("No valid orders: keep existing monitor data")
    return {"schema_version": 1, "scan_id": scan_id,
            "captured_at": captured.isoformat(timespec="seconds"),
            "date": captured.date().isoformat(), "source_filename": filename,
            "parser_version": parser, "orders": list(rows.values())}


def store_snapshot(root: Path, raw: dict):
    snapshot = snapshot_from(raw)
    path = root / "history" / "profi" / snapshot["date"] / (snapshot["scan_id"] + ".json")
    write_json(path, snapshot)
    for previous in (root / "history" / "profi").glob("*/" + snapshot["scan_id"] + ".json"):
        if previous != path:
            previous.unlink()


def metrics(rows: list[dict]) -> dict:
    return {"orders": len(rows), "remote_orders": sum(x.get("remote") is True for x in rows),
            "with_budget": sum(bool(x.get("budget")) for x in rows),
            "profile_relevant": sum(x.get("profile_fit") in {"high", "medium"} for x in rows),
            "high_fit": sum(x.get("profile_fit") == "high" for x in rows),
            "categories": dict(Counter(x["category"] for x in rows))}


def build_outputs(root: Path, processed_commits: list[str]) -> dict:
    snapshots = [read_json(p) for p in (root / "history" / "profi").glob("*/*.json")]
    snapshots.sort(key=lambda s: (s["captured_at"], s["scan_id"]))
    if not snapshots:
        raise ValueError("No saved captures to build monitor")
    history = {}
    daily_rows = defaultdict(dict)
    daily_new = Counter()
    daily_scans = Counter()
    runs = []
    for snapshot in snapshots:
        day = snapshot["date"]
        daily_scans[day] += 1
        new_ids = []
        for item in snapshot["orders"]:
            row = dict(item)
            row.update(classify(row))
            ident = row["order_id"]
            previous = history.get(ident)
            if previous is None:
                new_ids.append(ident)
                daily_new[day] += 1
            history[ident] = {**row,
                "first_seen": previous["first_seen"] if previous else snapshot["captured_at"],
                "last_seen": snapshot["captured_at"],
                "times_seen": previous["times_seen"] + 1 if previous else 1,
                "observed_dates": sorted(set((previous or {}).get("observed_dates", [])) | {day}),
                "is_current": False}
            daily_rows[day][ident] = history[ident]
        runs.append({"scan_id": snapshot["scan_id"], "date": day,
                     "captured_at": snapshot["captured_at"], "source_filename": snapshot["source_filename"],
                     "parser_version": snapshot["parser_version"], "new_observed": len(new_ids),
                     "new_ids": new_ids, **metrics(snapshot["orders"])})
    latest = snapshots[-1]
    current_ids = {x["order_id"] for x in latest["orders"]}
    for ident in current_ids:
        history[ident]["is_current"] = True
    current = [history[x["order_id"]] for x in latest["orders"]]
    # Not observed in the last scan does not prove that a customer closed it.
    days = [{"date": day, "scans": daily_scans[day], "new_observed": daily_new[day],
             **metrics(list(daily_rows[day].values()))} for day in sorted(daily_rows)]
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary = {**metrics(current), "new_observed": runs[-1]["new_observed"],
               "unique_observed": len(history), "scans": len(snapshots),
               "first_capture": snapshots[0]["captured_at"], "last_capture": latest["captured_at"]}
    warning = None if latest["parser_version"] != "legacy" else (
        "Последний сбор сделан старым парсером. Запустите v4_4: он точнее разбирает цены и карточки карусели."
    )
    status = {"schema_version": 1, "generated_at": generated, "status": "OK",
              "captured_at": latest["captured_at"], "parser_version": latest["parser_version"],
              "summary": summary, "warning": warning,
              "daily": days, "runs": list(reversed(runs)),
              "scope_note": "Показаны сохранённые карточки, а не все заказы площадки. «Впервые замечен» — первое попадание в наши сборы."}
    write_json(root / "profi_history.json", {"schema_version": 1, "generated_at": generated,
               "processed_source_commits": processed_commits, "orders": list(history.values())})
    write_json(root / "profi_current.json", {"schema_version": 1, "generated_at": generated,
               "captured_at": latest["captured_at"], "parser_version": latest["parser_version"],
               "summary": summary, "new_ids": runs[-1]["new_ids"], "orders": current})
    write_json(root / "profi_pipeline_status.json", status)
    return status


def git_backfill(root: Path, processed: list[str]) -> list[str]:
    result = subprocess.run(["git", "log", "--reverse", "--format=%H", "--", "profi_latest.json"],
                            cwd=root, text=True, capture_output=True, check=True)
    known = set(processed)
    for commit in result.stdout.splitlines():
        if commit in known:
            continue
        content = subprocess.run(["git", "show", commit + ":profi_latest.json"],
                                 cwd=root, text=True, capture_output=True)
        if content.returncode:
            continue
        try:
            store_snapshot(root, json.loads(content.stdout))
        except (ValueError, TypeError, KeyError) as exc:
            print(f"Skipping invalid older capture {commit[:8]}: {exc}")
        processed.append(commit)
        known.add(commit)
    return processed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--git-backfill", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    raw = read_json(root / "profi_latest.json")
    snapshot_from(raw)  # Validate latest input before any write.
    old = read_json(root / "profi_history.json", {})
    processed = list(old.get("processed_source_commits", []))
    if args.git_backfill:
        processed = git_backfill(root, processed)
    store_snapshot(root, raw)
    status = build_outputs(root, processed)
    print(json.dumps(status["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
