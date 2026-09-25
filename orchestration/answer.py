# =====================================================================
# KnowledgeEngine v9 — Orchestration layer (Jalon 3)
#
# Retrieval (Azure AI Search, hybrid + semantic reranker)
#   -> hierarchy split (axiom A3, rank-based on @search.rerankerScore)
#   -> generation (Azure OpenAI Chat Completions, Structured Outputs,
#      temperature=0 + seed -> axiom A1)
#   -> typed, schema-enforced answer.
#
# Design note (2026-09-10): Foundry Agent Service (PromptAgentDefinition +
# AzureAISearchTool) was the first option considered -- fully managed,
# deployable via SDK/Bicep. Dropped because it does not support Structured
# Outputs (response_format: json_schema) -- only direct Chat Completions /
# Responses API calls on an Azure OpenAI deployment do. A reliable,
# schema-enforced answer (no hallucinated shape, explicit `ambiguous` flag
# instead of guessing) was the explicit priority for this milestone, so
# this module calls the aif-knowledgeengine2-v9 deployment directly.
# Prompt Flow and Foundry's visual Workflows were also ruled out: both are
# being retired (2027-04-20 and 2026-12-01 respectively).
# See project memory jalon3-orchestration.md for the full discussion.
#
# Design note (2026-09-10, after first live test): the model is NOT asked
# to reproduce source titles -- source titles/order are already known
# deterministically from retrieval, so having the LLM retype them is an
# avoidable divergence point (the first live run showed the model writing
# "KB-A-001" instead of the indexed "KB-A-001.md"). The model only returns
# booleans (which sources it actually used); this script fills in the
# real titles from the retrieval result afterwards.
#
# Design note (2026-09-11, Jalon 4): added a keyless path
# (answer_query_core_keyless + build_aoai_client_keyless) for callers that
# use Azure AD RBAC instead of admin keys -- namely app/app.py, which runs
# on an Azure Web App and has no `az login` session to fetch keys with.
# answer_query_core(), retrieve() and answer_query() (the CLI, validated in
# Jalon 3) are UNCHANGED in behavior: retrieve() now delegates to the new
# _search_request() helper, but with the exact same {"api-key": ...} header
# it always used. Nothing here required or received a live retest, by
# design -- see project memory jalon4-app-interface.md.
#
# Design note (2026-09-17, post-Jalon 7 -- modality priority) [SUPERSEDED
# 2026-09-21: split_hierarchy() is gone, its job is now done at query level
# by retrieve_hierarchy()'s two modality-filtered searches -- the intent
# below still holds, only the mechanism changed]: split_hierarchy
# no longer picks the primary source by raw rank alone. A written KB document
# is preferred as PRIMARY over an audio/video transcript even when the
# transcript ranks higher on @search.rerankerScore -- a call is supporting
# evidence, not the source of truth, and users trust a KB article's wording
# over a transcript's. Falls back to the top-ranked result regardless of type
# only when no text document at all is present among the retrieved docs. Any
# audio/video doc among the rest is now guaranteed a slot in the annexes
# (bumping the lowest-ranked text annex if annex_count would otherwise crowd
# it out) -- "always mention audio/video as secondary" per Yassine's request.
#
# Design note (2026-09-18, later -- semantic merge across channels): per
# Yassine, call transcripts are sometimes incomplete (an agent skips a
# resolution step), so treating them as pure corroboration undersells what
# they can add. SYSTEM_PROMPT now explicitly asks the model to build ONE
# merged answer: the base procedure always comes from the SOURCE PRIMAIRE
# (never overridden or contradicted by an annex -- KB stays authoritative,
# matching the modality priority above), but genuinely useful
# complementary detail from annex call transcripts should be folded in, not
# just cited alongside. related_sources_used still reports which annexes
# actually contributed, now meaning "merged into the answer" rather than
# merely "consulted".
#
# Design note (2026-09-18, later still -- fidelity to the primary source):
# live testing showed the semantic-merge instruction above could nudge the
# model toward paraphrasing the SOURCE PRIMAIRE's own procedure rather than
# reproducing it -- Yassine flagged an answer that no longer read like the
# actual KB text. SYSTEM_PROMPT now explicitly requires the primary
# source's steps, order and wording to be kept faithfully; annex details
# may only be folded in around that unchanged backbone, never used to
# rewrite it. See also app/app.py's excerpt-visibility design note (same
# date) -- the KB excerpt is now shown by default precisely so this can be
# checked visually against the answer.
#
# Design note (2026-09-18 -- excerpt instead of full transcript): a full
# "voir la transcription" viewer (BlobClient route + sourcePath field) was
# built and then deliberately dropped before shipping, per Yassine: reading
# an entire call recording's transcript exposes more than the RAG actually
# used to answer (potential personal data unrelated to the question), and a
# raw speech-to-text dump ("Canal 0: ... Canal 1: ...") is noisy to read.
# attach_sources() now returns `excerpt` -- exactly the retrieved `chunk`
# (the same text already sent to the LLM as context, nothing more, nothing
# from the rest of the call) -- and app/app.py shows it in a collapsible
# <details>, not a link to the raw blob. No BlobClient, no extra Storage
# RBAC, no sourcePath field: smaller surface area, matches what was
# actually used to ground the answer.
#
# Keys are retrieved at runtime via `az` (never stored) for the CLI path,
# same convention as eval/evaluate_rag.py. Requires az login.
# Usage: python answer.py --client clienta --query "..."
# =====================================================================
import argparse
import base64
import json
import re
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import yaml
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent
# config/ (tracked, synthetic demo clients) is checked before clients-local/
# (git-ignored, real clients) -- a real client config can never shadow a
# tracked one by accident.
CONFIG_DIRS = [REPO_ROOT / "config", REPO_ROOT / "clients-local"]

RG = "rg-knowledgeengine-v9"
SEARCH_SERVICE = "srch-knowledgeengine2-v9"
SEARCH_ENDPOINT = f"https://{SEARCH_SERVICE}.search.windows.net"
SEARCH_API_VERSION = "2024-07-01"

AOAI_ACCOUNT = "aif-knowledgeengine2-v9"
AOAI_ENDPOINT = f"https://{AOAI_ACCOUNT}.openai.azure.com"
# First API version supporting Structured Outputs (response_format:
# json_schema, strict) -- same version eval/evaluate_rag.py already uses.
AOAI_API_VERSION = "2024-08-01-preview"

# Design note (2026-09-20, Jalon 7 follow-up -- candidate pool starving
# modality priority): retrieve()/_search_request() used to be asked for
# EXACTLY primaryCount + annexCount documents (top=primary_count +
# annex_count), then split_hierarchy() picked among THOSE for its
# text-over-audio/video PRIMARY preference (2026-09-17 design note above).
# That starves the preference of any real choice: once the audio-transcribe
# corpus grew (Jalon 7 PII backfill -- 131 files reprocessed for client-s),
# the top primary_count+annex_count hybrid+semantic hits for a query can
# ALL be audio/video chunks, so split_hierarchy never even sees a text KB
# document that ranks just outside that narrow window -- confirmed live on
# client-s: a direct index search for "mot de passe reinitialisation SSPR"
# surfaced KB0341/KB0339 (real SSPR procedure documents) outscoring every
# audio chunk, yet the deployed app answered from audio only, per Yassine.
# Fix: retrieve a wider CANDIDATE POOL (independent of primary_count +
# annex_count) so split_hierarchy has real text documents to choose from;
# the final primary/annex COUNTS handed to the model are unchanged.
#
# Design note (2026-09-21, Jalon 7 follow-up -- 12 was still too narrow,
# raised to the structural ceiling): confirmed live on client-s that 12
# was not enough -- "Comment reinitialiser mon mot de passe ?" surfaced
# KB0267/KB0341 at ranks 5/12 with a candidate window of 30 (k=30) but
# NEITHER at k=12: k feeds Azure AI Search's hybrid RRF fusion (vector +
# BM25) BEFORE semantic reranking, so shrinking k does not just truncate
# an already-sorted list, it changes which chunks are even competitive in
# the fusion. Rather than re-guess a bigger "seems to work" number that
# could fail on the next query, raised to 50 -- Azure AI Search's semantic
# ranker never reranks more than the top 50 results regardless of
# top/k requested, so 50 is not an arbitrary margin, it is the actual
# structural ceiling of what this architecture's reranking step can use.
# Below 50, real relevant documents can still be excluded before the
# reranker ever sees them (as just proven); above 50, a wider pool would
# buy nothing, since the reranker itself would never look past 50 anyway.
#
# Design note (2026-09-21, Jalon 7 follow-up -- SUPERSEDED by modality-
# filtered retrieval below): widening a single shared candidate pool was
# the wrong shape of fix and is abandoned here. It only ever made the
# right answer MORE LIKELY, never guaranteed: the text KB document and the
# audio chunks compete in ONE ranked list, so the bigger the transcribed
# call corpus grows, the further the official KB procedure gets pushed
# down, and there is no headroom left to buy -- Azure AI Search's semantic
# ranker never reranks past 50 results, so 50 is a hard ceiling, not a
# number that can keep growing with the corpus. Yassine's objection,
# correctly: every real client eventually has far more than 50 documents.
#
# The actual fix: stop deciding modality by POSITION in a shared ranking,
# and decide it in the QUERY itself -- two separate filtered searches per
# question (see retrieve_hierarchy()). The PRIMARY source comes from a
# search restricted to text KB documents, so it is by construction the
# best-ranked official KB document for that question, no matter how many
# audio chunks exist; the annexes come from a search restricted to
# audio/video. Neither can starve the other, because they never compete.
# This is scale-independent: it behaves identically at 150 documents and
# at 150 000.
#
# Honest scope of this guarantee: it makes the MODALITY of the primary
# source deterministic (always the top text KB document), NOT its topical
# correctness -- if the reranker ranks an off-topic KB article above the
# right one, that is a relevance problem, measured by eval/evaluate_rag.py,
# not something query filtering can fix.
_RERANK_CEILING = 50

# Modality filters (OData). _is_media() defines media as sourceType in
# ("audio", "video"); these two filters are its query-level equivalents and
# must stay consistent with it. The text filter spells out `eq null`
# explicitly on top of the `ne` clauses rather than relying on Azure AI
# Search's null-comparison semantics: ordinary KB documents have NO
# sourceType at all (never set by the Document Intelligence or native-text
# pipelines), so a filter that silently failed to match nulls would return
# zero text documents -- exactly the failure this change exists to prevent.
_TEXT_ONLY_FILTER = "(sourceType eq null or (sourceType ne 'audio' and sourceType ne 'video'))"
_MEDIA_ONLY_FILTER = "(sourceType eq 'audio' or sourceType eq 'video')"


