"""What the Diagnostic writes back into a ServiceNow ticket (V10 slice 6)."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestration"))

import pytest  # noqa: E402

from guide.writeback import handover_action, outcome, request_row, statuses, work_note  # noqa: E402

NOW = datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc)
RULES = [{"action": "password_reset", "label_pattern": "(?i)mot de passe|password"},
         {"action": "not_an_action", "label_pattern": ".*"}]


def view(state="GUIDING", step=1, ticket="INC0010042", approximate=False):
    return {"session_id": "sn-1", "client_id": "client-s", "ticket_id": ticket, "state": state, "current_step": step,
            "messages": [{"role": "user", "text": "Jean Dupont 06 12 34 56 78 n'arrive plus à se connecter"}],
            "guide": {"parent_id": "kefind:r1:KB0120", "title": "KB0120- LOCKED ACCOUNT", "approximate": approximate,
                      "steps": [{"order": 1, "title": "Ouvrir", "instruction": "Ouvrez la console AD.",
                                 "verbatim_from_kb": True},
                                {"order": 2, "title": "Déverrouiller", "instruction": "Cochez Déverrouiller.",
                                 "verbatim_from_kb": True}]}}


def test_the_note_says_fiche_steps_and_outcome_never_what_the_user_typed():
    note = work_note(view(), "Yassine", NOW)
    assert "« KB0120- LOCKED ACCOUNT »" in note and "Référence de la fiche : KB0120" in note
    assert "1. Ouvrez la console AD." in note and "2. Cochez Déverrouiller." in note
    assert "En cours : 1 étape(s) faite(s) sur 2." in note and "Validé par Yassine le 08/10/2026 09:30 UTC." in note
    assert "Jean" not in note and "06 12" not in note


def test_a_reformulated_step_never_reaches_the_ticket():
    v = view()
    v["guide"]["steps"][1] = {"order": 2, "title": "Déverrouiller", "verbatim_from_kb": False,
                              "instruction": "Déverrouillez le compte de Jean Dupont."}
    note = work_note(v, "Y", NOW)
    assert "1. Ouvrez la console AD." in note and "2. (étape reformulée par l'assistant : voir la fiche)" in note
    assert "Jean" not in note


def test_the_reference_is_the_fiche_id_whatever_the_source():
    v = view()
    assert "Référence de la fiche : KB0120\n" in work_note(v, "Y", NOW)
    v["guide"]["parent_id"] = "IDX-42"
    assert "Référence de la fiche : IDX-42\n" in work_note(v, "Y", NOW)


def test_outcomes():
    assert outcome(view(state="SOLVED", step=2)).startswith("Résolu")
    assert outcome(view(step=2)).startswith("Toutes les étapes faites")
    assert "non confirmée" in work_note(view(approximate=True), "Y", NOW)


def test_no_fiche_no_note():
    v = view()
    v["guide"] = None
    assert work_note(v, "Y", NOW) == ""


def test_handover_rules_only_name_actions_of_the_closed_list():
    assert handover_action("KB0300 - Réinitialisation du mot de passe", RULES) == "password_reset"
    assert handover_action("KB0120- LOCKED ACCOUNT", RULES[:1]) is None
    assert handover_action("Quoi que ce soit", [{"action": "not_an_action", "label_pattern": ".*"}]) is None


def test_the_request_row_is_keyed_once_per_session_and_kind():
    row = request_row(view(), "handover", work_note(view(), "Y", NOW), "u1", "Yassine", NOW, "password_reset")
    assert (row["RowKey"], row["ticketNumber"], row["status"], row["kind"]) == ("sn-1-handover", "INC0010042", "validated", "handover")
    assert "action proposée « password_reset »" in row["noteText"]
    assert row["executionStatus"] == "" and row["validatedAtUtc"] == "2026-10-08T09:30:00Z"


def test_a_request_needs_a_ticket_and_a_fiche():
    with pytest.raises(ValueError):
        request_row(view(ticket=None), "work_note", "note", "u", "n", NOW)
    with pytest.raises(ValueError):
        request_row(view(ticket="INC1; DROP"), "work_note", "note", "u", "n", NOW)
    with pytest.raises(ValueError):  # the executor writes incidents only
        request_row(view(ticket="RITM0010042"), "work_note", "note", "u", "n", NOW)
    with pytest.raises(ValueError):
        request_row(view(), "work_note", "", "u", "n", NOW)
    with pytest.raises(ValueError):
        request_row(view(), "close", "note", "u", "n", NOW)


def test_statuses_for_the_page():
    rows = [{"kind": "work_note", "executionStatus": "success"}, {"kind": "handover", "executionStatus": ""}]
    assert statuses(rows) == {"work_note": "écrit dans le ticket", "handover": "validé, en attente de l'exécuteur"}
    assert statuses([{"kind": "work_note", "executionStatus": "inactive"}])["work_note"].startswith("ticket clos")
