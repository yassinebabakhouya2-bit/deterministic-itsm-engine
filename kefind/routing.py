"""Deterministic routing at runtime: the routing tables and the code that reads them (levels L0 to L2).

The routing spec (docs/v10-deterministic-engine.md, "Semantic enrichment at ingestion, deterministic
routing at runtime") moves the meaning upstream: a person validates, per fiche, an intent of the client's
closed taxonomy, an application and a few short requests ("aliases"); this module turns those validated
decisions into tables, and reads a question against them, code only:

- L0, an identifier: a fiche number the question cites (``KB0233``) that only one fiche carries, or an
  error / event code only one validated fiche knows;
- L1, a validated alias: every routing token of the alias is among the question's routing tokens; the
  most specific alias wins (its tokens include every other match's), else nothing is decided here;
- L2, (intent, application): the question mapped onto the closed enum by a recorded model call
  (``kefind.route``), then looked up here.

This first part is the L1 normalization, frozen and versioned (``NORMALIZATION_VERSION``): the same text
always gives the same tokens, at ingestion (aliases) and at question time. It is deliberately NOT
``kefind.search.tokenize`` (shared with BM25F and the graph's shingles: changing one must never change the
other) nor ``kefind.semantic.normalize_text`` (the embedding and record key).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field

from .enrich import app_vocabulary
from .funnel import _ticket_entities
from .graph import own_numbers

NORMALIZATION_VERSION = 1

# Characters NFKD does not decompose: "mise en œuvre" must not become "mise en uvre".
_LIGATURES = str.maketrans({"œ": "oe", "Œ": "oe", "æ": "ae", "Æ": "ae", "ß": "ss", "ø": "o", "Ø": "o",
                            "đ": "d", "Đ": "d", "ł": "l", "Ł": "l"})
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Frozen grammar words (folded: no accent, lower case). Only words that never carry the need itself:
# negations ("pas", "not", "no", "plus", "aucun") stay tokens on purpose. Changing this list changes
# NORMALIZATION_VERSION, every table's sha256, and so what a person validated: never edit it in place.
STOP_WORDS = frozenset("""
a ai as au aux avec c ce ces cet cette comment d dans de des dois doit du elle elles en est et etre
faut il ils j je l la le les leur leurs lui m ma me mes moi mon n ne nos notre nous on ont ou par peut
peux pour pouvez puis qu quand que quel quelle quelles quels qui quoi s sa se ses si son sont sur suis
svp t ta te tes toi ton tu un une vos votre vous y bonjour merci
an and are at be by can could do does for from hello hi how i in is it its me my of on or please
that the this to what when where why with you your
""".split())


def fold_token(token: str) -> str:
    """The frozen inflection rule (version 1), applied the same way to aliases and questions:
    a final "s" goes (not "ss", "us", "is"; 4 letters at least: "comptes" -> "compte", "mots" -> "mot",
    "teams" -> "team"), then a final "ee" loses one "e" ("bloquee" -> "bloque"). Digits never change.
    Symmetric, so it only has to make two spellings of one word meet, never be good French."""
    if token.isdigit():
        return token
    if len(token) >= 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        token = token[:-1]
    if len(token) >= 4 and token.endswith("ee"):
        token = token[:-1]
    return token


def _fold_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").translate(_LIGATURES))
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def route_tokens(text: str) -> tuple[str, ...]:
    """The routing tokens of ``text``, in order of first appearance, each once: folded (accents, case,
    ligatures), split on anything but [a-z0-9], one-character tokens and stop words dropped, inflections
    folded (``fold_token``)."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(_fold_text(text)):
        if len(raw) < 2 or raw in STOP_WORDS:
            continue
        token = fold_token(raw)
        if token in STOP_WORDS or len(token) < 2:
            continue
        if token not in out:
            out.append(token)
    return tuple(out)


def token_key(text: str) -> tuple[str, ...]:
    """The sorted routing tokens: two aliases with the same key are the same alias for L1."""
    return tuple(sorted(route_tokens(text)))


