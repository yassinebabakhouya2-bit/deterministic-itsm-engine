"""Real ports of the guided engine: strict-schema model calls (temperature 0, seeded),
risk rules, retrieval adapter. Model output is validated by code before it can matter."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Callable, Dict, List, Optional, Tuple

from .contracts import GuideState, KbCandidate, OcrFinding, Variable
from .fsm import Ports, Thresholds
from .prompts import (EXTRACT_PROMPT, EXTRACT_SCHEMA, GUIDE_PROMPT, GUIDE_SCHEMA, HELP_PROMPT, HELP_SCHEMA,
                      JUDGE_PROMPT, JUDGE_SCHEMA, OCR_PROMPT, OCR_SCHEMA)
from .textutil import kb_text, redact

_RISK_RULES = {
    "mfa_reset": re.compile(r"(r[ée]initialis|reset|supprim|d[ée]sactiv|contourn|bypass).{0,40}"
                            r"(mfa|2fa|authentification (multi|[àa] deux)|double authentification|authenticator)", re.I | re.S),
    "privileged_access": re.compile(r"((donn|accord|ajout|attribu|grant|elev|[ée]lev)\w*.{0,40}"
                                    r"(droits?|acc[eè]s|r[oô]le)\s+(d['e ]?\s?)?(admin|global|privil[eè]g|[ée]lev))"
                                    r"|(devenir|be|make me)\s+(global\s+)?admin", re.I | re.S),
    "data_deletion": re.compile(r"(supprim|effac|delete|wipe|purge)\w*.{0,40}"
                                r"(donn[ée]es|fichiers?|bo[iî]te (mail|aux)|mailbox|compte|data|files|account)", re.I | re.S),
    "security_incident": re.compile(r"(phishing|hame[cç]onnage|ransomware|rançongiciel|compromis|piratage|pirat[ée]|"
                                    r"malware|fuite de donn[ée]es|data breach)", re.I),
}


def detect_risks(st: GuideState) -> List[str]:
    text = st.conversation_text or ""
    return sorted(flag for flag, rx in _RISK_RULES.items() if rx.search(text))


def _chat_json(aoai, model: str, seed: int, name: str, schema: dict, messages: list) -> dict:
    resp = aoai.chat.completions.create(
        model=model, temperature=0, seed=seed, messages=messages,
        response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}})
    return json.loads(resp.choices[0].message.content)


_ERROR_CODE_PATTERNS = [re.compile(r"^0x[0-9A-Fa-f]{8}$"), re.compile(r"^AADSTS\d{5,6}$"),
                        re.compile(r"^[A-Z]{3,5}-\d{3,6}$"), re.compile(r"^(error|erreur|code)?\s*\d{3,8}$", re.I),
                        re.compile(r"^[A-Z][A-Z0-9_]{5,40}$")]


def _known_error_code(text: str) -> bool:
    return any(rx.match(text.strip()) for rx in _ERROR_CODE_PATTERNS)


def make_extract_variables(aoai, model: str, seed: int) -> Callable[[str], List[Variable]]:
    def extract(text: str) -> List[Variable]:
        clean = redact(text)[:4000]
        try:
            data = _chat_json(aoai, model, seed, "guide_extract", EXTRACT_SCHEMA, [
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": f"<untrusted_ticket>\n{clean}\n</untrusted_ticket>"}])
        except Exception:
            return []
        if data.get("injection_suspected"):
            return []
        low, out = clean.lower(), []
        for v in data.get("variables", []):
            value = (v.get("value") or "").strip()[:256]
            if not value or value.lower() not in low or "[REDACTED]" in value:
                continue
            conf = max(0.0, min(1.0, float(v.get("confidence", 0))))
            if v["name"] == "error_code" and not _known_error_code(value):
                conf = min(conf, 0.5)
            out.append(Variable(name=v["name"], value=value, source="ticket_text", confidence=conf))
        return out
    return extract


def make_ocr(aoai, model: str, seed: int, images: Dict[str, Tuple[bytes, str]]):
    def ocr(refs: List[str]) -> Tuple[List[OcrFinding], bool]:
        findings, readable_any = [], False
        for ref in refs:
            blob = images.get(ref)
            if not blob:
                continue
            data, mime = blob
            sha = hashlib.sha256(data).hexdigest()
            b64 = base64.b64encode(data).decode("ascii")
            try:
                out = _chat_json(aoai, model, seed, "guide_ocr", OCR_SCHEMA, [
                    {"role": "system", "content": OCR_PROMPT},
                    {"role": "user", "content": [{"type": "text", "text": "Lis cette capture."},
                                                 {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}])
            except Exception:
                continue
            if not out.get("readable"):
                continue
            readable_any = True
            for f in out.get("findings", []):
                text = redact((f.get("text") or "").strip())[:512]
                if not text:
                    continue
                conf = max(0.0, min(1.0, float(f.get("confidence", 0))))
                findings.append(OcrFinding(image_sha256=sha, kind=f["kind"], text=text, ocr_confidence=conf,
                                           verified=f["kind"] == "error_code" and conf >= 0.85 and _known_error_code(text)))
        return findings, readable_any
    return ocr


def problem_text(st: GuideState) -> str:
    parts = [st.conversation_text]
    for v in st.variables:
        if v.name in ("application", "error_code", "os_family") and v.value not in st.conversation_text:
            parts.append(v.value)
    parts += [f.text for f in st.ocr_findings if f.kind != "cli_output" and f.text not in st.conversation_text]
    return " ".join(p for p in parts if p).strip()[:1500]


def make_retrieve(search_docs: Callable[[str], List[dict]], keep: int = 5):
    def retrieve(st: GuideState) -> List[KbCandidate]:
        best: Dict[str, dict] = {}
        for d in search_docs(problem_text(st)):
            pid = d.get("parent_id") or d.get("title")
            if not pid:
                continue
            score = float(d.get("@search.rerankerScore") or 0)
            cur = best.get(pid)
            if cur is None or score > cur["score"]:
                best[pid] = {"score": score, "title": d.get("title") or pid,
                             "chunks": [d.get("chunk_id")] if d.get("chunk_id") else [],
                             "excerpt": (d.get("chunk") or "").strip()[:4000]}
        ranked = sorted(best.items(), key=lambda kv: (-kv[1]["score"], kv[0]))[:keep]
        return [KbCandidate(parent_id=pid, title=v["title"], reranker_score=v["score"],
                            chunk_ids=[c for c in v["chunks"] if c], excerpt=v["excerpt"]) for pid, v in ranked]
    return retrieve


def make_judge(aoai, model: str, seed: int):
    def judge(st: GuideState, cands: List[KbCandidate]) -> Optional[str]:
        alias = {f"c{i + 1}": c for i, c in enumerate(cands[:3])}
        fiches = "\n".join(f'<fiche id="{a}" titre="{c.title}">\n{kb_text(c.excerpt, 1200)}\n</fiche>' for a, c in alias.items())
        data = _chat_json(aoai, model, seed, "guide_judge", JUDGE_SCHEMA, [
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": f"<probleme>\n{redact(problem_text(st))}\n</probleme>\n<fiches>\n{fiches}\n</fiches>"}])
        if data.get("exact") and data.get("best") in alias:
            return alias[data["best"]].parent_id
        return None
    return judge


def make_load_chunks(fetch_chunks: Callable[[str], List[dict]]):
    def load(parent_id: str) -> Dict[str, str]:
        return {c["chunk_id"]: c.get("chunk") or "" for c in fetch_chunks(parent_id) if c.get("chunk_id")}
    return load


def _doc(chunks: Dict[str, str]):
    alias = {f"c{i + 1}": cid for i, cid in enumerate(chunks)}
    text = "\n".join(f'<chunk id="{a}">\n{kb_text(chunks[cid], 6000)}\n</chunk>' for a, cid in alias.items())
    return alias, text


def make_build_guide(aoai, model: str, seed: int):
    def build(st: GuideState, cand: KbCandidate, chunks: Dict[str, str]) -> dict:
        alias, doc = _doc(chunks)
        vs = "\n".join(f"- {v.name}: {v.value}" for v in st.variables) or "(aucune)"
        d = _chat_json(aoai, model, seed, "guide_build", GUIDE_SCHEMA, [
            {"role": "system", "content": GUIDE_PROMPT},
            {"role": "user", "content": f"<kb_document>\n{doc}\n</kb_document>\n<diagnostic_state>\n{vs}\n</diagnostic_state>"}])
        for s in d.get("steps", []):
            s["source_chunk_id"] = alias.get(s.get("source_chunk_id"), s.get("source_chunk_id"))
        return d
    return build


def make_help_step(aoai, model: str, seed: int):
    def help_step(st: GuideState, step, chunks: Dict[str, str], user_text: str) -> dict:
        alias, doc = _doc(chunks)
        back = {cid: a for a, cid in alias.items()}
        d = _chat_json(aoai, model, seed, "guide_help", HELP_SCHEMA, [
            {"role": "system", "content": HELP_PROMPT},
            {"role": "user", "content": f"<kb_document>\n{doc}\n</kb_document>\n"
                                        f"<etape n=\"{step.order}\">{step.instruction}</etape>\n"
                                        f"<message>\n{redact(user_text)[:1500]}\n</message>"}])
        cid = alias.get(d.get("source_chunk_id"))
        text = (d.get("answer_fr") or "").strip()
        if d.get("found_in_kb") and cid and text:
            return {"text": text, "found_in_kb": True, "source_chunk_id": cid}
        # not grounded in the fiche: say so, and re-show the fiche's own wording of the step
        return {"text": "La fiche ne détaille pas davantage ce point. Rappel de la consigne : " + step.instruction,
                "found_in_kb": False, "source_chunk_id": step.source_chunk_id}
    return help_step


def build_ports(*, aoai, model: str, seed: int, search_docs, fetch_chunks, images,
                thresholds: Optional[Thresholds] = None) -> Ports:
    return Ports(
        extract_variables=make_extract_variables(aoai, model, seed), detect_risks=detect_risks,
        retrieve=make_retrieve(search_docs), ocr=make_ocr(aoai, model, seed, images),
        judge=make_judge(aoai, model, seed), load_chunks=make_load_chunks(fetch_chunks),
        build_guide=make_build_guide(aoai, model, seed), help_step=make_help_step(aoai, model, seed),
        thresholds=thresholds or Thresholds())
