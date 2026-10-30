"""Microsoft Entra ID sign-in, Microsoft Teams, SACC clause library, contract packages and ADM reporting."""
import base64
import csv
import datetime as dt
import hmac
import html
import io
import json
import os
from pathlib import Path

from .engine import ProcurementError, _post_json_async, evaluate
from .server import route

ROLES = ["admin", "adm", "cfo", "finance", "ap", "procurement", "legal", "security", "it", "employee"]
ROLE_CLAIMS = ("roles", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role")
REPORT_ROLES = ("adm", "cfo", "finance", "procurement")


class Raw:
    """A non-JSON response body (HTML, CSV)."""

    def __init__(self, body, content_type):
        self.body = body.encode() if isinstance(body, str) else body
        self.content_type = content_type


# ------------------------------------------------------------ Entra ID sign-in
def auth_mode():
    return os.environ.get("PROCUREFLOW_AUTH", "demo").lower()


def resolve_uid(engine, headers, q):
    """Entra mode trusts the identity headers App Service Authentication injects
    (App Service strips any client-supplied X-MS-* headers). Demo mode uses the
    X-User-Id header or ?as= query from the UI's user switcher."""
    if auth_mode() == "entra":
        email = headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
        return provision_user(engine, email, _claims(headers.get("X-MS-CLIENT-PRINCIPAL"))) if email else None
    raw = headers.get("X-User-Id") or q.get("as")
    return int(raw) if raw and str(raw).isdigit() else None


def _claims(encoded):
    if not encoded:
        return {}
    try:
        data = json.loads(base64.b64decode(encoded))
    except (ValueError, TypeError):
        return {}
    out = {}
    for c in data.get("claims", []):
        out.setdefault(c.get("typ"), []).append(c.get("val"))
    return out


def provision_user(engine, email, claims):
    """Find or create the user; Entra app roles (if assigned) set their ProcureFlow role."""
    email = email.strip().lower()
    granted = {r for t in ROLE_CLAIMS for r in claims.get(t, [])}
    role = next((r for r in ROLES if r in granted), None)
    user = engine.db.one("SELECT * FROM users WHERE lower(email)=?", (email,))
    if not user:
        name = (claims.get("name") or [email])[0]
        uid = engine.create_user(name, email, role or "employee")
        engine.log(uid, "user", uid, "provisioned", {"via": "entra", "role": role or "employee"})
        return uid
    if role and role != user["role"]:
        engine.db.update("users", user["id"], {"role": role})
        engine.log(user["id"], "user", user["id"], "role_synced", {"role": role})
    return user["id"]


@route("GET", "/api/me")
def me(e, uid, body, q):
    return {**e._user(uid), "auth_mode": auth_mode()}


@route("POST", r"/api/users/(\d+)")
def update_user(e, uid, body, q, user_id):
    """Admin sets department, manager and role (Entra does not send these in sign-in claims)."""
    e._require_role(e._user(uid), "admin")
    fields = {k: body[k] for k in ("department_id", "manager_id", "role") if k in body}
    if fields.get("role") and fields["role"] not in ROLES:
        raise ProcurementError(f"Role must be one of {ROLES}")
    if int(user_id) == fields.get("manager_id"):
        raise ProcurementError("A user cannot be their own manager")
    e.db.update("users", int(user_id), fields)
    e.log(uid, "user", int(user_id), "updated", fields)
    return {"ok": True}


# --------------------------------------------------------------- Microsoft Teams
def adaptive_card(title, facts, url=None, link_title="Open in ProcureFlow", note=None):
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]
    if note:
        body.append({"type": "TextBlock", "text": note, "wrap": True, "isSubtle": True})
    body.append({"type": "FactSet", "facts": [{"title": k, "value": str(v)} for k, v in facts]})
    card = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4", "body": body}
    if url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": link_title, "url": url}]
    # Envelope accepted by a Teams "Workflows" webhook (Post to a channel when a webhook request is received).
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                "content": card}]}