def ignored_characters(text: str) -> list[str]:
    """The one-character letters and digits of ``text`` that routing drops (not grammar words): "imprimante
    étage 3" and "imprimante étage 4" have the same routing tokens, so an alias must never rely on them."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(_fold_text(text)):
        if len(raw) == 1 and raw not in STOP_WORDS and raw not in out:
            out.append(raw)
    return out


def alias_problem(text: str) -> str | None:
    """Why ``text`` cannot be an L1 alias, in French (shown to the reviewer), None when it can: fewer than
    two routing tokens, or a one-character letter or digit that routing would silently ignore."""
    ignored = ignored_characters(text)
    if ignored:
        shown = ", ".join(f"« {c} »" for c in ignored)
        return f"le routage ignore {shown} : écrire l'alias sans, ou avec le mot entier"
    if len(route_tokens(text)) < 2:
        return "moins de 2 mots utiles au routage"
    return None


# --- the routing tables and their matchers (L0 to L2) ---------------------------------------------------
#
# Everything below is code only and float-free: the same tables and the same question give the same
# decision, byte for byte. A table is built from what a person validated (kefind.review), never from a
# proposal alone, and a fiche a person has not reviewed yet BLOCKS what it proposes instead of leaving
# another fiche alone in front: a partial review never makes a key look unique.

TABLES_VERSION = 1
ROUTE_VERSION = 1          # kefind.route's level logic
MAP_PROMPT_VERSION = 1     # kefind.route's L2 prompt and schema
L1_MAX_UNCOVERED = 1       # question tokens an L1 alias may leave uncovered
MIN_GATE_QUESTIONS = 30    # measurable questions the scoreboard's gate needs before "passed"
# The code the tables were built for: inside content_sha256, so a gate measured under one code version
# never vouches for another, and kecore_func refuses to serve tables built for different code.
CODE_VERSION = {"tables": TABLES_VERSION, "normalization": NORMALIZATION_VERSION, "route": ROUTE_VERSION,
                "map_prompt": MAP_PROMPT_VERSION, "l1_max_uncovered": L1_MAX_UNCOVERED}
# A fiche number as a technician writes it to designate a fiche: "KB0233", "kb-233", never "KB 233" nor
# "512 kb 300" (the funnel's permissive pattern still reads those, so they still restrict the scope).
L0_NUMBER_RE = re.compile(r"(?i)(?<![\w.])KB[-_]?0*(\d{2,7})(?![\w])")
L0_CODE_PREFIXES = ("err:", "evt:")


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def _is_routable(kbmap, fiche_id: str) -> bool:
    return (fiche_id in kbmap.searchable_set and kbmap.graph.canonical(fiche_id) == fiche_id
            and kbmap.graph.prune([fiche_id])[0] == [fiche_id])


def routable(kbmap) -> list[str]:
    """The fiches that can be reviewed and routed to: searchable, not a duplicate (``kbmap.ranked``) and not
    replaced by another fiche -- today's engine never shows a superseded fiche (``graph.prune``), so no
    table may route to one either. Order of ``kbmap.ranked`` (sorted ids)."""
    return [f for f in kbmap.ranked if kbmap.graph.prune([f])[0] == [f]]


def _code_owner(kbmap, code: str) -> str | None:
    """The one fiche an error / event code designates after the graph, None when it is shared."""
    pruned = kbmap.graph.prune(kbmap.entity_index.get(code, []))[0]
    return pruned[0] if len(pruned) == 1 else None


def candidate_codes(kbmap) -> dict[str, list[str]]:
    """{fiche_id: the error / event codes only this routable fiche carries}, sorted: the codes a reviewer
    may validate as L0 identifiers (``kefind.review.validate_decision``), with the same uniqueness rule
    ``build_tables`` applies again at build time."""
    allowed = set(routable(kbmap))
    out: dict[str, list[str]] = {}
    for code in kbmap.entity_index:
        if code.startswith(L0_CODE_PREFIXES):
            owner = _code_owner(kbmap, code)
            if owner in allowed:
                out.setdefault(owner, []).append(code)
    return {f: sorted(codes) for f, codes in sorted(out.items())}


def kb_sha256(kbmap, current: dict[str, str]) -> str:
    """What the tables' behaviour depends on besides their own content: the routable fiches and their
    source hashes (a new, changed or removed fiche), the other searchable fiches' (their codes and numbers
    resolve through the graph), the graph's duplicate and replacement relations, and the client's
    dictionary (which applications a question names). Equal content_sha256 AND kb_sha256: the same
    answers, so an activation may carry over to a new run without a new gate."""
    ids = routable(kbmap)
    routed = set(ids)
    relations = []
    for fiche_id, node in sorted(kbmap.graph.nodes.items()):
        if node.duplicate_of:
            relations.append([fiche_id, "duplicate_of", node.duplicate_of])
        relations.extend([fiche_id, "superseded_by", target] for target in sorted(node.superseded_by))
    return _sha({
        "routable": [[f, current.get(f) or ""] for f in sorted(ids)],
        "searchable": [[f, current.get(f) or ""] for f in kbmap.searchable if f not in routed],
        "graph": relations,
        "dictionary": {k: sorted(str(v) for v in values) for k, values in sorted(kbmap.dictionary.items())},
    })


@dataclass
class RoutingTables:
    """One build's routing tables, immutable once sealed (``content_sha256``, ``sha256``)."""

    client: str
    run_id: str
    build_id: str
    code_version: dict = field(default_factory=lambda: dict(CODE_VERSION))
    taxonomy: dict = field(default_factory=dict)       # {"version","sha256","intents":[{"id","label_fr",
                                                       # "description_fr"}]} or {} (no frozen taxonomy: no L2)
    apps: list[str] = field(default_factory=list)      # the application vocabulary (kefind.enrich.app_vocabulary)
    kb_sha256: str = ""
    l0_numbers: dict[str, str] = field(default_factory=dict)   # "233" -> fiche_id
    l0_codes: dict[str, str] = field(default_factory=dict)     # "err:0x80070005" -> fiche_id
    l1: list[dict] = field(default_factory=list)       # [{"tokens","fiche_id","alias","lang","source","ref_id","app"}]
    l2: list[dict] = field(default_factory=list)       # [{"intent","app","fiches"}] (app "": names no application)
    blocked: dict = field(default_factory=lambda: {"l1": [], "l2": []})
    collisions: dict = field(default_factory=lambda: {"l0_numbers": {}, "l1": [], "l2": []})
    stats: dict = field(default_factory=dict)
    content_sha256: str = ""
    sha256: str = ""

    def content(self) -> dict:
        """What the routing decisions depend on, nothing else: no run or build id, no statistic, no alias
        wording (only its tokens), so an identical rebuild -- or the next run's -- has the same value. The
        colliding L1 token sets are in it: they still block a less specific alias (``match_l1``)."""
        return {
            "code_version": self.code_version,
            "taxonomy_sha256": (self.taxonomy or {}).get("sha256", ""),
            "apps": self.apps,
            "l0_numbers": self.l0_numbers,
            "l0_codes": self.l0_codes,
            "l1": [[e["tokens"], e["fiche_id"], e.get("app") or ""] for e in self.l1],
            "l1_collisions": [[c["tokens"], c["fiches"]] for c in (self.collisions or {}).get("l1", [])],
            "l2": self.l2,
            "blocked": self.blocked,
        }

    def compute_content_sha256(self) -> str:
        return _sha(self.content())

    def compute_sha256(self) -> str:
        data = self.to_dict()
        data.pop("sha256", None)
        return _sha(data)

    def seal(self) -> "RoutingTables":
        self.content_sha256 = self.compute_content_sha256()
        self.sha256 = self.compute_sha256()
        return self

    def verify(self) -> bool:
        """True when both hashes match the tables as they are (a blob altered or cut is refused)."""
        return self.content_sha256 == self.compute_content_sha256() and self.sha256 == self.compute_sha256()

    def to_dict(self) -> dict:
        return asdict(self)

    def to_bytes(self) -> bytes:
        return (json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, indent=1) + "\n").encode("utf-8")

    @classmethod
    def from_dict(cls, data: dict) -> "RoutingTables":
        """Tolerant: unknown keys ignored, missing ones defaulted. The hashes are read, never recomputed:
        ``verify`` says whether they still hold."""
        data = data or {}
        out = cls(**{"client": "", "run_id": "", "build_id": "",
                     **{k: v for k, v in data.items() if k in cls.__dataclass_fields__ and v is not None}})
        out.blocked = {"l1": [], "l2": [], **(out.blocked or {})}
        out.collisions = {"l0_numbers": {}, "l1": [], "l2": [], **(out.collisions or {})}
        return out


