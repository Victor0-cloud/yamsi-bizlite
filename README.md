# YAMSI BizLite Brain
Fresh FastAPI backend for AMOSE Water, Nughe Farms and Phone Center.

## Status
31 local tests pass. Dashboard is a preview, not a live owner portal.
Supabase business directory connection is implemented.
WhatsApp verification and signed inbox ingestion are implemented; deployment and Meta verification remain pending.
No automatic accounting posting, conversational workflow, owner login or GPS integration yet.

## Deploy
Use render.yaml for a separate Render service. Enter secrets privately in Render.
Apply the Supabase inbox permission migration before enabling message delivery.
Keep access logging disabled so webhook verification tokens do not appear in logs.
Read WHATSAPP_DEPLOYMENT.md for setup and verification requirements.

## Tests
python -m unittest discover -v
