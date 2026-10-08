import unittest

import csv
import io
from collections import Counter

from kecore.tickets import MAX_FIELD_CHARS, scrub, scrub_entity, ticket_text


HEADER = (
    '"Date d\'émission";"N° de ticket";"N° d\'origine";"Priorité";"Criticité";'
    '"Impact";"Statut";"Meta Statut";"Cause réelle";"Titre";"Sujet";"Application / Service";'
    '"Description";"Bénéficiaire";"Demandeur";"Intervenant en cours";"Groupe en cours";'
    '"Entité complète";"Localisation complète";"Groupe responsable du sujet";"SLA";'
    '"Date de résolution";"Numéro SR";"Référence externe";"Enregistré par";"Origine";'
    '"Résolution";"Groupe de résolution";"Dernière modification";"1er groupe d\'affectation";'
    '"Résolu par (intervenant)";"Sujet complet"'
)
WIDTH_ROW = "-;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8"


def row(ticket_id="I261001_1220", beneficiary="Jean Dupont", description="Problème de connexion"):
    cols = [
        "01/10/2026 18:16:09", ticket_id, "", "4", "03 - Moyenne", "03 - Région", "A prendre en compte",
        "En cours", "", "INSTALLATION UNIFLOW", "Ouverture par email", "Uniflow", description,
        beneficiary, "Marie Martin", "Paul Durand", "Groupe A", "Entité X", "Site Y", "Groupe B",
        "4h", "", "", "", "Alice Technicienne", "Portail", "Résolu, voir KB0120", "Groupe C",
        "02/10/2026", "Groupe D", "Bob Résolveur", "Sujet complet",
    ]
    return ";".join(f'"{c}"' for c in cols)


def csv_bytes(*rows):
    lines = [HEADER, WIDTH_ROW, *rows]
    return ("\n".join(lines)).encode("utf-8-sig")


class ScrubTest(unittest.TestCase):
    def test_a_real_row_is_kept(self):
        tickets, report = scrub(csv_bytes(row()))
        self.assertEqual(report.tickets, 1)
        self.assertEqual(tickets[0].id, "I261001_1220")

    def test_the_width_row_is_not_a_ticket(self):
        tickets, report = scrub(csv_bytes(row()))
        self.assertEqual(report.rows_read, 2)
        self.assertEqual(report.skipped, 1)

    def test_names_are_dropped_not_masked(self):
        tickets, _ = scrub(csv_bytes(row(beneficiary="Jean Dupont")))
        entity = tickets[0].to_entity("client-s")
        self.assertNotIn("Jean", str(entity.values()))
        self.assertNotIn("b_n_ficiaire", entity)
        self.assertNotIn("beneficiaire", entity)

    def test_an_email_in_free_text_is_masked(self):
        tickets, report = scrub(csv_bytes(row(description="Contactez-moi sur jean.dupont@example.com")))
        self.assertIn("[email]", tickets[0].fields["Description"])
        self.assertNotIn("jean.dupont@example.com", tickets[0].fields["Description"])
        self.assertEqual(report.masked.get("[email]"), 1)

    def test_a_phone_number_in_free_text_is_masked(self):
        tickets, _ = scrub(csv_bytes(row(description="Rappelez au 06 12 34 56 78 svp")))
        self.assertIn("[phone]", tickets[0].fields["Description"])
        self.assertNotIn("06 12 34 56 78", tickets[0].fields["Description"])

    def test_a_duplicate_ticket_id_counts_once(self):
        tickets, report = scrub(csv_bytes(row(), row()))
        self.assertEqual(report.tickets, 1)
        self.assertEqual(report.skipped, 2)  # the width row, then the repeated ticket id

    def test_text_joins_title_subject_description(self):
        tickets, _ = scrub(csv_bytes(row(description="Détail du problème")))
        text = tickets[0].text()
        self.assertIn("INSTALLATION UNIFLOW", text)
        self.assertIn("Détail du problème", text)

    def test_entity_keys_are_valid_table_property_names(self):
        tickets, _ = scrub(csv_bytes(row()))
        entity = tickets[0].to_entity("client-s")
        for key in entity:
            self.assertRegex(key, r"^[A-Za-z_][A-Za-z0-9_]*$")

    def test_embedded_newline_in_a_quoted_field_does_not_split_the_row(self):
        multiline_row = row(description="Ligne 1\nLigne 2")
        tickets, report = scrub(csv_bytes(multiline_row))
        self.assertEqual(report.tickets, 1)
        self.assertIn("Ligne 1", tickets[0].fields["Description"])
        self.assertIn("Ligne 2", tickets[0].fields["Description"])

    def test_resolution_text_is_kept_scrubbed_not_dropped(self):
        tickets, _ = scrub(csv_bytes(row()))
        self.assertIn("KB0120", tickets[0].fields["Résolution"])


