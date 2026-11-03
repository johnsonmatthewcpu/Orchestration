"""Procurement orchestration engine: intake, approval routing, budgets, POs,
receiving, three-way invoice matching, vendors, contracts and renewals."""
import datetime as dt
import json
import threading
import urllib.request
from pathlib import Path

from .db import Database

DEFAULT_CONFIG = Path(__file__).with_name("workflow.json")

OPEN_REQUEST_STATUSES = ("pending_approval",)
COMMITTED_REQUEST_STATUSES = ("approved", "ordered")


class ProcurementError(Exception):
    """Raised for invalid input or a disallowed action; the message is user-facing."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Condition language used by intake questions (show_if) and approval rules.
#   {"field": "amount", "op": ">=", "value": 5000}
#   {"all": [...]}, {"any": [...]}, {"not": {...}}
# --------------------------------------------------------------------------- #

def resolve(ctx, path):
    value = ctx
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def evaluate(cond, ctx):
    if not cond:
        return True
    if "all" in cond:
        return all(evaluate(c, ctx) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, ctx) for c in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], ctx)
    actual = resolve(ctx, cond["field"])
    op, expected = cond.get("op", "=="), cond.get("value")
    if op == "exists":
        return actual not in (None, "") if expected is not False else actual in (None, "")
    if actual is None:
        return False
    if op == "==":
        return actual == expected or (_num(actual) is not None and _num(actual) == _num(expected))
    if op == "!=":
        return not evaluate({**cond, "op": "=="}, ctx)
    if op == "in":
        return actual in expected
    if op == "not_in":
        return actual not in expected
    a, e = _num(actual), _num(expected)
    if a is None or e is None:
        return False
    return {">": a > e, ">=": a >= e, "<": a < e, "<=": a <= e}[op]


class Engine:
    def __init__(self, db=None, config=None, today=None, webhook_sender=None):
        self.db = db or Database()
        if config is None or isinstance(config, (str, Path)):
            config = json.loads(Path(config or DEFAULT_CONFIG).read_text())
        self.config = config
        self._today = today or dt.date.today
        self._send_webhook = webhook_sender or _post_json_async
        self.listeners = []
        self.validators = []  # callables(engine, ctx, department) that raise ProcurementError  # in-process subscribers: fn(event, payload)

    # ------------------------------------------------------------------ utils
    def today(self):
        return self._today()

    def now(self):
        return dt.datetime.now().isoformat(timespec="seconds")

    def log(self, actor_id, entity, entity_id, action, detail=None):
        detail = detail or {}
        self.db.insert("audit_log", {
            "ts": self.now(), "actor_id": actor_id, "entity": entity,
            "entity_id": entity_id, "action": action, "detail": detail,
        })
        self.emit(f"{entity}.{action}", {"id": entity_id, "actor_id": actor_id, **detail})

    def emit(self, event, payload):
        for listener in self.listeners:
            try:
                listener(event, payload)
            except Exception:  # integrations must never break the workflow
                pass
        for hook in self.db.all("SELECT * FROM webhooks"):
            if "*" in hook["events"] or event in hook["events"]:
                self._send_webhook(hook["url"], {"event": event, "data": payload, "ts": self.now()})

    def _user(self, user_id):
        user = self.db.get("users", user_id) if user_id else None
        if not user:
            raise ProcurementError("Unknown user", 401)
        return user

    def _require_role(self, user, *roles):
        if user["role"] not in (*roles, "admin"):
            raise ProcurementError(f"Requires role: {', '.join(roles)}", 403)

    # ------------------------------------------------------- org & reference
    def create_department(self, name, cost_center=None):
        return self.db.insert("departments", {"name": name, "cost_center": cost_center})

    def create_user(self, name, email, role="employee", department_id=None, manager_id=None):
        return self.db.insert("users", {"name": name, "email": email, "role": role,
                                        "department_id": department_id, "manager_id": manager_id})

    def set_budget(self, department_id, category, fiscal_year, amount, actor_id=None):
        existing = self.db.one("SELECT id FROM budgets WHERE department_id=? AND category=? AND fiscal_year=?",
                               (department_id, category, fiscal_year))
        if existing:
            self.db.update("budgets", existing["id"], {"amount": amount})
            budget_id = existing["id"]
        else:
            budget_id = self.db.insert("budgets", {"department_id": department_id, "category": category,
                                                   "fiscal_year": fiscal_year, "amount": amount})
        self.log(actor_id, "budget", budget_id, "set", {"amount": amount})
        return budget_id

    def budget_status(self, department_id, category, fiscal_year=None, exclude_request_id=None):
        year = fiscal_year or self.today().year
        budget = self.db.one("SELECT * FROM budgets WHERE department_id=? AND category=? AND fiscal_year=?",
                             (department_id, category, year))
        base = """SELECT COALESCE(SUM(amount), 0) FROM requests
                  WHERE department_id=? AND category=? AND substr(created_at,1,4)=? AND id != ?
                  AND status IN ({})"""
        args = (department_id, category, str(year), exclude_request_id or -1)
        committed = self.db.scalar(base.format(",".join("?" * len(COMMITTED_REQUEST_STATUSES))),
                                   args + COMMITTED_REQUEST_STATUSES)
        pending = self.db.scalar(base.format(",".join("?" * len(OPEN_REQUEST_STATUSES))),
                                 args + OPEN_REQUEST_STATUSES)
        spent = self.db.scalar(
            """SELECT COALESCE(SUM(i.amount), 0) FROM invoices i
               JOIN purchase_orders p ON p.id = i.po_id JOIN requests r ON r.id = p.request_id
               WHERE r.department_id=? AND r.category=? AND substr(r.created_at,1,4)=? AND i.status='paid'""",
            (department_id, category, str(year)))
        amount = budget["amount"] if budget else None
        return {
            "fiscal_year": year, "budget": amount, "committed": committed, "pending": pending, "spent": spent,
            "remaining": None if amount is None else round(amount - committed, 2),
        }

    # ------------------------------------------------------------- vendors
    def create_vendor(self, name, email=None, tax_id=None, status="pending", risk_tier="unassessed", actor_id=None):
        if self.db.one("SELECT id FROM vendors WHERE lower(name)=lower(?)", (name,)):
            raise ProcurementError(f"Vendor '{name}' already exists")
        vendor_id = self.db.insert("vendors", {"name": name, "email": email, "tax_id": tax_id, "status": status,
                                               "risk_tier": risk_tier, "onboarding": {}, "created_at": self.now()})
        self.log(actor_id, "vendor", vendor_id, "created", {"name": name, "status": status})
        return vendor_id

    def update_vendor(self, vendor_id, actor_id, **fields):
        user = self._user(actor_id)
        self._require_role(user, "procurement", "security")
        allowed = {k: v for k, v in fields.items() if k in ("email", "tax_id", "status", "risk_tier") and v is not None}
        if allowed.get("status") and allowed["status"] not in ("pending", "active", "blocked"):
            raise ProcurementError("Invalid vendor status")
        if allowed.get("risk_tier") and allowed["risk_tier"] not in ("unassessed", "low", "medium", "high"):
            raise ProcurementError("Invalid risk tier")
        if not self.db.get("vendors", vendor_id):
            raise ProcurementError("Vendor not found", 404)
        self.db.update("vendors", vendor_id, allowed)
        self.log(actor_id, "vendor", vendor_id, "updated", allowed)

    # -------------------------------------------------------------- intake
    def request_context(self, req, vendor, department):
        status = self.budget_status(req["department_id"], req["category"], exclude_request_id=req.get("id"))
        over = status["remaining"] is not None and req["amount"] > status["remaining"]
        return {
            "amount": req["amount"], "category": req["category"], "currency": req.get("currency", "USD"),
            "department": department["name"] if department else None,
            "answers": req.get("answers") or {},
            "vendor": {"name": vendor["name"], "is_new": vendor["status"] == "pending",
                       "risk_tier": vendor["risk_tier"], "status": vendor["status"]} if vendor else {},
            "budget": {**status, "over": over, "missing": status["budget"] is None},
        }

    def visible_questions(self, ctx):
        return [q for q in self.config["intake_questions"] if evaluate(q.get("show_if"), ctx)]

    def _validate_answers(self, ctx):
        errors, clean = [], {}
        for q in self.visible_questions(ctx):
            value = ctx["answers"].get(q["id"])
            if value in (None, ""):
                if q.get("required"):
                    errors.append(f"'{q['label']}' is required")
                continue
            if q["type"] == "number":
                if _num(value) is None:
                    errors.append(f"'{q['label']}' must be a number")
                    continue
                value = _num(value)
            if q["type"] == "select" and value not in q["options"]:
                errors.append(f"'{q['label']}' must be one of {q['options']}")
            clean[q["id"]] = value
        if errors:
            raise ProcurementError("; ".join(errors))
        return clean

    def create_request(self, requester_id, data, skip_validation=False):
        requester = self._user(requester_id)
        title = (data.get("title") or "").strip()
        category = data.get("category")
        if not title:
            raise ProcurementError("Title is required")
        if category not in self.config["categories"]:
            raise ProcurementError(f"Category must be one of {self.config['categories']}")

        lines = data.get("line_items") or []
        if lines:
            try:
                lines = [{"description": str(l["description"]).strip(), "quantity": float(l["quantity"]),
                          "unit_price": float(l["unit_price"])} for l in lines]
            except (KeyError, TypeError, ValueError):
                raise ProcurementError("Each line item needs description, quantity and unit_price")
            if any(l["quantity"] <= 0 or l["unit_price"] < 0 or not l["description"] for l in lines):
                raise ProcurementError("Line items need a description, positive quantity and non-negative price")
            amount = round(sum(l["quantity"] * l["unit_price"] for l in lines), 2)
        else:
            amount = _num(data.get("amount"))
            if amount is None or amount <= 0:
                raise ProcurementError("Amount must be a positive number (or provide line items)")
            lines = [{"description": title, "quantity": 1.0, "unit_price": amount}]

        department_id = data.get("department_id") or requester["department_id"]
        department = self.db.get("departments", department_id) if department_id else None
        if not department:
            raise ProcurementError("A department is required")

        vendor, new_vendor = None, None
        if data.get("vendor_id"):
            vendor = self.db.get("vendors", int(data["vendor_id"]))
            if not vendor:
                raise ProcurementError("Vendor not found")
            if vendor["status"] == "blocked":
                raise ProcurementError(f"Vendor '{vendor['name']}' is blocked")
        elif (data.get("new_vendor") or {}).get("name"):
            new_vendor = data["new_vendor"]
            vendor = self.db.one("SELECT * FROM vendors WHERE lower(name)=lower(?)", (new_vendor["name"].strip(),))
            if vendor and vendor["status"] == "blocked":
                raise ProcurementError(f"Vendor '{vendor['name']}' is blocked")
            if vendor:
                new_vendor = None
            else:  # created only after validation passes
                vendor = {"name": new_vendor["name"].strip(), "status": "pending", "risk_tier": "unassessed"}
        else:
            raise ProcurementError("Choose an existing vendor or propose a new one")

        draft = {"amount": amount, "category": category, "department_id": department["id"],
                 "answers": data.get("answers") or {}, "currency": data.get("currency") or "USD"}
        ctx = self.request_context(draft, vendor, department)
        answers = draft["answers"] if skip_validation else self._validate_answers(ctx)
        ctx["answers"] = answers
        if not skip_validation:
            for check in self.validators:
                check(self, ctx, department)
        if new_vendor:
            vendor = self.db.get("vendors", self.create_vendor(
                vendor["name"], new_vendor.get("email"), new_vendor.get("tax_id"), actor_id=requester_id))

        ts = self.now()
        request_id = self.db.insert("requests", {
            "title": title, "requester_id": requester_id, "department_id": department["id"],
            "vendor_id": vendor["id"], "category": category, "amount": amount, "currency": draft["currency"],
            "description": data.get("description") or "", "line_items": lines, "answers": answers,
            "flags": {"over_budget": ctx["budget"]["over"], "no_budget": ctx["budget"]["missing"],
                      "new_vendor": ctx["vendor"]["is_new"], "budget_snapshot": ctx["budget"],
                      **({"renewal_of_contract": data["renewal_of_contract"]} if data.get("renewal_of_contract") else {})},
            "status": "pending_approval", "created_at": ts, "updated_at": ts,
        })
        self.log(requester_id, "request", request_id, "submitted", {"title": title, "amount": amount})
        self._build_approval_chain(request_id, requester, ctx)
        return request_id

    # ----------------------------------------------------------- approvals
    def _resolve_approver(self, spec, requester):
        if spec == "manager":
            return {"approver_user_id": requester["manager_id"]} if requester["manager_id"] else None
        if spec.startswith("role:"):
            return {"approver_role": spec[5:]}
        if spec.startswith("user:"):
            user = self.db.one("SELECT id FROM users WHERE email=?", (spec[5:],))
            return {"approver_user_id": user["id"]} if user else None
        raise ProcurementError(f"Bad approver spec '{spec}'", 500)

    def _build_approval_chain(self, request_id, requester, ctx):
        for rule in self.config["approval_rules"]:
            if not evaluate(rule.get("when"), ctx):
                continue
            approver = self._resolve_approver(rule["approver"], requester)
            step = {"request_id": request_id, "stage": rule["stage"], "name": rule["name"],
                    "status": "waiting", **(approver or {})}
            if approver is None:
                step.update(status="skipped", comment="No approver could be resolved")
            self.db.insert("approval_steps", step)
        self._advance(request_id, actor_id=None)

    def approver_emails(self, step):
        if step["approver_user_id"]:
            return [self.db.get("users", step["approver_user_id"])["email"]]
        return [u["email"] for u in self.db.all("SELECT email FROM users WHERE role=?", (step["approver_role"],))]

    def steps(self, request_id):
        return self.db.all("SELECT * FROM approval_steps WHERE request_id=? ORDER BY stage, id", (request_id,))

    def _advance(self, request_id, actor_id):
        steps = self.steps(request_id)
        if any(s["status"] == "pending" for s in steps):
            return
        waiting = [s for s in steps if s["status"] == "waiting"]
        if not waiting:
            self._finalize(request_id, actor_id)
            return
        next_stage = min(s["stage"] for s in waiting)
        for s in waiting:
            if s["stage"] == next_stage:
                self.db.update("approval_steps", s["id"], {"status": "pending"})
                req = self.db.get("requests", request_id)
                self.emit("approval.requested", {"request_id": request_id, "step": s["name"],
                                                 "title": req["title"], "amount": req["amount"],
                                                 "approver_user_id": s["approver_user_id"],
                                                 "approver_role": s["approver_role"],
                                                 "approver_emails": self.approver_emails(s)})

    def can_act(self, user, step, request):
        if step["status"] != "pending" or user["id"] == request["requester_id"]:
            return False
        return (user["role"] == "admin" or step["approver_user_id"] == user["id"]
                or (step["approver_role"] is not None and step["approver_role"] == user["role"]))

    def inbox(self, user_id):
        user = self._user(user_id)
        out = []
        for req in self.db.all("SELECT * FROM requests WHERE status='pending_approval' ORDER BY created_at"):
            for step in self.steps(req["id"]):
                if self.can_act(user, step, req):
                    out.append({"request": req, "step": step})
                    break
        return out

    def decide(self, request_id, user_id, decision, comment="", step_id=None):
        if decision not in ("approve", "reject"):
            raise ProcurementError("Decision must be 'approve' or 'reject'")
        user = self._user(user_id)
        req = self.db.get("requests", request_id)
        if not req:
            raise ProcurementError("Request not found", 404)
        if req["status"] != "pending_approval":
            raise ProcurementError(f"Request is {req['status']}, not awaiting approval", 409)
        actionable = [s for s in self.steps(request_id)
                      if self.can_act(user, s, req) and (step_id is None or s["id"] == int(step_id))]
        if not actionable:
            raise ProcurementError("You have no pending approval on this request", 403)
        step = actionable[0]
        if decision == "reject" and not (comment or "").strip():
            raise ProcurementError("A comment is required when rejecting")
        self.db.update("approval_steps", step["id"], {
            "status": "approved" if decision == "approve" else "rejected",
            "decided_by": user_id, "decided_at": self.now(), "comment": comment})
        self.log(user_id, "request", request_id, f"step_{decision}d", {"step": step["name"], "comment": comment})
        if decision == "reject":
            for s in self.steps(request_id):
                if s["status"] in ("waiting", "pending"):
                    self.db.update("approval_steps", s["id"], {"status": "skipped"})
            self._set_request_status(request_id, "rejected", user_id)
        else:
            self._advance(request_id, user_id)

    def cancel_request(self, request_id, user_id):
        user = self._user(user_id)
        req = self.db.get("requests", request_id)
        if not req:
            raise ProcurementError("Request not found", 404)
        if req["requester_id"] != user_id and user["role"] not in ("admin", "procurement"):
            raise ProcurementError("Only the requester or procurement can cancel", 403)
        if req["status"] != "pending_approval":
            raise ProcurementError("Only requests awaiting approval can be cancelled", 409)
        for s in self.steps(request_id):
            if s["status"] in ("waiting", "pending"):
                self.db.update("approval_steps", s["id"], {"status": "skipped"})
        self._set_request_status(request_id, "cancelled", user_id)

    def _set_request_status(self, request_id, status, actor_id):
        self.db.update("requests", request_id, {"status": status, "updated_at": self.now()})
        self.log(actor_id, "request", request_id, status)

    def _finalize(self, request_id, actor_id):
        """All approvals done: activate the vendor, issue the PO, record the contract."""
        req = self.db.get("requests", request_id)
        self._set_request_status(request_id, "approved", actor_id)
        vendor = self.db.get("vendors", req["vendor_id"])
        if vendor["status"] == "pending":
            self.db.update("vendors", vendor["id"], {"status": "active",
                                                     "onboarding": {"approved_via_request": request_id,
                                                                    "approved_at": self.now()}})
            self.log(actor_id, "vendor", vendor["id"], "activated", {"request_id": request_id})
        self.create_purchase_order(request_id, actor_id)
        term = _num(req["answers"].get("contract_term_months")) or 0
        if term > 0 and not req["flags"].get("renewal_of_contract"):
            start = self.today()
            self.create_contract(vendor["id"], req["title"], req["amount"], start.isoformat(),
                                 _add_months(start, int(term)).isoformat(),
                                 auto_renew=req["answers"].get("auto_renew") == "yes",
                                 request_id=request_id, actor_id=actor_id)
        elif req["flags"].get("renewal_of_contract"):
            self._apply_renewal(req, actor_id)
        self._set_request_status(request_id, "ordered", actor_id)

    # ------------------------------------------------------ purchase orders
    def create_purchase_order(self, request_id, actor_id=None):
        req = self.db.get("requests", request_id)
        if self.db.one("SELECT id FROM purchase_orders WHERE request_id=?", (request_id,)):
            raise ProcurementError("A PO already exists for this request", 409)
        with self.db.lock:
            seq = self.db.scalar("SELECT COUNT(*) FROM purchase_orders") + 1
            number = f"PO-{self.today().year}-{seq:05d}"
            po_id = self.db.insert("purchase_orders", {
                "number": number, "request_id": request_id, "vendor_id": req["vendor_id"],
                "amount": req["amount"], "currency": req["currency"], "status": "open", "created_at": self.now()})
            for line in req["line_items"]:
                self.db.insert("po_lines", {"po_id": po_id, **line})
        self.log(actor_id, "purchase_order", po_id, "issued", {"number": number, "amount": req["amount"],
                                                               "request_id": request_id})
        return po_id

    def po_lines(self, po_id):
        return self.db.all("SELECT * FROM po_lines WHERE po_id=? ORDER BY id", (po_id,))

    def receive(self, po_id, user_id, receipts):
        """receipts: [{"line_id": int, "quantity": float}] — records goods/services received."""
        self._user(user_id)
        po = self.db.get("purchase_orders", po_id)
        if not po:
            raise ProcurementError("PO not found", 404)
        if po["status"] in ("closed", "cancelled"):
            raise ProcurementError(f"PO is {po['status']}", 409)
        lines = {l["id"]: l for l in self.po_lines(po_id)}
        for r in receipts:
            line = lines.get(int(r["line_id"]))
            qty = _num(r.get("quantity"))
            if not line or qty is None or qty <= 0:
                raise ProcurementError("Each receipt needs a valid PO line and positive quantity")
            if line["received_quantity"] + qty > line["quantity"] + 1e-9:
                raise ProcurementError(f"Receiving {qty} would exceed ordered quantity for '{line['description']}'")
            line["received_quantity"] += qty
            self.db.update("po_lines", line["id"], {"received_quantity": line["received_quantity"]})
        fully = all(l["received_quantity"] >= l["quantity"] - 1e-9 for l in lines.values())
        self.db.update("purchase_orders", po_id, {"status": "received" if fully else "partially_received"})
        self.log(user_id, "purchase_order", po_id, "received", {"receipts": receipts})
        # Re-run matching for invoices held for missing receipts.
        for inv in self.db.all("SELECT * FROM invoices WHERE po_id=? AND status='exception'", (po_id,)):
            self._match_invoice(inv["id"])

    # ------------------------------------------------------------ invoices
    def create_invoice(self, user_id, data):
        self._user(user_id)
        po = None
        if data.get("po_number"):
            po = self.db.one("SELECT * FROM purchase_orders WHERE number=?", (data["po_number"].strip(),))
            if not po:
                raise ProcurementError("PO number not found")
        elif data.get("po_id"):
            po = self.db.get("purchase_orders", int(data["po_id"]))
        vendor_id = int(data.get("vendor_id") or (po and po["vendor_id"]) or 0)
        if not self.db.get("vendors", vendor_id):
            raise ProcurementError("Vendor is required")
        number = (data.get("number") or "").strip()
        if not number:
            raise ProcurementError("Invoice number is required")
        if self.db.one("SELECT id FROM invoices WHERE vendor_id=? AND number=?", (vendor_id, number)):
            raise ProcurementError("Duplicate invoice: this vendor already submitted that invoice number", 409)
        lines = data.get("lines") or []
        try:
            lines = [{"description": str(l["description"]), "quantity": float(l["quantity"]),
                      "unit_price": float(l["unit_price"])} for l in lines]
        except (KeyError, TypeError, ValueError):
            raise ProcurementError("Invoice lines need description, quantity and unit_price")
        amount = _num(data.get("amount"))
        if amount is None:
            amount = round(sum(l["quantity"] * l["unit_price"] for l in lines), 2)
        invoice_id = self.db.insert("invoices", {
            "number": number, "vendor_id": vendor_id, "po_id": po["id"] if po else None, "amount": amount,
            "lines": lines, "status": "received", "match_result": {}, "created_at": self.now()})
        self.log(user_id, "invoice", invoice_id, "received", {"number": number, "amount": amount})
        self._match_invoice(invoice_id)
        return invoice_id

    def _match_invoice(self, invoice_id):
        """Three-way match: invoice vs PO (price, quantity, vendor) vs receipts."""
        inv = self.db.get("invoices", invoice_id)
        tol = self.config.get("invoice_matching", {})
        price_tol = tol.get("price_tolerance_pct", 0) / 100
        amount_tol = tol.get("amount_tolerance_pct", 0) / 100
        issues = []
        po = self.db.get("purchase_orders", inv["po_id"]) if inv["po_id"] else None
        if not po:
            issues.append("No purchase order referenced")
        else:
            if po["vendor_id"] != inv["vendor_id"]:
                issues.append("Invoice vendor does not match PO vendor")
            line_sum = round(sum(l["quantity"] * l["unit_price"] for l in inv["lines"]), 2)
            if inv["lines"] and abs(line_sum - inv["amount"]) > max(0.01, inv["amount"] * amount_tol):
                issues.append(f"Invoice total {inv['amount']:.2f} != sum of lines {line_sum:.2f}")
            po_lines = self.po_lines(po["id"])
            prior = self.db.all("SELECT lines FROM invoices WHERE po_id=? AND id != ? AND status != 'rejected'",
                                (po["id"], invoice_id))
            billed = {}
            for p in prior:
                for l in p["lines"]:
                    billed[l["description"].lower()] = billed.get(l["description"].lower(), 0) + l["quantity"]
            prior_total = self.db.scalar(
                "SELECT COALESCE(SUM(amount),0) FROM invoices WHERE po_id=? AND id != ? AND status != 'rejected'",
                (po["id"], invoice_id))
            if prior_total + inv["amount"] > po["amount"] * (1 + amount_tol) + 0.01:
                issues.append(f"Cumulative invoiced {prior_total + inv['amount']:.2f} exceeds PO {po['amount']:.2f}")
            by_desc = {l["description"].lower(): l for l in po_lines}
            for i, line in enumerate(inv["lines"]):
                pl = by_desc.get(line["description"].lower()) or (po_lines[i] if len(po_lines) == len(inv["lines"]) else None)
                if not pl:
                    issues.append(f"Line '{line['description']}' is not on the PO")
                    continue
                if line["unit_price"] > pl["unit_price"] * (1 + price_tol) + 1e-9:
                    issues.append(f"'{line['description']}': unit price {line['unit_price']:.2f} "
                                  f"exceeds PO price {pl['unit_price']:.2f}")
                already = billed.get(pl["description"].lower(), 0)
                if already + line["quantity"] > pl["received_quantity"] + 1e-9:
                    issues.append(f"'{line['description']}': billed qty {already + line['quantity']:g} "
                                  f"exceeds received qty {pl['received_quantity']:g}")
            if not inv["lines"] and po["status"] != "received":
                issues.append("Goods/services not fully received for header-only invoice")
        result = {"matched": not issues, "issues": issues, "checked_at": self.now()}
        status = "matched" if not issues else "exception"
        self.db.update("invoices", invoice_id, {"match_result": result, "status": status})
        self.log(None, "invoice", invoice_id, status, {"issues": issues})

    def approve_invoice(self, invoice_id, user_id, comment=""):
        user = self._user(user_id)
        self._require_role(user, "finance", "ap")
        inv = self.db.get("invoices", invoice_id)
        if not inv:
            raise ProcurementError("Invoice not found", 404)
        if inv["status"] not in ("matched", "exception"):
            raise ProcurementError(f"Invoice is {inv['status']}", 409)
        if inv["status"] == "exception" and not comment.strip():
            raise ProcurementError("Approving an exception requires a comment explaining the override")
        self.db.update("invoices", invoice_id, {"status": "approved"})
        self.log(user_id, "invoice", invoice_id, "approved", {"override": inv["status"] == "exception",
                                                              "comment": comment})

    def reject_invoice(self, invoice_id, user_id, comment):
        user = self._user(user_id)
        self._require_role(user, "finance", "ap")
        inv = self.db.get("invoices", invoice_id)
        if not inv or inv["status"] in ("paid", "rejected"):
            raise ProcurementError("Invoice cannot be rejected", 409)
        self.db.update("invoices", invoice_id, {"status": "rejected"})
        self.log(user_id, "invoice", invoice_id, "rejected", {"comment": comment})

    def pay_invoice(self, invoice_id, user_id):
        user = self._user(user_id)
        self._require_role(user, "finance", "ap")
        inv = self.db.get("invoices", invoice_id)
        if not inv or inv["status"] != "approved":
            raise ProcurementError("Only approved invoices can be paid", 409)
        self.db.update("invoices", invoice_id, {"status": "paid"})
        self.log(user_id, "invoice", invoice_id, "paid", {"amount": inv["amount"]})
        if inv["po_id"]:
            po = self.db.get("purchase_orders", inv["po_id"])
            paid = self.db.scalar("SELECT COALESCE(SUM(amount),0) FROM invoices WHERE po_id=? AND status='paid'",
                                  (po["id"],))
            if po["status"] == "received" and paid >= po["amount"] - 0.01:
                self.db.update("purchase_orders", po["id"], {"status": "closed"})
                self.log(user_id, "purchase_order", po["id"], "closed")

    # ------------------------------------------------------------ contracts
    def create_contract(self, vendor_id, title, value, start_date, end_date, notice_days=60, auto_renew=False,
                        request_id=None, actor_id=None):
        if end_date <= start_date:
            raise ProcurementError("Contract end date must be after start date")
        contract_id = self.db.insert("contracts", {
            "vendor_id": vendor_id, "request_id": request_id, "title": title, "value": value,
            "start_date": start_date, "end_date": end_date, "notice_days": int(notice_days),
            "auto_renew": 1 if auto_renew else 0, "status": "active"})
        self.log(actor_id, "contract", contract_id, "created", {"title": title, "end_date": end_date})
        return contract_id

    def run_scheduled_jobs(self, actor_id=None):
        """Kick off renewal requests ahead of notice deadlines and expire lapsed contracts."""
        today = self.today()
        lookahead = int(self.config.get("renewal_lookahead_days", 90))
        started, expired = [], []
        for c in self.db.all("SELECT * FROM contracts WHERE status='active'"):
            end = dt.date.fromisoformat(c["end_date"])
            if end < today:
                if c["auto_renew"]:
                    start = dt.date.fromisoformat(c["start_date"])
                    new_end = end + (end - start)
                    self.db.update("contracts", c["id"], {"end_date": new_end.isoformat(), "renewal_request_id": None})
                    self.log(actor_id, "contract", c["id"], "auto_renewed", {"new_end_date": new_end.isoformat()})
                else:
                    self.db.update("contracts", c["id"], {"status": "expired"})
                    self.log(actor_id, "contract", c["id"], "expired")
                    expired.append(c["id"])
                continue
            deadline = end - dt.timedelta(days=c["notice_days"])
            if c["renewal_request_id"] is None and (deadline - today).days <= lookahead:
                started.append(self._start_renewal(c, deadline, actor_id))
        return {"renewals_started": started, "contracts_expired": expired}

    def _start_renewal(self, contract, deadline, actor_id):
        origin = self.db.get("requests", contract["request_id"]) if contract["request_id"] else None
        requester_id = origin["requester_id"] if origin else None
        if requester_id is None:
            owner = self.db.one("SELECT id FROM users WHERE role IN ('procurement','admin') ORDER BY id")
            if not owner:
                raise ProcurementError("No procurement user to own the renewal", 500)
            requester_id = owner["id"]
        req_id = self.create_request(requester_id, {
            "title": f"Renewal: {contract['title']}",
            "category": origin["category"] if origin else self.config["categories"][0],
            "amount": contract["value"], "vendor_id": contract["vendor_id"],
            "department_id": origin["department_id"] if origin else None,
            "description": f"Auto-generated renewal. Notice deadline {deadline.isoformat()}.",
            "answers": {**(origin["answers"] if origin else {}), "business_justification":
                        f"Renewal of contract #{contract['id']} (notice deadline {deadline.isoformat()})"},
            "renewal_of_contract": contract["id"],
        }, skip_validation=True)
        self.db.update("contracts", contract["id"], {"renewal_request_id": req_id})
        self.log(actor_id, "contract", contract["id"], "renewal_started", {"request_id": req_id})
        return req_id

    def _apply_renewal(self, req, actor_id):
        c = self.db.get("contracts", req["flags"]["renewal_of_contract"])
        if not c:
            return
        start, end = dt.date.fromisoformat(c["start_date"]), dt.date.fromisoformat(c["end_date"])
        new_end = end + (end - start)
        self.db.update("contracts", c["id"], {"start_date": end.isoformat(), "end_date": new_end.isoformat(),
                                              "value": req["amount"], "renewal_request_id": None, "status": "active"})
        self.log(actor_id, "contract", c["id"], "renewed", {"request_id": req["id"], "new_end_date": new_end.isoformat()})

    # ------------------------------------------------------------- reporting
    def request_detail(self, request_id):
        req = self.db.get("requests", request_id)
        if not req:
            raise ProcurementError("Request not found", 404)
        req["steps"] = self.steps(request_id)
        req["vendor"] = self.db.get("vendors", req["vendor_id"])
        req["requester"] = self.db.get("users", req["requester_id"])
        req["department"] = self.db.get("departments", req["department_id"])
        po = self.db.one("SELECT * FROM purchase_orders WHERE request_id=?", (request_id,))
        if po:
            po["lines"] = self.po_lines(po["id"])
        req["purchase_order"] = po
        req["budget"] = self.budget_status(req["department_id"], req["category"], int(req["created_at"][:4]))
        req["history"] = self.db.all("SELECT * FROM audit_log WHERE entity='request' AND entity_id=? ORDER BY id",
                                     (request_id,))
        return req

    def dashboard(self):
        year = self.today().year
        by_status = {r["status"]: r["n"] for r in
                     self.db.all("SELECT status, COUNT(*) n FROM requests GROUP BY status")}
        spend = self.db.all(
            """SELECT d.name department, r.category, COALESCE(SUM(r.amount),0) committed
               FROM requests r JOIN departments d ON d.id=r.department_id
               WHERE r.status IN ('approved','ordered') AND substr(r.created_at,1,4)=?
               GROUP BY d.name, r.category ORDER BY committed DESC""", (str(year),))
        budgets = []
        for b in self.db.all("""SELECT b.*, d.name department FROM budgets b JOIN departments d ON d.id=b.department_id
                                WHERE fiscal_year=? ORDER BY d.name, b.category""", (year,)):
            budgets.append({"department": b["department"], "category": b["category"],
                            **self.budget_status(b["department_id"], b["category"], year)})
        cutoff = (self.today() + dt.timedelta(days=int(self.config.get("renewal_lookahead_days", 90)))).isoformat()
        renewals = self.db.all("""SELECT c.*, v.name vendor FROM contracts c JOIN vendors v ON v.id=c.vendor_id
                                  WHERE c.status='active' AND c.end_date <= ? ORDER BY c.end_date""", (cutoff,))
        cycle = self.db.all(
            """SELECT r.id, r.created_at, MAX(a.ts) done FROM requests r JOIN audit_log a
               ON a.entity='request' AND a.entity_id=r.id AND a.action='ordered' GROUP BY r.id""")
        hours = [(dt.datetime.fromisoformat(c["done"]) - dt.datetime.fromisoformat(c["created_at"])).total_seconds()
                 / 3600 for c in cycle]
        return {
            "requests_by_status": by_status,
            "committed_spend": spend,
            "budgets": budgets,
            "invoice_exceptions": self.db.scalar("SELECT COUNT(*) FROM invoices WHERE status='exception'"),
            "open_pos": self.db.scalar("SELECT COUNT(*) FROM purchase_orders WHERE status NOT IN ('closed','cancelled')"),
            "upcoming_renewals": renewals,
            "avg_cycle_time_hours": round(sum(hours) / len(hours), 2) if hours else None,
        }


def _add_months(d, months):
    month = d.month - 1 + months
    year, month = d.year + month // 12, month % 12 + 1
    days = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30,
            31, 31, 30, 31, 30, 31][month - 1]
    return dt.date(year, month, min(d.day, days))


def _post_json_async(url, payload):
    def send():
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:  # webhooks are best-effort
            pass
    threading.Thread(target=send, daemon=True).start()
