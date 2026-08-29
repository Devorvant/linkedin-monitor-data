#!/usr/bin/env python3
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

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


def compact_projects(signal):
    result = []
    for value in signal.get("projects") or []:
        if isinstance(value, str):
            text = norm(value)
        elif isinstance(value, dict):
            text = norm(value.get("name") or value.get("title"))
        else:
            text = norm(str(value))
        if text and text not in result:
            result.append(text)
    return result[:8]


def compact_technologies(signal):
    result = []
    for value in signal.get("technologies") or []:
        text = norm(str(value))
        if text and text not in result:
            result.append(text)
    return result[:10]


def name_tokens(value):
    return [x for x in re.findall(r"[a-z0-9]+", unquote(norm(value)).casefold()) if x]


def person_profile_url(name, signal):
    """Return a person /in/ URL only when it can be safely matched to this name."""
    name_cf = norm(name).casefold()
    author = norm(signal.get("author")).casefold()
    source_url = norm(signal.get("source_url"))

    # Author's own profile is unambiguous.
    if name_cf and name_cf == author and "/in/" in source_url:
        return source_url

    # Future analyzer versions may attach a URL directly to the person object.
    for person in signal.get("people") or []:
        if norm(person.get("name")).casefold() == name_cf:
            direct = norm(person.get("profile_url") or person.get("url"))
            if "/in/" in direct:
                return direct

    candidates = []
    for item in signal.get("links") or []:
        url = norm(item.get("url"))
        if item.get("link_type") == "person" and "/in/" in url:
            candidates.append(url)

    target_tokens = name_tokens(name)
    if not target_tokens:
        return None

    best_url = None
    best_score = 0
    for url in candidates:
        try:
            slug = urlparse(url).path.split("/in/", 1)[1].strip("/")
        except Exception:
            continue
        slug_tokens = name_tokens(slug)
        overlap = sum(1 for token in target_tokens if any(token == s or token in s or s in token for s in slug_tokens))
        # Require two matching tokens, except for an uncommon single-token exact author name.
        if overlap >= 2 and overlap > best_score:
            best_score = overlap
            best_url = url
    return best_url


def company_profile_url(name, signal):
    author = norm(signal.get("author")).casefold()
    name_cf = norm(name).casefold()
    for item in signal.get("links") or []:
        url = norm(item.get("url"))
        if item.get("link_type") != "company" or "/company/" not in url:
            continue
        if item.get("relation") == "author" and author == name_cf:
            return re.sub(r"/posts/?(?:\?.*)?$", "/", url)
    source = norm(signal.get("source_url"))
    if author == name_cf and "/company/" in source:
        return re.sub(r"/posts/?(?:\?.*)?$", "/", source)
    return None


def migrate_signal_history(old):
    history = list(old.get("signal_history") or [])
    if history:
        return history
    if not old:
        return []
    at = old.get("last_seen") or old.get("updated_at") or old.get("first_seen")
    if not at:
        return []
    return [{
        "at": at,
        "signal_id": (old.get("source_signal_ids") or [None])[-1],
        "priority": old.get("latest_priority"),
        "confidence": old.get("latest_confidence"),
        "source_url": old.get("latest_source_url"),
        "reason": (old.get("reasons") or [None])[-1],
        "projects": [],
        "technologies": [],
        "source": "legacy_snapshot",
    }]


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

    profile_url = old.get("profile_url")
    if kind == "person":
        profile_url = person_profile_url(name, signal) or profile_url
    elif kind == "company":
        profile_url = company_profile_url(name, signal) or profile_url

    signal_history = migrate_signal_history(old)
    if is_new_signal or (not old and not sid):
        signal_history.append({
            "at": now,
            "signal_id": sid,
            "priority": signal.get("priority"),
            "confidence": signal.get("confidence"),
            "source_url": signal.get("source_url"),
            "profile_url": profile_url,
            "reason": reason or None,
            "projects": compact_projects(signal),
            "technologies": compact_technologies(signal),
            "source": "feed_signal",
        })
    signal_history = signal_history[-25:]

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
        "profile_url": profile_url,
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
        "signal_history": signal_history,
        "reasons": reasons,
        "latest_source_url": signal.get("source_url"),
        "status_history": status_history,
        "automation": automation,
        "updated_at": now,
    }


def build(signals, existing):
    now = datetime.now(timezone.utc).isoformat()
    records = {r["id"]: r for r in existing.get("items", []) if r.get("id")}

    company_names = {norm(s.get("company")).casefold() for s in signals.get("signals", []) if norm(s.get("company"))}
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
        "schema_version": 4,
        "generated_at": now,
        "status_model": {
            "allowed": STATUSES,
            "manual_fields": MANUAL_FIELDS,
            "note": "Automation preserves manual fields. profile_url is distinct from source_url. Outbound remains approval-only."
        },
        "summary": {
            "total": len(items),
            "people": sum(1 for r in items if r.get("kind") == "person"),
            "companies": sum(1 for r in items if r.get("kind") == "company"),
            "people_with_profile_url": sum(1 for r in items if r.get("kind") == "person" and r.get("profile_url")),
            "status_counts": status_counts,
            "review_recommended": sum(1 for r in items if (r.get("automation") or {}).get("recommended_status") == "REVIEW" and r.get("status") == "WATCH"),
            "do_not_contact": sum(1 for r in items if r.get("do_not_contact")),
            "repeat_entities": sum(1 for r in items if as_int(r.get("times_seen")) > 1),
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
        f"OK relationships={result['summary']['total']} people={result['summary']['people']} "
        f"companies={result['summary']['companies']} profiles={result['summary']['people_with_profile_url']} "
        f"review={result['summary']['review_recommended']} repeats={result['summary']['repeat_entities']} -> {path}"
    )


if __name__ == "__main__":
    main()
