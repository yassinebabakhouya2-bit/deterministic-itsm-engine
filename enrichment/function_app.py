# =====================================================================
# fn-kb-enrich — enrichissement semantique a l'indexation
#
# Deux endpoints, appeles par Azure AI Search via WebApiSkill :
#   POST /api/enrich        documents texte du referentiel (fiches KB)
#   POST /api/enrich-call   transcriptions d'appels et de videos
#
# Principes de conception, dans l'ordre d'importance :
#
#  1. UN ALIAS N'EST RETENU QUE S'IL APPARAIT LITTERALEMENT DANS LE TEXTE.
#     Verifie par le code, pas par le modele. C'est ce qui permet d'alimenter
#     automatiquement le synonym map de production sans revue humaine : une
#     sortie generative devient une extraction verifiee.
#
#  2. LE VOCABULAIRE VIENT DES FICHES, JAMAIS DES APPELS. /enrich-call ne peut
#     etiqueter un appel qu'avec des entites deja presentes dans l'index du
#     client (issues de la passe sur les documents texte). Les transcriptions
#     comportent des erreurs de reconnaissance vocale -- les laisser creer du
#     vocabulaire remplirait le referentiel de fantomes.
#
#  3. UN APPEL EST UNE PREUVE, PAS UNE DOCTRINE. On extrait d'un appel son
#     probleme, sa resolution et son issue -- jamais de triplets de relation,
#     qui injecteraient des anecdotes dans le graphe comme si c'etaient des
#     regles etablies.
#
#  4. UN ECHEC D'ENRICHISSEMENT EST UN WARNING, JAMAIS UNE ERREUR. Un 429 ne
#     doit pas faire tomber un run d'indexation : le document s'indexe, seul
#     son enrichissement manque.
#
#  5. DETERMINISME : temperature=0 + seed fixe, modele epingle, et cache par
#     empreinte du contenu ET du prompt -- changer le prompt invalide le cache.
# =====================================================================
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from typing import Dict, List, Optional, Tuple

import azure.functions as func
import requests
from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential
from openai import AzureOpenAI, RateLimitError

app = func.FunctionApp()

AOAI_API_VERSION = "2024-08-01-preview"
SEARCH_API_VERSION = "2024-07-01"
SEED = 42
VOCAB_TTL_S = 600


# ------------------------------------------------------- initialisation paresseuse
# RIEN ne se construit a l'import du module, et aucune variable d'environnement
# n'y est lue en acces direct. C'est deliberé : sur Functions, une exception a
# l'import du fichier ne produit pas d'erreur lisible -- elle tue l'hote, qui
# repond alors 503 sur TOUTES les routes, sans trace exploitable sans
# Application Insights. Un reglage manquant doit se voir comme un warning sur
# un enregistrement, pas comme une Function App morte (principe 4 du README).
def _setting(name: str, default: Optional[str] = None) -> str:
    value = os.environ.get(name) or default
    if not value:
        raise RuntimeError(f"reglage applicatif manquant: {name}")
    return value


_credential_singleton: Optional[DefaultAzureCredential] = None
_aoai_singleton: Optional[AzureOpenAI] = None
_tables_singleton = None


def _credential() -> DefaultAzureCredential:
    global _credential_singleton
    if _credential_singleton is None:
        _credential_singleton = DefaultAzureCredential()
    return _credential_singleton


def _aoai() -> AzureOpenAI:
    global _aoai_singleton
    if _aoai_singleton is None:
        _aoai_singleton = AzureOpenAI(
            azure_endpoint=_setting("AOAI_ENDPOINT"),
            api_version=AOAI_API_VERSION,
            azure_ad_token_provider=lambda: _credential().get_token(
                "https://cognitiveservices.azure.com/.default"
            ).token,
        )
    return _aoai_singleton


def _tables():
    global _tables_singleton
    if _tables_singleton is None:
        _tables_singleton = TableServiceClient(
            endpoint=_setting("CACHE_TABLE_ENDPOINT"), credential=_credential()
        ).get_table_client(_setting("CACHE_TABLE", "enrichCache"))
    return _tables_singleton


