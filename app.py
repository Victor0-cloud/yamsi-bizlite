import os
import secrets
from pathlib import Path
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from calculations import poultry, water, commission
from supabase_backend import read_businesses, DatabaseUnavailable, TENANT
from message_processor import process_inbox_batch
from owner_query import answer as owner_query_answer
from brain import answer_owner_question, load_reference_data

app = FastAPI(title="YAMSI BizLite Brain", version="0.1.0")
from whatsapp_webhook import router
app.include_router(router)
ROOT = Path(__file__).parent

def require_api_key(x_yamsi_key: str = Header(default="")):
    configured = os.environ.get("YAMSI_API_KEY")
    if not configured:
        raise HTTPException(503, "API key not configured; protected API is locked")
    if not secrets.compare_digest(x_yamsi_key, configured):
        raise HTTPException(401, "Invalid credentials")

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(ROOT / "dashboard.html")

@app.get("/health")
def health():
    return {"status":"ok", "identity":"YAMSI BizLite Brain", "stage":"local foundation"}

@app.get("/business/list", dependencies=[Depends(require_api_key)])
def businesses():
    try:
        return read_businesses()
    except DatabaseUnavailable:
        raise HTTPException(503, "Business database unavailable; check server configuration")


def calculate(fn, data):
    try:
        return fn(data)
    except (ValueError, KeyError, TypeError, ArithmeticError) as error:
        raise HTTPException(422, "Invalid or missing calculation inputs") from error

@app.post("/calculate/poultry-profit", dependencies=[Depends(require_api_key)])
def poultry_profit(data: dict):
    return calculate(poultry, data)

@app.post("/calculate/water-profit-v2", dependencies=[Depends(require_api_key)])
def water_profit(data: dict):
    return calculate(water, data)

@app.post("/calculate/commission", dependencies=[Depends(require_api_key)])
def commission_release(data: dict):
    return calculate(commission, data)

@app.get("/amose", include_in_schema=False)
def amose_public_page():
    return FileResponse(ROOT / "amose.html")

@app.post("/internal/process-whatsapp-inbox", dependencies=[Depends(require_api_key)])
async def process_whatsapp_inbox():
    try:
        return await process_inbox_batch()
    except DatabaseUnavailable:
        raise HTTPException(503, "Inbox processing unavailable; check server configuration")

@app.post("/internal/owner-query", dependencies=[Depends(require_api_key)])
async def owner_query_endpoint(data: dict):
    # Only the owner holds YAMSI_API_KEY today; there is no separate staff
    # login, so a caller reaching this protected route is treated as the
    # owner. Answers ONLY from recorded rows -- see owner_query.py / brain.py.
    question = data.get("question", "")
    try:
        if data.get("business_id") or data.get("branch_id") or data.get("employee_id"):
            # Caller already knows the exact scope -- skip entity resolution.
            return await owner_query_answer(TENANT, question, is_owner=True,
                business_id=data.get("business_id"), branch_id=data.get("branch_id"),
                employee_id=data.get("employee_id"), on_date=data.get("date"))
        # Pure natural language: let the Brain resolve employee/business/branch
        # names from real recorded rows (never guessing between ambiguous matches).
        known_employees, known_businesses = await load_reference_data(TENANT)
        return await answer_owner_question(TENANT, question, is_owner=True,
            known_employees=known_employees, known_businesses=known_businesses)
    except DatabaseUnavailable:
        raise HTTPException(503, "Owner query unavailable; check server configuration")
