"""Plain-language intake and Government of Canada procurement compliance checks.

parse_text() turns a free-text description into a draft request. assess() checks a draft against
the Government Contracts Regulations (GCR) s.5-6 sole-source exceptions and the Treasury Board
Directive on the Management of Procurement (DMP). Limits live in workflow.json -> "compliance";
confirm them against the current regulations and your departmental delegation.
"""
import datetime as dt
import re

from .engine import ProcurementError
from .server import draft_context, route

GROUNDS = {
    "emergency": "GCR s.6(a): pressing emergency in which delay would be injurious to the public interest",
    "low_value": "GCR s.6(b): estimated expenditure does not exceed the low-dollar limit",
    "public_interest": "GCR s.6(c): the nature of the work is such that soliciting bids would not be in the public interest",
    "single_supplier": "GCR s.6(d): only one person or firm is capable of performing the contract",
}
CONSIDERATIONS = [
    ("indigenous_considered", "Indigenous business participation"),
    ("security_requirements", "security requirements"),
    ("accessibility_considered", "accessibility"),
    ("environmental_considered", "environmental (green procurement) considerations"),
]
CATEGORY_WORDS = {
    "Software": ["software", "licence", "license", "saas", "subscription", "cloud", "platform", "application"],
    "Hardware": ["laptop", "hardware", "server", "equipment", "device", "monitor", "printer", "computer"],
    "Professional Services": ["consult", "services", "advisory", "training", "audit", "study", "analysis", "expert"],
    "Facilities": ["facility", "renovation", "furniture", "cleaning", "building", "maintenance", "repair"],
    "Marketing": ["marketing", "advertising", "campaign", "event", "outreach"],
    "Travel": ["travel", "flight", "hotel", "conference"],
}
SOLE_WORDS = r"sole[- ]?source|non[- ]?competitive|directed contract|only (?:one )?(?:supplier|vendor|firm|company)|proprietary|exclusive"
GROUND_WORDS = [
    ("emergency", r"emergenc|urgent|pressing|immediate(?:ly)? required|life and safety"),
    ("public_interest", r"national security|public interest|confidential(?:ity)?|classified"),
    ("single_supplier", r"only (?:one )?(?:supplier|vendor|firm|company)|proprietary|exclusive|patent|copyright|"
                        r"compatib|original (?:equipment )?manufacturer|oem|sole (?:distributor|owner)"),
]
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}


def _money(num, scale):
    return float(num.replace(",", "")) * _SCALE.get((scale or "").lower(), 1)


