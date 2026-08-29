#!/usr/bin/env python3
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def key(kind, name):
    return f"{kind}:{norm(name).casefold()}"


def score_for(signal):
    base = 85 if signal.get("priority") == "high" else 65
    confidence = int(signal.get("confidence") or 0)
    career = int(signal.get("career_relevance") or 0)
    networking = int(signal.get("networking_relevance") or 0)
    technical = int(signal.get("technical_relevance") or 0)
    return min(100, round(base * 0.35 + confidence * 0.25 + career * 0.15 + networking * 0.15 + technical * 0.10))


def suggested_action(signal, kind):
    actions = signal.get("recommended_actions") or []
    preferred = (
        ["connect_person", "follow_person", "engage_with_post", "watch"]
        if kind == "person"
        else ["check_jobs", "follow_company", "engage_with_post", "watch"]
    )
    for action in preferred:
        if action in actions:
            return action
    return "watch"


def upsert(records, kind, name, signal, now):
    name = norm(name)
    if not name:
        return
    rid = key(kind, name)
    old = records.get(rid, {})
    source_ids = list(old.get("source_signal_ids") or [])
    sid = signal.get("signal_id")
    if sid and sid not in source_ids:
        source_ids.append(sid)

    reasons = list(old.get("reasons") or [])
    reason = norm(signal.get("why_relevant"))
    if reason and reason not in reasons:
        reasons.append(reason)
    reasons = reasons[-5:]

    companies = list(old.get("companies") or [])
    company = norm(signal.get("company"))
    if company and company not in companies:
        companies.append(company)

    roles = list(old.get("roles") or [])
    for p in signal.get("people") or []:
        if norm(p.get("name")) == name:
            role = norm(p.get("role"))
            if role and role not in roles:
                roles.append(role)

    seen_this_signal = sid not in (old.get("source_signal_ids") or []) if sid else True
    score = score_for(signal)
    records[rid] = {
        "id": rid,
        "kind": kind,
        "name": name,
        "status": old.get("status", "WATCH"),
        "first_seen": old.get("first_seen", now),
        "last_seen": now,
        "times_seen": int(old.get("times_seen") or 0) + (1 if seen_this_signal else 0),
        "score": max(int(old.get("score") or 0), score),
        "latest_priority": signal.get("priority"),
        "latest_confidence": signal.get("confidence"),
        "suggested_action": suggested_action(signal, kind),
        "companies": companies,
        "roles": roles,
        "source_signal_ids": source_ids[-20:],
        "reasons": reasons,
        "latest_source_url": signal.get("source_url"),
        "updated_at": now,
    }


def build(signals, existing):
    now = datetime.now(timezone.utc).isoformat()
    records = {r["id"]: r for r in existing.get("items", []) if r.get("id")}

    for signal in signals.get("signals", []):
        if signal.get("priority") not in {"high", "medium"}:
            continue

        author = norm(signal.get("author"))
        author_type = signal.get("author_type")
        if author and author_type in {"person", "company"}:
            upsert(records, author_type, author, signal, now)

        company = norm(signal.get("company"))
        if company:
            upsert(records, "company", company, signal, now)

        for p in signal.get("people") or []:
            name = norm(p.get("name"))
            relation = p.get("relation")
            confidence = p.get("confidence")
            if name and (relation == "author" or confidence == "confirmed"):
                upsert(records, "person", name, signal, now)

    items = list(records.values())
    items.sort(key=lambda r: (-int(r.get("score") or 0), r.get("kind", ""), r.get("name", "").casefold()))
    return {
        "schema_version": 1,
        "generated_at": now,
        "summary": {
            "total": len(items),
            "people": sum(1 for r in items if r.get("kind") == "person"),
            "companies": sum(1 for r in items if r.get("kind") == "company"),
            "watch": sum(1 for r in items if r.get("status") == "WATCH"),
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
    print(f"OK relationships={result['summary']['total']} people={result['summary']['people']} companies={result['summary']['companies']} -> {path}")


if __name__ == "__main__":
    main()
