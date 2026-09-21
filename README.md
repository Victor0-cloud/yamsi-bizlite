# YAMSI BizLite Brain
Fresh FastAPI backend for AMOSE Water, Nughe Farms and Phone Center.

## Status
651 local tests pass (run `.\.venv\Scripts\python.exe -m pytest -q`).
Dashboard is a preview, not a live owner portal.
Supabase business directory connection is implemented.
WhatsApp signed webhook ingestion, deterministic draft intake, human
confirmation with atomic posting, queued outbound dispatch with bounded
retry, stale-claim recovery, and delivery-status sync are implemented;
live Meta/Render activation is a manual runbook step (see below).
Raw WhatsApp messages create drafts only -- nothing posts financial or
operational records without human confirmation.

## Deploy
Use render.yaml for a separate Render service. Enter secrets privately in Render.
Apply the Supabase migrations in order before enabling message delivery.
Keep access logging disabled so webhook verification tokens do not appear in logs.
Read WHATSAPP_DEPLOYMENT.md for the live activation runbook (Meta app,
token, webhook, provider-account registration, safe inbound/outbound
tests, rollback, rotation, incident recovery).
DAILY_BUSINESS_RECORDS.md documents accepted staff message formats.

## Tests
.\.venv\Scripts\python.exe -m pytest -q
SQL probes (local shadow database only):
supabase/probes/daily_business_records_probes.sql
supabase/probes/live_whatsapp_probes.sql
