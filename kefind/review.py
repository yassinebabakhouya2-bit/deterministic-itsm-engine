"""Review decisions: what a person validated, per fiche, before anything routes on it.

The routing spec (docs/v10-deterministic-engine.md, "Semantic enrichment at ingestion, deterministic
routing at runtime") lets nothing route unless a person validated it. Passes A and C only PROPOSE; a
reviewer, in the Web App's "Revue" tab, validates per fiche an intent of the client's frozen taxonomy, an
application, a few short requests ("aliases") and the error / event codes that only this fiche carries --
or rejects the fiche. This module is the code side of that decision, pure functions only:

- ``validate_decision`` turns a posted form into a ``ReviewDecision`` or refuses it with a FRENCH message
  (shown to the reviewer as is). Every rule the routing tables rely on is checked here first, so a stored
  decision never holds an alias L1 cannot see the way the person read it ("imprimante étage 3").
- A decision is keyed by the fiche AND the hash of its source text (``source_sha256``, the extracted text
  of ``runs/<run>/fiches.jsonl`` before boilerplate removal, computed by the caller): when the document
  changes, the decision no longer applies ("stale") and the fiche goes back to review, its previous
  decision shown as context. Adding OTHER documents never stales a decision.
- ``current_status`` / ``current_decisions`` / ``status_counts`` read the stored decisions against the
  current hashes; ``seen_reference_ids`` lists the reference questions a reviewer wrote an alias from, so
  the scoreboard's gate never measures right-first on the very phrasings it was tuned to.

Storage is the caller's (``kecore_func``): one Azure Tables row per (fiche, source hash), written through
``to_row``. Every string is written, "" for None, because the Tables SDK drops None properties on MERGE and
a re-decision must overwrite the previous intent or application.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

from kecore.text import EMAIL_RE, PHONE_RE, clean_text

from .funnel import MAX_TEXT_CHARS
from .routing import alias_problem, route_tokens, token_key

STATUSES = ("validated", "rejected")
ALIAS_SOURCES = ("pass_a", "pass_c", "reference", "manual")
LANGS = ("fr", "en")
MAX_ALIASES = 40
MAX_ALIAS_WORDS = 12
MIN_ALIAS_WORDS = 2
MAX_ALIAS_CHARS = 120
MAX_NOTE_CHARS = 500
MAX_CODES = 20
MAX_BY_CHARS = 120
MAX_BY_ID_CHARS = 64


class ChangedError(ValueError):
    """The fiche or its decision changed since the reviewer saw it (HTTP 409): reload, then decide."""


class UnknownFicheError(LookupError):
    """The fiche is not a routable fiche of the run under review (HTTP 404)."""


def fiche_key(fiche_id: str) -> str:
    """A fiche id is a document name (spaces, en dashes, '/'): URLs and RowKeys carry this hash instead.
    Same value as ``kecore_func.tickets_service.fiche_row_key``."""
    return hashlib.sha256(fiche_id.encode("utf-8")).hexdigest()[:32]


def decision_row_key(fiche_id: str, source_sha256: str) -> str:
    """The RowKey of one decision: one row per (fiche, source text), so a changed document never
    overwrites the decision taken on its previous text (it stays readable as "previous")."""
    return hashlib.sha256(f"{fiche_id}\n{source_sha256}".encode("utf-8")).hexdigest()[:32]


def _alias(item: dict) -> dict:
    return {"text": str(item.get("text") or ""), "lang": str(item.get("lang") or ""),
            "source": str(item.get("source") or ""), "ref_id": str(item.get("ref_id") or "")}


def _sorted_aliases(aliases) -> list[dict]:
    return sorted((_alias(a) for a in aliases or [] if isinstance(a, dict)),
                  key=lambda a: (a["lang"], a["text"], a["source"], a["ref_id"]))


def _json_list(value) -> list:
    """A list stored as JSON in a table row (or already a list, from a snapshot); [] when unreadable."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass
