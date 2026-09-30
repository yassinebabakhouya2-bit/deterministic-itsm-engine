"""Diagnostic service: persistence + one entry point per event. Sits between the web
layer (app/diagnostic.py) and the pure FSM. Stores the state in an Azure Table
(`diagsessions`), one row per session, optimistic concurrency on the ETag.

Images are never persisted: they live in memory for the duration of the request
(same rule as the existing assistant) and only the validated OCR findings (text,
sha256) are kept in the state."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from .contracts import DiagnosticState, Event, FsmState, TERMINAL
from .fsm import Ports, StepResult, advance, escalate

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PART = 30000            # Azure Table string property: 32K chars max
_MAX_PARTS = 4
MAX_MESSAGES = 14
DEADLINE_HOURS = 24

Image = Tuple[str, bytes, str]          # (filename, bytes, mime)


class Conflict(Exception):
    """Another writer updated the session in between (caller may retry)."""


class NotFound(Exception):
    pass


# ---------------------------------------------------------------- stores

def _pack(record: dict, prefix: str, text: str) -> None:
    parts = [text[i:i + _PART] for i in range(0, len(text), _PART)] or [""]
    if len(parts) > _MAX_PARTS:
        raise ValueError(f"{prefix} too large")
    for i in range(_MAX_PARTS):
        record[f"{prefix}{i}"] = parts[i] if i < len(parts) else ""


def _unpack(record: dict, prefix: str) -> str:
    return "".join(record.get(f"{prefix}{i}") or "" for i in range(_MAX_PARTS))


class MemoryStore:
    """Test/local store with the same contract as TableStore."""
    def __init__(self):
        self.rows: Dict[Tuple[str, str], dict] = {}
        self._v = 0

    def get(self, client_id, session_id):
        r = self.rows.get((client_id, session_id))
        return (dict(r), r["_etag"]) if r else (None, None)

    def create(self, record):
        key = (record["client_id"], record["session_id"])
        if key in self.rows:
            raise Conflict("exists")
        self._v += 1
        self.rows[key] = dict(record, _etag=str(self._v))

    def update(self, record, etag):
        key = (record["client_id"], record["session_id"])
        if self.rows[key]["_etag"] != etag:
            raise Conflict("modified")
        self._v += 1
        self.rows[key] = dict(record, _etag=str(self._v))

    def list(self, client_id):
        return [dict(r) for (c, _), r in self.rows.items() if c == client_id]


class TableStore:
    def __init__(self, table_client):
        self.t = table_client

    @staticmethod
    def _entity(rec):
        e = {k: v for k, v in rec.items() if not k.startswith("_")}
        e["PartitionKey"], e["RowKey"] = rec["client_id"], rec["session_id"]
        return e

    @staticmethod
    def _record(e):
        r = {k: v for k, v in dict(e).items() if k not in ("PartitionKey", "RowKey")}
        r["client_id"], r["session_id"] = e["PartitionKey"], e["RowKey"]
        return r

    def get(self, client_id, session_id):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            e = self.t.get_entity(partition_key=client_id, row_key=session_id)
        except ResourceNotFoundError:
            return None, None
        return self._record(e), e.metadata.get("etag")

    def create(self, record):
        from azure.core.exceptions import ResourceExistsError
        try:
            self.t.create_entity(self._entity(record))
        except ResourceExistsError as exc:
            raise Conflict("exists") from exc

    def update(self, record, etag):
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError
        from azure.data.tables import UpdateMode
        try:
            self.t.update_entity(self._entity(record), mode=UpdateMode.REPLACE, etag=etag,
                                 match_condition=MatchConditions.IfNotModified)
        except ResourceModifiedError as exc:
            raise Conflict("modified") from exc

    def list(self, client_id):
        cols = ["RowKey", "userId", "origin", "ticketId", "state", "title", "createdUtc", "updatedUtc",
                "deadlineUtc"]
        return [self._record(e) | {"session_id": e["RowKey"], "client_id": client_id}
                for e in self.t.query_entities("PartitionKey eq @pk", parameters={"pk": client_id},
                                               select=cols)]


# --------------------------------------------------------------- service

class DiagnosticService:
    def __init__(self, store, ports_factory: Callable[[str, Dict[str, Tuple[bytes, str]]], Ports],
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.store = store
        self.ports_factory = ports_factory
        self.now = now

    # ----- public API
    def start(self, *, client_id: str, user_id: str, origin: str, text: str, images: List[Image] = (),
              ticket_id: Optional[str] = None, session_id: Optional[str] = None,
              event_id: Optional[str] = None, required_variables: Optional[List[str]] = None) -> dict:
        session_id = session_id or uuid.uuid4().hex[:24]
        if not SESSION_ID_RE.match(session_id):
            raise ValueError("invalid session id")
        existing, _ = self.store.get(client_id, session_id)
        if existing:                                   # same ticket delivered twice -> same session
            return self.reply(client_id=client_id, session_id=session_id, text=text, images=images,
                              event_id=event_id)
        now = self.now()
        state = DiagnosticState(
            session_id=session_id, ticket_id=ticket_id, client_id=client_id, origin=origin,
            created_utc=now, deadline_utc=now + timedelta(hours=DEADLINE_HOURS),
            **({"required_variables": required_variables} if required_variables else {}))
        record = {"client_id": client_id, "session_id": session_id, "userId": user_id, "origin": origin,
                  "ticketId": ticket_id or "", "title": ((text.strip().splitlines() or [""])[0][:120] or "Capture d'écran"),
                  "createdUtc": now.isoformat()}
        return self._process(record, None, state, [], text, list(images), event_id or f"e-{uuid.uuid4().hex[:12]}",
                             "created", creating=True)

    def reply(self, *, client_id: str, session_id: str, text: str, images: List[Image] = (),
              event_id: Optional[str] = None) -> dict:
        record, etag = self.store.get(client_id, session_id)
        if record is None:
            raise NotFound(session_id)
        state = DiagnosticState.model_validate_json(_unpack(record, "stateJson"))
        messages = json.loads(_unpack(record, "messagesJson") or "[]")
        eid = event_id or f"e-{uuid.uuid4().hex[:12]}"
        if eid in state.seen_event_ids:                 # idempotent delivery
            return self._view(record, state, messages, [])
        return self._process(record, etag, state, messages, text, list(images), eid, "reply")

    def get(self, client_id: str, session_id: str) -> dict:
        record, _ = self.store.get(client_id, session_id)
        if record is None:
            raise NotFound(session_id)
        state = DiagnosticState.model_validate_json(_unpack(record, "stateJson"))
        return self._view(record, state, json.loads(_unpack(record, "messagesJson") or "[]"), [])

    def sweep(self, client_id: str) -> int:
        """Escalate sessions still waiting for a reply after their deadline."""
        n, now = 0, self.now()
        for row in self.store.list(client_id):
            if row.get("state") != FsmState.NEED_DIAGNOSTIC_DATA.value:
                continue
            try:
                if datetime.fromisoformat(row["deadlineUtc"]) > now:
                    continue
                record, etag = self.store.get(client_id, row["session_id"])
                state = DiagnosticState.model_validate_json(_unpack(record, "stateJson"))
                msgs = json.loads(_unpack(record, "messagesJson") or "[]")
                self._process(record, etag, state, msgs, "", [], f"timeout-{row['session_id']}", "timeout")
                n += 1
            except Conflict:
                continue
        return n

    def list_sessions(self, client_id: str) -> List[dict]:
        return sorted(self.store.list(client_id), key=lambda r: r.get("updatedUtc", ""), reverse=True)

    # ----- internals
    def _process(self, record, etag, state, messages, text, images, event_id, kind, creating=False) -> dict:
        refs, blobs = [], {}
        for i, (name, data, mime) in enumerate(images):
            ref = f"{event_id}-img{i}"
            refs.append(ref)
            blobs[ref] = (data, mime)
        evt = Event(event_id=event_id, kind=kind, text=text, attachments=refs)
        ports = self.ports_factory(state.client_id, blobs)
        now = self.now()
        if kind != "timeout":
            messages.append({"ts": now.isoformat(), "role": "user", "kind": "text", "text": text[:2000] or ("Capture d'écran jointe." if images else ""),
                             "images": len(images)})
        try:
            result: StepResult = advance(state, evt, ports, now)
        except Exception as exc:                        # technical failure -> human, never improvisation
            st = state.model_copy(deep=True)
            st.seen_event_ids.append(event_id)
            out: List[dict] = []
            escalate(st, f"technical_error:{type(exc).__name__}", out)
            result = StepResult(DiagnosticState.model_validate(st.model_dump()), out)
        for m in result.outbox:
            messages.append(self._message_of(m, now))
        record = dict(record)
        self._write_state(record, result.state, messages, now)
        if creating:
            self.store.create(record)
        else:
            self.store.update(record, etag)
        return self._view(record, result.state, messages, result.outbox)

    @staticmethod
    def _message_of(m: dict, now: datetime) -> dict:
        base = {"ts": now.isoformat(), "role": "assistant", "kind": m["kind"]}
        if m["kind"] == "question":
            return base | {"prompt": m["prompt"]}
        if m["kind"] == "plan":
            return base | {"plan": m["plan"]}
        return base | {"reason": m["reason"], "dossier": m.get("dossier", {})}

    @staticmethod
    def _write_state(record: dict, state: DiagnosticState, messages: List[dict], now: datetime) -> None:
        messages = messages[-MAX_MESSAGES:]
        while True:                                    # drop oldest until it fits the property budget
            blob = json.dumps(messages, ensure_ascii=False)
            if len(blob) <= _PART * _MAX_PARTS or len(messages) <= 1:
                break
            messages = messages[1:]
        _pack(record, "messagesJson", blob)
        _pack(record, "stateJson", state.model_dump_json())
        record["state"] = state.state.value
        record["deadlineUtc"] = state.deadline_utc.isoformat()
        record["updatedUtc"] = now.isoformat()

    @staticmethod
    def _view(record: dict, state: DiagnosticState, messages: List[dict], outbox: List[dict]) -> dict:
        return {"session_id": record["session_id"], "client_id": record["client_id"],
                "origin": record.get("origin"), "ticket_id": state.ticket_id, "user_id": record.get("userId"),
                "state": state.state.value, "terminal": state.state in TERMINAL, "turn": state.turn_count,
                "confidence": state.confidence, "escalation_reason": state.escalation_reason,
                "messages": messages[-MAX_MESSAGES:], "outbox": outbox,
                "candidates": [c.model_dump() for c in state.candidates[:3]],
                "plan": state.final_plan.model_dump() if state.final_plan else None}
