"""Scoring rules.

Every rate carries a 95% Wilson interval: on 100 tickets the honest error bar
is several points wide, and the report says so instead of hiding it.

Definitions (on the first run, the one a technician would see):

* exact fiche @1: the fiche shown is one of the expected fiches, over tickets
  that have an expected fiche;
* wrong fiche shown: a fiche is shown and it is not an expected one, over all
  tickets (a fiche shown for a ticket that no fiche covers is wrong);
* no fiche shown: the engine abstained or asked a question, over all tickets;
* correct "no fiche": no fiche shown, over tickets that no fiche covers;
* recall@5: an expected fiche is among the first 5 candidates, whatever the
  decision (the ceiling for a better decision step);
* stability: same kind and same fiche shown in every run, over all tickets.

For a routing engine (records carrying a ``route``, see ``routing_gate``):

* right fiche first: the first fiche a technician meets, shown or offered by a
  question, is an expected one (an abstention offers nothing);
* wrong exact fiche: a fiche shown as exact at L0-L2 (validated by a person)
  that is not an expected one, over every run and every question.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

Z95 = 1.959963984540054
RECALL_K = 5


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def tickets_needed(max_rate: float, z: float = Z95) -> int | None:
    """Smallest n for which zero errors out of n puts the rate under max_rate.

    With no error the upper Wilson bound is z^2 / (n + z^2), hence the closed form.
    """
    if not 0 < max_rate < 1:
        return None
    n = max(1, math.ceil(z * z * (1 - max_rate) / max_rate) - 1)
    while wilson(0, n, z)[1] > max_rate:
        n += 1
    return n


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    data = sorted(values)
    rank = (pct / 100) * (len(data) - 1)
    low, high = math.floor(rank), math.ceil(rank)
    return data[low] + (data[high] - data[low]) * (rank - low)


@dataclass(frozen=True)
class Rate:
    k: int
    n: int

    @property
    def value(self) -> float | None:
        return self.k / self.n if self.n else None

    @property
    def low(self) -> float | None:
        return wilson(self.k, self.n)[0] if self.n else None

    @property
    def high(self) -> float | None:
        return wilson(self.k, self.n)[1] if self.n else None

    def to_dict(self) -> dict:
        return {"k": self.k, "n": self.n, "value": self.value, "ci95": [self.low, self.high] if self.n else None}


def ticket_key(record: dict) -> str:
    return f"{record['client']}/{record['ticket_id']}"


def shown(record: dict) -> str | None:
    if record.get("kind") == "fiche" and record.get("fiches"):
        return record["fiches"][0]
    return None


def is_exact(record: dict) -> bool:
    fiche = shown(record)
    return fiche is not None and fiche in record["expected"]


def by_run(records: list[dict]) -> dict[int, dict[str, dict]]:
    runs: dict[int, dict[str, dict]] = defaultdict(dict)
    for record in records:
        runs[int(record["run"])][ticket_key(record)] = record
    return dict(sorted(runs.items()))


def canonical(records: list[dict]) -> list[dict]:
    runs = by_run(records)
    return list(next(iter(runs.values())).values()) if runs else []


@dataclass
class EngineSummary:
    engine: str
    runs: int
    tickets: int
    with_fiche: int
    without_fiche: int
    exact: Rate
    wrong_shown: Rate
    no_fiche: Rate
    questions: Rate
    correct_none: Rate
    recall_at_k: Rate
    stability: Rate | None
    exact_by_run: list[float | None]
    latency_p50: float | None
    latency_p95: float | None
    cost_per_ticket: float | None
    currency: str
    errors: int
    error_samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {}
        for name, value in self.__dict__.items():
            out[name] = value.to_dict() if isinstance(value, Rate) else value
        return out


def summarize(engine: str, records: list[dict], prices=None) -> EngineSummary:
    runs = by_run(records)
    if not runs:
        raise ValueError(f"no results for engine {engine}")
    first = next(iter(runs.values()))
    canon = list(first.values())
    with_fiche = [r for r in canon if r["expected"]]
    without_fiche = [r for r in canon if not r["expected"]]
    n = len(canon)

    exact = sum(1 for r in with_fiche if is_exact(r))
    wrong = sum(1 for r in canon if shown(r) is not None and shown(r) not in r["expected"])
    no_fiche = sum(1 for r in canon if shown(r) is None)
    questions = sum(1 for r in canon if r["kind"] == "question")
    correct_none = sum(1 for r in without_fiche if shown(r) is None)
    recall = sum(1 for r in with_fiche if set(r["fiches"][:RECALL_K]) & set(r["expected"]))

    stability = None
    if len(runs) > 1:
        stable = 0
        for key, record in first.items():
            signature = (record["kind"], shown(record))
            if all(key in run and (run[key]["kind"], shown(run[key])) == signature for run in runs.values()):
                stable += 1
        stability = Rate(stable, n)

    exact_by_run: list[float | None] = []
    for run in runs.values():
        answerable = [r for r in run.values() if r["expected"]]
        exact_by_run.append(sum(1 for r in answerable if is_exact(r)) / len(answerable) if answerable else None)

    latencies = [float(r["latency_s"]) for r in records if r.get("latency_s") is not None and not r.get("error")]
    failed = [r for r in records if r.get("error")]
    cost = None
    if prices is not None and records:
        cost = sum(prices.cost(r.get("usage") or {}) for r in records) / len(records)
    samples = []
    for record in failed:
        if record["error"] not in samples:
            samples.append(record["error"])
        if len(samples) == 5:
            break

    return EngineSummary(
        engine=engine,
        runs=len(runs),
        tickets=n,
        with_fiche=len(with_fiche),
        without_fiche=len(without_fiche),
        exact=Rate(exact, len(with_fiche)),
        wrong_shown=Rate(wrong, n),
        no_fiche=Rate(no_fiche, n),
        questions=Rate(questions, n),
        correct_none=Rate(correct_none, len(without_fiche)),
        recall_at_k=Rate(recall, len(with_fiche)),
        stability=stability,
        exact_by_run=exact_by_run,
        latency_p50=percentile(latencies, 50),
        latency_p95=percentile(latencies, 95),
        cost_per_ticket=cost,
        currency=prices.currency if prices is not None else "",
        errors=len(failed),
        error_samples=samples,
    )


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar test on the discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


@dataclass
class PairComparison:
    first: str
    second: str
    tickets: int
    first_only: int
    second_only: int
    both: int
    neither: int
    p_value: float

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def compare(first: str, first_records: list[dict], second: str, second_records: list[dict]) -> PairComparison:
    """Paired comparison of exact fiche @1 on the tickets both engines saw."""
    a = {ticket_key(r): r for r in canonical(first_records) if r["expected"]}
    b = {ticket_key(r): r for r in canonical(second_records) if r["expected"]}
    keys = sorted(set(a) & set(b))
    first_only = sum(1 for k in keys if is_exact(a[k]) and not is_exact(b[k]))
    second_only = sum(1 for k in keys if is_exact(b[k]) and not is_exact(a[k]))
    both = sum(1 for k in keys if is_exact(a[k]) and is_exact(b[k]))
    return PairComparison(
        first=first,
        second=second,
        tickets=len(keys),
        first_only=first_only,
        second_only=second_only,
        both=both,
        neither=len(keys) - first_only - second_only - both,
        p_value=mcnemar_exact(first_only, second_only),
    )


@dataclass
class SweepRow:
    threshold: float | None
    exact: Rate
    wrong_shown: Rate
    no_fiche: Rate

    def to_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "exact": self.exact.to_dict(),
            "wrong_shown": self.wrong_shown.to_dict(),
            "no_fiche": self.no_fiche.to_dict(),
        }


@dataclass
class Calibration:
    max_wrong: float
    rows: list[SweepRow]
    recommended: SweepRow | None

    def to_dict(self) -> dict:
        return {
            "max_wrong": self.max_wrong,
            "recommended": self.recommended.to_dict() if self.recommended else None,
            "rows": [row.to_dict() for row in self.rows],
        }


def calibrate(records: list[dict], max_wrong: float) -> Calibration | None:
    """What if the engine showed its fiche only when its score is at least t?

    The recommended threshold is the lowest one whose wrong-fiche rate stays
    under the ceiling even at the top of its 95% interval: on a small sample
    this is deliberately strict. Pick it on one set of tickets and confirm it
    on another before trusting it.
    """
    canon = canonical(records)
    scored = [r for r in canon if shown(r) is not None and r.get("score") is not None]
    if not scored:
        return None
    n = len(canon)
    answerable = sum(1 for r in canon if r["expected"])
    unscored = [r for r in canon if shown(r) is not None and r.get("score") is None]
    rows = []
    for threshold in [None] + sorted({float(r["score"]) for r in scored}):
        kept = unscored + [r for r in scored if threshold is None or float(r["score"]) >= threshold]
        exact = sum(1 for r in kept if is_exact(r))
        rows.append(
            SweepRow(
                threshold=threshold,
                exact=Rate(exact, answerable),
                wrong_shown=Rate(len(kept) - exact, n),
                no_fiche=Rate(n - len(kept), n),
            )
        )
    recommended = next((row for row in rows if row.wrong_shown.high is not None and row.wrong_shown.high <= max_wrong), None)
    return Calibration(max_wrong=max_wrong, rows=rows, recommended=recommended)


# Routing (kefind.route): the decisions by level, and the gate a set of routing tables must pass before
# a person may activate them. Spec (docs/v10-deterministic-engine.md): "deploy only if a wrong fiche
# shown as exact (L0-L2) is 0 and right-fiche-first is at least today's engine's", on the same questions.

EXACT_LEVELS = ("L0", "L1", "L2")
MIN_GATE_QUESTIONS = 30  # = kefind.routing.MIN_GATE_QUESTIONS (scoreboard does not import kefind)
GATE_STATUSES = ("invalid", "failed", "insufficient", "passed")
_SAMPLE_IDS = 5


def first_fiche(record: dict) -> str | None:
    """The fiche a technician meets first: the one shown, or the first one a question offers. Routing
    turns a fiche the base engine would show into an offer (L3), so right-first, not exact @1, is the
    measure both engines can be held to. An abstention offers nothing, whatever it ranked."""
    if record.get("kind") in ("fiche", "question") and record.get("fiches"):
        return record["fiches"][0]
    return None


def right_first(record: dict) -> bool:
    """The first fiche met is an expected one; never true on a question no fiche covers."""
    return bool(record["expected"]) and first_fiche(record) in record["expected"]


def route_level(record: dict) -> str | None:
    """None for a record no router decided (any engine before routing, and every record written then)."""
    return (record.get("route") or {}).get("level")


def is_routed_exact(record: dict) -> bool:
    """A fiche shown as exact because a person validated it into the tables (L0-L2)."""
    return route_level(record) in EXACT_LEVELS and record.get("kind") == "fiche"


def is_wrong_exact(record: dict) -> bool:
    """A routed exact fiche that is not an expected one; showing any fiche when none is expected is wrong."""
    return is_routed_exact(record) and shown(record) not in record["expected"]


def _level_counts(records: list[dict]) -> dict:
    return {
        "n": len(records),
        "with_fiche": sum(1 for r in records if r["expected"]),
        "shown": sum(1 for r in records if shown(r) is not None),
        "exact": sum(1 for r in records if is_exact(r)),
        "wrong_exact": sum(1 for r in records if is_wrong_exact(r)),
        "right_first": sum(1 for r in records if right_first(r)),
        "questions": sum(1 for r in records if r["kind"] == "question"),
        "abstain": sum(1 for r in records if r["kind"] == "abstain"),
    }


def by_route(records: list[dict]) -> dict[str, dict]:
    """Counts per decision level on the first run: {"L0".."L4" | "none": {n, with_fiche, shown, exact,
    wrong_exact, right_first, questions, abstain}}, levels in order, "none" (not routed) last; a level
    no question reached is absent."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for record in canonical(records):
        groups[route_level(record) or "none"].append(record)
    return {level: _level_counts(groups[level]) for level in sorted(groups, key=lambda lv: (lv == "none", lv))}


