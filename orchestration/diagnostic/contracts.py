"""Pydantic contracts of the diagnostic engine (section D of the architecture
document). Every model forbids unknown fields; the same models produce the JSON
Schema sent to Azure OpenAI Structured Outputs."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TURNS = 4
CONF_THRESHOLD = 0.95


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FsmState(str, Enum):
    INIT_TRIAGE = "INIT_TRIAGE"
    NEED_DIAGNOSTIC_DATA = "NEED_DIAGNOSTIC_DATA"
    OCR_PROCESSING = "OCR_PROCESSING"
    KB_MATCHED = "KB_MATCHED"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    HUMAN_ESCALATION = "HUMAN_ESCALATION"


TERMINAL = (FsmState.ACTION_PROPOSED, FsmState.HUMAN_ESCALATION)
SourceSystem = Literal["servicenow_kb", "sharepoint"]
RiskFlag = Literal["privileged_access", "data_deletion", "mfa_reset", "security_incident"]


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
    bbox: tuple[int, int, int, int]
    verified: bool


class KbCandidate(Strict):
    parent_id: str
    title: str
    reranker_score: float
    chunk_ids: list[str] = []
    source_system: SourceSystem = "servicenow_kb"
    source_url: str = ""
    excerpt: str = Field("", max_length=1500)   # best matching passage, shown as the illustrating source


class TurnRecord(Strict):
    turn: int = Field(ge=0)
    event_id: Optional[str] = None
    state_before: FsmState
    state_after: FsmState
    question_id: Optional[str] = None
    evidence_hash: str
    confidence: float = Field(ge=0, le=1)


class ChoiceOption(Strict):
    id: str
    label: str = Field(max_length=120)


class UserNextActionPrompt(Strict):
    question_id: str
    kind: Literal["choose_one", "confirm_value", "free_text_short",
                  "request_screenshot", "request_command_output"]
    target_variable: str
    text_fr: str = Field(max_length=400)
    options: list[ChoiceOption] = Field(default=[], max_length=6)
    screenshot_hint: Optional[str] = Field(None, max_length=200)
    why_needed: str = Field(max_length=200)
    discriminates: list[str] = []


class PlanStep(Strict):
    order: int = Field(ge=1)
    instruction: str = Field(max_length=500)
    action_type: Literal["user_instruction", "agent_check", "agent_action"]
    source_chunk_id: str
    verbatim_from_kb: bool
    requires_confirmation: bool = True
    rollback: Optional[str] = None


class FinalExecutionPlan(Strict):
    schema_version: Literal["1.0"] = "1.0"
    ticket_id: Optional[str] = None
    session_id: str
    kb_parent_id: str
    kb_title: str
    kb_version: str
    source_system: SourceSystem
    source_url: str
    confidence: float = Field(ge=0, le=1)      # threshold enforced by the FSM (configurable)
    preconditions: list[str]
    steps: list[PlanStep] = Field(min_length=1, max_length=25)
    verification: list[str]
    closure_code: Literal["resolved_by_procedure", "pending_user_confirmation"]
    evidence_hash: str
    plan_sha256: str


class PlanRefusal(Strict):
    applicable: Literal[False] = False
    reason: str = Field(max_length=300)
    missing_information: list[str] = []


class DiagnosticState(Strict):
    session_id: str
    ticket_id: Optional[str] = None
    client_id: str
    origin: Literal["servicenow", "app"] = "app"
    state: FsmState = FsmState.INIT_TRIAGE
    created_utc: datetime
    deadline_utc: datetime
    turn_count: int = Field(0, ge=0, le=MAX_TURNS)
    max_turns: Literal[4] = MAX_TURNS
    ocr_failures_in_row: int = Field(0, ge=0, le=2)
    plan_failures: int = Field(0, ge=0, le=2)
    stagnant_turns: int = Field(0, ge=0, le=MAX_TURNS + 1)
    pending_attachments: list[str] = []
    conversation_text: str = Field("", max_length=4000)
    variables: list[Variable] = []
    required_variables: list[str] = ["application"]
    missing_variables: list[str] = []
    ocr_findings: list[OcrFinding] = []
    candidates: list[KbCandidate] = []
    selected_parent_id: Optional[str] = None
    rejected_parent_ids: list[str] = []
    confidence: float = Field(0, ge=0, le=1)
    asked_question_ids: list[str] = []
    last_question_target: Optional[str] = None
    last_question_kind: Optional[str] = None
    seen_event_ids: list[str] = []
    history: list[TurnRecord] = []
    risk_flags: list[RiskFlag] = []
    final_plan: Optional[FinalExecutionPlan] = None
    escalation_reason: Optional[str] = None

    @model_validator(mode="after")
    def _invariants(self):
        if self.state == FsmState.HUMAN_ESCALATION and not self.escalation_reason:
            raise ValueError("escalation_reason required")
        if self.state == FsmState.ACTION_PROPOSED and not (self.selected_parent_id and self.final_plan):
            raise ValueError("selected_parent_id and final_plan required")
        return self


class Event(Strict):
    event_id: str
    kind: Literal["created", "reply", "timeout"]
    text: str = ""
    attachments: list[str] = []          # references (blob paths), never raw bytes
