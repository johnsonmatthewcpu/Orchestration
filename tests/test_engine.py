import datetime as dt
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from procureflow.engine import Engine, ProcurementError, evaluate
from procureflow.seed import seed
from procureflow.server import make_handler

TODAY = dt.date(2026, 10, 23)


def make_engine():
    sent = []
    e = Engine(today=lambda: TODAY, webhook_sender=lambda url, payload: sent.append(payload))
    seed(e)
    e.sent = sent
    return e


def uid(e, key):
    return e.db.scalar("SELECT id FROM users WHERE email=?", (f"{key}@example.com",))


def vid(e, name):
    return e.db.scalar("SELECT id FROM vendors WHERE name=?", (name,))


def approve_all(e, request_id):
    """Approve every step by whoever can act on it, until the request leaves pending_approval."""
    users = e.db.all("SELECT * FROM users")
    for _ in range(20):
        req = e.db.get("requests", request_id)
        if req["status"] != "pending_approval":
            return
        for step in e.steps(request_id):
            actor = next((u for u in users if u["role"] != "admin" and e.can_act(u, step, req)), None)
            if actor:
                e.decide(request_id, actor["id"], "approve")
                break
    raise AssertionError("approval loop did not finish")


class ConditionTests(unittest.TestCase):
    def test_operators(self):
        ctx = {"amount": 100, "category": "Software", "answers": {"x": "yes"}}
        self.assertTrue(evaluate({"field": "amount", "op": ">=", "value": 100}, ctx))
        self.assertFalse(evaluate({"field": "amount", "op": ">", "value": 100}, ctx))
        self.assertTrue(evaluate({"field": "category", "op": "in", "value": ["Software"]}, ctx))
        self.assertTrue(evaluate({"any": [{"field": "answers.x", "op": "==", "value": "yes"},
                                          {"field": "missing", "op": ">", "value": 1}]}, ctx))
        self.assertFalse(evaluate({"field": "answers.missing", "op": "==", "value": "yes"}, ctx))
        self.assertTrue(evaluate({"not": {"field": "amount", "op": "<", "value": 5}}, ctx))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.e = make_engine()

    def software_request(self, **over):
        data = {"title": "Design tool seats", "category": "Software", "vendor_id": vid(self.e, "Acme Cloud"),
                "line_items": [{"description": "Seat licence", "quantity": 10, "unit_price": 1200}],
                "answers": {"business_justification": "Team needs it", "handles_customer_data": "no",
                            "sso_supported": "yes", "contract_term_months": 12, "auto_renew": "no"}}
        data.update(over)
        return self.e.create_request(uid(self.e, "engineer"), data)

    def test_conditional_questions_are_required(self):
        with self.assertRaisesRegex(ProcurementError, "What kinds of data"):
            self.software_request(answers={"business_justification": "x", "handles_customer_data": "yes",
                                           "sso_supported": "yes", "contract_term_months": 0})

    def test_routing_builds_expected_chain(self):
        rid = self.software_request()
        names = [s["name"] for s in self.e.steps(rid)]
        self.assertEqual(names, ["Manager approval", "IT review", "Legal review", "Finance approval"])
        pending = [s for s in self.e.steps(rid) if s["status"] == "pending"]
        self.assertEqual([s["name"] for s in pending], ["Manager approval"])

    def test_full_lifecycle_po_receipt_invoice_payment(self):
        rid = self.software_request()
        approve_all(self.e, rid)
        req = self.e.request_detail(rid)
        self.assertEqual(req["status"], "ordered")
        po = req["purchase_order"]
        self.assertTrue(po["number"].startswith("PO-2026-"))
        self.assertEqual(self.e.db.scalar("SELECT COUNT(*) FROM contracts WHERE request_id=?", (rid,)), 1)

        ap = uid(self.e, "ap")
        inv = self.e.create_invoice(ap, {"po_number": po["number"], "number": "INV-1",
                                         "lines": [{"description": "Seat licence", "quantity": 10, "unit_price": 1200}]})
        self.assertEqual(self.e.db.get("invoices", inv)["status"], "exception")  # nothing received yet

        line = po["lines"][0]
        self.e.receive(po["id"], uid(self.e, "engineer"), [{"line_id": line["id"], "quantity": 10}])
        self.assertEqual(self.e.db.get("invoices", inv)["status"], "matched")  # re-matched on receipt

        self.e.approve_invoice(inv, ap)
        self.e.pay_invoice(inv, ap)
        self.assertEqual(self.e.db.get("purchase_orders", po["id"])["status"], "closed")
        self.assertEqual(self.e.budget_status(req["department_id"], "Software")["spent"], 12000)

    def test_invoice_price_and_duplicate_checks(self):
        rid = self.software_request()
        approve_all(self.e, rid)
        po = self.e.request_detail(rid)["purchase_order"]
        self.e.receive(po["id"], uid(self.e, "engineer"), [{"line_id": po["lines"][0]["id"], "quantity": 10}])
        ap = uid(self.e, "ap")
        inv = self.e.create_invoice(ap, {"po_number": po["number"], "number": "INV-9",
                                         "lines": [{"description": "Seat licence", "quantity": 10, "unit_price": 1300}]})
        result = self.e.db.get("invoices", inv)["match_result"]
        self.assertTrue(any("unit price" in i for i in result["issues"]))
        with self.assertRaisesRegex(ProcurementError, "Duplicate"):
            self.e.create_invoice(ap, {"po_number": po["number"], "number": "INV-9", "lines": []})
        with self.assertRaisesRegex(ProcurementError, "comment"):
            self.e.approve_invoice(inv, ap)

    def test_requester_cannot_self_approve_and_reject_needs_comment(self):
        mgr = uid(self.e, "eng_mgr")
        rid = self.e.create_request(mgr, {"title": "Monitor", "category": "Hardware", "amount": 400,
                                          "vendor_id": vid(self.e, "Globex Hardware"),
                                          "answers": {"business_justification": "x", "contract_term_months": 0}})
        with self.assertRaises(ProcurementError):
            self.e.decide(rid, mgr, "approve")
        admin = uid(self.e, "admin")
        with self.assertRaisesRegex(ProcurementError, "comment"):
            self.e.decide(rid, admin, "reject")
        self.e.decide(rid, admin, "reject", "Use existing stock")
        self.assertEqual(self.e.db.get("requests", rid)["status"], "rejected")

    def test_new_vendor_onboarding_and_over_budget(self):
        rid = self.e.create_request(uid(self.e, "marketer"), {
            "title": "Brand campaign", "category": "Marketing", "amount": 200000,
            "new_vendor": {"name": "Brand Studio", "email": "hi@brand.example"},
            "answers": {"business_justification": "Launch", "contract_term_months": 0, "competitive_quotes": 3}})
        req = self.e.db.get("requests", rid)
        self.assertTrue(req["flags"]["over_budget"])
        names = [s["name"] for s in self.e.steps(rid)]
        self.assertIn("Vendor onboarding review", names)
        self.assertIn("Over-budget approval (CFO)", names)
        self.assertEqual(self.e.db.get("vendors", req["vendor_id"])["status"], "pending")
        approve_all(self.e, rid)
        self.assertEqual(self.e.db.get("vendors", req["vendor_id"])["status"], "active")

    def test_failed_validation_does_not_create_vendor(self):
        with self.assertRaises(ProcurementError):
            self.e.create_request(uid(self.e, "marketer"), {"title": "x", "category": "Marketing", "amount": 10,
                                                            "new_vendor": {"name": "Ghost Co"}, "answers": {}})
        self.assertIsNone(vid(self.e, "Ghost Co"))

    def test_blocked_vendor_rejected(self):
        with self.assertRaisesRegex(ProcurementError, "blocked"):
            self.software_request(vendor_id=vid(self.e, "Shady Data LLC"))

    def test_renewal_job_starts_and_completes_renewal(self):
        result = self.e.run_scheduled_jobs()
        self.assertEqual(len(result["renewals_started"]), 1)
        self.assertEqual(self.e.run_scheduled_jobs()["renewals_started"], [])  # idempotent
        contract = self.e.db.one("SELECT * FROM contracts")
        old_end = contract["end_date"]
        approve_all(self.e, result["renewals_started"][0])
        contract = self.e.db.get("contracts", contract["id"])
        self.assertEqual(contract["start_date"], old_end)
        self.assertIsNone(contract["renewal_request_id"])

    def test_webhooks_fire(self):
        self.e.db.insert("webhooks", {"url": "http://example.invalid/hook", "events": ["request.submitted"]})
        self.software_request()
        self.assertEqual([p["event"] for p in self.e.sent], ["request.submitted"])


class ServerTests(unittest.TestCase):
    def test_api_round_trip(self):
        e = make_engine()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(e))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def call(path, body=None, user=None):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json", "X-User-Id": str(user or "")},
                                         method="POST" if body is not None else "GET")
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())
        try:
            self.assertEqual(call("/api/meta")[0], 200)
            self.assertEqual(call("/api/dashboard")[0], 401)
            eng = uid(e, "engineer")
            status, out = call("/api/requests", {"title": "Laptop", "category": "Hardware", "amount": 2500,
                                                 "vendor_id": vid(e, "Globex Hardware"),
                                                 "answers": {"business_justification": "New hire",
                                                             "contract_term_months": 0}}, eng)
            self.assertEqual(status, 200, out)
            status, inbox = call("/api/inbox", user=uid(e, "eng_mgr"))
            self.assertEqual([i["request"]["id"] for i in inbox], [out["id"]])
            self.assertEqual(call(f"/api/requests/{out['id']}/approve", {}, eng)[0], 403)
            with urllib.request.urlopen(base + "/") as r:
                self.assertIn(b"ProcureFlow", r.read())
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
