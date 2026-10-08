"""The client dictionary's online loop and its review (V10 slice 5, pilier 2).

kecore learns a client's software dictionary offline, from its KB (``kecore.profile``). Live
questions bring new names too: a question that reaches ``POST /api/kecore/find`` with an
``observe`` key (the Diagnostic sends a hash of its session, never the session id) is observed:
a product-like name after a trigger word -- "l'application X", "le logiciel Y" -- that the
dictionary does not know yet. A candidate counts once per distinct session (however many times a
session asks again); one seen in ``MIN_OBSERVATIONS`` distinct sessions is offered for review, and
nothing reaches the dictionary without a person's decision.

Personal data: the question is never stored. A candidate is short (``MAX_TERM_WORDS`` words,
``MAX_TERM_CHARS`` characters: a longer run of capitalized words is a sentence, a signature or
people's names, and is dropped), and until it was seen in ``MIN_OBSERVATIONS`` distinct sessions
only a hash of it, its count and hashes of the sessions are stored (table ``kefindpending``). Its
spelling is written once it reaches the threshold, when it is very unlikely to be anything but the
name of a piece of software.

Decisions are rows of the same table, set once with optimistic concurrency (If-Match): two
reviewers never lose each other's decision, and a decision is never undone by a concurrent
observation (an observation never writes the status). The next kecore run (``POST
/api/kecore/runs``) reads them (``decisions``) with the optional hand-written
kecore-<client>/dictionary-decisions.json {"rejected": [...], "accepted": [{"spelling",
"canonical"}]}: the fiches must be read again with the new dictionary for an entity to find them.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

import kecore_pipeline as pipeline
from kecore.pending import DEFAULT_MIN_OBSERVATIONS
from kecore.profile import _dictionary_candidates

MIN_OBSERVATIONS = DEFAULT_MIN_OBSERVATIONS
MAX_TERM_WORDS = 3
MAX_TERM_CHARS = 40
MAX_OBSERVED_CHARS = 4000  # the beginning of a question is enough to name its software
MAX_KEYS = 8               # the latest sessions remembered per candidate (hashes), for distinctness
WRITE_TRIES = 3            # optimistic concurrency: a write that lost the race reads the row again
OBSERVE_KEY_RE = re.compile(r"[0-9a-f]{16,64}")  # always fullmatch: a hash of the session, never its id
_TERM_RE = re.compile(r"[^\x00-\x1f]{1,80}")  # always fullmatch
_CANONICAL_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")  # always fullmatch: an entry id


def term_row_key(client: str, term: str) -> str:
    return hashlib.sha256(f"kefindpending|{client}|{term.lower()}".encode("utf-8")).hexdigest()[:32]


def entry_row_key(entry: str) -> str:
    return "entry-" + hashlib.sha256(entry.encode("utf-8")).hexdigest()[:32]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _known(dictionary: dict[str, list[str]]) -> set[str]:
    known = {entry.lower() for entry in dictionary}
    for forms in dictionary.values():
        known.update(form.lower() for form in forms)
    return known


def candidate_terms(text: str, dictionary: dict[str, list[str]]) -> dict[str, str]:
    """term (lower case) -> spelling: the short product-like names of one question the dictionary does not know."""
    known = _known(dictionary)
    out: dict[str, str] = {}
    for spelling in sorted(_dictionary_candidates(text[:MAX_OBSERVED_CHARS])):
        term = spelling.lower()
        if term in known or term in out or len(spelling) > MAX_TERM_CHARS or len(spelling.split()) > MAX_TERM_WORDS:
            continue
        out[term] = spelling
    return out


def _keys(row: dict) -> list[str]:
    try:
        keys = json.loads(row.get("keys") or "[]")
    except ValueError:
        return []
    return [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []


def _observe_one(table, client: str, term: str, spelling: str, key: str) -> str:
    row_key = term_row_key(client, term)
    row, etag = table.read(client, row_key)
    now = _now()
    if row is None:
        entity = {"PartitionKey": client, "RowKey": row_key, "kind": "candidate", "status": "pending", "seen": 1,
                  "keys": json.dumps([key]), "first_seen": now, "last_seen": now, "term": "", "spelling": ""}
        if MIN_OBSERVATIONS <= 1:
            entity.update(term=term, spelling=spelling)
        return "counted" if table.create(entity) else "conflict"
    if row.get("status") != "pending":
        return "decided"  # the decision stands: no more counting
    keys = _keys(row)
    if key in keys:
        return "same_session"
    seen = int(row.get("seen") or 0) + 1
    update = {"PartitionKey": client, "RowKey": row_key, "seen": seen, "keys": json.dumps((keys + [key])[-MAX_KEYS:]),
              "last_seen": now}
    if seen >= MIN_OBSERVATIONS:
        update.update(term=term, spelling=row.get("spelling") or spelling)
    return "counted" if table.merge_if(update, etag) else "conflict"


def observe(table, client: str, text: str, dictionary: dict[str, list[str]], key: str) -> list[str]:
    """One question of one session (``key``: a hash of the session) observed. Returns the terms counted."""
    if not isinstance(key, str) or not OBSERVE_KEY_RE.fullmatch(key):
        return []
    counted = []
    for term, spelling in candidate_terms(text, dictionary).items():
        outcome = "conflict"
        for _ in range(WRITE_TRIES):
            outcome = _observe_one(table, client, term, spelling, key)
            if outcome != "conflict":
                break
        if outcome == "counted":
            counted.append(term)
    return counted


def _recorded(rows: list[dict]) -> dict:
    rejected, accepted = set(), []
    for row in rows:
        status = row.get("status")
        if row.get("kind") == "entry" and status == "rejected" and row.get("entry"):
            rejected.add(row["entry"])
        elif row.get("kind") == "candidate" and row.get("spelling"):
            if status == "rejected":
                rejected.add(row["spelling"])
            elif status == "accepted":
                accepted.append({"spelling": row["spelling"], "canonical": row.get("canonical") or None})
    return {"rejected": sorted(rejected), "accepted": sorted(accepted, key=lambda a: a["spelling"].lower())}


def decisions(table, client: str) -> dict:
    """The decisions recorded in the review tab, as the next kecore run reads them:
    {"rejected": [entry ids and spellings], "accepted": [{"spelling", "canonical"}]}."""
    return _recorded(table.list(client))


def review(storage: pipeline.Storage, table, client: str, dictionary: dict[str, list[str]], run_id: str | None) -> dict:
    """What the review tab shows: the current dictionary, the candidates ready for a decision, the decisions.
    ValueError when the hand-written dictionary-decisions.json is invalid."""
    rows = table.list(client)
    recorded = _recorded(rows)
    rejected = set(pipeline.dictionary_rejected(storage, client)) | set(recorded["rejected"])
    candidates = [r for r in rows if r.get("kind") == "candidate"]
    ready = sorted((r for r in candidates if r.get("status") == "pending" and r.get("term")
                    and int(r.get("seen") or 0) >= MIN_OBSERVATIONS),
                   key=lambda r: (-int(r.get("seen") or 0), r["term"]))
    return {
        "client": client, "run_id": run_id, "min_observations": MIN_OBSERVATIONS,
        "dictionary": [{"id": entry_id, "forms": forms, "rejected": entry_id in rejected}
                       for entry_id, forms in sorted(dictionary.items())],
        "ready": [{"term": r["term"], "spelling": r.get("spelling") or r["term"], "seen": int(r.get("seen") or 0)}
                  for r in ready],
        "watching": sum(1 for r in candidates if r.get("status") == "pending"
                        and int(r.get("seen") or 0) < MIN_OBSERVATIONS),
        "decided": sorted(({"term": r.get("term"), "spelling": r.get("spelling"), "status": r.get("status"),
                            "canonical": r.get("canonical") or None, "decided_by": r.get("decided_by") or ""}
                           for r in candidates if r.get("status") in ("accepted", "rejected") and r.get("term")),
                          key=lambda d: d["term"]),
        "decisions": {"rejected": sorted(rejected),
                      "accepted": pipeline.dictionary_accepted(storage, client) + recorded["accepted"]},
    }


def validate_decision(body, allowed_clients) -> dict:
    """{"client", "term": "<candidate>", "accept": bool, "canonical": "<entry id>"|null, "by": "<name>"}
    or {"client", "entry": "<dictionary entry id>", "accept": false, "by": ...} to reject an entry."""
    if not isinstance(body, dict):
        raise ValueError("a JSON object is expected")
    client = body.get("client")
    if not isinstance(client, str) or client not in set(allowed_clients):
        raise ValueError("unknown client")
    accept = body.get("accept")
    if not isinstance(accept, bool):
        raise ValueError("accept must be true or false")
    term, entry, canonical = body.get("term"), body.get("entry"), body.get("canonical")
    if (term is None) == (entry is None):
        raise ValueError("give either a candidate term or a dictionary entry")
    if term is not None and (not isinstance(term, str) or not _TERM_RE.fullmatch(term)):
        raise ValueError("invalid term")
    if entry is not None and (not isinstance(entry, str) or not _CANONICAL_RE.fullmatch(entry) or accept):
        raise ValueError("an entry of the dictionary can only be rejected (accept: false)")
    if canonical is not None and (not isinstance(canonical, str) or not _CANONICAL_RE.fullmatch(canonical)):
        raise ValueError("canonical: the id of an existing dictionary entry (letters, digits, '-')")
    by = body.get("by") or ""
    if not isinstance(by, str) or len(by) > 120:
        raise ValueError("invalid by")
    return {"client": client, "term": term.lower() if term else None, "entry": entry, "accept": accept,
            "canonical": canonical, "by": by}


def decide(table, payload: dict) -> dict:
    """Records one decision, once. FileNotFoundError: no such candidate; ValueError: already decided."""
    client, now = payload["client"], _now()
    if payload["entry"] is not None:
        row = {"PartitionKey": client, "RowKey": entry_row_key(payload["entry"]), "kind": "entry",
               "entry": payload["entry"], "status": "rejected", "decided_by": payload["by"], "decided_at": now}
        if not table.create(row):
            raise ValueError(f"the entry {payload['entry']!r} was already rejected")
        return {"client": client, "entry": payload["entry"], "status": "rejected",
                "applies_at": "next kecore run (POST /api/kecore/runs)"}
    status = "accepted" if payload["accept"] else "rejected"
    for _ in range(WRITE_TRIES):
        row, etag = table.read(client, term_row_key(client, payload["term"]))
        if row is None or row.get("kind") != "candidate" or row.get("term") != payload["term"]:
            raise FileNotFoundError("unknown candidate")  # never observed, or not seen often enough yet
        if row.get("status") != "pending":
            raise ValueError(f"{payload['term']!r} was already {row.get('status')}")
        update = {"PartitionKey": client, "RowKey": row["RowKey"], "status": status,
                  "canonical": payload["canonical"] or "", "decided_by": payload["by"], "decided_at": now}
        if table.merge_if(update, etag):  # lost only to a concurrent writer: read the row again
            return {"client": client, "term": payload["term"], "spelling": row.get("spelling"), "status": status,
                    "canonical": payload["canonical"], "applies_at": "next kecore run (POST /api/kecore/runs)"}
    raise ValueError(f"{payload['term']!r} is being written by someone else: try again")


__all__ = ["observe", "decisions", "review", "validate_decision", "decide", "candidate_terms", "term_row_key",
           "entry_row_key", "MIN_OBSERVATIONS", "OBSERVE_KEY_RE"]
