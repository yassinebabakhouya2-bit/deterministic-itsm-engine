"""Fiche identifiers: shared with the engine, see kecore.ids."""

from kecore.ids import (
    DEFAULT_FICHE_REGEX,
    DOC_EXTENSIONS,
    compile_pattern,
    extract_fiche_id,
    strip_document_path,
)

__all__ = ["DEFAULT_FICHE_REGEX", "DOC_EXTENSIONS", "compile_pattern", "extract_fiche_id", "strip_document_path"]
