"""C-19-8K : guardrail API keys (issue · list · revoke) and the project mode (observe · block).

Everything goes through storage/accounts.py (no SQL here). The raw key leaves the server once, in the issue response
(never stored, logged, or put in an error). Someone else's project or key is 404 ; the shared read-only demo project
is 403. State-changing requests carry the session's CSRF token, as POST /api/projects does.
"""

import secrets

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from psycopg import Error as DatabaseError

from storage import accounts
from storage.safety import assert_no_secrets

from backend.auth.dependencies import get_current_user
from backend.schemas import (ApiKeyIssueRequest, ApiKeyIssueResponse, ApiKeyResponse, ApiKeyRevokeResponse,
                             CsrfRequest, ProjectModeRequest, ProjectResponse)


router = APIRouter(tags=["keys"])

MAX_ID = 2**63 - 1   # BIGINT ids


def _check_csrf(user, token: str) -> None:
    if not secrets.compare_digest(user["csrf_token"].encode(), token.encode()):
        raise HTTPException(403, "Invalid CSRF token")


def _storage_error(exc: Exception) -> HTTPException:
    """Map a storage failure to a status without echoing its message."""
    if isinstance(exc, LookupError):
        if str(exc) == "user not found":
            return HTTPException(401, "Login required")
        return HTTPException(404, "Not found")
    if isinstance(exc, RuntimeError) and str(exc) == "account is pending deletion":
        return HTTPException(403, "Account recovery required")
    return HTTPException(503, "Key service unavailable")


def _call(fn, *args):
    try:
        return fn(*args)
    except (LookupError, RuntimeError, DatabaseError) as exc:
        raise _storage_error(exc) from exc


def _writable_project(user, project_id: int) -> dict:
    """The caller's own project ; 403 for the read-only demo project, 404 for anything else."""
    for project in _call(accounts.list_projects, user["id"]):
        if project["id"] == project_id:
            if project["read_only"]:
                raise HTTPException(403, "The demo project is read-only")
            return project
    raise HTTPException(404, "Project not found")


@router.get("/api/projects/{project_id}/keys", response_model=list[ApiKeyResponse])
def list_keys(project_id: int = Path(..., ge=1, le=MAX_ID), user=Depends(get_current_user)):
    _writable_project(user, project_id)
    return _call(accounts.list_api_keys, project_id, user["id"])


@router.post("/api/projects/{project_id}/keys", response_model=ApiKeyIssueResponse, status_code=201)
def issue_key(payload: ApiKeyIssueRequest, response: Response, project_id: int = Path(..., ge=1, le=MAX_ID),
              user=Depends(get_current_user)):
    _check_csrf(user, payload.csrf_token)
    try:
        assert_no_secrets(payload.name, "api key name")
    except ValueError as exc:
        raise HTTPException(422, "Invalid key name") from exc
    _writable_project(user, project_id)
    raw_key, info = _call(accounts.issue_api_key, project_id, user["id"], payload.name)
    # The only copy of the raw key : no caching anywhere on the way back.
    response.headers["Cache-Control"] = "no-store"
    return {**info, "api_key": raw_key}


@router.post("/api/keys/{key_id}/revoke", response_model=ApiKeyRevokeResponse)
def revoke_key(payload: CsrfRequest, key_id: int = Path(..., ge=1, le=MAX_ID), user=Depends(get_current_user)):
    _check_csrf(user, payload.csrf_token)
    # Storage finds keys of the caller's own writable projects only : any other key (someone else's, the demo
    # project's) is the same 404.
    _call(accounts.revoke_api_key, key_id, user["id"])
    return {"id": key_id, "revoked": True}


@router.patch("/api/projects/{project_id}", response_model=ProjectResponse)
def set_mode(payload: ProjectModeRequest, project_id: int = Path(..., ge=1, le=MAX_ID),
             user=Depends(get_current_user)):
    _check_csrf(user, payload.csrf_token)
    _writable_project(user, project_id)
    _call(accounts.set_project_mode, project_id, user["id"], payload.mode)
    return _writable_project(user, project_id)
