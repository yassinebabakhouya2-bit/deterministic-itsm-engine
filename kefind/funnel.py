"""Trouver la fiche par le code (V10 tranche 3) : entités d'abord, graphe ensuite, texte pour départager.

La carte du KB d'un client (``KBMap``) est figée par un run kecore : ses fiches décomposées
(étapes vérifiées, entités canoniques), son profil (dont le dictionnaire du client) et le graphe
entre fiches (``kefind.graph``). Pour un ticket, tout ce qui suit est du code. Le LLM n'intervient
qu'en amont, s'il est demandé : ``kefind.interpret`` traduit le ticket en termes de recherche dans
les mots des fiches (anglais et français), vérifiés par le code ; ces termes s'ajoutent aux mots du
ticket à l'étape 4, ils ne filtrent jamais et ne décident de rien.

1. Entités du ticket : ``kecore.entities.extract_entities`` avec le MÊME dictionnaire que celui
   qui a servi à lire les fiches. Une entité absente de la carte est ignorée, et tracée.
2. Filtre par niveaux d'entités, du plus informatif au moins informatif : réponses du technicien
   à une question précédente (toutes doivent tenir), numéro de fiche cité (KB0052, résolu par le
   graphe), identifiants (code d'erreur, événement, numéro KB ServiceNow),
   mise à jour, application, éléments techniques (chemin, registre, commande, URL, menu,
   raccourci). Dans un niveau, une fiche passe si elle porte AU MOINS UNE des entités du ticket ;
   elle doit passer TOUS les niveaux retenus. Quand plus aucune fiche ne passe, le dernier niveau
   retenu est retiré, puis le précédent : chaque essai est tracé. Le système d'exploitation ne
   filtre jamais (un ticket le cite souvent en passant) ; il sert aux questions.
3. Graphe : un doublon devient sa fiche canonique, une fiche remplacée la fiche qui la remplace.
4. Texte : BM25F sur la structure que kecore a extraite (titre x3, symptôme et cause x2,
   étapes x1, corps x1), statistiques calculées sur toute la carte, avec les mots du ticket et les
   termes de l'interprétation. Le texte ne fait que classer les fiches que les entités ont gardées.
5. Décision par seuils : une fiche nettement devant -> "fiche" ; plusieurs fiches proches dont
   une seule a son titre repris par le ticket -> cette fiche ; sinon "question" (l'entité qui les
   sépare, sinon le choix entre au plus trois fiches) ; rien d'assez proche -> "abstain". Une fiche n'est montrée directement que si une entité forte l'a désignée
   (numéro de fiche, code d'erreur ou d'événement, mise à jour) ou si le ticket (ou son
   interprétation) reprend au moins deux mots de son titre, nom du document compris, dont un au
   moins est informatif ; sinon elle est proposée avec les suivantes. Sans aucune entité connue, le texte cherche sur toute la carte avec un seuil plus
   strict ; s'il approche sans l'atteindre, la question demande l'application.

Les seuils (``FunnelConfig``) sont des valeurs de départ : ils se calibrent sur des tickets
étiquetés (tranche 4), pas à la main.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from pathlib import PurePosixPath
from typing import Sequence

from kecore.decompose import DecomposedFiche
from kecore.entities import APPS, OPERATING_SYSTEMS, extract_entities
from kecore.text import clean_text

from .graph import KB_NUMBER_RE, KBGraph, build_graph
from .interpret import Interpretation
from .search import SEARCHABLE_STATUSES, tokenize

FUNNEL_VERSION = 2  # 2: min_show (a calibrated floor under which a fiche is offered, not shown)
MAX_TEXT_CHARS = 20_000

# Filter levels, most informative first; (name, entity prefixes). "answered" holds the entities the
# technician chose in answer to a question (all must hold), "cited" the fiches whose number the
# ticket writes. The operating system never filters on its own: tickets name it in passing.
FILTER_LEVELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("answered", ()),
    ("cited", ()),
    ("identifier", ("err", "evt", "kb")),
    ("update", ("update",)),
    ("application", ("app",)),
    ("technical", ("reg", "path", "cmd", "url", "menu", "key")),
)
STRONG_LEVELS = frozenset({"answered", "cited", "identifier", "update"})
DESIGNATING_LEVELS = frozenset({"answered", "cited"})  # they name the fiche: no text needed
QUESTION_KINDS = ("app", "err", "evt", "os")
_WORDING = {
    "app": ("l'application", "concernée", "est-elle"),
    "err": ("le code d'erreur", "affiché", "est-il"),
    "evt": ("l'identifiant d'événement", "affiché", "est-il"),
    "os": ("le système d'exploitation", "concerné", "est-il"),
}

FIELD_WEIGHTS = {"title": 3.0, "symptom": 2.0, "steps": 1.0, "body": 1.0}
TITLE_ROLES = frozenset({"title"})
SYMPTOM_ROLES = frozenset({"symptom", "cause"})
BM25_K1 = 1.2
BM25_B = 0.75
TEXT_SATURATION = 4.0
APP_SUGGESTIONS_FROM = 10
TITLE_TERMS_FOR_DIRECT = 2  # without a strong entity, a fiche is shown only if the ticket repeats 2 title words,
                            # at least one of them informative ("LOCKED ACCOUNT": "account" is in half the fiches)
TITLE_TERM_MAX_SHARE = 0.25  # a title word found in more than this share of the fiches says little
TITLE_TERM_MIN_ALLOWED = 3  # ...but in a small KB a word shared by 3 fiches still tells them apart
# Words a ticket uses to talk, not to describe (the base list of kefind.search covers articles and
# prepositions). "je" is in 2 of the 185 client-s fiches: without this list it looked like a rare,
# telling word and pulled French ticket after French ticket towards the same French guide.
QUERY_STOPWORDS = frozenset(
    "je tu il elle on nous vous ils elles me te lui leur leurs mon ma mes ton ta tes son sa ses notre nos votre vos "
    "moi toi ai as avons avez suis es sommes etes etait fait faire peux peut pouvez dois doit devez veux veut voulez "
    "mais donc car ni si aussi bien tout tous toute toutes rien chose bonjour merci cordialement svp stp salut "
    "madame monsieur depuis encore deja toujours jamais besoin "
    "i me my we our you your he she it its they them their this that these those am was were be been being have has "
    "had do does did can could would should will shall may might must need needs please thanks thank hello hi dear "
    "regards how what why when where which who there here from by at as but if so not no yes all any some also just "
    "still".split()
)
_NAME_WORD_RE = re.compile(r"[^\W\d_]{3,}")


@dataclass
class FunnelConfig:
    min_text: float = 0.25  # fiches kept by application or technical entities only
    min_text_strong: float = 0.10  # kept by a cited fiche number, an error/event code or an update
    min_text_no_entity: float = 0.40  # no known entity: the text searches the whole map
    gap: float = 0.12  # lead of the first fiche over the second needed to show it
    min_show: float = 0.0  # calibrated on labeled tickets (scoreboard): under it a fiche is offered in a
                           # choice instead of shown alone; 0 = off. Never applies to a fiche the ticket
                           # designates itself (cited number, the technician's own answer).
    max_choices: int = 3  # fiches offered at most in a choice question
    max_options: int = 5  # values offered at most in an entity question
    top_k: int = 5  # ranked candidates returned with every decision

    @classmethod
    def from_dict(cls, data: dict | None) -> "FunnelConfig":
        data = data or {}
        names = {f.name: f.type for f in fields(cls)}
        unknown = sorted(set(data) - set(names))
        if unknown:
            raise ValueError(f"réglage(s) inconnu(s) : {', '.join(unknown)}. Attendus : {', '.join(sorted(names))}")
        values = {}
        for name, value in data.items():
            values[name] = int(value) if name in ("max_choices", "max_options", "top_k") else float(value)
        return cls(**values)


def _saturate(value: float) -> float:
    return value / (value + TEXT_SATURATION) if value > 0 else 0.0


def _spans(fiche: DecomposedFiche, roles) -> str:
    return "\n".join(fiche.text[s["start"]:s["end"]] for s in fiche.sections if s.get("role") in roles)


def _stem(fiche: DecomposedFiche) -> str:
    return PurePosixPath((fiche.source or "").replace("\\", "/")).stem


def _descriptive(name: str) -> bool:
    """A document name that says something beyond its number ("KB0032 - Clear the Teams cache")."""
    return bool(_NAME_WORD_RE.findall(KB_NUMBER_RE.sub(" ", name)))


def _fields(fiche: DecomposedFiche, title: str) -> dict[str, str]:
    return {
        "title": f"{title}\n{_spans(fiche, TITLE_ROLES)}",
        "symptom": _spans(fiche, SYMPTOM_ROLES),
        "steps": "\n".join(step.text for step in fiche.steps),
        "body": fiche.text,
    }


class _TextIndex:
    """BM25F over the fields of each fiche; document frequencies and lengths over the whole map."""

    def __init__(self, fiches: Sequence[DecomposedFiche], titles: dict[str, str]):
        self.docs: dict[str, dict[str, tuple[Counter, int]]] = {}
        frequency: Counter = Counter()
        lengths: dict[str, list[int]] = {name: [] for name in FIELD_WEIGHTS}
        for fiche in fiches:
            entry = {}
            seen: set[str] = set()
            for name, text in _fields(fiche, titles[fiche.fiche_id]).items():
                tokens = tokenize(text)
                entry[name] = (Counter(tokens), len(tokens))
                lengths[name].append(len(tokens))
                seen.update(tokens)
            frequency.update(seen)
            self.docs[fiche.fiche_id] = entry
        self.n = len(self.docs)
        self.frequency = frequency
        self.average = {name: (sum(values) / len(values) if values else 0.0) for name, values in lengths.items()}

    def idf(self, term: str) -> float:
        df = self.frequency.get(term, 0)
        return math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))

    def score(self, fiche_id: str, terms: Sequence[str]) -> tuple[float, list[str], list[str]]:
        """BM25F score, the ticket terms found in the fiche, and those found in its title."""
        entry = self.docs[fiche_id]
        total = 0.0
        matched = []
        in_title = []
        for term in terms:
            weighted = 0.0
            for name, weight in FIELD_WEIGHTS.items():
                counts, length = entry[name]
                tf = counts.get(term, 0)
                if tf:
                    weighted += weight * tf / (1 - BM25_B + BM25_B * length / (self.average[name] or 1.0))
            if weighted:
                total += self.idf(term) * weighted / (BM25_K1 + weighted)
                matched.append(term)
                if entry["title"][0].get(term):
                    in_title.append(term)
        return total, matched, in_title


class KBMap:
    """The map of one client's KB, built from one kecore run: never mixed with another client."""

    def __init__(self, client: str, fiches: Sequence[DecomposedFiche], dictionary: dict[str, list[str]] | None = None,
                 graph: KBGraph | None = None, run_id: str | None = None):
        by_id: dict[str, DecomposedFiche] = {}
        for fiche in fiches:
            if fiche.client != client:
                raise ValueError(f"fiche {fiche.fiche_id} appartient au client {fiche.client!r}, pas à {client!r}")
            by_id.setdefault(fiche.fiche_id, fiche)
        self.client = client
        self.run_id = run_id
        self.fiches = by_id
        self.dictionary = {k: list(v) for k, v in (dictionary or {}).items()}
        self.graph = graph if graph is not None else build_graph(list(by_id.values()))
        self.searchable = sorted(i for i, f in by_id.items() if f.status in SEARCHABLE_STATUSES)
        self.searchable_set = frozenset(self.searchable)
        # A title several fiches share is a template heading ("General Information" heads 198 of
        # the 242 client-s fiches), not a title: the document name says more.
        shared = Counter(f.title for f in by_id.values())
        self.titles: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        for fiche_id, fiche in by_id.items():
            own_title = fiche.title if fiche.title and shared[fiche.title] == 1 else ""
            stem = _stem(fiche)
            self.titles[fiche_id] = "\n".join(dict.fromkeys(t for t in (stem, fiche_id, own_title) if t))
            if stem and _descriptive(stem):
                self.labels[fiche_id] = stem
            elif own_title and own_title not in fiche_id:
                self.labels[fiche_id] = f"{fiche_id} – {own_title}"
            else:
                self.labels[fiche_id] = fiche_id
        # Entities of a fiche: those kecore read in its title and text, plus those of its document
        # name, read by the same code with the same dictionary.
        self.fiche_entities: dict[str, frozenset[str]] = {}
        index: dict[str, list[str]] = {}
        for fiche_id in self.searchable:
            own = {entry["canonical"] for entry in by_id[fiche_id].entities}
            named = {e.canonical for e in extract_entities(self.titles[fiche_id], self.dictionary or None)}
            self.fiche_entities[fiche_id] = frozenset(own | named)
            for canonical in self.fiche_entities[fiche_id]:
                index.setdefault(canonical, []).append(fiche_id)
        self.entity_index = {canonical: sorted(ids) for canonical, ids in sorted(index.items())}
        # Only canonical fiches are ranked (the graph maps a duplicate to its canonical fiche), so
        # only they count in the text statistics: twins would make their own words look common.
        self.ranked = [i for i in self.searchable if self.graph.canonical(i) == i]
        self.text_index = _TextIndex([by_id[i] for i in self.ranked], self.titles)

    def entities_of(self, fiche_id: str) -> frozenset[str]:
        return self.fiche_entities.get(fiche_id, frozenset())

    def label(self, fiche_id: str) -> str:
        """How a fiche is named to a technician: its document name, else its number and title."""
        return self.labels.get(fiche_id, fiche_id)

    def stats(self) -> dict:
        kinds = Counter(canonical.split(":", 1)[0] for canonical in self.entity_index)
        return {
            "client": self.client,
            "run_id": self.run_id,
            "fiches": len(self.fiches),
            "searchable": len(self.searchable),
            "ranked": len(self.ranked),
            "entities": len(self.entity_index),
            "entities_by_kind": dict(sorted(kinds.items())),
            "fiches_without_entity": sum(1 for i in self.searchable if not self.fiche_entities[i]),
            "dictionary": len(self.dictionary),
            "graph": self.graph.stats(),
        }