def _alias_texts(values) -> list[str]:
    out = []
    for value in values or []:
        text = value.get("text") if isinstance(value, dict) else value
        if isinstance(text, str):
            out.append(text)
    return out


def build_tables(kbmap, current: dict[str, str], taxonomy, decisions: list, proposals: dict[str, dict],
                 build_id: str) -> RoutingTables:
    """The routing tables of one run, from ALL stored decisions (stale ones too: a stale fiche's previous
    aliases still block) and the proposals of the fiches nobody reviewed yet.

    ``current``: {fiche_id: source_sha256}; ``taxonomy``: the frozen taxonomy (version, sha256, intents,
    ids(), fingerprint(id)) or None (no L2); ``proposals``: {fiche_id: {"intent": pass-C intent | None,
    "app": pass-A primary app | None, "aliases": [pass-A and pass-C alias texts]}} (may be partial).

    - L0 numbers: each own number of a routable fiche not rejected, when the graph resolves it to that
      fiche alone (a number two fiches carry is a collision);
    - L0 codes: the codes a person validated, kept while the graph still resolves them to that fiche alone;
    - L1: validated aliases (2 routing tokens at least) with the decision's application; a token set two
      fiches claim is a collision, one a fiche under review proposes (or validated before its text
      changed) is blocked;
    - L2 (with a taxonomy): (intent, application) -> validated fiches, the intent's meaning unchanged since
      the decision (fingerprint); a key is blocked while a fiche under review proposes the same intent with
      the same application, or with none -- or, for the key without application, with any application."""
    from .review import fiche_statuses  # kefind.review reads this module's normalization: imported here

    ids = routable(kbmap)
    statuses = fiche_statuses(decisions, current, ids)
    apps = app_vocabulary(kbmap.dictionary)
    app_set = set(apps)
    skipped = {"alias_tokens": 0, "intent_not_in_taxonomy": 0, "intent_changed": 0, "app_not_in_vocabulary": 0,
               "code_not_unique": 0}
    counts = {"proposed": 0, "validated": 0, "rejected": 0, "stale": 0}
    for status, _, _ in statuses.values():
        counts[status] += 1
    validated = {f: entry[1] for f, entry in statuses.items() if entry[0] == "validated"}
    under_review = [f for f, entry in statuses.items() if entry[0] in ("proposed", "stale")]

    def previous_validated(fiche_id):
        status, _, previous = statuses[fiche_id]
        return previous if status == "stale" and previous is not None and previous.status == "validated" else None

    # L0 numbers: no review needed (the number designates the fiche), but never a rejected fiche
    l0_numbers: dict[str, str] = {}
    number_collisions: dict[str, list[str]] = {}
    for fiche_id in ids:
        if statuses[fiche_id][0] == "rejected":
            continue
        for number in own_numbers(kbmap.fiches[fiche_id]):
            holders = [x for x in kbmap.graph.resolve_number(number) if x in kbmap.searchable_set]
            resolved = kbmap.graph.prune(holders)[0]
            if resolved == [fiche_id]:
                l0_numbers[str(number)] = fiche_id
            elif len(resolved) > 1:
                number_collisions[str(number)] = sorted(resolved)

    # L0 codes: only those a person validated, and only while still unique
    l0_codes: dict[str, str] = {}
    for fiche_id, decision in validated.items():
        for code in decision.codes:
            if code.startswith(L0_CODE_PREFIXES) and _code_owner(kbmap, code) == fiche_id:
                l0_codes[code] = fiche_id
            else:
                skipped["code_not_unique"] += 1

    # an application no longer in the vocabulary: the fiche's L1 / L2 entries would never match as decided
    usable = {}
    for fiche_id, decision in validated.items():
        if decision.primary_app and decision.primary_app not in app_set:
            skipped["app_not_in_vocabulary"] += 1
        else:
            usable[fiche_id] = decision

    # L1
    claims: dict[tuple[str, ...], dict[str, dict]] = {}
    for fiche_id, decision in usable.items():
        for alias in decision.aliases:
            text = alias.get("text") or ""
            tokens = token_key(text)
            if len(tokens) < 2 or alias_problem(text):
                skipped["alias_tokens"] += 1
                continue
            claims.setdefault(tokens, {}).setdefault(fiche_id, {
                "tokens": list(tokens), "fiche_id": fiche_id, "alias": text, "lang": alias.get("lang") or "",
                "source": alias.get("source") or "", "ref_id": alias.get("ref_id") or "",
                "app": decision.primary_app or ""})
    waiting: dict[tuple[str, ...], set[str]] = {}
    for fiche_id in under_review:
        texts = _alias_texts((proposals.get(fiche_id) or {}).get("aliases"))
        previous = previous_validated(fiche_id)
        if previous is not None:
            texts += _alias_texts(previous.aliases)
        for text in texts:
            tokens = token_key(text)
            if len(tokens) >= 2:
                waiting.setdefault(tokens, set()).add(fiche_id)
    l1: list[dict] = []
    l1_collisions: list[dict] = []
    l1_blocked: list[dict] = []
    for tokens, owners in sorted(claims.items()):
        if len(owners) > 1:
            l1_collisions.append({"tokens": list(tokens), "fiches": sorted(owners),
                                  "aliases": sorted({e["alias"] for e in owners.values()})})
        elif tokens in waiting:
            l1_blocked.append({"tokens": list(tokens), "fiches": sorted(owners), "reason": "pending review",
                               "pending": sorted(waiting[tokens])})
        else:
            l1.extend(owners.values())
    l1.sort(key=lambda e: (e["tokens"], e["fiche_id"]))

    # L2
    l2: list[dict] = []
    l2_blocked: list[dict] = []
    l2_collisions: list[dict] = []
    taxonomy_dict: dict = {}
    if taxonomy is not None:
        taxonomy_dict = {"version": taxonomy.version, "sha256": taxonomy.sha256,
                         "intents": sorted(({"id": i.get("id"), "label_fr": i.get("label_fr") or "",
                                             "description_fr": i.get("description_fr") or ""}
                                            for i in taxonomy.intents), key=lambda i: str(i["id"]))}
        known = set(taxonomy.ids())
        keys: dict[tuple[str, str], list[str]] = {}
        for fiche_id, decision in usable.items():
            if not decision.intent:
                continue
            if decision.intent not in known:
                skipped["intent_not_in_taxonomy"] += 1
            elif decision.intent_fingerprint != taxonomy.fingerprint(decision.intent):
                skipped["intent_changed"] += 1
            else:
                keys.setdefault((decision.intent, decision.primary_app or ""), []).append(fiche_id)
        proposed: dict[str, list[tuple[str, str]]] = {}
        for fiche_id in under_review:
            proposal = proposals.get(fiche_id) or {}
            if isinstance(proposal.get("intent"), str) and proposal["intent"]:
                proposed.setdefault(proposal["intent"], []).append((fiche_id, proposal.get("app") or ""))
            previous = previous_validated(fiche_id)
            if previous is not None and previous.intent:
                proposed.setdefault(previous.intent, []).append((fiche_id, previous.primary_app or ""))
        for (intent, app), fiches in sorted(keys.items()):
            entry = {"intent": intent, "app": app, "fiches": sorted(fiches)}
            pending = sorted({f for f, other in proposed.get(intent, []) if other in (app, "") or app == ""})
            if len(fiches) > 1:
                l2_collisions.append(dict(entry, fiches=list(entry["fiches"])))
            if pending:
                l2_blocked.append({**entry, "pending": pending})
            else:
                l2.append(entry)

    tables = RoutingTables(
        client=kbmap.client, run_id=kbmap.run_id or "", build_id=build_id, code_version=dict(CODE_VERSION),
        taxonomy=taxonomy_dict, apps=apps, kb_sha256=kb_sha256(kbmap, current),
        l0_numbers=dict(sorted(l0_numbers.items())), l0_codes=dict(sorted(l0_codes.items())), l1=l1, l2=l2,
        blocked={"l1": l1_blocked, "l2": l2_blocked},
        collisions={"l0_numbers": dict(sorted(number_collisions.items())), "l1": l1_collisions, "l2": l2_collisions},
        stats={"routable": len(ids), **counts, "l0_numbers": len(l0_numbers), "l0_codes": len(l0_codes),
               "l1": len(l1), "l2": len(l2), "skipped": skipped})
    return tables.seal()


