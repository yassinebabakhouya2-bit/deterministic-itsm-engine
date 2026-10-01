"""Technical entities of IT fiches, each brought to a canonical id.

Regexes for what has a fixed shape (error codes, event ids, fiche and update
numbers, paths, registry keys, commands, URLs, menu paths, keyboard shortcuts),
a dictionary for applications and operating systems. The same code reads
fiches and tickets, so that "0x80070005" in a ticket meets "-2147024891" in a
fiche as err:0x80070005.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

TECHNICAL_KINDS = frozenset({"error", "event", "kb", "update", "path", "registry", "command", "url", "menu", "shortcut"})


@dataclass(frozen=True)
class Entity:
    kind: str
    text: str
    canonical: str
    start: int
    end: int


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return " ".join(text.lower().replace("'", " ").replace("’", " ").split())


# --- error codes and identifiers --------------------------------------------

HEX_CODE_RE = re.compile(r"(?<![\w-])0x[0-9a-fA-F]{4,8}\b")
NEG_HRESULT_RE = re.compile(r"(?<![\w.])-(2\d{9})\b")
WORD_CODE_RE = re.compile(
    r"(?i)\b(?:code\s+d['’]\s*erreur|erreur|error|code)\s*(?:n°|no\.?|#|:)?\s*(\d{3,5})\b(?!\s*(?:%|mo|go|ko|mb|gb|kb|ms|s\b))"
)
EVENT_RE = re.compile(
    r"(?i)\b(?:event\s*id|eventid|id\s+(?:d['’]\s*)?[ée]v[ée]nement|[ée]v[ée]nement)\s*(?:n°|#|:)?\s*(\d{1,5})\b"
)
SERVICENOW_KB_RE = re.compile(r"\bKB0\d{6,}\b")
WINDOWS_UPDATE_RE = re.compile(r"\bKB[1-9]\d{5,6}\b")
TICKET_RE = re.compile(r"\b(?:INC|RITM|REQ|CHG|PRB|SCTASK|TASK)\d{6,}\b")

# --- paths, registry, URLs ----------------------------------------------------

# A folder name may hold a few spaces ("Program Files"), never a '%' or ':' (that is the next path).
_SEGMENT = r"[^\\\s\"'<>|*?%:]+(?: [^\\\s\"'<>|*?%:]+){0,3}"
_LAST = r"[^\\\s\"'<>|*?%:]*"
DRIVE_PATH_RE = re.compile(rf"\b[A-Za-z]:\\(?:{_SEGMENT}\\)*{_LAST}")
ENV_PATH_RE = re.compile(rf"%[A-Za-z_][\w()]*%(?:\\(?:{_SEGMENT}\\)*{_LAST})?")
UNC_PATH_RE = re.compile(rf"\\\\[\w.$-]+(?:\\(?:{_SEGMENT}\\)*{_LAST})?")
REGISTRY_RE = re.compile(
    rf"\b(?:HKEY_LOCAL_MACHINE|HKEY_CURRENT_USER|HKEY_CLASSES_ROOT|HKEY_USERS|HKEY_CURRENT_CONFIG|HKLM|HKCU|HKCR|HKU|HKCC)"
    rf"(?:\\(?:{_SEGMENT}\\)*{_LAST})?"
)
REGISTRY_ROOTS = {"hklm": "hkey_local_machine", "hkcu": "hkey_current_user", "hkcr": "hkey_classes_root",
                  "hku": "hkey_users", "hkcc": "hkey_current_config"}
URL_RE = re.compile(r"\bhttps?://[^\s<>\"'»)\]]+")

# --- commands -------------------------------------------------------------------

KNOWN_COMMANDS = (
    "ipconfig", "ping", "nslookup", "tracert", "pathping", "netstat", "gpupdate", "gpresult", "sfc", "dism",
    "chkdsk", "netsh", "regedit", "msconfig", "msinfo32", "taskkill", "tasklist", "klist", "dsregcmd", "whoami",
    "systeminfo", "shutdown", "robocopy", "xcopy", "wsreset", "msiexec", "certutil", "winget", "wmic", "bcdedit",
    "diskpart", "manage-bde", "mstsc", "arp", "route", "hostname", "cleanmgr", "control", "appwiz.cpl",
)
_SWITCHES = r"(?:\s+(?:/[\w:.=?-]+|--?[A-Za-z][\w:.=-]*))*"
EXECUTABLE_RE = re.compile(rf"(?i)(?<![\w\\/.-])[\w-]+\.(?:exe|msc|cpl|bat|cmd|ps1|msi|vbs)(?!\w)(?!\.\w){_SWITCHES}")
KNOWN_COMMAND_RE = re.compile(
    rf"(?i)(?<![\w\\/.-])(?:net\s+(?:use|user|time|stop|start|localgroup|view)|{'|'.join(re.escape(c) for c in KNOWN_COMMANDS)})"
    rf"(?![\w-])(?!\.\w){_SWITCHES}"
)
CMDLET_RE = re.compile(
    r"\b(?:Get|Set|New|Remove|Add|Restart|Start|Stop|Test|Reset|Clear|Invoke|Enable|Disable|Update|Install|"
    r"Uninstall|Import|Export|Repair|Resolve|Connect|Disconnect|Grant|Revoke|Unlock)-[A-Z][A-Za-z]+\b"
)

# --- keyboard shortcuts -------------------------------------------------------------

_KEY = r"(?:ctrl|ctl|alt\s*gr|altgr|alt|shift|maj|win(?:dows)?|cmd|command|option|fn|suppr|del(?:ete)?|tab|entr[ée]e|enter|[ée]chap|esc|f\d{1,2}|[a-z0-9])"
SHORTCUT_RE = re.compile(rf"(?i)(?<![\w+])(?:ctrl|ctl|alt\s*gr|altgr|alt|shift|maj|win(?:dows)?|cmd|command|option|fn)(?:\s*\+\s*{_KEY}){{1,3}}(?![\w+])")
FUNCTION_KEY_RE = re.compile(r"(?i)\b(?:touche|appuyez\s+sur|press)\s+(F\d{1,2})\b")
KEY_ALIASES = {"ctl": "ctrl", "maj": "shift", "windows": "win", "suppr": "del", "delete": "del", "entree": "enter",
               "entrée": "enter", "echap": "esc", "échap": "esc", "alt gr": "altgr", "command": "cmd"}

# --- menu paths -------------------------------------------------------------------------

MENU_SEPARATORS = re.compile(r"\s*[>›→]\s*")
_WORD_RE = re.compile(r"[\w'’()&.-]+")
MENU_LEAD_STOPWORDS = frozenset(
    "allez aller rendez vous ouvrez ouvrir cliquez cliquer selectionnez selectionner choisissez choisir naviguez "
    "naviguer accedez acceder puis ensuite dans sur le la les l' du de des au aux en via menu onglet go to open "
    "click select choose navigate the in on then menu tab under".split()
)
MENU_TAIL_STOPWORDS = frozenset("et puis pour afin ou and then to or sinon si if".split())

# --- applications and operating systems -----------------------------------------------

APPS = {
    "outlook": ["outlook", "outlook 365", "ms outlook", "microsoft outlook"],
    "teams": ["teams", "microsoft teams", "ms teams"],
    "word": ["word", "microsoft word", "ms word", "winword"],
    "excel": ["excel", "microsoft excel", "ms excel"],
    "powerpoint": ["powerpoint", "power point"],
    "onedrive": ["onedrive", "one drive"],
    "sharepoint": ["sharepoint", "share point"],
    "office": ["office 365", "microsoft 365", "m365", "o365", "suite office", "pack office", "office"],
    "exchange": ["exchange", "exchange online"],
    "edge": ["microsoft edge", "edge"],
    "chrome": ["google chrome", "chrome"],
    "firefox": ["firefox"],
    "citrix": ["citrix workspace", "citrix receiver", "citrix"],
    "anyconnect": ["cisco anyconnect", "anyconnect", "cisco secure client"],
    "globalprotect": ["globalprotect", "global protect"],
    "forticlient": ["forticlient", "forti client"],
    "zoom": ["zoom"],
    "webex": ["webex"],
    "acrobat": ["adobe acrobat", "acrobat reader", "adobe reader", "acrobat"],
    "sap": ["sap gui", "sapgui", "sap"],
    "servicenow": ["servicenow", "service now"],
    "easyvista": ["easyvista", "easy vista"],
    "active-directory": ["active directory"],
    "entra-id": ["entra id", "azure active directory", "azure ad"],
    "intune": ["intune", "company portal", "portail d'entreprise", "portail entreprise"],
    "bitlocker": ["bitlocker"],
    "defender": ["microsoft defender", "windows defender", "defender"],
    "onenote": ["onenote"],
    "skype": ["skype entreprise", "skype for business", "skype"],
    "vmware": ["vmware horizon", "vmware"],
    "java": ["java"],
}
OPERATING_SYSTEMS = {
    "windows-11": ["windows 11", "win 11", "win11"],
    "windows-10": ["windows 10", "win 10", "win10"],
    "windows-server": ["windows server"],
    "macos": ["macos", "mac os", "os x"],
    "ios": ["ios", "iphone", "ipad"],
    "android": ["android"],
    "linux": ["linux", "ubuntu"],
}


def _dictionary_regex(table: dict[str, list[str]]) -> tuple[re.Pattern, dict[str, str]]:
    alias_to_id: dict[str, str] = {}
    for canonical, aliases in table.items():
        for alias in aliases:
            alias_to_id[alias.lower()] = canonical
    ordered = sorted(alias_to_id, key=len, reverse=True)
    pattern = re.compile(r"(?i)(?<![\w.-])(?:" + "|".join(re.escape(a).replace(r"\ ", r"\s+") for a in ordered) + r")(?![\w-])")
    return pattern, alias_to_id


APP_RE, APP_ALIASES = _dictionary_regex(APPS)
OS_RE, OS_ALIASES = _dictionary_regex(OPERATING_SYSTEMS)

_TRAILING = ".,;:!?)»\"'"


def _trim(text: str, start: int, end: int) -> tuple[str, int, int]:
    while end > start and text[end - 1] in _TRAILING:
        end -= 1
    return text[start:end], start, end


def _hresult_from_negative(value: str) -> str | None:
    """'-2147024891' (a negative 32-bit HRESULT) -> '0x80070005'."""
    number = int(value)
    if not 0 < number <= 2**31:
        return None
    return f"0x{2**32 - number:08X}"


def _menu_entities(text: str) -> list[Entity]:
    found: list[Entity] = []
    offset = 0
    for line in text.split("\n"):
        if any(sep in line for sep in ">›→"):
            found.extend(_menus_in_line(line, offset))
        offset += len(line) + 1
    return found


def _menus_in_line(line: str, offset: int) -> list[Entity]:
    pieces = []
    cursor = 0
    for match in MENU_SEPARATORS.finditer(line):
        pieces.append((cursor, match.start()))
        cursor = match.end()
    pieces.append((cursor, len(line)))
    if len(pieces) < 2:
        return []
    head_start, head_end = pieces[0]
    boundary = max(line.rfind(mark, head_start, head_end) for mark in ",;:.(")
    if boundary >= head_start:
        head_start = boundary + 1
    words_first = list(_WORD_RE.finditer(line, head_start, head_end))[-4:]
    while words_first and (_plain(words_first[0].group()) in MENU_LEAD_STOPWORDS or words_first[0].group().endswith(":")):
        words_first.pop(0)
    if not words_first:
        return []
    start = words_first[0].start()
    labels = [line[start:words_first[-1].end()]]
    end = words_first[-1].end()
    for index, (a, b) in enumerate(pieces[1:], 1):
        is_last = index == len(pieces) - 1
        words = list(_WORD_RE.finditer(line, a, b))
        if not is_last:
            if not words or len(words) > 6:
                break
            labels.append(line[a:b].strip())
            end = b
            continue
        kept = []
        for word in words[:5]:
            token = word.group()
            if _plain(token) in MENU_TAIL_STOPWORDS:
                break
            kept.append(word)
            if line[word.end():word.end() + 1] in ".,;:":
                break
        if kept:
            labels.append(line[kept[0].start():kept[-1].end()])
            end = kept[-1].end()
    if len(labels) < 2 or not any(ch.isalpha() for ch in labels[-1]):
        return []
    raw, s, e = _trim(line, start, end)
    canonical = "menu:" + " > ".join(_plain(label.strip(_TRAILING)) for label in labels)
    return [Entity("menu", raw, canonical, offset + s, offset + e)]


def extract_entities(text: str) -> list[Entity]:
    """All entities of a text, ordered by position."""
    found: list[Entity] = []

    def add(kind: str, match_text: str, canonical: str, start: int, end: int) -> None:
        found.append(Entity(kind, match_text, canonical, start, end))

    for match in HEX_CODE_RE.finditer(text):
        digits = match.group()[2:].upper()
        add("error", match.group(), f"err:0x{digits}", match.start(), match.end())
    for match in NEG_HRESULT_RE.finditer(text):
        code = _hresult_from_negative(match.group(1))
        if code:
            add("error", match.group(), f"err:{code}", match.start(), match.end())
    for match in WORD_CODE_RE.finditer(text):
        add("error", match.group(), f"err:{match.group(1)}", match.start(), match.end())
    for match in EVENT_RE.finditer(text):
        add("event", match.group(), f"evt:{match.group(1)}", match.start(), match.end())
    for match in SERVICENOW_KB_RE.finditer(text):
        add("kb", match.group(), f"kb:{match.group()}", match.start(), match.end())
    for match in WINDOWS_UPDATE_RE.finditer(text):
        add("update", match.group(), f"update:{match.group()}", match.start(), match.end())
    for match in TICKET_RE.finditer(text):
        add("ticket", match.group(), f"ticket:{match.group()}", match.start(), match.end())

    for pattern in (DRIVE_PATH_RE, ENV_PATH_RE, UNC_PATH_RE):
        for match in pattern.finditer(text):
            raw, start, end = _trim(text, match.start(), match.end())
            canonical = "path:" + raw.replace("/", "\\").rstrip("\\").lower()
            add("path", raw, canonical, start, end)
    for match in REGISTRY_RE.finditer(text):
        raw, start, end = _trim(text, match.start(), match.end())
        root, _, rest = raw.partition("\\")
        root = REGISTRY_ROOTS.get(root.lower(), root.lower())
        canonical = "reg:" + (root + ("\\" + rest if rest else "")).rstrip("\\").lower()
        add("registry", raw, canonical, start, end)
    for match in URL_RE.finditer(text):
        raw, start, end = _trim(text, match.start(), match.end())
        scheme, _, rest = raw.partition("://")
        host, slash, path = rest.partition("/")
        add("url", raw, f"url:{scheme.lower()}://{host.lower()}{slash}{path.rstrip('/')}", start, end)

    for pattern in (KNOWN_COMMAND_RE, EXECUTABLE_RE, CMDLET_RE):
        for match in pattern.finditer(text):
            raw, start, end = _trim(text, match.start(), match.end())
            add("command", raw, "cmd:" + " ".join(raw.lower().split()), start, end)

    for match in SHORTCUT_RE.finditer(text):
        keys = [k.strip().lower() for k in match.group().split("+")]
        keys = [KEY_ALIASES.get(" ".join(k.split()), " ".join(k.split())) for k in keys]
        add("shortcut", match.group(), "key:" + "+".join(keys), match.start(), match.end())
    for match in FUNCTION_KEY_RE.finditer(text):
        add("shortcut", match.group(1), f"key:{match.group(1).lower()}", match.start(1), match.end(1))

    found.extend(_menu_entities(text))

    for pattern, aliases, kind in ((APP_RE, APP_ALIASES, "app"), (OS_RE, OS_ALIASES, "os")):
        for match in pattern.finditer(text):
            key = " ".join(match.group().lower().split())
            add(kind, match.group(), f"{kind}:{aliases[key]}", match.start(), match.end())

    unique: dict[tuple[str, int, int], Entity] = {}
    for entity in found:
        unique.setdefault((entity.canonical, entity.start, entity.end), entity)
    ordered = sorted(unique.values(), key=lambda e: (e.start, -(e.end - e.start), e.kind))
    containers = [e for e in ordered if e.kind in ("path", "registry", "url")]
    ordered = [
        e for e in ordered
        if e.kind not in ("app", "os") or not any(c.start <= e.start and e.end <= c.end for c in containers)
    ]
    return _drop_nested(ordered)


def _drop_nested(entities: list[Entity]) -> list[Entity]:
    """Keep 'outlook.exe /safe' and drop the bare 'outlook.exe' inside it (same kind only)."""
    kept: list[Entity] = []
    for entity in entities:
        if any(o.kind == entity.kind and o.start <= entity.start and entity.end <= o.end and o != entity for o in kept):
            continue
        kept.append(entity)
    return kept


def canonical_set(text: str, kinds: frozenset[str] | None = None) -> set[str]:
    return {e.canonical for e in extract_entities(text) if kinds is None or e.kind in kinds}


def novel_technical_entities(candidate: str, reference: str) -> list[str]:
    """Technical entities named in ``candidate`` that ``reference`` does not contain.

    Used on any rewording of a step: it may not add a command, a path, a menu,
    a key, an error code or a fiche number that the step itself does not have.
    """
    reference_entities = canonical_set(reference, TECHNICAL_KINDS)
    reference_plain = _plain(reference)
    novel = []
    for entity in extract_entities(candidate):
        if entity.kind not in TECHNICAL_KINDS or entity.canonical in reference_entities:
            continue
        if entity.kind == "menu":
            labels = entity.canonical[len("menu:"):].split(" > ")
            if all(label in reference_plain for label in labels):
                continue
        if entity.canonical not in novel:
            novel.append(entity.canonical)
    return novel
