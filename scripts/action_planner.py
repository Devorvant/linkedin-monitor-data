#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_ACTIONS = {
    "connect_person", "follow_person", "follow_company", "engage_with_post",
    "save_for_content", "check_jobs", "review_technology", "watch", "no_action"
}
ACTION_PRIORITY = [
    "check_jobs", "connect_person", "engage_with_post", "follow_person",
    "follow_company", "save_for_content", "review_technology", "watch"
]
OUTBOUND = {"connect_person"}


def load_json(path: Path, default=None):
    if not path.exists():
        return default if default is not None else {}
    return json.loads(path.read_text(encoding="utf-8"))


def pick_actions(signal):
    recommended = [a for a in signal.get("recommended_actions", []) if a in ALLOWED_ACTIONS]
    priority = signal.get("priority", "low")
    confidence = int(signal.get("confidence") or 0)
    hiring = signal.get("hiring") or {}
    relationship_targets = signal.get("relationship_targets") or []
    content_angle = signal.get("content_angle")
    actions = []
    if hiring.get("detected") and hiring.get("intent") in {"strong", "explicit"}:
        actions.append("check_jobs")
    if relationship_targets and confidence >= 75 and "connect_person" in recommended:
        actions.append("connect_person")
    for action in ACTION_PRIORITY:
        if action in recommended and action not in actions:
            actions.append(action)
    if content_angle and "save_for_content" not in actions and priority == "high":
        actions.append("save_for_content")
    if not actions:
        actions = ["watch"] if priority == "medium" else ["no_action"]
    return actions[:2]


def best_target(signal):
    targets = signal.get("relationship_targets") or []
    if targets:
        t = targets[0]
        return {"target_role": t.get("target_role"), "reason": t.get("reason"), "priority": t.get("priority")}
    people = signal.get("people") or []
    if people:
        p = people[0]
        return {"name": p.get("name"), "role": p.get("role"), "relation": p.get("relation")}
    return None


def match_crm(signal, crm):
    items = crm.get("items", []) if crm else []
    author = (signal.get("author") or "").strip().casefold()
    company = (signal.get("company") or "").strip().casefold()
    for record in items:
        name = (record.get("name") or "").strip().casefold()
        if name and (name == author or name == company):
            return record
    return None


def apply_crm_guard(actions, crm_record):
    if not crm_record:
        return actions, False, None
    status = crm_record.get("status", "WATCH")
    do_not_contact = bool(crm_record.get("do_not_contact"))
    guarded = []
    blocked_reason = None
    for action in actions:
        if action in OUTBOUND:
            if do_not_contact:
                blocked_reason = "do_not_contact"
                continue
            if status in {"CONNECTED", "CONTACTED", "REPLIED", "CLOSED"}:
                blocked_reason = f"crm_status_{status.lower()}"
                continue
        guarded.append(action)
    if not guarded:
        guarded = ["no_action" if status == "CLOSED" else "watch"]
    return guarded[:2], bool(blocked_reason), blocked_reason


def build_queue(data, crm):
    items = []
    for signal in data.get("signals", []):
        if signal.get("priority") not in {"high", "medium"}:
            continue
        crm_record = match_crm(signal, crm)
        actions, blocked, blocked_reason = apply_crm_guard(pick_actions(signal), crm_record)
        primary = actions[0]
        items.append({
            "signal_id": signal.get("signal_id"),
            "source_post_id": signal.get("source_post_id"),
            "priority": signal.get("priority"),
            "confidence": signal.get("confidence"),
            "author": signal.get("author"),
            "company": signal.get("company"),
            "primary_action": primary,
            "secondary_action": actions[1] if len(actions) > 1 else None,
            "approval_required": primary in OUTBOUND,
            "execution_status": "PENDING_APPROVAL" if primary in OUTBOUND else "READY",
            "crm_guard_applied": blocked,
            "crm_guard_reason": blocked_reason,
            "crm_entity_id": crm_record.get("id") if crm_record else None,
            "crm_status": crm_record.get("status") if crm_record else None,
            "target": best_target(signal),
            "reason": signal.get("why_relevant"),
            "content_angle": signal.get("content_angle"),
            "source_url": signal.get("source_url"),
            "analysis_engine": signal.get("analysis_engine"),
            "llm": signal.get("llm"),
        })
    rank = {"high": 0, "medium": 1}
    items.sort(key=lambda x: (rank.get(x.get("priority"), 9), -(x.get("confidence") or 0)))
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_generated_at": data.get("generated_at"),
        "policy": {"outbound_requires_human_approval": True, "outbound_actions": sorted(OUTBOUND)},
        "summary": {
            "items": len(items),
            "high": sum(1 for x in items if x.get("priority") == "high"),
            "medium": sum(1 for x in items if x.get("priority") == "medium"),
            "pending_approval": sum(1 for x in items if x.get("execution_status") == "PENDING_APPROVAL"),
            "primary_action_counts": {a: sum(1 for x in items if x.get("primary_action") == a) for a in sorted({x.get("primary_action") for x in items})},
        },
        "items": items,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default="feed_signals_latest.json")
    ap.add_argument("--crm", default="relationship_watchlist.json")
    ap.add_argument("--output", default="action_queue_latest.json")
    args = ap.parse_args()
    data = load_json(Path(args.signals), {"signals": []})
    crm = load_json(Path(args.crm), {"items": []})
    queue = build_queue(data, crm)
    Path(args.output).write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK action_items={queue['summary']['items']} pending_approval={queue['summary']['pending_approval']} -> {args.output}")


if __name__ == "__main__":
    main()