COLUMNS = next(csv.reader(io.StringIO(HEADER), delimiter=";"))


def export(**values):
    """A one-ticket export where any column can be set by name (accents and all)."""
    base = dict(zip(COLUMNS, next(csv.reader(io.StringIO(row()), delimiter=";"))))
    base.update(values)
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(COLUMNS)
    out.write(WIDTH_ROW + "\n")
    writer.writerow([base[c] for c in COLUMNS])
    return out.getvalue().encode("utf-8-sig")


def field_of(data, column):
    tickets, report = scrub(data)
    return tickets[0].fields[column], report


class CleaningTest(unittest.TestCase):
    def test_every_free_text_column_is_scrubbed_not_only_the_ticket_text(self):
        value, report = field_of(export(**{"Résolution": "Fait, prévenu jean.dupont@example.com"}), "Résolution")
        self.assertEqual(value, "Fait, prévenu [email]")
        value, _ = field_of(export(**{"Cause réelle": "Rappeler le 06 12 34 56 78"}), "Cause réelle")
        self.assertEqual(value, "Rappeler le [phone]")

    def test_structured_columns_are_kept_exactly(self):
        data = export(**{"Date d'émission": "01/10/2026 18:16:09", "Numéro SR": "0612345678"})
        tickets, _ = scrub(data)
        self.assertEqual(tickets[0].fields["Date d'émission"], "01/10/2026 18:16:09")
        self.assertEqual(tickets[0].fields["Numéro SR"], "0612345678")

    def test_the_signature_is_cut_from_the_closing_formula_on(self):
        text = "Mon PC ne démarre plus.\nCordialement,\nJean Dupont\nTél : 06 12 34 56 78"
        value, report = field_of(export(Description=text), "Description")
        self.assertEqual(value, "Mon PC ne démarre plus.")
        self.assertEqual(report.masked.get("[signature]"), 1)
        self.assertNotIn("[phone]", report.masked)

    def test_a_closing_word_inside_a_sentence_cuts_nothing(self):
        text = "Merci beaucoup pour votre aide, le problème persiste.\nL'écran reste noir."
        value, _ = field_of(export(Description=text), "Description")
        self.assertEqual(value, text)

    def test_a_text_that_is_only_a_formula_is_not_emptied(self):
        value, _ = field_of(export(Description="Cordialement"), "Description")
        self.assertEqual(value, "Cordialement")

    def test_the_name_after_a_greeting_is_replaced(self):
        value, report = field_of(export(Description="Bonjour Jean,\nMon écran clignote."), "Description")
        self.assertEqual(value, "Bonjour [nom],\nMon écran clignote.")
        self.assertEqual(report.masked.get("[nom]"), 1)

    def test_mentions_are_masked_and_e_mails_are_not_broken(self):
        value, report = field_of(export(Description="Voir avec @Jean Dupont ou jean.dupont@example.com"), "Description")
        self.assertEqual(value, "Voir avec [mention] ou [email]")
        self.assertEqual(report.masked.get("[mention]"), 1)

    def test_a_very_long_value_is_cut_to_the_table_limit(self):
        value, report = field_of(export(Description="x" * (MAX_FIELD_CHARS + 50)), "Description")
        self.assertEqual(len(value), MAX_FIELD_CHARS)
        self.assertEqual(report.masked.get("[truncated]"), 1)


