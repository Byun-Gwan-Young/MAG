"""Live PostgreSQL write boundary for the Guardrail service.

Purpose
-------
Persist structured live-session facts without turning Storage into the
deadlock judge, proxy, or dashboard.  Public functions in this module are the
service/backend Storage contract; change their signatures only with a
cross-team contract change.

Important invariants
--------------------
- Only writable user projects receive live traffic; the canonical demo project
  is read-only.
- Session start snapshots mode/provider context.
- Observation for a step must exist before that step's judgement.
- Session-row locks serialize child writes vs. session end.
- Exact retries are idempotent; conflicting retries are rejected.
- Raw model/user text, reasoning bodies, request/response bodies, and all keys
  are forbidden by the persistence boundary.
- ``get_session_progress(..., after_step)`` is a compatibility *step cursor*,
  not a lossless event cursor; late same-step events require a future
  backend/proxy contract.

See ``storage/README.md`` for known limits and integration handoff items.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Jsonb

from storage.db import get_connection
from storage.safety import assert_no_secrets, assert_safe_storage_json, secret_hits


VALID_MODES = {"observe", "block"}

# observation_data is judge *input*. Computed progress/criteria belong only in
# judgements.result_data; keeping these shapes separate prevents two conflicting
# truths for the same step.
_LIVE_OBSERVATION_RESULT_KEYS = {
    "progress",
    "criteria",
    "decision",
    "rule_id",
    "reason",
    "judgement_ns",
    "stagnant_run",
}

# Safety checks live in storage.safety so every Storage writer enforces the same
# persistence boundary.  Keep secret/text filtering centralized; otherwise a new
# provider/key format could be blocked in one writer but accidentally stored by
# another.

def _validate_nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _validate_mode(mode: str) -> str:
    value = _validate_nonempty(mode, "mode").lower()
    if value not in VALID_MODES:
        raise ValueError("mode must be 'observe' or 'block'")
    return value


def _validate_positive_int(value: Any, label: str, *, allow_none: bool = False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be an integer >= 1")
    return value


def _validate_nonnegative_number(value: Any, label: str, *, allow_none: bool = True):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{label} must be a non-negative number")
    return value


def _validate_nonnegative_int(value: Any, label: str, *, allow_none: bool = False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be an integer >= 0")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_equal(left: Any, right: Any) -> bool:
    return _canonical_json(left) == _canonical_json(right)


def _row_dict(cursor, row):
    if row is None:
        return None
    columns = [column.name for column in cursor.description]
    return dict(zip(columns, row))


def _rows_dict(cursor):
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _require_writable_project(cursor, project_id: int, *, lock: bool = True):
    # New live sessions lock the account first and then the project.  The initial
    # unlocked read is used only to discover ownership and preserve exact retry
    # behavior; the authoritative routing/mode snapshot is re-read with
    # ``FOR SHARE`` before a new session is inserted.
    lock_clause = " FOR SHARE" if lock else ""
    cursor.execute(
        f"""
        SELECT id, mode, read_only, provider, owner_user_id
        FROM projects
        WHERE id = %s
          AND deleted_at IS NULL{lock_clause}
        """,
        (project_id,),
    )
    row = cursor.fetchone()
    if row is None or row[2]:
        # A read-only demo project must not receive live traffic ; a deleted project (C-19-9c) is not found.
        raise LookupError("project not found")
    return {
    "id": row[0],
    "mode": row[1],
    "read_only": row[2],
    "provider": row[3],
    "owner_user_id": row[4],
    }


def _require_active_project_owner(cursor, project) -> None:
    """Lock and require an active owner before a new live session is created."""
    owner_user_id = project.get("owner_user_id")

    # Preserve existing behavior for legacy/system-owned projects.
    # Read-only demo projects are already rejected by _require_writable_project().
    if owner_user_id is None:
        return

    cursor.execute(
        """
        SELECT account_status
        FROM users
        WHERE id = %s
        FOR SHARE
        """,
        (owner_user_id,),
    )
    row = cursor.fetchone()

    if row is None:
        raise LookupError("project owner not found")

    if row[0] != "active":
        raise RuntimeError("account is pending deletion")




def _require_live_session(
    cursor, session_id: str, *, active: bool = False, lock: bool = False
):
    # Every live *write* calls this with lock=True.  The row lock serializes
    # child writes against end_live_session(): whichever operation locks the
    # session first commits first; a writer that arrives after end then sees
    # status='ended' and is rejected.  Reads intentionally do not lock.
    lock_clause = " FOR UPDATE" if lock else ""
    cursor.execute(
        f"""
        SELECT id, status, source
        FROM sessions
        WHERE id = %s{lock_clause}
        """,
        (session_id,),
    )
    row = cursor.fetchone()
    if row is None or row[2] != "live":
        raise LookupError("live session not found")
    if active and row[1] != "active":
        raise ValueError("live session is not active")
    return {"id": row[0], "status": row[1], "source": row[2]}


def _scenario_id(cursor, project_id: int, scenario_key: str, scenario_hash: str) -> int:
    cursor.execute(
        """
        SELECT id
        FROM scenarios
        WHERE project_id = %s
          AND scenario_key = %s
          AND scenario_hash = %s
        ORDER BY id
        """,
        (project_id, scenario_key, scenario_hash),
    )
    rows = cursor.fetchall()

    if not rows:
        raise LookupError("scenario not found")
    if len(rows) != 1:
        raise RuntimeError("scenario identity is not unique")
    return rows[0][0]


def _load_live_start_identity(cursor, session_id: str):
    """Load the fields that define an idempotent start_live_session retry."""

    cursor.execute(
        """
        SELECT
            id, scenario_id, status, guardrail_mode, model,
            execution_config, session_data, source, started_at
        FROM sessions
        WHERE id = %s
        """,
        (session_id,),
    )
    return _row_dict(cursor, cursor.fetchone())


def _is_exact_start_retry(existing, scenario_id, mode, model, execution_config) -> bool:
    """Return True only when reusing session_id is a byte-for-byte safe retry.

    Notice that the *current* project provider/mode is not consulted here.  Once
    a live session exists, its own stored snapshot is the historical truth.  This
    means a network retry still succeeds after the user changes project settings,
    while a genuinely different session configuration is rejected.
    """

    return (
        existing is not None
        and existing["source"] == "live"
        and existing["scenario_id"] == scenario_id
        and existing["status"] == "active"
        and existing["guardrail_mode"] == mode
        and existing["model"] == model
        and _json_equal(existing["execution_config"] or {}, execution_config)
        and isinstance(existing.get("session_data"), dict)
        and existing["session_data"].get("import_source") == "live"
        and isinstance(existing["session_data"].get("provider"), str)
        and bool(existing["session_data"].get("provider"))
    )


def _live_start_result(existing):
    return {
        "id": existing["id"],
        "status": existing["status"],
        "scenario_id": existing["scenario_id"],
        "guardrail_mode": existing["guardrail_mode"],
        "model": existing["model"],
        "source": existing["source"],
        "provider": existing["session_data"]["provider"],
        "started_at": existing["started_at"],
    }


LIVE_SCENARIO_SCHEMA_VERSION = "live-gateway-v1"


def ensure_live_scenario(project_id, scenario_key, scenario_hash, name, config):
    """C-19-9 : register (or find) the scenario identity a gateway session starts under.

    ``start_live_session`` needs an exact (project, scenario_key, scenario_hash)
    row (README "Live scenario identity").  The service gateway builds that
    identity from what the caller declared (``X-Guardrail-Scenario`` and the
    session registration) and registers it here before the session starts.
    The row is written by the existing importer upsert (``import_utils.
    upsert_scenario``) : no new SQL.  Read-only / missing projects are refused
    exactly as for live sessions.  Returns the scenario id.
    """

    from storage.import_utils import upsert_scenario

    scenario_key = _validate_nonempty(scenario_key, "scenario_key")
    scenario_hash = _validate_nonempty(scenario_hash, "scenario_hash")
    name = _validate_nonempty(name, "name")
    if len(scenario_key) > 100:
        raise ValueError("scenario_key must be at most 100 characters")
    if not isinstance(config, dict):
        raise ValueError("config must be a dict")
    assert_no_secrets(scenario_key, "scenario_key")
    assert_no_secrets(name, "name")
    assert_safe_storage_json(config, "config")

    with get_connection() as conn:
        with conn.cursor() as cursor:
            _require_writable_project(cursor, project_id, lock=False)
            return upsert_scenario(cursor, project_id, {
                "scenario_key": scenario_key,
                "scenario_hash": scenario_hash,
                "schema_version": LIVE_SCENARIO_SCHEMA_VERSION,
                "name": name,
                "task_kind": None,
                "config": config,
            })


def start_live_session(
    project_id,
    session_id,
    scenario_key,
    scenario_hash,
    mode,
    model,
    execution_config,
):
    """Start a live session using an already-registered exact scenario version.

    Exact retries are idempotent.  Reusing a session_id for a different
    scenario/configuration is rejected instead of silently overwriting history.
    """

    session_id = _validate_nonempty(session_id, "session_id")
    scenario_key = _validate_nonempty(scenario_key, "scenario_key")
    scenario_hash = _validate_nonempty(scenario_hash, "scenario_hash")
    mode = _validate_mode(mode)
    model = _validate_nonempty(model, "model")
    assert_no_secrets(model, "model")

    if execution_config is None:
        execution_config = {}
    if not isinstance(execution_config, dict):
        raise ValueError("execution_config must be a dict")
    assert_safe_storage_json(execution_config, "execution_config")

    with get_connection() as conn:
        with conn.cursor() as cursor:
            # First read is intentionally unlocked: exact retries remain allowed
            # for an already-started session even after its owner requests
            # deletion. New sessions acquire locks below in user -> project order.
            project = _require_writable_project(cursor, project_id, lock=False)
            scenario_id = _scenario_id(cursor, project_id, scenario_key, scenario_hash)

            # Check an already-created session *before* comparing against current
            # project settings.  Idempotent network retries must keep working even
            # if the owner changed provider/mode immediately after the original
            # session started.  The session snapshot is historical truth.
            existing = _load_live_start_identity(cursor, session_id)
            if existing is not None:
                if not _is_exact_start_retry(
                    existing, scenario_id, mode, model, execution_config
                ):
                    raise ValueError(
                        "session_id already exists with different live-session data"
                    )
                return _live_start_result(existing)

            # Lock order for a *new* session is user -> project, matching account
            # purge. This avoids the project -> user / user -> project inversion
            # that could otherwise create a database deadlock.
            _require_active_project_owner(cursor, project)
            locked_project = _require_writable_project(cursor, project_id, lock=True)
            if locked_project["owner_user_id"] != project["owner_user_id"]:
                raise RuntimeError("project ownership changed during live session start")
            project = locked_project

            # The public signature contains mode because the proxy already passes
            # it, but projects.mode is the source of truth for *new* sessions.
            # Reject disagreement instead of silently recording a mode different
            # from the project's current policy.
            if mode != project["mode"]:
                raise ValueError("mode does not match project mode")

            # Snapshot semantic provider identity at session start.  A project may
            # later switch Groq -> KMU -> OpenAI; historical sessions must still
            # say which provider they actually started under.  Credentials and
            # provider API keys are never stored here.
            session_data = {
                "import_source": "live",
                "provider": project["provider"],
            }
            assert_safe_storage_json(session_data, "session_data")

            cursor.execute(
                """
                INSERT INTO sessions (
                    id,
                    scenario_id,
                    status,
                    guardrail_mode,
                    model,
                    execution_config,
                    session_data,
                    validity,
                    source
                )
                VALUES (
                    %s, %s, 'active', %s, %s, %s, %s, 'not_counted', 'live'
                )
                ON CONFLICT (id) DO NOTHING
                RETURNING id, started_at
                """,
                (
                    session_id,
                    scenario_id,
                    mode,
                    model,
                    Jsonb(execution_config),
                    Jsonb(session_data),
                ),
            )
            inserted = cursor.fetchone()

            if inserted is not None:
                return {
                    "id": inserted[0],
                    "status": "active",
                    "scenario_id": scenario_id,
                    "guardrail_mode": mode,
                    "model": model,
                    "source": "live",
                    "provider": project["provider"],
                    "started_at": inserted[1],
                }

            # Another worker may have inserted the same session_id between the
            # pre-check and INSERT.  Re-read after ON CONFLICT and apply the same
            # exact-retry rule so concurrent starts remain deterministic.
            existing = _load_live_start_identity(cursor, session_id)
            if not _is_exact_start_retry(
                existing, scenario_id, mode, model, execution_config
            ):
                raise ValueError("session_id already exists with different live-session data")

            return _live_start_result(existing)


def add_observation(session_id, step, agent_id, observation_data):
    """Persist one structured judge input.

    Same-value retries are accepted.  Conflicting retries for the same
    (session_id, step) are rejected so historical judge input is not rewritten.
    """

    session_id = _validate_nonempty(session_id, "session_id")
    step = _validate_positive_int(step, "step")
    agent_id = None if agent_id is None else _validate_nonempty(agent_id, "agent_id")
    assert_no_secrets(agent_id, "agent_id")

    if not isinstance(observation_data, dict):
        raise ValueError("observation_data must be a dict")
    reserved = sorted(_LIVE_OBSERVATION_RESULT_KEYS.intersection(observation_data))
    if reserved:
        raise ValueError(
            "observation_data must contain judge input only; judgement/result keys are forbidden: "
            f"{reserved}"
        )
    assert_safe_storage_json(observation_data, "observation_data")

    with get_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_session(cursor, session_id, active=True, lock=True)

            cursor.execute(
                """
                INSERT INTO observations (
                    session_id, step, agent_id, observed_at, observation_data
                )
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP, %s)
                ON CONFLICT (session_id, step) DO NOTHING
                RETURNING id, observed_at
                """,
                (session_id, step, agent_id, Jsonb(observation_data)),
            )
            inserted = cursor.fetchone()

            if inserted is not None:
                return {
                    "id": inserted[0],
                    "session_id": session_id,
                    "step": step,
                    "agent_id": agent_id,
                    "observed_at": inserted[1],
                    "observation_data": observation_data,
                }

            cursor.execute(
                """
                SELECT id, agent_id, observed_at, observation_data
                FROM observations
                WHERE session_id = %s AND step = %s
                """,
                (session_id, step),
            )
            existing = _row_dict(cursor, cursor.fetchone())

            if (
                existing is None
                or existing["agent_id"] != agent_id
                or not _json_equal(existing["observation_data"], observation_data)
            ):
                raise ValueError("observation already exists with different data")

            return {
                "id": existing["id"],
                "session_id": session_id,
                "step": step,
                "agent_id": existing["agent_id"],
                "observed_at": existing["observed_at"],
                "observation_data": existing["observation_data"],
            }


def add_judgement(
    session_id,
    step,
    agent_id,
    decision,
    rule_id,
    progress_state,
    stagnant_run,
    reason,
    judgement_ns,
    result_data,
):
    """Persist one progress/criteria result for a live step."""

    session_id = _validate_nonempty(session_id, "session_id")
    step = _validate_positive_int(step, "step")
    agent_id = None if agent_id is None else _validate_nonempty(agent_id, "agent_id")
    decision = _validate_nonempty(decision, "decision")
    rule_id = _validate_nonempty(rule_id, "rule_id")
    assert_no_secrets(agent_id, "agent_id")
    assert_no_secrets(decision, "decision")
    assert_no_secrets(rule_id, "rule_id")

    if progress_state is not None:
        progress_state = _validate_nonempty(progress_state, "progress_state")
        assert_no_secrets(progress_state, "progress_state")
    if stagnant_run is not None:
        stagnant_run = _validate_nonnegative_number(stagnant_run, "stagnant_run", allow_none=True)
        if not isinstance(stagnant_run, int):
            raise ValueError("stagnant_run must be an integer >= 0")
    judgement_ns = _validate_nonnegative_number(judgement_ns, "judgement_ns", allow_none=True)
    if judgement_ns is not None and not isinstance(judgement_ns, int):
        raise ValueError("judgement_ns must be an integer >= 0")

    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string or None")
    assert_no_secrets(reason, "reason")

    if not isinstance(result_data, dict):
        raise ValueError("result_data must be a dict")
    assert_safe_storage_json(result_data, "result_data")

    criteria = result_data.get("criteria")
    progress = result_data.get("progress")
    criteria = criteria if isinstance(criteria, dict) else {}
    progress = progress if isinstance(progress, dict) else {}

    rule_hash = criteria.get("criteria_sha256")
    progress_version = progress.get("version")

    # result_data is the detailed copy of the same judgement.  Reject
    # contradictions instead of storing two different truths for one decision.
    nested_decision = criteria.get("decision")
    if nested_decision is not None and nested_decision != decision:
        raise ValueError("result_data criteria decision does not match decision")

    nested_progress_state = progress.get("state")
    if nested_progress_state is not None and nested_progress_state != progress_state:
        raise ValueError("result_data progress state does not match progress_state")

    nested_rule_id = criteria.get("criteria_version")
    if nested_rule_id is not None and nested_rule_id != rule_id:
        raise ValueError("result_data criteria_version does not match rule_id")

    with get_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_session(cursor, session_id, active=True, lock=True)

            # A judgement is semantically the result of evaluating an
            # observation.  Enforcing this relation here prevents a partial or
            # out-of-order caller from creating a judgement with no judge input.
            cursor.execute(
                """
                SELECT agent_id
                FROM observations
                WHERE session_id = %s AND step = %s
                """,
                (session_id, step),
            )
            observation_row = cursor.fetchone()
            if observation_row is None:
                raise ValueError("judgement requires an observation for the same step")
            if observation_row[0] != agent_id:
                raise ValueError("judgement agent_id must match the observation agent_id")

            cursor.execute(
                """
                INSERT INTO judgements (
                    session_id, step, agent_id, observed_at,
                    decision, rule_id, rule_hash,
                    progress_state, progress_version,
                    stagnant_run, reason, judgement_ns, result_data
                )
                VALUES (
                    %s, %s, %s, CURRENT_TIMESTAMP,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s
                )
                ON CONFLICT (session_id, step, rule_id) DO NOTHING
                RETURNING id, observed_at
                """,
                (
                    session_id,
                    step,
                    agent_id,
                    decision,
                    rule_id,
                    rule_hash,
                    progress_state,
                    progress_version,
                    stagnant_run,
                    reason,
                    judgement_ns,
                    Jsonb(result_data),
                ),
            )
            inserted = cursor.fetchone()

            if inserted is not None:
                return {
                    "id": inserted[0],
                    "session_id": session_id,
                    "step": step,
                    "agent_id": agent_id,
                    "observed_at": inserted[1],
                    "decision": decision,
                    "rule_id": rule_id,
                    "rule_hash": rule_hash,
                    "progress_state": progress_state,
                    "progress_version": progress_version,
                    "stagnant_run": stagnant_run,
                    "reason": reason,
                    "judgement_ns": judgement_ns,
                    "result_data": result_data,
                }

            cursor.execute(
                """
                SELECT
                    id, agent_id, observed_at, decision, rule_hash,
                    progress_state, progress_version,
                    stagnant_run, reason, judgement_ns, result_data
                FROM judgements
                WHERE session_id = %s
                  AND step = %s
                  AND rule_id = %s
                """,
                (session_id, step, rule_id),
            )
            existing = _row_dict(cursor, cursor.fetchone())

            comparable = (
                existing is not None
                and existing["agent_id"] == agent_id
                and existing["decision"] == decision
                and existing["rule_hash"] == rule_hash
                and existing["progress_state"] == progress_state
                and existing["progress_version"] == progress_version
                and existing["stagnant_run"] == stagnant_run
                and existing["reason"] == reason
                and existing["judgement_ns"] == judgement_ns
                and _json_equal(existing["result_data"], result_data)
            )
            if not comparable:
                raise ValueError("judgement already exists with different data")

            return {
                "id": existing["id"],
                "session_id": session_id,
                "step": step,
                "agent_id": existing["agent_id"],
                "observed_at": existing["observed_at"],
                "decision": existing["decision"],
                "rule_id": rule_id,
                "rule_hash": existing["rule_hash"],
                "progress_state": existing["progress_state"],
                "progress_version": existing["progress_version"],
                "stagnant_run": existing["stagnant_run"],
                "reason": existing["reason"],
                "judgement_ns": existing["judgement_ns"],
                "result_data": existing["result_data"],
            }


def add_llm_call(
    session_id,
    call_index,
    step,
    agent_id,
    prompt_tokens,
    completion_tokens,
    reasoning_tokens,
    latency_ms,
    status_code,
    retry_count=None,
    tool_call_count=None,
):
    """Persist LLM-call metadata only; no request/response body is accepted."""

    session_id = _validate_nonempty(session_id, "session_id")
    call_index = _validate_positive_int(call_index, "call_index")
    step = _validate_positive_int(step, "step", allow_none=True)
    agent_id = None if agent_id is None else _validate_nonempty(agent_id, "agent_id")

    prompt_tokens = _validate_nonnegative_number(prompt_tokens, "prompt_tokens", allow_none=True)
    completion_tokens = _validate_nonnegative_number(
        completion_tokens, "completion_tokens", allow_none=True
    )
    reasoning_tokens = _validate_nonnegative_number(
        reasoning_tokens, "reasoning_tokens", allow_none=True
    )
    latency_ms = _validate_nonnegative_number(latency_ms, "latency_ms", allow_none=True)
    status_code = _validate_nonnegative_number(status_code, "status_code", allow_none=True)
    retry_count = _validate_nonnegative_number(retry_count, "retry_count", allow_none=True)
    tool_call_count = _validate_nonnegative_number(
        tool_call_count, "tool_call_count", allow_none=True
    )

    for value, label in (
        (prompt_tokens, "prompt_tokens"),
        (completion_tokens, "completion_tokens"),
        (reasoning_tokens, "reasoning_tokens"),
        (status_code, "status_code"),
        (retry_count, "retry_count"),
        (tool_call_count, "tool_call_count"),
    ):
        if value is not None and not isinstance(value, int):
            raise ValueError(f"{label} must be an integer >= 0")

    with get_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_session(cursor, session_id, active=True, lock=True)

            cursor.execute(
                """
                INSERT INTO llm_calls (
                    session_id, call_index, step, agent_id,
                    prompt_tokens, completion_tokens, reasoning_tokens,
                    latency_ms, status_code, retry_count, tool_call_count
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id, call_index) DO NOTHING
                RETURNING id
                """,
                (
                    session_id,
                    call_index,
                    step,
                    agent_id,
                    prompt_tokens,
                    completion_tokens,
                    reasoning_tokens,
                    latency_ms,
                    status_code,
                    retry_count,
                    tool_call_count,
                ),
            )
            inserted = cursor.fetchone()

            payload = {
                "session_id": session_id,
                "call_index": call_index,
                "step": step,
                "agent_id": agent_id,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "reasoning_tokens": reasoning_tokens,
                "latency_ms": latency_ms,
                "status_code": status_code,
                "retry_count": retry_count,
                "tool_call_count": tool_call_count,
            }

            if inserted is not None:
                return {"id": inserted[0], **payload}

            cursor.execute(
                """
                SELECT
                    id, step, agent_id,
                    prompt_tokens, completion_tokens, reasoning_tokens,
                    latency_ms, status_code, retry_count, tool_call_count
                FROM llm_calls
                WHERE session_id = %s AND call_index = %s
                """,
                (session_id, call_index),
            )
            existing = _row_dict(cursor, cursor.fetchone())

            comparable_keys = (
                "step",
                "agent_id",
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "latency_ms",
                "status_code",
                "retry_count",
                "tool_call_count",
            )
            if existing is None or any(existing[key] != payload[key] for key in comparable_keys):
                raise ValueError("llm_call already exists with different data")

            return {"id": existing["id"], **payload}


def end_live_session(
    session_id,
    termination_reason,
    steps_reached,
    total_tokens,
    first_stop_step,
):
    """Mark a live session ended without changing its end time on exact retries."""

    session_id = _validate_nonempty(session_id, "session_id")
    if termination_reason is not None:
        termination_reason = _validate_nonempty(termination_reason, "termination_reason")
        assert_no_secrets(termination_reason, "termination_reason")

    steps_reached = _validate_nonnegative_int(
        steps_reached, "steps_reached", allow_none=True
    )
    total_tokens = _validate_nonnegative_number(
        total_tokens, "total_tokens", allow_none=True
    )
    first_stop_step = _validate_positive_int(
        first_stop_step, "first_stop_step", allow_none=True
    )

    if total_tokens is not None and not isinstance(total_tokens, int):
        raise ValueError("total_tokens must be an integer >= 0")

    if (
        first_stop_step is not None
        and steps_reached is not None
        and first_stop_step > steps_reached
    ):
        raise ValueError("first_stop_step cannot exceed steps_reached")

    with get_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_session(cursor, session_id, lock=True)

            cursor.execute(
                """
                SELECT
                    status, termination_reason, steps_reached,
                    total_tokens, first_stop_step, ended_at
                FROM sessions
                WHERE id = %s AND source = 'live'
                """,
                (session_id,),
            )
            existing = _row_dict(cursor, cursor.fetchone())
            if existing is None:
                raise LookupError("live session not found")

            if existing["status"] == "ended":
                exact_retry = (
                    existing["termination_reason"] == termination_reason
                    and existing["steps_reached"] == steps_reached
                    and existing["total_tokens"] == total_tokens
                    and existing["first_stop_step"] == first_stop_step
                )
                if not exact_retry:
                    raise ValueError("live session already ended with different data")
                return {
                    "id": session_id,
                    "status": "ended",
                    "termination_reason": existing["termination_reason"],
                    "steps_reached": existing["steps_reached"],
                    "total_tokens": existing["total_tokens"],
                    "first_stop_step": existing["first_stop_step"],
                    "ended_at": existing["ended_at"],
                }

            cursor.execute(
                """
                UPDATE sessions
                SET
                    status = 'ended',
                    termination_reason = %s,
                    steps_reached = %s,
                    total_tokens = %s,
                    first_stop_step = %s,
                    ended_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND source = 'live'
                  AND status = 'active'
                RETURNING ended_at
                """,
                (
                    termination_reason,
                    steps_reached,
                    total_tokens,
                    first_stop_step,
                    session_id,
                ),
            )
            ended_at = cursor.fetchone()[0]

            return {
                "id": session_id,
                "status": "ended",
                "termination_reason": termination_reason,
                "steps_reached": steps_reached,
                "total_tokens": total_tokens,
                "first_stop_step": first_stop_step,
                "ended_at": ended_at,
            }


def get_session_progress(session_id, after_step):
    """Return observations and judgements strictly after ``after_step``.

    ``after_step`` is a compatibility *step cursor*, not a durable event cursor.
    If the backend can observe step N before its judgement for N commits and then
    permanently advances to N, that late judgement can be missed.  The current PM
    contract fixes this function signature, so Storage keeps strict step semantics
    here and documents the limitation instead of silently changing API behavior.
    A future dashboard contract should use event/id cursors (or an explicit step
    completion watermark) when lossless incremental polling is required.

    This intentionally returns a small Storage-level shape; the backend/API layer
    can wrap or rename fields for the dashboard without coupling Storage to a UI
    response contract.
    """

    session_id = _validate_nonempty(session_id, "session_id")
    if isinstance(after_step, bool) or not isinstance(after_step, int) or after_step < 0:
        raise ValueError("after_step must be an integer >= 0")

    with get_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_session(cursor, session_id)

            cursor.execute(
                """
                SELECT
                    step, agent_id, observed_at, observation_data
                FROM observations
                WHERE session_id = %s
                  AND step > %s
                ORDER BY step, id
                """,
                (session_id, after_step),
            )
            observations = _rows_dict(cursor)

            cursor.execute(
                """
                SELECT
                    step, agent_id, observed_at,
                    decision, rule_id, rule_hash,
                    progress_state, progress_version,
                    stagnant_run, reason, judgement_ns, result_data
                FROM judgements
                WHERE session_id = %s
                  AND step > %s
                ORDER BY step, id
                """,
                (session_id, after_step),
            )
            judgements = _rows_dict(cursor)

            return {
                "session_id": session_id,
                "after_step": after_step,
                "observations": observations,
                "judgements": judgements,
            }


def get_session_llm_calls(session_id):
    """C-19-9b : the session's LLM-call token metadata, in call order.

    Only the counted values the gateway already stores (``add_llm_call``) :
    call index, step, prompt and completion tokens.  A NULL token count means
    the provider response carried no usage (shown as "측정 안 됨"), never 0.
    No request/response text exists in ``llm_calls`` to return.
    """

    session_id = _validate_nonempty(session_id, "session_id")
    with get_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_session(cursor, session_id)
            cursor.execute(
                """
                SELECT
                    call_index, step, prompt_tokens, completion_tokens
                FROM llm_calls
                WHERE session_id = %s
                ORDER BY call_index
                """,
                (session_id,),
            )
            return _rows_dict(cursor)


# --- C-19-9b (DM-120) : per-project utterance log, off by default ---------------------------------------------------
# The one place raw model text may be stored : the first UTTERANCE_MAX_CHARS characters of an LLM response body
# (assistant content, or narrative for our output contract), only for a project whose owner turned utterance_log on,
# kept UTTERANCE_RETENTION_DAYS days. Reasoning text, request bodies and keys are never accepted here.
UTTERANCE_MAX_CHARS = 1000
UTTERANCE_RETENTION_DAYS = 7


def _purge_expired_utterances(cursor) -> int:
    cursor.execute("DELETE FROM utterances WHERE expires_at <= CURRENT_TIMESTAMP")
    return cursor.rowcount if isinstance(getattr(cursor, "rowcount", None), int) else 0


def purge_expired_utterances() -> int:
    """Delete utterances past their 7 days; returns how many rows went. Also run on every write and read below."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            return _purge_expired_utterances(cursor)