# --- matching a question ---------------------------------------------------------------------------------

def identifier_scope(kbmap, cleaned_text: str, answers=()) -> dict:
    """The fiches the question's identifiers point to, read exactly as the funnel reads them
    (``kefind.funnel._ticket_entities``): each cited fiche number (the funnel's permissive pattern) and
    each known error / event code, resolved through the graph. A number or code no fiche carries points
    nowhere and is left out, as the funnel ignores it. L0 decides only on the intersection; L1 and L2 may
    only return a fiche of the union when the question carries identifiers at all (``any``)."""
    known, _, _, step = _ticket_entities(kbmap, cleaned_text, answers)
    resolutions: list[list[str]] = []
    for fiches in step["fiche_numbers"].values():
        pruned = sorted(kbmap.graph.prune(fiches)[0])
        if pruned:
            resolutions.append(pruned)
    for code in known:
        if code.startswith(L0_CODE_PREFIXES):
            pruned = sorted(kbmap.graph.prune(kbmap.entity_index.get(code, []))[0])
            if pruned:
                resolutions.append(pruned)
    sets = [set(r) for r in resolutions]
    return {"resolutions": resolutions,
            "intersection": sorted(set.intersection(*sets)) if sets else [],
            "union": sorted(set().union(*sets)),
            "any": bool(sets)}


