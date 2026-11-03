"""JSON API + static web UI on Python's built-in http.server.

The acting user is taken from the X-User-Id header (chosen in the UI's user
switcher). That is for local testing only — put real SSO in front before
exposing this to a network.
"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .engine import ProcurementError, _num, evaluate

STATIC = Path(__file__).with_name("static")

ROUTES = []


def route(method, pattern):
    def wrap(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn))
        return fn
    return wrap


# ------------------------------------------------------------------ handlers
@route("GET", "/api/meta")
def meta(e, uid, body, q):
    return {"categories": e.config["categories"], "questions": e.config["intake_questions"],
            "rules": e.config["approval_rules"],
            "users": e.db.all("SELECT u.*, d.name department FROM users u LEFT JOIN departments d ON d.id=u.department_id ORDER BY u.name"),
            "departments": e.db.all("SELECT * FROM departments ORDER BY name"),
            "vendors": e.db.all("SELECT id, name, status, risk_tier FROM vendors ORDER BY name")}


meta.public = True


def draft_context(e, uid, body):
    """Build the evaluation context for an unsaved draft (same shape the engine uses)."""
    vendor = e.db.get("vendors", int(body["vendor_id"])) if body.get("vendor_id") else (
        {"name": body.get("new_vendor_name") or "", "status": "pending", "risk_tier": "unassessed"})
    user = e._user(uid)
    dept = e.db.get("departments", int(body.get("department_id") or user["department_id"] or 0))
    if not dept:
        raise ProcurementError("A department is required")
    return e.request_context({"amount": _num(body.get("amount")) or 0, "category": body.get("category"),
                              "department_id": dept["id"], "answers": body.get("answers") or {}}, vendor, dept), dept


@route("POST", "/api/intake/preview")
def intake_preview(e, uid, body, q):
    """Return visible questions + the approval chain a draft would get, without saving."""
    ctx, _ = draft_context(e, uid, body)
    chain = [{"name": r["name"], "stage": r["stage"], "approver": r["approver"]}
             for r in e.config["approval_rules"] if evaluate(r.get("when"), ctx)]
    return {"questions": e.visible_questions(ctx), "approval_chain": chain, "budget": ctx["budget"]}


@route("GET", "/api/dashboard")
def dashboard(e, uid, body, q):
    return e.dashboard()


@route("GET", "/api/requests")
def list_requests(e, uid, body, q):
    sql = """SELECT r.*, u.name requester, v.name vendor, d.name department FROM requests r
             JOIN users u ON u.id=r.requester_id JOIN vendors v ON v.id=r.vendor_id
             JOIN departments d ON d.id=r.department_id"""
    if q.get("mine"):
        return e.db.all(sql + " WHERE r.requester_id=? ORDER BY r.id DESC", (uid,))
    return e.db.all(sql + " ORDER BY r.id DESC")


@route("POST", "/api/requests")
def create_request(e, uid, body, q):
    return {"id": e.create_request(uid, body)}


@route("GET", r"/api/requests/(\d+)")
def get_request(e, uid, body, q, rid):
    detail = e.request_detail(int(rid))
    user = e._user(uid)
    detail["can_act"] = [s["id"] for s in detail["steps"] if e.can_act(user, s, detail)]
    return detail


@route("POST", r"/api/requests/(\d+)/(approve|reject)")
def decide(e, uid, body, q, rid, decision):
    e.decide(int(rid), uid, decision, body.get("comment", ""), body.get("step_id"))
    return {"ok": True}


@route("POST", r"/api/requests/(\d+)/cancel")
def cancel(e, uid, body, q, rid):
    e.cancel_request(int(rid), uid)
    return {"ok": True}


@route("GET", "/api/inbox")
def inbox(e, uid, body, q):
    return e.inbox(uid)


@route("GET", "/api/vendors")
def vendors(e, uid, body, q):
    return e.db.all("""SELECT v.*, (SELECT COALESCE(SUM(amount),0) FROM purchase_orders p WHERE p.vendor_id=v.id) po_total
                       FROM vendors v ORDER BY name""")


@route("POST", "/api/vendors")
def create_vendor(e, uid, body, q):
    e._require_role(e._user(uid), "procurement")
    return {"id": e.create_vendor(body.get("name", "").strip() or _missing("name"), body.get("email"),
                                  body.get("tax_id"), body.get("status", "active"),
                                  body.get("risk_tier", "unassessed"), uid)}


@route("POST", r"/api/vendors/(\d+)")
def update_vendor(e, uid, body, q, vid):
    e.update_vendor(int(vid), uid, **body)
    return {"ok": True}


@route("GET", "/api/budgets")
def budgets(e, uid, body, q):
    year = int(q.get("year") or e.today().year)
    rows = e.db.all("""SELECT b.*, d.name department FROM budgets b JOIN departments d ON d.id=b.department_id
                       WHERE fiscal_year=? ORDER BY d.name, category""", (year,))
    return [{**b, **e.budget_status(b["department_id"], b["category"], year)} for b in rows]


@route("POST", "/api/budgets")
def set_budget(e, uid, body, q):
    e._require_role(e._user(uid), "finance")
    return {"id": e.set_budget(int(body["department_id"]), body["category"],
                               int(body.get("fiscal_year") or e.today().year), float(body["amount"]), uid)}


@route("GET", "/api/purchase-orders")
def pos(e, uid, body, q):
    rows = e.db.all("""SELECT p.*, v.name vendor, r.title FROM purchase_orders p JOIN vendors v ON v.id=p.vendor_id
                       JOIN requests r ON r.id=p.request_id ORDER BY p.id DESC""")
    for p in rows:
        p["lines"] = e.po_lines(p["id"])
    return rows


@route("POST", r"/api/purchase-orders/(\d+)/receive")
def receive(e, uid, body, q, pid):
    e.receive(int(pid), uid, body.get("receipts") or [])
    return {"ok": True}


@route("GET", "/api/invoices")
def invoices(e, uid, body, q):
    return e.db.all("""SELECT i.*, v.name vendor, p.number po_number FROM invoices i JOIN vendors v ON v.id=i.vendor_id
                       LEFT JOIN purchase_orders p ON p.id=i.po_id ORDER BY i.id DESC""")


@route("POST", "/api/invoices")
def create_invoice(e, uid, body, q):
    return {"id": e.create_invoice(uid, body)}


@route("POST", r"/api/invoices/(\d+)/(approve|reject|pay)")
def invoice_action(e, uid, body, q, iid, action):
    if action == "approve":
        e.approve_invoice(int(iid), uid, body.get("comment", ""))
    elif action == "reject":
        e.reject_invoice(int(iid), uid, body.get("comment", ""))
    else:
        e.pay_invoice(int(iid), uid)
    return {"ok": True}


@route("GET", "/api/contracts")
def contracts(e, uid, body, q):
    return e.db.all("""SELECT c.*, v.name vendor FROM contracts c JOIN vendors v ON v.id=c.vendor_id
                       ORDER BY c.end_date""")


@route("POST", "/api/contracts")
def create_contract(e, uid, body, q):
    e._require_role(e._user(uid), "procurement", "legal")
    return {"id": e.create_contract(int(body["vendor_id"]), body["title"], float(body["value"]),
                                    body["start_date"], body["end_date"], int(body.get("notice_days") or 60),
                                    bool(body.get("auto_renew")), actor_id=uid)}


@route("POST", "/api/jobs/run")
def run_jobs(e, uid, body, q):
    return e.run_scheduled_jobs(uid)


@route("GET", "/api/audit")
def audit(e, uid, body, q):
    return e.db.all("""SELECT a.*, u.name actor FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id
                       ORDER BY a.id DESC LIMIT 300""")


@route("GET", "/api/webhooks")
def webhooks(e, uid, body, q):
    return e.db.all("SELECT * FROM webhooks")


@route("POST", "/api/webhooks")
def add_webhook(e, uid, body, q):
    e._require_role(e._user(uid), "admin")
    if not str(body.get("url", "")).startswith(("http://", "https://")):
        raise ProcurementError("Webhook URL must be http(s)")
    return {"id": e.db.insert("webhooks", {"url": body["url"], "events": body.get("events") or ["*"]})}


def _missing(field):
    raise ProcurementError(f"'{field}' is required")


# -------------------------------------------------------------------- server
def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, status, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            url = urlparse(self.path)
            if method == "GET" and not url.path.startswith("/api/"):
                return self._static(url.path)
            for m, pattern, fn in ROUTES:
                match = pattern.match(url.path)
                if m == method and match:
                    break
            else:
                return self._send(404, {"error": "Not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                uid = integrations.resolve_uid(engine, self.headers, q)
                if uid is None and not getattr(fn, "public", False):
                    raise ProcurementError("Select a user (X-User-Id header)", 401)
                out = fn(engine, uid, body, q, *match.groups())
                if isinstance(out, integrations.Raw):
                    self._send(200, out.body, out.content_type)
                else:
                    self._send(200, out)
            except ProcurementError as exc:
                self._send(exc.status, {"error": str(exc)})
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._send(400, {"error": f"Bad request: {exc}"})

        def _static(self, path):
            target = (STATIC / (path.lstrip("/") or "index.html")).resolve()
            if not target.is_file() or STATIC.resolve() not in target.parents:
                target = STATIC / "index.html"
            ctype = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}.get(target.suffix, "text/plain")
            self._send(200, target.read_bytes(), ctype + "; charset=utf-8")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def serve(engine, host="127.0.0.1", port=8000):
    httpd = ThreadingHTTPServer((host, port), make_handler(engine))
    print(f"ProcureFlow running at http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


from . import integrations  # noqa: E402  (registers Entra, Teams, clause and report routes)
from . import compliance  # noqa: E402,F401  (registers NL intake + compliance routes)
