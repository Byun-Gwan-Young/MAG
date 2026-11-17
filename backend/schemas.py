"""Public session metadata; raw configuration and model text are omitted."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from storage.accounts import SELECTABLE_PROVIDER_IDS


def _check_provider(value):
    """C-19-8P : a provider id from the list (never 'custom', never an address). None = not sent."""
    if value is None:
        return value
    if not isinstance(value, str) or value not in SELECTABLE_PROVIDER_IDS:
        raise ValueError("provider must be one of the listed provider ids")
    return value


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    mode: Literal["observe", "block"] = "observe"
    # C-19-8P : left out = the server default provider (the old behaviour).
    provider: str | None = Field(default=None, max_length=32)
    csrf_token: str = Field(min_length=1, max_length=256)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    _provider = field_validator("provider")(_check_provider)


class ProjectResponse(BaseModel):
    """Project metadata. The provider id is shown ; its address (upstream_url) never is."""
    id: int
    name: str
    mode: str
    rule_id: str
    provider: str | None = None
    created_at: datetime
    # C-19-9b : the project's utterance log (off by default ; on = LLM response excerpts kept 7 days).
    utterance_log: bool = False


class ProviderResponse(BaseModel):
    """C-19-8P : one provider choice. No address and no server setting."""
    id: str
    name: str
    group: Literal["global", "korea", "team"]
    available: bool


class CsrfRequest(BaseModel):
    """C-19-8K : a state-changing request that carries only the session's CSRF token."""
    model_config = ConfigDict(extra="forbid")

    csrf_token: str = Field(min_length=1, max_length=256)


class ProjectModeRequest(CsrfRequest):
    """PATCH /api/projects/{id} : the mode (C-19-8K), the provider (C-19-8P) and/or the utterance log (C-19-9b),
    at least one."""
    mode: Literal["observe", "block"] | None = None
    provider: str | None = Field(default=None, max_length=32)
    utterance_log: bool | None = None

    _provider = field_validator("provider")(_check_provider)

    @model_validator(mode="after")
    def one_change(self):
        if self.mode is None and self.provider is None and self.utterance_log is None:
            raise ValueError("mode, provider or utterance_log is required")
        return self


class ApiKeyIssueRequest(CsrfRequest):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class ApiKeyResponse(BaseModel):
    """Key metadata only : never the raw key or its hash."""
    id: int
    project_id: int
    key_prefix: str
    name: str | None
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    revoked: bool


class ApiKeyIssueResponse(ApiKeyResponse):
    # The raw key, in this one response only (C-19-8K, DM-92 ③). Not stored, not logged.
    api_key: str


class ApiKeyRevokeResponse(BaseModel):
    id: int
    revoked: bool


class SessionDetailResponse(BaseModel):
    id: str
    scenario_id: int
    status: str
    termination_reason: str | None
    phase: str | None
    guardrail_mode: str | None
    model: str | None
    steps_reached: int | None
    total_tokens: int | None
    first_stop_step: int | None
    started_at: datetime
    ended_at: datetime | None


class SessionListResponse(BaseModel):
    items: list[SessionDetailResponse]
    total: int
    limit: int
    offset: int
