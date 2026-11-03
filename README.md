# ProcureFlow

A procurement orchestration tool written in Python, modeled on the features Zip (ziphq.com) describes publicly: intake-to-procure, approval routing, budgets, POs, three-way invoice matching, vendor onboarding and contract renewals. It is an original implementation that uses only the Python standard library (3.10+), with no Zip code or private APIs.

## Run it

```bash
python -m procureflow            # opens on http://127.0.0.1:8000 and loads demo data into procureflow.db
```

Open the URL and use **Acting as** (top right) to switch between demo users, for example the requester, their manager, IT, legal, finance and AP.

Other options: `--db PATH`, `--port N`, `--host 0.0.0.0`, `--config my_workflow.json`, `--no-seed`.
`python -m procureflow jobs` runs the renewal/expiry jobs once; schedule it daily with cron.

## Try this walkthrough
1. As **Jordan Engineer**, open **New request** and fill it in. The questions change based on earlier answers, and the preview shows the budget and the approval chain the request will get.
2. Switch to **Morgan Manager**, open **My approvals** and approve. Then approve each later step as IT, Legal and Finance.
3. Once the last approval is in, a PO is issued, a new vendor becomes active, and a contract is recorded.
4. Go to **POs & receiving** and record what was received. Then, as **Alex Payables**, go to **Invoices** and enter an invoice. It is checked against the PO and the receipts (a three-way match); a mismatch is flagged as an exception.
5. Approve the invoice and mark it paid. The PO closes, and the budget's "Paid" figure goes up.
6. Under **Contracts**, click **Run renewal / expiry jobs now** to start a renewal request ahead of the notice deadline.

## Customizing the workflow (`procureflow/workflow.json`)
- `intake_questions`: form fields. `show_if` makes a question appear only when its condition is true.
- `approval_rules`: each rule has a `when` condition and an `approver`: `manager`, `role:<role>` or `user:<email>`. Rules with the same `stage` run in parallel, and stages run in order.
- Conditions: `{"field": "amount", "op": ">=", "value": 5000}`, which can be combined with `all`, `any` and `not`. Available fields: `amount`, `category`, `department`, `answers.<id>`, `vendor.is_new`, `vendor.risk_tier`, `budget.over`, `budget.remaining`.
- `invoice_matching`: price and amount tolerances, as percentages.

## Built-in controls
- Requesters can't approve their own requests.
- Rejections, and approvals of invoices with match exceptions, require a comment.
- Blocked vendors can't be used.
- Duplicate invoice numbers from the same vendor are rejected.
- You can't receive more than was ordered.
- Every action is recorded in the audit log.

## API and webhooks
The JSON API is under `/api/*`; see `procureflow/server.py`. Send the acting user's id in the `X-User-Id` header. Webhooks (`POST /api/webhooks`, admin only) receive events such as `request.submitted`, `approval.requested`, `purchase_order.issued` and `invoice.exception`, which you can use to connect Slack, Teams or your ERP.

## Before production use
- `X-User-Id` is a testing convenience, not authentication. Put SSO or an authenticating reverse proxy in front.
- Use a backed-up database file, and add ERP sync (NetSuite, SAP, etc.) through webhooks.

## Tests
```bash
python -m unittest discover -s tests -t .
```

## Microsoft Entra ID, Teams, clauses and ADM reporting

Everything here is configured with environment variables, so nothing changes in the code.

| Variable | Purpose |
|---|---|
| `PROCUREFLOW_AUTH=entra` | Sign in with Microsoft Entra ID. Turn on App Service Authentication with the Microsoft provider. Users are created on first sign-in, and Entra app roles named `adm`, `cfo`, `finance`, `ap`, `procurement`, `legal`, `security`, `it` or `admin` set their role. An admin then sets each person's department and manager (`POST /api/users/{id}`). |
| `TEAMS_WEBHOOK_URL` | Posts Teams cards when an approval is needed, a request is rejected, or a PO is issued (with a link to the contract package). Create the URL in Teams: channel → Workflows → "Post to a channel when a webhook request is received". |
| `PROCUREFLOW_INTEGRATION_KEY` | Shared secret for approving in Teams (see below). |
| `APP_BASE_URL` | The app's public URL, used for links in the Teams cards. |

**Approving inside Teams (Power Automate):**
1. Register the flow's HTTP trigger URL as a webhook for the `approval.requested` event (`POST /api/webhooks`).
2. Add the action "Start and wait for an approval" and assign it to `data.approver_emails`.
3. Add an HTTP action that POSTs `{"key", "request_id", "approver_email", "decision", "comment"}` to `/api/integrations/teams/decision`, using the responder's email and the outcome.

**Full SACC inventory:** on a computer with internet access, run `python -m procureflow sync-clauses --out sacc_clauses.csv`. It crawls the SACC Manual on canadabuys.canada.ca, keeps the newest version of each clause, imports them all, and saves a CSV you can commit or load elsewhere with `import-clauses`. Re-run it to pick up updates.

**SACC clauses (CanadaBuys):** alternatively, export or compile the clauses you use as a CSV with the columns `code,title,text,version_date,source_url`, then run `python -m procureflow import-clauses clauses.csv`. Rules in `workflow.json` (`clause_rules`) pick clauses for each request. The SACC codes there are examples, so confirm them against the SACC Manual. The contract package at `/api/requests/{id}/package` includes the request details, the approval record and the full clause text, and flags any clause missing from the library.

**ADM dashboard:** open `/adm.html` (for the adm, cfo, finance and procurement roles). It shows budget use, committed and paid spend, overdue approvals, bottlenecks, top vendors and renewals, with CSV export and a button that posts a summary card to Teams. Budgets are by calendar year for now; government fiscal years (April–March) are not supported yet.

## Plain-language intake and Government of Canada compliance
On **New request**, describe the need in plain language and click **Fill in the form**. ProcureFlow drafts the request (title, category, supplier, cost, term, procurement method, and the sole-source exception if one applies). You then review and complete it.

Each draft is checked live, and submission is blocked until every failure is fixed (`procureflow/compliance.py`):
- **Government Contracts Regulations s.5–6:** bids must be solicited unless a sole source rests on one of the four s.6 exceptions: emergency, low value (≤ $25,000), public interest, or single supplier.
- **Directive on the Management of Procurement:** a written justification on file; checks for contract splitting with the same supplier over the last 365 days; Indigenous participation, security, accessibility and environmental considerations; and a conflict-of-interest declaration.
- **Warnings** (these don't block): ACAN posting for single-supplier claims, amounts above your delegated authority, trade-agreement thresholds, and proactive disclosure.
- Every sole source gets a **contracting authority review** step in the approval chain.

All limits are in `workflow.json` under `compliance`. Confirm them against the current regulations, your department's delegation instrument and the current trade-agreement thresholds before production use.