def _sample(ids: list[str]) -> str:
    more = len(ids) - _SAMPLE_IDS
    return ", ".join(ids[:_SAMPLE_IDS]) + (f" and {more} more" if more > 0 else "")


def _later_runs_problem(name: str, records: list[dict], keys: set[str]) -> list[str]:
    """Runs after the first that answered other questions (an interrupted repeat): the first run is the
    one every number reads, but a wrong exact fiche in a later run still counts, so every run must be whole."""
    uneven = [str(run) for run, by_key in list(by_run(records).items())[1:] if set(by_key) != keys]
    if not uneven:
        return []
    return [f"the {name} engine's run(s) {', '.join(uneven)} did not answer the same questions as the base "
            "engine's first run: the measure is incomplete"]


def routing_gate(base: list[dict], routed: list[dict], exclude_ids=(), *, failures: int = 0, degraded: int = 0,
                 min_questions: int = MIN_GATE_QUESTIONS) -> dict:
    """May these routing tables be activated? ``base``: today's engine's records; ``routed``: the routing
    engine's, on the same frozen questions; ``exclude_ids``: questions a reviewer saw while writing the
    tables (``kefind.review.seen_reference_ids``), as ticket ids or client/ticket_id keys.

    Status, the first that applies:
    - "invalid": the measure cannot be trusted either way (the engines did not answer the same
      questions or against the same labels, a decision failed, a model or embedding call failed
      transiently, a decision was degraded): run it again;
    - "failed": a fiche shown as exact at L0-L2 is wrong, on ANY routed question, excluded ones
      included (a seen question is exactly where an alias written from it can lose to another fiche's);
      or the right fiche comes first less often than without routing on the compared questions;
    - "insufficient": fewer compared questions than ``min_questions`` (never fewer than 1), or no
      question decided at L0-L2 at all: nothing proves the tables right, though nothing proves them
      wrong (a person may still activate them, with a recorded reason);
    - "passed".
    The comparison (right-first and McNemar) is on the base engine's questions that expect a fiche,
    minus the excluded ones: a reviewer's phrasings would otherwise score the tables written from them.
    Wrong exact fiches are counted over every run; ids are ticket ids, sorted."""
    base_canon = {ticket_key(r): r for r in canonical(base)}
    route_canon = {ticket_key(r): r for r in canonical(routed)}
    keys = sorted(base_canon)
    invalid: list[str] = []
    failed: list[str] = []
    insufficient: list[str] = []

    missing = sorted(set(base_canon) - set(route_canon))
    extra = sorted(set(route_canon) - set(base_canon))
    if missing or extra:
        invalid.append(f"the routed engine did not answer the same questions as the base engine ({len(missing)} "
                       f"missing, {len(extra)} extra, e.g. {_sample(missing + extra)})")
    invalid += _later_runs_problem("base", base, set(base_canon))
    invalid += _later_runs_problem("routed", routed, set(base_canon))
    relabeled = sorted(base_canon[k]["ticket_id"] for k in keys
                       if k in route_canon and sorted(base_canon[k]["expected"]) != sorted(route_canon[k]["expected"]))
    if relabeled:
        invalid.append(f"{len(relabeled)} question(s) carry different expected fiches in the two results "
                       f"(e.g. {_sample(relabeled)}): the engines were not measured on the same labels")
    for name, records in (("base", base), ("routed", routed)):
        errors = sorted({r["ticket_id"] for r in records if r.get("error")})
        if errors:
            invalid.append(f"{len(errors)} {name} decision(s) failed (e.g. {_sample(errors)}): a failed decision "
                           "measures nothing")
    if int(failures or 0) > 0:
        invalid.append(f"{int(failures)} transient model or embedding failure(s) (throttling, server, network): "
                       "run the measure again")
    marked = sorted({r["ticket_id"] for r in list(base) + list(routed) if r.get("degraded")})
    if int(degraded or 0) > 0:
        invalid.append(f"{int(degraded)} degraded decision(s): they were not made the way /find makes them; "
                       "run the measure again")
    elif marked:
        invalid.append(f"{len(marked)} question(s) carry a degraded decision (e.g. {_sample(marked)}): they were "
                       "not made the way /find makes them; run the measure again")

    exact_keys = {ticket_key(r) for r in routed if is_routed_exact(r)}
    wrong = {ticket_key(r): r["ticket_id"] for r in routed if is_wrong_exact(r)}
    wrong_ids = sorted(set(wrong.values()))
    if wrong:
        failed.append(f"{len(wrong)} fiche(s) shown as exact at L0-L2 are wrong ({_sample(wrong_ids)}): "
                      "fix or reject the decisions that route them")

    # one id passed as a bare string would otherwise exclude its characters, and nothing it names
    excluded_ids = {exclude_ids} if isinstance(exclude_ids, str) else {str(x) for x in exclude_ids or ()}

    def is_excluded(record: dict) -> bool:
        return record["ticket_id"] in excluded_ids or ticket_key(record) in excluded_ids

    excluded = sorted({base_canon[k]["ticket_id"] for k in keys if is_excluded(base_canon[k])})
    compared = [k for k in keys if k in route_canon and base_canon[k]["expected"] and not is_excluded(base_canon[k])]
    base_k = sum(1 for k in compared if right_first(base_canon[k]))
    route_k = sum(1 for k in compared if right_first(route_canon[k]))
    base_only = sum(1 for k in compared if right_first(base_canon[k]) and not right_first(route_canon[k]))
    route_only = sum(1 for k in compared if right_first(route_canon[k]) and not right_first(base_canon[k]))
    if route_k < base_k:
        failed.append(f"the right fiche comes first less often with routing ({route_k} of {len(compared)}) than "
                      f"without ({base_k} of {len(compared)})")

    floor = max(1, int(min_questions))
    if len(compared) < floor:
        insufficient.append(f"only {len(compared)} question(s) with an expected fiche can be compared "
                            f"({len(excluded)} excluded as seen by a reviewer); at least {floor} are needed")
    # the evidence must come from questions nobody wrote the tables from: a seen reference decided at L1 by
    # the alias written from it proves nothing about the tables (it still counts for wrong exact fiches)
    unseen_exact = {ticket_key(r) for r in routed if is_routed_exact(r) and not is_excluded(r)}
    if not exact_keys:
        insufficient.append("no question was decided at L0-L2: the measure says nothing about the validated tables")
    elif not unseen_exact:
        insufficient.append("every question decided at L0-L2 was seen by a reviewer while writing the tables: "
                            "the measure says nothing about questions the tables were not written from")

    status = "invalid" if invalid else "failed" if failed else "insufficient" if insufficient else "passed"
    return {
        "status": status,
        "passed": status == "passed",
        "tickets": len(keys),
        "compared": len(compared),
        "excluded": excluded,
        "wrong_exact": {"k": len(wrong), "n": len(exact_keys), "ids": wrong_ids},
        "routed_exact": len(exact_keys),
        "routed_exact_unseen": len(unseen_exact),
        "right_first": {"base": Rate(base_k, len(compared)).to_dict(), "route": Rate(route_k, len(compared)).to_dict()},
        "mcnemar": {"base_only": base_only, "route_only": route_only, "p_value": mcnemar_exact(base_only, route_only)},
        "by_level": by_route(routed),
        "reasons": invalid + failed + insufficient,
    }