class StoredRowTest(unittest.TestCase):
    def stored(self, **values):
        entity = {"PartitionKey": "client-s", "RowKey": "I1", "titre": "Ecran noir", "sujet": "Poste",
                  "description": "Ecran noir au démarrage", "date_d_emission": "01/10/2026 18:16:09",
                  "kefind_kind": "fiche", "kefind_candidates": '["KB0001"]'}
        entity.update(values)
        return entity

    def test_a_row_scrubbed_before_the_fix_is_cleaned_in_place(self):
        counts = Counter()
        changes = scrub_entity(self.stored(resolution="Prévenu jean.dupont@example.com"), counts)
        self.assertEqual(changes, {"resolution": "Prévenu [email]"})

    def test_cleaning_twice_changes_nothing_the_second_time(self):
        entity = self.stored(description="Bonjour Paul,\nEcran noir.\nCordialement\nPaul 06 12 34 56 78")
        first = scrub_entity(entity, Counter())
        entity.update(first)
        self.assertEqual(scrub_entity(entity, Counter()), {})
        self.assertEqual(entity["description"], "Bonjour [nom],\nEcran noir.")

    def test_keys_dates_and_machine_fields_are_never_touched(self):
        changes = scrub_entity(self.stored(), Counter())
        self.assertEqual(changes, {})

    def test_a_person_column_found_on_a_stored_row_is_emptied(self):
        counts = Counter()
        changes = scrub_entity(self.stored(demandeur="Marie Martin"), counts)
        self.assertEqual(changes, {"demandeur": ""})
        self.assertEqual(counts["[dropped]"], 1)

    def test_ticket_text_of_a_stored_row_matches_the_parsed_ticket(self):
        tickets, _ = scrub(csv_bytes(row(description="Détail du problème")))
        entity = tickets[0].to_entity("client-s")
        self.assertEqual(ticket_text(entity), tickets[0].text())


class ReviewCasesTest(unittest.TestCase):
    """Cases an independent review found on 2026-10-08 (runbook 18.8)."""

    def desc(self, text, **columns):
        value, report = field_of(export(Description=text, **columns), "Description")
        return value, report

    def test_a_signature_on_the_same_line_as_the_formula_is_cut(self):
        self.assertEqual(self.desc("Ecran noir.\nCordialement, Jean Dupont\nChef de projet")[0], "Ecran noir.")
        self.assertEqual(self.desc("Ecran noir.\nCdt\nJean Dupont")[0], "Ecran noir.")
        self.assertEqual(self.desc("Ecran noir. Bonne journée, cordialement Jean")[0], "Ecran noir. Bonne journée")
        self.assertEqual(self.desc("Ecran noir.\nMerci, Jean Dupont")[0], "Ecran noir.")

    def test_a_title_before_the_greeted_name_is_masked_with_it(self):
        self.assertEqual(self.desc("Bonjour M. Dupont,\nEcran noir.")[0], "Bonjour [nom],\nEcran noir.")
        self.assertEqual(self.desc("Bonjour Monsieur Jean Dupont\nEcran noir.")[0], "Bonjour [nom]\nEcran noir.")

    def test_a_greeting_followed_by_the_problem_keeps_the_problem(self):
        for text in ("Bonjour Outlook plante au démarrage", "Bonjour je n'arrive pas à me connecter",
                     "Bonjour à tous, Teams ne démarre plus"):
            self.assertEqual(self.desc(text)[0], text)

    def test_forwarded_header_lines_are_masked(self):
        text = "Voir ci-dessous.\nDe : Jean Dupont <jean.dupont@example.com>\nObjet : VPN KO\nLe VPN ne marche plus."
        value, report = self.desc(text)
        self.assertEqual(value, "Voir ci-dessous.\nDe : [masqué]\nObjet : VPN KO\nLe VPN ne marche plus.")
        self.assertEqual(report.masked.get("[masqué]"), 1)

    def test_a_mention_takes_the_capitalized_surname_with_it(self):
        self.assertEqual(self.desc("Voir avec @jean Dupont svp")[0], "Voir avec [mention] svp")

    def test_dates_are_never_taken_for_phones_but_phones_are(self):
        for text in ("Depuis le 01.10.2026 18:16 rien ne marche", "Depuis le 01-10-2026 18:16 rien ne marche",
                     "Ticket du 05 10 2026 à 9h"):
            self.assertEqual(self.desc(text)[0], text)
        self.assertEqual(self.desc("Rappeler le 06.12.34.56.78 svp")[0], "Rappeler le [phone] svp")
        self.assertEqual(self.desc("Rappeler le +33 6 12 34 56 78")[0], "Rappeler le [phone]")
        for number in ("0661-234567", "0537-123456", "+212 661-234567", "0612 34 56 78", "05.37.12.34.56"):
            self.assertEqual(self.desc(f"Joignable au {number} svp")[0], "Joignable au [phone] svp", number)

    def test_names_of_the_person_columns_are_masked_in_the_text(self):
        value, report = self.desc("Jean Dupont n'arrive plus à se connecter, écran blanc",
                                  **{"Bénéficiaire": "Jean Dupont", "Demandeur": "Pierre Blanc"})
        self.assertEqual(value, "[nom] n'arrive plus à se connecter, écran blanc")
        value, _ = self.desc("Ecrire à Jean.Dupont@example.com", **{"Bénéficiaire": "Jean Dupont"})
        self.assertEqual(value, "Ecrire à [email]")

    def test_a_person_column_with_a_stray_space_or_an_unknown_column_is_never_stored(self):
        data = export().decode("utf-8-sig").replace('"Bénéficiaire"', '"Bénéficiaire "').replace(
            '"Origine"', '"Contact du VIP"').encode("utf-8")
        tickets, report = scrub(data)
        entity = tickets[0].to_entity("client-s")
        self.assertNotIn("Jean", " ".join(str(v) for v in entity.values()))
        self.assertNotIn("contact_du_vip", entity)
        self.assertEqual(report.unknown_columns, ["Contact du VIP"])

    def test_a_ticket_id_a_table_key_would_refuse_is_skipped(self):
        tickets, report = scrub(export(**{"N° de ticket": "I2610/01#2"}))
        self.assertEqual((report.tickets, report.skipped), (0, 2))

    def test_placeholders_never_reach_the_text_kefind_reads(self):
        tickets, _ = scrub(export(Description="Merci de répondre à jean@example.com ou au 06 12 34 56 78"))
        self.assertEqual(tickets[0].text().splitlines()[-1], "Merci de répondre à ou au")
        self.assertEqual(ticket_text(tickets[0].to_entity("client-s")), tickets[0].text())
        self.assertIn("[email]", tickets[0].fields["Description"])  # stored masked, read without the mask