class ReviewDecision:
    fiche_id: str
    source_sha256: str
    status: str                                   # "validated" | "rejected"
    intent: str | None = None                     # an id of the frozen taxonomy, None when none
    intent_fingerprint: str = ""                  # taxonomy.fingerprint(intent) when decided: a refreeze that
                                                  # changes the intent's meaning stops it from routing
    taxonomy_version: int = 0                     # the frozen taxonomy the intent was chosen from (0: none)
    primary_app: str | None = None                # an application id of the client's vocabulary, None when none
    aliases: list[dict] = field(default_factory=list)   # [{"text","lang","source","ref_id"}] by (lang, text)
    codes: list[str] = field(default_factory=list)      # validated L0 codes ("err:0x80070005"), sorted
    note: str = ""
    decided_by: str = ""
    decided_by_id: str = ""
    decided_at: str = ""
    revision: int = 0                             # set by the caller from the stored row (re-decisions)
    run_id: str = ""                              # the kecore run the reviewer saw, set by the caller

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewDecision":
        """Tolerant: unknown keys ignored, missing ones defaulted, lists normalized and sorted."""
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__ and v is not None}
        out = cls(**{"fiche_id": "", "source_sha256": "", "status": "", **known})
        out.fiche_id, out.source_sha256, out.status = str(out.fiche_id), str(out.source_sha256), str(out.status)
        out.intent = str(out.intent) if out.intent else None          # "" (a table row) means None
        out.primary_app = str(out.primary_app) if out.primary_app else None
        out.aliases = _sorted_aliases(_json_list(out.aliases))
        out.codes = sorted({str(c) for c in _json_list(out.codes) if c})
        out.taxonomy_version, out.revision = _int(out.taxonomy_version), _int(out.revision)
        for name in ("intent_fingerprint", "note", "decided_by", "decided_by_id", "decided_at", "run_id"):
            setattr(out, name, str(getattr(out, name) or ""))
        return out

    def to_row(self, client: str) -> dict:
        """The Azure Tables entity. EVERY string property is written, "" for None: the Tables SDK drops
        None properties, and a MERGE would then keep the previous intent or application of a re-decided
        fiche. Lists are JSON ("[]" when empty), integers stay integers."""
        return {
            "PartitionKey": client,
            "RowKey": decision_row_key(self.fiche_id, self.source_sha256),
            "fiche_id": self.fiche_id or "",
            "source_sha256": self.source_sha256 or "",
            "status": self.status or "",
            "intent": self.intent or "",
            "intent_fingerprint": self.intent_fingerprint or "",
            "taxonomy_version": int(self.taxonomy_version or 0),
            "primary_app": self.primary_app or "",
            "aliases": json.dumps(_sorted_aliases(self.aliases), sort_keys=True, ensure_ascii=False),
            "codes": json.dumps(sorted(self.codes or []), ensure_ascii=False),
            "note": self.note or "",
            "decided_by": self.decided_by or "",
            "decided_by_id": self.decided_by_id or "",
            "decided_at": self.decided_at or "",
            "revision": int(self.revision or 0),
            "run_id": self.run_id or "",
        }

    @classmethod
    def from_row(cls, row: dict) -> "ReviewDecision":
        """A table row back to a decision: "" means None for the intent and the application; the table's
        own properties (PartitionKey, RowKey, etag, Timestamp) are ignored."""
        return cls.from_dict({k: v for k, v in (row or {}).items() if k in cls.__dataclass_fields__})


# --- validation ------------------------------------------------------------------------------------------

def _string(body: dict, name: str) -> str:
    value = body.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"champ « {name} » : texte attendu")
    return value.strip()


def _expected(reference: dict) -> list[str]:
    return [str(x) for x in _json_list((reference or {}).get("expected"))]


def check_seen(body: dict, stored: ReviewDecision | None) -> None:
    """The decision the reviewer saw (``body["seen"]``: its ``decided_at``, "" when there was none) must
    still be the stored one for this (fiche, source hash): two reviewers never silently overwrite each
    other. Raises ``ChangedError``."""
    seen = str((body or {}).get("seen") or "")
    if (stored.decided_at if stored is not None else "") != seen:
        raise ChangedError("la décision de cette fiche a changé depuis son affichage : rechargez la fiche")