def add_utterance(session_id, call_index, step, agent_id, text) -> bool:
    """Store one LLM response excerpt for a live session whose project has utterance_log on.

    Returns False and stores nothing when the project's setting is off (the default), the text is empty, or the
    text carries a key pattern (it is dropped, not cleaned). The setting is read in the same transaction as the
    insert, so turning it off stops storage at once. ``text`` is cut to UTTERANCE_MAX_CHARS. An exact retry of the
    same call index is ignored.
    """

    session_id = _validate_nonempty(session_id, "session_id")
    call_index = _validate_positive_int(call_index, "call_index")
    step = _validate_positive_int(step, "step", allow_none=True)
    if agent_id is not None:
        agent_id = _validate_nonempty(agent_id, "agent_id")
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    excerpt = text.strip()[:UTTERANCE_MAX_CHARS]
    if not excerpt or secret_hits(excerpt):
        return False

    with get_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_session(cursor, session_id, lock=True)
            cursor.execute(
                """
                SELECT p.utterance_log
                FROM sessions s
                JOIN scenarios sc ON sc.id = s.scenario_id
                JOIN projects p ON p.id = sc.project_id
                WHERE s.id = %s
                  AND p.read_only = FALSE
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            _purge_expired_utterances(cursor)
            if row is None or row[0] is not True:
                return False
            cursor.execute(
                """
                INSERT INTO utterances (
                    session_id, call_index, step, agent_id, excerpt, expires_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    CURRENT_TIMESTAMP + make_interval(days => %s)
                )
                ON CONFLICT (session_id, call_index) DO NOTHING
                """,
                (session_id, call_index, step, agent_id, excerpt, UTTERANCE_RETENTION_DAYS),
            )
            return True


def list_session_utterances(session_id):
    """The session's stored, unexpired utterance excerpts in call order ([] when the project never turned it on)."""

    session_id = _validate_nonempty(session_id, "session_id")
    with get_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_session(cursor, session_id)
            _purge_expired_utterances(cursor)
            cursor.execute(
                """
                SELECT
                    call_index, step, agent_id, excerpt, created_at, expires_at
                FROM utterances
                WHERE session_id = %s
                  AND expires_at > CURRENT_TIMESTAMP
                ORDER BY call_index
                """,
                (session_id,),
            )
            return _rows_dict(cursor)