# Design note (2026-09-20, Jalon 7 follow-up -- richer call summaries): live
# use after the Jalon 7 PII backfill showed primary_safe_summary/
# annex_safe_summaries were too generic to be useful ("decrit une procedure
# de reinitialisation...") even when the underlying call had concrete,
# actionable detail -- per Yassine. The instruction now explicitly asks for
# a detailed, concrete account of the call's actions/steps/result (a mini
# resolution procedure), while keeping the strict, unchanged prohibition on
# ever reproducing a spelled-out password/code/identifier verbatim -- only
# that specific category of information stays abstracted; everything else
# about the call can now be described concretely. See also the
# _looks_sensitive() backstop below, unchanged.
SYSTEM_PROMPT = (
    "Tu es l'assistant de support IT du client. Construis UNE SEULE reponse "
    "integree et coherente, basee UNIQUEMENT sur le CONTEXTE fourni. Le "
    "CONTEXTE te donne d'abord une ou plusieurs SOURCES PRIMAIRES numerotees "
    "(des documents de base de connaissances officiels, les SEULS autorises a "
    "fournir la procedure de base), puis des SOURCES ANNEXES numerotees "
    "(souvent des transcriptions d'appels support -- utiles mais parfois "
    "incompletes, un agent peut avoir oublie une etape).\n\n"
    "CHOIX DE LA SOURCE PRIMAIRE -- les SOURCES PRIMAIRES sont classees par un "
    "moteur de recherche, pas par comprehension de la question : la premiere "
    "n'est PAS forcement la bonne. Lis-les toutes et choisis celle qui traite "
    "reellement du sujet exact de la question. Indique son numero (1 pour la "
    "premiere du CONTEXTE) dans primary_source_index, et mets true dans "
    "primary_sources_used pour chaque SOURCE PRIMAIRE ayant reellement "
    "contribue a la reponse (au minimum celle que tu as choisie), false pour "
    "les autres -- une fiche qui traite d'un sujet voisin mais different doit "
    "rester a false et ne doit surtout pas etre utilisee. Si AUCUNE des "
    "SOURCES PRIMAIRES ne traite le sujet de la question, mets "
    "primary_source_index a null et ambiguous a true avec la raison, plutot "
    "que de repondre a partir d'une fiche hors sujet ou d'une annexe seule.\n\n"
    "REDACTION -- la procedure de base vient TOUJOURS de la SOURCE PRIMAIRE "
    "choisie, reprise FIDELEMENT : memes etapes, meme ordre, meme formulation "
    "autant que possible -- ne la resume pas au point de perdre une etape ou "
    "un detail, ne la remplace jamais par le contenu d'une annexe, et "
    "n'invente pas de contradiction entre les deux. Enrichis-la en revanche "
    "activement avec les details complementaires et reellement utiles trouves "
    "dans les SOURCES ANNEXES (une precision, une astuce pratique, une etape "
    "alternative mentionnee dans un appel) -- fusionne semantiquement cette "
    "information plutot que de simplement juxtaposer ou citer chaque source "
    "separement, mais SANS jamais alterer, raccourcir ou reformuler "
    "substantiellement la procedure de la SOURCE PRIMAIRE elle-meme. Pour "
    "related_sources_used, renvoie un booleen par SOURCE ANNEXE, dans le MEME "
    "ORDRE que le CONTEXTE -- true seulement si cette annexe a reellement "
    "contribue a la reponse finale (ne retape jamais les titres, ils sont deja "
    "connus). Si l'information ne se trouve pas dans le CONTEXTE, ou si la "
    "question est ambigue, dis-le explicitement (ambiguous=true, avec la "
    "raison) plutot que d'inventer une reponse.\n\n"
    "CONFIDENTIALITE DES APPELS -- une SOURCE (primaire ou annexe) qui est un "
    "appel audio/video contient parfois un extrait ou l'agent fait epeler au "
    "client un mot de passe, un code ou un identifiant lettre par lettre ou "
    "chiffre par chiffre : cette valeur precise ne doit JAMAIS apparaitre, "
    "meme partiellement ou reformulee, nulle part dans ta reponse -- decris ce "
    "fait en termes generaux (par exemple : le technicien a fait epeler un "
    "nouveau mot de passe au client, sans jamais repeter lequel). EN DEHORS de "
    "cette regle stricte sur les identifiants/mots de passe/codes epeles, "
    "redige pour chaque SOURCE de type appel reellement utilisee (primaire ou "
    "annexe) un resume AUSSI CONCRET ET DETAILLE que possible du deroulement "
    "de l'appel : les actions effectuees, les etapes suivies dans l'ordre, les "
    "verifications faites et le resultat obtenu -- comme une mini procedure de "
    "resolution, jamais juste une phrase vague, mais toujours dans tes propres "
    "mots (jamais une citation verbatim du transcript). Mets ces resumes dans "
    "primary_safe_summaries (un par SOURCE PRIMAIRE, MEME ORDRE que le "
    "CONTEXTE, null pour une primaire texte ou non utilisee) et dans "
    "annex_safe_summaries (un par SOURCE ANNEXE, MEME ORDRE que le CONTEXTE, "
    "null pour une annexe texte ou non utilisee)."
)

# Strict JSON Schema for Structured Outputs -- Azure/OpenAI does not prescribe
# field names, only the enforcement mechanism. Deliberately does NOT include
# source titles: those are already known deterministically from retrieval: the
# model only reports which ones it used (booleans), titles are filled in by
# this script afterwards (see module docstring, 2026-09-10 note).
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "Reponse a la question, basee uniquement sur le CONTEXTE fourni.",
        },
        "primary_source_index": {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "description": (
                "Numero (1 = la premiere du CONTEXTE) de la SOURCE PRIMAIRE retenue "
                "comme base de la reponse : celle qui traite reellement du sujet de la "
                "question, PAS forcement la premiere. null si aucune ne le traite "
                "(alors ambiguous=true)."
            ),
        },
        "primary_sources_used": {
            "type": "array",
            "items": {"type": "boolean"},
            "description": (
                "Un booleen par SOURCE PRIMAIRE, dans le meme ordre que le CONTEXTE. "
                "true pour celles ayant reellement contribue a la reponse (au minimum "
                "celle de primary_source_index), false pour une fiche hors sujet."
            ),
        },
        "related_sources_used": {
            "type": "array",
            "items": {"type": "boolean"},
            "description": (
                "Un booleen par SOURCE ANNEXE, dans le meme ordre que le CONTEXTE. "
                "Tableau vide si aucune annexe fournie ou utilisee."
            ),
        },
        "ambiguous": {
            "type": "boolean",
            "description": "true si la question ne peut pas etre repondue de facon fiable a partir du CONTEXTE.",
        },
        "unanswerable_reason": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Si ambiguous=true, pourquoi. Sinon null.",
        },
        "primary_safe_summaries": {
            "type": "array",
            "items": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "description": (
                "Un resume DETAILLE (actions, etapes, resultat) par SOURCE PRIMAIRE de "
                "type appel audio/video reellement utilisee, MEME ORDRE que le CONTEXTE, "
                "sans jamais reproduire verbatim un mot de passe/code/identifiant epele. "
                "null pour une SOURCE PRIMAIRE texte (KB) ou non utilisee -- une primaire "
                "audio/video est un cas exceptionnel : aucun document KB ne couvrait la "
                "question."
            ),
        },
        "annex_safe_summaries": {
            "type": "array",
            "items": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "description": (
                "Un resume DETAILLE (meme regle que primary_safe_summaries : jamais de mot "
                "de passe/code/identifiant epele verbatim) par SOURCE ANNEXE de type appel "
                "audio/video reellement utilisee, MEME ORDRE que le CONTEXTE. Null pour "
                "toute annexe texte (KB) ou non utilisee."
            ),
        },
    },
    "required": [
        "answer",
        "primary_source_index",
        "primary_sources_used",
        "related_sources_used",
        "ambiguous",
        "unanswerable_reason",
        "primary_safe_summaries",
        "annex_safe_summaries",
    ],
    "additionalProperties": False,
}


# =====================================================================
# Design note (2026-09-24, Jalon 9 -- diagnostic screenshot upload, layer 1
# of the iterative-diagnostic feature): an agent can attach a screenshot of
# what they see on the client's machine to their question. Deliberately a
# SIBLING of answer_query_core()/generate(), never calling into it and never
# called by it -- same retrieval (retrieve_hierarchy/select_primaries/
# format_context) but its own prompt (SCREENSHOT_SYSTEM_PROMPT) and schema
# (SCREENSHOT_ANSWER_SCHEMA, = ANSWER_SCHEMA plus screen_reading). Nothing
# in this block is imported or called by answer_query_core,
# answer_query_core_keyless, generate() or ANSWER_SCHEMA/SYSTEM_PROMPT --
# the existing single-shot path (CLI, eval/evaluate_rag.py, app.py's normal
# question flow) is byte-for-byte unaffected.
#
# Uses GPT-4o's own multimodal vision input (image_url content part with a
# base64 data URI) rather than a separate Azure AI Vision Read resource --
# one fewer Azure resource/auth surface to provision under time pressure,
# at the cost of Read's exact-character-offset guarantees. Backstopped by
# _ERROR_CODE_PATTERNS: the model is asked to transcribe visible text
# verbatim into screen_reading, and detected_error_codes is extracted from
# THAT text by regex after the call, never trusted as a separate model
# claim -- same spirit as _looks_sensitive() below (regex backstop on model
# output, not a replacement for the prompt instruction).
# =====================================================================
SCREENSHOT_SYSTEM_PROMPT = (
    "Tu es l'assistant de support IT du client, en session de diagnostic avec "
    "un agent service desk qui vient de joindre une capture d'ecran de ce "
    "qu'il voit sur le poste du client, avec sa question. Le CONTEXTE (extrait "
    "de la base de connaissances) reste la SEULE base autorisee pour la "
    "procedure -- memes regles que d'habitude : choisis explicitement la "
    "SOURCE PRIMAIRE qui traite reellement du sujet (primary_source_index), "
    "ambiguous=true si aucune ne correspond, jamais d'invention, jamais de "
    "resume qui deforme la procedure de la source primaire choisie.\n\n"
    "LECTURE DE LA CAPTURE -- avant de repondre, decris PRECISEMENT et "
    "LITTERALEMENT ce que tu lis sur l'image dans screen_reading : texte "
    "visible (messages d'erreur, codes, numeros), nom de la fenetre ou de "
    "l'application si identifiable. Ne l'invente jamais, ne le devine pas -- "
    "si l'image ne contient aucun texte exploitable ou n'est pas lisible, "
    "ecris exactement 'aucun texte lisible sur cette image'. EXCEPTION "
    "STRICTE -- si un mot de passe, code ou identifiant PRECIS est visible "
    "sur la capture, ne le recopie JAMAIS tel quel dans screen_reading -- "
    "decris ce fait en termes generaux uniquement. Utilise ensuite cette "
    "lecture pour relier le probleme observe a la bonne procedure du "
    "CONTEXTE, exactement comme si l'agent avait decrit ce texte dans sa "
    "question."
)

SCREENSHOT_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        **ANSWER_SCHEMA["properties"],
        "screen_reading": {
            "type": "string",
            "description": (
                "Description precise et litterale de ce qui est lisible sur la "
                "capture d'ecran jointe (texte, code d'erreur, nom de fenetre). "
                "'aucun texte lisible sur cette image' si rien d'exploitable."
            ),
        },
    },
    "required": ANSWER_SCHEMA["required"] + ["screen_reading"],
    "additionalProperties": False,
}

_ERROR_CODE_PATTERNS = re.compile(
    r"\b\d{4}\b|0x[0-9A-Fa-f]{6,8}|Event ID \d+", re.IGNORECASE
)

