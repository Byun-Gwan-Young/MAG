"""C-19-9 part 3 : the live dashboard API — the signed-in user's gateway sessions from the database.

GET /api/live/sessions        the newest sessions of the user's own projects (all of them, newest first)
GET /api/live/sessions/{id}   one of them with its judged steps, in the React session viewer's shape

The same paths and shapes as the Mac live monitor (tools/demo_live.py, C-19-4), so the React LivePage needs no new
code path ; only the data source differs (the DB rows the service gateway writes through storage.live).
Read-only. Ownership : the user's projects from storage.accounts.list_projects, sessions from the existing backend
repository queries (owner checked in SQL), steps from storage.live.get_session_progress. Someone else's session, the
read-only demo project's records and unknown ids are all 404. No new SQL here. Every response is checked for model
text fields and key patterns before it leaves (a hit is a 500 without the body).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError

from storage import accounts, live
from storage.safety import forbidden_fields, secret_hits

from backend.auth.dependencies import get_current_user
from backend.db import db_connection
from backend.repositories.sessions import fetch_session_detail, fetch_sessions


router = APIRouter(prefix="/api/live", tags=["live"])

LIVE_VERSION = "service-live-v1"
LIST_LIMIT = 20
STOP_DECISIONS = ("deadlock", "termination_failure")
STATE_LABELS = {"baseline": "기준", "progress": "진전", "stagnant": "정체", "oscillating": "되돌림",
                "unmeasured": "측정 안 됨", "declared": "완료 선언"}
STALLED = ("max_steps", "guardrail_stop")


def _iso(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _now():
    return datetime.now(timezone.utc)


def _class(reason):
    # tools/aggregate_demo.termination_class
    if reason is None:
        return None
    if reason in ("success", "escalated"):
        return reason
    return "stalled" if reason in STALLED else "other"


def _guarded(payload):
    if forbidden_fields(payload) or secret_hits(payload):
        return JSONResponse({"detail": "response withheld: model text field or key pattern found"}, status_code=500)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def _projects(user):
    try:
        return [p for p in accounts.list_projects(user["id"]) if not p["read_only"]]
    except LookupError:
        raise HTTPException(401, "Login required") from None
    except RuntimeError:
        raise HTTPException(403, "Account recovery required") from None
    except DatabaseError:
        raise HTTPException(503, "Database unavailable") from None


def _progress(session_id):
    try:
        return live.get_session_progress(session_id, 0)
    except LookupError:
        return None


def _step_row(observation, judgement):
    data = (observation or {}).get("observation_data") or {}
    result = (judgement or {}).get("result_data") or {}
    progress, criteria = result.get("progress") or {}, result.get("criteria") or {}
    artifact = data.get("artifact") or {}
    conversation = data.get("conversation") or {}
    t3 = progress.get("t3") or {}
    explanation = criteria.get("explanation") if criteria.get("decision") in STOP_DECISIONS else None
    state = progress.get("state")
    reverted = (progress.get("flags") or {}).get("revert") or t3.get("global_revert")
    tests = (None if artifact.get("test_pass_count") is None
             else [artifact["test_pass_count"], artifact.get("test_total_count")])
    report = None
    if conversation:
        issues = conversation.get("open_issue_ids")
        report = {"status": conversation.get("status"), "handoff": conversation.get("handoff_to"),
                  "adopted": conversation.get("adopted_items"), "issues": len(issues or []),
                  "issue_ids": list(issues or []), "spec": None, "declared": False, "deliverable": False}
    step = (judgement or observation)["step"]
    agent = (judgement or observation).get("agent_id")
    return {"n": step, "agent": agent, "role": agent, "state": state,
            "label": "되돌림" if reverted else STATE_LABELS.get(state, state),
            "stop": None if explanation is None else {"t": explanation.get("template_id"),
                                                      "text": explanation.get("text")},
            "blocked": False, "status": "completed", "fail": [], "tests": tests, "report": report, "claim": None,
            "tools": 0, "tool_errors": 0, "changed": len(t3.get("changed_paths") or []),
            "paths": list(t3.get("changed_paths") or []),
            # Per-step tokens are not readable from the DB yet (no llm_calls reader in storage.live) : null, not 0.
            "tok": None, "cum": None, "cumc": None}


def _view(project, row, progress, now):
    """One session in the shape of tools/demo_live.session_view (the screen's session plus live fields)."""
    judgements = {j["step"]: j for j in (progress or {}).get("judgements") or []}
    observations = {o["step"]: o for o in (progress or {}).get("observations") or []}
    rows = [_step_row(observations.get(n), judgements.get(n)) for n in sorted(set(judgements) | set(observations))]
    stops = [r["n"] for r in rows if r["stop"]]
    first = next((r for r in rows if r["stop"]), None)
    ended = row["status"] == "ended"
    reason = row["termination_reason"] if ended else None
    # A block-mode session that ended with the runner's guardrail_stop had its next request refused with 403
    # (the proxy refuses every request after the STOP) : that step is the 403 cell.
    blocked = (first["n"] + 1 if first is not None and row["guardrail_mode"] == "block"
               and reason == "guardrail_stop" else None)
    if blocked is not None:
        rows.append({"n": blocked, "agent": None, "role": None, "state": None, "label": "차단 (403)", "stop": None,
                     "blocked": True, "status": "blocked", "fail": [], "tests": None, "report": None, "claim": None,
                     "tools": 0, "tool_errors": 0, "changed": 0, "paths": [], "tok": None, "cum": None, "cumc": None})
    times = [o.get("observed_at") for o in observations.values() if o.get("observed_at")]
    times += [row["started_at"]] if row.get("started_at") else []
    last = max(times) if times else None
    key = row["id"].split("-", 1)[1] if row["id"].startswith(f"p{project['id']}-") else row["id"]
    return {
        "id": row["id"], "short": key[-12:], "session_key": key, "scenario": None, "name": project["name"],
        "title": None, "task": None, "task_name": project["name"],
        # The table shows "task_name · variant" : for a gateway session that is "project · session key".
        "variant": key, "type": None,
        "project_id": project["id"], "provider": project.get("provider"),
        "mode": row["guardrail_mode"], "class": _class(reason), "termination": reason, "escalated_by": None,
        "escalated_by_label": None, "steps": len([r for r in rows if not r["blocked"]]), "max_steps": None,
        "tokens": row["total_tokens"], "credits": None,
        "first_stop": first["n"] if first else None, "template": first["stop"]["t"] if first else None,
        "stop_text": first["stop"]["text"] if first else None, "stop_steps": stops, "blocked_step": blocked,
        "if_stopped": None, "started_at": _iso(row.get("started_at")), "account": None, "plan": None,
        "replay": False, "model": row["model"], "step_rows": rows,
        "live": {"status": "ended" if ended else "running", "lane": None, "lanes": None, "repeat": None,
                 "model": row["model"], "updated_at": _iso(last),
                 "idle_seconds": None if last is None else max(0, round((now - last).total_seconds())),
                 "in_progress": None},
    }


LIST_KEYS = ("id", "short", "session_key", "scenario", "name", "task", "task_name", "variant", "type", "project_id",
             "provider", "mode", "class", "termination", "steps", "max_steps", "tokens", "first_stop", "template",
             "stop_text", "blocked_step", "account", "plan", "started_at", "model")


@router.get("/sessions")
def sessions(limit: int = Query(LIST_LIMIT, ge=1, le=50), user=Depends(get_current_user)):
    projects = _projects(user)
    now = _now()
    found = []
    try:
        with db_connection() as conn:
            for project in projects:
                for row in fetch_sessions(conn, project["id"], limit, 0, user["id"])["items"]:
                    found.append((project, row))
        found.sort(key=lambda pr: (pr[1]["started_at"], pr[1]["id"]), reverse=True)
        views = [_view(project, row, _progress(row["id"]), now) for project, row in found[:limit]]
    except DatabaseError:
        raise HTTPException(503, "Database unavailable") from None
    items = [{**{k: v[k] for k in LIST_KEYS}, "live": {k: v["live"][k] for k in ("status", "updated_at",
                                                                                   "idle_seconds", "in_progress")}}
             for v in views]
    return _guarded({"version": LIVE_VERSION, "now": _iso(now), "sessions": items})


@router.get("/sessions/{session_id}")
def session(session_id: str = Path(..., min_length=1, max_length=200), user=Depends(get_current_user)):
    projects = _projects(user)
    try:
        with db_connection() as conn:
            match = next(((p, row) for p in projects
                          if (row := fetch_session_detail(conn, p["id"], session_id, user["id"])) is not None), None)
        if match is None:
            raise HTTPException(404, "Session not found")
        progress = _progress(session_id)
    except DatabaseError:
        raise HTTPException(503, "Database unavailable") from None
    if progress is None:
        raise HTTPException(404, "Session not found")
    now = _now()
    return _guarded({"version": LIVE_VERSION, "now": _iso(now), "session": _view(*match, progress, now)})