# ---------------------------------------------------------------- normalisation
def _norm(text: str) -> str:
    """Minuscules, sans accents, espaces normalises. Sert a la fois a comparer
    un alias au texte source et a dedupliquer les entites : « Reinitialisation »
    et « reinitialisation » sont le meme terme."""
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", text).strip().lower()


# Termes trop generiques pour etre des entites : ils matcheraient tout et
# rendraient le filtrage par entite inutile. Ajuster au besoin, c'est une
# decision de vocabulaire, pas une regle technique.
_STOPWORDS = {
    _norm(w)
    for w in [
        "mot de passe", "password", "utilisateur", "user", "probleme", "erreur",
        "ticket", "compte", "session", "application", "logiciel", "service",
        "support", "service desk", "client", "poste", "ordinateur", "telephone",
        "email", "mail", "procedure", "document", "windows",
    ]
}


def _is_usable_entity(name: str) -> bool:
    n = _norm(name)
    return len(n) >= 2 and n not in _STOPWORDS


# ---------------------------------------------------------------------- cache
def _cache_key(kind: str, prompt: str, text: str) -> str:
    h = hashlib.sha256()
    for part in (kind, prompt, text):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _cache_get(key: str) -> Optional[dict]:
    try:
        return json.loads(_tables().get_entity(partition_key=key[:2], row_key=key)["payload"])
    except Exception:
        return None


def _cache_put(key: str, payload: dict) -> None:
    try:
        _tables().upsert_entity(
            {"PartitionKey": key[:2], "RowKey": key,
             "payload": json.dumps(payload, ensure_ascii=False)}
        )
    except Exception:
        logging.warning("ecriture cache impossible (%s)", key[:12], exc_info=True)


# ------------------------------------------------------- vocabulaire du client
_vocab_cache: Dict[str, Tuple[Dict[str, str], float]] = {}


def _client_vocabulary(client_id: str) -> Dict[str, str]:
    """Entites deja connues du client, lues dans l'index lui-meme (facette sur
    le champ `entities`, alimente par la passe sur les documents texte).
    Retourne {forme normalisee: forme canonique}. Cache en memoire par instance.

    Aucun parametre a saisir : onboarder un client ne demande aucune
    configuration de vocabulaire (axiome A2)."""
    cached = _vocab_cache.get(client_id)
    if cached and (time.time() - cached[1]) < VOCAB_TTL_S:
        return cached[0]

    vocab: Dict[str, str] = {}
    try:
        token = _credential().get_token("https://search.azure.com/.default").token
        r = requests.post(
            f'{_setting("SEARCH_ENDPOINT")}/indexes/idx-{client_id}/docs/search'
            f"?api-version={SEARCH_API_VERSION}",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={
                "search": "*",
                "top": 0,
                "facets": ["entities,count:1000"],
                "filter": f"clientId eq '{client_id}'",
            },
            timeout=30,
        )
        r.raise_for_status()
        for facet in r.json().get("@search.facets", {}).get("entities", []):
            value = facet.get("value")
            if value:
                vocab[_norm(value)] = value
    except Exception:
        logging.warning("vocabulaire indisponible pour %s", client_id, exc_info=True)

    _vocab_cache[client_id] = (vocab, time.time())
    return vocab