# Design note (2026-09-25 -- backstop for a live-tested leak, layer 1+2): a
# real diagnostic-conversation test on client-s showed the model can still
# transcribe a password verbatim into screen_reading despite the prompt
# instruction above (a password-reset tool's generated temporary password,
# visible as plain text on the uploaded screenshot, was copied
# character-for-character: "Mot de passe temporaire: NEKA18023526_mdp").
# The existing CONFIDENTIALITE DES APPELS rule only ever covered a password
# SPELLED OUT in a call transcript -- nothing covered a password simply
# VISIBLE as text on a screenshot. Same "prompt instruction first, regex
# backstop second" posture as _looks_sensitive()/_SPELLING_MARKERS/_DIGIT_RUN
# below (which cover the call case) -- narrow and pattern-specific, not a
# general secrets scanner, but it catches the exact shape just observed.
_VISIBLE_PASSWORD_PATTERN = re.compile(
    r"(mot de passe(?:\s+temporaire)?|password|mdp|pwd)\s*[:=]\s*"
    r"([A-Za-z0-9_@#.\-]{6,})",
    re.IGNORECASE,
)


def _redact_visible_password(text: Optional[str]) -> Optional[str]:
    """Backstop for screen_reading -- see design note above. Replaces the
    captured value after a 'mot de passe : ...'/'password: ...' style label
    with a fixed placeholder, leaves everything else untouched."""
    if not text:
        return text
    return _VISIBLE_PASSWORD_PATTERN.sub(
        lambda m: f"{m.group(1)} : [valeur masquee]", text
    )



# =====================================================================
# Design note (2026-09-24, later same day -- diagnostic layer 2: persistent
# multi-turn state). Layer 1 above answers every question in isolation --
# app/app.py persisted each turn for DISPLAY only, never fed prior turns
# back into generation, so a follow-up question had no memory of what was
# already served or already tried in the SAME conversation. This closes
# that gap WITHOUT a second state store: the state is derived
# deterministically from the turns app/app.py already persists (see
# _build_diagnostic_state_block) -- same "reproductible depuis la source,
# jamais un state parallele" posture as _detect_query_entities() above, no
# new Azure resource, no new table. Retrieval is UNCHANGED (still grounded
# in the current question only, like every other path here) -- only
# generation gains an ETAT DU DIAGNOSTIC block. DIAGNOSTIC_ANSWER_SCHEMA
# unifies the plain and screenshot cases (screen_reading is always present,
# reading 'aucune capture fournie a ce tour' when no image was attached
# THIS turn) so app/app.py has a single call for every turn of a
# conversation -- see diagnostic_query_core_keyless below, which replaces
# BOTH answer_query_core_keyless and analyze_screenshot_query_core_keyless
# in app/app.py. Those two, and the plain generate()/ANSWER_SCHEMA path,
# are untouched -- CLI/eval/evaluate_rag.py stay byte-for-byte unaffected
# (module docstring, 2026-09-11 note).
# =====================================================================
DIAGNOSTIC_SYSTEM_PROMPT = (
    "Tu es l'assistant de support IT du client, en session de diagnostic "
    "iterative avec un agent service desk : la conversation peut compter "
    "plusieurs tours, chacun affinant le precedent. Le CONTEXTE (extrait de "
    "la base de connaissances) reste la SEULE base autorisee pour la "
    "procedure -- memes regles que d'habitude : choisis explicitement la "
    "SOURCE PRIMAIRE qui traite reellement du sujet (primary_source_index), "
    "ambiguous=true si aucune ne correspond, jamais d'invention.\n\n"
    "REDACTION ET CONFIDENTIALITE -- memes regles strictes que d'habitude : "
    "reprends fidelement la procedure de la SOURCE PRIMAIRE, enrichis-la des "
    "SOURCES ANNEXES pertinentes, et si une SOURCE est un appel qui contient "
    "un mot de passe/code/identifiant epele, cette valeur ne doit JAMAIS "
    "apparaitre dans ta reponse ni dans les resumes -- decris le fait en "
    "termes generaux uniquement.\n\n"
    "LECTURE DE CAPTURE D'ECRAN -- si une image est jointe A CE TOUR, decris "
    "PRECISEMENT et LITTERALEMENT ce qui y est lisible dans screen_reading "
    "(texte, code d'erreur, nom de fenetre) ; si elle n'est pas lisible, "
    "ecris exactement 'aucun texte lisible sur cette image'. Si AUCUNE image "
    "n'est jointe a ce tour, ecris exactement 'aucune capture fournie a ce "
    "tour' -- ne decris jamais une capture d'un tour precedent comme si elle "
    "etait nouvelle. EXCEPTION STRICTE -- si un mot de passe, code ou "
    "identifiant PRECIS est visible sur la capture (par exemple un mot de "
    "passe temporaire genere par un outil de reinitialisation), ne le "
    "recopie JAMAIS tel quel dans screen_reading ni ailleurs -- decris ce "
    "fait en termes generaux uniquement (ex: 'un mot de passe temporaire "
    "est visible mais non retranscrit ici'), exactement comme pour un mot "
    "de passe epele dans un appel.\n\n"
    "SUIVI MULTI-TOURS -- si un bloc ETAT DU DIAGNOSTIC est fourni ci-dessous, "
    "il resume ce qui a deja ete etabli dans cette conversation (fiches deja "
    "servies, codes d'erreur deja releves, entites deja identifiees, tours "
    "precedents). Utilise-le pour CONTINUER le diagnostic, pas pour repartir "
    "de zero : ne reproduis jamais une reponse deja donnee a l'identique, ne "
    "repropose jamais une fiche deja servie sauf si un element nouveau "
    "(capture, precision de l'agent) la rend a nouveau pertinente, et ne "
    "repropose jamais une etape que l'agent a explicitement signalee comme "
    "deja tentee sans succes dans un tour precedent -- passe a l'etape "
    "suivante ou recommande l'escalade. Termine par UNE verification "
    "concrete a demander avant le prochain tour (prochaine_verification), "
    "vide si aucune n'est necessaire. Mets diagnostic_termine=true seulement "
    "si le probleme semble resolu ou si le point d'escalade recommande dans "
    "ta reponse est atteint."
)

DIAGNOSTIC_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        **ANSWER_SCHEMA["properties"],
        "screen_reading": {
            "type": "string",
            "description": (
                "Description precise et litterale de ce qui est lisible sur la "
                "capture d'ecran jointe A CE TOUR. Exactement 'aucune capture "
                "fournie a ce tour' si aucune image n'est jointe cette fois-ci, "
                "'aucun texte lisible sur cette image' si l'image jointe n'a "
                "rien d'exploitable."
            ),
        },
        "prochaine_verification": {
            "type": "string",
            "description": (
                "Action ou verification concrete a demander a l'agent avant le "
                "prochain tour (ex: 'Demande au client si le mode hors ligne "
                "est actif'). Chaine vide si aucune verification n'est "
                "necessaire ou si diagnostic_termine=true."
            ),
        },
        "diagnostic_termine": {
            "type": "boolean",
            "description": (
                "true si le probleme semble resolu par cette reponse, ou si le "
                "point d'escalade recommande est atteint (plus rien a "
                "diagnostiquer cote assistant). false tant que le diagnostic "
                "peut continuer."
            ),
        },
    },
    "required": ANSWER_SCHEMA["required"]
    + ["screen_reading", "prochaine_verification", "diagnostic_termine"],
    "additionalProperties": False,
}


# Defense-in-depth backstop for primary_safe_summaries / annex_safe_summaries
# (2026-09-19): the SYSTEM_PROMPT instructs the model to never quote a
# dictated credential verbatim in these summaries, but an instruction can
# fail -- especially since the model already has the raw spelled-out text
# in its own context window. This catches the two textual signatures of
# character-by-character dictation actually seen in a live transcript
# (repeated minuscule/majuscule/epelle phrasing -- the French convention
# for spelling over the phone, e.g. "la lettre T comme Thierry" -- or a
# long run of space-separated single digits) and SUPPRESSES the summary
# entirely rather than trying to further redact text that already looks
# unreliable. This is a backstop, not the primary control -- the prompt
# instruction is what should catch it first.
_SPELLING_MARKERS = re.compile(
    r"\b(minuscule|majuscule|en capitale|j'epelle|je vous epelle|epelle|"
    r"lettre par lettre|chiffre par chiffre|comme dans le mot)\b",
    re.IGNORECASE,
)
_DIGIT_RUN = re.compile(r"(?:\b\d\b[\s,.-]+){4,}\d\b")


def _looks_sensitive(text: Optional[str]) -> bool:
    if not text:
        return False
    if len(_SPELLING_MARKERS.findall(text)) >= 2:
        return True
    if _DIGIT_RUN.search(text):
        return True
    return False


def az(cmd: str) -> str:
    """Run an az command and return stdout (text)."""
    out = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    if out.returncode != 0:
        raise RuntimeError(f"az command failed: {cmd}\n{out.stderr}")
    return out.stdout.strip()


def load_engine_config(client_id: str) -> dict:
    """Axiom A2: all client specifics come from engine.<client>.yaml -- this
    module never hardcodes a client."""
    for d in CONFIG_DIRS:
        p = d / f"engine.{client_id}.yaml"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return yaml.safe_load(f)
    searched = ", ".join(str(d) for d in CONFIG_DIRS)
    raise FileNotFoundError(f"No engine.{client_id}.yaml found in: {searched}")


# Design note (2026-09-21, Jalon 7 follow-up -- exhaustive KNN does not
# scale for free): exhaustive=true (added above) fixes the run-to-run
# candidate-set flicker from approximate HNSW search, but it costs a
# roughly linear scan of the index's vector field -- fine at client-s's
# current scale (~150 chunks) but not a safe blanket default: axiom A2
# (client-agnosticism) means a future client's corpus size is unknown at
# onboarding time, and each client has its own dedicated index (M2), so
# the real cost driver is THAT client's own chunk count, nothing else.
# _use_exhaustive_knn() checks the index's actual document ($count) --
# not a config guess -- and only asks for exhaustive search below a
# threshold; a bigger client automatically falls back to approximate HNSW
# (accepting the documented flicker there) rather than risking latency.
# Cached per index for _EXHAUSTIVE_KNN_CACHE_TTL_S: doc counts change
# slowly relative to a process's lifetime, and a redeploy resets the
# cache anyway; on any error (count endpoint unreachable, bad response)
# this fails OPEN toward exact search -- the safer default when the
# count itself is unknown.
_EXHAUSTIVE_KNN_MAX_DOCS = 5000
_EXHAUSTIVE_KNN_CACHE_TTL_S = 3600
_exhaustive_knn_cache: Dict[str, Tuple[bool, float]] = {}