def validate_decision(body: dict, *, current: dict[str, str], routable: set[str], taxonomy, apps: set[str],
                      references: dict[str, dict], candidate_codes: dict[str, list[str]],
                      now: str) -> ReviewDecision:
    """The decision a reviewer posted, checked against what the routing tables need; refusals are French
    (shown to the reviewer). ``current``: {fiche_id: source_sha256} of the run under review; ``routable``:
    ``kefind.routing.routable(kbmap)``; ``taxonomy``: the frozen taxonomy (``kefind.taxonomy.Taxonomy``:
    version, ids(), fingerprint(id)) or None; ``apps``: the client's application vocabulary;
    ``references``: {ref_id: {"text", "expected": [fiche ids]}}; ``candidate_codes``: {fiche_id: the codes
    only this fiche carries} (``kefind.routing.candidate_codes``).

    Raises ``UnknownFicheError`` (not a routable fiche), ``ChangedError`` (the fiche's text changed since
    it was shown) or ``ValueError``. ``revision`` and ``run_id`` are left to the caller, which reads the
    stored row (``check_seen``)."""
    if not isinstance(body, dict):
        raise ValueError("décision illisible : un objet JSON est attendu")
    fiche_id = body.get("fiche_id")
    if not isinstance(fiche_id, str) or fiche_id not in routable or fiche_id not in current:
        raise UnknownFicheError("fiche inconnue ou hors du routage de ce run")
    if body.get("source_sha256") != current[fiche_id]:
        raise ChangedError("le texte de la fiche a changé depuis son affichage : rechargez la fiche")
    status = body.get("status")
    if status not in STATUSES:
        raise ValueError(f"statut « {status} » inconnu : validated ou rejected")
    by = " ".join(_string(body, "by").split())
    if not by:
        raise ValueError("auteur de la décision manquant")
    if len(by) > MAX_BY_CHARS:
        raise ValueError(f"auteur trop long ({MAX_BY_CHARS} caractères au plus)")
    by_id = _string(body, "by_id")
    if len(by_id) > MAX_BY_ID_CHARS:
        raise ValueError(f"identifiant d'auteur trop long ({MAX_BY_ID_CHARS} caractères au plus)")
    note = _string(body, "note")
    if len(note) > MAX_NOTE_CHARS:
        raise ValueError(f"note trop longue ({MAX_NOTE_CHARS} caractères au plus)")
    decision = ReviewDecision(fiche_id=fiche_id, source_sha256=current[fiche_id], status=status, note=note,
                              decided_by=by, decided_by_id=by_id, decided_at=str(now or ""))
    if status == "rejected":
        return decision  # a rejected fiche routes on nothing: intent, application, aliases, codes stay empty

    # intent: one of the frozen taxonomy, with the fingerprint of its meaning at decision time
    intent = _string(body, "intent")
    if intent:
        if taxonomy is None:
            raise ValueError("aucune taxonomie n'est figée : l'intention doit rester vide")
        if intent not in set(taxonomy.ids()):
            raise ValueError(f"intention « {intent} » absente de la taxonomie figée (version {taxonomy.version})")
        decision.intent = intent
        decision.intent_fingerprint = taxonomy.fingerprint(intent)
        decision.taxonomy_version = int(taxonomy.version)

    app = _string(body, "primary_app")
    if app:
        if app not in apps:
            raise ValueError(f"application « {app} » absente du vocabulaire d'applications du client")
        decision.primary_app = app

    # codes: only identifiers this fiche alone carries (shown on the review page)
    codes = body.get("codes") or []
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
        raise ValueError("codes : une liste de textes est attendue")
    if len(codes) > MAX_CODES:
        raise ValueError(f"{len(codes)} codes : {MAX_CODES} au plus")
    allowed = set(candidate_codes.get(fiche_id) or [])
    for code in codes:
        if code not in allowed:
            raise ValueError(f"code « {code} » : pas un identifiant propre à cette fiche")
    decision.codes = sorted(set(codes))

    # aliases: what L1 will match, checked the way L1 reads them
    aliases = body.get("aliases") or []
    if not isinstance(aliases, list):
        raise ValueError("alias : une liste est attendue")
    if len(aliases) > MAX_ALIASES:
        raise ValueError(f"{len(aliases)} alias : {MAX_ALIASES} au plus")
    kept: dict[tuple[str, ...], dict] = {}
    for n, item in enumerate(aliases, 1):
        if not isinstance(item, dict):
            raise ValueError(f"alias n° {n} : illisible")
        raw = item.get("text")
        text = " ".join(raw.split()) if isinstance(raw, str) else ""
        if not text:
            raise ValueError(f"alias n° {n} : texte vide")
        if EMAIL_RE.search(text) or PHONE_RE.search(text):
            raise ValueError(f"alias n° {n} : une adresse e-mail ou un numéro de téléphone n'est jamais un alias")
        if len(text) > MAX_ALIAS_CHARS:
            raise ValueError(f"alias « {text[:60]}… » : {MAX_ALIAS_CHARS} caractères au plus")
        words = len(text.split())
        if not MIN_ALIAS_WORDS <= words <= MAX_ALIAS_WORDS:
            raise ValueError(f"alias « {text} » : de {MIN_ALIAS_WORDS} à {MAX_ALIAS_WORDS} mots")
        problem = alias_problem(text)
        if problem:
            raise ValueError(f"alias « {text} » : {problem}")
        lang = item.get("lang")
        if lang not in LANGS:
            raise ValueError(f"alias « {text} » : langue « {lang} » inconnue (fr ou en)")
        source = item.get("source")
        if source not in ALIAS_SOURCES:
            raise ValueError(f"alias « {text} » : origine « {source} » inconnue")
        ref_id = ""
        if source == "reference":
            ref_id = item.get("ref_id") if isinstance(item.get("ref_id"), str) else ""
            if not ref_id:
                raise ValueError(f"alias « {text} » : question de référence manquante")
            if ref_id not in references or fiche_id not in _expected(references[ref_id]):
                raise ValueError(f"alias « {text} » : question de référence « {ref_id} » inconnue "
                                 "ou qui n'attend pas cette fiche")
        key = token_key(text)
        if key not in kept:  # the same alias for L1 (same routing tokens): the first one is kept, silently
            kept[key] = {"text": text, "lang": lang, "source": source, "ref_id": ref_id}
    decision.aliases = _sorted_aliases(kept.values())
    return decision


