"""Rules: the sections and the steps of a fiche, from its layout and wording.

This is the deterministic half of the double decomposition. The other half is
the LLM (llm_segment.py); a fiche is trusted only when the two agree.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

ROLES = ("symptom", "cause", "prerequisite", "resolution", "workaround", "escalation", "info", "title", "other")

SECTION_KEYWORDS = {
    "symptom": [
        "symptome", "symptomes", "symptom", "symptoms", "description", "description du probleme", "probleme",
        "problem", "issue", "contexte", "context", "constat", "incident", "manifestation", "message d'erreur",
        "comportement", "situation", "resume", "summary", "apercu", "overview",
    ],
    "cause": ["cause", "causes", "origine", "root cause", "explication", "raison", "analyse"],
    "prerequisite": [
        "prerequis", "pre-requis", "pre requis", "prerequisites", "prerequisite", "avant de commencer",
        "conditions prealables", "requirements", "conditions", "pre-requisites",
    ],
    "resolution": [
        "resolution", "solution", "solutions", "procedure", "mode operatoire", "etapes", "marche a suivre",
        "actions", "actions a realiser", "actions a mener", "steps", "fix", "correctif", "instructions",
        "traitement", "operations", "methode", "que faire", "depannage", "manipulation", "manipulations",
        # headings that open fallback steps; the steps under them get the heading as their condition
        "si le probleme persiste", "en cas d'echec", "si cela ne fonctionne pas", "if the problem persists",
    ],
    "workaround": ["contournement", "solution de contournement", "workaround", "palliatif", "solution temporaire"],
    "escalation": ["escalade", "escalation", "support", "contact", "contacts", "assistance"],
    "info": [
        "informations", "information", "informations complementaires", "remarque", "remarques", "note", "notes",
        "a savoir", "references", "reference", "liens", "voir aussi", "see also", "important", "attention",
        "avertissement", "warning", "environnement", "environment", "perimetre", "scope",
    ],
    "title": ["titre", "title", "objet", "sujet"],
}


def plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return " ".join(text.lower().replace("’", "'").split())


_KEYWORDS = {plain(keyword): role for role, keywords in SECTION_KEYWORDS.items() for keyword in keywords}
_NUMBERING_RE = re.compile(r"^(?:\d{1,2}|[ivx]{1,4}|[a-z])\s*[.)/-]\s+")


def heading_key(text: str) -> str:
    key = plain(text.strip().strip("#*_ ").rstrip(":").strip())
    key = _NUMBERING_RE.sub("", key)
    return key.strip(" :.-")


def keyword_role(key: str) -> str | None:
    """Role of a heading from the keyword table: exact match, then its first words."""
    if key in _KEYWORDS:
        return _KEYWORDS[key]
    words = key.split()
    for size in range(min(len(words), 4), 0, -1):
        prefix = " ".join(words[:size])
        if prefix in _KEYWORDS:
            return _KEYWORDS[prefix]
    return None


# --- sections ---------------------------------------------------------------------------

MD_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
BOLD_LINE_RE = re.compile(r"^\s*(\*\*|__)(.+?)\1\s*:?\s*$")
LABEL_RE = re.compile(r"^\s*([A-Za-zÀ-ÖØ-öø-ÿ'’ /-]{3,40}?)\s*:\s*(.*)$")


@dataclass
class Section:
    title: str | None
    key: str
    role: str
    start: int
    end: int
    heading_start: int | None = None
    title_span: tuple[int, int] | None = None

    def to_dict(self) -> dict:
        return {"title": self.title, "role": self.role, "start": self.start, "end": self.end}


@dataclass
class Heading:
    title: str
    key: str
    body_start: int  # where the section's text starts (after an inline label, or the next line)
    explicit: bool  # markdown or bold heading, whatever its words


def detect_heading(line: str, line_start: int, line_end: int, role_lookup) -> Heading | None:
    match = MD_HEADING_RE.match(line)
    if match:
        return Heading(match.group(2), heading_key(match.group(2)), line_end, True)
    match = BOLD_LINE_RE.match(line)
    if match:
        return Heading(match.group(2), heading_key(match.group(2)), line_end, True)
    if line[:1] in (" ", "\t"):
        return None  # an indented line continues a list item
    match = LABEL_RE.match(line)
    if match:
        key = heading_key(match.group(1))
        role = role_lookup(key)
        inline = bool(match.group(2).strip())
        # "Remarque : ..." inside a procedure is a note, not the start of a new section
        if role is not None and not (inline and role == "info"):
            body = match.start(2) if inline else len(line)
            return Heading(match.group(1), key, line_start + body if body < len(line) else line_end, False)
        if inline:
            return None  # "Environnement : Windows 10" is a line of content
    stripped = line.strip()
    if stripped and len(stripped) <= 60 and not stripped.endswith((".", "!", "?", ";", ",")):
        key = heading_key(stripped)
        if key and role_lookup(key) is not None:
            return Heading(stripped.rstrip(":").strip(), key, line_end, False)
        letters = [c for c in stripped if c.isalpha()]
        if len(letters) >= 4 and all(c.isupper() for c in letters) and len(stripped.split()) <= 6:
            return Heading(stripped, heading_key(stripped), line_end, True)
    return None


def iter_lines(text: str):
    position = 0
    for line in text.split("\n"):
        yield line, position, position + len(line)
        position += len(line) + 1


def split_sections(text: str, role_lookup=keyword_role) -> list[Section]:
    sections: list[Section] = []
    current = Section(None, "", "preamble", 0, len(text))
    for line, start, end in iter_lines(text):
        heading = detect_heading(line, start, end, role_lookup)
        if heading is None:
            continue
        current.end = start
        sections.append(current)
        role = role_lookup(heading.key) or "other"
        body_start = min(heading.body_start + (1 if heading.body_start == end else 0), len(text))
        title = heading.title.strip()
        offset = line.find(title)
        title_span = (start + offset, start + offset + len(title)) if offset >= 0 else None
        current = Section(title, heading.key, role, body_start, len(text), heading_start=start, title_span=title_span)
    current.end = len(text)
    sections.append(current)
    sections = [s for s in sections if s.heading_start is not None or text[s.start:s.end].strip()]
    if len(sections) == 1 and sections[0].heading_start is None:
        sections[0].role = "unknown"
    return sections


# --- steps -----------------------------------------------------------------------------------

MARKER_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>(?:[EÉeé]tape|ETAPE|ÉTAPE|[Ss]tep|STEP)\s*(?P<word_number>\d{1,2})\s*[:.)\-–]?"
    r"|(?P<number>\d{1,2})[.)](?!\d)|[a-zA-Z][.)](?=\s)|[-*•–·▪●◦➢➤►✓✔])\s+(?P<body>\S.*)$"
)

_ER_VERBS = """
cliquer ouvrir fermer selectionner choisir redemarrer relancer verifier controler tester consulter regarder
observer confirmer identifier demander noter saisir taper entrer appuyer lancer executer aller supprimer
desinstaller installer reinstaller connecter deconnecter reconnecter copier coller renommer vider reinitialiser
desactiver activer modifier configurer reconfigurer patienter attendre eteindre allumer rallumer debrancher
rebrancher brancher retirer inserer remplacer mettre effacer ajouter creer cocher decocher valider contacter
essayer naviguer acceder rechercher telecharger synchroniser forcer purger nettoyer deverrouiller reparer utiliser
demarrer arreter quitter positionner placer glisser deplacer imprimer scanner partager exporter importer
sauvegarder enregistrer restaurer basculer indiquer repondre signaler accepter refuser autoriser bloquer
debloquer mettre_a_jour actualiser rafraichir recharger reessayer recommencer reprendre verrouiller
deployer joindre envoyer transferer ouvrir_session fermer_session faire suivre derouler developper
""".split()
_IMPERATIVES = """
cliquez ouvrez fermez selectionnez choisissez redemarrez relancez verifiez controlez testez consultez regardez
observez confirmez identifiez demandez notez saisissez tapez entrez appuyez lancez executez allez rendez supprimez
desinstallez installez reinstallez connectez deconnectez reconnectez copiez collez renommez videz reinitialisez
desactivez activez modifiez configurez reconfigurez patientez attendez eteignez allumez rallumez debranchez
rebranchez branchez retirez inserez remplacez mettez effacez ajoutez creez cochez decochez validez contactez
essayez naviguez accedez recherchez telechargez synchronisez forcez purgez nettoyez deverrouillez reparez utilisez
demarrez arretez quittez positionnez placez glissez deplacez imprimez scannez partagez exportez importez
sauvegardez enregistrez restaurez basculez indiquez repondez signalez acceptez refusez autorisez bloquez
debloquez actualisez rafraichissez rechargez reessayez recommencez reprenez verrouillez deployez joignez
envoyez transferez faites suivez deroulez developpez assurez fermez-le ouvrez-le relancez-le
""".split()
_ENGLISH = """
click open close select choose restart reboot relaunch verify check ensure confirm test look see identify ask
note type enter press launch run go navigate delete remove uninstall install reinstall connect disconnect
reconnect copy paste rename clear empty reset disable enable modify change configure wait turn switch unplug
plug insert replace add create tick untick validate contact try access search download sync force purge clean
unlock repair use start stop quit save restore update refresh reload retry sign log make
""".split()
ACTION_VERBS = frozenset(v.replace("_", " ") for v in _ER_VERBS) | frozenset(_IMPERATIVES) | frozenset(_ENGLISH) | {"s'assurer", "assurez-vous"}
CHECK_VERBS = frozenset(
    """verifier verifiez controler controlez tester testez consulter consultez regarder regardez observer observez
    confirmer confirmez identifier identifiez demander demandez noter notez assurez assurez-vous s'assurer
    check verify ensure confirm test look see identify ask note""".split()
)
LEADING_FILLERS = (
    "tout d'abord", "dans un premier temps", "dans un second temps", "pour commencer", "si besoin", "au besoin",
    "ensuite", "puis", "enfin", "d'abord", "egalement", "aussi", "alors", "maintenant", "then", "next", "finally",
    "first", "also", "now",
)
# "Dans Outlook, cliquez sur ..." opens with a place or a moment; "Pour toute question, ..." does not.
LEADING_PLACES = frozenset(
    "dans sur depuis via une apres avant au a partir lorsque des une_fois from in on after once within under".split()
)
CONDITION_RE = re.compile(
    r"(?i)^\s*(?:et\s+|mais\s+|ou\s+)?(?:si\b|s['’]ils?\b|lorsqu['’]|lorsque\b|quand\b|dans le cas o[uù]\b|"
    r"en cas d['’]|en cas de\b|au cas o[uù]\b|sinon\b|if\b|when\b|in case\b|unless\b|sauf si\b|otherwise\b)"
    r"[^,:;\n]{0,160}?(?=\s*[,:;]|\s+(?:alors|then)\b)"
)
FAILURE_RE = re.compile(
    r"(?i)^\s*(?:"
    r"si (?:le|ce|cela|ça|le même|l['’]?)\s*(?:probl[eè]me|souci|dysfonctionnement|incident|message|erreur|blocage|comportement)\s+(?:persiste|subsiste|r[ée]appara[iî]t|continue|est toujours)"
    r"|si (?:cela|ça|ceci|rien) ne (?:fonctionne|marche|change|r[ée]sout|r[eè]gle|suffit)"
    r"|si (?:cette|la|l['’])\s*(?:[ée]tape|manipulation|solution|op[ée]ration|action|proc[ée]dure) (?:ne fonctionne pas|[ée]choue|n['’]a pas fonctionn[ée]|ne suffit pas|ne r[ée]sout pas)"
    r"|si (?:ce n['’]est pas|toujours pas|vous n['’]arrivez (?:toujours )?pas|l['’]utilisateur n['’]arrive (?:toujours )?pas)"
    r"|en cas d['’][ée]chec|sinon"
    r"|if (?:the |this )?(?:problem|issue|error) persists|if (?:this|that|it) (?:does not|doesn['’]t) (?:work|help)"
    r"|if (?:it|this|that) fails|otherwise"
    r")"
)
GOTO_RE = re.compile(
    r"(?i)\b(?:passez|passer|allez|aller|rendez-vous|retournez|retourner|reprenez|reprendre|revenez|recommencez|recommencer|"
    r"refaites|refaire|r[ée]p[ée]tez|r[ée]p[ée]ter|go|proceed|return|skip|continue|repeat|redo)"
    r"\s+(?:directement\s+)?(?:(?:à|a|au|to|with)\s+)?(?:l['’]\s*)?(?:[ée]tape|step)\s+(?:n°\s*)?(\d{1,2})\b"
)
SENTENCE_END_RE = re.compile(r"(?<=[.!?;])\s+(?=[\"«(]?[A-ZÀ-ÖØ-Þ0-9])")
_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+")


def first_verb(text: str) -> str | None:
    """The verb that opens a step, after fillers, a condition or a leading complement."""
    sentence = MARKER_RE.sub(r"\g<body>", text.strip(), count=1) if MARKER_RE.match(text) else text.strip()
    candidates = [sentence]
    condition = CONDITION_RE.match(sentence)
    if condition:
        candidates.insert(0, sentence[condition.end():].lstrip(" ,:;"))
    comma = sentence.find(",")
    if 0 < comma <= 60 and plain(sentence[:comma]).split(" ")[0] in LEADING_PLACES:
        candidates.append(sentence[comma + 1:])
    for candidate in candidates:
        lowered = plain(candidate)
        for filler in LEADING_FILLERS:
            if lowered.startswith(filler + " ") or lowered.startswith(filler + ","):
                lowered = lowered[len(filler):].lstrip(" ,")
                break
        if lowered.startswith("alors "):
            lowered = lowered[6:]
        match = _WORD_RE.match(lowered)
        if not match:
            continue
        word = match.group().lower().strip("'-")
        if word.startswith("s'") and word not in ACTION_VERBS:
            word = word[2:]
        base = word.split("-")[0]
        for form in (word, base):
            if form in ACTION_VERBS:
                return form
        if lowered.startswith("make sure") or lowered.startswith("mettre a jour"):
            return lowered.split()[0]
    return None


def step_kind(text: str, role: str) -> str:
    if role == "prerequisite":
        return "check"
    verb = first_verb(text)
    if verb in CHECK_VERBS or text.rstrip().endswith("?") or plain(text).startswith("est-ce que"):
        return "check"
    return "action"


@dataclass
class RuleStep:
    start: int
    end: int
    role: str
    level: int = 0
    number: int | None = None
    from_list: bool = True
    condition: tuple[int, int] | None = None
    after_failure: bool = False
    goto: int | None = None
    kind: str = "action"
    extra: dict = field(default_factory=dict)


def _annotate(step: RuleStep, text: str) -> RuleStep:
    body = text[step.start:step.end]
    match = CONDITION_RE.match(body)
    if match:
        offset = len(match.group()) - len(match.group().lstrip())
        step.condition = (step.start + match.start() + offset, step.start + match.end())
        if FAILURE_RE.match(match.group()):
            step.after_failure = True
    elif FAILURE_RE.match(body):
        step.after_failure = True
    goto = GOTO_RE.search(body)
    if goto:
        step.goto = int(goto.group(1))
    step.kind = step_kind(body, step.role)
    return step


def _list_steps(text: str, section: Section) -> tuple[list[RuleStep], list[tuple[int, int]]]:
    """List items of a section, and the blocks of its other lines (paragraphs between items)."""
    steps: list[RuleStep] = []
    blocks: list[tuple[int, int]] = []
    current: RuleStep | None = None
    block: list[int] | None = None
    body = text[section.start:section.end]
    for line, start, end in iter_lines(body):
        start += section.start
        end = start + len(line.rstrip())
        if not line.strip():
            current = None
            block = None
            continue
        match = MARKER_RE.match(line)
        if match:
            block = None
            number = match.group("number") or match.group("word_number")
            indent = len(match.group("indent").expandtabs(4))
            current = RuleStep(
                start=start + match.start("body"),
                end=end,
                role=section.role,
                level=min(indent // 2, 3),
                number=int(number) if number else None,
            )
            steps.append(current)
        elif current is not None:
            current.end = end
        elif block is not None:
            block[1] = end
        else:
            block = [start + len(line) - len(line.lstrip()), end]
            blocks.append(block)
    return steps, [(a, b) for a, b in blocks]


def _sentence_steps(text: str, blocks: list[tuple[int, int]], role: str) -> list[RuleStep]:
    """Sentences that open with an instruction verb, inside paragraphs."""
    steps = []
    for block_start, block_end in blocks:
        chunk = text[block_start:block_end]
        cursor = 0
        pieces = []
        for match in SENTENCE_END_RE.finditer(chunk):
            pieces.append((cursor, match.start()))
            cursor = match.end()
        pieces.append((cursor, len(chunk)))
        for a, b in pieces:
            sentence = chunk[a:b]
            stripped = sentence.strip()
            if len(stripped) < 4 or first_verb(stripped) is None:
                continue
            lead = len(sentence) - len(sentence.lstrip())
            steps.append(
                RuleStep(start=block_start + a + lead, end=block_start + a + len(sentence.rstrip()), role=role, from_list=False)
            )
    return steps


def candidate_sections(sections: list[Section]) -> list[Section]:
    """Sections that may hold steps: the resolution-like ones when the fiche has some."""
    if any(s.role in ("resolution", "workaround") for s in sections):
        allowed = {"resolution", "workaround", "prerequisite", "escalation"}
    else:
        allowed = {"resolution", "workaround", "prerequisite", "escalation", "other", "unknown", "preamble"}
    return [s for s in sections if s.role in allowed]


def rule_steps(text: str, sections: list[Section]) -> list[RuleStep]:
    steps: list[RuleStep] = []
    for section in candidate_sections(sections):
        listed, blocks = _list_steps(text, section)
        found = sorted(listed + _sentence_steps(text, blocks, section.role), key=lambda s: s.start)
        for step in found:
            _annotate(step, text)
        title = section.title or ""
        if found and section.title_span and (CONDITION_RE.match(title + ",") or FAILURE_RE.match(title)):
            # "Si le problème persiste :" as a heading: the condition of every step below it
            for step in found:
                if step.condition is None:
                    step.condition = section.title_span
            if FAILURE_RE.match(title):
                found[0].after_failure = True
        steps.extend(found)
    steps.sort(key=lambda s: s.start)
    return steps


RESOLUTION_ROLES = frozenset({"resolution", "workaround", "other", "unknown", "preamble"})