def match_l0(tables: RoutingTables, kbmap, cleaned_text: str, scope: dict) -> dict:
    """L0: decided only when every identifier of the question points to the same single routable fiche
    (``scope["intersection"] == [f]``) AND at least one of them is a table entry for it: its number written
    the strict way (``L0_NUMBER_RE``), or a code a person validated for it."""
    out = {"decided": False, "rule": None, "key": None, "fiche_id": None, "scope": scope}
    inter = scope.get("intersection") or []
    if len(inter) != 1 or not _is_routable(kbmap, inter[0]):
        return out
    fiche_id = inter[0]
    for match in L0_NUMBER_RE.finditer(cleaned_text):
        number = str(int(match.group(1)))
        if tables.l0_numbers.get(number) == fiche_id:
            out.update(decided=True, rule="fiche_number", key=number, fiche_id=fiche_id)
            return out
    known = _ticket_entities(kbmap, cleaned_text, ())[0]
    for code in known:
        if code.startswith(L0_CODE_PREFIXES) and tables.l0_codes.get(code) == fiche_id:
            out.update(decided=True, rule="identifier", key=code, fiche_id=fiche_id)
            return out
    return out


def _most_specific(matches: list[dict], blockers: list[dict]) -> dict | None:
    """The match whose token set strictly includes every other fiche's matches and every blocking token
    set the question also holds; None when there is none (ambiguous). Only one fiche can qualify; among
    its aliases, the longest wins (then by tokens, then by wording, for a stable result)."""
    winners = []
    for entry in matches:
        mine = set(entry["tokens"])
        others = [set(m["tokens"]) for m in matches if m["fiche_id"] != entry["fiche_id"]]
        others += [set(b["tokens"]) for b in blockers]
        if all(other < mine for other in others):
            winners.append(entry)
    return min(winners, key=lambda e: (-len(e["tokens"]), e["tokens"], e["alias"])) if winners else None


