# WhatsApp webhook stage
The fresh C-drive app.py project is authoritative. The old YAMSI repository and server are outside scope.
Deploy this project as a separate Render service using render.yaml.

## Routes
GET /webhooks/whatsapp: subscription verification, plain-text challenge.
POST /webhooks/whatsapp: raw-body HMAC SHA256 verification; durable inbox insertion before 200.
Missing secrets or failed storage produce 503. Invalid signatures produce 403.
Unsupported signed events are acknowledged without posting. No accounting records or replies are created.
Message and status event identifiers have separate namespaces. Unique database constraint resolves retry/concurrency conflicts.

## Render configuration
WHATSAPP_VERIFY_TOKEN: strong private value (generated locally in .env).
WHATSAPP_APP_SECRET: Meta app secret, entered privately.
SUPABASE_URL and SUPABASE_SECRET_KEY: same project as local backend.
YAMSI_API_KEY: keep existing protected-route key.
Apply 003_webhook_inbox_insert.sql in Supabase before delivery tests.
Do not commit .env or place secrets in the frontend.

Disable HTTP access logging, because the verification request carries its private token in query parameters.
For this local app only, the equivalent command is:
python -m uvicorn app:app --host 0.0.0.0 --port $PORT --no-access-log
render.yaml disables access logs so verification query tokens are not logged.

## Deployment gate
Publish this fresh project to its own private GitHub repository, then create a separate Render service. Tests must pass before deployment.
Verify public GET with correct/wrong token and POST with valid/invalid signature.
Use isolated test storage for POST delivery fixtures. Confirm retry deduplication in actual PostgreSQL before live use.
Expected callback ONLY after successful deployment:
https://<actual-new-render-host>/webhooks/whatsapp
Then copy verify token privately from Render configuration to Meta and Verify and save.
Remote deployment, RLS integration test, sender identity, conversation flow and accounting posting are not yet complete.
