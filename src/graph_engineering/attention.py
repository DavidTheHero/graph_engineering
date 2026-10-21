"""What is waiting for a person, and exactly which actions the controller will accept now.

The "Needs you" view renders this projection instead of the full ticket detail. Each item
names one decision, carries only the facts needed to make it, and lists actions that map
1:1 onto the existing operator commands (``/api/centre/intake-action`` and
``/api/centre/ticket-action``). The server still validates every command; this module only
avoids offering actions that would be refused. Tickets that are running, queued, finished
or cancelled are never listed.
"""
from __future__ import annotations

from .report_store import INVESTIGATION_BUDGET
from .routing import role_label

INTAKE = "/api/centre/intake-action"
TICKET = "/api/centre/ticket-action"
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _short(text, limit=220):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _action(action_id, label, endpoint, payload, *, input=None, primary=False, help=None, confirm=None):
    """``input``: None, 'text' (free text into payload['text']), 'answers' (one answer per question,
    joined into payload['text']), 'actor' (approver name into payload['actor']) or 'consent'
    (explicit checkbox into payload[<consent key>])."""
    return {"id": action_id, "label": label, "endpoint": endpoint, "payload": payload, "input": input,
            "primary": primary, "help": help, "confirm": confirm}


def coverage_lines(state):
    """Which acceptance criteria this delivery covers; the rest continue in a follow-up ticket (never silently)."""
    from .accepted_development import coverage_summary
    summary = coverage_summary(state)
    if not summary["total"] or not summary["remaining"]:
        return []
    return [f"Delivers acceptance criteria {', '.join(map(str, summary['covered'])) or 'none'} of {summary['total']}; remaining: "
            + "; ".join(f"{n}. {t}" for n, t, _ in summary["remaining"]) + " (a follow-up ticket is created after delivery)"]


