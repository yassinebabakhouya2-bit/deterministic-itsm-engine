"""Routing in Azure: passes B and C, the review of the routing proposals, the routing tables, and their
activation (routing chantier; spec docs/v10-deterministic-engine.md, "Semantic enrichment at ingestion,
deterministic routing at runtime").

Nothing routes unless a person validated it, and nothing reaches /find unless a person activated tables a
measure vouched for. This module is the storage side of that rule; kefind holds the decisions themselves
(``kefind.review``), the tables and their matchers (``kefind.routing``) and passes B and C
(``kefind.taxonomy``, ``kefind.assign``). Like kecore_pipeline it never imports an Azure SDK: storage is
any object with list/read/write/delete (+ write_if_absent), the review table any TableStorage (read ->
(row, etag), create -> bool, merge_if, list), so everything is tested in memory.

Under kecore-<client>/ (the only writers are this module and the Function routes calling it):

  taxonomy.json, taxonomy.history/v<n>.json   the frozen taxonomy, written only by a person's freeze
  routing/builds/<build>/                      one routing run ("build"): status.json, taxonomy.json (the
                                               frozen copy it used), taxonomy.proposed.json (pass B),
                                               assign/<nnnnn>.json + index.json + summary.json (pass C),
                                               decisions.json (the snapshot it was built from), tables.json,
                                               review.json (per-fiche proposals, for the review tab),
                                               summary.json
  routing/taxonomy-proposal.json               the build holding the last pass-B proposal, and its run
  routing/assignments.json                     the build holding the pass-C proposals, its run, taxonomy
  routing/latest-build.json                    the last build started, its state
  routing/candidate.json                       the tables the next measure and activation are about
  routing/active.json, routing/history/        the tables /find serves (a person activated them), and every
                                               earlier pointer
  routing/kill.json                            the sticky kill switch: only a deactivation sets it, only a
                                               person's activation clears it
  routing/running.json                         the lease: one routing run at a time
  routing/gates/<content_sha256>-<run>.json    a measure of those tables on that run (the scoreboard's gate)

Review decisions live in the ``kefindreview`` table (one row per fiche and source text, ``ReviewDecision
.to_row``), written only by ``decide`` / ``carry_forward`` (POST kecore/review/decision, /carry).

Why the source hash: a decision is keyed by the sha256 of the fiche's EXTRACTED text (runs/<run>/
fiches.jsonl, before boilerplate removal): adding other documents to the KB moves the corpus-wide
boilerplate and so the decomposed text of untouched fiches, and must never send them back to review.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import enrichment_service as enrichment_svc
import kecore_pipeline as pipeline
import kefind_service as finder
import scoreboard_service as sb_svc
from kecore.text import EMAIL_RE, PHONE_RE, clean_text
from kefind.assign import Assignment, make_assignment
from kefind.assign import stats as assign_stats
from kefind.enrich import Enrichment, app_vocabulary, content_sha256
from kefind.funnel import MAX_TEXT_CHARS
from kefind.graph import own_numbers
from kefind.review import (MAX_ALIAS_CHARS, MAX_ALIAS_WORDS, MIN_ALIAS_WORDS, ChangedError, ReviewDecision,
                           UnknownFicheError, check_seen, current_decisions, decision_row_key, fiche_key,
                           fiche_statuses, validate_decision)
from kefind.routing import CODE_VERSION, RoutingTables, alias_problem, build_tables, candidate_codes, route_tokens
from kefind.routing import routable as routable_fiches
from kefind.routing import token_key
from kefind.taxonomy import Taxonomy, freeze, make_taxonomy, removed_intents

TAXONOMY = "taxonomy.json"
TAXONOMY_HISTORY = "taxonomy.history/"
TAXONOMY_PROPOSAL = "routing/taxonomy-proposal.json"
ASSIGNMENTS = "routing/assignments.json"
LATEST_BUILD = "routing/latest-build.json"
CANDIDATE = "routing/candidate.json"
ACTIVE = "routing/active.json"
KILL = "routing/kill.json"
LEASE = "routing/running.json"
HISTORY = "routing/history/"
GATES = "routing/gates/"

LEASE_TTL_S = 2 * 3600       # host.json functionTimeout: a lease older than any activity can run is a dead run's
TEXT_EXCERPT_CHARS = 1500
WRITE_TRIES = 3
MAX_CARRY_KEYS = 500
MAX_BY_CHARS = 120
MAX_BY_ID_CHARS = 64
MAX_REASON_CHARS = 500
QUEUES = ("proposed", "stale")                       # what GET kecore/review/next walks
REVIEW_STATUSES = ("proposed", "validated", "rejected", "stale")
GATE_KEYS = ("sb_id", "status", "passed", "reasons", "wrong_exact", "route_first", "base_first", "compared",
             "excluded")
_KEY_RE = re.compile(r"[0-9a-f]{32}")                # always fullmatch
_ID_RE = re.compile(r"[0-9A-Za-z-]{1,64}")           # a build id, like a run id; always fullmatch


class StoredFileError(RuntimeError):
    """A file this service (or a person's freeze) wrote no longer reads as written: hand-edited, cut, or
    not JSON. Never guessed around -- the route answers 500 and says which file."""


class OrphansError(ChangedError):
    """A freeze would take their intent away from fiches a person validated (HTTP 409): ``orphans`` lists
    them; posting again with ``orphans_ok`` confirms."""

    def __init__(self, message: str, orphans: list[dict]):
        super().__init__(message)
        self.orphans = orphans


# --- small storage helpers -------------------------------------------------------------------------------

def _container(client: str) -> str:
    return pipeline.kecore_container(client)


def _bytes(data) -> bytes:
    """Every file here: sorted keys, indent 1, UTF-8, a final newline (byte-identical on a replay)."""
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def _read(storage, client: str, name: str):
    data = storage.read(_container(client), name)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StoredFileError(f"{_container(client)}/{name} is not valid JSON ({str(exc)[:200]})") from None


def _write(storage, client: str, name: str, data) -> None:
    storage.write(_container(client), name, _bytes(data))


def _write_if_absent(storage, client: str, name: str, data) -> bool:
    """Writes only a blob that does not exist yet (If-None-Match); a storage without a conditional write is
    read first (right for a single writer, as kecore_pipeline.StorageRecordStore)."""
    writer = getattr(storage, "write_if_absent", None)
    if writer is not None:
        return writer(_container(client), name, _bytes(data))
    if storage.read(_container(client), name) is not None:
        return False
    storage.write(_container(client), name, _bytes(data))
    return True


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def is_key(value) -> bool:
    """A fiche key as URLs carry it (kefind.review.fiche_key): 32 lowercase hexadecimal characters."""
    return isinstance(value, str) and _KEY_RE.fullmatch(value) is not None


def _parse_time(value) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stamp(now) -> str:
    return _parse_time(now).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _keep_history(storage, client: str, data: bytes, now) -> str:
    """A copy of a pointer about to change, under routing/history/<UTC time>.json (never overwritten)."""
    stamp, n = _stamp(now), 1
    name = f"{HISTORY}{stamp}.json"
    while storage.read(_container(client), name) is not None:
        n += 1
        name = f"{HISTORY}{stamp}-{n}.json"
    storage.write(_container(client), name, data)
    return name


def build_paths(build_id: str) -> dict[str, str]:
    base = f"routing/builds/{build_id}/"
    return {"dir": base, "status": base + "status.json", "taxonomy": base + "taxonomy.json",
            "proposed": base + "taxonomy.proposed.json", "assign_dir": base + "assign/",
            "assign_index": base + "assign/index.json", "assign_summary": base + "assign/summary.json",
            "decisions": base + "decisions.json", "tables": base + "tables.json", "review": base + "review.json",
            "summary": base + "summary.json"}


def gate_path(content_sha256: str, kb_run_id: str) -> str:
    """The measure of some tables on some run: the routing behaviour depends on the tables' content and on
    the run's map only, so an identical rebuild keeps its gate."""
    return f"{GATES}{content_sha256}-{kb_run_id}.json"


def _llm_counts(llm) -> dict:
    """The model calls made and the answers read from the record (kecore.llm.RecordingLLM's counters)."""
    counts = {"calls": getattr(llm, "calls", 0), "cached": getattr(llm, "hits", 0)}
    return {k: v if isinstance(v, int) and not isinstance(v, bool) else 0 for k, v in counts.items()}


def _map(storage, client: str, run_id: str):
    return finder.load_map(storage, client, run_id, with_semantic=False)


# --- identities ------------------------------------------------------------------------------------------

def source_hashes(storage, client: str, run_id: str) -> dict[str, str]:
    """{fiche_id: sha256 of its extracted text} from runs/<run>/fiches.jsonl (the review key: before
    boilerplate removal, so adding other documents never stales a decision). The first fiche of an id wins,
    as kecore's extraction keeps it. FileNotFoundError when the run has no fiches.jsonl."""
    data = pipeline._require(storage, _container(client), pipeline.layout(run_id)["fiches"])
    out: dict[str, str] = {}
    for item in pipeline.read_jsonl(data):
        fiche_id = item.get("fiche_id")
        if isinstance(fiche_id, str) and fiche_id not in out:
            out[fiche_id] = hashlib.sha256(str(item.get("text") or "").encode("utf-8")).hexdigest()
    return out


def frozen_taxonomy(storage, client: str) -> Taxonomy | None:
    """The client's frozen taxonomy (taxonomy.json), None before the first freeze. A file that no longer
    matches its sha256 (edited by hand) is refused: it would silently change what validated intents mean."""
    data = _read(storage, client, TAXONOMY)
    if data is None:
        return None
    taxonomy = Taxonomy.from_dict(_dict(data))
    if not isinstance(taxonomy.version, int) or taxonomy.version < 1 or not taxonomy.verify():
        raise StoredFileError(f"{_container(client)}/{TAXONOMY} does not match its sha256: it is written only by "
                              "POST kecore/review/taxonomy, never by hand")
    return taxonomy


def _build_taxonomy(storage, client: str, build_id: str) -> Taxonomy | None:
    """The frozen taxonomy a build copied at its start (a freeze during the build never mixes two)."""
    data = _read(storage, client, build_paths(build_id)["taxonomy"])
    if data is None:
        return None
    taxonomy = Taxonomy.from_dict(_dict(data))
    if not taxonomy.verify():
        raise StoredFileError(f"the taxonomy copy of build {build_id} does not match its sha256")
    return taxonomy


# --- requests --------------------------------------------------------------------------------------------

def _client(body, allowed_clients) -> str:
    client = body.get("client") if isinstance(body, dict) else None
    if not isinstance(client, str) or client not in set(allowed_clients):
        raise ValueError("unknown client")
    return client


def _by(body, name: str = "by", required: bool = True) -> str:
    value = body.get(name)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{name}: a text is expected")
    value = " ".join(value.split())
    if required and not value:
        raise ValueError(f"{name}: the name of the person is required")
    if len(value) > MAX_BY_CHARS:
        raise ValueError(f"{name}: {MAX_BY_CHARS} characters at most")
    return value


def validate_routing_request(body, allowed_clients, build_id: str) -> dict:
    """POST kecore/routing/runs: rebuild the routing tables (passes B and C when needed) and, by default,
    measure them. Deny-by-default on the client. The build id is the orchestration's instance id."""
    if not isinstance(body, dict):
        raise ValueError("a JSON object is expected")
    client = _client(body, allowed_clients)
    mode = body.get("mode", "record")
    if mode not in pipeline.MODES:
        raise ValueError("mode must be 'replay' or 'record'")
    llm_parallelism = body.get("llm_parallelism", pipeline.DEFAULT_LLM_PARALLELISM)
    if isinstance(llm_parallelism, bool) or not isinstance(llm_parallelism, int) \
            or not 1 <= llm_parallelism <= pipeline.MAX_LLM_PARALLELISM:
        raise ValueError(f"llm_parallelism must be an integer between 1 and {pipeline.MAX_LLM_PARALLELISM}")
    batch_size = body.get("batch_size", pipeline.DEFAULT_BATCH)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= pipeline.MAX_BATCH:
        raise ValueError(f"batch_size must be an integer between 1 and {pipeline.MAX_BATCH}")
    measure = body.get("measure", True)
    if not isinstance(measure, bool):
        raise ValueError("measure must be true or false")
    if not isinstance(build_id, str) or not _ID_RE.fullmatch(build_id):
        raise ValueError("invalid build id")
    return {"client": client, "mode": mode, "llm_parallelism": llm_parallelism, "batch_size": batch_size,
            "build_id": build_id, "measure": measure, "run_id": None, "kind": "manual"}


def validate_activate_request(body, allowed_clients) -> dict:
    """POST kecore/routing/activate {"client", "build_id", "by", "accept_insufficient"?, "reason"?}."""
    if not isinstance(body, dict):
        raise ValueError("a JSON object is expected")
    client = _client(body, allowed_clients)
    build_id = body.get("build_id")
    if not isinstance(build_id, str) or not _ID_RE.fullmatch(build_id):
        raise ValueError("build_id: the id of the tables to activate")
    accept = body.get("accept_insufficient", False)
    if not isinstance(accept, bool):
        raise ValueError("accept_insufficient must be true or false")
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason: a text is expected")
    reason = " ".join((reason or "").split())
    if len(reason) > MAX_REASON_CHARS:
        raise ValueError(f"reason: {MAX_REASON_CHARS} characters at most")
    if accept and not reason:
        raise ValueError("reason: accepting an insufficient measure needs a reason (it is recorded)")
    return {"client": client, "build_id": build_id, "by": _by(body), "accept_insufficient": accept,
            "reason": reason if accept else ""}


def validate_deactivate_request(body, allowed_clients) -> dict:
    """POST kecore/routing/deactivate {"client", "by"}."""
    if not isinstance(body, dict):
        raise ValueError("a JSON object is expected")
    return {"client": _client(body, allowed_clients), "by": _by(body)}


def validate_carry_request(body, allowed_clients) -> dict:
    """POST kecore/review/carry {"client", "by", "by_id"?, "keys": [fiche keys]}."""
    if not isinstance(body, dict):
        raise ValueError("a JSON object is expected")
    client = _client(body, allowed_clients)
    keys = body.get("keys")
    if not isinstance(keys, list) or not keys or len(keys) > MAX_CARRY_KEYS \
            or not all(isinstance(k, str) and _KEY_RE.fullmatch(k) for k in keys):
        raise ValueError(f"keys: 1 to {MAX_CARRY_KEYS} fiche keys (32 lowercase hexadecimal characters)")
    by_id = _by(body, "by_id", required=False)
    if len(by_id) > MAX_BY_ID_CHARS:
        raise ValueError(f"by_id: {MAX_BY_ID_CHARS} characters at most")
    return {"client": client, "by": _by(body), "by_id": by_id, "keys": list(dict.fromkeys(keys))}


# --- the lease -------------------------------------------------------------------------------------------

def _stale(held: dict, now) -> bool:
    try:
        started = _parse_time(held.get("started_at"))
    except (TypeError, ValueError):
        return True
    return (_parse_time(now) - started).total_seconds() > LEASE_TTL_S


def lease(storage, client: str, now) -> dict | None:
    """The routing run in progress ({"build_id", "kind", "started_at"}), None when there is none (or only a
    dead run's lease, older than ``LEASE_TTL_S``)."""
    try:
        held = _read(storage, client, LEASE)
    except StoredFileError:
        return None
    return held if isinstance(held, dict) and not _stale(held, now) else None


def _take_lease(storage, client: str, build_id: str, kind: str, now) -> dict | None:
    """None when ``build_id`` holds the lease (taken now, or already: an activity retried by Durable runs
    again); else the lease of the run in progress. A dead run's lease is replaced."""
    entry = {"build_id": build_id, "kind": kind, "started_at": str(now)}
    for _ in range(2):
        try:
            held = _read(storage, client, LEASE)
        except StoredFileError:
            held = {}
        if isinstance(held, dict) and held.get("build_id") == build_id:
            return None
        if isinstance(held, dict) and held and not _stale(held, now):
            return held
        if held is not None:
            storage.delete(_container(client), LEASE)
        if _write_if_absent(storage, client, LEASE, entry):
            return None
    return lease(storage, client, now) or {"build_id": "?", "started_at": "?"}


# --- pass A and pass C, as the review and the tables read them -------------------------------------------

def _enrichment_at(storage, client: str, run_id: str, index: int, fiche_id: str) -> Enrichment | None:
    """Pass A of one fiche (runs/<run>/enrichment/<position in kbmap.ranked>.json), None when missing or
    when the file is another fiche's (the file index is never an identity on its own)."""
    data = _read(storage, client, f"{enrichment_svc.paths(run_id)['dir']}{index:05d}.json")
    if not isinstance(data, dict):
        return None
    try:
        item = Enrichment.from_dict(data)
    except TypeError:
        return None
    return item if item.fiche_id == fiche_id else None


def _pass_a(storage, client: str, kbmap, ids) -> dict[str, Enrichment | None]:
    position = {f: i for i, f in enumerate(kbmap.ranked)}
    return {f: _enrichment_at(storage, client, kbmap.run_id, position[f], f) for f in ids if f in position}


def _assignment(data) -> Assignment | None:
    if not isinstance(data, dict):
        return None
    try:
        return Assignment.from_dict(data)
    except TypeError:
        return None


def _assignments_pointer(storage, client: str, run_id: str, taxonomy: Taxonomy | None) -> dict:
    """routing/assignments.json when it is about this run and this frozen taxonomy, else {}."""
    pointer = _dict(_read(storage, client, ASSIGNMENTS))
    if taxonomy is None or pointer.get("run_id") != run_id or pointer.get("taxonomy_sha256") != taxonomy.sha256 \
            or not isinstance(pointer.get("build_id"), str) or not _ID_RE.fullmatch(pointer["build_id"]):
        return {}
    return pointer


def _pass_c(storage, client: str, kbmap, taxonomy: Taxonomy | None, only: str | None = None
            ) -> tuple[str | None, dict[str, Assignment]]:
    """(the build holding them, {fiche_id: pass-C proposal}) for this run and this frozen taxonomy, by
    fiche id (assign/index.json), never by file position. ``only``: one fiche."""
    pointer = _assignments_pointer(storage, client, kbmap.run_id, taxonomy)
    if not pointer:
        return None, {}
    paths = build_paths(pointer["build_id"])
    index = _dict(_read(storage, client, paths["assign_index"]))
    out: dict[str, Assignment] = {}
    for fiche_id, number in sorted(index.items()):
        if only is not None and fiche_id != only:
            continue
        if not isinstance(number, str) or not number.isdigit():
            continue
        item = _assignment(_read(storage, client, f"{paths['assign_dir']}{number}.json"))
        if item is not None and item.fiche_id == fiche_id:
            out[fiche_id] = item
    return pointer["build_id"], out


def _current_a(kbmap, fiche_id: str, item: Enrichment | None) -> Enrichment | None:
    """A pass-A proposal that says something about the fiche as it is: no error, same text."""
    if item is None or item.error or item.content_sha256 != content_sha256(kbmap.fiches[fiche_id]):
        return None
    return item


def _current_c(kbmap, fiche_id: str, item: Assignment | None) -> Assignment | None:
    if item is None or item.error or item.content_sha256 != content_sha256(kbmap.fiches[fiche_id]):
        return None
    return item


def _proposals(kbmap, ids, pass_a: dict, pass_c: dict) -> dict[str, dict]:
    """What each routable fiche PROPOSES, for ``build_tables``: a fiche under review blocks these keys
    instead of leaving another fiche alone in front of them."""
    out = {}
    for fiche_id in ids:
        a = _current_a(kbmap, fiche_id, pass_a.get(fiche_id))
        c = _current_c(kbmap, fiche_id, pass_c.get(fiche_id))
        aliases = (list(a.semantic_aliases_fr) + list(a.semantic_aliases_en)) if a else []
        aliases += (list(c.core_aliases_fr) + list(c.core_aliases_en)) if c else []
        out[fiche_id] = {"intent": c.intent if c else None, "app": a.primary_app if a else None, "aliases": aliases}
    return out


def _review_index(kbmap, ids, pass_a: dict, pass_c: dict) -> dict[str, dict]:
    """Per fiche, what the review overview lists (one file per build instead of two per fiche)."""
    out = {}
    for fiche_id in ids:
        a, c = pass_a.get(fiche_id), pass_c.get(fiche_id)
        a_now = _current_a(kbmap, fiche_id, a)
        c_now = c if c is not None and c.content_sha256 == content_sha256(kbmap.fiches[fiche_id]) else None
        out[fiche_id] = {
            "intent_a": a_now.canonical_intent if a_now else None,
            "primary_app_a": a_now.primary_app if a_now else None,
            "app_inferred": bool(a_now.app_inferred) if a_now else False,
            "error_a": a.error if a is not None else None,
            "intent_c": c_now.intent if c_now and not c_now.error else None,
            "error_c": c_now.error if c_now else None,
        }
    return out


# --- the routing run: prepare, pass B, pass C, build, finish ---------------------------------------------

def prepare(storage, payload: dict, now) -> dict:
    """The first step of a routing run: takes the lease, resolves the run, says which passes are needed.

    - pass B (``needs_b``) only while no taxonomy is frozen, once per run (a failed proposal is asked again);
      refused while pass A has errors: a taxonomy frozen from a partial pass A misses intents for good;
    - pass C (``needs_c``) only with a frozen taxonomy, when the pass-C proposals are not about this run and
      this taxonomy, or have errors (a record run then asks only the missing calls);
    - the build ALWAYS follows: L0 numbers and L1 aliases need neither (phase 1).
    The frozen taxonomy is copied into the build, so a freeze during the run never mixes two.
    Returns {"client","run_id","build_id","fiches","taxonomy","needs_b","needs_c"} [+ "error"]."""
    client, build_id = payload["client"], payload["build_id"]
    run_id = payload.get("run_id") or finder.latest_run(storage, client)
    out = {"client": client, "run_id": run_id, "build_id": build_id, "fiches": 0, "taxonomy": None,
           "needs_b": False, "needs_c": False}
    held = _take_lease(storage, client, build_id, payload.get("kind") or "manual", now)
    if held is not None:
        return {**out, "error": f"a routing run is in progress ({held.get('build_id')}, since {held.get('started_at')})"}
    if not run_id:
        return {**out, "error": "no KB map for this client yet: start a kecore run first"}
    try:
        kbmap = _map(storage, client, run_id)
    except FileNotFoundError:
        return {**out, "error": f"no KB map for run {run_id}"}
    out["fiches"] = len(routable_fiches(kbmap))
    try:
        summary = _read(storage, client, enrichment_svc.paths(run_id)["summary"])
        taxonomy = frozen_taxonomy(storage, client)
    except StoredFileError as exc:
        return {**out, "error": str(exc)}
    if not isinstance(summary, dict):
        return {**out, "error": f"run {run_id} has no pass-A summary (enrichment/summary.json): run a kecore run "
                                "with enrich true first"}
    if taxonomy is None:
        pointer = _dict(_read(storage, client, TAXONOMY_PROPOSAL))
        out["needs_b"] = pointer.get("run_id") != run_id or bool(pointer.get("error"))
        errors = int(summary.get("errors") or 0)
        if out["needs_b"] and errors:
            return {**out, "error": f"pass A has {errors} errors: run a kecore record run first, only the missing "
                                    "calls are made"}
        return out
    out["taxonomy"] = {"version": taxonomy.version, "sha256": taxonomy.sha256}
    pointer = _dict(_read(storage, client, ASSIGNMENTS))
    out["needs_c"] = (pointer.get("run_id") != run_id or pointer.get("taxonomy_sha256") != taxonomy.sha256
                      or bool(pointer.get("errors")))
    _write(storage, client, build_paths(build_id)["taxonomy"], taxonomy.to_dict())
    return out


def propose_taxonomy(storage, payload: dict, llm) -> dict:
    """Pass B: the routable fiches' pass-A intents merged into one proposal (kefind.taxonomy), written to
    the build (taxonomy.proposed.json) and named by routing/taxonomy-proposal.json. Nothing is frozen here:
    a person reviews, edits and freezes it (POST kecore/review/taxonomy)."""
    client, run_id, build_id = payload["client"], payload["run_id"], payload["build_id"]
    kbmap = _map(storage, client, run_id)
    ids = routable_fiches(kbmap)
    enrichments = [item for item in _pass_a(storage, client, kbmap, ids).values() if item is not None]
    proposal = make_taxonomy(llm, enrichments, kbmap.dictionary, run_id=run_id)
    _write(storage, client, build_paths(build_id)["proposed"], proposal.to_dict())
    # a failed proposal is named too (the review tab shows why), and asked again by the next run
    _write(storage, client, TAXONOMY_PROPOSAL, {"build_id": build_id, "run_id": run_id, "error": proposal.error})
    return {"build_id": build_id, "run_id": run_id, "intents": len(proposal.intents), "dropped": len(proposal.dropped),
            "warnings": list(proposal.warnings), "error": proposal.error, "source": dict(proposal.source),
            "llm": _llm_counts(llm)}


def assign(storage, payload: dict, start: int, end: int, llm) -> dict:
    """Pass C of routable fiches [start, end) (kefind.assign), one file each (assign/<position>.json), with
    the taxonomy the build copied. A per-fiche failure stays in that fiche's file, never the batch's."""
    client, run_id, build_id = payload["client"], payload["run_id"], payload["build_id"]
    kbmap = _map(storage, client, run_id)
    ids = routable_fiches(kbmap)
    taxonomy = _build_taxonomy(storage, client, build_id)
    if taxonomy is None:
        raise FileNotFoundError(f"build {build_id} has no frozen taxonomy: pass C needs one")
    position = {f: i for i, f in enumerate(kbmap.ranked)}
    directory = build_paths(build_id)["assign_dir"]
    errors = aliases = dropped = 0
    for index in range(start, min(end, len(ids))):
        fiche_id = ids[index]
        enrichment = _enrichment_at(storage, client, run_id, position[fiche_id], fiche_id)
        result = make_assignment(llm, kbmap, fiche_id, taxonomy, enrichment)
        errors += result.error is not None
        aliases += len(result.core_aliases_fr) + len(result.core_aliases_en)
        dropped += len(result.dropped)
        _write(storage, client, f"{directory}{index:05d}.json", result.to_dict())
    return {"start": start, "end": end, "errors": errors, **_llm_counts(llm), "core_aliases": aliases,
            "dropped": dropped}


def assign_summary(storage, payload: dict) -> dict:
    """Reads every pass-C file of the build (all must be there), writes assign/index.json ({fiche_id:
    "nnnnn"}), assign/summary.json and routing/assignments.json (this build now holds the proposals)."""
    client, run_id, build_id = payload["client"], payload["run_id"], payload["build_id"]
    kbmap = _map(storage, client, run_id)
    ids = routable_fiches(kbmap)
    taxonomy = _build_taxonomy(storage, client, build_id)
    if taxonomy is None:
        raise FileNotFoundError(f"build {build_id} has no frozen taxonomy: pass C needs one")
    paths = build_paths(build_id)
    items, index = [], {}
    for i, fiche_id in enumerate(ids):
        name = f"{paths['assign_dir']}{i:05d}.json"
        data = pipeline._require(storage, _container(client), name)
        item = _assignment(json.loads(data.decode("utf-8")))
        if item is None or item.fiche_id != fiche_id:
            raise StoredFileError(f"{_container(client)}/{name} is not the pass-C proposal of {fiche_id!r}")
        items.append(item)
        index[fiche_id] = f"{i:05d}"
    summary = assign_stats(items)
    _write(storage, client, paths["assign_index"], index)
    _write(storage, client, paths["assign_summary"], summary)
    _write(storage, client, ASSIGNMENTS, {"build_id": build_id, "run_id": run_id, "taxonomy_sha256": taxonomy.sha256,
                                          "errors": summary["errors"]})
    return summary


def _decisions(rows) -> list[ReviewDecision]:
    out = [ReviewDecision.from_row(row) for row in rows or [] if isinstance(row, dict)]
    return sorted((d for d in out if d.fiche_id),
                  key=lambda d: (d.fiche_id, d.source_sha256, d.revision, d.decided_at, d.status))


def build(storage, review_rows, payload: dict, now) -> dict:
    """The routing tables of the run (kefind.routing.build_tables) from EVERY stored decision, the run's
    source hashes, the build's frozen taxonomy copy (None: L0 and L1 only) and what the fiches under review
    propose (pass A of the run, pass C when it is about this run and this taxonomy). Writes the snapshot
    (decisions.json), tables.json, review.json, summary.json, latest-build.json and -- when this build's run
    is the latest one or newer than the current candidate's -- candidate.json.

    Carry-over: when tables are active, the kill switch is off, and these tables have the same content AND
    the same knowledge base (content_sha256, kb_sha256) as the active ones, they answer exactly as the active
    ones do: active.json moves to them with no new measure, the old entry kept as ``previous`` (the run it
    was activated for keeps being served until this run is published). Anything else leaves active.json as
    it is and says why (``why_not``): a person measures and activates the new tables."""
    client, run_id, build_id = payload["client"], payload["run_id"], payload["build_id"]
    kbmap = _map(storage, client, run_id)
    current = source_hashes(storage, client, run_id)
    ids = routable_fiches(kbmap)
    taxonomy = _build_taxonomy(storage, client, build_id)
    decisions = _decisions(review_rows)
    pass_a = _pass_a(storage, client, kbmap, ids)
    assign_build, pass_c = _pass_c(storage, client, kbmap, taxonomy)
    tables = build_tables(kbmap, current, taxonomy, decisions, _proposals(kbmap, ids, pass_a, pass_c), build_id)
    paths = build_paths(build_id)
    _write(storage, client, paths["decisions"], [d.to_dict() for d in decisions])
    storage.write(_container(client), paths["tables"], tables.to_bytes())
    _write(storage, client, paths["review"], {"build_id": build_id, "run_id": run_id, "assign_build": assign_build,
                                              "fiches": _review_index(kbmap, ids, pass_a, pass_c)})
    entry = {"build_id": build_id, "run_id": run_id, "tables_sha256": tables.sha256,
             "content_sha256": tables.content_sha256, "kb_sha256": tables.kb_sha256}
    latest = finder.latest_run(storage, client)
    previous_candidate = _dict(_read(storage, client, CANDIDATE))
    is_candidate = (run_id == latest or not previous_candidate.get("run_id")
                    or str(run_id) >= str(previous_candidate["run_id"]))
    if is_candidate:
        _write(storage, client, CANDIDATE, entry)
    carried, why_not = _carry_over(storage, client, tables, now)
    summary = {**entry, "tables": tables.stats, "carried": carried, "why_not": why_not, "candidate": is_candidate,
               "assign_build": assign_build,
               "taxonomy": {"version": taxonomy.version, "sha256": taxonomy.sha256} if taxonomy else None}
    _write(storage, client, paths["summary"], summary)
    _write(storage, client, LATEST_BUILD, {"build_id": build_id, "run_id": run_id, "state": "built",
                                           "kind": payload.get("kind") or "manual"})
    return summary


def _killed(storage, client: str) -> dict:
    """routing/kill.json when the kill switch is on, else {} (an unreadable switch counts as ON: a person
    turned it off once, and only a person's activation rewrites it)."""
    try:
        kill = _dict(_read(storage, client, KILL))
    except StoredFileError:
        return {"killed": True, "by": "?", "at": "?"}
    return kill if kill.get("killed") is True else {}


def _carry_over(storage, client: str, tables: RoutingTables, now) -> tuple[bool, str]:
    before = storage.read(_container(client), ACTIVE)
    if before is None:
        return False, "no active tables"
    try:
        active = _dict(json.loads(before.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        return False, "routing/active.json is unreadable: a person activates the tables again"
    if active.get("build_id") == tables.build_id:
        return True, ""  # this very build, carried by an earlier attempt of this activity
    if _killed(storage, client):
        return False, "routing is switched off (kill switch): only a person's activation turns it back on"
    if str(tables.run_id or "") < str(active.get("run_id") or ""):
        return False, f"the active tables are for a newer run ({active.get('run_id')})"
    if tables.content_sha256 != active.get("content_sha256"):
        return False, "the routing tables changed (content_sha256): measure and activate them"
    if tables.kb_sha256 != active.get("kb_sha256"):
        return False, "the knowledge base changed (kb_sha256: fiches, graph or dictionary): measure and activate them"
    pointer = {"build_id": tables.build_id, "run_id": tables.run_id, "tables_sha256": tables.sha256,
               "content_sha256": tables.content_sha256, "kb_sha256": tables.kb_sha256,
               "activated_by": active.get("activated_by"), "activated_at": active.get("activated_at"),
               "gate": active.get("gate"), "override": active.get("override"), "carried_from": active.get("build_id"),
               "carried_at": str(now),
               "previous": {k: active.get(k) for k in ("build_id", "run_id", "tables_sha256", "content_sha256")}}
    # no compare-and-swap on a blob: read again right before the write, and leave a pointer that changed alone
    if storage.read(_container(client), ACTIVE) != before or _killed(storage, client):
        return False, "the active pointer changed meanwhile: left as it is"
    _keep_history(storage, client, before, now)
    storage.write(_container(client), ACTIVE, _bytes(pointer))
    return True, ""


def finish(storage, payload: dict, outcome: dict, now) -> dict:
    """The last step of every routing run, failed or not: the build's status.json, latest-build.json, and
    the lease released -- only when this build holds it (a run refused at prepare never frees another's)."""
    client, build_id = payload["client"], payload["build_id"]
    outcome = _dict(outcome)
    state = "completed" if outcome.get("state") == "completed" else "failed"
    status_ = {"state": state, "error": outcome.get("error"), "finished_at": str(now)}
    _write(storage, client, build_paths(build_id)["status"], status_)
    run_id = outcome.get("run_id") or payload.get("run_id") or ""
    _write(storage, client, LATEST_BUILD, {"build_id": build_id, "run_id": run_id, "state": state,
                                           "kind": payload.get("kind") or "manual"})
    try:
        held = _read(storage, client, LEASE)
    except StoredFileError:
        held = None
    if isinstance(held, dict) and held.get("build_id") == build_id:
        storage.delete(_container(client), LEASE)
    return {**status_, "build_id": build_id}


# --- tables served, activation, kill switch --------------------------------------------------------------

def load_tables(storage, client: str, build_id: str, expected_sha: str | None = None) -> RoutingTables:
    """A build's tables, checked: their own hashes (an altered or cut file), the sha the caller expects (the
    active pointer's), the code they were built for, the client. ValueError on any mismatch,
    FileNotFoundError when the build has no tables."""
    if not isinstance(build_id, str) or not _ID_RE.fullmatch(build_id):
        raise ValueError(f"invalid build id {build_id!r}")
    data = storage.read(_container(client), build_paths(build_id)["tables"])
    if data is None:
        raise FileNotFoundError(f"no routing tables for build {build_id}")
    try:
        tables = RoutingTables.from_dict(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"the routing tables of build {build_id} are unreadable ({str(exc)[:200]})") from None
    if not tables.verify():
        raise ValueError(f"the routing tables of build {build_id} do not match their sha256")
    if expected_sha is not None and tables.sha256 != expected_sha:
        raise ValueError(f"the routing tables of build {build_id} are not the ones activated (sha256)")
    if tables.code_version != CODE_VERSION:
        raise ValueError(f"the routing tables of build {build_id} were built for code {tables.code_version}, this "
                         f"code is {CODE_VERSION}: rebuild, measure and activate them")
    if tables.client != client:
        raise ValueError(f"the routing tables of build {build_id} are client {tables.client!r}'s")
    return tables


def active_for(storage, client: str, run_id: str) -> tuple[dict | None, str | None]:
    """The active entry that applies to ``run_id`` (the pointer, or its ``previous`` entry kept by a
    carry-over until the new run is published), and why none does: (entry, None), (None, None) when routing
    was never activated, (None, why) when it is switched off or built for another run. Never raises on a bad
    file: routing is optional, /find answers without it."""
    try:
        kill = _killed(storage, client)
        if kill:
            return None, f"routing is switched off (by {kill.get('by') or '?'} at {kill.get('at') or '?'})"
        active = _read(storage, client, ACTIVE)
    except StoredFileError as exc:
        return None, str(exc)
    if not isinstance(active, dict) or not active.get("build_id"):
        return None, None
    if active.get("run_id") == run_id:
        return active, None
    previous = _dict(active.get("previous"))
    if previous.get("build_id") and previous.get("run_id") == run_id:
        return previous, None
    return None, f"the active routing tables were built for run {active.get('run_id')}, not {run_id}"


def _gate_view(gate: dict | None) -> dict | None:
    if not isinstance(gate, dict):
        return None
    return {key: gate.get(key) for key in GATE_KEYS}


def activate(storage, payload: dict, now) -> dict:
    """A person activates a build's tables (validated request: ``validate_activate_request``). Refused
    (ValueError -> 409) unless the build's tables load and verify, were built on the run /find serves now,
    and a measure of them on that run (the gate) passed -- or was insufficient and the person accepts it with
    a reason, recorded as ``override``. The active pointer is written first (the previous one kept in
    routing/history/), then the kill switch is cleared: an activation is the only thing that clears it."""
    client, build_id, by = payload["client"], payload["build_id"], payload["by"]
    try:
        tables = load_tables(storage, client, build_id)
    except FileNotFoundError:
        raise ValueError(f"unknown build {build_id} (no routing tables)") from None
    latest = finder.latest_run(storage, client)
    if tables.run_id != latest:
        raise ValueError(f"build {build_id} was made on run {tables.run_id}, /find serves run {latest}: rebuild the "
                         "tables on it, measure them, then activate")
    gate = _read(storage, client, gate_path(tables.content_sha256, tables.run_id))
    if not isinstance(gate, dict):
        raise ValueError(f"these tables were never measured on run {tables.run_id}: rebuild and measure them first")
    if gate.get("content_sha256") != tables.content_sha256 or gate.get("kb_run_id") != tables.run_id:
        raise ValueError("the measure on file is not about these tables on this run")
    status_ = gate.get("status")
    reasons = "; ".join(str(r) for r in gate.get("reasons") or [])[:300]
    override = None
    if status_ == "insufficient" and payload.get("accept_insufficient") is True and payload.get("reason"):
        override = {"by": by, "reason": payload["reason"]}
    elif status_ == "insufficient":
        raise ValueError(f"the measure is insufficient ({reasons or 'not enough questions'}): accept it explicitly, "
                         "with a reason, to activate anyway")
    elif status_ != "passed":
        raise ValueError(f"the measure of these tables is {status_!r}" + (f": {reasons}" if reasons else ""))
    pointer = {"build_id": build_id, "run_id": tables.run_id, "tables_sha256": tables.sha256,
               "content_sha256": tables.content_sha256, "kb_sha256": tables.kb_sha256, "activated_by": by,
               "activated_at": str(now), "gate": _gate_view(gate), "override": override, "carried_from": None,
               "previous": None}
    before = storage.read(_container(client), ACTIVE)
    if before is not None:
        _keep_history(storage, client, before, now)
    _write(storage, client, ACTIVE, pointer)
    _write(storage, client, KILL, {"killed": False, "by": by, "at": str(now)})
    return pointer


def deactivate(storage, payload: dict, now) -> dict:
    """The sticky kill switch: kill.json first (every /find stops routing within a minute, whatever happens
    next), then the active pointer moved to routing/history/. Only a person's activation turns routing back
    on; a rebuild or a kecore run never does (carry-over honours the switch)."""
    client = payload["client"]
    _write(storage, client, KILL, {"killed": True, "by": payload["by"], "at": str(now)})
    before = storage.read(_container(client), ACTIVE)
    if before is not None:
        _keep_history(storage, client, before, now)
        storage.delete(_container(client), ACTIVE)
    return {"active": None, "killed": True}


def _candidate(storage, client: str) -> dict | None:
    candidate = _read(storage, client, CANDIDATE)
    if not isinstance(candidate, dict) or not candidate.get("build_id"):
        return None
    build_id = candidate["build_id"]
    summary = _dict(_read(storage, client, build_paths(build_id)["summary"])) if _ID_RE.fullmatch(build_id) else {}
    return {**candidate, "stats": _dict(summary.get("tables"))}


def status(storage, client: str, now=None) -> dict:
    """GET kecore/routing: where routing stands for the client. ``effective``: active tables apply to the
    run /find serves, they load and verify, and the kill switch is off -- else ``why_not_effective``."""
    now = now or _now()
    run_id = finder.latest_run(storage, client)
    try:
        taxonomy = frozen_taxonomy(storage, client)
        frozen = ({"version": taxonomy.version, "sha256": taxonomy.sha256, "frozen_by": taxonomy.frozen_by,
                   "frozen_at": taxonomy.frozen_at} if taxonomy else None)
    except StoredFileError as exc:
        frozen = {"error": str(exc)}
    candidate = _candidate(storage, client)
    gate = None
    if candidate and candidate.get("content_sha256") and candidate.get("run_id"):
        gate = _gate_view(_read(storage, client, gate_path(candidate["content_sha256"], candidate["run_id"])))
    try:
        active = _read(storage, client, ACTIVE)
    except StoredFileError:
        active = None
    killed = bool(_killed(storage, client))
    entry, why = active_for(storage, client, run_id) if run_id else (None, "no KB map for this client yet")
    effective = False
    if entry is not None:
        try:
            load_tables(storage, client, entry.get("build_id"), entry.get("tables_sha256"))
            effective = True
        except (ValueError, FileNotFoundError, StoredFileError) as exc:
            why = str(exc)
    elif why is None:
        why = "no active tables"
    return {"client": client, "run_id": run_id, "taxonomy": frozen,
            "latest_build": _read(storage, client, LATEST_BUILD), "running": lease(storage, client, now),
            "candidate": candidate, "active": active if isinstance(active, dict) else None, "effective": effective,
            "killed": killed, "gate": gate, "why_not_effective": None if effective else why}


# --- the review: overview, one fiche, the queue ------------------------------------------------------------

class _Rows:
    """The refquestions rows already read, behind the table interface scoreboard_service reads."""

    def __init__(self, rows):
        self._rows = [row for row in rows or [] if isinstance(row, dict)]

    def list(self, client, select=None):
        return list(self._rows)


def references(refs_rows, client: str, kbmap=None) -> dict[str, dict]:
    """{ref_id: {"text", "expected"}} of the client's reference questions, read exactly as the scoreboard
    reads them (sb_svc.references_of), each expected fiche with the fiche the graph maps it to."""
    out = {}
    for ref_id, text, expected in sb_svc.references_of(_Rows(refs_rows), client):
        out[ref_id] = {"text": text, "expected": sb_svc.accepted(kbmap, expected) if kbmap is not None else list(expected)}
    return out


def _named(kbmap, fiche_ids) -> list[dict]:
    return [{"fiche_id": f, "label": kbmap.label(f) if f in kbmap.fiches else f} for f in fiche_ids or []]


def _review_tables(storage, client: str, run_id: str) -> RoutingTables | None:
    """The tables of the last build of this run (else the candidate's), for collisions and blocked keys."""
    for name in (LATEST_BUILD, CANDIDATE):
        try:
            pointer = _dict(_read(storage, client, name))
        except StoredFileError:
            continue
        if pointer.get("run_id") != run_id or not pointer.get("build_id"):
            continue
        try:
            return load_tables(storage, client, pointer["build_id"])
        except (ValueError, FileNotFoundError):
            continue
    return None


def _issue_fiches(tables: RoutingTables | None) -> set[str]:
    if tables is None:
        return set()
    out: set[str] = set()
    for fiches in (tables.collisions.get("l0_numbers") or {}).values():
        out.update(fiches)
    for entry in list(tables.collisions.get("l1", [])) + list(tables.collisions.get("l2", [])):
        out.update(entry.get("fiches") or [])
    for entry in list(tables.blocked.get("l1", [])) + list(tables.blocked.get("l2", [])):
        out.update(entry.get("fiches") or [])
        out.update(entry.get("pending") or [])
    return out


def _queue_order(kbmap, ids, referenced: set[str], tables: RoutingTables | None) -> list[str]:
    """The review order: fiches reference questions expect first (they are measured), then fiches in a
    collision or a blocked key (they hold other fiches back), then by label (then id: a stable order)."""
    issues = _issue_fiches(tables)
    return sorted(ids, key=lambda f: (f not in referenced, f not in issues, kbmap.label(f).casefold(), f))


def _proposal_index(storage, client: str, kbmap, ids, taxonomy: Taxonomy | None) -> dict[str, dict]:
    """review.json of this run's last build (one read), else computed from the proposal files."""
    for name in (LATEST_BUILD, CANDIDATE):
        pointer = _dict(_read(storage, client, name))
        build_id = pointer.get("build_id")
        if pointer.get("run_id") != kbmap.run_id or not isinstance(build_id, str) or not _ID_RE.fullmatch(build_id):
            continue
        data = _dict(_read(storage, client, build_paths(build_id)["review"]))
        if data.get("run_id") == kbmap.run_id and isinstance(data.get("fiches"), dict):
            return data["fiches"]
    return _review_index(kbmap, ids, _pass_a(storage, client, kbmap, ids), _pass_c(storage, client, kbmap, taxonomy)[1])


def _taxonomy_proposal(storage, client: str) -> dict | None:
    pointer = _dict(_read(storage, client, TAXONOMY_PROPOSAL))
    build_id = pointer.get("build_id")
    if not isinstance(build_id, str) or not _ID_RE.fullmatch(build_id):
        return None
    proposal = _dict(_read(storage, client, build_paths(build_id)["proposed"]))
    if not proposal:
        return None
    return {"build_id": build_id, "run_id": pointer.get("run_id"), "intents": list(proposal.get("intents") or []),
            "warnings": list(proposal.get("warnings") or []), "error": proposal.get("error"),
            "source": _dict(proposal.get("source"))}


def review_overview(storage, review_rows, refs_rows, client: str, kbmap, status_filter: str | None = None, now=None,
                    current: dict | None = None) -> dict:
    """GET kecore/review: the review of the latest run's routable fiches -- counts, the fiche list (in
    review order; ``status_filter`` keeps one status, counts stay global; an unknown filter is ignored),
    the taxonomy (frozen, pass B's proposal), collisions and blocked keys of the run's last tables, and the
    routing state (candidate, active, gate)."""
    current = current if current is not None else source_hashes(storage, client, kbmap.run_id)
    ids = routable_fiches(kbmap)
    decisions = _decisions(review_rows)
    statuses = fiche_statuses(decisions, current, ids)
    refs = references(refs_rows, client, kbmap)
    referenced = {f for ref in refs.values() for f in ref["expected"]}
    taxonomy = frozen_taxonomy(storage, client)
    tables = _review_tables(storage, client, kbmap.run_id)
    index = _proposal_index(storage, client, kbmap, ids, taxonomy)
    counts = {"proposed": 0, "validated": 0, "rejected": 0, "stale": 0}
    rows = []
    for fiche_id in _queue_order(kbmap, ids, referenced, tables):
        state, decision, _ = statuses[fiche_id]
        counts[state] += 1
        if status_filter in REVIEW_STATUSES and state != status_filter:
            continue
        proposal = _dict(index.get(fiche_id))
        validated = decision if decision is not None and decision.status == "validated" else None
        rows.append({"key": fiche_key(fiche_id), "fiche_id": fiche_id, "label": kbmap.label(fiche_id), "status": state,
                     "intent_a": proposal.get("intent_a"), "intent_c": proposal.get("intent_c"),
                     "intent": validated.intent if validated else None,
                     "primary_app": validated.primary_app if validated else proposal.get("primary_app_a"),
                     "app_inferred": False if validated else bool(proposal.get("app_inferred")),
                     "error": proposal.get("error_a") or proposal.get("error_c"),
                     "referenced": fiche_id in referenced})
    frozen = None
    if taxonomy is not None:
        validated_intents: dict[str, int] = {}
        for decision in current_decisions(decisions, current, ids).values():
            if decision.status == "validated" and decision.intent:
                validated_intents[decision.intent] = validated_intents.get(decision.intent, 0) + 1
        frozen = {"version": taxonomy.version, "sha256": taxonomy.sha256, "frozen_by": taxonomy.frozen_by,
                  "frozen_at": taxonomy.frozen_at, "source_build": taxonomy.source_build,
                  "intents": [{**intent, "fiches": validated_intents.get(intent["id"], 0)} for intent in taxonomy.intents]}
    collisions = {"l0_numbers": {}, "l1": [], "l2": []}
    blocked = {"l1": [], "l2": []}
    if tables is not None:
        collisions = {
            "l0_numbers": {n: _named(kbmap, fiches) for n, fiches in sorted(tables.collisions.get("l0_numbers", {}).items())},
            "l1": [{"tokens": list(c["tokens"]), "fiches": _named(kbmap, c.get("fiches")), "aliases": list(c.get("aliases") or [])}
                   for c in tables.collisions.get("l1", [])],
            "l2": [{"intent": c["intent"], "app": c["app"], "fiches": _named(kbmap, c.get("fiches"))}
                   for c in tables.collisions.get("l2", [])]}
        blocked = {
            "l1": [{"tokens": list(b["tokens"]), "fiches": _named(kbmap, b.get("fiches")), "reason": b.get("reason") or "",
                    "pending": _named(kbmap, b.get("pending"))} for b in tables.blocked.get("l1", [])],
            "l2": [{"intent": b["intent"], "app": b["app"], "fiches": _named(kbmap, b.get("fiches")),
                    "pending": _named(kbmap, b.get("pending"))} for b in tables.blocked.get("l2", [])]}
    routing = status(storage, client, now)
    build_ = _dict(routing["latest_build"])
    return {
        "client": client, "run_id": kbmap.run_id,
        "build": {k: build_.get(k) for k in ("build_id", "kind", "run_id", "state")} if build_ else None,
        "running": routing["running"],
        "taxonomy": {"frozen": frozen, "proposed": _taxonomy_proposal(storage, client)},
        "counts": {**counts, "routable": len(ids)},
        "fiches": rows,
        "collisions": collisions,
        "blocked": blocked,
        "routing": {k: routing[k] for k in ("candidate", "active", "effective", "killed", "gate", "why_not_effective")},
    }


def _alias_ok(text: str) -> bool:
    """An alias the review form may offer: one that ``validate_decision`` would accept as it is."""
    words = len(text.split())
    return (MIN_ALIAS_WORDS <= words <= MAX_ALIAS_WORDS and len(text) <= MAX_ALIAS_CHARS
            and not EMAIL_RE.search(text) and not PHONE_RE.search(text) and alias_problem(text) is None)


def _prefill_aliases(decided: list[dict], proposed: list[tuple[str, str, str]]) -> list[dict]:
    """The decided aliases ticked, then the proposals (source, lang, text) not ticked; one per token key."""
    out, seen = [], set()
    for alias in decided:
        key = token_key(alias.get("text") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": alias.get("text") or "", "lang": alias.get("lang") or "fr",
                    "source": alias.get("source") or "manual", "ref_id": alias.get("ref_id") or "", "checked": True})
    for source, lang, raw in proposed:
        text = " ".join(str(raw or "").split())
        key = token_key(text)
        if not text or key in seen or not _alias_ok(text):
            continue
        seen.add(key)
        out.append({"text": text, "lang": lang, "source": source, "ref_id": "", "checked": False})
    return out


def _l0_number(kbmap, fiche_id: str) -> str | None:
    """The fiche's own number when it designates it alone (the rule ``build_tables`` applies)."""
    for number in own_numbers(kbmap.fiches[fiche_id]):
        holders = [x for x in kbmap.graph.resolve_number(number) if x in kbmap.searchable_set]
        if kbmap.graph.prune(holders)[0] == [fiche_id]:
            return str(number)
    return None


def _proposal_a_view(item: Enrichment | None) -> dict | None:
    if item is None:
        return None
    return {k: getattr(item, k) for k in ("canonical_intent", "intent_label_fr", "primary_app", "supported_apps",
                                          "apps_inferred", "app_inferred", "app_evidence", "semantic_aliases_fr",
                                          "semantic_aliases_en", "trigger_keywords", "dropped", "error")}


def review_fiche(storage, review_rows, refs_rows, client: str, kbmap, key: str, now=None,
                 current: dict | None = None) -> dict:
    """GET kecore/review/fiche: one routable fiche to review, by its key, with its proposals, its decisions
    (current, and previous when its text changed), a pre-filled form, the L0 identifiers it would route by
    and the reference questions expecting it. UnknownFicheError (404) for a key of no routable fiche."""
    if not is_key(key):
        raise ValueError("key: 32 lowercase hexadecimal characters")
    current = current if current is not None else source_hashes(storage, client, kbmap.run_id)
    ids = routable_fiches(kbmap)
    fiche_id = {fiche_key(f): f for f in ids}.get(key)
    if fiche_id is None or fiche_id not in current:
        raise UnknownFicheError("unknown fiche, or not routable in this run")
    decisions = _decisions(review_rows)
    statuses = fiche_statuses(decisions, current, ids)
    state, decision, previous = statuses[fiche_id]
    refs = references(refs_rows, client, kbmap)
    referenced = {f for ref in refs.values() for f in ref["expected"]}
    queue = [f for f in _queue_order(kbmap, ids, referenced, _review_tables(storage, client, kbmap.run_id))
             if statuses[f][0] == state]
    taxonomy = frozen_taxonomy(storage, client)
    position = {f: i for i, f in enumerate(kbmap.ranked)}
    a = _enrichment_at(storage, client, kbmap.run_id, position[fiche_id], fiche_id)
    _, assigned = _pass_c(storage, client, kbmap, taxonomy, only=fiche_id)
    c = assigned.get(fiche_id)
    c = c if c is not None and c.content_sha256 == content_sha256(kbmap.fiches[fiche_id]) else None
    a_now = _current_a(kbmap, fiche_id, a)
    codes = candidate_codes(kbmap).get(fiche_id, [])
    ids_of_taxonomy = set(taxonomy.ids()) if taxonomy else set()

    # the form: the current decision, else (text changed) the previous one, else the proposals
    basis = decision if decision is not None and decision.status == "validated" else (
        previous if state == "stale" and previous is not None and previous.status == "validated" else None)
    proposed = []
    if c is not None and not c.error:
        proposed += [("pass_c", "fr", t) for t in c.core_aliases_fr] + [("pass_c", "en", t) for t in c.core_aliases_en]
    if a_now is not None:
        proposed += [("pass_a", "fr", t) for t in a_now.semantic_aliases_fr] + \
                    [("pass_a", "en", t) for t in a_now.semantic_aliases_en]
    if basis is not None:
        # an intent whose meaning changed since (refrozen taxonomy) is not offered again silently
        intent = basis.intent if (basis.intent and taxonomy is not None
                                  and taxonomy.fingerprint(basis.intent) == basis.intent_fingerprint) else ""
        prefill = {"intent": intent, "primary_app": basis.primary_app or "",
                   "aliases": _prefill_aliases(basis.aliases, proposed),
                   "codes": [x for x in basis.codes if x in codes]}
    else:
        # pass C's core aliases ticked (short, written for L1), pass A's offered unticked; an inferred
        # application (no quote, not read by kecore) is never pre-selected; codes are never pre-ticked
        core = [{"text": " ".join(text.split()), "lang": lang, "source": source, "ref_id": ""}
                for source, lang, text in proposed if source == "pass_c" and _alias_ok(" ".join(text.split()))]
        intent = c.intent if c is not None and not c.error and c.intent in ids_of_taxonomy else ""
        app = a_now.primary_app if a_now is not None and a_now.primary_app and not a_now.app_inferred else ""
        prefill = {"intent": intent or "", "primary_app": app or "",
                   "aliases": _prefill_aliases(core, [item for item in proposed if item[0] == "pass_a"]),
                   "codes": []}

    validated_aliases = basis.aliases if basis is not None else []
    alias_refs = {a_.get("ref_id") for a_ in validated_aliases if a_.get("source") == "reference"}
    alias_texts = {" ".join((a_.get("text") or "").split()) for a_ in validated_aliases}
    fiche_refs = [{"ref_id": ref_id, "text": ref["text"], "expected": list(ref["expected"]),
                   "tokens": list(route_tokens(clean_text(ref["text"])[:MAX_TEXT_CHARS])),
                   "is_alias": ref_id in alias_refs or " ".join(ref["text"].split()) in alias_texts}
                  for ref_id, ref in sorted(refs.items()) if fiche_id in ref["expected"]]
    latest_build = _dict(_read(storage, client, LATEST_BUILD))
    return {
        "client": client, "run_id": kbmap.run_id, "build_id": latest_build.get("build_id"), "key": key,
        "fiche_id": fiche_id, "label": kbmap.label(fiche_id), "source_sha256": current[fiche_id], "status": state,
        "position": {"index": queue.index(fiche_id) + 1, "total": len(queue)},
        "text_excerpt": (kbmap.fiches[fiche_id].text or "")[:TEXT_EXCERPT_CHARS],
        "proposal_a": _proposal_a_view(a),
        "proposal_c": ({k: getattr(c, k) for k in ("intent", "core_aliases_fr", "core_aliases_en", "dropped", "error")}
                       if c is not None else None),
        "decision": decision.to_dict() if decision is not None else None,
        "previous_decision": previous.to_dict() if previous is not None else None,
        "prefill": prefill,
        "taxonomy": [{"id": i["id"], "label_fr": i["label_fr"]} for i in taxonomy.intents] if taxonomy else [],
        "apps": app_vocabulary(kbmap.dictionary),
        "candidate_codes": list(codes),
        "l0_number": _l0_number(kbmap, fiche_id),
        "references": fiche_refs,
        "seen": decision.decided_at if decision is not None else "",
    }


def next_key(storage, review_rows, refs_rows, client: str, kbmap, status_: str, current: dict | None = None
             ) -> str | None:
    """GET kecore/review/next: the key of the first fiche of that status (proposed or stale) in review
    order (``_queue_order``), None when the queue is empty."""
    if status_ not in QUEUES:
        raise ValueError(f"status: one of {', '.join(QUEUES)}")
    current = current if current is not None else source_hashes(storage, client, kbmap.run_id)
    ids = routable_fiches(kbmap)
    statuses = fiche_statuses(_decisions(review_rows), current, ids)
    referenced = {f for ref in references(refs_rows, client, kbmap).values() for f in ref["expected"]}
    for fiche_id in _queue_order(kbmap, ids, referenced, _review_tables(storage, client, kbmap.run_id)):
        if statuses[fiche_id][0] == status_:
            return fiche_key(fiche_id)
    return None


# --- decisions -----------------------------------------------------------------------------------------------

@dataclass
class _Context:
    """What a decision is checked against, read once (a carry-over checks many decisions)."""

    kbmap: object
    current: dict
    ids: list
    taxonomy: Taxonomy | None
    apps: set
    references: dict
    codes: dict


def _context(storage, client: str, kbmap, refs_rows, current: dict | None = None) -> _Context:
    return _Context(kbmap=kbmap, current=current if current is not None else source_hashes(storage, client, kbmap.run_id),
                    ids=routable_fiches(kbmap), taxonomy=frozen_taxonomy(storage, client),
                    apps=set(app_vocabulary(kbmap.dictionary)), references=references(refs_rows, client, kbmap),
                    codes=candidate_codes(kbmap))


def _store(ctx: _Context, table, client: str, body: dict, now) -> ReviewDecision:
    decision = validate_decision(body, current=ctx.current, routable=set(ctx.ids), taxonomy=ctx.taxonomy,
                                 apps=ctx.apps, references=ctx.references, candidate_codes=ctx.codes, now=now)
    for alias in decision.aliases:
        # a reference question promoted as an alias is that question, word for word: anything else would also
        # mark the reference "seen" and take it out of the gate's comparison
        if alias["source"] == "reference":
            wanted = " ".join(str(ctx.references[alias["ref_id"]]["text"]).split())
            if " ".join(alias["text"].split()) != wanted:
                raise ValueError(f"alias « {alias['text']} » : ce n'est pas le texte de la question de référence "
                                 f"« {alias['ref_id']} »")
    decision.run_id = ctx.kbmap.run_id or ""
    row_key = decision_row_key(decision.fiche_id, decision.source_sha256)
    for _ in range(WRITE_TRIES):
        row, etag = table.read(client, row_key)
        stored = ReviewDecision.from_row(row) if row else None
        check_seen(body, stored)
        decision.revision = stored.revision + 1 if stored is not None else 0
        entity = decision.to_row(client)
        if table.create(entity) if row is None else table.merge_if(entity, etag):
            return decision
    raise ChangedError("la décision de cette fiche a changé pendant l'enregistrement : rechargez la fiche")


def _collisions(ctx: _Context, table, client: str, decision: ReviewDecision) -> list[dict]:
    """Each alias of ``decision`` that another fiche's current validated decision also holds (same routing
    tokens): neither fiche will route on it (``build_tables`` lists it as a collision)."""
    if decision.status != "validated" or not decision.aliases:
        return []
    claims: dict[tuple[str, ...], set[str]] = {}
    for fiche_id, other in current_decisions(_decisions(table.list(client)), ctx.current, ctx.ids).items():
        if fiche_id == decision.fiche_id or other.status != "validated":
            continue
        for alias in other.aliases:
            claims.setdefault(token_key(alias.get("text") or ""), set()).add(fiche_id)
    out = []
    for alias in decision.aliases:
        others = claims.get(token_key(alias["text"]))
        if others:
            out.append({"alias": alias["text"], "fiches": sorted(others)})
    return out


def decide(table, storage, client: str, body: dict, kbmap, refs_rows, now) -> dict:
    """POST kecore/review/decision: a person's decision on one fiche, checked (kefind.review.validate_decision,
    French refusals: ValueError -> 400, UnknownFicheError -> 404, ChangedError -> 409), then written to the
    review table: created, or merged into the row as the reviewer saw it (``seen``: its decided_at; a row
    changed meanwhile is a 409, never silently overwritten), with ``revision`` + 1. Returns {"decision",
    "collisions": [{"alias", "fiches"}]} (other current validated fiches already holding one of its aliases)."""
    if not isinstance(body, dict):
        raise ValueError("décision illisible : un objet JSON est attendu")
    ctx = _context(storage, client, kbmap, refs_rows)
    decision = _store(ctx, table, client, body, now)
    return {"decision": decision.to_dict(), "collisions": _collisions(ctx, table, client, decision)}


def carry_forward(table, storage, client: str, body: dict, kbmap, refs_rows, now) -> dict:
    """POST kecore/review/carry: each stale fiche's previous decision posted again on its new text, checked
    again in full (an alias, a code or a reference the new text no longer allows is refused for that fiche).
    An intent whose meaning changed since (refrozen taxonomy) is never carried: a person decides again.
    ``body``: the validated request (``validate_carry_request``). Returns {"carried": n, "failed": [{"key",
    "error"}]} -- French errors, shown to the reviewer."""
    ctx = _context(storage, client, kbmap, refs_rows)
    statuses = fiche_statuses(_decisions(table.list(client)), ctx.current, ctx.ids)
    by_key = {fiche_key(f): f for f in ctx.ids}
    carried, failed = 0, []
    for key in body["keys"]:
        fiche_id = by_key.get(key)
        if fiche_id is None or fiche_id not in ctx.current:
            failed.append({"key": key, "error": "fiche inconnue ou hors du routage de ce run"})
            continue
        state, _, previous = statuses[fiche_id]
        if state != "stale" or previous is None:
            failed.append({"key": key, "error": "cette fiche n'est pas « à revoir » : rien à reconduire"})
            continue
        if previous.intent and (ctx.taxonomy is None
                                or ctx.taxonomy.fingerprint(previous.intent) != previous.intent_fingerprint):
            failed.append({"key": key, "error": f"l'intention « {previous.intent} » a changé de sens ou n'est plus dans "
                                                "la taxonomie figée : décidez à nouveau"})
            continue
        repost = {"fiche_id": fiche_id, "source_sha256": ctx.current[fiche_id], "seen": "", "status": previous.status,
                  "intent": previous.intent or "", "primary_app": previous.primary_app or "",
                  "aliases": [dict(a) for a in previous.aliases], "codes": list(previous.codes), "note": previous.note,
                  "by": body["by"], "by_id": body.get("by_id") or ""}
        try:
            _store(ctx, table, client, repost, now)
        except (ValueError, LookupError) as exc:
            failed.append({"key": key, "error": str(exc)[:300]})
            continue
        carried += 1
    return {"carried": carried, "failed": failed}


# --- the taxonomy freeze ---------------------------------------------------------------------------------------

def freeze_taxonomy(storage, review_rows, body: dict, now, kbmap=None) -> dict:
    """POST kecore/review/taxonomy: a person freezes the client's taxonomy (kefind.taxonomy.freeze; a refusal
    names the row, in French -> 400). The previous version stays in taxonomy.history/. An identical content
    makes no new version. When the new taxonomy takes an intent away from fiches a person validated (gone,
    or with another label or description: a new meaning), it is refused (OrphansError -> 409, with them)
    unless ``orphans_ok`` is true -- their intent then stops routing until a person validates them again.
    Two freezes from the same version: the second is refused (ChangedError -> 409), nothing overwritten.
    ``kbmap``: the latest map (labels, and which decisions are current); without it, each fiche's latest
    decision counts."""
    if not isinstance(body, dict):
        raise ValueError("demande illisible : un objet JSON est attendu")
    by = body.get("by")
    by = " ".join(by.split()) if isinstance(by, str) else ""
    if not by:
        raise ValueError("auteur manquant")
    if len(by) > MAX_BY_CHARS:
        raise ValueError(f"auteur trop long ({MAX_BY_CHARS} caractères au plus)")
    source_build = body.get("source_build") or ""
    if not isinstance(source_build, str) or (source_build and not _ID_RE.fullmatch(source_build)):
        raise ValueError("source_build invalide")
    orphans_ok = body.get("orphans_ok", False)
    if not isinstance(orphans_ok, bool):
        raise ValueError("orphans_ok : vrai ou faux")
    client = body["client"]
    old = frozen_taxonomy(storage, client)
    new = freeze(body.get("intents"), (old.version + 1) if old else 1, by, str(now), source_build)
    if old is not None and new.sha256 == old.sha256:
        return {"version": old.version, "sha256": old.sha256, "intents": old.intents, "orphans": [], "unchanged": True}
    removed = set(removed_intents(old, new))
    orphans = []
    if removed:
        decisions = _decisions(review_rows)
        if kbmap is not None:
            applying = current_decisions(decisions, source_hashes(storage, client, kbmap.run_id), routable_fiches(kbmap))
        else:  # no map: each fiche's latest decision
            applying = {}
            for decision in decisions:
                known = applying.get(decision.fiche_id)
                if known is None or (decision.decided_at, decision.revision) > (known.decided_at, known.revision):
                    applying[decision.fiche_id] = decision
        for fiche_id, decision in sorted(applying.items()):
            if decision.status == "validated" and decision.intent in removed:
                label = kbmap.label(fiche_id) if kbmap is not None and fiche_id in kbmap.fiches else fiche_id
                orphans.append({"fiche_id": fiche_id, "label": label, "intent": decision.intent})
    if orphans and not orphans_ok:
        raise OrphansError(f"{len(orphans)} fiche(s) validée(s) perdraient leur intention (retirée ou changée de "
                           "sens) : confirmez pour figer quand même, elles seront à revalider", orphans)
    if not _write_if_absent(storage, client, f"{TAXONOMY_HISTORY}v{new.version}.json", new.to_dict()):
        raise ChangedError("une autre version de la taxonomie vient d'être figée : rechargez la page")
    _write(storage, client, TAXONOMY, new.to_dict())
    return {"version": new.version, "sha256": new.sha256, "intents": new.intents, "orphans": orphans}


__all__ = [
    "source_hashes", "frozen_taxonomy", "validate_routing_request", "validate_activate_request",
    "validate_deactivate_request", "validate_carry_request", "lease", "prepare", "propose_taxonomy", "assign",
    "assign_summary", "build", "finish", "load_tables", "active_for", "activate", "deactivate", "status",
    "references", "review_overview", "review_fiche", "next_key", "decide", "carry_forward", "freeze_taxonomy",
    "build_paths", "gate_path", "is_key", "StoredFileError", "OrphansError", "ChangedError", "UnknownFicheError",
    "LEASE_TTL_S", "QUEUES",
]