# ------------------------------------------------------------- appel au modele
def _chat(system_prompt: str, user_content: str, schema: dict, schema_name: str) -> dict:
    """temperature=0 + seed : deux indexations du meme contenu donnent le meme
    resultat. Les 429 sont retentes avec un backoff exponentiel ; au-dela,
    l'exception remonte a l'appelant qui la transformera en warning."""
    delay = 2.0
    for attempt in range(5):
        try:
            resp = _aoai().chat.completions.create(
                model=_setting("AOAI_DEPLOYMENT", "gpt-4o-enrich"),
                temperature=0,
                seed=SEED,
                max_tokens=1500,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            return json.loads(resp.choices[0].message.content)
        except RateLimitError:
            if attempt == 4:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


# ============================================================ FICHES DU REFERENTIEL
DOC_PROMPT = (
    "Tu analyses un extrait de fiche de base de connaissances informatique. Tu en "
    "extrais UNIQUEMENT ce qui est ecrit, sans rien deduire ni completer :\n\n"
    "1. entities : les objets techniques dont la fiche TRAITE -- logiciels, "
    "materiels, codes d'erreur, services, methodes, roles, procedures, portails. "
    "Pas les termes simplement mentionnes au passage. Pour chacun, donne aussi "
    "ses alias TELS QU'ILS APPARAISSENT DANS LE TEXTE : acronyme et forme "
    "longue, traduction anglaise ou francaise, variante d'ecriture. N'invente "
    "jamais un alias absent du texte : il sera rejete.\n\n"
    "2. triples : les mecaniques internes explicitement enoncees, sous forme "
    "sujet -> relation -> objet. Uniquement des relations ECRITES, jamais "
    "plausibles.\n\n"
    "3. audience : lue dans le bloc DESTINATION ou Diffusion de la fiche. USERS "
    "si la case Users est cochee, SERVICE_DESK si la fiche s'adresse a une "
    "equipe interne, INDETERMINE si l'extrait ne permet pas de trancher.\n\n"
    "Si une categorie est absente, renvoie une liste vide."
)

DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": [
                        "LOGICIEL", "MATERIEL", "CODE_ERREUR", "SERVICE",
                        "METHODE", "ROLE", "PROCEDURE", "PORTAIL"]},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "type", "aliases"],
                "additionalProperties": False,
            },
        },
        "triples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string", "enum": [
                        "depend_de", "declenche_erreur", "resolu_par",
                        "prerequis_de", "alternative_a", "valide_par",
                        "documente_dans", "s_applique_a"]},
                    "object": {"type": "string"},
                },
                "required": ["subject", "predicate", "object"],
                "additionalProperties": False,
            },
        },
        "audience": {"type": "string", "enum": ["USERS", "SERVICE_DESK", "INDETERMINE"]},
    },
    "required": ["entities", "triples", "audience"],
    "additionalProperties": False,
}

UNIFIED_EMPTY = {
    "entities": [], "entity_types": [], "aliases": [], "triples": [],
    "audience": "INDETERMINE",
    "call_problem": "", "call_resolution": "", "call_outcome": "NON_RESOLU",
}


def _verify_aliases(raw_entities: List[dict], source_text: str) -> List[dict]:
    """Le garde-fou central : on ne garde un alias que s'il figure LITTERALEMENT
    dans le texte source (comparaison insensible a la casse et aux accents).
    Une entite dont ni le nom ni aucun alias n'apparait est rejetee en entier --
    c'est une invention du modele, pas une extraction."""
    norm_source = _norm(source_text)
    kept = []
    for ent in raw_entities:
        name = (ent.get("name") or "").strip()
        if not name or not _is_usable_entity(name):
            continue
        aliases = [
            a.strip() for a in (ent.get("aliases") or [])
            if a and a.strip() and _norm(a) in norm_source and _norm(a) != _norm(name)
        ]
        if _norm(name) not in norm_source and not aliases:
            continue  # ni le nom ni un alias n'existe dans le texte : rejete
        seen, uniq = set(), []
        for a in aliases:
            if _norm(a) not in seen:
                seen.add(_norm(a))
                uniq.append(a)
        kept.append({"name": name, "type": ent.get("type") or "PROCEDURE",
                     "aliases": uniq})
    return kept


def _shape_doc(raw: dict, source_text: str) -> dict:
    entities = _verify_aliases(raw.get("entities") or [], source_text)
    names = {_norm(e["name"]) for e in entities}
    triples = [
        t for t in (raw.get("triples") or [])
        if t.get("subject") and t.get("object") and t.get("predicate")
    ]
    # Serialisation des alias : « canonique|alias1|alias2 », consomme par le
    # generateur de synonym map.
    alias_rows = [
        e["name"] + "|" + "|".join(e["aliases"]) for e in entities if e["aliases"]
    ]
    return {
        "entities": [e["name"] for e in entities],
        "entity_types": sorted({e["type"] for e in entities}),
        "aliases": alias_rows,
        "triples": [
            {"subject": t["subject"], "predicate": t["predicate"], "object": t["object"]}
            for t in triples
            if _norm(t["subject"]) in names or _norm(t["object"]) in names
        ],
        "audience": raw.get("audience") or "INDETERMINE",
        # Champs du volet APPEL : toujours presents, jamais remplis par une
        # fiche. Une seule forme de sortie pour les deux volets (voir enrich()).
        "call_problem": "", "call_resolution": "", "call_outcome": "NON_RESOLU",
    }