class TeamsNotifier:
    """Posts Adaptive Cards to a Teams channel for approvals and outcomes."""

    def __init__(self, engine, webhook_url, base_url, sender=_post_json_async):
        self.engine, self.url, self.base, self.send = engine, webhook_url, base_url.rstrip("/"), sender

    def __call__(self, event, data):
        if event == "approval.requested":
            req = self.engine.db.get("requests", data["request_id"])
            self.send(self.url, adaptive_card(
                f"Approval needed: {req['title']}", self._facts(req) + [
                    ("Step", data["step"]), ("Approver(s)", ", ".join(data.get("approver_emails") or []))],
                f"{self.base}/#requests/{req['id']}", "Review and approve"))
        elif event in ("request.rejected", "request.ordered"):
            req = self.engine.db.get("requests", data["id"])
            done = event == "request.ordered"
            po = self.engine.db.one("SELECT number FROM purchase_orders WHERE request_id=?", (req["id"],))
            self.send(self.url, adaptive_card(
                f"{'Approved, PO issued' if done else 'Rejected'}: {req['title']}",
                self._facts(req) + ([("PO", po["number"])] if po else []),
                f"{self.base}/api/requests/{req['id']}/package" if done else f"{self.base}/#requests/{req['id']}",
                "Open contract package" if done else "Open request"))

    def _facts(self, req):
        requester = self.engine.db.get("users", req["requester_id"])
        dept = self.engine.db.get("departments", req["department_id"])
        vendor = self.engine.db.get("vendors", req["vendor_id"])
        return [("Request", f"#{req['id']}"), ("Amount", f"${req['amount']:,.2f} {req['currency']}"),
                ("Requester", requester["name"]), ("Department", dept["name"]), ("Vendor", vendor["name"])]


def install(engine, base_url):
    """Attach integrations configured through environment variables."""
    url = os.environ.get("TEAMS_WEBHOOK_URL")
    if url:
        engine.listeners.append(TeamsNotifier(engine, url, os.environ.get("APP_BASE_URL") or base_url))


@route("POST", "/api/integrations/teams/decision")
def teams_decision(e, uid, body, q):
    """Callback for a Power Automate 'Start and wait for an approval' flow:
    {"key", "request_id", "approver_email", "decision": "Approve"|"Reject", "comment"}."""
    expected = os.environ.get("PROCUREFLOW_INTEGRATION_KEY")
    if not expected:
        raise ProcurementError("Integration key is not configured", 503)
    if not hmac.compare_digest(str(body.get("key", "")), expected):
        raise ProcurementError("Invalid integration key", 401)
    decision = {"approve": "approve", "approved": "approve", "reject": "reject", "rejected": "reject"}.get(
        str(body.get("decision", "")).lower())
    if not decision:
        raise ProcurementError("decision must be Approve or Reject")
    approver = e.db.one("SELECT id FROM users WHERE lower(email)=?", (str(body.get("approver_email", "")).lower(),))
    if not approver:
        raise ProcurementError("Approver email is not a ProcureFlow user", 404)
    comment = (body.get("comment") or "").strip() or ("Approved in Teams" if decision == "approve" else "Rejected in Teams")
    e.decide(int(body["request_id"]), approver["id"], decision, comment)
    return {"ok": True}


teams_decision.public = True


# ------------------------------------------------------- SACC clause library
def import_clauses(engine, rows, actor_id=None):
    """Upsert clauses from dicts with code, title, text (+ optional section, version_date, source_url)."""
    count = 0
    for row in rows:
        row = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if not row.get("code") or not row.get("title"):
            continue
        values = {k: row.get(k, "") for k in ("title", "text", "section", "version_date", "source_url")}
        existing = engine.db.one("SELECT id FROM clauses WHERE code=?", (row["code"],))
        if existing:
            engine.db.update("clauses", existing["id"], values)
        else:
            engine.db.insert("clauses", {"code": row["code"], **values})
        count += 1
    engine.log(actor_id, "clause_library", None, "imported", {"count": count})
    return count


