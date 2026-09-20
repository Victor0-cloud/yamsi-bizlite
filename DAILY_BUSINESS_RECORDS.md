# Daily Business Records — Accepted WhatsApp Message Formats (Stage 1)

Deterministic intake for YAMSI BizLite water-business daily records.
Applies ONLY to the AMOSE water-business scope (`amose_table_water`,
Asaba and Warri branches). Every other business keeps its previous
extraction behavior exactly (Nughe Farms Warri keeps poultry parsing;
all other scopes keep generic sale parsing).
Parsing is conservative: anything not explicitly present is reported in
`missing_fields` for clarification, never inferred. One record per
message — a message matching two kinds is refused as ambiguous.

Raw intake creates **drafts only**. Nothing posts until a human reviewer
confirms via `REVIEW CONFIRM <YR-REF> KEY <key>` (sale reports) or API
review (all other kinds). Reviewer messages never expose internal UUIDs.

## Production

- `Produced 250 good bags, 5 rejected`
- `Production today: produced 300 bags, 12 damaged`
- Fields: `good_quantity`, `rejected_quantity` (when stated),
  `production_date` (explicit `YYYY-MM-DD`, else the inbox receipt date),
  `shift` (`morning`/`afternoon`/`night`/`full_day` when stated).
  `product_id` always needs reviewer resolution.

## Sale

- `Sold 100 bags at 500 cash`
- Fields: `quantity`, `unit`, `unit_price` (naira, as written),
  `currency` (NGN when `NGN`/`naira`/`₦` present),
  `payment_method` (`cash`/`transfer`/`pos` when stated).

## Payment received

- `Received 50000 cash for sale YR-XXXXXXXXXX`
- `Received NGN 50,000 cash for sale YR-XXXXXXXXXX`
- Fields: `amount_kobo` (integer kobo; `₦`/`NGN`/`naira`, commas and
  kobo decimals accepted), `method` (when stated), `sale_ref` (the sale
  reference — never inferred when absent).

## Expense

- `Spent 20000 on tricycle fuel cash`
- `Spent 15000 on ATWAP dues transfer`
- Fields: `amount_kobo`, `category` (stable machine value),
  `description` (staff wording, always preserved), `payment_method`
  (when stated).
- Categories: `fuel`, `maintenance`, `salaries` (incl. operator pay),
  `transport`, `packaging`, `utilities`, `rent`, `purchases`, `other`
  (incl. miscellaneous), `task_force` (task-force payments),
  `atwap_dues` (ATWAP dues), `tricycle_service` (tricycle service).

## Cash handover

- `Michael handed 80000 cash to Vivian`
- Fields: `amount_kobo`, `from_name`, `to_name` (staff-written names).
- Confirmation resolves both employees from authoritative scoped records
  (same tenant, business, branch). Self-handover and cross-scope
  handovers are refused. The custody row keeps giver (`from_custodian_id`)
  and receiver (`custodian_id`).

## Bank deposit

- `Vivian deposited 70000 to Access Bank, reference ABC123`
- Fields: `amount_kobo`, `depositor_name`, `destination_account`,
  `reference` (required — a deposit is never confirmed without one).
- Confirmation requires an **owner-approved destination account**: the
  latest effective `biz_setting_versions` row for the exact
  tenant/business/branch with key `approved_bank_deposit_accounts` and
  value `{"accounts": [{"name": "Access Bank", "reference": "ACC-01"},
  ...]}`. Matching trims whitespace and ignores case; the custody
  record keeps the canonical approved name. A missing, malformed, or
  empty setting — or an unapproved account — fails closed. A deposit is
  never confirmed from raw text alone.
- Duplicate prevention is scoped by tenant, business, branch, **and**
  destination account, compared case-insensitively after trimming, so
  trivial variants cannot bypass it.

## Operator pay

No message pays an operator, and no caller-supplied bag total is ever
accepted. `preview_operator_pay_for_work(tenant, business, branch,
operator, date_from, date_to)` sums `good_quantity` from
`status='confirmed'` production runs for that exact scope, operator, and
inclusive work period (drafts/voids excluded, rejected bags never read)
after validating the operator's assignment, then applies the
owner-confirmed `operator_piece_rate_per_bag` setting. Without the
setting the preview returns `rate not configured` (still reporting the
confirmed total and source period). Read-only: no expense, payment, or
cash movement is created until separately confirmed.

## Multi-branch senders

With more than one assignment, start the message with `<branch>:` naming
exactly one assigned branch, e.g. `ASABA: Sold 100 bags at 500 cash`.
Otherwise a branch-clarification reply is queued and nothing is submitted.
