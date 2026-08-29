#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_ACTIONS = {
    "connect_person", "follow_person", "follow_company", "engage_with_post",
    "save_for_content", "check_jobs", "review_technology", "watch", "no_action",
    "message_person", "job_outreach", "research_contact", "research_company",
    "find_warm_path"
}
OUTBOUND = {"connect_person", "message_person", "job_outreach"}

STAGE_BY_CRM = {
    "WATCH": "observe",
    "REVIEW": "research",
    "CONNECT": "engage",
    "CONNECTED": "connected",
    "CONTACTED": "contacted",
    "REPLIED": "conversation",
    "CLOSED": "closed",
}


def load_json(path: Path, default=None):
    if not path.exists():
        return default if default is not None else {}
    return json.loads(path.read_text(encoding="utf-8"))


def clean(value):
    return str(value or "").strip()


def people(signal):
    return [p for p in (signal.get("people") or []) if clean(p.get("name"))]


def target_info(signal):
    """Return the best concrete person plus strategic target role, when available."""
    ps = people(signal)
    rel_targets = signal.get("relationship_targets") or []
    t = rel_targets[0] if rel_targets else {}

    # Prefer author person, then first confirmed named person.
    chosen = None
    author = clean(signal.get("author"))
    if signal.get("author_type") == "person" and author:
        chosen = next((p for p in ps if clean(p.get("name")).casefold() == author.casefold()), None)
        chosen = chosen or {"name": author, "relation": "author", "role": None}
    if not chosen and ps:
        chosen = next((p for p in ps if p.get("confidence") == "confirmed"), ps[0])

    return {
        "name": clean((chosen or {}).get("name")) or None,
        "role": clean((chosen or {}).get("role")) or None,
        "relation": clean((chosen or {}).get("relation")) or None,
        "target_role": clean(t.get("target_role")) or None,
        "target_reason": clean(t.get("reason")) or None,
        "target_priority": clean(t.get("priority")) or None,
        "company": clean(signal.get("company")) or None,
    }


def match_crm(signal, crm):
    items = crm.get("items", []) if crm else []
    author = clean(signal.get("author")).casefold()
    company = clean(signal.get("company")).casefold()
    names = {clean(p.get("name")).casefold() for p in people(signal)}
    names.discard("")
    for record in items:
        name = clean(record.get("name")).casefold()
        if name and (name == author or name == company or name in names):
            return record
    return None


def relationship_stage(crm_record):
    if not crm_record:
        return "observe"
    return STAGE_BY_CRM.get(crm_record.get("status", "WATCH"), "observe")


def has_hiring_signal(signal):
    h = signal.get("hiring") or {}
    return bool(h.get("detected")) and h.get("intent") in {"strong", "explicit"}


def is_job_relevant(signal):
    return has_hiring_signal(signal) and max(
        int(signal.get("career_relevance") or 0), int(signal.get("technical_relevance") or 0)
    ) >= 65


def research_plan(signal, target):
    concrete_person = bool(target.get("name"))
    company = clean(signal.get("company"))
    generic_role = bool(target.get("target_role")) and not concrete_person
    needed = generic_role or not concrete_person
    tasks = []
    if needed:
        tasks.append("identify_concrete_person")
    if concrete_person and not target.get("role"):
        tasks.append("verify_current_role")
    if company:
        tasks.append("check_company_and_relevant_team")
    if has_hiring_signal(signal):
        tasks.append("check_relevant_open_roles")
    if concrete_person:
        tasks.append("check_shared_context_or_warm_path")
    # Preserve order, remove duplicates.
    tasks = list(dict.fromkeys(tasks))
    return {"needed": needed, "tasks": tasks}