def parse_text(e, text):
    """Best-effort extraction from plain language. The requester reviews the draft before submitting."""
    t = " ".join((text or "").split())
    if len(t) < 15:
        raise ProcurementError("Describe what you need in a sentence or two")
    low, notes = t.lower(), []
    first = re.split(r"(?<=[.!?])\s", t)[0]
    title = re.sub(r"^(?:hi|hello)?[, ]*(?:please\s+)?(?:i|we)\s+(?:need|want|would like|have)\s+(?:to\s+)?"
                   r"(?:buy|purchase|procure|get|acquire|contract|sole[- ]source|hire)?\s*", "", first, flags=re.I)
    title = re.sub(r"^(?:an?|the)\s+", "", title, flags=re.I)
    title = (title[:1].upper() + title[1:]).rstrip(".")[:80] or t[:80]

    lines = []
    for m in re.finditer(r"(\d[\d,]*)\s+([a-z][a-z\- ]{1,40}?)\s+(?:at|@)\s*\$\s*([\d,]+(?:\.\d+)?)\s*(k|m|thousand|million)?\b", low):
        lines.append({"description": m.group(2).strip().capitalize(), "quantity": float(m.group(1).replace(",", "")),
                      "unit_price": _money(m.group(3), m.group(4))})
    if not lines:
        amounts = [_money(n, s) for n, s in re.findall(r"\$\s*([\d,]+(?:\.\d+)?)\s*(k|m|thousand|million)?\b", low)]
        amounts += [_money(n, s) for n, s in re.findall(r"\b([\d,]+(?:\.\d+)?)\s*(k|thousand|million)?\s*(?:dollars|cad)\b", low)]
        if amounts:
            lines = [{"description": title, "quantity": 1.0, "unit_price": max(amounts)}]
        else:
            notes.append("No dollar value found. Enter the estimated cost, including taxes.")

    cats = e.config["categories"]
    scores = {c: sum(low.count(w) for w in CATEGORY_WORDS.get(c, [])) for c in cats}
    category = max(cats, key=lambda c: scores[c]) if any(scores.values()) else cats[0]
    if not any(scores.values()):
        notes.append("Category not recognized. Please choose one.")

    draft = {"title": title, "category": category, "description": t, "line_items": lines, "answers": {}}
    vendor = next((v for v in e.db.all("SELECT id, name, status FROM vendors ORDER BY length(name) DESC")
                   if v["name"].lower() in low), None)
    if vendor:
        draft["vendor_id"] = vendor["id"]
        if vendor["status"] == "blocked":
            notes.append(f"{vendor['name']} is a blocked supplier and can't be used.")
    else:
        m = re.search(r"\b(?:from|with|by|vendor|supplier|firm|company)\s+([A-Z][\w&.\-]*(?:\s+(?:[A-Z][\w&.\-]*|Inc\.?|Ltd\.?|LLC|Corp\.?))*)", t)
        if m:
            draft["new_vendor_name"] = m.group(1).rstrip(".")
        else:
            notes.append("Supplier not found. Choose or add one.")

    a = draft["answers"]
    a["business_justification"] = t
    term = re.search(r"(\d+)\s*[- ]?(month|year)s?\b", low)
    a["contract_term_months"] = (int(term.group(1)) * (12 if term.group(2) == "year" else 1)) if term else 0
    date = re.search(r"\b(20\d\d-\d\d-\d\d)\b", t)
    if date:
        a["needed_by"] = date.group(1)
    if re.search(SOLE_WORDS, low) or any(re.search(p, low) for _, p in GROUND_WORDS):
        a["procurement_method"] = "sole_source"
        ground = next((g for g, p in GROUND_WORDS if re.search(p, low)), None)
        amount = sum(l["quantity"] * l["unit_price"] for l in lines)
        if not ground and lines and amount <= e.config.get("compliance", {}).get("low_value_limit", 25000):
            ground = "low_value"
        if ground:
            a["sole_source_ground"] = ground
        else:
            notes.append("Sole source detected, but no GCR s.6 exception is clear. Pick one.")
        a["sole_source_justification"] = t
        a["competitive_quotes"] = 0
    else:
        a["procurement_method"] = "competitive"
        quotes = re.search(r"(\d+)\s+(?:quotes|bids|proposals)", low)
        if quotes:
            a["competitive_quotes"] = int(quotes.group(1))
    if "indigenous" in low:
        a["indigenous_considered"] = "yes"
    for level in ("top secret", "secret", "reliability"):
        if level in low:
            a["security_requirements"] = level.replace(" ", "_")
            break
    if re.search(r"(?:personal|employee|client|citizen|customer) (?:data|information)", low):
        a["handles_customer_data"] = "yes"
    notes.append("Confirm the DMP considerations and the conflict-of-interest declaration before you submit.")
    return {"draft": draft, "notes": notes}


def _check(rule, status, message):
    return {"rule": rule, "status": status, "message": message}