def _process_doc(text: str, title: str) -> Tuple[dict, list]:
    """Volet FICHE : extraction entites/alias/triplets/audience. Voir DOC_PROMPT."""
    key = _cache_key("doc", DOC_PROMPT, text)
    hit = _cache_get(key)
    if hit is not None:
        return hit, []
    try:
        raw = _chat(DOC_PROMPT, f"FICHE: {title}\n\nEXTRAIT:\n{text[:12000]}",
                    DOC_SCHEMA, "kb_semantics")
        payload = _shape_doc(raw, text)
        _cache_put(key, payload)
        return payload, []
    except Exception as exc:
        logging.exception("enrichissement fiche impossible")
        return UNIFIED_EMPTY, [{"message": f"enrichissement indisponible: {type(exc).__name__}"}]


# ==================================================== APPELS ET VIDEOS
CALL_PROMPT = (
    "Tu analyses le resume d'un appel de support informatique, pas une procedure "
    "officielle. Un appel est un cas particulier : tu n'en tires aucune regle "
    "generale, aucune dependance technique, aucune doctrine.\n\n"
    "Tu extrais :\n"
    "1. call_problem : en une phrase, le probleme reellement traite.\n"
    "2. call_resolution : en une ou deux phrases, ce qui a ete fait concretement.\n"
    "3. call_outcome : RESOLU si le probleme a ete regle pendant l'appel ; "
    "ESCALADE si l'appel s'est conclu par l'ouverture d'un ticket ou le renvoi "
    "vers une autre equipe ; NON_RESOLU s'il se termine sans solution ni "
    "escalade.\n"
    "4. entities : UNIQUEMENT des entites figurant dans la LISTE AUTORISEE "
    "ci-dessous, et uniquement celles dont l'appel traite reellement -- pas "
    "celles citees au passage. Si aucune ne correspond, renvoie une liste vide. "
    "Tu n'inventes JAMAIS une entite absente de la liste : elle sera rejetee.\n\n"
    "La transcription peut comporter des erreurs de reconnaissance vocale et des "
    "passages masques par des asterisques. Ne cherche pas a les reconstituer."
)