def strategy_actions(signal, crm_record, target, research):
    """Produce an ordered relationship strategy, not just one raw command."""
    recommended = [a for a in signal.get("recommended_actions", []) if a in ALLOWED_ACTIONS]
    status = (crm_record or {}).get("status", "WATCH")
    dnc = bool((crm_record or {}).get("do_not_contact"))
    concrete_person = bool(target.get("name"))
    company = clean(signal.get("company"))
    actions = []

    def add(action, why, approval=None):
        if action not in ALLOWED_ACTIONS or any(x["action"] == action for x in actions):
            return
        if approval is None:
            approval = action in OUTBOUND
        actions.append({"action": action, "reason": why, "approval_required": bool(approval)})

    if status == "CLOSED":
        add("no_action", "Relationship is closed in CRM.", False)
        return actions

    if dnc:
        add("watch", "CRM is marked do_not_contact; observation only.", False)
        if company:
            add("research_company", "Company context can still be monitored without outbound contact.", False)
        return actions

    if research.get("needed"):
        add("research_contact", "A concrete person should be identified before any connection or message.", False)
        if company:
            add("research_company", "Check the relevant team, current projects and open roles.", False)
    else:
        if status in {"WATCH", "REVIEW", "CONNECT"}:
            if "engage_with_post" in recommended:
                add("engage_with_post", "Create a natural context before direct outreach.", False)
            if "follow_person" in recommended:
                add("follow_person", "Keep the person in the observation loop.", False)
            if concrete_person and "connect_person" in recommended:
                add("connect_person", "Concrete relevant person identified; connection can be considered.", True)
            if concrete_person:
                add("find_warm_path", "Check shared contacts or other contextual paths before outreach.", False)

        if status == "CONNECTED":
            add("message_person", "The person is already connected; a contextual message can be prepared.", True)
        elif status == "CONTACTED":
            add("watch", "Already contacted; wait for a reply or a new signal.", False)
        elif status == "REPLIED":
            add("watch", "Conversation exists; manage follow-up manually from CRM context.", False)

    if has_hiring_signal(signal):
        add("check_jobs", "Hiring signal detected; verify whether relevant roles exist.", False)
        if concrete_person and status == "CONNECTED" and is_job_relevant(signal):
            add("job_outreach", "Relevant hiring context plus an established LinkedIn connection.", True)

    if company and "follow_company" in recommended:
        add("follow_company", "Keep company activity visible for future signals.", False)
    if "review_technology" in recommended:
        add("review_technology", "Technical project or technology is relevant to the profile.", False)
    if signal.get("content_angle") and "save_for_content" in recommended:
        add("save_for_content", "Signal may support an original expert post later.", False)

    if not actions:
        add("watch", "No higher-confidence action is justified yet.", False)
    return actions[:6]


def short_topic(signal):
    projects = signal.get("projects") or []
    if projects:
        p = projects[0]
        if isinstance(p, str):
            return p[:120]
        return clean(p.get("name") or p.get("title"))[:120]
    tech = signal.get("technologies") or []
    if tech:
        return ", ".join(map(str, tech[:3]))
    return clean(signal.get("company") or signal.get("author") or "your recent work")


def drafts(signal, target, actions):
    """Conservative drafts only; nothing here is sent automatically."""
    action_names = {x["action"] for x in actions}
    name = target.get("name") or "there"
    first = name.split()[0] if name and name != "there" else "there"
    topic = short_topic(signal)
    company = clean(signal.get("company"))
    out = {}

    if "connect_person" in action_names:
        out["connection_note"] = (
            f"Hi {first}, I came across your work on {topic}. "
            "My background is in flight controls, UAV systems and simulation, and the topic is closely aligned with my work. "
            "I'd be glad to connect."
        )
    if "message_person" in action_names:
        out["message"] = (
            f"Hi {first}, I noticed your recent work around {topic}. "
            "I work on flight controls, GNC, UAV/autonomous systems and simulation. "
            "I thought there may be useful overlap in our technical interests and would be interested in exchanging perspectives."
        )
    if "job_outreach" in action_names:
        where = f" at {company}" if company else ""
        out["job_outreach"] = (
            f"Hi {first}, I'm exploring senior/lead opportunities{where} related to flight controls, GNC, autonomy and simulation. "
            "My background includes UAV/autopilot development, flight dynamics and MATLAB/Simulink/Python. "
            "If there is a relevant team or role, I'd appreciate any direction on whom best to speak with."
        )
    return out


