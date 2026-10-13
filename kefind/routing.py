"""Deterministic routing at runtime: the routing tables and the code that reads them (levels L0 to L2).

The routing spec (docs/v10-deterministic-engine.md, "Semantic enrichment at ingestion, deterministic
routing at runtime") moves the meaning upstream: a person validates, per fiche, an intent of the client's
closed taxonomy, an application and a few short requests ("aliases"); this module turns those validated
decisions into tables, and reads a question against them, code only:

- L0, an identifier: a fiche number the question cites (``KB0233``) that only one fiche carries, or an
  error / event code only one validated fiche knows;
- L1, a validated alias: every routing token of the alias is among the question's routing tokens; the
  most specific alias wins (its tokens include every other match's), else nothing is decided here;
- L2, (intent, application): the question mapped onto the closed enum by a recorded model call
  (``kefind.route``), then looked up here.

This first part is the L1 normalization, frozen and versioned (``NORMALIZATION_VERSION``): the same text
always gives the same tokens, at ingestion (aliases) and at question time. It is deliberately NOT
``kefind.search.tokenize`` (shared with BM25F and the graph's shingles: changing one must never change the
other) nor ``kefind.semantic.normalize_text`` (the embedding and record key).
"""

from __future__ import annotations

import re
import unicodedata

NORMALIZATION_VERSION = 1

# Characters NFKD does not decompose: "mise en œuvre" must not become "mise en uvre".
_LIGATURES = str.maketrans({"œ": "oe", "Œ": "oe", "æ": "ae", "Æ": "ae", "ß": "ss", "ø": "o", "Ø": "o",
                            "đ": "d", "Đ": "d", "ł": "l", "Ł": "l"})
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Frozen grammar words (folded: no accent, lower case). Only words that never carry the need itself:
# negations ("pas", "not", "no", "plus", "aucun") stay tokens on purpose. Changing this list changes
# NORMALIZATION_VERSION, every table's sha256, and so what a person validated: never edit it in place.
STOP_WORDS = frozenset("""
a ai as au aux avec c ce ces cet cette comment d dans de des dois doit du elle elles en est et etre
faut il ils j je l la le les leur leurs lui m ma me mes moi mon n ne nos notre nous on ont ou par peut
peux pour pouvez puis qu quand que quel quelle quelles quels qui quoi s sa se ses si son sont sur suis
svp t ta te tes toi ton tu un une vos votre vous y bonjour merci
an and are at be by can could do does for from hello hi how i in is it its me my of on or please
that the this to what when where why with you your
""".split())


def fold_token(token: str) -> str:
    """The frozen inflection rule (version 1), applied the same way to aliases and questions:
    a final "s" goes (not "ss", "us", "is"; 4 letters at least: "comptes" -> "compte", "mots" -> "mot",
    "teams" -> "team"), then a final "ee" loses one "e" ("bloquee" -> "bloque"). Digits never change.
    Symmetric, so it only has to make two spellings of one word meet, never be good French."""
    if token.isdigit():
        return token
    if len(token) >= 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        token = token[:-1]
    if len(token) >= 4 and token.endswith("ee"):
        token = token[:-1]
    return token


def _fold_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").translate(_LIGATURES))
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def route_tokens(text: str) -> tuple[str, ...]:
    """The routing tokens of ``text``, in order of first appearance, each once: folded (accents, case,
    ligatures), split on anything but [a-z0-9], one-character tokens and stop words dropped, inflections
    folded (``fold_token``)."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(_fold_text(text)):
        if len(raw) < 2 or raw in STOP_WORDS:
            continue
        token = fold_token(raw)
        if token in STOP_WORDS or len(token) < 2:
            continue
        if token not in out:
            out.append(token)
    return tuple(out)


def token_key(text: str) -> tuple[str, ...]:
    """The sorted routing tokens: two aliases with the same key are the same alias for L1."""
    return tuple(sorted(route_tokens(text)))


__all__ = ["NORMALIZATION_VERSION", "STOP_WORDS", "fold_token", "route_tokens", "token_key"]
