import os
import secrets
from pathlib import Path
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from calculations import poultry, water, commission
from supabase_backend import read_businesses, DatabaseUnavailable

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
