# main_backend.py
from __future__ import annotations

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
import os
import json
import sys
from datetime import datetime, timedelta
from typing import Optional, Tuple

# -----------------------------------------------------------------------------
# Import utils base (เฉพาะสิ่งที่ใช้เชื่อม Google Sheet)
# -----------------------------------------------------------------------------
CURRENT_DIR = os.path.dirname(__file__)
PARENT_DIR = os.path.dirname(CURRENT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.append(PARENT_DIR)

try:
    from utils import (  # type: ignore
        create_gsheet_client,
        open_google_sheet,
        SPREADSHEET_KEY,
        TOPUP_SHEET_NAME,
        USERS_SHEET_NAME,
    )
except Exception:
    def create_gsheet_client():  # type: ignore
        raise RuntimeError("utils.create_gsheet_client unavailable")
    def open_google_sheet(client, key):  # type: ignore
        raise RuntimeError("utils.open_google_sheet unavailable")
    SPREADSHEET_KEY = ""        # type: ignore
    TOPUP_SHEET_NAME = "Topups" # type: ignore
    USERS_SHEET_NAME = "Users"  # type: ignore

# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
app = FastAPI(title="Main Backend + Credentials + Topups")

# =============================================================================
# Credentials API (existing)
# =============================================================================
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

# =============================================================================
# Internal Topups API (protected by X-Internal-Auth)
# =============================================================================
INTERNAL_AUTH_SECRET = os.getenv("INTERNAL_AUTH_SECRET", "").strip()

def _require_internal_auth(request: Request) -> None:
    hdr = request.headers.get("X-Internal-Auth")
    if not INTERNAL_AUTH_SECRET or hdr != INTERNAL_AUTH_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

# =============================================================================
# Role mapping & policy
# =============================================================================
def _load_role_map() -> dict[float, str]:
    """
    แมพยอดเงิน → role
    default: 1500→vipi, 2500→vipii, 3500→vipiii
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
EXPAND_MONTHS = int(os.getenv("ROLE_MONTHS", "1"))  # ต่ออายุ +กี่เดือน (ดีฟอลต์ 1)

# สิทธิ์การใช้งานต่อ role (ชื่อคอลัมน์ไทยในชีต: D/E)
ROLE_POLICY = {
    "vipi":   {"sites": 3,  "can_prebook": True},
    "vipii":  {"sites": 6,  "can_prebook": True},
    "vipiii": {"sites": 10, "can_prebook": True},
    "normal": {"sites": 0,  "can_prebook": False},  # fallback
    "admin":  {"sites": 999,"can_prebook": True},   # กันเผื่อในอนาคต
}

# =============================================================================
# Helpers (Sheets)
# =============================================================================
def _dt_yyyymmdd(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")

def _ensure_topup_sheet(client):
    ss = open_google_sheet(client, SPREADSHEET_KEY)
    try:
        ws = ss.worksheet(TOPUP_SHEET_NAME)
    except Exception:
        ws = ss.add_worksheet(title=TOPUP_SHEET_NAME, rows=1000, cols=12)
        ws.append_row([
            "TxID",             # A
            "Username",         # B
            "Amount",           # C
            "Method",           # D
            "Note",             # E
            "Status",           # F  Pending/Approved/Rejected
            "RequestedAtISO",   # G
            "ProofLink",        # H
            "ReviewedAtISO",    # I
            "AdminNote"         # J
        ])
    return ws

def _get_user_row(client, username: str) -> Tuple[object, Optional[dict], Optional[int]]:
    """
    คืน (worksheet, record, row_index) จากแท็บ Users
    """
    ss = open_google_sheet(client, SPREADSHEET_KEY)
    try:
        ws = ss.worksheet(USERS_SHEET_NAME)
    except Exception:
        # ถ้าไม่มีแท็บ Users ให้สร้างโครงขั้นต่ำ
        ws = ss.add_worksheet(title=USERS_SHEET_NAME, rows=1000, cols=10)
        ws.append_row(["Username", "Password", "Role", "สามารถตั้งจองล่วงหน้าได้กี่ site", "ตั้งจองล่วงหน้าได้ไหม", "Expiration date"])
    records = ws.get_all_records()
    for idx, rec in enumerate(records, start=2):
        if str(rec.get("Username", "")).strip() == str(username).strip():
            return ws, rec, idx
    return ws, None, None

def _get_user_role(client, username: str) -> Optional[str]:
    _, rec, _ = _get_user_row(client, username)
    if rec:
        return (rec.get("Role") or "").strip().lower() or None
    return None

def _is_admin_role(role: Optional[str]) -> bool:
    return (role or "").strip().lower() == "admin"

def _find_topup_by_txid(client, txid: str):
    ss = open_google_sheet(client, SPREADSHEET_KEY)
    ws = _ensure_topup_sheet(client)
    records = ws.get_all_records()
    for idx, rec in enumerate(records, start=2):  # header row = 1
        if str(rec.get("TxID", "")).strip().upper() == txid.strip().upper():
            return ws, rec, idx
    return None, None, None

def _update_user_role_and_expiration(client, username: str, new_role: str, months: int = 1) -> bool:
    """
    อัปเดตคอลัมน์ในแท็บ Users:
      C: Role
      D: สามารถตั้งจองล่วงหน้าได้กี่ site
      E: ตั้งจองล่วงหน้าได้ไหม  (TRUE/FALSE)
      F: Expiration date (YYYY-MM-DD)

    ถ้าผู้ใช้ปัจจุบันเป็น admin จะไม่เขียน role ทับ
    """
    if not username or username == "-":
        return False

    ws, rec, row_idx = _get_user_row(create_gsheet_client(), username)
    if not row_idx:
        return False  # ไม่พบผู้ใช้

    cur_role = (rec.get("Role") or "").strip().lower()
    if _is_admin_role(cur_role):
        # อย่าลดสิทธิ์แอดมิน
        return True

    # ต่ออายุแบบง่าย = วันนี้ + months * 30 วัน
    new_exp = datetime.utcnow() + timedelta(days=30 * max(1, months))

    # เขียน Role + Expiration
    ws.update(f"C{row_idx}", str(new_role))          # Role
    ws.update(f"F{row_idx}", _dt_yyyymmdd(new_exp))  # Expiration

    # อัปเดตสิทธิ์การใช้งานตาม ROLE_POLICY
    policy = ROLE_POLICY.get(new_role.lower())
    if policy:
        ws.update(f"D{row_idx}", str(policy["sites"]))                      # จำนวน site
        ws.update(f"E{row_idx}", "TRUE" if policy["can_prebook"] else "FALSE")  # จองล่วงหน้าได้ไหม

    return True

# =============================================================================
# Local Topups functions (ย้ายมาจาก utils)
# =============================================================================
def record_topup_request(user_info: dict, amount: float, method: str, note: str | None = None) -> dict:
    """
    สร้างแถว Pending ในชีต Topups แล้วคืน {"TxID": <id>}
    """
    from uuid import uuid4
    client = create_gsheet_client()
    ws = _ensure_topup_sheet(client)

    txid = str(uuid4())[:8].upper()
    username = str(user_info.get("Username") or "-")
    now_iso = datetime.utcnow().isoformat()

    row = [txid, username, float(amount), str(method), str(note or ""), "Pending", now_iso, "", "", ""]
    ws.append_row(row)
    return {"TxID": txid}

def update_topup_status_paid(txid: str, amount: float | None, provider: str, provider_txn_id: str) -> bool:
    """
    Mark TxID = Approved และบันทึก provider_txn_id ลง AdminNote
    ถ้ามี amount มาและไม่ตรงกับ Amount ในชีต ให้ return False
    """
    client = create_gsheet_client()
    ws = _ensure_topup_sheet(client)
    records = ws.get_all_records()

    for idx, rec in enumerate(records, start=2):
        if str(rec.get("TxID", "")).strip().upper() == str(txid).strip().upper():
            if amount is not None:
                try:
                    rec_amt = float(rec.get("Amount", 0))
                    if abs(rec_amt - float(amount)) > 1e-6:
                        return False
                except Exception:
                    return False
            now_iso = datetime.utcnow().isoformat()
            prev_note = (rec.get("AdminNote") or "").strip()
            add = f"Paid via {provider}: {provider_txn_id}"
            note = (prev_note + " | " if prev_note else "") + add

            ws.update(f"F{idx}", "Approved")     # Status
            ws.update(f"I{idx}", now_iso)        # ReviewedAtISO
            ws.update(f"J{idx}", note)           # AdminNote
            return True
    return False

# =============================================================================
# Internal endpoints
# =============================================================================
@app.post("/internal/topups/request")
async def topups_request(request: Request):
    """
    ถูกเรียกจาก Payment Backend/Worker ตอนจะสร้าง Checkout Session
    - ถ้า user เป็น admin: อนุญาตทุกจำนวน (> 0)
    - ถ้าไม่ใช่ admin: บังคับยอดต้องตรง ROLE_MAP (1500/2500/3500 โดยดีฟอลต์)
    """
    _require_internal_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    user = body.get("user") or {"Username": "-"}
    username = str((user or {}).get("Username") or "-").strip() or "-"
    amount = body.get("amount")
    method = body.get("method") or "Stripe/Checkout"
    description = body.get("description") or "Top-up"

    try:
        amt = float(amount)
        if amt <= 0:
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="amount must be positive float")

    # ตรวจ role ผู้ใช้จากชีตเพื่อรู้ว่าเป็น admin ไหม (หาไม่ได้ = ปล่อยเป็น non-admin)
    try:
        client = create_gsheet_client()
        role = _get_user_role(client, username) if username and username != "-" else None
    except Exception:
        role = None

    is_admin = _is_admin_role(role)

    # non-admin ต้องอยู่ใน allowlist
    if (not is_admin) and ROLE_MAP and round(amt, 2) not in ROLE_MAP:
        allowed = ", ".join(str(int(a)) if a.is_integer() else str(a) for a in sorted(ROLE_MAP))
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
    เรียกจาก Payment Webhook/Worker เมื่อชำระสำเร็จ
      - จะ mark Topups = Approved
      - แล้วไปอัปเดต Users (Role + Expiration + สิทธิ์ sites & prebook)
      - ถ้า user เป็น admin -> ไม่เปลี่ยน Role/สิทธิ์ (ปล่อยตามเดิม)
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

    amount_f: Optional[float] = None
    if amount is not None:
        try:
            amount_f = round(float(amount), 2)
        except Exception:
            raise HTTPException(status_code=400, detail="amount must be float or null")

    try:
        client = create_gsheet_client()

        # หาใน Topups เพื่อรู้ Username และยอด
        _, rec_top, _ = _find_topup_by_txid(client, txid)
        if rec_top:
            status_cur = str(rec_top.get("Status", "")).strip().lower()
            if status_cur in ("approved", "paid"):
                return {"ok": True}  # idempotent

        # อัปเดต Topups = Approved
        ok = update_topup_status_paid(
            txid=txid,
            amount=amount_f,               # ถ้ามี amount ให้ตรวจ mismatch
            provider=provider,
            provider_txn_id=provider_txn_id,
        )
        if not ok and amount_f is None:
            # เผื่อ webhook ไม่ส่งยอดมา ลองผ่านแบบไม่เช็คยอด
            ok = update_topup_status_paid(
                txid=txid,
                amount=None,
                provider=provider,
                provider_txn_id=provider_txn_id,
            )
        if not ok:
            return JSONResponse({"ok": False, "error": "update_topup_status_paid failed"}, status_code=200)

        # อัปเดต Users เฉพาะ non-admin เท่านั้น
        if rec_top:
            username = str(rec_top.get("Username", "")).strip()
            role_now = _get_user_role(client, username) or "normal"
            if not _is_admin_role(role_now):
                # เลือก role จากยอดที่จ่าย (ROLE_MAP)
                amt_sheet = rec_top.get("Amount")
                try:
                    amt_sheet = round(float(amt_sheet), 2)
                except Exception:
                    amt_sheet = amount_f  # fallback จาก webhook

                if amt_sheet is not None and ROLE_MAP:
                    desired_role = ROLE_MAP.get(amt_sheet)
                    if desired_role:
                        _update_user_role_and_expiration(client, username, desired_role, months=EXPAND_MONTHS)

        return {"ok": True}

    except Exception as e:
        # กัน retry storm ของ Stripe: ตอบ 200 + ok:false
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