def _use_exhaustive_knn(index: str, headers: Dict[str, str]) -> bool:
    cached = _exhaustive_knn_cache.get(index)
    if cached and (time.time() - cached[1]) < _EXHAUSTIVE_KNN_CACHE_TTL_S:
        return cached[0]
    try:
        r = requests.get(
            f"{SEARCH_ENDPOINT}/indexes/{index}/docs/$count?api-version={SEARCH_API_VERSION}",
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        result = int(r.text) <= _EXHAUSTIVE_KNN_MAX_DOCS
    except Exception:
        result = True
    _exhaustive_knn_cache[index] = (result, time.time())
    return result


# Design note (2026-09-24, Jalon 9): query-time entity boost. Azure AI
# Search's "entity-boost" scoringProfile (search/index.template.json) only
# fires when the request supplies a `queryEntities` scoringParameter -- it
# does nothing on its own. The entities it should boost are detected here by
# plain substring comparison against the client's OWN already-indexed
# vocabulary (the `entities` facet, same technique
# enrichment/function_app.py::_client_vocabulary() already uses server-side
# for call enrichment) -- no model call, deterministic, and it can never
# boost a term that isn't already a verified entity somewhere in that
# client's own corpus.
_ENTITY_VOCAB_CACHE_TTL_S = 600
_entity_vocab_cache: Dict[str, Tuple[Dict[str, str], float]] = {}


def _norm_entity(text: str) -> str:
    """Lowercase, accent-stripped, whitespace-collapsed -- mirrors
    enrichment/function_app.py::_norm() so a query and an indexed entity
    normalize identically regardless of accents/case."""
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", text).strip().lower()


def _client_entity_vocabulary(
    index: str, headers: Dict[str, str], client_id: Optional[str]
) -> Dict[str, str]:
    """{normalized entity: canonical form}, read from the client's own index
    (facet on `entities`, populated by the enrichment skill). Cached briefly
    per index -- this changes only after a reindex, never within a single
    conversation. On any error, fails open to an empty vocabulary: entity
    boost is an enhancement, never a reason to fail a query."""
    cached = _entity_vocab_cache.get(index)
    if cached and (time.time() - cached[1]) < _ENTITY_VOCAB_CACHE_TTL_S:
        return cached[0]
    vocab: Dict[str, str] = {}
    try:
        body = {"search": "*", "top": 0, "facets": ["entities,count:1000"]}
        if client_id:
            body["filter"] = f"clientId eq '{client_id.replace(chr(39), chr(39) * 2)}'"
        r = requests.post(
            f"{SEARCH_ENDPOINT}/indexes/{index}/docs/search?api-version={SEARCH_API_VERSION}",
            headers={**headers, "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
        r.raise_for_status()
        for facet in r.json().get("@search.facets", {}).get("entities", []):
            value = facet.get("value")
            if value:
                vocab[_norm_entity(value)] = value
    except Exception:
        pass
    _entity_vocab_cache[index] = (vocab, time.time())
    return vocab


def _detect_query_entities(query: str, vocab: Dict[str, str]) -> List[str]:
    """Entities from the client's controlled vocabulary that literally
    appear in the query (substring match on normalized text, longest terms
    checked first so a short entity can't spuriously pre-empt a longer one
    that contains it). Returns canonical forms, fed to the `queryEntities`
    scoringParameter -- this can never introduce a term absent from the
    client's own already-verified vocabulary."""
    norm_query = _norm_entity(query)
    if not norm_query or not vocab:
        return []
    found = []
    for norm_e, canonical in sorted(vocab.items(), key=lambda kv: -len(kv[0])):
        if len(norm_e) >= 2 and norm_e in norm_query:
            found.append(canonical)
    return found


def _search_request(
    query: str,
    index: str,
    top: int,
    headers: Dict[str, str],
    client_id: Optional[str] = None,
    modality: Optional[str] = None,
    scoring_profile: Optional[str] = None,
    scoring_parameters: Optional[List[str]] = None,
) -> List[Dict]:
    """Raw Azure AI Search hybrid+semantic query -- same request body
    regardless of auth mechanism, only the headers differ (api-key vs a
    Bearer token). Returns docs sorted by @search.rerankerScore (desc), the
    real Azure-native ranking signal used for axiom A3. Shared by retrieve()
    (api-key, Jalon 3) and answer_query_core_keyless() (RBAC, Jalon 4).

    client_id (Jalon 5): when given, adds an explicit `filter: clientId eq
    '<client_id>'` -- defense in depth on top of the physical per-client
    index isolation (idx-<client>) already in place since Jalon 2. The
    clientId field has been projected onto every document since the
    skillset was built (search/skillset.template.json) but was never
    actually queried against until now. Single quotes are doubled (OData
    escaping) even though callers only ever pass our own known client ids,
    never raw user input."""
    body = {
        "search": query,
        # Design note (2026-09-21, Jalon 7 follow-up -- non-deterministic
        # candidate set): without "exhaustive", Azure AI Search runs
        # approximate nearest-neighbor (HNSW) vector search -- graph
        # traversal is approximate, so a borderline document can be IN or
        # OUT of the candidate set on two calls of the identical query,
        # even though its @search.rerankerScore is bit-identical once it IS
        # returned (confirmed live on client-s: KB0267/KB0341 present with
        # identical scores on 2/3 repeated identical calls, entirely absent
        # on the 3rd). That silently starves split_hierarchy()'s
        # text-over-audio preference even with a wide candidate pool
        # (_MIN_CANDIDATE_POOL) -- axiom A1 (determinism) covered only the
        # GPT-4o generation step (temp=0, seed), never retrieval, until now.
        # exhaustive=true forces exact (not approximate) nearest-neighbor
        # search -- negligible latency cost at this corpus size (~150
        # docs/client), removes this source of run-to-run flicker.
        "vectorQueries": [
            {
                "kind": "text",
                "text": query,
                "fields": "text_vector",
                "k": top,
                "exhaustive": _use_exhaustive_knn(index, headers),
            }
        ],
        "queryType": "semantic",
        "semanticConfiguration": "sem-config",
        "select": "title,chunk,sourceType,parent_id,chunk_id",
        "top": top,
    }
    if scoring_profile:
        body["scoringProfile"] = scoring_profile
        if scoring_parameters:
            body["scoringParameters"] = scoring_parameters
    filters = []
    if client_id:
        filters.append(f"clientId eq '{client_id.replace(chr(39), chr(39) * 2)}'")
    if modality == "text":
        filters.append(_TEXT_ONLY_FILTER)
    elif modality == "media":
        filters.append(_MEDIA_ONLY_FILTER)
    if filters:
        body["filter"] = " and ".join(filters)
    r = requests.post(
        f"{SEARCH_ENDPOINT}/indexes/{index}/docs/search?api-version={SEARCH_API_VERSION}",
        headers={**headers, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    if not r.ok:
        # Design note (2026-09-21): raise_for_status() alone discards the
        # response body, which is where Azure AI Search actually explains a
        # 400 (bad filter, bad vectorQueries.k, bad semanticConfiguration,
        # etc.) -- surface it instead of an opaque HTTPError.
        raise RuntimeError(
            f"Azure AI Search {r.status_code} on index '{index}': {r.text}"
        )
    docs = r.json().get("value", [])
    return sorted(docs, key=lambda d: d.get("@search.rerankerScore", 0), reverse=True)


def retrieve(
    query: str, index: str, top: int, search_key: str, client_id: Optional[str] = None
) -> List[Dict]:
    """Hybrid + semantic search, api-key auth. Unchanged contract from Jalon 3.
    No longer used by answer_query_core(), which goes through
    retrieve_hierarchy() (2026-09-21) -- kept as the single-search helper
    for ad-hoc/CLI use. See
    _search_request() for the shared request logic (incl. the optional
    clientId filter added in Jalon 5) and answer_query_core_keyless() for
    the RBAC-based alternative (Jalon 4)."""
    return _search_request(query, index, top, {"api-key": search_key}, client_id=client_id)


def _is_media(doc: Dict) -> bool:
    """True for an audio/video-derived document (sourceType projected from
    blob metadata by the -text skillset -- see search/skillset.template.json
    and ingestion/audio-transcribe/workflow-definition.json). Absent/None for
    every ordinary KB document (Document Intelligence and native-text
    pipelines alike never set it)."""
    return doc.get("sourceType") in ("audio", "video")


# Design note (2026-09-21, Jalon 7 follow-up -- un fragment ne fait pas une
# procedure): retrieval returns ONE chunk per hit, and for a KB document
# split into several chunks that chunk is often not the one carrying the
# procedure. Measured on client-s: KB0341 (SSPR) has 3 chunks -- a cover
# page, a GENERAL/OBJET/PREREQUISITES preamble, and the actual procedure
# (1133 chars, the largest). The engine retrieved the PREAMBLE, so it
# answered the password-reset question from the document's front matter
# while the steps sat in a chunk it never saw -- which is what made the
# answer read as vague next to a comparable production tool, far more than
# any ranking issue. A KB document here is ~1.7 kB in total, so once one is
# a PRIMARY candidate there is no reason to show the model a fragment of
# it: _expand_to_full_documents() refetches every chunk of that document
# and hands over the whole text. Applied to PRIMARY candidates only, never
# to call transcripts: a call is long, repetitive, and only the matched
# passage is relevant, whereas a primary is meant to be reproduced
# faithfully and must therefore be complete.
_MAX_EXPANDED_CHARS = 20000


def _chunk_order(doc: Dict) -> int:
    """Chunks are keyed <hash>_<base64 blob url>_text_sections_<n>. Sort on n
    NUMERICALLY: lexicographic order would put _10 before _2 and silently
    scramble the steps of any document with more than ten chunks."""
    m = re.search(r"_(\d+)$", doc.get("chunk_id") or "")
    return int(m.group(1)) if m else 0


def _fetch_document_chunks(
    parent_ids: List[str],
    index: str,
    headers: Dict[str, str],
    client_id: Optional[str] = None,
) -> Dict[str, List[Dict]]:
    """Every chunk of the given source documents, grouped by parent_id and in
    document order. One filter-only request for all of them (no search terms,
    no vector query: this is a fetch by key, not a relevance question)."""
    filters = ["search.in(parent_id, '" + "|".join(parent_ids) + "', '|')"]
    if client_id:
        filters.append(f"clientId eq '{client_id.replace(chr(39), chr(39) * 2)}'")
    r = requests.post(
        f"{SEARCH_ENDPOINT}/indexes/{index}/docs/search?api-version={SEARCH_API_VERSION}",
        headers={**headers, "Content-Type": "application/json"},
        json={
            "search": "*",
            "select": "chunk_id,parent_id,title,chunk,sourceType",
            "filter": " and ".join(filters),
            "top": 1000,
        },
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(
            f"Azure AI Search {r.status_code} on index '{index}': {r.text}"
        )
    grouped: Dict[str, List[Dict]] = {}
    for d in r.json().get("value", []):
        grouped.setdefault(d.get("parent_id"), []).append(d)
    for chunks in grouped.values():
        chunks.sort(key=_chunk_order)
    return grouped


def _expand_to_full_documents(
    docs: List[Dict],
    index: str,
    headers: Dict[str, str],
    client_id: Optional[str] = None,
) -> List[Dict]:
    """Replace each doc's matched chunk with its FULL source document text.
    Everything else about the doc is preserved -- notably its
    @search.rerankerScore, which stays the score of the chunk that actually
    matched (the ranking decision was made on that chunk and must not be
    rewritten after the fact). Degrades to the original chunks on any error:
    a richer context is an enrichment, never a reason to fail an answer."""
    parent_ids = []
    for d in docs:
        pid = d.get("parent_id")
        if pid and pid not in parent_ids:
            parent_ids.append(pid)
    if not parent_ids:
        return docs
    try:
        grouped = _fetch_document_chunks(parent_ids, index, headers, client_id=client_id)
    except Exception:
        return docs

    expanded = []
    for d in docs:
        chunks = grouped.get(d.get("parent_id")) or []
        if len(chunks) <= 1:
            expanded.append(d)
            continue
        text = "\n".join(ch.get("chunk") or "" for ch in chunks)[:_MAX_EXPANDED_CHARS]
        merged = dict(d)
        merged["chunk"] = text
        expanded.append(merged)
    return expanded


def retrieve_hierarchy(
    query: str,
    index: str,
    primary_count: int,
    annex_count: int,
    headers: Dict[str, str],
    client_id: Optional[str] = None,
) -> Tuple[List[Dict], List[Dict]]:
    """Axiom A3 hierarchy, built from TWO modality-filtered searches instead
    of one shared ranked list (2026-09-21 -- replaces split_hierarchy(); see
    the _RERANK_CEILING design note for why the previous rank-based split
    and its widening candidate pool were abandoned).

    Search 1 is restricted to text KB documents, search 2 to audio/video.
    The PRIMARY candidates are the top primary_count results of search 1, so
    they are BY CONSTRUCTION official KB documents -- the size of the
    transcribed-call corpus cannot influence them at all, because calls never
    compete in that search. Several candidates are returned rather than one
    (2026-09-21) because the reranker orders KB documents by search relevance,
    not by understanding the question: measured on client-s, "Comment
    reinitialiser mon mot de passe ?" ranks KB0050 (One Time Password) and
    KB0267 (resetting an iPhone) ABOVE KB0341 (SSPR password reset), the only
    one that actually answers it. Picking which candidate answers the question
    is a judgement the model makes well and a reranker score does not, so the
    model reports it via primary_source_index -- the determinism guarantee is
    on the MODALITY (the base procedure always comes from an official KB
    document, never from a call), not on trusting the ranking blindly. Annex slots are filled from
    search 2 first ("audio/video always mentioned as secondary", per
    Yassine, now guaranteed rather than rank-dependent), and any slot left
    over is filled with the next-best text documents -- so a client with no
    audio at all (the synthetic demo clients) still gets text annexes and
    behaves exactly as before.

    Both searches ask for _RERANK_CEILING results: past 50 the semantic
    ranker stops reranking, and below it a smaller k would narrow the
    hybrid RRF fusion feeding the reranker (measured on client-s: k=12 and
    k=30 return genuinely different candidates, not one truncation of the
    other). Only primary_count + annex_count of them are ever used.

    Falls back to the top audio/video result as PRIMARY only when the text
    search returns nothing at all -- the exceptional "no KB document covers
    this question" case that primary_safe_summaries exists for."""
    # Design note (2026-09-24, Jalon 9): entity-boost is additive, not a
    # replacement for the modality split above -- it only re-weights WITHIN
    # each already-filtered search (see _search_request's scoringProfile),
    # so it cannot make a call/video outrank a text KB document or vice
    # versa. Detected once and reused for both searches: the same query
    # entities are equally valid signals in either modality.
    query_entities = _detect_query_entities(
        query, _client_entity_vocabulary(index, headers, client_id)
    )
    scoring_kwargs = (
        {
            "scoring_profile": "entity-boost",
            "scoring_parameters": ["queryEntities-" + ",".join(query_entities)],
        }
        if query_entities
        else {}
    )
    text_docs = _search_request(
        query, index, _RERANK_CEILING, headers, client_id=client_id, modality="text", **scoring_kwargs
    )
    media_docs = _search_request(
        query, index, _RERANK_CEILING, headers, client_id=client_id, modality="media", **scoring_kwargs
    )

    if primary_count < 1:
        return [], (media_docs + text_docs)[:annex_count]

    if text_docs:
        unique_text = _dedupe_by_document(text_docs)
        # candidats pour la selection (large) vs. annexes texte de repli
        # (inchangees : elles comblent les creneaux au-dela de primaryCount
        # quand il n'y a pas assez d'audio -- cas des clients demo).
        primaries, rest_text = unique_text[:_SELECTION_CANDIDATES], unique_text[primary_count:]
    elif media_docs:
        # No text KB document matches at all -- exceptional. ONE media primary
        # only, never primary_count of them: this is the degraded "aucun
        # document KB ne couvre la question" case the prompt handles, not an
        # invitation to assemble a procedure out of several call transcripts.
        primaries, rest_text = media_docs[:1], []
        media_docs = media_docs[1:]
    else:
        return [], []

    annexes = media_docs[:annex_count]
    if len(annexes) < annex_count:
        annexes += rest_text[: annex_count - len(annexes)]
    return primaries, annexes


def format_context(primaries: List[Dict], annexes: List[Dict]) -> str:
    parts = []
    for i, p in enumerate(primaries, start=1):
        score = p.get("@search.rerankerScore", 0)
        parts.append(
            f"[SOURCE PRIMAIRE {i} — {p.get('title', '')} — score={score:.2f}]\n"
            f"{p.get('chunk', '')}"
        )
    for i, a in enumerate(annexes, start=1):
        score = a.get("@search.rerankerScore", 0)
        parts.append(
            f"[SOURCE ANNEXE {i} — {a.get('title', '')} — score={score:.2f}]\n"
            f"{a.get('chunk', '')}"
        )
    return "\n\n".join(parts) if parts else "(aucune source pertinente trouvee)"


# Design note (2026-09-22, Jalon 7 follow-up -- selectionner AVANT d'expanser):
# expanding every PRIMARY candidate to its full document (2026-09-21) was the
# wrong order of operations and is corrected here. Measured on client-s: the
# three candidates expanded to 15 830 / 2 309 / 1 757 characters -- KB0050
# (One Time Password, off topic) alone took 65% of the context, ahead of
# KB0341 (SSPR), the only one answering the question. The answer that came
# out told the user to go to a URL that exists ONLY in that off-topic
# document, while the real SSPR portal was nowhere in the context. Note that
# groundedness scored it 4.0 and saw nothing: it asks whether the answer is
# supported by the context, not whether it is supported by the RIGHT source
# in it -- a wrong-but-present URL is perfectly "grounded".
# So: pick the document FIRST (one cheap Structured Outputs call over the
# matched fragments -- proven to work: on 2026-09-21 the model picked KB0341
# correctly from fragments alone, against the ranking), THEN expand only the
# one that was picked. The final context holds a single, complete, on-topic
# KB document plus the call annexes, so off-topic material cannot bleed into
# the answer at all instead of merely being outranked in it.
# Combien de documents DISTINCTS la selection voit. Decouple de primaryCount
# (2026-09-22) : primaryCount plafonne ce qui entre dans le CONTEXTE, pas ce
# parmi quoi on choisit. Mesure sur client-s : KB0320 -- la fiche qui porte
# les cas utilisateur (Ctrl+Alt+Suppr, VPOD/UNITY) et que l'outil de
# production du client utilise -- arrive au rang 7 du classement texte. Avec
# une liste de candidats coupee a primaryCount=3, elle ne pouvait JAMAIS etre
# retenue, quelle que soit la qualite du jugement du modele. La deduplication
# par document compte autant : sans elle, deux chunks de KB0050 occupaient
# deux des trois places.
_SELECTION_CANDIDATES = 10
_SELECTION_EXCERPT_CHARS = 1200


def _dedupe_by_document(docs: List[Dict]) -> List[Dict]:
    """Une entree par document source (son chunk le mieux classe), pour que la
    selection voie dix documents differents et non dix morceaux de trois."""
    seen, out = set(), []
    for d in docs:
        key = d.get("parent_id") or d.get("title")
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out

_SELECTION_PROMPT = (
    "Tu es l'assistant de support IT du client. On te donne une question et "
    "plusieurs EXTRAITS de fiches de base de connaissances, numerotes. Ces "
    "extraits sont classes par un moteur de recherche, pas par comprehension "
    "de la question : le premier n'est PAS forcement le bon. Indique dans "
    "indexes les numeros de TOUTES les fiches necessaires pour repondre "
    "completement, la plus centrale en premier -- une procedure reelle est "
    "souvent repartie sur plusieurs fiches (le cas de l'utilisateur dans "
    "l'une, le lien du portail ou la procedure interne dans une autre), et "
    "n'en retenir qu'une donne une reponse incomplete. Mais ne retiens QUE "
    "celles qui traitent le sujet EXACT : une fiche portant sur un sujet "
    "voisin mais different (un autre produit, un autre type d'appareil, une "
    "autre procedure) doit etre exclue, meme si elle est bien classee. Si "
    "aucune ne traite le sujet, renvoie un tableau vide plutot que la moins "
    "mauvaise. Tu ne rediges pas de reponse : tu choisis, et tu justifies en "
    "une phrase."
)

_SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "indexes": {
            "type": "array",
            "items": {"type": "integer"},
            "description": (
                "Numeros (1 = premier extrait) de toutes les fiches necessaires pour "
                "repondre completement, la plus centrale en premier. Tableau vide si "
                "aucune ne traite le sujet de la question."
            ),
        },
        "reason": {
            "type": "string",
            "description": "Une phrase : pourquoi cette fiche, ou pourquoi aucune.",
        },
    },
    "required": ["indexes", "reason"],
    "additionalProperties": False,
}


def select_primaries(
    query: str,
    candidates: List[Dict],
    client: AzureOpenAI,
    model: str,
    seed: int,
) -> Tuple[List[int], str]:
    """Which candidate KB documents actually cover the question (0-based
    indexes, most central first), empty when none does. Judged on the matched
    fragments only -- cheap, and enough: this is a topic-matching decision,
    not a reading of the whole procedure.

    Returns SEVERAL documents, not one (2026-09-22): the comparison with the
    client's existing production tool showed a real procedure legitimately
    spread over several KB articles -- the end-user cases and the SSPR portal
    link in KB0320/KB0134, the Service Desk's own MFA-verification procedure
    in KB0341 -- and answering from a single one is structurally incomplete,
    whichever one is picked. Excluding off-topic material stays the job of
    this selection step (which is what keeps a 15 kB unrelated document out of
    the context), NOT of an arbitrary cap of one.

    temperature=0 + seed, like every other model call here (axiom A1). Any
    failure degrades to the top-ranked candidate rather than failing the
    answer, with the reason recorded in _trace."""
    if not candidates:
        return [], "aucun document texte candidat"
    if len(candidates) == 1:
        return [0], "un seul candidat"
    parts = [
        f"[FICHE {i} — {d.get('title', '')}]\n{(d.get('chunk') or '')[:_SELECTION_EXCERPT_CHARS]}"
        for i, d in enumerate(candidates, start=1)
    ]
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            seed=seed,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "primary_selection",
                    "strict": True,
                    "schema": _SELECTION_SCHEMA,
                },
            },
            messages=[
                {"role": "system", "content": _SELECTION_PROMPT},
                {
                    "role": "user",
                    "content": f"QUESTION: {query}\n\n" + "\n\n".join(parts),
                },
            ],
        )
        data = json.loads(resp.choices[0].message.content)
    except Exception as exc:
        return [0], f"selection indisponible ({type(exc).__name__}), repli sur le mieux classe"
    raw, reason = data.get("indexes") or [], (data.get("reason") or "").strip()
    chosen = []
    for idx in raw:
        if isinstance(idx, bool) or not isinstance(idx, int):
            continue
        if 1 <= idx <= len(candidates) and idx - 1 not in chosen:
            chosen.append(idx - 1)
    if not chosen:
        return [], reason or "aucune fiche ne traite le sujet"
    return chosen, reason


def generate(
    query: str,
    context: str,
    client: AzureOpenAI,
    model: str,
    temperature: float,
    seed: int,
) -> dict:
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        seed=seed,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "orchestrated_answer",
                "strict": True,
                "schema": ANSWER_SCHEMA,
            },
        },
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"CONTEXTE:\n{context}\n\nQUESTION: {query}"},
        ],
    )
    return json.loads(resp.choices[0].message.content)


