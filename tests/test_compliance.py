import unittest

from procureflow import compliance as C
from procureflow.engine import ProcurementError
from tests.test_engine import make_engine, uid

DMP = {"indigenous_considered": "yes", "security_requirements": "none", "accessibility_considered": "yes",
       "environmental_considered": "not_applicable", "conflict_of_interest_declared": "yes",
       "handles_customer_data": "no", "sso_supported": "yes"}
WHY = ("The software is proprietary and Acme Cloud holds the copyright; no other supplier can provide "
       "maintenance or updates to the licensed product we already run.")


class ComplianceTests(unittest.TestCase):
    def setUp(self):
        self.e = make_engine()
        C.install(self.e)
        self.eng = uid(self.e, "engineer")

    def submit(self, text, **answers):
        d = C.parse_text(self.e, text)["draft"]
        d["answers"].update(answers)
        if d.get("new_vendor_name"):
            d["new_vendor"] = {"name": d.pop("new_vendor_name")}
        return self.e.create_request(self.eng, d)

    def test_parse_sole_source(self):
        r = C.parse_text(self.e, "We need to sole source a 12 month licence renewal from Acme Cloud for $18,500. " + WHY)
        d, a = r["draft"], r["draft"]["answers"]
        self.assertEqual((d["category"], d["line_items"][0]["unit_price"]), ("Software", 18500))
        self.assertEqual((a["procurement_method"], a["sole_source_ground"], a["contract_term_months"]),
                         ("sole_source", "single_supplier", 12))
        self.assertIn("vendor_id", d)

    def test_submission_blocked_until_dmp_considerations_answered(self):
        text = "We need to sole source a licence renewal from Acme Cloud for $18,500. " + WHY
        with self.assertRaises(ProcurementError) as err:
            self.submit(text, handles_customer_data="no", sso_supported="yes")
        self.assertIn("mandatory considerations", str(err.exception))
        rid = self.submit(text, **DMP)
        names = [s["name"] for s in self.e.steps(rid)]
        self.assertIn("Contracting authority: sole-source review", names)

    def test_low_value_limit_and_contract_splitting(self):
        with self.assertRaises(ProcurementError) as err:
            self.submit("Buy consulting services from Initech Consulting for $40,000, sole source.",
                        sole_source_ground="low_value", sole_source_justification=WHY, **DMP)
        self.assertIn("low-dollar limit", str(err.exception))
        text = "Sole source consulting services from Initech Consulting for $20,000. " + WHY
        self.submit(text, sole_source_ground="low_value", **DMP)
        with self.assertRaises(ProcurementError) as err:
            self.submit(text, sole_source_ground="low_value", **DMP)
        self.assertIn("contract splitting", str(err.exception))

    def test_competitive_passes(self):
        rid = self.submit("Purchase 10 laptops at $1,500 each from Globex Hardware after 3 quotes.", **DMP)
        self.assertEqual(self.e.db.get("requests", rid)["amount"], 15000)


if __name__ == "__main__":
    unittest.main()