@dataclass
class Finding:
    kind: str  # "fiche" | "question" | "abstain"
    reason: str
    fiche_id: str | None = None
    fiches: list[str] = field(default_factory=list)  # ranked candidates, the shown fiche first
    score: float | None = None
    question: str | None = None
    asks: str | None = None  # "app" | "err" | "evt" | "os" (an entity) | "fiche" (a choice between fiches)
    options: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    version: int = FUNNEL_VERSION
    designated: bool = False  # shown because the ticket named it (cited number, technician's answer)

    def to_dict(self) -> dict:
        return asdict(self)


def _label(kbmap: KBMap, canonical: str) -> str:
    prefix, _, value = canonical.partition(":")
    if prefix == "app":
        if value in APPS:
            return APPS[value][0]
        forms = kbmap.dictionary.get(value) or []
        written = [f for f in forms if not f.isupper()]  # "ClearPass" reads better than "CLEARPASS"
        return (written or forms or [value.replace("-", " ")])[0]
    if prefix == "os" and value in OPERATING_SYSTEMS:
        return OPERATING_SYSTEMS[value][0]
    return value


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " ou " + items[-1]


def _discriminator(kbmap: KBMap, close: list[str], exclude: set[str], config: FunnelConfig):
    """The first entity kind that splits the close fiches, every one of them reachable by an answer."""
    for prefix in QUESTION_KINDS:
        values = {f: sorted(c for c in kbmap.entities_of(f) if c.startswith(prefix + ":") and c not in exclude)
                  for f in close}
        if not all(values.values()):
            continue
        options: dict[str, list[str]] = {}
        for fiche_id in close:
            for value in values[fiche_id]:
                options.setdefault(value, []).append(fiche_id)
        options = {value: ids for value, ids in options.items() if len(ids) < len(close)}
        reached = {fiche_id for ids in options.values() for fiche_id in ids}
        if 2 <= len(options) <= config.max_options and reached == set(close):
            return prefix, sorted(options.items())
    return None