def match_l1(tables: RoutingTables, cleaned_text: str, question_apps: set[str], scope: dict) -> dict:
    """L1: the validated aliases whose routing tokens are all in the question's. An alias of a fiche about
    another application than the one(s) the question names is left out. The most specific alias decides;
    a token set two fiches claim, or one a fiche under review proposes, still competes (it can only stop a
    less specific alias, never decide). Then the question may say at most ``L1_MAX_UNCOVERED`` routing
    token more than the alias ("Outlook démarre mais ne synchronise pas" is not "Outlook ne démarre pas"),
    and the fiche must be among those the question's identifiers point to, if any."""
    question = set(route_tokens(cleaned_text))
    apps = set(question_apps or ())
    matches = [e for e in tables.l1 if set(e["tokens"]) <= question
               and not (e.get("app") and apps and e["app"] not in apps)]
    blockers = [{"tokens": list(b["tokens"]), "fiches": sorted(set(b.get("fiches") or []) | set(b.get("pending") or []))}
                for b in list(tables.collisions.get("l1", [])) + list(tables.blocked.get("l1", []))
                if set(b["tokens"]) <= question]
    out = {"decided": False, "fiche_id": None, "key": None, "tokens": [],
           "matches": [{"fiche_id": e["fiche_id"], "alias": e["alias"]} for e in matches],
           "blockers": blockers, "ambiguous": False, "uncovered": 0, "outside_scope": False, "candidate": None}
    if not matches:
        return out
    winner = _most_specific(matches, blockers)
    if winner is None:
        out["ambiguous"] = True
        return out
    fiche_id = winner["fiche_id"]
    out["candidate"] = fiche_id
    out["uncovered"] = len(question - set(winner["tokens"]))
    out["outside_scope"] = bool(scope.get("any")) and fiche_id not in (scope.get("union") or [])
    if out["uncovered"] <= L1_MAX_UNCOVERED and not out["outside_scope"]:
        out.update(decided=True, fiche_id=fiche_id, key=winner["alias"], tokens=list(winner["tokens"]))
    return out