class SecondReviewTest(unittest.TestCase):
    """Second review pass, 2026-10-08 (runbook 18.8): what the first fixes over- or under-did."""

    def desc(self, text, **columns):
        return field_of(export(Description=text, **columns), "Description")[0]

    def test_a_closing_word_inside_the_problem_cuts_nothing(self):
        for text in ("Le CDT du chantier n'a plus accès à Teams", "Le PC du cdt ne démarre plus",
                     "Bonjour,\nje vous remercie cordialement de votre aide : Outlook plante au démarrage",
                     "Bonjour,\nRegards to the config: proxy KO"):
            self.assertEqual(self.desc(text), text)

    def test_greetings_with_several_names_or_a_period(self):
        self.assertEqual(self.desc("Bonjour Jean et Paul,\nEcran noir."), "Bonjour [nom],\nEcran noir.")
        self.assertEqual(self.desc("Bonjour Jean.\nEcran noir."), "Bonjour [nom].\nEcran noir.")

    def test_more_forwarded_headers(self):
        self.assertEqual(self.desc("Voir dessous.\nA : Jean Dupont <jean@example.com>\nVPN KO"), "Voir dessous.\nA : [masqué]\nVPN KO")
        self.assertEqual(self.desc("Voir dessous.\nDe la part de : Jean Dupont\nVPN KO"), "Voir dessous.\nDe la part de : [masqué]\nVPN KO")
        self.assertEqual(self.desc("Etapes :\nA : redémarrer le poste"), "Etapes :\nA : redémarrer le poste")

    def test_an_upper_case_title_keeps_its_words(self):
        columns = {"Bénéficiaire": "Marie Blanc", "Enregistré par": "ADMINISTRATEUR"}
        self.assertEqual(self.desc("ECRAN BLANC AU DEMARRAGE", **columns), "ECRAN BLANC AU DEMARRAGE")
        self.assertEqual(self.desc("DROITS ADMINISTRATEUR SUR LE POSTE", **columns), "DROITS ADMINISTRATEUR SUR LE POSTE")
        self.assertEqual(self.desc("PC DE MARIE BLANC LENT", **columns), "PC DE [nom] LENT")
        self.assertEqual(self.desc("Mme Blanc rappelle", **columns), "Mme [nom] rappelle")

    def test_a_huge_field_is_cleaned_fast(self):
        import time
        started = time.perf_counter()
        self.desc("a" * 200_000)
        self.desc("x." * 100_000)
        self.assertLess(time.perf_counter() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