def _selected_primary_index(result: dict, primaries: List[Dict]) -> Optional[int]:
    """0-based index of the SOURCE PRIMAIRE the model chose as the base of its
    answer, or None when it reported that none of the candidates covers the
    question (or returned an out-of-range value -- treated the same way, never
    silently coerced to the first candidate as if it had been chosen). Read
    BEFORE attach_sources() pops the raw model fields: the caller needs it too,
    for _trace/the confidence badge."""
    idx = result.get("primary_source_index")
    if isinstance(idx, bool) or not isinstance(idx, int):
        return None
    return idx - 1 if 1 <= idx <= len(primaries) else None


def attach_sources(result: dict, primaries: List[Dict], annexes: List[Dict]) -> dict:
    """Replaces the model's index/boolean-only report (primary_source_index,
    primary_sources_used, related_sources_used) with the public
    primary_source/related_sources contract, using the REAL titles from
    retrieval -- never a title the model retyped (see module docstring,
    2026-09-10 note). Also attaches `excerpt` (2026-09-18) -- the retrieved
    `chunk` verbatim -- and `safe_summary` (2026-09-19).

    Several PRIMARY candidates are now retrieved (2026-09-21) but the public
    contract is unchanged, deliberately: `primary_source` stays a single
    object -- the candidate the model picked as the base of its answer -- and
    any OTHER candidate it actually used is reported in `related_sources`,
    where app/app.py already groups KB sources separately from call
    transcripts. Candidates the model judged off-topic are dropped from the
    response entirely (they stay in _trace.primary_candidates for debugging):
    showing a user a KB article about resetting an iPhone under a question
    about passwords is noise, not traceability.

    excerpt is shown verbatim by app/app.py ONLY for sourceType not in
    (audio, video): authored KB text, safe to show in full. For an
    audio/video source, `excerpt` is still populated here (kept for
    completeness/traceability -- see _trace/eval usage elsewhere) but
    app/app.py never renders it; instead `safe_summary` -- the model's own
    redacted-by-instruction paraphrase, run through the _looks_sensitive()
    backstop -- is what's safe to show, restoring some value from the call
    without ever surfacing what was actually said."""
    selected = _selected_primary_index(result, primaries)
    result.pop("primary_source_index", None)
    primary_used_flags = result.pop("primary_sources_used", [])
    used_flags = result.pop("related_sources_used", [])
    primary_summaries_raw = result.pop("primary_safe_summaries", [])
    annex_summaries_raw = result.pop("annex_safe_summaries", [])

    def _safe(text: Optional[str]) -> Optional[str]:
        text = (text or "").strip() or None
        return text if text and not _looks_sensitive(text) else None

    def _entry(doc: Dict, used: bool, summary_raw: Optional[str]) -> dict:
        is_media = doc.get("sourceType") in ("audio", "video")
        return {
            "title": doc.get("title"),
            "used": used,
            "sourceType": doc.get("sourceType"),
            "excerpt": doc.get("chunk"),
            "safe_summary": _safe(summary_raw) if is_media and used else None,
        }

    def _psum(i: int) -> Optional[str]:
        return primary_summaries_raw[i] if i < len(primary_summaries_raw) else None

    # When the model reported that NO candidate covers the question, the
    # best-ranked one is still surfaced (flagged used=False) so the user sees
    # what was actually retrieved -- same behaviour as before, when there was
    # only ever one primary and it was shown whether used or not.
    base_i = selected if selected is not None else 0
    result["primary_source"] = (
        _entry(primaries[base_i], selected is not None, _psum(base_i))
        if primaries
        else None
    )

    related_sources = []
    for i, p in enumerate(primaries):
        if i == base_i:
            continue
        used = primary_used_flags[i] if i < len(primary_used_flags) else False
        if used:
            related_sources.append(_entry(p, True, _psum(i)))
    for i, a in enumerate(annexes):
        used = used_flags[i] if i < len(used_flags) else False
        summary = annex_summaries_raw[i] if i < len(annex_summaries_raw) else None
        related_sources.append(_entry(a, used, summary))
    result["related_sources"] = related_sources
    return result