def lookup_l2(tables: RoutingTables, intent: str | None, app: str | None, question_has_app: bool) -> dict:
    """L2: the validated fiches of (intent, application). Only an exact key gives an exact fiche:
    - with an application: exact when (intent, app) holds one fiche; several are offered; a missing key
      offers the fiches of (intent, "") -- fiches naming no application are never exact for one that does;
    - without: exact only when the question names no application at all and the intent's only validated
      fiche is the one naming no application; otherwise every fiche of the intent is offered.
    A blocked key (a fiche under review proposes it) is never exact: its fiches and the pending ones are
    offered. Returns {"exact": fiche_id | None, "offer": [ids, sorted], "reason": str}."""
    if not intent:
        return {"exact": None, "offer": [], "reason": "no_intent"}
    keys = {(e["intent"], e["app"]): sorted(e["fiches"]) for e in tables.l2}
    blocked = {(e["intent"], e["app"]): sorted(set(e["fiches"]) | set(e.get("pending") or []))
               for e in tables.blocked.get("l2", [])}
    if app:
        key = (intent, app)
        if key in blocked:
            return {"exact": None, "offer": blocked[key], "reason": "pending_review"}
        if key in keys:
            fiches = keys[key]
            if len(fiches) == 1:
                return {"exact": fiches[0], "offer": [], "reason": "intent_app"}
            return {"exact": None, "offer": fiches, "reason": "several_fiches"}
        generic = blocked.get((intent, "")) or keys.get((intent, ""))
        if generic:
            return {"exact": None, "offer": generic, "reason": "no_fiche_for_app_generic_offered"}
        return {"exact": None, "offer": [], "reason": "no_fiche"}
    union: set[str] = set()
    any_blocked = False
    for (key_intent, _), fiches in keys.items():
        if key_intent == intent:
            union.update(fiches)
    for (key_intent, _), fiches in blocked.items():
        if key_intent == intent:
            union.update(fiches)
            any_blocked = True
    if not union:
        return {"exact": None, "offer": [], "reason": "no_fiche"}
    generic = keys.get((intent, "")) or []
    if not question_has_app and not any_blocked and len(generic) == 1 and union == set(generic):
        return {"exact": generic[0], "offer": [], "reason": "intent_no_app"}
    reason = ("pending_review" if any_blocked else "question_names_an_application" if question_has_app
              else "several_fiches" if len(union) > 1 else "fiche_of_an_application")
    return {"exact": None, "offer": sorted(union), "reason": reason}


__all__ = ["NORMALIZATION_VERSION", "STOP_WORDS", "fold_token", "route_tokens", "token_key", "ignored_characters",
           "alias_problem", "TABLES_VERSION", "ROUTE_VERSION", "MAP_PROMPT_VERSION", "L1_MAX_UNCOVERED",
           "MIN_GATE_QUESTIONS", "CODE_VERSION", "L0_NUMBER_RE", "L0_CODE_PREFIXES", "routable", "candidate_codes",
           "kb_sha256", "RoutingTables", "build_tables", "identifier_scope", "match_l0", "match_l1", "lookup_l2"]