def item(ticket, *, approved_head=None, original=None, automatic_development=False, automatic_recovery=False, automatic_acceptance=False):
    """The attention item for one ticket row (``Tickets.list()`` shape), or None if nothing is needed."""
    state, status, node = ticket["state"], ticket["status"], ticket["node"]
    # Working, queued or cancelled tickets need nobody right now. A paused merge approval means the operator
    # rejected it; paused blocked/failed tickets still need a retry decision, so they stay.
    if ticket.get("lease_owner") or ticket.get("cancel") or status in ("cancelled", "ready", "running"):
        return None
    if ticket.get("paused") and status not in ("blocked", "escalated", "failed"):
        return None
    # The repository policy will act on these itself on the next scheduler sweep: nobody needs to.
    if automatic_recovery and status in ("blocked", "escalated"):
        return None
    if automatic_acceptance and status == "ready_for_review":
        return None
    intake = state["config"].get("run_mode") == "intake"
    developing = state["config"].get("run_mode") == "accepted_development"
    draft = state.get("enhanced") or {}
    revision = state.get("revision", 0)
    base = {"ticket_id": ticket["id"], "run_id": ticket["run_id"], "priority": ticket["priority"],
            "title": draft.get("title") or (original or {}).get("original", "")[:120] or state.get("candidate", {}).get("title")
                     or "Repository workflow",
            "waiting_since": ticket.get("run_updated"), "context": [], "questions": [], "actions": []}
    intake_payload = {"ticket_id": ticket["id"], "revision": revision, "actor": "Local operator"}
    generation = state.get("intake", {}).get("generation", 0)
    ask_label = "Ask " + role_label(state["config"], "question_answering", "the model")

    def intake_action(action_id, label, **kw):
        return _action(action_id, label, INTAKE, intake_payload | {"action": action_id}, **kw)

    if intake and status == "needs_information":
        answers = {a["question"]: a for a in (state.get("question_answers") or {}).get("answers", [])}
        recorded = state.get("question_answers") or {}
        # The registry label only while that key still names the model that answered (keys can be repointed).
        entry = state["config"].get("models", {}).get(recorded.get("model_key"), {})
        proposer = (entry.get("label") if entry.get("model_id") == recorded.get("model_id") else None) \
            or recorded.get("model_id") or "Model"
        questions = [{"text": q, "proposal": answers[q] | {"model": proposer} if q in answers else None}
                     for q in draft.get("questions", [])]
        limitations = [_short(x, 160) for x in draft.get("limitations", [])[:2]]
        actions = []
        budget_left = generation < INVESTIGATION_BUDGET
        if questions and budget_left:  # The server refuses clarifications once the budget is used.
            actions.append(intake_action("clarify", "Send answers", input="answers", primary=True,
                                         help="Your answers are recorded as operator input and investigation continues."))
        if budget_left:
            if questions:
                actions.append(intake_action("answer_questions", ask_label,
                                             help="Proposes answers from the code; only verified references are used."))
            actions.append(intake_action("reinvestigate", "Investigate further", input="text",
                                         help="Optional note; reruns the repository investigation."))
        actions.append(intake_action("reject_enhancement", "Reject", confirm="Reject this ticket's enhancement?"))
        return base | {
            "kind": "clarification",
            "headline": ("Investigation budget used: reject, or create a scoped follow-up ticket" if not budget_left else
                         f"Answer {len(questions)} question{'s' * (len(questions) != 1)} so investigation can continue"
                         if questions else "Investigation needs more information"),
            # Without budget the answer boxes would have no action to submit them.
            **({"questions_readonly": True} if not budget_left else {}),
            "context": [c for c in [f"Hypothesis: {_short(draft.get('root_cause_hypothesis'))}" if draft.get("root_cause_hypothesis") else None,
                                    *[f"Limitation: {x}" for x in limitations],
                                    f"Further investigations used: {generation}/{INVESTIGATION_BUDGET}"] if c],
            "questions": questions, "actions": actions}

    if intake and status == "duplicate_review":
        candidates = state.get("duplicate_candidates", [])
        actions = [intake_action("mark_duplicate", f"Duplicate of {c['ticket_id']}") | {
            "payload": intake_payload | {"action": "mark_duplicate", "related_ticket": c["ticket_id"]}} for c in candidates[:3]]
        actions.append(intake_action("reinvestigate", "Not a duplicate: investigate", primary=True))
        return base | {"kind": "duplicate_review", "headline": "Possible duplicate: confirm or continue",
                       "context": [f"{c['ticket_id']}: {int(c['similarity'] * 100)}% wording overlap" for c in candidates[:3]],
                       "actions": actions}

    if intake and status in ("ready_for_review", "enhancement_rejected"):
        validation = state.get("finding_validation") or {}
        valid = bool(validation.get("source_references_valid") and validation.get("verified_references"))
        actions = []
        if status == "ready_for_review":
            actions.append(intake_action("accept", "Accept", primary=True,
                                         help=None if valid else "Unavailable: source references are not verified"))
            if not valid:
                actions[-1]["disabled"] = True
        if generation < INVESTIGATION_BUDGET:
            actions.append(intake_action("reinvestigate", "Investigate further", input="text"))
        if status == "ready_for_review":
            actions.append(intake_action("reject_enhancement", "Reject", confirm="Reject this enhancement?"))
        if not actions:
            return None  # Rejected with no budget left: the operator's decision is final here.
        return base | {
            "kind": "review",
            "headline": "Review the enhanced ticket" if status == "ready_for_review" else "Enhancement rejected: choose the next step",
            "context": [c for c in [_short(draft.get("problem_statement")),
                                    f"Hypothesis: {_short(draft.get('root_cause_hypothesis'))}" if draft.get("root_cause_hypothesis") else None,
                                    f"Verified source references: {validation.get('verified_references', 0)}"
                                    + ("" if valid else " (accept unavailable until references verify)"),
                                    *[f"Acceptance: {_short(x, 160)}" for x in draft.get("acceptance_criteria", [])[:3]],
                                    *[f"Open question: {_short(q, 160)}" for q in draft.get("questions", [])[:3]]] if c],
            "actions": actions}

    if intake and status in ("ready_for_development", "development_queued") and not automatic_development:
        actions = []
        if status == "ready_for_development":
            actions.append(intake_action("send_development", "Send to development queue", primary=True))
        else:
            actions.append(intake_action("start_development", "Start development", input="consent", primary=True,
                                         help="I authorise isolated code execution and implementation for this accepted ticket")
                           | {"consent_key": "trust_execution"})
        return base | {"kind": "start_development",
                       "headline": "Accepted: authorise development" if status == "development_queued"
                                   else "Accepted: send to the development queue",
                       "context": [f"Accepted revision {state.get('accepted_revision')}: {_short(draft.get('title'), 160)}",
                                   "Publication and merging need separate approval later."],
                       "actions": actions}

    if status == "ready_for_delivery":
        github = state["config"].get("backend") == "github"
        return base | {"kind": "publication",
                       "headline": "Approve publication of the tested change" if github else "Approve the local PR preview",
                       "context": [f"Tested head: {state.get('head', '')[:12]}",
                                   f"Independent review: {state.get('review', {}).get('decision', 'unknown')}",
                                   *coverage_lines(state),
                                   "Publishes to GitHub; merge approval stays separate." if github
                                   else "Local simulation only; nothing is published to GitHub."],
                       "actions": [intake_action("publish_development", "Approve publication", input="actor", primary=True)
                                   | {"payload": intake_payload | {"action": "publish_development", "approve_publication": True}},
                                   intake_action("reject_delivery", "Reject publication", input="text",
                                                 confirm="Reject this tested change? Nothing is published; the ticket returns to review with your reason.")]}

    if status == "waiting_approval" and approved_head != state.get("head"):
        verification = state.get("verification", {})
        return base | {"kind": "merge_approval", "headline": "Approve merging this exact tested commit",
                       "context": [f"Commit: {state.get('head')}",
                                   f"Tests: {'passed' if verification.get('passed') else 'not passed'}; "
                                   f"review: {state.get('review', {}).get('decision', 'unknown')}",
                                   f"Changed files: {', '.join(state.get('plan', {}).get('files', [])[:4])}",
                                   *([f"Existing tests adapted to the accepted change: {', '.join(state['plan']['adapted_tests'])}"]
                                     if state.get('plan', {}).get('adapted_tests') else []),
                                   *coverage_lines(state),
                                   "Checks and repository policy are re-verified before the merge."],
                       "actions": [_action("approve", "Approve commit", TICKET,
                                           {"ticket_id": ticket["id"], "action": "approve", "head": state.get("head")},
                                           input="actor", primary=True),
                                   _action("reject", "Reject", TICKET, {"ticket_id": ticket["id"], "action": "reject"},
                                           confirm="Reject and pause this change?")]}

    if status == "scanned" and state["config"].get("run_mode") == "scan":
        return base | {"kind": "scan_decision", "headline": "Validated finding: allow development?",
                       "context": [_short(state.get("candidate", {}).get("actual") or state.get("candidate", {}).get("title"))],
                       "actions": [_action("develop", "Continue development", TICKET,
                                           {"ticket_id": ticket["id"], "action": "develop"}, input="consent", primary=True,
                                           help="I approve local execution and scoped implementation for this finding")
                                   | {"consent_key": "trust_execution"}]}

    if status in ("blocked", "escalated", "failed"):
        error = state.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        retry_left = ticket["retries"] < ticket["max_retries"]
        actions = []
        if retry_left:
            actions.append(_action("retry", "Retry with backoff", TICKET, {"ticket_id": ticket["id"], "action": "retry"},
                                   primary=True))
        if developing and draft.get("questions"):
            actions.append(intake_action("answer_questions", ask_label))
        notes = state.get("development_information", []) if developing else []
        if developing:
            actions.append(intake_action("development_information", "Add troubleshooting information", input="text"))
            actions.append(intake_action("revise_requirement", "Revise requirement", primary=not retry_left,
                                         help="Returns the ticket to review so you can edit the acceptance criteria and "
                                              "open questions. Your notes are kept; retries reset after you accept again.",
                                         confirm="Stop development and return this ticket to review?"))
        return base | {"kind": "blocked" if retry_left and status == "blocked" else "dead_letter",
                       "headline": f"Stopped at {node.replace('_', ' ')}" + ("" if retry_left else ": retry limit reached"),
                       "context": [c for c in [_short(message, 300) if message else None,
                                               f"Retries used: {ticket['retries']}/{ticket['max_retries']}",
                                               *[f"Your note: {_short(n['text'], 200)}" for n in notes[-2:]],
                                               ("Notes guide the next attempt but do not change the accepted criteria; "
                                                "use Revise requirement if the criteria are wrong.") if notes else None,
                                               None if retry_left or developing else "Review the failure and create a scoped follow-up ticket."] if c],
                       "actions": actions}
    return None


