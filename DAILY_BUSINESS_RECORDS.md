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
- Confirmation creates a single `deposit_confirmed` custody entry;
  reusing an already-recorded reference is refused as a duplicate.

## Operator pay

No message pays an operator. A preview is computed only from confirmed
good-production bags and the owner-confirmed `operator_piece_rate_per_bag`
setting. Without the setting the preview returns `rate not configured`,
and no expense or cash movement is created until separately confirmed.

## Multi-branch senders

With more than one assignment, start the message with `<branch>:` naming
exactly one assigned branch, e.g. `ASABA: Sold 100 bags at 500 cash`.
Otherwise a branch-clarification reply is queued and nothing is submitted.
