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


def post_permalink(signal):
    source = planner.clean(signal.get("source_url"))
    if "/feed/update/" in source or ("/posts/" in source and "activity-" in source):
        return source

    for item in signal.get("links") or []:
        if not isinstance(item, dict):
            continue
        url = planner.clean(item.get("url"))
        if "/feed/update/" in url or ("/posts/" in url and "activity-" in url):
            return url

    post_id = planner.clean(signal.get("source_post_id") or signal.get("post_id"))
    if post_id:
        return f"https://www.linkedin.com/feed/update/urn:li:ugcPost:{post_id}/"
    return None


def crm_company_url(crm, company):
    company_key = norm_name(company)
    if not company_key:
        return None
    for item in crm.get("items") or []:
        if item.get("kind") != "company":
            continue
        if norm_name(item.get("name")) != company_key:
            continue
        url = planner.clean(item.get("profile_url"))
        if "/company/" in url:
            return url
    return None


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


def as_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def signal_context(signal):
    hiring = signal.get("hiring") or {}
    return {
        "signal_types": list(signal.get("signal_types") or []),
        "career_relevance": as_int(signal.get("career_relevance")),
        "technical_relevance": as_int(signal.get("technical_relevance")),
        "networking_relevance": as_int(signal.get("networking_relevance")),
        "content_relevance": as_int(signal.get("content_relevance")),
        "hiring_detected": bool(hiring.get("detected")),
        "hiring_intent": planner.clean(hiring.get("intent")),
        "hiring_roles": list(hiring.get("roles") or []),
        "profile_matches": list(signal.get("profile_matches") or []),
    }


def refine_action_plan(item, signal):
    plan = [dict(x) for x in (item.get("action_plan") or [])]
    if not plan:
        return

    ctx = signal_context(signal)
    types = set(ctx["signal_types"])
    hiring = ctx["hiring_detected"] or "HIRING_SIGNAL" in types
    connected = "CONNECTION" in (item.get("known_relationships") or [])
    occurrences = as_int((item.get("history_stats") or {}).get("occurrences"))

    # Adjacent hiring should first be checked, not immediately turned into
    # a second outbound job-outreach task.
    if hiring and connected and ctx["career_relevance"] < 85:
        plan = [x for x in plan if x.get("action") != "job_outreach"]

    if hiring:
        if connected:
            order = {
                "check_jobs": 10,
                "message_person": 20,
                "job_outreach": 30,
                "find_warm_path": 40,
                "engage_with_post": 50,
                "research_company": 60,
                "review_technology": 70,
                "save_for_content": 80,
            }
        else:
            order = {
                "check_jobs": 10,
                "find_warm_path": 20,
                "connect_person": 30,
                "engage_with_post": 40,
                "follow_person": 50,
                "research_company": 60,
                "review_technology": 70,
                "save_for_content": 80,
            }
    else:
        order = {
            "research_contact": 10,
            "find_warm_path": 20,
            "follow_person": 30,
            "engage_with_post": 40,
            "connect_person": 50,
            "research_company": 60,
            "review_technology": 70,
            "save_for_content": 80,
            "check_jobs": 90,
        }
        if occurrences <= 1:
            for action in plan:
                if action.get("action") == "connect_person":
                    action["reason"] = (
                        "One relevant signal so far; check context first and consider connecting after that."
                    )

    indexed = list(enumerate(plan))
    indexed.sort(key=lambda pair: (order.get(pair[1].get("action"), 999), pair[0]))
    item["action_plan"] = [x for _, x in indexed]


def calculate_action_score(item, signal):
    ctx = signal_context(signal)
    types = set(ctx["signal_types"])
    age = as_int((item.get("freshness") or {}).get("age_days"))
    occurrences = as_int((item.get("history_stats") or {}).get("occurrences"))
    relationships = set(item.get("known_relationships") or [])
    hiring = ctx["hiring_detected"] or "HIRING_SIGNAL" in types

    components = {
        "career": round(ctx["career_relevance"] * 0.40, 1),
        "technical": round(ctx["technical_relevance"] * 0.15, 1),
        "networking": round(ctx["networking_relevance"] * 0.08, 1),
        "confidence": round(as_int(item.get("confidence")) * 0.07, 1),
        "freshness": max(0, 10 - age),
        "relationship": min(
            8,
            (6 if "CONNECTION" in relationships else 0)
            + (1 if "FOLLOWER" in relationships else 0)
            + (2 if "FOLLOWING_PERSON" in relationships else 0)
            + (2 if "FOLLOWING_COMPANY" in relationships else 0),
        ),
        "repeat": min(5, max(0, occurrences - 1)),
        "hiring": 8 if hiring else 0,
        "exact_hiring_match": 5 if hiring and ctx["career_relevance"] >= 90 else 0,
        "opportunity": 2 if "OPPORTUNITY_SIGNAL" in types else 0,
        "priority": 2 if item.get("priority") == "high" else 1,
    }
    score = min(100, round(sum(components.values())))
    return score, components, ctx


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

        company_name = planner.clean(
            item["target"].get("company") or item.get("company") or representative.get("company")
        )
        exact_company_url = crm_company_url(crm, company_name)
        if exact_company_url:
            company_url = exact_company_url
        if company_url:
            item["target"]["company_url"] = company_url

        item["post_url"] = post_permalink(representative)
        item["signal_context"] = signal_context(representative)

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
        representative = representative_by_id.get(planner.clean(item.get("signal_id")), {})
        refine_action_plan(item, representative)

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
        score, components, ctx = calculate_action_score(item, representative)
        item["action_score"] = score
        item["action_score_components"] = components
        item["signal_context"] = ctx
        recompute_item_fields(item, representative)

    queue["items"].sort(
        key=lambda x: (
            0 if x.get("recommendation_status") == "ACTIONABLE_NOW" else
            1 if x.get("recommendation_status") == "WATCHLIST" else 2,
            -int(x.get("action_score") or 0),
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
