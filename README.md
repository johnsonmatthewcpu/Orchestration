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