CALL_SCHEMA = {
    "type": "object",
    "properties": {
        "call_problem": {"type": "string"},
        "call_resolution": {"type": "string"},
        "call_outcome": {"type": "string", "enum": ["RESOLU", "ESCALADE", "NON_RESOLU"]},
        "entities": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["call_problem", "call_resolution", "call_outcome", "entities"],
    "additionalProperties": False,
}

_SUMMARY_RE = re.compile(
    r"(Probleme|Problème)\s*:(?P<body>.*?)(?=---\s*Transcription|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _call_summary(content: str) -> Tuple[str, bool]:
    """Le pipeline audio produit deja, en tete de chaque transcription, un bloc
    « Probleme : ... Resolution : ... ». C'est la matiere de l'extraction : deja
    synthetique, deja nettoyee, et sans le bruit des milliers de mots de dialogue
    qui suivent. Retourne (texte, bloc_trouve)."""
    m = _SUMMARY_RE.search(content or "")
    if m:
        return ("Probleme :" + m.group("body")).strip()[:4000], True
    return (content or "")[:3000], False


def _process_call(content: str, title: str, client_id: str) -> Tuple[dict, list]:
    """Volet APPEL : call_problem/resolution/outcome + entites VERROUILLEES sur
    le vocabulaire deja connu du client (jamais de nouveau vocabulaire cree a
    partir d'une transcription -- voir _client_vocabulary)."""
    summary, found = _call_summary(content)
    warnings = [] if found else [{"message": "bloc Probleme/Resolution absent, repli sur le debut du contenu"}]

    if not summary.strip():
        return UNIFIED_EMPTY, warnings + [{"message": "contenu vide"}]

    vocab = _client_vocabulary(client_id)
    if not vocab:
        # Aucun vocabulaire : la passe sur les documents texte n'a pas encore
        # tourne. On n'etiquette pas plutot que d'etiqueter n'importe comment.
        return UNIFIED_EMPTY, warnings + [{"message": "vocabulaire du client vide : indexer les documents texte d'abord"}]

    allowed = sorted(vocab.values())
    prompt = CALL_PROMPT + "\n\nLISTE AUTORISEE : " + ", ".join(allowed)
    key = _cache_key("call", prompt, summary)
    hit = _cache_get(key)
    if hit is not None:
        return hit, warnings

    try:
        raw = _chat(prompt, f"APPEL: {title}\n\n{summary}", CALL_SCHEMA, "call_semantics")
        # Second filtre, mecanique : meme si le modele sort de la liste, on
        # ne garde que ce qui existe reellement dans le vocabulaire.
        kept = []
        for name in raw.get("entities") or []:
            canonical = vocab.get(_norm(name))
            if canonical and canonical not in kept:
                kept.append(canonical)
        payload = {
            "entities": kept,
            "entity_types": [], "aliases": [], "triples": [],
            "audience": "INDETERMINE",
            "call_problem": (raw.get("call_problem") or "").strip(),
            "call_resolution": (raw.get("call_resolution") or "").strip(),
            "call_outcome": raw.get("call_outcome") or "NON_RESOLU",
        }
        _cache_put(key, payload)
        return payload, warnings
    except Exception as exc:
        logging.exception("enrichissement appel impossible")
        return UNIFIED_EMPTY, warnings + [{"message": f"enrichissement indisponible: {type(exc).__name__}"}]


# ==================================================== POINT D'ENTREE UNIQUE
# Appele par le skillset pour TOUT document texte du pipeline natif -- fiches
# du referentiel ET transcriptions audio/video (le pipeline audio/video ecrit
# ses resumes comme de simples fichiers texte dans le meme conteneur Blob,
# distingues uniquement par la metadonnee sourceType). Le routage se fait ICI,
# jamais dans le skillset : Azure AI Search ne sait pas appeler une URI
# differente selon le contenu d'un document (axiome A4 -- une seule logique
# d'orchestration, reutilisee).
@app.function_name(name="enrich")
@app.route(route="enrich", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def enrich(req: func.HttpRequest) -> func.HttpResponse:
    try:
        records = req.get_json().get("values", [])
    except Exception:
        return func.HttpResponse('{"values": []}', status_code=400,
                                 mimetype="application/json")

    out = []
    for rec in records:
        rid, data = rec.get("recordId"), (rec.get("data") or {})
        text = (data.get("text") or "").strip()
        title = data.get("title") or ""
        client_id = data.get("clientId") or ""
        source_type = (data.get("sourceType") or "").strip().lower()

        if not text:
            out.append({"recordId": rid, "data": UNIFIED_EMPTY, "errors": [],
                        "warnings": [{"message": "extrait vide"}]})
            continue

        if source_type in ("audio", "video"):
            payload, warnings = _process_call(text, title, client_id)
        else:
            payload, warnings = _process_doc(text, title)

        out.append({"recordId": rid, "data": payload, "errors": [], "warnings": warnings})

    return func.HttpResponse(json.dumps({"values": out}, ensure_ascii=False),
                             status_code=200, mimetype="application/json")


# Conserve pour test direct / appel manuel (voir README) : meme logique que le
# volet APPEL de enrich(), jamais appelee par le skillset lui-meme.
@app.function_name(name="enrich_call")
@app.route(route="enrich-call", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def enrich_call(req: func.HttpRequest) -> func.HttpResponse:
    try:
        records = req.get_json().get("values", [])
    except Exception:
        return func.HttpResponse('{"values": []}', status_code=400,
                                 mimetype="application/json")

    out = []
    for rec in records:
        rid, data = rec.get("recordId"), (rec.get("data") or {})
        content = (data.get("content") or data.get("text") or "").strip()
        title = data.get("title") or ""
        client_id = data.get("clientId") or ""

        if not content:
            out.append({"recordId": rid, "data": UNIFIED_EMPTY, "errors": [],
                        "warnings": [{"message": "contenu vide"}]})
            continue

        payload, warnings = _process_call(content, title, client_id)
        out.append({"recordId": rid, "data": payload, "errors": [], "warnings": warnings})

    return func.HttpResponse(json.dumps({"values": out}, ensure_ascii=False),
                             status_code=200, mimetype="application/json")
