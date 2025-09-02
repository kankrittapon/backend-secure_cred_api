# main_backend.py
from __future__ import annotations

import os
import sys
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse

# ==============================
# Path/bootstrap
# ==============================
CURRENT_DIR = os.path.dirname(__file__)
PARENT_DIR = os.path.dirname(CURRENT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.append(PARENT_DIR)

app = FastAPI(title="Main Backend + Credentials + Topups")

# ==============================
# Credentials download (ของเดิม)
# ==============================
TOKEN_FILE_MAP = {
    "69fa5371392bdfe7160f378ef4b10bb6": "branchs.json",
    "1582b63313475631d732f4d1aed9a534": "times.json",
    "a48bca796db6089792a2d9047c7ebf78": "ithitec.json",
    "0857df816fa1952d96c6b76762510516": "pmrocket.json",
    "8155bfa0c8faaed0a7917df38f0238b6": "rocketbooking.json",
    "a2htZW5odWFrdXltYWV5ZWQ=":         "credentials.json",  # Google Service Account
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

# ==============================
# Internal auth
# ==============================
INTERNAL_AUTH_SECRET = os.getenv("INTERNAL_AUTH_SECRET", "").strip()

def _require_internal_auth(request: Request) -> None:
    hdr = request.headers.get("X-Internal-Auth")
    if not INTERNAL_AUTH_SECRET or hdr != INTERNAL_AUTH_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

# ==============================
# Google Sheets client (ย้ายมาจาก utils)
# ==============================
# ต้องมีไฟล์ /etc/secrets/credentials.json (service account)
SPREADSHEET_KEY = os.getenv("SPREADSHEET_KEY", "").strip()
USERS_SHEET_NAME = os.getenv("USERS_SHEET_NAME", "Users").strip() or "Users"
TOPUP_SHEET_NAME = os.getenv("TOPUP_SHEET_NAME", "Topups").strip() or "Topups"

def create_gsheet_client():
    try:
        import gspread  # type: ignore
    except Exception as e:
        raise RuntimeError("gspread is not installed in this environment") from e

    cred_path = os.path.join(SECRET_PATH_PREFIX, "credentials.json")
    if not os.path.exists(cred_path):
        raise RuntimeError("credentials.json not found in /etc/secrets")
    try:
        # ใช้ไฟล์โดยตรง (ง่าย/เร็ว)
        gc = gspread.service_account(filename=cred_path)
        return gc
    except Exception as e:
        raise RuntimeError(f"cannot create gspread client: {e}")

def open_google_sheet(client, key):
    try:
        return client.open_by_key(key)
    except Exception as e:
        raise RuntimeError(f"open_by_key failed: {e}")

# ==============================
# Role mapping & policy
# ==============================
def _load_role_map() -> dict[float, str]:
    """
    default map: 1500→vipi, 2500→vipii, 3500→vipiii
    override ได้ด้วย ENV: ROLE_MAP_JSON='{"1500":"vipi","2500":"vipii","3500":"vipiii"}'
    """
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
EXPAND_MONTHS = int(os.getenv("ROLE_MONTHS", "1"))  # ต่ออายุเพิ่มกี่เดือน (default 1)

# สิทธิ์การใช้งานแต่ละ role (คอลัมน์ D/E ของแท็บ Users)
ROLE_POLICY = {
    "vipi":   {"sites": 3,  "can_prebook": True},
    "vipii":  {"sites": 6,  "can_prebook": True},
    "vipiii": {"sites": 10, "can_prebook": True},
    "normal": {"sites": 0,  "can_prebook": False},
    "admin":  {"sites": 999, "can_prebook": True},  # กันเหนียว
}

# ==============================
# Helpers (sheet ops)
# ==============================
def _dt_yyyymmdd(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _find_topup_by_txid(client, txid: str) -> Tuple[object, Optional[dict], Optional[int]]:
    ss = open_google_sheet(client, SPREADSHEET_KEY)
    ws = ss.worksheet(TOPUP_SHEET_NAME)
    records = ws.get_all_records()
    for idx, rec in enumerate(records, start=2):  # header = row1
        if str(rec.get("TxID", "")).strip().upper() == txid.strip().upper():
            return ws, rec, idx
    return ws, None, None

def _get_user_row(client, username: str) -> Tuple[object, Optional[dict], Optional[int]]:
    ss = open_google_sheet(client, SPREADSHEET_KEY)
    ws = ss.worksheet(USERS_SHEET_NAME)
    records = ws.get_all_records()
    for idx, rec in enumerate(records, start=2):
        if str(rec.get("Username", "")).strip() == str(username).strip():
            return ws, rec, idx
    return ws, None, None

def _get_user_role(client, username: str) -> Optional[str]:
    _, rec, _ = _get_user_row(client, username)
    if rec:
        return str(rec.get("Role", "")).strip().lower() or None
    return None

def _is_admin_role(role: Optional[str]) -> bool:
    return (role or "").strip().lower() == "admin"

# ==============================
# Core Topup functions (ย้ายมาจาก utils)
# ==============================
def record_topup_request(user_info: dict, amount: float, method: str, note: Optional[str] = None) -> dict:
    """
    เขียนคำขอ topup ลงแท็บ Topups:
    Header คาดว่า: TxID | Username | Amount | Method | Note | Status | RequestedAtISO | ProofLink | ReviewedAtISO | AdminNote
    """
    username = str((user_info or {}).get("Username") or "-").strip() or "-"
    txid = uuid.uuid4().hex[:12].upper()

    client = create_gsheet_client()
    ss = open_google_sheet(client, SPREADSHEET_KEY)
    ws = ss.worksheet(TOPUP_SHEET_NAME)

    row = [
        txid,
        username,
        float(amount),
        method,
        (note or ""),
        "Pending",            # Status
        _iso_now(),           # RequestedAtISO
        "",                   # ProofLink
        "",                   # ReviewedAtISO
        "",                   # AdminNote
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return {"TxID": txid}

def update_topup_status_paid(txid: str, amount: Optional[float], provider: str, provider_txn_id: str) -> bool:
    """
    เปลี่ยนสถานะรายการใน Topups เป็น Approved
    - ถ้า amount ถูกส่งมา และไม่ตรงกับ Amount เดิมในชีต => return False
    - เขียน ReviewedAtISO และ AdminNote (provider info)
    """
    client = create_gsheet_client()
    ws, rec, row_idx = _find_topup_by_txid(client, txid)
    if not rec or not row_idx:
        return False

    # amount guard ถ้าส่งมา
    if amount is not None:
        try:
            amt_sheet = round(float(rec.get("Amount", 0.0)), 2)
        except Exception:
            amt_sheet = None
        if amt_sheet is not None and round(float(amount), 2) != amt_sheet:
            return False

    # เขียนสถานะ + note
    reviewed_at = _iso_now()
    admin_note = f"provider={provider}; txn={provider_txn_id}"

    # col mapping: F:Status, I:ReviewedAtISO, J:AdminNote  (ตาม header ด้านบน)
    ws.update_acell(f"F{row_idx}", "Approved")
    ws.update_acell(f"I{row_idx}", reviewed_at)
    ws.update_acell(f"J{row_idx}", admin_note)
    return True

def _update_user_role_and_expiration(client, username: str, new_role: str, months: int = 1) -> bool:
    """
    อัปเดตในแท็บ Users ตามคอลัมน์:
      C: Role
      D: สามารถตั้งจองล่วงหน้าได้กี่ site
      E: ตั้งจองล่วงหน้าได้ไหม (TRUE/FALSE)
      F: Expiration date (YYYY-MM-DD)

    * ถ้าผู้ใช้งานปัจจุบันเป็น admin จะ "ไม่เขียนทับ" role/สิทธิ์
    """
    if not username or username == "-":
        return False

    ss = open_google_sheet(client, SPREADSHEET_KEY)
    ws = ss.worksheet(USERS_SHEET_NAME)
    records = ws.get_all_records()

    row_idx = None
    cur_role = None
    for idx, rec in enumerate(records, start=2):
        if str(rec.get("Username", "")).strip() == str(username).strip():
            row_idx = idx
            cur_role = str(rec.get("Role", "")).strip().lower()
            break
    if not row_idx:
        return False

    if _is_admin_role(cur_role):
        return True  # อย่าไปลดสิทธิ์ admin

    new_exp = datetime.utcnow() + timedelta(days=30 * max(1, months))
    ws.update_acell(f"C{row_idx}", str(new_role))
    ws.update_acell(f"F{row_idx}", _dt_yyyymmdd(new_exp))
    
    policy = ROLE_POLICY.get(new_role.lower())
    if policy:
        ws.update_acell(f"D{row_idx}", str(policy["sites"]))
        ws.update_acell(f"E{row_idx}", "TRUE" if policy["can_prebook"] else "FALSE")

    return True

# ==============================
# Internal APIs
# ==============================
@app.post("/internal/topups/request")
async def topups_request(request: Request):
    """
    เรียกจาก Payment Backend/Worker ตอนจะสร้าง Checkout Session
    - admin: เติมเท่าไรก็ได้ (>0)
    - non-admin: บังคับยอดต้องตรง ROLE_MAP (1500/2500/3500 โดยดีฟอลต์)
    """
    _require_internal_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    # รองรับทั้ง user.Username และ username ตรง ๆ
    user = body.get("user") or {}
    username = str(user.get("Username") or body.get("username") or "-").strip() or "-"
    amount = body.get("amount")
    method = body.get("method") or "Stripe/Checkout"
    description = body.get("description") or "Top-up"

    try:
        amt = float(amount)
        if amt <= 0:
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="amount must be positive float")

    # ตรวจ role ผู้ใช้ (เพื่อทราบว่าเป็น admin หรือไม่)
    try:
        client = create_gsheet_client()
        role = _get_user_role(client, username) if username and username != "-" else None
    except Exception:
        role = None

    is_admin = _is_admin_role(role)

    if (not is_admin) and ROLE_MAP and round(amt, 2) not in ROLE_MAP:
        allowed = ", ".join(
            str(int(a)) if float(a).is_integer() else str(a)
            for a in sorted(ROLE_MAP)
        )
        raise HTTPException(status_code=400, detail=f"amount must be one of {{{allowed}}}")

    try:
        rec = record_topup_request({"Username": username}, amt, method, description)
        txid = str(rec.get("TxID") or rec.get("txid") or "").strip()
        if not txid:
            raise RuntimeError("no TxID returned")
        return {"TxID": txid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"record_topup_request failed: {e}")

@app.post("/internal/topups/mark-paid")
async def topups_mark_paid(request: Request):
    """
    เรียกจาก Payment Webhook/Worker เมื่อชำระสำเร็จ:
      - Mark Topups = Approved (+ ReviewedAtISO, AdminNote)
      - ถ้า non-admin: อัปเดต Users (Role + Expiration + สิทธิ์ sites/prebook)
    """
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

    try:
        client = create_gsheet_client()

        # ใช้เพื่อเช็ค idempotency และรู้ username + amount เดิม
        ws_top, rec_top, row_idx = _find_topup_by_txid(client, txid)
        if rec_top:
            status_cur = str(rec_top.get("Status", "")).strip().lower()
            if status_cur in ("approved", "paid"):
                return {"ok": True}

        ok = update_topup_status_paid(
            txid=txid,
            amount=amount_f,
            provider=provider,
            provider_txn_id=provider_txn_id,
        )
        if not ok and amount_f is None:
            ok = update_topup_status_paid(
                txid=txid,
                amount=None,
                provider=provider,
                provider_txn_id=provider_txn_id,
            )
        if not ok:
            return JSONResponse({"ok": False, "error": "update_topup_status_paid failed"}, status_code=200)

        # อัปเดต Users (ถ้าไม่ใช่ admin)
        if rec_top:
            username = str(rec_top.get("Username", "")).strip()
            role_now = _get_user_role(client, username) or "normal"
            if not _is_admin_role(role_now):
                amt_sheet = rec_top.get("Amount")
                try:
                    amt_sheet = round(float(amt_sheet), 2)
                except Exception:
                    amt_sheet = amount_f

                if amt_sheet is not None and ROLE_MAP:
                    desired_role = ROLE_MAP.get(amt_sheet)
                    if desired_role:
                        _update_user_role_and_expiration(client, username, desired_role, months=EXPAND_MONTHS)

        return {"ok": True}

    except Exception as e:
        # ป้องกัน retry-storm -> ตอบ 200 + ok:false
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