def _entity_question(kbmap: KBMap, prefix: str, options, reason: str, score: float | None) -> Finding:
    label, concerned, verb = _WORDING[prefix]
    labels = [_label(kbmap, value) for value, _ in options]
    return Finding("question", reason, score=score, asks=prefix,
                   question=f"Pour trouver la bonne fiche : {label} {concerned} {verb} {_join(labels)} ?",
                   options=[{"value": value, "label": text, "fiches": ids} for (value, ids), text in zip(options, labels)])


def _choice(kbmap: KBMap, fiche_ids: list[str], reason: str, score: float | None) -> Finding:
    titles = [f"« {kbmap.label(f)} »" for f in fiche_ids]
    if len(fiche_ids) == 1:
        question = f"Cette fiche correspond-elle au problème : {titles[0]} ?"
    else:
        question = f"Laquelle de ces fiches correspond au problème : {_join(titles)} ?"
    return Finding("question", reason, score=score, asks="fiche", question=question,
                   options=[{"fiche_id": f, "label": kbmap.label(f)} for f in fiche_ids])


def _show(kbmap: KBMap, fiche_id: str, reason: str, score: float, designated: bool, ranked: list[str],
          config: FunnelConfig, trace: list[dict]) -> Finding:
    """THE fiche -- unless the calibrated floor (``min_show``) says a score this low is not enough to
    show it alone: it is then offered first in a choice. A designated fiche is always shown."""
    if not designated and config.min_show > 0 and score < config.min_show:
        choices = [fiche_id] + [f for f in ranked if f != fiche_id][: config.max_choices - 1]
        return _finish(_choice(kbmap, choices, f"{reason}_below_min_show", score), ranked, config, trace)
    return _finish(Finding("fiche", reason, fiche_id=fiche_id, score=score, designated=designated),
                   ranked, config, trace)


