#!/usr/bin/env python3
import argparse
import copy
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

import action_planner as planner


PRIORITY_RANK = {"high": 0, "medium": 1}
COMPANY_SCOPED_ACTIONS = {"follow_company", "research_company", "check_jobs"}
PERSON_SCOPED_ACTIONS = {
    "follow_person", "connect_person", "message_person", "job_outreach",
    "research_contact", "find_warm_path",
}


def load_json(path, default=None):
    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def norm_name(value):
    text = planner.clean(value).casefold()
    text = re.sub(r"\s*•\s*(?:1-й|2-й|3-й\+?)\s*$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def company_slug(url):
    match = re.search(r"https?://(?:www\.)?linkedin\.com/company/([^/?#]+)", planner.clean(url), re.I)
    return match.group(1).casefold() if match else ""


def signal_key(signal):
    sid = planner.clean(signal.get("signal_id"))
    if sid:
        return f"signal:{sid}"
    post = planner.clean(signal.get("source_post_id"))
    if post:
        return f"post:{post}:{norm_name(signal.get('author'))}:{norm_name(signal.get('company'))}"
    return "fallback:" + "|".join([
        norm_name(signal.get("author")),
        norm_name(signal.get("company")),
        planner.clean(signal.get("source_url")).casefold(),
        planner.clean(signal.get("why_relevant")).casefold()[:160],
    ])


def entity_key(signal):
    target = planner.target_info(signal)
    if target.get("name"):
        slugs = [planner.profile_slug(url) for url in planner.signal_urls(signal)]
        slugs = [slug for slug in slugs if slug]
        if slugs:
            return f"person:{slugs[0]}"
        return f"person:{norm_name(target.get('name'))}"

    company = planner.clean(target.get("company") or signal.get("company"))
    if company:
        slugs = [company_slug(url) for url in planner.signal_urls(signal)]
        slugs = [slug for slug in slugs if slug]
        if slugs:
            return f"company:{slugs[0]}"
        return f"company:{norm_name(company)}"

    return signal_key(signal)


def date_for_file(path):
    return path.stem


def collect_signals(history_dir, latest_path=None):
    by_signal = {}
    source_files = []

    files = sorted(history_dir.glob("*.json")) if history_dir.exists() else []
    for path in files:
        source_files.append(path.name)
        data = load_json(path, {"signals": []})
        date = date_for_file(path)
        for signal in data.get("signals", []):
            if signal.get("priority") not in {"high", "medium"}:
                continue
            row = copy.deepcopy(signal)
            row["_history_date"] = date
            row["_history_source"] = path.name
            key = signal_key(row)
            old = by_signal.get(key)
            if old is None or row["_history_date"] >= old["_history_date"]:
                by_signal[key] = row

    if latest_path and latest_path.exists():
        data = load_json(latest_path, {"signals": []})
        generated = planner.clean(data.get("generated_at"))
        date = generated[:10] if generated else datetime.now(timezone.utc).date().isoformat()
        for signal in data.get("signals", []):
            if signal.get("priority") not in {"high", "medium"}:
                continue
            row = copy.deepcopy(signal)
            row["_history_date"] = date
            row["_history_source"] = latest_path.name
            key = signal_key(row)
            old = by_signal.get(key)
            if old is None or row["_history_date"] >= old["_history_date"]:
                by_signal[key] = row

    return list(by_signal.values()), source_files


def representative_signals(signals):
    buckets = {}
    for signal in signals:
        buckets.setdefault(entity_key(signal), []).append(signal)

    representatives = []
    stats_by_signal_id = {}
    for _, rows in buckets.items():
        rows.sort(
            key=lambda s: (
                s.get("_history_date") or "",
                -int(s.get("confidence") or 0),
            ),
            reverse=True,
        )
        representative = copy.deepcopy(rows[0])
        dates = sorted({s.get("_history_date") for s in rows if s.get("_history_date")})
        stats = {
            "occurrences": len(rows),
            "first_seen": dates[0] if dates else None,
            "last_seen": dates[-1] if dates else None,
            "dates": dates,
            "high": sum(1 for s in rows if s.get("priority") == "high"),
            "medium": sum(1 for s in rows if s.get("priority") == "medium"),
        }
        representative["_history_stats"] = stats
        representatives.append(representative)
        stats_by_signal_id[planner.clean(representative.get("signal_id"))] = stats

    return representatives, stats_by_signal_id


def direct_urls(signal, target):
    person_url = ""
    company_url = ""
    target_name = norm_name((target or {}).get("name"))
    target_company = norm_name((target or {}).get("company") or signal.get("company"))

    for url in planner.signal_urls(signal):
        if not person_url and planner.profile_slug(url):
            person_url = planner.clean(url)
        if not company_url and company_slug(url):
            company_url = planner.clean(url)

    source = planner.clean(signal.get("source_url"))
    if target_name and "/in/" in source:
        person_url = source
    if target_company and "/company/" in source:
        company_url = source

    return person_url or None, company_url or None


def action_scope_key(item, action):
    target = item.get("target") or {}
    if action in COMPANY_SCOPED_ACTIONS:
        company = planner.clean(target.get("company") or item.get("company"))
        return (action, "company", norm_name(company)) if company else None
    if action in PERSON_SCOPED_ACTIONS:
        name = planner.clean(target.get("name") or item.get("author"))
        return (action, "person", norm_name(name)) if name else None
    return None


def freshness_for(last_seen, reference_date):
    try:
        seen = date.fromisoformat(str(last_seen))
        age_days = max(0, (reference_date - seen).days)
    except Exception:
        age_days = 999

    if age_days <= 3:
        bucket = "fresh"
    elif age_days <= 7:
        bucket = "current"
    elif age_days <= 14:
        bucket = "aging"
    else:
        bucket = "stale"

    return {
        "age_days": age_days,
        "bucket": bucket,
        "actionable_now": age_days <= 7,
    }


def apply_freshness_guard(item):
    freshness = item.get("freshness") or {}
    raw_age = freshness.get("age_days")
    age_days = int(raw_age if raw_age is not None else 999)
    if age_days <= 7:
        return

    stale_actions = {
        "engage_with_post",
        "connect_person",
        "message_person",
        "job_outreach",
    }
    kept = []
    removed = []
    for action in item.get("action_plan") or []:
        if action.get("action") in stale_actions:
            removed.append(action.get("action"))
            continue
        kept.append(action)

    if removed:
        item["freshness_guard_applied"] = True
        item["freshness_guard_removed"] = removed
    item["action_plan"] = kept


def recompute_item_fields(item, representative):
    plan = item.get("action_plan") or []
    if not plan:
        plan = [{
            "action": "watch",
            "reason": "No current action remains after accumulated-history deduplication.",
            "approval_required": False,
        }]
        item["action_plan"] = plan

    item["primary_action"] = plan[0]["action"]
    item["secondary_action"] = plan[1]["action"] if len(plan) > 1 else None
    item["approval_required"] = any(bool(x.get("approval_required")) for x in plan)
    pending = any(
        bool(x.get("approval_required")) and x.get("action") in planner.OUTBOUND
        for x in plan
    )
    item["execution_status"] = "PENDING_APPROVAL" if pending else "READY"
    item["drafts"] = planner.drafts(representative, item.get("target") or {}, plan)


def rebuild(history_dir, latest_signals, crm_path, relationships_dir):
    signals, source_files = collect_signals(history_dir, latest_signals)
    representatives, stats_by_signal_id = representative_signals(signals)

    crm = load_json(crm_path, {"items": []})
    known = planner.load_known_relationships(relationships_dir)
    queue = planner.build_queue({"signals": representatives}, crm, known)

    representative_by_id = {
        planner.clean(s.get("signal_id")): s for s in representatives
    }

    for item in queue.get("items", []):
        sid = planner.clean(item.get("signal_id"))
        item["history_stats"] = stats_by_signal_id.get(sid, {
            "occurrences": 1, "first_seen": None, "last_seen": None, "high": 0, "medium": 0
        })
        representative = representative_by_id.get(sid, {})
        person_url, company_url = direct_urls(representative, item.get("target") or {})
        item.setdefault("target", {})
        if person_url:
            item["target"]["profile_url"] = person_url
        if company_url:
            item["target"]["company_url"] = company_url

    seen = set()
    for item in queue.get("items", []):
        filtered = []
        for action in item.get("action_plan") or []:
            key = action_scope_key(item, action.get("action"))
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            filtered.append(action)
        item["action_plan"] = filtered

    action_names = sorted({
        action.get("action")
        for item in queue.get("items", [])
        for action in item.get("action_plan", [])
        if action.get("action")
    })
    queue["summary"] = {
        "items": len(queue.get("items", [])),
        "high": sum(1 for x in queue.get("items", []) if x.get("priority") == "high"),
        "medium": sum(1 for x in queue.get("items", []) if x.get("priority") == "medium"),
        "pending_approval": sum(
            1 for x in queue.get("items", [])
            if x.get("execution_status") == "PENDING_APPROVAL"
        ),
        "research_needed": sum(
            1 for x in queue.get("items", [])
            if (x.get("research") or {}).get("needed")
        ),
        "actionable_now": sum(1 for x in queue.get("items", []) if x.get("recommendation_status") == "ACTIONABLE_NOW"),
        "watchlist": sum(1 for x in queue.get("items", []) if x.get("recommendation_status") == "WATCHLIST"),
        "archive": sum(1 for x in queue.get("items", []) if x.get("recommendation_status") == "ARCHIVE"),
        "primary_action_counts": {
            action: sum(1 for x in queue.get("items", []) if x.get("primary_action") == action)
            for action in sorted({x.get("primary_action") for x in queue.get("items", []) if x.get("primary_action")})
        },
        "action_plan_counts": {
            action: sum(
                1 for x in queue.get("items", [])
                if any(p.get("action") == action for p in x.get("action_plan", []))
            )
            for action in action_names
        },
    }

    dates = sorted({
        s.get("_history_date") for s in signals if s.get("_history_date")
    })
    reference_date = (
        date.fromisoformat(dates[-1]) if dates
        else datetime.now(timezone.utc).date()
    )

    for item in queue.get("items", []):
        stats = item.get("history_stats") or {}
        item["freshness"] = freshness_for(stats.get("last_seen"), reference_date)
        item["action_plan_full"] = copy.deepcopy(item.get("action_plan") or [])
        item["drafts_full"] = copy.deepcopy(item.get("drafts") or {})
        item["execution_status_full"] = item.get("execution_status")
        item["approval_required_full"] = item.get("approval_required")
        item["recommendation_status"] = (
            "ACTIONABLE_NOW" if item["freshness"]["actionable_now"]
            else "WATCHLIST" if item["freshness"]["age_days"] <= 14
            else "ARCHIVE"
        )
        apply_freshness_guard(item)
        representative = representative_by_id.get(planner.clean(item.get("signal_id")), {})
        recompute_item_fields(item, representative)

    queue["items"].sort(
        key=lambda x: (
            0 if x.get("recommendation_status") == "ACTIONABLE_NOW" else
            1 if x.get("recommendation_status") == "WATCHLIST" else 2,
            PRIORITY_RANK.get(x.get("priority"), 9),
            int(
                (x.get("freshness") or {}).get("age_days")
                if (x.get("freshness") or {}).get("age_days") is not None
                else 999
            ),
            -int((x.get("history_stats") or {}).get("occurrences") or 0),
            -int(x.get("confidence") or 0),
        )
    )

    action_names = sorted({
        action.get("action")
        for item in queue.get("items", [])
        for action in item.get("action_plan", [])
        if action.get("action")
    })
    queue["summary"].update({
        "pending_approval": sum(
            1 for x in queue.get("items", [])
            if x.get("execution_status") == "PENDING_APPROVAL"
        ),
        "actionable_now": sum(
            1 for x in queue.get("items", [])
            if x.get("recommendation_status") == "ACTIONABLE_NOW"
        ),
        "watchlist": sum(
            1 for x in queue.get("items", [])
            if x.get("recommendation_status") == "WATCHLIST"
        ),
        "archive": sum(
            1 for x in queue.get("items", [])
            if x.get("recommendation_status") == "ARCHIVE"
        ),
        "primary_action_counts": {
            action: sum(1 for x in queue.get("items", []) if x.get("primary_action") == action)
            for action in sorted({
                x.get("primary_action")
                for x in queue.get("items", [])
                if x.get("primary_action")
            })
        },
        "action_plan_counts": {
            action: sum(
                1 for x in queue.get("items", [])
                if any(p.get("action") == action for p in x.get("action_plan", []))
            )
            for action in action_names
        },
    })

    queue["schema_version"] = 4
    queue["recommendation_mode"] = "accumulated_current_state"
    queue["history_scope"] = {
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "history_files": len(source_files),
        "unique_signals": len(signals),
        "entities": len(representatives),
    }
    queue["generated_at"] = datetime.now(timezone.utc).isoformat()
    queue["source_generated_at"] = queue["generated_at"]
    return queue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history-dir", default="history/feed_signals")
    ap.add_argument("--latest-signals", default="feed_signals_latest.json")
    ap.add_argument("--crm", default="relationship_watchlist.json")
    ap.add_argument("--relationships-dir", default="linkedin_relationships")
    ap.add_argument("--output", default="recommendations_current.json")
    args = ap.parse_args()

    result = rebuild(
        Path(args.history_dir),
        Path(args.latest_signals),
        Path(args.crm),
        Path(args.relationships_dir),
    )
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    scope = result.get("history_scope") or {}
    print(
        f"OK current_recommendations={result['summary']['items']} "
        f"signals={scope.get('unique_signals', 0)} "
        f"entities={scope.get('entities', 0)} "
        f"period={scope.get('first_date')}..{scope.get('last_date')} -> {args.output}"
    )


if __name__ == "__main__":
    main()