def assess(e, ctx, department_id, exclude_request_id=None):
    c = e.config.get("compliance", {})
    a, amount, out = ctx["answers"], ctx["amount"] or 0, []
    method = a.get("procurement_method")
    low_limit = c.get("low_value_limit", 25000)
    trade = c.get("trade_agreement_threshold", 133800)
    if method not in ("competitive", "sole_source"):
        out.append(_check("GCR s.5", "fail", "Choose a procurement method. GCR s.5 requires bids to be solicited "
                                            "unless a s.6 exception applies."))
    elif method == "competitive":
        out.append(_check("GCR s.5", "pass", "Competitive process: bids will be solicited."))
        if amount >= trade:
            out.append(_check("Trade agreements", "warn", f"At or above ${trade:,.0f}, trade agreements likely apply. "
                                                         "Use an open, publicly posted solicitation on CanadaBuys."))
    else:
        ground = a.get("sole_source_ground")
        just = (a.get("sole_source_justification") or "").strip()
        if ground not in GROUNDS:
            out.append(_check("GCR s.6", "fail", "Sole source needs one of the four GCR s.6 exceptions: "
                                                "emergency, low value, public interest or single supplier."))
        else:
            out.append(_check("GCR s.6", "pass", GROUNDS[ground]))
        if len(just) < c.get("min_justification_chars", 100):
            out.append(_check("DMP: file documentation", "fail", "Write a justification of at least "
                              f"{c.get('min_justification_chars', 100)} characters explaining why the exception "
                              "applies and what alternatives were considered."))
        if ground == "low_value" and amount > low_limit:
            out.append(_check("GCR s.6(b)", "fail", f"${amount:,.2f} exceeds the ${low_limit:,.0f} low-dollar limit. "
                                                   "Compete it or use another exception."))
        if ground == "single_supplier" and amount > c.get("acan_over", 25000) and a.get("acan_posted") != "yes":
            out.append(_check("ACAN", "warn", "Post an Advance Contract Award Notice on CanadaBuys (at least 15 calendar "
                                             "days) so other suppliers can challenge the single-supplier claim."))
        if ground == "emergency":
            out.append(_check("GCR s.6(a)", "warn", "Record the emergency, the date it arose and why a delay would harm the "
                                                   "public interest. Limit the contract to what the emergency needs."))
        if amount > c.get("delegated_sole_source_limit", 25000) and ground != "emergency":
            out.append(_check("Contracting authority", "warn", "This is above your delegated non-competitive limit. "
                              "It must go to PSPC or be approved at the required delegation level."))
        if amount >= trade:
            out.append(_check("Trade agreements", "warn", "Confirm a limited-tendering reason under each applicable trade "
                                                         "agreement (CFTA, CUSMA, CETA, CPTPP, WTO-GPA)."))
        # Contract splitting: the DMP prohibits dividing a requirement to avoid controls or competition.
        since = (e.today() - dt.timedelta(days=c.get("split_window_days", 365))).isoformat()
        prior = e.db.scalar(
            """SELECT COALESCE(SUM(r.amount),0) FROM requests r JOIN vendors v ON v.id=r.vendor_id
               WHERE r.department_id=? AND lower(v.name)=lower(?) AND r.status NOT IN ('rejected','cancelled')
                 AND r.created_at>=? AND r.id IS NOT ?""",
            (department_id, ctx["vendor"].get("name", ""), since, exclude_request_id)) or 0
        if prior and amount <= low_limit < prior + amount:
            out.append(_check("DMP: contract splitting", "fail", f"This supplier already has ${prior:,.2f} in requests from "
                              f"your department in the last {c.get('split_window_days', 365)} days. Together that is over "
                              f"${low_limit:,.0f}. Combine the requirement instead of splitting it."))
    missing = [label for key, label in CONSIDERATIONS if not a.get(key)]
    out.append(_check("DMP: mandatory considerations", "fail" if missing else "pass",
                      ("Answer: " + ", ".join(missing) + ".") if missing else
                      "Indigenous participation, security, accessibility and environmental considerations are addressed."))
    coi = a.get("conflict_of_interest_declared")
    out.append(_check("Conflict of interest", "pass" if coi == "yes" else "fail",
                      "Declared none." if coi == "yes" else "The requester must declare no conflict of interest."))
    if amount > c.get("proactive_disclosure_over", 10000):
        out.append(_check("Proactive disclosure", "info", f"Contracts over ${c.get('proactive_disclosure_over', 10000):,.0f} "
                                                         "are published in proactive disclosure."))
    return out


def validator(e, ctx, department):
    if not e.config.get("compliance", {}).get("enforce", True):
        return
    fails = [f"{x['rule']}: {x['message']}" for x in assess(e, ctx, department["id"]) if x["status"] == "fail"]
    if fails:
        raise ProcurementError("Not compliant. " + " | ".join(fails))


def install(engine):
    if validator not in engine.validators:
        engine.validators.append(validator)


@route("POST", "/api/intake/parse")
def intake_parse(e, uid, body, q):
    return parse_text(e, body.get("text"))


@route("POST", "/api/intake/compliance")
def intake_compliance(e, uid, body, q):
    ctx, dept = draft_context(e, uid, body)
    return {"checks": assess(e, ctx, dept["id"])}
