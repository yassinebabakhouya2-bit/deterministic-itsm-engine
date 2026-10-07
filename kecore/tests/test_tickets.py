import unittest

from kecore.tickets import scrub


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


if __name__ == "__main__":
    unittest.main()
