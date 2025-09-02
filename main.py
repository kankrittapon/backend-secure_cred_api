from __future__ import annotations

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
import os
import json
import sys
from datetime import datetime, timedelta

# Ensure we can import utils.py from parent directory
CURRENT_DIR = os.path.dirname(__file__)
PARENT_DIR = os.path.dirname(CURRENT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.append(PARENT_DIR)

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
# Internal Topups API (protected by X-Internal-Auth)
# -----------------------------------------------------------------------------
INTERNAL_AUTH_SECRET = os.getenv("INTERNAL_AUTH_SECRET", "").strip()
def _require_internal_auth(request: Request) -> None:
    hdr = request.headers.get("X-Internal-Auth")
    if not INTERNAL_AUTH_SECRET or hdr != INTERNAL_AUTH_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

# Import utils (Google Sheets integration)
try:
    from utils import (
        record_topup_request,
        update_topup_status_paid,
        create_gsheet_client,
        open_google_sheet,
        SPREADSHEET_KEY,
        TOPUP_SHEET_NAME,
    )  # type: ignore
except Exception as e:  # pragma: no cover
    def record_topup_request(user_info, amount, method, note=None):  # type: ignore
        raise RuntimeError("utils.record_topup_request unavailable: ensure utils.py is accessible")
    def update_topup_status_paid(txid, amount, provider, provider_txn_id):  # type: ignore
        raise RuntimeError("utils.update_topup_status_paid unavailable: ensure utils.py is accessible")
    def create_gsheet_client():  # type: ignore
        raise RuntimeError("utils.create_gsheet_client unavailable")
    def open_google_sheet(client, key):  # type: ignore
        raise RuntimeError("utils.open_google_sheet unavailable")
    SPREADSHEET_KEY = ""  # type: ignore
    TOPUP_SHEET_NAME = "Topups"  # type: ignore

# ---------------- Role mapping & policy ----------------
# ค่าเริ่มต้น: 1500->vipi, 2500->vipii, 3500->vipiii
# สามารถ override ด้วย ENV ROLE_MAP_JSON เช่น {"1500":"vipi","2500":"vipii","3500":"vipiii"}
import math
def _load_role_map() -> dict[float, str]:
    raw = os.getenv("ROLE_MAP_JSON", "").strip()
    if raw:
        try:
            obj = json.loads(raw)
            m: dict[float, str] = {}
            for k, v in obj.items():
                m[round(float(k), 2)] = str(v)
            return m
        except Exception:
            pass
    return {1500.0: "vipi", 2500.0: "vipii", 3500.0: "vipiii"}

ROLE_MAP = _load_role_map()
EXPAND_MONTHS = int(os.getenv("ROLE_MONTHS", "1"))  # ต่ออายุ +กี่เดือน (ดีฟอลต์ 1)

USERS_SHEET_NAME = os.getenv("USERS_SHEET_NAME", "Users")  # แท็บ Users

def _dt_yyyymmdd(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")

def _parse_date(val) -> datetime | None:
    if not val and val != 0:
        return None
    s = str(val).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None

def _find_topup_by_txid(client, txid: str):
    ss = open_google_sheet(client, SPREADSHEET_KEY)
    ws = ss.worksheet(TOPUP_SHEET_NAME)
    records = ws.get_all_records()
    for idx, rec in enumerate(records, start=2):  # header row = 1
        if str(rec.get("TxID", "")).strip().upper() == txid.strip().upper():
            return ws, rec, idx
    return None, None, None

def _update_user_role_and_expiration(client, username: str, new_role: str, months: int = 1) -> bool:
    if not username or username == "-":
        return False
    ss = open_google_sheet(client, SPREADSHEET_KEY)
    ws = ss.worksheet(USERS_SHEET_NAME)
    records = ws.get_all_records()
    row_idx = None
    cur_exp = None
    for idx, rec in enumerate(records, start=2):
        if str(rec.get("Username", "")).strip() == str(username).strip():
            row_idx = idx
            cur_exp = rec.get("Expiration date", "")
            break
    if not row_idx:
        return False  # ไม่พบผู้ใช้

    # คำนวณวันหมดอายุใหม่ = วันนี้ + months (ไม่ซับซ้อนเรื่องเดือน 28/29/30/31)
    new_exp = datetime.utcnow() + timedelta(days=30 * max(1, months))
    # เขียน Role และ Expiration date
    ws.update(f"C{row_idx}", str(new_role))               # คอลัมน์ Role (สมมติอยู่คอลัมน์ C)
    ws.update(f"F{row_idx}", _dt_yyyymmdd(new_exp))       # คอลัมน์ Expiration date (สมมติอยู่คอลัมน์ F)
    return True

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

    # (ออปชัน) เข้มงวดยอดตาม ROLE_MAP ถ้าต้องการ
    if ROLE_MAP and round(amt, 2) not in ROLE_MAP:
        allowed = ", ".join(str(int(a)) if a.is_integer() else str(a) for a in sorted(ROLE_MAP))
        raise HTTPException(status_code=400, detail=f"amount must be one of {{{allowed}}}")

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
            amount_f = round(float(amount), 2)
        except Exception:
            raise HTTPException(status_code=400, detail="amount must be float or null")

    # ----- Idempotent + Role mapping -----
    try:
        client = create_gsheet_client()

        # หาแถวใน Topups ด้วย TxID เพื่อรู้ Username และยอด
        ws_top, rec_top, row_idx = _find_topup_by_txid(client, txid)
        if not rec_top:
            # ถ้าไม่พบก็ยัง mark-paid ได้ (เฉพาะอัปเดตสถานะ) แต่จะอัปเดต Users ไม่ได้
            pass
        else:
            # ถ้า Status เป็น Approved อยู่แล้ว ให้ตอบ ok ทันที (idempotent)
            status_cur = str(rec_top.get("Status", "")).strip().lower()
            if status_cur in ("approved", "paid"):
                return {"ok": True}

        # อัปเดตสถานะใน Topups เป็น Approved (และเขียน admin note)
        ok = update_topup_status_paid(
            txid=txid,
            amount=amount_f,               # ถ้า amount_f != Amount ในชีตจะ return False (utils ออกแบบไว้)
            provider=provider,
            provider_txn_id=provider_txn_id,
        )

        # ถ้า amount mismatch ให้พยายามผ่านแบบไม่เช็คยอด (เผื่อ webhook ส่งมาเป็น None)
        if not ok and amount_f is None:
            ok = update_topup_status_paid(
                txid=txid,
                amount=None,
                provider=provider,
                provider_txn_id=provider_txn_id,
            )

        # ถ้าทำ Topups ไม่สำเร็จ ก็จบด้วย ok=false
        if not ok:
            return JSONResponse({"ok": False, "error": "update_topup_status_paid failed"}, status_code=200)

        # ---- อัปเดต Users: map amount -> role (+ต่ออายุ) ----
        if rec_top:
            username = str(rec_top.get("Username", "")).strip()
            amt_sheet = rec_top.get("Amount")
            try:
                amt_sheet = round(float(amt_sheet), 2)
            except Exception:
                amt_sheet = amount_f  # fallback จาก webhook

            # เช็คยอดตาม ROLE_MAP (ถ้าไม่ตรง ให้ข้ามการอัปเดต Users แต่ยังตอบ ok)
            if amt_sheet is not None and ROLE_MAP:
                desired_role = ROLE_MAP.get(amt_sheet)
                if desired_role:
                    _update_user_role_and_expiration(client, username, desired_role, months=EXPAND_MONTHS)

        return {"ok": True}

    except Exception as e:
        # ป้องกัน retry-storm: ตอบ 200 พร้อม ok:false
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
