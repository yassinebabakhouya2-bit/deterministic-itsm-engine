"""Lire/écrire des fiches décomposées en JSONL (le format de
``kecore.decompose.DecomposedFiche.to_dict()``, une fiche par ligne) —
l'équivalent de ``kecore.fiches.load_jsonl``/``save_fiches`` pour les fiches
déjà décomposées.
"""

from __future__ import annotations

import json
from pathlib import Path

from kecore.decompose import DecomposedFiche
from kecore.errors import InputError


def load_decomposed_jsonl(path: str | Path) -> list[DecomposedFiche]:
    path = Path(path)
    fiches: list[DecomposedFiche] = []
    try:
        handle_text = path.open(encoding="utf-8-sig")
    except FileNotFoundError:
        raise InputError(f"not found: {path}") from None
    with handle_text as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                fiches.append(DecomposedFiche.from_dict(data))
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise InputError(f"{path}:{lineno}: invalid decomposed fiche ({exc})") from None
    return fiches


def save_decomposed_jsonl(path: str | Path, fiches: list[DecomposedFiche]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for fiche in fiches:
            handle.write(json.dumps(fiche.to_dict(), ensure_ascii=False) + "\n")


__all__ = ["load_decomposed_jsonl", "save_decomposed_jsonl"]
