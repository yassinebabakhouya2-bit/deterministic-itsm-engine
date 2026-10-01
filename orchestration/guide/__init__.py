"""Guided resolution engine (v2 of the diagnostic tab).

Pipeline: find the EXACT KB fiche -> show an elegant summary of its resolution
steps -> walk the user through the steps one by one, helping on each blocked
step from the fiche only, until the problem is solved. There is no escalation:
the session ends when the user confirms the resolution; when a fiche does not
solve it, the next candidate fiche is proposed.

Pure state machine (fsm.py): the LLM only reads (extraction, OCR), judges
between candidate fiches, drafts the guide and words the help; the transitions
are plain code, so the same events and port outputs give the same states."""