def answer_query_core(
    client_id: str,
    query: str,
    search_key: str,
    aoai_client: AzureOpenAI,
    cfg: Optional[dict] = None,
) -> dict:
    """Reusable core: no az calls, no client construction -- callers that need
    many answers (e.g. eval/evaluate_rag.py, batch-scoring a golden dataset)
    should fetch credentials ONCE and call this directly instead of going
    through answer_query(), which re-fetches keys on every call (fine for a
    single CLI invocation, wasteful in a loop). See orchestration/README.md,
    axiom A4 (separation) -- eval/ reuses this rather than duplicating it."""
    cfg = cfg or load_engine_config(client_id)
    index = cfg["knowledge"]["index"]
    primary_count = cfg["retrieval"]["primaryCount"]
    annex_count = cfg["retrieval"]["annexCount"]
    gen = cfg["generation"]

    search_headers = {"api-key": search_key}
    candidates, annexes = retrieve_hierarchy(
        query, index, primary_count, annex_count, search_headers, client_id=client_id
    )
    chosen, selection_reason = select_primaries(
        query, candidates, aoai_client, gen["model"], gen["seed"]
    )
    chosen = chosen[:primary_count]  # primaryCount (engine.yaml) plafonne le CONTEXTE
    if chosen:
        primaries = _expand_to_full_documents(
            [candidates[i] for i in chosen], index, search_headers, client_id=client_id
        )
        # Un document retenu comme primaire ne doit pas reapparaitre en annexe
        # (possible pour un client sans audio, ou les annexes sont du texte).
        _keys = {d.get("parent_id") or d.get("title") for d in primaries}
        annexes = [a for a in annexes if (a.get("parent_id") or a.get("title")) not in _keys]
    else:
        # Aucune fiche ne traite le sujet : on montre le mieux classe TEL QUEL
        # (fragment, non expanse) -- expanser un document hors sujet, c'est
        # exactement ce qui a contamine la reponse le 2026-09-21.
        primaries = candidates[:1]
    context = format_context(primaries, annexes)

    result = generate(query, context, aoai_client, gen["model"], gen["temperature"], gen["seed"])
    selected = _selected_primary_index(result, primaries)
    result = attach_sources(result, primaries, annexes)
    base_primary = primaries[selected if selected is not None else 0] if primaries else None

    # Traceability (axiom A5): attach which source was primary/annex, its real
    # reranker score, and the raw context string (golden-dataset evaluation
    # needs it verbatim -- see eval/evaluate_rag.py). Deliberately NOT asking
    # the model to self-report Groundedness/Relevance/Retrieval -- a model
    # grading its own answer is a hallucination risk. Those scores stay the
    # job of the batch Azure AI Foundry Evaluators pipeline (eval/), run
    # against a golden dataset, not a per-call estimate.
    result["_trace"] = {
        "client": client_id,
        "index": index,
        "context": context,
        "primary_title": base_primary.get("title") if base_primary else None,
        "primary_reranker_score": (
            base_primary.get("@search.rerankerScore") if base_primary else None
        ),
        "primary_selected_by_model": selected is not None,
        "primary_selection_reason": selection_reason,
        "primary_candidates": [
            {"title": d.get("title"), "score": d.get("@search.rerankerScore")}
            for d in candidates
        ],
        "annex_titles": [a.get("title") for a in annexes],
    }
    return result


def answer_query(client_id: str, query: str) -> dict:
    """Convenience one-shot wrapper for interactive/CLI use: fetches keys via
    az and builds the AzureOpenAI client itself, then delegates to
    answer_query_core(). For batch use (many queries), call
    answer_query_core() directly with credentials fetched once."""
    cfg = load_engine_config(client_id)

    print("Retrieving keys (runtime, not stored)...")
    search_key = az(
        f"az search admin-key show --service-name {SEARCH_SERVICE} "
        f"--resource-group {RG} --query primaryKey -o tsv"
    )
    aoai_key = az(
        f"az cognitiveservices account keys list --name {AOAI_ACCOUNT} "
        f"--resource-group {RG} --query key1 -o tsv"
    )
    aoai_client = AzureOpenAI(
        azure_endpoint=AOAI_ENDPOINT, api_key=aoai_key, api_version=AOAI_API_VERSION
    )

    return answer_query_core(client_id, query, search_key, aoai_client, cfg=cfg)


# =====================================================================
# Keyless path (Jalon 4) -- Azure AD RBAC instead of admin keys.
#
# Added for app/app.py (Azure Web App), which has no `az login` session to
# fetch keys with and should not carry admin keys as app settings (axiom
# A5, same reasoning already applied to Search's own managed identity in
# infra/modules/roles.bicep). DefaultAzureCredential resolves to the App
# Service's system-assigned managed identity in Azure, and falls back to an
# interactive `az login` session locally (AzureCliCredential) -- so this
# also works for local dev, it is just not used by answer_query()/eval/
# above, which are unchanged and keep using admin keys as validated in
# Jalon 3.
#
# Requires the caller's identity to hold, on the target resources (see
# infra/modules/roles.bicep):
#   - "Search Index Data Reader" on the Search service
#   - "Cognitive Services OpenAI User" on the Foundry account (same role
#     already granted to Search's own identity, for embeddings)
# =====================================================================


def build_aoai_client_keyless(credential: DefaultAzureCredential) -> AzureOpenAI:
    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )
    return AzureOpenAI(
        azure_endpoint=AOAI_ENDPOINT,
        azure_ad_token_provider=token_provider,
        api_version=AOAI_API_VERSION,
    )


def answer_query_core_keyless(
    client_id: str,
    query: str,
    search_bearer_token: str,
    aoai_client: AzureOpenAI,
    cfg: Optional[dict] = None,
) -> dict:
    """Same pipeline as answer_query_core(), RBAC/Bearer auth on Search
    instead of an admin api-key. Kept as a separate function rather than
    branching inside answer_query_core() so the validated Jalon 3 function
    and its callers (CLI, eval/evaluate_rag.py) stay byte-for-byte
    unchanged -- see module docstring, 2026-09-11 note."""
    cfg = cfg or load_engine_config(client_id)
    index = cfg["knowledge"]["index"]
    primary_count = cfg["retrieval"]["primaryCount"]
    annex_count = cfg["retrieval"]["annexCount"]
    gen = cfg["generation"]

    search_headers = {"Authorization": f"Bearer {search_bearer_token}"}
    candidates, annexes = retrieve_hierarchy(
        query, index, primary_count, annex_count, search_headers, client_id=client_id
    )
    chosen, selection_reason = select_primaries(
        query, candidates, aoai_client, gen["model"], gen["seed"]
    )
    chosen = chosen[:primary_count]  # primaryCount (engine.yaml) plafonne le CONTEXTE
    if chosen:
        primaries = _expand_to_full_documents(
            [candidates[i] for i in chosen], index, search_headers, client_id=client_id
        )
        # Un document retenu comme primaire ne doit pas reapparaitre en annexe
        # (possible pour un client sans audio, ou les annexes sont du texte).
        _keys = {d.get("parent_id") or d.get("title") for d in primaries}
        annexes = [a for a in annexes if (a.get("parent_id") or a.get("title")) not in _keys]
    else:
        # Aucune fiche ne traite le sujet : on montre le mieux classe TEL QUEL
        # (fragment, non expanse) -- expanser un document hors sujet, c'est
        # exactement ce qui a contamine la reponse le 2026-09-21.
        primaries = candidates[:1]
    context = format_context(primaries, annexes)

    result = generate(query, context, aoai_client, gen["model"], gen["temperature"], gen["seed"])
    selected = _selected_primary_index(result, primaries)
    result = attach_sources(result, primaries, annexes)
    base_primary = primaries[selected if selected is not None else 0] if primaries else None
    result["_trace"] = {
        "client": client_id,
        "index": index,
        "context": context,
        "primary_title": base_primary.get("title") if base_primary else None,
        "primary_reranker_score": (
            base_primary.get("@search.rerankerScore") if base_primary else None
        ),
        "primary_selected_by_model": selected is not None,
        "primary_selection_reason": selection_reason,
        "primary_candidates": [
            {"title": d.get("title"), "score": d.get("@search.rerankerScore")}
            for d in candidates
        ],
        "annex_titles": [a.get("title") for a in annexes],
    }
    return result


