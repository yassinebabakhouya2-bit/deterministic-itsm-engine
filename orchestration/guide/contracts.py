"""Pydantic contracts of the guided resolution engine (all models forbid unknown fields)."""
from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Phase(str, Enum):
    LOCATE = "LOCATE"        # finding the exact fiche
    GUIDING = "GUIDING"      # walking through the steps
    SOLVED = "SOLVED"        # user confirmed the resolution (only terminal phase)
    OPEN = "OPEN"            # no fiche matches: free-form diagnostic answer instead (not terminal)
    STUCK = "STUCK"          # the free-form fallback itself failed: waiting for a new description (not terminal)


TERMINAL = (Phase.SOLVED,)
SourceSystem = Literal["servicenow_kb", "sharepoint"]
RiskFlag = Literal["privileged_access", "data_deletion", "mfa_reset", "security_incident"]
ACTION_RE = re.compile(r"^(pick:[1-3]|start|done|blocked|explain|back|wrong_fiche|solved_yes|solved_no|none)$")


class Variable(Strict):
    name: Literal["application", "os_family", "os_version", "error_code",
                  "scope", "tenant_id", "device_type", "symptom"]
    value: str = Field(max_length=256)
    source: Literal["ticket_text", "user_reply", "ocr", "ticket_meta"]
    confidence: float = Field(ge=0, le=1)
    confirmed: bool = False


class OcrFinding(Strict):
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["error_code", "dialog_title", "dialog_text", "path",
                  "cli_output", "ui_state", "guid"]
    text: str = Field(max_length=512)
    ocr_confidence: float = Field(ge=0, le=1)
    verified: bool


class KbCandidate(Strict):
    parent_id: str
    title: str
    reranker_score: float
    chunk_ids: list[str] = []
    source_system: SourceSystem = "servicenow_kb"
    source_url: str = ""
    excerpt: str = Field("", max_length=4000)


class GuideStep(Strict):
    order: int = Field(ge=1)
    title: str = Field(max_length=100)
    instruction: str = Field(max_length=700)
    source_chunk_id: str
    verbatim_from_kb: bool = False


class Guide(Strict):
    parent_id: str
    title: str
    source_system: SourceSystem = "servicenow_kb"
    source_url: str = ""
    summary: str = Field("", max_length=500)
    preconditions: list[str] = []
    steps: list[GuideStep] = Field(min_length=1, max_length=25)
    verification: list[str] = []
    origin: Literal["llm", "fallback"] = "llm"
    approximate: bool = False            # fiche chosen automatically after the clarification rounds
    guide_sha256: str = ""


class Choice(Strict):
    parent_id: str
    title: str
    score: float


class OpenTurn(Strict):
    """One exchange of the free-form fallback (orchestration/answer.py's diagnostic_query_core_keyless,
    the classic assistant's own engine), kept so the next turn's prompt carries this conversation's
    history -- same shape app/app.py already builds from its own conversation table, trimmed to what
    _build_diagnostic_state_block actually reads."""
    query: str = Field("", max_length=1000)
    answer: str = Field("", max_length=1500)
    primary_title: Optional[str] = None
    error_codes: list[str] = []
    screen_reading: Optional[str] = Field(None, max_length=500)


class GuideState(Strict):
    session_id: str
    ticket_id: Optional[str] = None
    client_id: str
    origin: Literal["servicenow", "app"] = "app"
    phase: Phase = Phase.LOCATE
    created_utc: datetime
    conversation_text: str = Field("", max_length=4000)
    variables: list[Variable] = []
    ocr_findings: list[OcrFinding] = []
    risk_flags: list[RiskFlag] = []
    candidates: list[KbCandidate] = []
    choices: list[Choice] = []
    rejected_parent_ids: list[str] = []
    locate_rounds: int = Field(0, ge=0)
    selected_parent_id: Optional[str] = None
    guide: Optional[Guide] = None
    current_step: int = Field(0, ge=0)        # == len(steps) means "all done, verify"
    # False while the chosen fiche is only shown (title, summary, steps), before the user starts it;
    # True by default so a session stored before this field existed keeps walking its steps.
    steps_started: bool = True
    step_attempts: int = Field(0, ge=0)
    open_turns: list[OpenTurn] = Field([], max_length=8)
    seen_event_ids: list[str] = []


class Event(Strict):
    event_id: str
    kind: Literal["created", "reply"]
    text: str = ""
    attachments: list[str] = []
    action: Optional[str] = None

    @field_validator("action")
    @classmethod
    def _action(cls, v):
        if v is not None and not ACTION_RE.match(v):
            raise ValueError("unknown action")
        return v
