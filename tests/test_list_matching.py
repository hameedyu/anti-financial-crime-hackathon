import unittest

from evidence_intelligence import FactExtractor
from data_processing import CaseRepository
from tests import fixtures as F

ENTRY = {"entry_id": "L1", "kind": "individual", "name": "Robin Fernworth", "date_of_birth": "1972-02-13",
         "identifiers": {"internal_case": "IC-1"}, "address": "Unit 4B, Drayley Works, Raystead", "reason": "test",
         "added_on": "2025-01-01"}


def run(docs, cp="Rayhurst Consulting Ltd", matched_on="company officer name and month and year of birth", entry=ENTRY,
        when="2026-08-08T10:00:00Z"):
    t = F.tx("T1", "C1", when, "out", "faster_payment", 2400, cp=cp)
    root = F.make_institution("bank", cases=[F.case("CS1", "C1", ["A1"])], customers=[F.customer("C1")], transactions=[t],
                              documents=docs, internal_list=[entry],
                              alerts=[F.alert("A1", "CS1", "C1", "TM22", {"list_entry": "L1", "matched_on": matched_on,
                                                                         "matched_party": entry["name"]}, ["T1"])])
    try:
        return FactExtractor().extract(CaseRepository(root).get_case_bundle("CS1")).internal_list_matches[0]
    finally:
        F.cleanup(root)


def register(born, resigned="", cp="Rayhurst Consulting Ltd"):
    return (F.doc_row("REG-1", "C1", "company_register_extract", "Register", "2026-08-08"),
            F.md("REG-1", f"Company register extract for {cp}", "8 August 2026",
                 "**Company number:** 1\n\n| Officer | Role | Born | Appointed | Resigned |\n|---|---|---|---|---|\n"
                 f"| Robin Fernworth | Director | {born} | 1 March 2020 | {resigned} |"))


class ListMatchingTests(unittest.TestCase):
    def test_same_name_different_birth_month_is_identifier_conflict(self):
        lm = run([register("May 1993")])
        self.assertEqual(lm.candidate_confidence, "identifier_conflict")
        self.assertIn("month_year_of_birth", lm.conflicting_fields)
        self.assertEqual(lm.supporting_ids, ["REG-1"])

    def test_same_name_same_birth_month_is_probable_with_control_timeline(self):
        lm = run([register("February 1972", resigned="7 August 2026")])
        self.assertEqual(lm.candidate_confidence, "probable")
        self.assertEqual(lm.control_relationship["status"], "role_ended_before_payment")
        lm2 = run([register("February 1972")])
        self.assertEqual(lm2.control_relationship["status"], "in_role_on_payment_date")

    def test_name_only_is_possible_not_proof(self):
        lm = run([], cp="Robin Fernworth", matched_on="name")
        self.assertEqual(lm.candidate_confidence, "possible")
        self.assertIn("date_of_birth", lm.unknown_fields)

    def test_address_unit_differs(self):
        doc = (F.doc_row("RCT-1", "C1", "receipt", "Receipt", "2026-08-08"),
               F.md("RCT-1", "Receipt from Bramstead Print Studio Ltd", "8 August 2026",
                    "We have received your payment of £2,400.00.\n\nYour classes will be held at Unit 4A, Drayley Works, Raystead."))
        e = dict(ENTRY, name="Bramstead Print Studio Ltd", kind="business", date_of_birth=None)
        lm = run([doc], cp="Bramstead Print Studio Ltd", matched_on="registered address in Drayley Works, Raystead", entry=e)
        self.assertEqual(lm.candidate_confidence, "possible")  # the name also matches; the unit conflict is reported
        self.assertIn("address", lm.conflicting_fields)

    def test_gift_letter_full_dob(self):
        gift = (F.doc_row("GFT-1", "C1", "gift_letter", "Gift", "2026-08-01"),
                F.md("GFT-1", "Gift letter from Robin Fernworth", "1 August 2026",
                     "I, Robin Fernworth, born 1 June 1987, hereby confirm that I am providing a gift of £2,400.00 to my relative, "
                     "Alex Testperson. This transfer is made as an outright gift, and there is no requirement for repayment."))
        lm = run([gift], cp="Robin Fernworth", matched_on="name")
        self.assertEqual(lm.candidate_confidence, "identifier_conflict")


if __name__ == "__main__":
    unittest.main()
