import base64
import json
import os
import unittest
from unittest import mock

from procureflow import integrations as I
from tests.test_engine import approve_all, make_engine, uid, vid

CLAUSE_CSV = "code,title,text,version_date\n2010A,General Conditions - Goods (Medium Complexity),Sample text,2022-12-01\n"


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.e = make_engine()
        self.rid = self.e.create_request(uid(self.e, "engineer"), {
            "title": "Laptops", "category": "Hardware", "vendor_id": vid(self.e, "Globex Hardware"),
            "line_items": [{"description": "Laptop", "quantity": 2, "unit_price": 3000}],
            "answers": {"business_justification": "New staff", "contract_term_months": 0}})

    def test_clause_import_selection_and_package(self):
        import csv, io
        self.assertEqual(I.import_clauses(self.e, list(csv.DictReader(io.StringIO(CLAUSE_CSV)))), 1)
        req = self.e.db.get("requests", self.rid)
        self.assertEqual([c["code"] for c in I.select_clauses(self.e, req)], ["2010A"])
        approve_all(self.e, self.rid)
        page = I.package(self.e, uid(self.e, "engineer"), {}, {}, str(self.rid)).body.decode()
        self.assertIn("PO-", page)
        self.assertIn("General Conditions - Goods", page)

    def test_entra_provisioning_and_role_sync(self):
        principal = base64.b64encode(json.dumps({"claims": [{"typ": "roles", "val": "finance"},
                                                           {"typ": "name", "val": "Pat New"}]}).encode())
        headers = {"X-MS-CLIENT-PRINCIPAL-NAME": "Pat@Example.com", "X-MS-CLIENT-PRINCIPAL": principal}
        with mock.patch.dict(os.environ, {"PROCUREFLOW_AUTH": "entra"}):
            new_id = I.resolve_uid(self.e, headers, {})
            self.assertEqual(I.resolve_uid(self.e, headers, {"as": "1"}), new_id)  # ?as= ignored in entra mode
        user = self.e.db.get("users", new_id)
        self.assertEqual((user["email"], user["role"], user["name"]), ("pat@example.com", "finance", "Pat New"))

    def test_teams_decision_callback(self):
        body = {"key": "secret", "request_id": self.rid, "approver_email": "eng_mgr@example.com", "decision": "Approve"}
        with mock.patch.dict(os.environ, {"PROCUREFLOW_INTEGRATION_KEY": "secret"}):
            with self.assertRaises(I.ProcurementError):
                I.teams_decision(self.e, None, {**body, "key": "wrong"}, {})
            I.teams_decision(self.e, None, body, {})
        step = self.e.steps(self.rid)[0]
        self.assertEqual((step["status"], step["comment"]), ("approved", "Approved in Teams"))

    def test_teams_cards_and_adm_report(self):
        sent = []
        self.e.listeners.append(I.TeamsNotifier(self.e, "https://teams.example/hook", "https://app", lambda u, p: sent.append(p)))
        approve_all(self.e, self.rid)
        titles = [p["attachments"][0]["content"]["body"][0]["text"] for p in sent]
        self.assertTrue(any(t.startswith("Approval needed") for t in titles))
        self.assertTrue(titles[-1].startswith("Approved, PO issued"))
        rep = I.adm_report(self.e)
        self.assertEqual(rep["kpis"]["committed"], 6000)
        with self.assertRaises(I.ProcurementError):
            I.adm_json(self.e, uid(self.e, "engineer"), {}, {})
        self.assertIn("kpi,committed,6000", I.adm_csv(self.e, uid(self.e, "adm"), {}, {}).body.decode())

    def test_sacc_harvest(self):
        from procureflow import sacc
        root = "https://canadabuys.canada.ca/en/sacc-manual"
        pages = {
            root: '<main><h1>SACC Manual</h1><a href="/en/sacc-manual/2035">a</a>'
                  '<a href="/en/sacc-manual/2035-old">b</a><a href="https://other.ca/x">c</a></main>',
            root + "/2035": "<main><h1>2035 (2022-12-01) General Conditions - Higher Complexity - Services</h1>"
                            "<p>01 Interpretation</p></main>",
            root + "/2035-old": "<main><h1>2035 (2020-05-28) General Conditions - Higher Complexity - Services</h1></main>",
        }
        got = sacc.harvest(root, fetch=pages.__getitem__, delay=0, log=lambda *_: None)
        self.assertEqual([(c["code"], c["version_date"]) for c in got], [("2035", "2022-12-01")])
        self.assertIn("01 Interpretation", got[0]["text"])
        self.assertEqual(I.import_clauses(self.e, got), 1)


if __name__ == "__main__":
    unittest.main()