def analyze_screenshot_query(client_id: str, query: str, image_path: str) -> dict:
    """Convenience one-shot wrapper for interactive/CLI use, mirroring
    answer_query() (2026-09-24 screenshot-upload design note) -- fetches
    keys via az, reads+base64-encodes the local image file, delegates to
    analyze_screenshot_query_core()."""
    import mimetypes

    cfg = load_engine_config(client_id)

    print("Retrieving keys (runtime, not stored)...")
    search_key = az(
        f"az search admin-key show --service-name {SEARCH_SERVICE} "
        f"--resource-group {RG} --query primaryKey -o tsv"
    )
    aoai_key = az(
        f"az cognitiveservices account keys list --name {AOAI_ACCOUNT} "
        f"--resource-group {RG} --query key1 -o tsv"
    )
    aoai_client = AzureOpenAI(
        azure_endpoint=AOAI_ENDPOINT, api_key=aoai_key, api_version=AOAI_API_VERSION
    )

    image_bytes = Path(image_path).read_bytes()
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    image_mime = mimetypes.guess_type(image_path)[0] or "image/png"

    return analyze_screenshot_query_core(
        client_id, query, image_b64, image_mime, search_key, aoai_client, cfg=cfg
    )


def _generate_screenshot(
    query: str,
    context: str,
    image_b64: str,
    image_mime: str,
    client: AzureOpenAI,
    model: str,
    seed: int,
) -> dict:
    """Vision-enabled sibling of generate() -- see the 2026-09-24 design note
    above SCREENSHOT_SYSTEM_PROMPT. temperature=0 + seed, same determinism
    axiom (A1) as generate(), but NOT the same function: generate() has no
    image content part and must stay that way for the CLI/eval/normal-app
    path to remain byte-for-byte unchanged."""
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        seed=seed,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "screenshot_diagnostic",
                "strict": True,
                "schema": SCREENSHOT_ANSWER_SCHEMA,
            },
        },
        messages=[
            {"role": "system", "content": SCREENSHOT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"CONTEXTE:\n{context}\n\nQUESTION DE L'AGENT: {query}",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{image_mime};base64,{image_b64}"},
                    },
                ],
            },
        ],
    )
    result = json.loads(resp.choices[0].message.content)
    result["screen_reading"] = _redact_visible_password(result.get("screen_reading"))
    result["detected_error_codes"] = sorted(
        set(_ERROR_CODE_PATTERNS.findall(result.get("screen_reading") or ""))
    )
    return result


def analyze_screenshot_query_core(
    client_id: str,
    query: str,
    image_b64: str,
    image_mime: str,
    search_key: str,
    aoai_client: AzureOpenAI,
    cfg: Optional[dict] = None,
) -> dict:
    """Screenshot-upload sibling of answer_query_core() (api-key path) --
    see the 2026-09-24 design note above SCREENSHOT_SYSTEM_PROMPT. Retrieval
    is byte-for-byte the same call sequence as answer_query_core(); only the
    generation step differs (_generate_screenshot instead of generate)."""
    cfg = cfg or load_engine_config(client_id)
    index = cfg["knowledge"]["index"]
    primary_count = cfg["retrieval"]["primaryCount"]
    annex_count = cfg["retrieval"]["annexCount"]
    gen = cfg["generation"]

    search_headers = {"api-key": search_key}
    candidates, annexes = retrieve_hierarchy(
        query, index, primary_count, annex_count, search_headers, client_id=client_id
    )
    chosen, selection_reason = select_primaries(
        query, candidates, aoai_client, gen["model"], gen["seed"]
    )
    chosen = chosen[:primary_count]
    if chosen:
        primaries = _expand_to_full_documents(
            [candidates[i] for i in chosen], index, search_headers, client_id=client_id
        )
        _keys = {d.get("parent_id") or d.get("title") for d in primaries}
        annexes = [a for a in annexes if (a.get("parent_id") or a.get("title")) not in _keys]
    else:
        primaries = candidates[:1]
    context = format_context(primaries, annexes)

    result = _generate_screenshot(query, context, image_b64, image_mime, aoai_client, gen["model"], gen["seed"])
    selected = _selected_primary_index(result, primaries)
    result = attach_sources(result, primaries, annexes)
    base_primary = primaries[selected if selected is not None else 0] if primaries else None
    result["_trace"] = {
        "client": client_id,
        "index": index,
        "context": context,
        "primary_title": base_primary.get("title") if base_primary else None,
        "primary_reranker_score": (
            base_primary.get("@search.rerankerScore") if base_primary else None
        ),
        "primary_selected_by_model": selected is not None,
        "primary_selection_reason": selection_reason,
        "primary_candidates": [
            {"title": d.get("title"), "score": d.get("@search.rerankerScore")}
            for d in candidates
        ],
        "annex_titles": [a.get("title") for a in annexes],
    }
    return result


def analyze_screenshot_query_core_keyless(
    client_id: str,
    query: str,
    image_b64: str,
    image_mime: str,
    search_bearer_token: str,
    aoai_client: AzureOpenAI,
    cfg: Optional[dict] = None,
) -> dict:
    """Same as analyze_screenshot_query_core(), RBAC/Bearer auth on Search
    instead of an admin api-key -- for app/app.py, mirroring the
    answer_query_core / answer_query_core_keyless split (2026-09-11 note)."""
    cfg = cfg or load_engine_config(client_id)
    index = cfg["knowledge"]["index"]
    primary_count = cfg["retrieval"]["primaryCount"]
    annex_count = cfg["retrieval"]["annexCount"]
    gen = cfg["generation"]

    search_headers = {"Authorization": f"Bearer {search_bearer_token}"}
    candidates, annexes = retrieve_hierarchy(
        query, index, primary_count, annex_count, search_headers, client_id=client_id
    )
    chosen, selection_reason = select_primaries(
        query, candidates, aoai_client, gen["model"], gen["seed"]
    )
    chosen = chosen[:primary_count]
    if chosen:
        primaries = _expand_to_full_documents(
            [candidates[i] for i in chosen], index, search_headers, client_id=client_id
        )
        _keys = {d.get("parent_id") or d.get("title") for d in primaries}
        annexes = [a for a in annexes if (a.get("parent_id") or a.get("title")) not in _keys]
    else:
        primaries = candidates[:1]
    context = format_context(primaries, annexes)

    result = _generate_screenshot(query, context, image_b64, image_mime, aoai_client, gen["model"], gen["seed"])
    selected = _selected_primary_index(result, primaries)
    result = attach_sources(result, primaries, annexes)
    base_primary = primaries[selected if selected is not None else 0] if primaries else None
    result["_trace"] = {
        "client": client_id,
        "index": index,
        "context": context,
        "primary_title": base_primary.get("title") if base_primary else None,
        "primary_reranker_score": (
            base_primary.get("@search.rerankerScore") if base_primary else None
        ),
        "primary_selected_by_model": selected is not None,
        "primary_selection_reason": selection_reason,
        "primary_candidates": [
            {"title": d.get("title"), "score": d.get("@search.rerankerScore")}
            for d in candidates
        ],
        "annex_titles": [a.get("title") for a in annexes],
    }
    return result


def _build_diagnostic_state_block(prior_turns: List[Dict], vocab: Dict[str, str]) -> str:
    """Deterministic text block summarizing prior turns of THIS conversation
    -- fiches deja servies (par titre : the only identifier already
    available without new plumbing -- primary_source has no separate kb_id
    field, see attach_sources()), codes d'erreur deja detectes (already
    regex-validated by _ERROR_CODE_PATTERNS when that turn ran), entites du
    vocabulaire controle mentionnees (same deterministic detection as
    _detect_query_entities, applied here to prior queries AND prior screen
    readings). Returns "" for an empty prior_turns (new conversation) -- the
    caller then omits the block from the prompt entirely rather than
    sending an empty section.

    prior_turns: list of plain dicts shaped like app/app.py's _load_turns()
    output (query, answer_text, primary_source, detected_error_codes,
    screen_reading) -- deliberately duck-typed, this module never imports
    app/app.py."""
    if not prior_turns:
        return ""
    fiches: List[str] = []
    codes: List[str] = []
    entites: List[str] = []
    captures = 0
    for t in prior_turns:
        ps = t.get("primary_source")
        if ps and ps.get("title") and ps.get("used") and ps["title"] not in fiches:
            fiches.append(ps["title"])
        for c in t.get("detected_error_codes") or []:
            if c not in codes:
                codes.append(c)
        probe_text = f"{t.get('query') or ''} {t.get('screen_reading') or ''}"
        for e in _detect_query_entities(probe_text, vocab):
            if e not in entites:
                entites.append(e)
        if t.get("screen_reading") and t["screen_reading"] not in (
            "aucune capture fournie a ce tour",
            "aucun texte lisible sur cette image",
        ):
            captures += 1
    lines = ["ETAT DU DIAGNOSTIC (accumule sur cette conversation) :"]
    lines.append("- Fiches deja servies : " + (", ".join(fiches) if fiches else "aucune"))
    lines.append("- Codes d'erreur deja releves : " + (", ".join(codes) if codes else "aucun"))
    lines.append("- Entites deja identifiees : " + (", ".join(entites) if entites else "aucune"))
    if captures:
        lines.append(f"- {captures} capture(s) d'ecran deja fournie(s) plus tot dans cette conversation.")
    lines.append("- Tours precedents de cette conversation, du plus ancien au plus recent :")
    for i, t in enumerate(prior_turns, 1):
        q = (t.get("query") or "").strip()
        a = (t.get("answer_text") or "").strip()
        if len(a) > 600:
            a = a[:600].rstrip() + "…"
        lines.append(f"  Tour {i} -- Q: {q}")
        lines.append(f"  Tour {i} -- R: {a}")
    return "\n".join(lines)