# --- reading the stored decisions ------------------------------------------------------------------------

def current_status(fiche_id: str, source_sha256: str, decisions_of_fiche: list[ReviewDecision]
                   ) -> tuple[str, ReviewDecision | None, ReviewDecision | None]:
    """(status, current, previous) of one fiche. ``current``: the decision taken on this source text
    (highest revision); ``previous``: the latest decision (by decided_at, then revision) taken on another
    text of the same fiche. Status: the current decision's, else "stale" when only a previous one
    exists, else "proposed" (never reviewed)."""
    mine = [d for d in decisions_of_fiche if d.fiche_id == fiche_id]
    now = [d for d in mine if source_sha256 and d.source_sha256 == source_sha256]
    before = [d for d in mine if d.source_sha256 != source_sha256]
    current = max(now, key=lambda d: (d.revision, d.decided_at, d.status), default=None)
    previous = max(before, key=lambda d: (d.decided_at, d.revision, d.source_sha256, d.status), default=None)
    if current is not None:
        return current.status, current, previous
    return ("stale" if previous is not None else "proposed"), None, previous


def fiche_statuses(decisions: list[ReviewDecision], current: dict[str, str], routable
                   ) -> dict[str, tuple[str, ReviewDecision | None, ReviewDecision | None]]:
    """``current_status`` of every routable fiche, by fiche id (sorted). A fiche with no current source
    hash has no current decision: its decisions all count as previous ones."""
    by_fiche: dict[str, list[ReviewDecision]] = {}
    for decision in decisions:
        by_fiche.setdefault(decision.fiche_id, []).append(decision)
    return {f: current_status(f, current.get(f) or "", by_fiche.get(f, [])) for f in sorted(set(routable))}


def current_decisions(decisions: list[ReviewDecision], current: dict[str, str], routable
                      ) -> dict[str, ReviewDecision]:
    """The decision that applies today to each routable fiche (validated or rejected), by fiche id."""
    return {f: entry[1] for f, entry in fiche_statuses(decisions, current, routable).items() if entry[1] is not None}


def status_counts(decisions: list[ReviewDecision], current: dict[str, str], routable) -> dict[str, int]:
    counts = {"proposed": 0, "validated": 0, "rejected": 0, "stale": 0}
    for status, _, _ in fiche_statuses(decisions, current, routable).values():
        counts[status] += 1
    return counts


def seen_reference_ids(decisions_current: dict[str, ReviewDecision], references: dict[str, dict]) -> list[str]:
    """The reference questions a reviewer could have tuned an alias to, sorted. The scoreboard leaves them
    out of the right-first comparison (they still count for wrong exact fiches). A reference is "seen"
    when it is the source of a validated alias, or when a validated alias of a fiche it expects has all
    its routing tokens among the reference's (L1 would route it through an alias the reviewer wrote with
    the reference on screen, even reworded)."""
    sources: set[str] = set()
    token_sets: dict[str, list[frozenset[str]]] = {}
    for fiche_id, decision in decisions_current.items():
        if decision.status != "validated":
            continue
        for alias in decision.aliases:
            if alias.get("source") == "reference" and alias.get("ref_id"):
                sources.add(alias["ref_id"])
            text = alias.get("text") or ""
            tokens = token_key(text)
            if len(tokens) >= 2 and not alias_problem(text):  # what build_tables keeps: no other alias routes
                token_sets.setdefault(fiche_id, []).append(frozenset(tokens))
    seen = []
    for ref_id, reference in sorted(references.items()):
        if ref_id in sources:
            seen.append(ref_id)
            continue
        question = set(route_tokens(clean_text(str((reference or {}).get("text") or ""))[:MAX_TEXT_CHARS]))
        if any(tokens <= question for f in _expected(reference) for tokens in token_sets.get(f, [])):
            seen.append(ref_id)
    return seen


__all__ = ["STATUSES", "ALIAS_SOURCES", "MAX_ALIASES", "MAX_ALIAS_WORDS", "MAX_ALIAS_CHARS", "MAX_NOTE_CHARS",
           "MAX_CODES", "ChangedError", "UnknownFicheError", "ReviewDecision", "fiche_key", "decision_row_key",
           "check_seen", "validate_decision", "current_status", "fiche_statuses", "current_decisions",
           "status_counts", "seen_reference_ids"]
