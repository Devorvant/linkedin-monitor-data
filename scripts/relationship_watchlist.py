#!/usr/bin/env python3
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ["WATCH", "REVIEW", "CONNECT", "CONNECTED", "CONTACTED", "REPLIED", "CLOSED"]
MANUAL_FIELDS = ["status", "notes", "tags", "do_not_contact", "last_contacted_at", "next_follow_up_at"]


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def norm(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def entity_id(kind, name):
    return f"{kind}:{norm(name).casefold()}"


def as_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def score_for(signal):
    base = 85 if signal.get("priority") == "high" else 65
    confidence = as_int(signal.get("confidence"))
    career = as_int(signal.get("career_relevance"))
    networking = as_int(signal.get("networking_relevance"))
    technical = as_int(signal.get("technical_relevance"))
    return min(100, round(base * 0.35 + confidence * 0.25 + career * 0.15 + networking * 0.15 + technical * 0.10))


def recommended_status(score, kind, signal):
    if score >= 85:
        return "REVIEW"
    if kind == "person" and score >= 72 and "connect_person" in (signal.get("recommended_actions") or []):
        return "REVIEW"
    return "WATCH"


def suggested_action(signal, kind, status, do_not_contact):
    if status == "CLOSED":
        return "no_action"
    if do_not_contact and kind == "person":
        return "watch"
    actions = signal.get("recommended_actions") or []
    if kind == "person":
        preferred = ["connect_person", "follow_person", "engage_with_post", "watch"]
        if status in {"CONNECTED", "CONTACTED", "REPLIED"}:
            preferred = ["engage_with_post", "follow_person", "watch"]
    else:
        preferred = ["check_jobs", "follow_company", "engage_with_post", "watch"]
    for action in preferred:
        if action in actions:
            return action
    return "watch"


def preserve_manual(old):
    return {
        "status": old.get("status") if old.get("status") in STATUSES else "WATCH",
        "notes": old.get("notes", ""),
        "tags": list(old.get("tags") or []),
        "do_not_contact": bool(old.get("do_not_contact", False)),
        "last_contacted_at": old.get("last_contacted_at"),
        "next_follow_up_at": old.get("next_follow_up_at"),
    }


def upsert(records, kind, name, signal, now):
    name = norm(name)
    if not name:
        return
    rid = entity_id(kind, name)
    old = records.get(rid, {})
    manual = preserve_manual(old)

    source_ids = list(old.get("source_signal_ids") or [])
    sid = signal.get("signal_id")
    is_new_signal = bool(sid and sid not in source_ids)
    if sid and is_new_signal:
        source_ids.append(sid)

    reasons = list(old.get("reasons") or [])
    reason = norm(signal.get("why_relevant"))
    if reason and (is_new_signal or not sid) and reason not in reasons:
        reasons.append(reason)
    reasons = reasons[-5:]

    companies = list(old.get("companies") or [])
    company = norm(signal.get("company"))
    if company and company not in companies:
        companies.append(company)

    roles = list(old.get("roles") or [])
    for person in signal.get("people") or []:
        if norm(person.get("name")).casefold() == name.casefold():
            role = norm(person.get("role"))
            if role and role not in roles:
                roles.append(role)

    score = score_for(signal)
    best_score = max(as_int(old.get("score")), score)

    if "priority_counts" in old:
        priority_counts = dict(old.get("priority_counts") or {"high": 0, "medium": 0})
    else:
        priority_counts = {"high": 0, "medium": 0}
        legacy_priority = old.get("latest_priority")
        if old.get("source_signal_ids") and legacy_priority in priority_counts:
            priority_counts[legacy_priority] = max(1, as_int(old.get("times_seen")))

    if is_new_signal:
        p = signal.get("priority")
        if p in {"high", "medium"}:
            priority_counts[p] = as_int(priority_counts.get(p)) + 1

    status_history = list(old.get("status_history") or [])
    automation = dict(old.get("automation") or {})
    previously_observed = automation.get("last_observed_status")
    if previously_observed and manual["status"] != previously_observed:
        status_history.append({"status": manual["status"], "at": now, "source": "manual_edit_detected"})
    elif not status_history:
        status_history.append({"status": manual["status"], "at": old.get("first_seen", now), "source": "created"})
    status_history = status_history[-20:]

    first_seen = old.get("first_seen", now)
    last_seen = now if is_new_signal or not old else old.get("last_seen", first_seen)
    times_seen = as_int(old.get("times_seen")) + (1 if is_new_signal or (not old and not sid) else 0)
    if not old and sid:
        times_seen = 1

    automation.update({
        "last_observed_status": manual["status"],
        "recommended_status": recommended_status(best_score, kind, signal),
        "last_processed_at": now,
    })

    records[rid] = {
        "id": rid,
        "kind": kind,
        "name": name,
        **manual,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "times_seen": times_seen,
        "score": best_score,
        "latest_priority": signal.get("priority"),
        "latest_confidence": signal.get("confidence"),
        "priority_counts": priority_counts,
        "suggested_action": suggested_action(signal, kind, manual["status"], manual["do_not_contact"]),
        "companies": companies,
        "roles": roles,
        "source_signal_ids": source_ids[-50:],
        "reasons": reasons,
        "latest_source_url": signal.get("source_url"),
        "status_history": status_history,
        "automation": automation,
        "updated_at": now,
    }


def build(signals, existing):
    now = datetime.now(timezone.utc).isoformat()
    records = {r["id"]: r for r in existing.get("items", []) if r.get("id")}

    company_names = {
        norm(s.get("company")).casefold()
        for s in signals.get("signals", [])
        if norm(s.get("company"))
    }
    for rid, record in list(records.items()):
        if record.get("kind") == "person" and norm(record.get("name")).casefold() in company_names:
            records.pop(rid, None)

    for signal in signals.get("signals", []):
        if signal.get("priority") not in {"high", "medium"}:
            continue

        author = norm(signal.get("author"))
        author_type = signal.get("author_type")
        if author and author_type in {"person", "company"}:
            upsert(records, author_type, author, signal, now)

        company = norm(signal.get("company"))
        company_cf = company.casefold()
        if company:
            upsert(records, "company", company, signal, now)

        for person in signal.get("people") or []:
            name = norm(person.get("name"))
            relation = person.get("relation")
            confidence = person.get("confidence")
            if not name or name.casefold() == company_cf:
                continue
            if relation == "author" or confidence == "confirmed":
                upsert(records, "person", name, signal, now)

    items = list(records.values())
    items.sort(key=lambda r: (r.get("status") == "CLOSED", -as_int(r.get("score")), r.get("kind", ""), r.get("name", "").casefold()))

    status_counts = {status: sum(1 for r in items if r.get("status") == status) for status in STATUSES}
    return {
        "schema_version": 2,
        "generated_at": now,
        "status_model": {
            "allowed": STATUSES,
            "manual_fields": MANUAL_FIELDS,
            "note": "Automation preserves manual fields. Outbound actions remain manual/approval-only."
        },
        "summary": {
            "total": len(items),
            "people": sum(1 for r in items if r.get("kind") == "person"),
            "companies": sum(1 for r in items if r.get("kind") == "company"),
            "status_counts": status_counts,
            "review_recommended": sum(1 for r in items if (r.get("automation") or {}).get("recommended_status") == "REVIEW" and r.get("status") == "WATCH"),
            "do_not_contact": sum(1 for r in items if r.get("do_not_contact")),
        },
        "items": items,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default="feed_signals_latest.json")
    ap.add_argument("--watchlist", default="relationship_watchlist.json")
    args = ap.parse_args()

    signals = load_json(Path(args.signals), {"signals": []})
    path = Path(args.watchlist)
    existing = load_json(path, {"items": []})
    result = build(signals, existing)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"OK relationships={result['summary']['total']} "
        f"people={result['summary']['people']} companies={result['summary']['companies']} "
        f"review={result['summary']['review_recommended']} -> {path}"
    )


if __name__ == "__main__":
    main()
