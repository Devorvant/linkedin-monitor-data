#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_ACTIONS = {
    "connect_person",
    "follow_person",
    "follow_company",
    "engage_with_post",
    "save_for_content",
    "check_jobs",
    "review_technology",
    "watch",
    "no_action",
}

ACTION_PRIORITY = [
    "check_jobs",
    "connect_person",
    "engage_with_post",
    "follow_person",
    "follow_company",
    "save_for_content",
    "review_technology",
    "watch",
]


def load_json(path: Path):
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

    for a in ACTION_PRIORITY:
        if a in recommended and a not in actions:
            actions.append(a)

    if content_angle and "save_for_content" not in actions and priority == "high":
        actions.append("save_for_content")

    if not actions:
        actions = ["watch"] if priority == "medium" else ["no_action"]

    return actions[:2]


def best_target(signal):
    targets = signal.get("relationship_targets") or []
    if targets:
        t = targets[0]
        return {
            "target_role": t.get("target_role"),
            "reason": t.get("reason"),
            "priority": t.get("priority"),
        }
    people = signal.get("people") or []
    if people:
        p = people[0]
        return {
            "name": p.get("name"),
            "role": p.get("role"),
            "relation": p.get("relation"),
        }
    return None


def build_queue(data):
    items = []
    for signal in data.get("signals", []):
        if signal.get("priority") not in {"high", "medium"}:
            continue
        actions = pick_actions(signal)
        items.append({
            "signal_id": signal.get("signal_id"),
            "source_post_id": signal.get("source_post_id"),
            "priority": signal.get("priority"),
            "confidence": signal.get("confidence"),
            "author": signal.get("author"),
            "company": signal.get("company"),
            "primary_action": actions[0],
            "secondary_action": actions[1] if len(actions) > 1 else None,
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
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_generated_at": data.get("generated_at"),
        "summary": {
            "items": len(items),
            "high": sum(1 for x in items if x.get("priority") == "high"),
            "medium": sum(1 for x in items if x.get("priority") == "medium"),
            "primary_action_counts": {a: sum(1 for x in items if x.get("primary_action") == a) for a in sorted({x.get("primary_action") for x in items})},
        },
        "items": items,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default="feed_signals_latest.json")
    ap.add_argument("--output", default="action_queue_latest.json")
    args = ap.parse_args()

    data = load_json(Path(args.signals))
    queue = build_queue(data)
    Path(args.output).write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK action_items={queue['summary']['items']} high={queue['summary']['high']} medium={queue['summary']['medium']} -> {args.output}")


if __name__ == "__main__":
    main()
