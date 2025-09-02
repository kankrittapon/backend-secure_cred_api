from __future__ import annotations

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
import os
import json
import sys

# Ensure we can import utils.py from parent directory
CURRENT_DIR = os.path.dirname(__file__)
PARENT_DIR = os.path.dirname(CURRENT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.append(PARENT_DIR)

# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
app = FastAPI(title="Main Backend + Credentials + Topups")

# -----------------------------------------------------------------------------
# Credentials API (existing)
# -----------------------------------------------------------------------------
TOKEN_FILE_MAP = {
    "69fa5371392bdfe7160f378ef4b10bb6": "branchs.json",       # example
    "1582b63313475631d732f4d1aed9a534": "times.json",         # example
    "a48bca796db6089792a2d9047c7ebf78": "ithitec.json",
    "0857df816fa1952d96c6b76762510516": "pmrocket.json",
    "8155bfa0c8faaed0a7917df38f0238b6": "rocketbooking.json",
    "a2htZW5odWFrdXltYWV5ZWQ=":         "credentials.json",   # Google Service Account
}

# Path ที่แพลตฟอร์มโหลด Secret Files เข้าไว้
SECRET_PATH_PREFIX = "/etc/secrets"

@app.get("/")
def root():
    return {"message": "API is running"}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/get-credentials")
async def get_credentials(request: Request):
    token = request.headers.get("X-API-Token")
    if not token or token not in TOKEN_FILE_MAP:
        raise HTTPException(status_code=403, detail="Unauthorized or unknown API token")
    filename = TOKEN_FILE_MAP[token]
    filepath = os.path.join(SECRET_PATH_PREFIX, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"{filename} not found in /etc/secrets")
    return FileResponse(filepath, media_type="application/json", filename=filename)

# -----------------------------------------------------------------------------
# Internal Topups API (new) - protected by X-Internal-Auth
# -----------------------------------------------------------------------------
INTERNAL_AUTH_SECRET = os.getenv("INTERNAL_AUTH_SECRET", "").strip()

def _require_internal_auth(request: Request) -> None:
    hdr = request.headers.get("X-Internal-Auth")
    if not INTERNAL_AUTH_SECRET or hdr != INTERNAL_AUTH_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

# Import top-up helpers from utils (Google Sheets integration)
try:
    from utils import record_topup_request, update_topup_status_paid  # type: ignore
except Exception as e:  # pragma: no cover
    def record_topup_request(user_info, amount, method, note=None):  # type: ignore
        raise RuntimeError("utils.record_topup_request unavailable: ensure utils.py is accessible")
    def update_topup_status_paid(txid, amount, provider, provider_txn_id):  # type: ignore
        raise RuntimeError("utils.update_topup_status_paid unavailable: ensure utils.py is accessible")

@app.post("/internal/topups/request")
async def topups_request(request: Request):
    _require_internal_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    user = body.get("user") or {"Username": "-"}
    amount = body.get("amount")
    method = body.get("method") or "Stripe/Checkout"
    description = body.get("description") or "Top-up"

    try:
        amt = float(amount)
        if amt <= 0:
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="amount must be positive float")

    try:
        rec = record_topup_request(user, amt, method, description)
        txid = str(rec.get("TxID") or rec.get("txid") or "").strip()
        if not txid:
            raise RuntimeError("no TxID returned")
        return {"TxID": txid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"record_topup_request failed: {e}")

@app.post("/internal/topups/mark-paid")
async def topups_mark_paid(request: Request):
    _require_internal_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    txid = str(body.get("txid") or "").strip()
    amount = body.get("amount")  # may be null
    provider = str(body.get("provider") or "Stripe")
    provider_txn_id = str(body.get("provider_txn_id") or "")

    if not txid:
        raise HTTPException(status_code=400, detail="txid required")

    amount_f = None
    if amount is not None:
        try:
            amount_f = float(amount)
        except Exception:
            raise HTTPException(status_code=400, detail="amount must be float or null")

    try:
        ok = update_topup_status_paid(txid=txid, amount=amount_f, provider=provider, provider_txn_id=provider_txn_id)
        return {"ok": bool(ok)}
    except Exception as e:
        # Return 200 with ok:false to prevent retries storms if desired
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
