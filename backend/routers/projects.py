import secrets

from fastapi import APIRouter, Depends, HTTPException
from psycopg import Connection, Error as DatabaseError

from storage import accounts
from storage.safety import assert_no_secrets

from backend.db import get_db
from backend.auth.dependencies import get_current_user
from backend.repositories.projects import fetch_projects
from backend.schemas import ProjectCreateRequest, ProjectResponse, ProviderResponse


router = APIRouter(prefix="/api/projects", tags=["projects"])
providers_router = APIRouter(tags=["projects"])


@providers_router.get("/api/providers", response_model=list[ProviderResponse])
def list_providers(user=Depends(get_current_user)):
    """C-19-8P : the LLM providers a project may forward to. Ids, names and availability only (no address)."""
    return accounts.list_providers()


@router.get("", response_model=list[ProjectResponse])
def list_projects(user=Depends(get_current_user), conn: Connection = Depends(get_db)):
    return fetch_projects(conn, user["id"])


@router.post("", response_model=ProjectResponse, status_code=201)
def create_project(payload: ProjectCreateRequest, user=Depends(get_current_user)):
    if not secrets.compare_digest(user["csrf_token"].encode(), payload.csrf_token.encode()):
        raise HTTPException(403, "Invalid CSRF token")
    try:
        assert_no_secrets(payload.name, "project name")
    except ValueError as exc:
        raise HTTPException(422, "Invalid project name") from exc
    if payload.provider is not None and not accounts.provider_available(payload.provider):
        # In the list but not usable here (kmu without its server address).
        raise HTTPException(422, "Provider not available")
    # C-19-8P : no provider sent = the server default, through the exact old call.
    extra = () if payload.provider is None else (payload.provider,)
    try:
        # Storage locks and rechecks the account in the insert transaction,
        # serializing project creation against withdrawal and final deletion.
        return accounts.create_project(user["id"], payload.name, payload.mode, *extra)
    except LookupError as exc:
        raise HTTPException(401, "Login required") from exc
    except ValueError as exc:
        # User-controlled name/mode were validated above. Remaining validation
        # failures can be server provider/URL settings; do not echo them.
        raise HTTPException(503, "Project creation is not configured or unavailable") from exc
    except RuntimeError as exc:
        if str(exc) == "account is pending deletion":
            raise HTTPException(403, "Account recovery required") from exc
        raise HTTPException(503, "Project creation is not configured or unavailable") from exc
    except DatabaseError as exc:
        raise HTTPException(503, "Project database unavailable") from exc
