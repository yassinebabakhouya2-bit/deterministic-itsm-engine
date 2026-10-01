"""Markdown report and JSON summary from one or more results files."""

from __future__ import annotations

import datetime as dt
from itertools import combinations

from .metrics import (
    Calibration,
    EngineSummary,
    Rate,
    calibrate,
    canonical,
    compare,
    is_exact,
    shown,
    summarize,
    ticket_key,
    tickets_needed,
)

MAX_SWEEP_ROWS = 9


def pct(value: float | None, digits: int = 1) -> str:
    return "–" if value is None else f"{value * 100:.{digits}f}%"


def fmt_rate(rate: Rate | None) -> str:
    if rate is None or not rate.n:
        return "–"
    return f"{pct(rate.value)} ({pct(rate.low)}–{pct(rate.high)})"


def fmt_seconds(value: float | None) -> str:
    return "–" if value is None else f"{value:.2f} s"


def fmt_score(value: float | None) -> str:
    return "(always)" if value is None else f"{value:.4g}"


def cell(text) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def short(text: str, limit: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _warnings(results: dict[str, list[dict]], manifests: dict[str, dict]) -> list[str]:
    warnings: list[str] = []
    key_sets = {name: {ticket_key(r) for r in canonical(records)} for name, records in results.items()}
    if len({frozenset(keys) for keys in key_sets.values()}) > 1:
        sizes = ", ".join(f"{name}: {len(keys)}" for name, keys in key_sets.items())
        warnings.append(f"The engines did not run on the same tickets ({sizes}); the head-to-head uses the common ones.")
    hashes = {m.get("dataset_sha256") for m in manifests.values() if m.get("dataset_sha256")}
    if len(hashes) > 1:
        files = sorted({str(m.get("dataset")) for m in manifests.values() if m.get("dataset")})
        warnings.append(
            f"The results come from {len(hashes)} different ticket files or versions ({', '.join(files)}): "
            "compare engines on the same file."
        )
    produced: set[str] = set()
    expected: set[str] = set()
    for records in results.values():
        for record in records:
            produced.update(record["fiches"])
            expected.update(record["expected"])
    never = sorted(expected - produced)
    if never and expected:
        share = len(never) / len(expected)
        sample = ", ".join(never[:10])
        warnings.append(
            f"{len(never)} of {len(expected)} expected fiche ids ({pct(share, 0)}) never came out of any engine "
            f"(e.g. {sample}). If that share is high, labels and engines do not name fiches the same way."
        )
    return warnings


def _sweep_rows(calibration: Calibration) -> list:
    rows = calibration.rows
    if len(rows) <= MAX_SWEEP_ROWS:
        return rows
    step = (len(rows) - 1) / (MAX_SWEEP_ROWS - 2)
    picked = {0, len(rows) - 1}
    picked.update(round(i * step) for i in range(1, MAX_SWEEP_ROWS - 2))
    if calibration.recommended is not None:
        picked.add(rows.index(calibration.recommended))
    return [rows[i] for i in sorted(picked)]


def build_report(results: dict[str, list[dict]], prices, max_wrong: float, manifests: dict[str, dict] | None = None,
                 misses_per_engine: int = 30, tickets_text: dict[str, str] | None = None) -> tuple[str, dict]:
    manifests = manifests or {}
    tickets_text = tickets_text or {}
    names = list(results)
    summaries: dict[str, EngineSummary] = {name: summarize(name, results[name], prices) for name in names}
    calibrations = {name: calibrate(results[name], max_wrong) for name in names}
    comparisons = [compare(a, results[a], b, results[b]) for a, b in combinations(names, 2)]
    warnings = _warnings(results, manifests)

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    first = summaries[names[0]]
    datasets = sorted({(m.get("dataset"), m.get("dataset_sha256")) for m in manifests.values() if m.get("dataset")})

    lines: list[str] = ["# Scoreboard", ""]
    if len(datasets) == 1:
        dataset, digest = datasets[0]
        source = f"dataset `{dataset}`" + (f" (sha256 {digest[:12]})" if digest else "") + " · "
    elif datasets:
        source = f"{len(datasets)} ticket files · "
    else:
        source = ""
    lines.append(
        f"Generated {now} · {source}{first.tickets} labeled tickets: {first.with_fiche} with a fiche, "
        f"{first.without_fiche} without · ceiling on wrong fiches shown: {pct(max_wrong, 0)}"
    )
    lines.append("")

    show_none = any(s.without_fiche for s in summaries.values())
    header = ["Engine", "Exact fiche @1", "Wrong fiche shown", "No fiche shown"]
    if show_none:
        header.append('Correct "no fiche"')
    header += ["Recall@5", "Stability", "Latency p50 / p95", "Cost per ticket"]
    lines += ["## Summary", "", "| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for name in names:
        s = summaries[name]
        stability = f"{pct(s.stability.value, 0)} ({s.runs} runs)" if s.stability else "1 run"
        cost = "–" if s.cost_per_ticket is None else f"{s.currency} {s.cost_per_ticket:.4f}"
        row = [cell(name), fmt_rate(s.exact), fmt_rate(s.wrong_shown), fmt_rate(s.no_fiche)]
        if show_none:
            row.append(fmt_rate(s.correct_none))
        row += [fmt_rate(s.recall_at_k), stability, f"{fmt_seconds(s.latency_p50)} / {fmt_seconds(s.latency_p95)}", cost]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    widest = max((s.with_fiche for s in summaries.values()), default=0)
    if widest:
        half = 1.959963984540054 * (0.25 / widest) ** 0.5
        lines.append(
            f"Brackets are 95% intervals. With {widest} tickets that have a fiche, a rate is known to about "
            f"±{half * 100:.0f} points at worst: smaller gaps between engines are not proven."
        )
        lines.append("")

    lines += [f"**Ceiling: at most {pct(max_wrong, 0)} wrong fiches shown, judged on the upper end of the interval.**", ""]
    for name in names:
        s = summaries[name]
        if not s.wrong_shown.n:
            continue
        verdict = "within the ceiling" if s.wrong_shown.high <= max_wrong else "above the ceiling"
        lines.append(f"- {cell(name)}: {verdict} (upper end {pct(s.wrong_shown.high)})")
        if len(s.exact_by_run) > 1 and None not in s.exact_by_run:
            low, high = min(s.exact_by_run), max(s.exact_by_run)
            if high - low > 1e-9:
                lines.append(f"  - exact fiche @1 moved between {pct(low)} and {pct(high)} across runs")
    lines.append("")

    if comparisons:
        lines += [
            "## Head to head (exact fiche @1, same tickets)",
            "",
            "| Engines | Tickets | Only the first right | Only the second right | Both right | Neither | p (exact McNemar) | Reading |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for c in comparisons:
            if c.p_value < 0.05:
                winner = c.first if c.first_only > c.second_only else c.second
                reading = f"{winner} is better"
            else:
                reading = "no proven difference"
            lines.append(
                f"| {cell(c.first)} vs {cell(c.second)} | {c.tickets} | {c.first_only} | {c.second_only} | "
                f"{c.both} | {c.neither} | {c.p_value:.3f} | {reading} |"
            )
        lines += ["", "Only the tickets where the two engines disagree count in the test.", ""]

    calibrated = [(name, cal) for name, cal in calibrations.items() if cal is not None]
    if calibrated:
        lines += ["## Abstention threshold", ""]
        lines.append(
            "What each engine would do if it showed its fiche only when its score reaches the threshold, "
            "and abstained otherwise. Pick the threshold on one set of tickets and confirm it on another."
        )
        lines.append("")
        for name, cal in calibrated:
            lines += [f"### {cell(name)}", ""]
            rec = cal.recommended
            if rec is not None and rec.threshold is None:
                lines.append(f"No threshold needed: the engine is already within the {pct(max_wrong, 0)} ceiling.")
            elif rec is not None:
                lines.append(
                    f"Lowest threshold within the ceiling: **{fmt_score(rec.threshold)}** → exact fiche @1 "
                    f"{fmt_rate(rec.exact)}, wrong fiche shown {fmt_rate(rec.wrong_shown)}, no fiche shown {pct(rec.no_fiche.value)}."
                )
            else:
                n = summaries[name].tickets
                needed = tickets_needed(max_wrong)
                if needed is not None and n < needed:
                    lines.append(
                        f"No threshold can be proven within {pct(max_wrong, 0)} on {n} tickets: even with zero wrong "
                        f"fiches, that takes at least {needed} tickets."
                    )
                else:
                    lines.append(
                        f"No threshold keeps the wrong fiches under {pct(max_wrong, 0)}: the engine is wrong even at its highest scores."
                    )
            lines += [
                "",
                "| Show the fiche when score ≥ | Exact fiche @1 | Wrong fiche shown | No fiche shown |",
                "| --- | --- | --- | --- |",
            ]
            for row in _sweep_rows(cal):
                mark = " ←" if row is rec else ""
                lines.append(
                    f"| {fmt_score(row.threshold)}{mark} | {fmt_rate(row.exact)} | {fmt_rate(row.wrong_shown)} | {pct(row.no_fiche.value)} |"
                )
            lines.append("")

    lines += [f"## Misses on the first run (up to {misses_per_engine} per engine)", ""]
    for name in names:
        misses = []
        for record in canonical(results[name]):
            fiche = shown(record)
            missed = (record["expected"] and not is_exact(record)) or (not record["expected"] and fiche is not None)
            if missed:
                misses.append(record)
        lines += [f"### {cell(name)}: {len(misses)} {'miss' if len(misses) == 1 else 'misses'}", ""]
        if not misses:
            continue
        lines += ["| Ticket | Client | Expected | Decision | Score | Ticket text |", "| --- | --- | --- | --- | --- | --- |"]
        for record in misses[:misses_per_engine]:
            expected = " | ".join(record["expected"]) if record["expected"] else "none"
            fiche = shown(record)
            decision = f"fiche {fiche}" if fiche else record["kind"]
            if record.get("error"):
                decision = f"error: {short(record['error'], 60)}"
            score = "" if record.get("score") is None else f"{float(record['score']):.4g}"
            text = short(tickets_text.get(ticket_key(record), ""))
            lines.append(
                f"| {cell(record['ticket_id'])} | {cell(record['client'])} | {cell(expected)} | {cell(decision)} | {score} | {cell(text)} |"
            )
        lines.append("")

    errors = [(name, s) for name, s in summaries.items() if s.errors]
    if warnings or errors:
        lines += ["## Warnings", ""]
        lines += [f"- {w}" for w in warnings]
        for name, s in errors:
            sample = "; ".join(short(e, 120) for e in s.error_samples[:2])
            lines.append(f"- {cell(name)}: {s.errors} decisions failed and count as no fiche shown (e.g. {cell(sample)})")
        lines.append("")

    summary = {
        "generated_at": now,
        "max_wrong": max_wrong,
        "prices": {"currency": prices.currency, "source": prices.source} if prices is not None else None,
        "engines": {
            name: {**summaries[name].to_dict(), "calibration": calibrations[name].to_dict() if calibrations[name] else None}
            for name in names
        },
        "comparisons": [c.to_dict() for c in comparisons],
        "warnings": warnings,
        "manifests": manifests,
    }
    return "\n".join(lines).rstrip() + "\n", summary