def apply_guard(actions, crm_record):
    if not crm_record:
        return actions, False, None
    status = crm_record.get("status", "WATCH")
    dnc = bool(crm_record.get("do_not_contact"))
    guarded = []
    blocked = []
    for item in actions:
        action = item["action"]
        if action in OUTBOUND:
            if dnc:
                blocked.append(f"{action}:do_not_contact")
                continue
            if action == "connect_person" and status in {"CONNECTED", "CONTACTED", "REPLIED", "CLOSED"}:
                blocked.append(f"{action}:crm_status_{status.lower()}")
                continue
            if action in {"message_person", "job_outreach"} and status not in {"CONNECTED", "CONTACTED", "REPLIED"}:
                blocked.append(f"{action}:not_connected")
                continue
        guarded.append(item)
    if not guarded:
        guarded = [{"action": "no_action" if status == "CLOSED" else "watch", "reason": "CRM guard removed unsafe or duplicate outbound actions.", "approval_required": False}]
    return guarded, bool(blocked), ";".join(blocked) or None


def build_queue(data, crm):
    items = []
    for signal in data.get("signals", []):
        if signal.get("priority") not in {"high", "medium"}:
            continue
        crm_record = match_crm(signal, crm)
        target = target_info(signal)
        research = research_plan(signal, target)
        action_plan = strategy_actions(signal, crm_record, target, research)
        action_plan, blocked, blocked_reason = apply_guard(action_plan, crm_record)
        primary = action_plan[0]["action"]
        draft_set = drafts(signal, target, action_plan)
        approval_required = any(x.get("approval_required") for x in action_plan)
        pending = any(x.get("approval_required") and x["action"] in OUTBOUND for x in action_plan)

        items.append({
            "signal_id": signal.get("signal_id"),
            "source_post_id": signal.get("source_post_id"),
            "priority": signal.get("priority"),
            "confidence": signal.get("confidence"),
            "author": signal.get("author"),
            "company": signal.get("company"),
            # Compatibility fields used by the dashboard and existing consumers.
            "primary_action": primary,
            "secondary_action": action_plan[1]["action"] if len(action_plan) > 1 else None,
            "approval_required": approval_required,
            "execution_status": "PENDING_APPROVAL" if pending else "READY",
            # Strategy v3.
            "relationship_stage": relationship_stage(crm_record),
            "action_plan": action_plan,
            "research": research,
            "drafts": draft_set,
            "crm_guard_applied": blocked,
            "crm_guard_reason": blocked_reason,
            "crm_entity_id": crm_record.get("id") if crm_record else None,
            "crm_status": crm_record.get("status") if crm_record else None,
            "do_not_contact": bool(crm_record.get("do_not_contact")) if crm_record else False,
            "target": target,
            "reason": signal.get("why_relevant"),
            "content_angle": signal.get("content_angle"),
            "source_url": signal.get("source_url"),
            "analysis_engine": signal.get("analysis_engine"),
            "llm": signal.get("llm"),
        })

    rank = {"high": 0, "medium": 1}
    items.sort(key=lambda x: (rank.get(x.get("priority"), 9), -(x.get("confidence") or 0)))
    action_names = sorted({a["action"] for x in items for a in x.get("action_plan", [])})
    return {
        "schema_version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_generated_at": data.get("generated_at"),
        "policy": {
            "outbound_requires_human_approval": True,
            "outbound_actions": sorted(OUTBOUND),
            "automatic_outbound_enabled": False,
            "relationship_flow": ["observe", "research", "engage", "connect", "contact", "job_outreach"],
        },
        "summary": {
            "items": len(items),
            "high": sum(1 for x in items if x.get("priority") == "high"),
            "medium": sum(1 for x in items if x.get("priority") == "medium"),
            "pending_approval": sum(1 for x in items if x.get("execution_status") == "PENDING_APPROVAL"),
            "research_needed": sum(1 for x in items if (x.get("research") or {}).get("needed")),
            "primary_action_counts": {a: sum(1 for x in items if x.get("primary_action") == a) for a in sorted({x.get("primary_action") for x in items})},
            "action_plan_counts": {a: sum(1 for x in items if any(p.get("action") == a for p in x.get("action_plan", []))) for a in action_names},
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
    print(
        f"OK action_items={queue['summary']['items']} "
        f"pending_approval={queue['summary']['pending_approval']} "
        f"research_needed={queue['summary']['research_needed']} -> {args.output}"
    )


if __name__ == "__main__":
    main()
