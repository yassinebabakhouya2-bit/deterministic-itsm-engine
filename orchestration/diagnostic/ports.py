"""Real ports for the diagnostic FSM: model calls with strict schemas, risk rules,
retrieval adapter, deterministic question selection. Every function here is either
deterministic code or a temperature-0 seeded model call whose output is validated
by code before it can influence a transition."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Callable, Dict, List, Optional, Tuple

from .contracts import (DiagnosticState, FinalExecutionPlan, KbCandidate, OcrFinding,
                        UserNextActionPrompt, Variable, ChoiceOption)
from .fsm import Ports, Thresholds, compute_plan_sha256, evidence_hash
from .prompts import (EXTRACT_PROMPT, EXTRACT_SCHEMA, OCR_PROMPT, OCR_SCHEMA, PLAN_PROMPT,
                      PLAN_SCHEMA)

# ------------------------------------------------------------------ risks

_RISK_RULES = {
    "mfa_reset": re.compile(
        r"(r[ée]initialis|reset|supprim|d[ée]sactiv|contourn|bypass).{0,40}"
        r"(mfa|2fa|authentification (multi|[àa] deux)|double authentification|authenticator)",
        re.I | re.S),
    "privileged_access": re.compile(
        r"((donn|accord|ajout|attribu|grant|elev|[ée]lev)\w*.{0,40}"
        r"(droits?|acc[eè]s|r[oô]le)\s+(d['e ]?\s?)?(admin|global|privil[eè]g|[ée]lev))"
        r"|(devenir|be|make me)\s+(global\s+)?admin", re.I | re.S),
    "data_deletion": re.compile(
        r"(supprim|effac|delete|wipe|purge)\w*.{0,40}"
        r"(donn[ée]es|fichiers?|bo[iî]te (mail|aux)|mailbox|compte|data|files|account)",
        re.I | re.S),
    "security_incident": re.compile(
        r"(phishing|hame[cç]onnage|ransomware|rançongiciel|compromis|piratage|pirat[ée]|"
        r"malware|fuite de donn[ée]es|data breach)", re.I),
}


def detect_risks(st: DiagnosticState) -> List[str]:
    text = st.conversation_text or ""
    return sorted(flag for flag, rx in _RISK_RULES.items() if rx.search(text))


# ------------------------------------------------------------ model calls

def _chat_json(aoai, model: str, seed: int, schema_name: str, schema: dict, messages: list) -> dict:
    resp = aoai.chat.completions.create(
        model=model, temperature=0, seed=seed, messages=messages,
        response_format={"type": "json_schema",
                         "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
    )
    return json.loads(resp.choices[0].message.content)


_ERROR_CODE_PATTERNS = [
    re.compile(r"^0x[0-9A-Fa-f]{8}$"),
    re.compile(r"^AADSTS\d{5,6}$"),
    re.compile(r"^[A-Z]{3,5}-\d{3,6}$"),
    re.compile(r"^(error|erreur|code)?\s*\d{3,8}$", re.I),
    re.compile(r"^[A-Z][A-Z0-9_]{5,40}$"),
]


def _known_error_code(text: str) -> bool:
    t = text.strip()
    return any(rx.match(t) for rx in _ERROR_CODE_PATTERNS)


_SECRET = re.compile(r"(mot de passe|password|pwd|mdp)\s*[:=]\s*\S+", re.I)


def make_extract_variables(aoai, model: str, seed: int) -> Callable[[str], List[Variable]]:
    def extract(text: str) -> List[Variable]:
        clean = _SECRET.sub(r"\1: [REDACTED]", text)[:4000]
        try:
            data = _chat_json(aoai, model, seed, "diag_extract", EXTRACT_SCHEMA, [
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": f"<untrusted_ticket>\n{clean}\n</untrusted_ticket>"},
            ])
        except Exception:
            return []
        if data.get("injection_suspected"):
            return []                      # an injection attempt yields no facts at all
        low = clean.lower()
        out: List[Variable] = []
        for v in data.get("variables", []):
            value = (v.get("value") or "").strip()[:256]
            # zero inference: a value must literally appear in the text
            if not value or value.lower() not in low or "[REDACTED]" in value:
                continue
            conf = max(0.0, min(1.0, float(v.get("confidence", 0))))
            if v["name"] == "error_code" and not _known_error_code(value):
                conf = min(conf, 0.5)
            out.append(Variable(name=v["name"], value=value, source="ticket_text", confidence=conf))
        return out
    return extract


def make_ocr(aoai, model: str, seed: int,
             images: Dict[str, Tuple[bytes, str]]) -> Callable[[List[str]], Tuple[List[OcrFinding], bool]]:
    """images: reference -> (bytes, mime). Bytes stay in memory, never persisted."""
    def ocr(refs: List[str]) -> Tuple[List[OcrFinding], bool]:
        findings: List[OcrFinding] = []
        any_readable = False
        for ref in refs:
            blob = images.get(ref)
            if not blob:
                continue
            data, mime = blob
            sha = hashlib.sha256(data).hexdigest()
            b64 = base64.b64encode(data).decode("ascii")
            try:
                out = _chat_json(aoai, model, seed, "diag_ocr", OCR_SCHEMA, [
                    {"role": "system", "content": OCR_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": "Lis cette capture."},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]},
                ])
            except Exception:
                continue
            if not out.get("readable"):
                continue
            any_readable = True
            for f in out.get("findings", []):
                text = _SECRET.sub(r"\1: [REDACTED]", (f.get("text") or "").strip())[:512]
                if not text:
                    continue
                conf = max(0.0, min(1.0, float(f.get("confidence", 0))))
                verified = f["kind"] == "error_code" and conf >= 0.85 and _known_error_code(text)
                findings.append(OcrFinding(image_sha256=sha, kind=f["kind"], text=text,
                                           ocr_confidence=conf, bbox=(0, 0, 0, 0), verified=verified))
        return findings, any_readable
    return ocr


# -------------------------------------------------------------- retrieval

def build_query(st: DiagnosticState) -> str:
    parts = [st.conversation_text]
    for v in st.variables:
        if v.name in ("application", "error_code", "os_family") and v.value not in st.conversation_text:
            parts.append(v.value)
    parts += [f.text for f in st.ocr_findings
              if f.kind != "cli_output" and f.text not in st.conversation_text]
    return " ".join(p for p in parts if p).strip()[:1500]


def make_retrieve(search_docs: Callable[[str], List[dict]], keep: int = 5
                  ) -> Callable[[DiagnosticState], List[KbCandidate]]:
    """search_docs(query) -> text KB hits (dicts with parent_id, title, chunk_id,
    @search.rerankerScore). Duplicated documents keep their best score; the order is
    stable (score desc, parent_id asc)."""
    def retrieve(st: DiagnosticState) -> List[KbCandidate]:
        best: Dict[str, dict] = {}
        for d in search_docs(build_query(st)):
            pid = d.get("parent_id") or d.get("title")
            if not pid:
                continue
            score = float(d.get("@search.rerankerScore") or 0)
            cur = best.get(pid)
            if cur is None or score > cur["score"]:
                best[pid] = {"score": score, "title": d.get("title") or pid,
                             "chunks": [d.get("chunk_id")] if d.get("chunk_id") else [],
                             "excerpt": (d.get("chunk") or "").strip()[:1500]}
        ranked = sorted(best.items(), key=lambda kv: (-kv[1]["score"], kv[0]))[:keep]
        return [KbCandidate(parent_id=pid, title=v["title"], reranker_score=v["score"],
                            chunk_ids=[c for c in v["chunks"] if c], excerpt=v["excerpt"])
                for pid, v in ranked]
    return retrieve


# --------------------------------------------------------------- questions

_OS_OPTIONS = [("windows", "Windows"), ("macos", "macOS"), ("ios", "iPhone / iPad"),
               ("android", "Android"), ("linux", "Linux")]


def next_question(st: DiagnosticState) -> Optional[UserNextActionPrompt]:
    """Deterministic: the code picks WHAT to ask, from missing variables first, then from
    the ambiguity between the two best candidates. Already-asked ids are skipped."""
    names = {v.name for v in st.variables if v.confidence >= 0.5}
    verified_code = any(f.kind == "error_code" and f.verified for f in st.ocr_findings)
    asked = set(st.asked_question_ids)

    def q(qid, kind, var, text, why, **kw):
        return None if qid in asked else UserNextActionPrompt(
            question_id=qid, kind=kind, target_variable=var, text_fr=text, why_needed=why, **kw)

    steps = []
    if "application" not in names:
        steps.append(lambda: q("q_application", "free_text_short", "application",
                               "Quelle application ou quel service est concerné ?",
                               "Cibler la bonne fiche."))
    if len(st.candidates) >= 2:
        top = st.candidates[:3]
        qid = "q_disc_" + hashlib.sha1("|".join(c.parent_id for c in top).encode()).hexdigest()[:8]
        steps.append(lambda: q(qid, "choose_one", "symptom",
                               "Laquelle de ces situations correspond le mieux à votre problème ?",
                               "Départager des fiches proches.",
                               options=[ChoiceOption(id=c.parent_id[:60], label=c.title[:120]) for c in top],
                               discriminates=[c.parent_id for c in top]))
    # a screenshot already read without any code: asking for another one is pointless
    if "error_code" not in names and not verified_code and not st.ocr_findings:
        steps.append(lambda: q("q_error_code", "request_screenshot", "error_code",
                               "Pouvez-vous joindre une capture d'écran du message d'erreur complet "
                               "(fenêtre entière, sans recadrage) ? Masquez les mots de passe.",
                               "Lire le code d'erreur exact.",
                               screenshot_hint="Fenêtre entière, message d'erreur visible."))
    if "symptom" not in names:
        steps.append(lambda: q("q_symptom", "free_text_short", "symptom",
                               "Que se passe-t-il exactement, et à quel moment ?",
                               "Distinguer des fiches proches."))
    if "os_family" not in names:
        steps.append(lambda: q("q_os", "choose_one", "os_family",
                               "Sur quel système le problème se produit-il ?",
                               "Certaines procédures diffèrent selon le système.",
                               options=[ChoiceOption(id=i, label=l) for i, l in _OS_OPTIONS]))
    for build in steps:
        result = build()
        if result is not None:
            return result
    return None


# -------------------------------------------------------------------- plan

def make_load_chunks(fetch_chunks: Callable[[str], List[dict]]) -> Callable[[str], Dict[str, str]]:
    def load(parent_id: str) -> Dict[str, str]:
        return {c["chunk_id"]: c.get("chunk") or "" for c in fetch_chunks(parent_id) if c.get("chunk_id")}
    return load


def make_build_plan(aoai, model: str, seed: int, source_system: str = "servicenow_kb"
                    ) -> Callable[[DiagnosticState, Dict[str, str]], dict]:
    def build(st: DiagnosticState, chunks: Dict[str, str]) -> dict:
        ids = list(chunks)                                  # stable document order
        alias = {f"c{i + 1}": cid for i, cid in enumerate(ids)}
        doc = "\n".join(f'<chunk id="{a}">\n{chunks[cid]}\n</chunk>' for a, cid in alias.items())
        vs = "\n".join(f"- {v.name}: {v.value} (confirmee={v.confirmed})" for v in st.variables)
        draft = _chat_json(aoai, model, seed, "diag_plan", PLAN_SCHEMA, [
            {"role": "system", "content": PLAN_PROMPT},
            {"role": "user", "content":
                f"<kb_document>\n{doc}\n</kb_document>\n<diagnostic_state>\n{vs}\n</diagnostic_state>"},
        ])
        if not draft.get("applicable") or not draft.get("steps"):
            return {"applicable": False, "reason": (draft.get("reason") or "non applicable")[:300],
                    "missing_information": draft.get("missing_information") or []}
        cand = next((c for c in st.candidates if c.parent_id == st.selected_parent_id), None)
        steps = []
        for i, s in enumerate(draft["steps"], start=1):
            steps.append({"order": i, "instruction": s["instruction"][:500],
                          "action_type": s["action_type"],
                          "source_chunk_id": alias.get(s["source_chunk_id"], s["source_chunk_id"]),
                          "verbatim_from_kb": bool(s["verbatim_from_kb"]),
                          "requires_confirmation": True, "rollback": None})
        plan = {
            "schema_version": "1.0", "ticket_id": st.ticket_id, "session_id": st.session_id,
            "kb_parent_id": st.selected_parent_id, "kb_title": cand.title if cand else "",
            "kb_version": "unversioned",
            "source_system": cand.source_system if cand else source_system,
            "source_url": cand.source_url if cand else "",
            "confidence": st.confidence, "preconditions": draft.get("preconditions") or [],
            "steps": steps, "verification": draft.get("verification") or [],
            "closure_code": "pending_user_confirmation", "evidence_hash": evidence_hash(st),
        }
        plan["plan_sha256"] = compute_plan_sha256(plan)
        return plan
    return build


# ---------------------------------------------------------------- assembly

def build_ports(*, aoai, model: str, seed: int, search_docs: Callable[[str], List[dict]],
                fetch_chunks: Callable[[str], List[dict]], images: Dict[str, Tuple[bytes, str]],
                thresholds: Optional[Thresholds] = None, source_system: str = "servicenow_kb") -> Ports:
    return Ports(
        extract_variables=make_extract_variables(aoai, model, seed),
        detect_risks=detect_risks,
        retrieve=make_retrieve(search_docs),
        ocr=make_ocr(aoai, model, seed, images),
        next_question=next_question,
        load_chunks=make_load_chunks(fetch_chunks),
        build_plan=make_build_plan(aoai, model, seed, source_system),
        thresholds=thresholds or Thresholds(),
    )
