"""Server-only Supabase access. Never returns credentials or raw upstream errors."""
import os
import re
from pathlib import Path
import httpx

ROOT = Path(__file__).parent
TENANT = "00000000-0000-0000-0000-000000000001"

def load_config():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value)>1 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key in ("SUPABASE_URL","SUPABASE_SECRET_KEY","YAMSI_API_KEY","WHATSAPP_VERIFY_TOKEN","WHATSAPP_APP_SECRET"):
            os.environ.setdefault(key,value)

class DatabaseUnavailable(Exception):
    pass

def credentials():
    url = os.environ.get("SUPABASE_URL","").rstrip("/")
    secret = os.environ.get("SUPABASE_SECRET_KEY","")
    if not re.fullmatch(r"https://[a-z0-9]{20}\.supabase\.co",url):
        raise DatabaseUnavailable("Project URL is missing or invalid. Copy the exact URL from Supabase.")
    if not secret or secret == "your_secret_key_here":
        raise DatabaseUnavailable("Supabase server key is missing.")
    return url, secret

def read_businesses():
    url, secret = credentials()
    headers = {"apikey":secret}
    if secret.startswith("eyJ"):
        headers["Authorization"] = "Bearer " + secret
    try:
        with httpx.Client(base_url=url, headers=headers, timeout=20, follow_redirects=False) as client:
            business = client.get("/rest/v1/biz_businesses", params={"tenant_id":"eq."+TENANT,"select":"id,name,business_type"})
            branches = client.get("/rest/v1/biz_branches", params={"tenant_id":"eq."+TENANT,"select":"business_id,id,name"})
            for response in (business,branches):
                if response.status_code != 200:
                    raise DatabaseUnavailable("Supabase read failed (HTTP " + str(response.status_code) + ").")
            return {"businesses":[{"id":b["id"],"name":b["name"],"type":b["business_type"],
                "branches":[r["id"] for r in branches.json() if r["business_id"]==b["id"]]} for b in business.json()]}
    except (httpx.HTTPError,ValueError,KeyError,TypeError):
        raise DatabaseUnavailable("Unable to read business records from Supabase.") from None

def _headers(secret):
    headers = {"apikey": secret}
    if secret.startswith("eyJ"):
        headers["Authorization"] = "Bearer " + secret
    return headers

async def rest_get(path, params=None):
    """Shared async REST GET for Stage 007+ modules (task_engine, evidence_store,
    rule_engine, owner_query). Raises DatabaseUnavailable on any non-200."""
    url, secret = credentials()
    try:
        async with httpx.AsyncClient(base_url=url, headers=_headers(secret), timeout=8, follow_redirects=False) as client:
            response = await client.get(path, params=params)
    except httpx.HTTPError:
        raise DatabaseUnavailable("Read failed for " + path) from None
    if response.status_code != 200:
        raise DatabaseUnavailable("Read failed for " + path)
    return response.json()

async def rest_post(path, rows, params=None, prefer="resolution=ignore-duplicates,return=minimal"):
    url, secret = credentials()
    headers = _headers(secret)
    headers["Prefer"] = prefer
    try:
        async with httpx.AsyncClient(base_url=url, headers=headers, timeout=8, follow_redirects=False) as client:
            response = await client.post(path, params=params, json=rows)
    except httpx.HTTPError:
        raise DatabaseUnavailable("Write failed for " + path) from None
    if response.status_code not in (200, 201, 204):
        raise DatabaseUnavailable("Write failed for " + path)
    return response

async def rest_patch(path, params, body, prefer="return=minimal"):
    url, secret = credentials()
    headers = _headers(secret)
    headers["Prefer"] = prefer
    try:
        async with httpx.AsyncClient(base_url=url, headers=headers, timeout=8, follow_redirects=False) as client:
            response = await client.patch(path, params=params, json=body)
    except httpx.HTTPError:
        raise DatabaseUnavailable("Update failed for " + path) from None
    if response.status_code not in (200, 204):
        raise DatabaseUnavailable("Update failed for " + path)
    return response

load_config()