def _finish(finding: Finding, ranked: list[str], config: FunnelConfig, trace: list[dict]) -> Finding:
    ordered = list(ranked[: config.top_k])
    if finding.fiche_id:
        ordered = [finding.fiche_id] + [f for f in ordered if f != finding.fiche_id]
    finding.fiches = ordered
    trace.append({"step": "decide", "kind": finding.kind, "reason": finding.reason, "fiche_id": finding.fiche_id,
                  "asks": finding.asks})
    finding.trace = trace
    return finding


def find(kbmap: KBMap, text: str, answers: Sequence[str] = (), config: FunnelConfig | None = None,
         interpretation: Interpretation | None = None) -> Finding:
    """The decision for one ticket. ``answers``: entities the technician chose in answer to a question;
    ``interpretation``: the ticket's search terms from ``kefind.interpret`` (text only, never a filter)."""
    config = config or FunnelConfig()
    trace: list[dict] = []
    cleaned = clean_text(text or "")[:MAX_TEXT_CHARS]
    if interpretation is not None:
        trace.append({"step": "interpret", "terms": list(interpretation.terms), "dropped": list(interpretation.dropped),
                      "application": interpretation.application, "model": interpretation.model,
                      "cached": interpretation.cached, "error": interpretation.error,
                      "tokens": {"in": interpretation.usage.input_tokens, "out": interpretation.usage.output_tokens}})
    if not kbmap.searchable:
        return _finish(Finding("abstain", "empty_map"), [], config, trace)

    # 1. entities of the ticket, read with the dictionary the fiches were read with
    ticket = list(dict.fromkeys(e.canonical for e in extract_entities(cleaned, kbmap.dictionary or None)))
    known = [c for c in ticket if c in kbmap.entity_index]
    given = list(dict.fromkeys(a for a in answers if isinstance(a, str)))
    accepted = [a for a in given if a in kbmap.entity_index and a not in known]
    numbers = list(dict.fromkeys(int(m.group(1)) for m in KB_NUMBER_RE.finditer(cleaned)))
    resolved = {str(n): [f for f in kbmap.graph.resolve_number(n) if f in kbmap.searchable_set] for n in numbers}
    cited = list(dict.fromkeys(f for n in numbers for f in resolved[str(n)]))
    trace.append({
        "step": "entities", "ticket": ticket, "known": known,
        "unknown_ignored": [c for c in ticket if c not in kbmap.entity_index],
        "answers": accepted, "answers_ignored": [a for a in given if a not in kbmap.entity_index],
        "fiche_numbers": resolved, "dictionary": len(kbmap.dictionary),
    })

    # 2. the entity filter, level by level; the least informative level kept is dropped first
    levels: list[tuple[str, list[str], set[str]]] = []
    for name, prefixes in FILTER_LEVELS:
        if name == "answered":  # the technician's own choices: every one of them must hold
            values = accepted
            reached = set.intersection(*(set(kbmap.entity_index[v]) for v in values)) if values else set()
        elif name == "cited":
            values, reached = cited, set(cited)
        else:
            values = [c for c in known if c.split(":", 1)[0] in prefixes]
            reached = set().union(*(kbmap.entity_index[v] for v in values)) if values else set()
        if values:
            levels.append((name, list(values), reached))
    active = list(levels)
    attempts = []
    pool: set[str] = set()
    while active:
        pool = set.intersection(*(reached for _, _, reached in active))
        attempts.append({"levels": [name for name, _, _ in active], "candidates": len(pool)})
        if pool:
            break
        active.pop()
    if levels:
        trace.append({"step": "filter", "levels": {name: values for name, values, _ in levels}, "attempts": attempts,
                      "kept": [name for name, _, _ in active]})
        mode = "entities"
        kept = {name for name, _, _ in active}
        strong = bool(kept & STRONG_LEVELS)
        designated = bool(kept & DESIGNATING_LEVELS)
        threshold = 0.0 if designated else config.min_text_strong if strong else config.min_text
        candidates = sorted(pool)
    else:
        mode = "text_only"
        strong = False
        designated = False
        threshold = config.min_text_no_entity
        candidates = list(kbmap.ranked)

    # 3. the graph: a duplicate becomes its canonical fiche, a replaced fiche the fiche replacing it
    pruned, changes = kbmap.graph.prune(candidates)
    pruned = [f for f in pruned if f in kbmap.text_index.docs]
    if changes:
        trace.append({"step": "graph", "changes": changes})

    # 4. the text ranks what the entities kept: the ticket's words and the interpretation's terms
    words = set(tokenize(cleaned))
    if interpretation is not None:
        words |= set(tokenize(" ".join(interpretation.terms)))
    terms = sorted(words - QUERY_STOPWORDS)
    scored = []
    for fiche_id in pruned:
        raw, matched, in_title = kbmap.text_index.score(fiche_id, terms)
        scored.append((round(_saturate(raw), 6), fiche_id, matched, in_title))
    scored.sort(key=lambda item: (-item[0], item[1]))
    ranked = [item[1] for item in scored]
    scores = {item[1]: item[0] for item in scored}
    frequent = max(TITLE_TERM_MIN_ALLOWED, TITLE_TERM_MAX_SHARE * kbmap.text_index.n)
    title_terms = {item[1]: list(item[3]) for item in scored}
    informative = {item[1]: [t for t in item[3] if kbmap.text_index.frequency.get(t, 0) <= frequent] for item in scored}
    trace.append({"step": "rank", "mode": mode, "candidates": len(ranked), "threshold": threshold,
                  "top": [{"fiche_id": f, "text": s, "terms": m[:12], "title_terms": title_terms[f]}
                          for s, f, m, _ in scored[: config.top_k]]})

    # 5. the decision
    if not ranked:
        return _finish(Finding("abstain", "no_candidate"), ranked, config, trace)
    top = ranked[0]
    best = scores[top]
    exclude = set(known) | set(accepted)
    def named(fiche_id: str) -> bool:
        return len(title_terms[fiche_id]) >= TITLE_TERMS_FOR_DIRECT and bool(informative[fiche_id])

    if best >= threshold:
        second = scores[ranked[1]] if len(ranked) > 1 else None
        lead = second is None or round(best - second, 6) >= config.gap
        if lead and (strong or named(top)):
            reason = f"{mode}_single" if second is None else f"{mode}_clear_lead"
            return _show(kbmap, top, reason, best, designated, ranked, config, trace)
        if lead:
            # nothing as specific as an error code, and the ticket does not repeat the fiche's
            # title: the fiche is proposed with the next ones, never shown as THE fiche
            return _finish(_choice(kbmap, ranked[: config.max_choices], f"{mode}_no_title_match", best),
                           ranked, config, trace)
        close = [f for f in ranked if round(best - scores[f], 6) < config.gap]
        # close by text, but the ticket names the title of only one of them: that one
        titled = [f for f in close if named(f)]
        if len(titled) == 1:
            return _show(kbmap, titled[0], f"{mode}_close_title_match", scores[titled[0]], designated, ranked,
                         config, trace)
        # an entity question only when the ticket's own entities brought these fiches together
        found = _discriminator(kbmap, close, exclude, config) if mode == "entities" else None
        if found:
            return _finish(_entity_question(kbmap, found[0], found[1], f"{mode}_close_entity", best), ranked, config, trace)
        return _finish(_choice(kbmap, close[: config.max_choices], f"{mode}_close_choice", best), ranked, config, trace)
    if mode == "entities":
        if len(ranked) <= config.max_choices:
            return _finish(_choice(kbmap, ranked, "entities_weak_text_few", best), ranked, config, trace)
        return _finish(Finding("abstain", "entities_weak_text", score=best), ranked, config, trace)
    if best >= config.min_text:
        apps = []
        for fiche_id in ranked[:APP_SUGGESTIONS_FROM]:
            for value in sorted(c for c in kbmap.entities_of(fiche_id) if c.startswith("app:")):
                if value not in apps:
                    apps.append(value)
        if apps:
            options = [(value, [f for f in ranked[:APP_SUGGESTIONS_FROM] if value in kbmap.entities_of(f)])
                       for value in apps[: config.max_options]]
            finding = _entity_question(kbmap, "app", options, "text_only_ask_application", best)
            finding.question = "Quelle application est concernée ? Par exemple : " + _join([o["label"] for o in finding.options]) + "."
            return _finish(finding, ranked, config, trace)
        return _finish(Finding("abstain", "text_only_weak", score=best), ranked, config, trace)
    return _finish(Finding("abstain", "text_only_nothing_close", score=best), ranked, config, trace)


def fiche_view(kbmap: KBMap, fiche_id: str) -> dict:
    """What a technician is guided with: the fiche's verified steps (its own text) and its neighbours."""
    fiche = kbmap.fiches[fiche_id]
    node = kbmap.graph.nodes.get(fiche_id)

    def linked(ids) -> list[dict]:
        return [{"fiche_id": i, "label": kbmap.label(i)} for i in ids]

    group = next((g for g in kbmap.graph.groups if fiche_id in g), [])
    return {
        "fiche_id": fiche.fiche_id,
        "label": kbmap.label(fiche_id),
        "title": fiche.title,
        "status": fiche.status,
        "source": fiche.source,
        "steps": [{"n": s.n, "text": s.text, "role": s.role, "kind": s.kind, "condition": s.condition,
                   "after_failure": s.after_failure, "on_failure": s.on_failure, "goto": s.goto,
                   "instruction": s.instruction} for s in fiche.steps],
        "prerequisites": linked(node.prerequisites if node else []),
        "references": linked(node.references if node else []),
        "duplicates": [i for i in group if i != fiche_id],
    }


__all__ = ["FILTER_LEVELS", "FUNNEL_VERSION", "Finding", "FunnelConfig", "KBMap", "fiche_view", "find"]
