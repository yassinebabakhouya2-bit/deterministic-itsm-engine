"""Quality report of a decomposed KB: the gate of slice 2 and what to look at."""

from __future__ import annotations

import datetime as dt

from .decompose import DecomposedFiche, summarize_kb
from .profile import Profile

MAX_ROWS = 20


def _cell(text) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _short(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _pct(part: int, whole: int) -> str:
    return "–" if not whole else f"{100 * part / whole:.1f}%"


def build_report(client: str, decomposed: list[DecomposedFiche], profile: Profile | None, llm_stats: dict,
                 warnings: list[str] | None = None) -> tuple[str, dict]:
    summary = summarize_kb(decomposed)
    known_ids = {d.fiche_id for d in decomposed}
    missing: dict[str, list[str]] = {}
    for d in decomposed:
        for ref in d.references:
            if ref not in known_ids:
                missing.setdefault(ref, []).append(d.fiche_id)
    summary["missing_references"] = {ref: sorted(by) for ref, by in sorted(missing.items())}
    summary["llm"] = llm_stats
    summary["warnings"] = warnings or []

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    llm_line = llm_stats.get("model") or "not run"
    lines = [f"# KB decomposition: {client}", ""]
    lines.append(f"Generated {now} · {summary['fiches']} fiches · LLM: {llm_line}")
    lines.append("")
    lines += [
        "## Summary",
        "",
        "| Fiches | Guided | Citable | Info only | Steps | Steps on a verified extract | Mean agreement |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        f"| {summary['fiches']} | {summary['guided']} ({_pct(summary['guided'], summary['fiches'])}) "
        f"| {summary['citable']} | {summary['info_only']} | {summary['steps']} "
        f"| {summary['steps_verified']} ({_pct(summary['steps_verified'], summary['steps'])}) "
        f"| {'–' if summary['mean_agreement'] is None else summary['mean_agreement']} |",
        "",
    ]
    gate = summary["steps"] == summary["steps_verified"]
    lines.append(
        f"**Gate of slice 2: every step on a verified extract: {'PASS' if gate else 'FAIL'}** "
        f"({summary['steps_verified']} of {summary['steps']})."
    )
    lines.append("")
    lines.append(
        "Guided: both methods agree, the fiche can be followed step by step. Citable: shown as a source, never "
        "guided. Info only: no resolution step."
    )
    lines.append("")
    if llm_stats.get("model"):
        lines.append(
            f"LLM: {llm_stats.get('calls', 0)} calls, {llm_stats.get('cached', 0)} answers read from the record, "
            f"{llm_stats.get('errors', 0)} failures, {llm_stats.get('input_tokens', 0)} tokens in and "
            f"{llm_stats.get('output_tokens', 0)} out. Quotes not found in the fiche: {summary['llm_quotes_rejected']}; "
            f"rewordings dropped for adding technical content: {summary['instructions_dropped']}."
        )
    else:
        lines.append("The LLM pass did not run: every fiche keeps low confidence until it does.")
    lines.append("")

    if profile is not None:
        lines += ["## Writing profile", ""]
        stability = "–" if profile.stability is None else f"{profile.stability:.2f}"
        lines.append(
            f"Learned on {profile.fiches} fiches · stable: {'yes' if profile.stable else 'no'} (overlap {stability}) · "
            + ", ".join(f"{k}: {v}" for k, v in sorted(profile.styles.items()))
        )
        lines.append("")
        top = sorted(profile.headings.items(), key=lambda item: -item[1]["count"])[:15]
        if top:
            lines += ["| Heading | Fiches | Role | Source |", "| --- | --- | --- | --- |"]
            for key, entry in top:
                lines.append(f"| {_cell(entry.get('example', key))} | {entry['count']} | {entry.get('role') or '–'} | {entry['source']} |")
            lines.append("")
        if profile.boilerplate:
            lines.append(f"Boilerplate removed from every fiche ({len(profile.boilerplate)} lines), e.g.:")
            lines += [f"- {_short(line, 90)}" for line in profile.boilerplate[:5]]
            lines.append("")

    citable = sorted((d for d in decomposed if d.status == "citable"), key=lambda d: (d.methods.get("agreement") or 0))
    if citable:
        lines += [f"## Citable fiches, least agreement first (up to {MAX_ROWS})", ""]
        lines += ["| Fiche | Title | Steps | Agreement | Why |", "| --- | --- | --- | --- | --- |"]
        for d in citable[:MAX_ROWS]:
            agreement = d.methods.get("agreement")
            why = "; ".join(r for r in d.reasons if not r.startswith("single method")) or d.reasons[0] if d.reasons else ""
            lines.append(
                f"| {_cell(d.fiche_id)} | {_cell(_short(d.title, 50))} | {len(d.steps)} | "
                f"{'–' if agreement is None else agreement} | {_cell(_short(why, 90))} |"
            )
        lines.append("")
    info = [d for d in decomposed if d.status == "info_only"]
    if info:
        lines += [f"## Info-only fiches (up to {MAX_ROWS})", "", "| Fiche | Title |", "| --- | --- |"]
        lines += [f"| {_cell(d.fiche_id)} | {_cell(_short(d.title, 70))} |" for d in info[:MAX_ROWS]]
        lines.append("")
    if missing:
        lines += ["## References to fiches that are not in the KB", ""]
        lines += [f"- {ref}: referenced by {', '.join(sorted(by)[:5])}" for ref, by in list(sorted(missing.items()))[:MAX_ROWS]]
        lines.append("")
    if warnings:
        lines += ["## Warnings", ""]
        lines += [f"- {_cell(w)}" for w in warnings[:50]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", summary