def read_clause_file(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    return json.loads(text) if path.lower().endswith(".json") else list(csv.DictReader(io.StringIO(text)))


def select_clauses(engine, req):
    vendor = engine.db.get("vendors", req["vendor_id"])
    dept = engine.db.get("departments", req["department_id"])
    ctx = engine.request_context(req, vendor, dept)
    out = []
    for rule in engine.config.get("clause_rules", []):
        if evaluate(rule.get("when"), ctx):
            for code in rule["clauses"]:
                out.append({"rule": rule["name"], "code": code,
                            "clause": engine.db.one("SELECT * FROM clauses WHERE code=?", (code,))})
    return out


@route("GET", "/api/clauses")
def list_clauses(e, uid, body, q):
    term = f"%{q.get('q', '')}%"
    return e.db.all("SELECT * FROM clauses WHERE code LIKE ? OR title LIKE ? ORDER BY code", (term, term))


@route("POST", "/api/clauses/import")
def import_clauses_route(e, uid, body, q):
    e._require_role(e._user(uid), "procurement", "legal")
    rows = body.get("clauses") or list(csv.DictReader(io.StringIO(body.get("csv", ""))))
    return {"imported": import_clauses(e, rows, uid)}


@route("GET", r"/api/requests/(\d+)/clauses")
def request_clauses(e, uid, body, q, rid):
    req = e.db.get("requests", int(rid))
    if not req:
        raise ProcurementError("Request not found", 404)
    return select_clauses(e, req)


# ------------------------------------------------------------ contract package
@route("GET", r"/api/requests/(\d+)/package")
def package(e, uid, body, q, rid):
    """Printable PO / contract package: request, approvals record and clauses."""
    req = e.request_detail(int(rid))
    esc = html.escape
    po = req["purchase_order"]
    users = {u["id"]: u["name"] for u in e.db.all("SELECT id, name FROM users")}
    lines = "".join(f"<tr><td>{esc(l['description'])}</td><td class=n>{l['quantity']:g}</td>"
                    f"<td class=n>{l['unit_price']:,.2f}</td><td class=n>{l['quantity'] * l['unit_price']:,.2f}</td></tr>"
                    for l in req["line_items"])
    steps = "".join(f"<tr><td>{esc(s['name'])}</td><td>{esc(s['status'])}</td><td>{esc(users.get(s['decided_by'], ''))}</td>"
                    f"<td>{esc(s['decided_at'] or '')}</td><td>{esc(s['comment'] or '')}</td></tr>" for s in req["steps"])
    answers = "".join(f"<tr><td>{esc(k.replace('_', ' '))}</td><td>{esc(str(v))}</td></tr>" for k, v in req["answers"].items())
    clauses = select_clauses(e, req)
    clause_html = "".join(
        f"<section class=clause><h3>{esc(c['code'])} {esc(c['clause']['title'])}"
        f"{' <small>(' + esc(c['clause']['version_date']) + ')</small>' if c['clause']['version_date'] else ''}</h3>"
        f"<p class=muted>Selected by rule: {esc(c['rule'])}</p><div class=text>{esc(c['clause']['text'])}</div></section>"
        if c["clause"] else
        f"<section class='clause missing'><h3>{esc(c['code'])}</h3><p>Not in the clause library. Import it from the "
        f"SACC Manual on CanadaBuys before issuing.</p></section>" for c in clauses) or "<p>No clause rules matched.</p>"
    status = "ISSUED" if po else "DRAFT (not yet fully approved)"
    page = f"""<!doctype html><html lang=en><head><meta charset=utf-8><title>{esc(po['number'] if po else 'Draft')} contract package</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#1b1f27}}
table{{width:100%;border-collapse:collapse;margin:8px 0 16px}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}.muted{{color:#666;font-size:12px;margin:0}}.clause{{border:1px solid #ddd;padding:10px 14px;margin:10px 0;border-radius:6px}}
.missing{{border-color:#c62828;background:#fdecea}}.text{{white-space:pre-wrap}}h1{{font-size:20px}}h2{{font-size:16px;margin-top:24px}}
@media print{{.noprint{{display:none}}}}</style></head><body>
<p class=noprint><button onclick="print()">Print / save as PDF</button></p>
<h1>{esc(po['number'] if po else 'Purchase request #' + str(req['id']))}: {esc(req['title'])}</h1>
<p><b>Status:</b> {status}<br><b>Vendor:</b> {esc(req['vendor']['name'])} {esc(req['vendor']['email'] or '')}<br>
<b>Requester:</b> {esc(req['requester']['name'])}<br><b>Department / cost centre:</b> {esc(req['department']['name'])} / {esc(req['department']['cost_center'] or '')}<br>
<b>Category:</b> {esc(req['category'])}<br><b>Total:</b> ${req['amount']:,.2f} {esc(req['currency'])}</p>
<h2>Line items</h2><table><tr><th>Description</th><th class=n>Qty</th><th class=n>Unit price</th><th class=n>Total</th></tr>{lines}</table>
<h2>Requirement details</h2><table>{answers}</table>
<h2>Approval record</h2><table><tr><th>Step</th><th>Status</th><th>By</th><th>When</th><th>Comment</th></tr>{steps}</table>
<h2>Terms and conditions</h2><p class=muted>Clauses from the Standard Acquisition Clauses and Conditions (SACC) Manual, selected by the rules in workflow.json. Confirm the versions are current before issuing.</p>{clause_html}
</body></html>"""
    return Raw(page, "text/html; charset=utf-8")


# ---------------------------------------------------------------- ADM report
def adm_report(e):
    year = e.today().year
    ys = str(year)
    today = e.today()
    sla = int(e.config.get("approval_sla_days", 5))
    reqs = e.db.all("SELECT * FROM requests WHERE substr(created_at,1,4)=?", (ys,))
    committed = sum(r["amount"] for r in reqs if r["status"] in ("approved", "ordered"))
    paid = e.db.scalar("SELECT COALESCE(SUM(amount),0) FROM invoices WHERE status='paid' AND substr(created_at,1,4)=?", (ys,))
    budget_total = e.db.scalar("SELECT COALESCE(SUM(amount),0) FROM budgets WHERE fiscal_year=?", (year,))
    dash = e.dashboard()

    pending = []
    bottlenecks = {}
    for r in (r for r in reqs if r["status"] == "pending_approval"):
        age = (today - dt.date.fromisoformat(r["created_at"][:10])).days
        steps = [s["name"] for s in e.steps(r["id"]) if s["status"] == "pending"]
        for s in steps:
            bottlenecks[s] = bottlenecks.get(s, 0) + 1
        pending.append({"id": r["id"], "title": r["title"], "amount": r["amount"], "age_days": age,
                        "waiting_on": ", ".join(steps), "overdue": age > sla})
    decided = [r for r in reqs if r["status"] in ("approved", "ordered", "rejected")]

    departments = []
    for d in e.db.all("SELECT * FROM departments ORDER BY name"):
        b = e.db.scalar("SELECT COALESCE(SUM(amount),0) FROM budgets WHERE department_id=? AND fiscal_year=?", (d["id"], year))
        c = sum(r["amount"] for r in reqs if r["department_id"] == d["id"] and r["status"] in ("approved", "ordered"))
        departments.append({"department": d["name"], "budget": b, "committed": c,
                            "utilization_pct": round(100 * c / b, 1) if b else None})
    monthly = e.db.all("""SELECT substr(created_at,1,7) month, SUM(amount) committed, COUNT(*) pos FROM purchase_orders
                          WHERE substr(created_at,1,4)=? GROUP BY month ORDER BY month""", (ys,))
    vendors = e.db.all("""SELECT v.name vendor, SUM(p.amount) total, COUNT(*) pos FROM purchase_orders p
                          JOIN vendors v ON v.id=p.vendor_id WHERE substr(p.created_at,1,4)=?
                          GROUP BY v.name ORDER BY total DESC LIMIT 5""", (ys,))
    hours = dash["avg_cycle_time_hours"]
    return {
        "as_of": today.isoformat(), "year": year, "approval_sla_days": sla,
        "kpis": {
            "requests_submitted": len(reqs),
            "value_requested": round(sum(r["amount"] for r in reqs), 2),
            "committed": round(committed, 2), "paid": round(paid, 2), "budget": round(budget_total, 2),
            "budget_utilization_pct": round(100 * committed / budget_total, 1) if budget_total else None,
            "avg_cycle_days": round(hours / 24, 1) if hours is not None else None,
            "pending_approvals": len(pending), "overdue_approvals": sum(p["overdue"] for p in pending),
            "rejection_rate_pct": round(100 * sum(r["status"] == "rejected" for r in decided) / len(decided), 1) if decided else None,
            "invoice_exceptions": dash["invoice_exceptions"], "renewals_due": len(dash["upcoming_renewals"]),
        },
        "departments": departments, "monthly": monthly, "top_vendors": vendors,
        "pending": sorted(pending, key=lambda p: -p["age_days"]),
        "bottlenecks": [{"step": k, "count": v} for k, v in sorted(bottlenecks.items(), key=lambda x: -x[1])],
        "renewals": [{"title": c["title"], "vendor": c["vendor"], "end_date": c["end_date"], "value": c["value"]}
                     for c in dash["upcoming_renewals"]],
    }


def _report_user(e, uid):
    e._require_role(e._user(uid), *REPORT_ROLES)


@route("GET", "/api/reports/adm")
def adm_json(e, uid, body, q):
    _report_user(e, uid)
    return adm_report(e)


@route("GET", "/api/reports/adm.csv")
def adm_csv(e, uid, body, q):
    _report_user(e, uid)
    rep = adm_report(e)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "item", "value"])
    for k, v in rep["kpis"].items():
        w.writerow(["kpi", k, v])
    for d in rep["departments"]:
        w.writerow(["department_committed", d["department"], d["committed"]])
        w.writerow(["department_budget", d["department"], d["budget"]])
    for m in rep["monthly"]:
        w.writerow(["monthly_committed", m["month"], m["committed"]])
    for v in rep["top_vendors"]:
        w.writerow(["top_vendor", v["vendor"], v["total"]])
    for p in rep["pending"]:
        w.writerow(["pending_approval", f"#{p['id']} {p['title']} ({p['waiting_on']})", p["age_days"]])
    return Raw(buf.getvalue(), "text/csv; charset=utf-8")


@route("POST", "/api/reports/adm/teams")
def adm_to_teams(e, uid, body, q):
    _report_user(e, uid)
    url = os.environ.get("TEAMS_WEBHOOK_URL")
    if not url:
        raise ProcurementError("Set TEAMS_WEBHOOK_URL to post to Teams", 503)
    k = adm_report(e)["kpis"]
    facts = [("Committed", f"${k['committed']:,.0f} of ${k['budget']:,.0f} ({k['budget_utilization_pct']}%)"),
             ("Paid", f"${k['paid']:,.0f}"), ("Requests submitted", k["requests_submitted"]),
             ("Pending approvals", f"{k['pending_approvals']} ({k['overdue_approvals']} overdue)"),
             ("Avg. cycle time", f"{k['avg_cycle_days']} days"), ("Invoice exceptions", k["invoice_exceptions"]),
             ("Contracts up for renewal", k["renewals_due"])]
    base = os.environ.get("APP_BASE_URL", "").rstrip("/")
    _post_json_async(url, adaptive_card(f"Procurement summary for the ADM, {e.today().isoformat()}", facts,
                                        f"{base}/adm.html" if base else None, "Open ADM dashboard"))
    return {"ok": True}
