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

load_config()