def items(tickets, rows=None) -> list[dict]:
    """Every ticket waiting for a person, most urgent first (priority, then longest waiting).

    Pass ``rows`` (already loaded ``Tickets.list()``) to stay inside the caller's read transaction.
    """
    from .autonomy import acceptable, policy, recoverable
    result, automatic_by_repository, policies = [], {}, {}
    sweeping = not tickets.setting("paused", False)  # A paused scheduler sweeps nothing: nothing is hidden then.
    originals = dict(tickets.db.execute("SELECT ticket_id, original FROM ticket_reports").fetchall())  # One query.
    for ticket in rows if rows is not None else tickets.list():
        repository_id = ticket["state"].get("intake", {}).get("repository_id")
        if repository_id and repository_id not in policies:
            policies[repository_id] = policy(tickets, repository_id)
            automatic_by_repository[repository_id] = bool(policies[repository_id].get("development"))
        setting = policies.get(repository_id) or {}
        recovering = bool(sweeping and setting.get("development") and ticket["state"].get("autonomy_recoveries", 0) < setting.get("recovery_rounds", 0)
                          and recoverable(ticket))
        accepting = bool(sweeping and setting.get("auto_accept") and acceptable(ticket))
        found = item(ticket, approved_head=tickets.store.approved_head(ticket["run_id"]),
                     original={"original": originals[ticket["id"]]} if ticket["id"] in originals else None,
                     automatic_development=automatic_by_repository.get(repository_id, False),
                     automatic_recovery=recovering, automatic_acceptance=accepting)
        if found:
            found["repository"] = ticket["state"]["config"].get("source_repo", ticket["repo"])
            result.append(found)
    return sorted(result, key=lambda i: (PRIORITY_ORDER.get(i["priority"], 9), i.get("waiting_since") or ""))