def _generate_diagnostic(
    query: str,
    context: str,
    state_block: str,
    image_b64: Optional[str],
    image_mime: Optional[str],
    client: AzureOpenAI,
    model: str,
    seed: int,
) -> dict:
    """Generation step for a diagnostic-conversation turn -- sibling of
    generate()/_generate_screenshot(), unifying both: always uses
    DIAGNOSTIC_ANSWER_SCHEMA/DIAGNOSTIC_SYSTEM_PROMPT, attaches an image
    content part only when one was actually provided THIS turn (image_b64
    not None), and prepends state_block (from
    _build_diagnostic_state_block) to the user message when non-empty."""
    user_text = f"CONTEXTE:\n{context}\n"
    if state_block:
        user_text += f"\n{state_block}\n"
    user_text += f"\nMESSAGE COURANT DE L'AGENT: {query}"
    content = [{"type": "text", "text": user_text}]
    if image_b64:
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}}
        )
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        seed=seed,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "diagnostic_turn",
                "strict": True,
                "schema": DIAGNOSTIC_ANSWER_SCHEMA,
            },
        },
        messages=[
            {"role": "system", "content": DIAGNOSTIC_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    )
    result = json.loads(resp.choices[0].message.content)
    result["screen_reading"] = _redact_visible_password(result.get("screen_reading"))
    result["detected_error_codes"] = sorted(
        set(_ERROR_CODE_PATTERNS.findall(result.get("screen_reading") or ""))
    )
    return result


def _build_retrieval_query(query: str, prior_turns: List[Dict], window: int = 2) -> str:
    """Design note (2026-09-25, found live-testing layer 2): retrieval was
    grounded on the CURRENT turn's raw query text only -- fine for a
    standalone question, but a short elliptical follow-up in a diagnostic
    conversation ("comment le faire") carries no topic signal on its own.
    Observed live: after a turn about "AUTHENTICATOR", "comment le faire"
    alone retrieved an unrelated document (LogMeIn) -- Yassine's call:
    unacceptable, the engine has to know the topic is still Authenticator.
    Folds the last `window` prior queries into the text handed to
    retrieve_hierarchy()/select_primaries() (never into the text shown to
    generation -- _generate_diagnostic still gets the raw current `query`,
    the model already has the full prior turns via state_block). This also
    feeds _detect_query_entities() inside retrieve_hierarchy() (same text),
    so a controlled-vocabulary term mentioned in a recent turn (e.g.
    'AUTHENTICATOR') keeps triggering the entity-boost scoringProfile even
    when the current turn doesn't restate it -- no extra model call, same
    deterministic mechanism already used for a single turn."""
    if not prior_turns:
        return query
    recent = " ".join((t.get("query") or "").strip() for t in prior_turns[-window:])
    return f"{recent} {query}".strip()


def diagnostic_query_core(
    client_id: str,
    query: str,
    prior_turns: List[Dict],
    search_key: str,
    aoai_client: AzureOpenAI,
    image_b64: Optional[str] = None,
    image_mime: Optional[str] = None,
    cfg: Optional[dict] = None,
) -> dict:
    """Diagnostic-conversation sibling of answer_query_core() (api-key
    path) -- see the 2026-09-24 (later) design note above
    DIAGNOSTIC_SYSTEM_PROMPT. CLI/testing use (diagnostic_query()); the app
    uses the RBAC/keyless twin below."""
    cfg = cfg or load_engine_config(client_id)
    index = cfg["knowledge"]["index"]
    primary_count = cfg["retrieval"]["primaryCount"]
    annex_count = cfg["retrieval"]["annexCount"]
    gen = cfg["generation"]

    search_headers = {"api-key": search_key}
    retrieval_query = _build_retrieval_query(query, prior_turns)
    candidates, annexes = retrieve_hierarchy(
        retrieval_query, index, primary_count, annex_count, search_headers, client_id=client_id
    )
    chosen, selection_reason = select_primaries(
        retrieval_query, candidates, aoai_client, gen["model"], gen["seed"]
    )
    chosen = chosen[:primary_count]
    if chosen:
        primaries = _expand_to_full_documents(
            [candidates[i] for i in chosen], index, search_headers, client_id=client_id
        )
        _keys = {d.get("parent_id") or d.get("title") for d in primaries}
        annexes = [a for a in annexes if (a.get("parent_id") or a.get("title")) not in _keys]
    else:
        primaries = candidates[:1]
    context = format_context(primaries, annexes)

    vocab = _client_entity_vocabulary(index, search_headers, client_id)
    state_block = _build_diagnostic_state_block(prior_turns, vocab)

    result = _generate_diagnostic(
        query, context, state_block, image_b64, image_mime, aoai_client, gen["model"], gen["seed"]
    )
    selected = _selected_primary_index(result, primaries)
    result = attach_sources(result, primaries, annexes)
    base_primary = primaries[selected if selected is not None else 0] if primaries else None
    result["_trace"] = {
        "client": client_id,
        "index": index,
        "context": context,
        "state_block": state_block,
        "retrieval_query": retrieval_query,
        "primary_title": base_primary.get("title") if base_primary else None,
        "primary_reranker_score": (
            base_primary.get("@search.rerankerScore") if base_primary else None
        ),
        "primary_selected_by_model": selected is not None,
        "primary_selection_reason": selection_reason,
        "primary_candidates": [
            {"title": d.get("title"), "score": d.get("@search.rerankerScore")}
            for d in candidates
        ],
        "annex_titles": [a.get("title") for a in annexes],
    }
    return result


def diagnostic_query_core_keyless(
    client_id: str,
    query: str,
    prior_turns: List[Dict],
    search_bearer_token: str,
    aoai_client: AzureOpenAI,
    image_b64: Optional[str] = None,
    image_mime: Optional[str] = None,
    cfg: Optional[dict] = None,
) -> dict:
    """Same as diagnostic_query_core(), RBAC/Bearer auth on Search instead
    of an admin api-key -- app/app.py calls this for EVERY turn of a
    diagnostic conversation now (prior_turns=[] for a brand-new one),
    replacing both answer_query_core_keyless and
    analyze_screenshot_query_core_keyless in that file. Those two functions
    (and answer_query_core/analyze_screenshot_query_core) are kept
    unchanged for the CLI/eval paths."""
    cfg = cfg or load_engine_config(client_id)
    index = cfg["knowledge"]["index"]
    primary_count = cfg["retrieval"]["primaryCount"]
    annex_count = cfg["retrieval"]["annexCount"]
    gen = cfg["generation"]

    search_headers = {"Authorization": f"Bearer {search_bearer_token}"}
    retrieval_query = _build_retrieval_query(query, prior_turns)
    candidates, annexes = retrieve_hierarchy(
        retrieval_query, index, primary_count, annex_count, search_headers, client_id=client_id
    )
    chosen, selection_reason = select_primaries(
        retrieval_query, candidates, aoai_client, gen["model"], gen["seed"]
    )
    chosen = chosen[:primary_count]
    if chosen:
        primaries = _expand_to_full_documents(
            [candidates[i] for i in chosen], index, search_headers, client_id=client_id
        )
        _keys = {d.get("parent_id") or d.get("title") for d in primaries}
        annexes = [a for a in annexes if (a.get("parent_id") or a.get("title")) not in _keys]
    else:
        primaries = candidates[:1]
    context = format_context(primaries, annexes)

    vocab = _client_entity_vocabulary(index, search_headers, client_id)
    state_block = _build_diagnostic_state_block(prior_turns, vocab)

    result = _generate_diagnostic(
        query, context, state_block, image_b64, image_mime, aoai_client, gen["model"], gen["seed"]
    )
    selected = _selected_primary_index(result, primaries)
    result = attach_sources(result, primaries, annexes)
    base_primary = primaries[selected if selected is not None else 0] if primaries else None
    result["_trace"] = {
        "client": client_id,
        "index": index,
        "context": context,
        "state_block": state_block,
        "retrieval_query": retrieval_query,
        "primary_title": base_primary.get("title") if base_primary else None,
        "primary_reranker_score": (
            base_primary.get("@search.rerankerScore") if base_primary else None
        ),
        "primary_selected_by_model": selected is not None,
        "primary_selection_reason": selection_reason,
        "primary_candidates": [
            {"title": d.get("title"), "score": d.get("@search.rerankerScore")}
            for d in candidates
        ],
        "annex_titles": [a.get("title") for a in annexes],
    }
    return result


def diagnostic_query(
    client_id: str,
    query: str,
    prior_turns_path: Optional[str] = None,
    image_path: Optional[str] = None,
) -> dict:
    """CLI convenience for testing layer 2 without the web app:
    prior_turns_path points to a JSON file holding a list of turn dicts
    (same shape app/app.py's _load_turns() returns -- see
    _build_diagnostic_state_block's docstring), letting Yassine simulate a
    follow-up turn from the terminal. Fetches keys via az, like
    analyze_screenshot_query()."""
    import mimetypes

    cfg = load_engine_config(client_id)

    print("Retrieving keys (runtime, not stored)...")
    search_key = az(
        f"az search admin-key show --service-name {SEARCH_SERVICE} "
        f"--resource-group {RG} --query primaryKey -o tsv"
    )
    aoai_key = az(
        f"az cognitiveservices account keys list --name {AOAI_ACCOUNT} "
        f"--resource-group {RG} --query key1 -o tsv"
    )
    aoai_client = AzureOpenAI(
        azure_endpoint=AOAI_ENDPOINT, api_key=aoai_key, api_version=AOAI_API_VERSION
    )

    prior_turns = []
    if prior_turns_path:
        # utf-8-sig (2026-09-24 later note): Windows PowerShell's
        # `Set-Content -Encoding utf8` writes a BOM -- plain utf-8 here
        # would leave it in the string and break json.loads(). -sig
        # strips a BOM if present, harmless if absent (recurring
        # Windows/PowerShell encoding theme, see project memory).
        loaded = json.loads(Path(prior_turns_path).read_text(encoding="utf-8-sig"))
        # Defensive (2026-09-24 later): a common PowerShell pitfall is a
        # single-element array collapsing to a bare object through the
        # pipeline (`@($x) | ConvertTo-Json` only unwraps when $x is
        # itself already an array) -- accept either shape rather than
        # failing deep inside _build_diagnostic_state_block with a
        # confusing AttributeError.
        prior_turns = loaded if isinstance(loaded, list) else [loaded]

    image_b64 = image_mime = None
    if image_path:
        image_bytes = Path(image_path).read_bytes()
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        image_mime = mimetypes.guess_type(image_path)[0] or "image/png"

    return diagnostic_query_core(
        client_id,
        query,
        prior_turns,
        search_key,
        aoai_client,
        image_b64=image_b64,
        image_mime=image_mime,
        cfg=cfg,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KnowledgeEngine v9 -- orchestration layer")
    parser.add_argument("--client", required=True, help="client id, e.g. clienta or client-v")
    parser.add_argument("--query", required=True)
    parser.add_argument(
        "--image",
        help=(
            "Path to a local screenshot (2026-09-24 design note): routes to "
            "analyze_screenshot_query() instead of answer_query() -- diagnostic "
            "screenshot-upload path, does not affect the default flow. Combined "
            "with --prior-turns, routes to diagnostic_query() instead (layer 2)."
        ),
    )
    parser.add_argument(
        "--prior-turns",
        help=(
            "Path to a JSON file of prior turns (2026-09-24 later, layer 2 "
            "design note above DIAGNOSTIC_SYSTEM_PROMPT): routes to "
            "diagnostic_query() instead of answer_query()/analyze_screenshot_query() "
            "-- simulates a follow-up turn in a diagnostic conversation, with or "
            "without --image. Pass an empty JSON array ([]) to test diagnostic_query() "
            "on a first turn (no prior state, same as a brand-new conversation)."
        ),
    )
    args = parser.parse_args()

    if args.prior_turns is not None:
        result = diagnostic_query(args.client, args.query, args.prior_turns, args.image)
    elif args.image:
        result = analyze_screenshot_query(args.client, args.query, args.image)
    else:
        result = answer_query(args.client, args.query)
    print(json.dumps(result, ensure_ascii=False, indent=2))
