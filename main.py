# main_backend.py
from __future__ import annotations

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
import os, json, sys, uuid, requests
from datetime import datetime, timedelta
from typing import Optional, Tuple

# ==============================
# App
# ==============================
app = FastAPI(title="Main Backend + Credentials + Topups")

# ==============================
# Credentials API (existing)
# ==============================
TOKEN_FILE_MAP = {
    "69fa5371392bdfe7160f378ef4b10bb6": "branchs.json",
    "1582b63313475631d732f4d1aed9a534": "times.json",
    "a48bca796db6089792a2d9047c7ebf78": "ithitec.json",
    "0857df816fa1952d96c6b76762510516": "pmrocket.json",
    "8155bfa0c8faaed0a7917df38f0238b6": "rocketbooking.json",
    "a2htZW5odWFrdXltYWV5ZWQ=": "credentials.json",  # Google Service Account
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
# Google Sheets helpers (embedded from utils)
# ==============================
SPREADSHEET_KEY = os.getenv("SPREADSHEET_KEY", "").strip()
TOPUP_SHEET_NAME = os.getenv("TOPUP_SHEET_NAME", "Topups").strip() or "Topups"
USERS_SHEET_NAME = os.getenv("USERS_SHEET_NAME", "Users").strip() or "Users"

CREDENTIALS_BASE_URL = os.getenv("CREDENTIALS_BASE_URL", "").strip()
API_TOKEN = os.getenv("API_TOKEN", "").strip()

# columns for Topups sheet
TOPUP_HEADERS = [
    "Timestamp",         # A
    "TxID",              # B
    "Username",          # C
    "Amount",            # D
    "Method",            # E
    "Description",       # F
    "Status",            # G (Pending/Approved)
    "AdminNote",         # H (free text)
    "Provider",          # I (e.g., Stripe)
    "ProviderTxnID",     # J (pi_xxx / ch_xxx)
]

def _dt_yyyymmdd(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")

def _ts_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def create_gsheet_client():
    """
    โหลด service-account JSON ทาง HTTP จาก service credentials endpoint (ปกป้องด้วย API_TOKEN)
    แล้ว authorize gspread client
    """
    if not CREDENTIALS_BASE_URL or not API_TOKEN:
        raise RuntimeError("CREDENTIALS_BASE_URL or API_TOKEN not set")

    r = requests.get(
        CREDENTIALS_BASE_URL,
        headers={"X-API-Token": API_TOKEN},
        timeout=15,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"fetch credentials failed: {r.status_code} {r.text[:200]}")

    sa = r.json()
    from google.oauth2.service_account import Credentials  # lazy import
    import gspread

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc

def open_google_sheet(client, key: str):
    if not key:
        raise RuntimeError("SPREADSHEET_KEY not set")
    return client.open_by_key(key)

def _ensure_topup_sheet(client):
    ss = open_google_sheet(client, SPREADSHEET_KEY)
    try:
        ws = ss.worksheet(TOPUP_SHEET_NAME)
    except Exception:
        ws = ss.add_worksheet(title=TOPUP_SHEET_NAME, rows=2000, cols=len(TOPUP_HEADERS))
        ws.update("A1", [TOPUP_HEADERS])
    # ถ้าหัวตารางหาย บังคับเขียนใหม่
    try:
        headers = ws.row_values(1)
        if headers[: len(TOPUP_HEADERS)] != TOPUP_HEADERS:
            ws.update("A1", [TOPUP_HEADERS])
    except Exception:
        ws.update("A1", [TOPUP_HEADERS])
    return ws

def _find_topup_row(ws, txid: str) -> Optional[int]:
    records = ws.get_all_records()  # heavy but simple
    for idx, rec in enumerate(records, start=2):
        if str(rec.get("TxID", "")).strip().upper() == txid.strip().upper():
            return idx
    return None

def record_topup_request(user_info: dict, amount: float, method: str, note: Optional[str] = None) -> dict:
    """
    สร้างแถวในแท็บ Topups: Status=Pending และคืน TxID
    """
    client = create_gsheet_client()
    ws = _ensure_topup_sheet(client)

    txid = uuid.uuid4().hex[:8].upper()
    username = str((user_info or {}).get("Username") or "-").strip() or "-"

    row = [
        _ts_iso(),          # Timestamp
        txid,               # TxID
        username,           # Username
        float(round(amount, 2)),  # Amount
        str(method or ""),  # Method
        str(note or ""),    # Description
        "Pending",          # Status
        "",                 # AdminNote
        "",                 # Provider
        "",                 # ProviderTxnID
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return {"TxID": txid}

def update_topup_status_paid(txid: str, amount: Optional[float], provider: str, provider_txn_id: str) -> bool:
    """
    อัปเดตสถานะเป็น Approved ถ้าพบ TxID; หากส่ง amount มาแล้วไม่ตรงกับในแถว จะคืน False
    """
    client = create_gsheet_client()
    ws = _ensure_topup_sheet(client)

    row_idx = _find_topup_row(ws, txid)
    if not row_idx:
        return False

    # อ่านค่า Amount ปัจจุบันเพื่อเทียบ
    recs = ws.get_all_records()
    rec = recs[row_idx - 2] if (row_idx - 2) < len(recs) else {}
    amt_sheet = rec.get("Amount")
    try:
        amt_sheet = float(amt_sheet)
    except Exception:
        amt_sheet = None

    if (amount is not None) and (amt_sheet is not None) and (round(amount, 2) != round(amt_sheet, 2)):
        # mismatch
        return False

    # เขียนสถานะ + โน้ต + provider
    ws.update(f"G{row_idx}", "Approved")  # Status
    note = f"[{_ts_iso()}] {provider} paid, ref={provider_txn_id}"
    ws.update(f"H{row_idx}", note)        # AdminNote
    ws.update(f"I{row_idx}", provider)    # Provider
    ws.update(f"J{row_idx}", provider_txn_id)  # ProviderTxnID
    return True

# ==============================
# Role mapping & policy + Users update
# ==============================
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
EXPAND_MONTHS = int(os.getenv("ROLE_MONTHS", "1"))

ROLE_POLICY = {
    "vipi":   {"sites": 3,  "can_prebook": True},
    "vipii":  {"sites": 6,  "can_prebook": True},
    "vipiii": {"sites": 10, "can_prebook": True},
    "normal": {"sites": 0,  "can_prebook": False},
    "admin":  {"sites": 999,"can_prebook": True},
}

def _get_user_row(client, username: str) -> Tuple[object, Optional[dict], Optional[int]]:
    ss = open_google_sheet(client, SPREADSHEET_KEY)
    ws = ss.worksheet(USERS_SHEET_NAME)
    recs = ws.get_all_records()
    for idx, rec in enumerate(recs, start=2):
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

def _update_user_role_and_expiration(client, username: str, new_role: str, months: int = 1) -> bool:
    if not username or username == "-":
        return False
    ss = open_google_sheet(client, SPREADSHEET_KEY)
    ws = ss.worksheet(USERS_SHEET_NAME)
    recs = ws.get_all_records()

    row_idx = None
    cur_role = None
    for idx, rec in enumerate(recs, start=2):
        if str(rec.get("Username", "")).strip() == str(username).strip():
            row_idx = idx
            cur_role = str(rec.get("Role", "")).strip().lower()
            break
    if not row_idx:
        return False

    if _is_admin_role(cur_role):  # อย่าลดสิทธิ์ admin
        return True

    new_exp = datetime.utcnow() + timedelta(days=30 * max(1, months))
    ws.update(f"C{row_idx}", str(new_role))            # Role
    ws.update(f"F{row_idx}", _dt_yyyymmdd(new_exp))    # Expiration

    policy = ROLE_POLICY.get(new_role.lower())
    if policy:
        ws.update(f"D{row_idx}", str(policy["sites"]))                 # sites
        ws.update(f"E{row_idx}", "TRUE" if policy["can_prebook"] else "FALSE")
    return True

# ==============================
# Internal endpoints
# ==============================
@app.post("/internal/topups/request")
async def topups_request(request: Request):
    """
    ถูกเรียกจาก Payment Worker ตอนจะสร้าง Checkout Session
    - admin: อนุญาตทุกจำนวน (ต้อง > 0)
    - non-admin: ยอดต้องตรง ROLE_MAP (1500/2500/3500 โดยดีฟอลต์)
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

    # ตรวจ role ผู้ใช้จากชีต เพื่อรู้ว่าเป็น admin ไหม
    try:
        client = create_gsheet_client()
        role = _get_user_role(client, username) if username and username != "-" else None
    except Exception:
        role = None

    is_admin = _is_admin_role(role)

    if (not is_admin) and ROLE_MAP and round(amt, 2) not in ROLE_MAP:
        allowed = ", ".join(str(int(a)) if float(a).is_integer() else str(a) for a in sorted(ROLE_MAP))
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
    เรียกจาก Worker เมื่อชำระสำเร็จ:
      - mark Topups = Approved
      - อัปเดต Users (role + expiration + สิทธิ์)
      - ถ้า user เป็น admin จะไม่เปลี่ยน role
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

        # เช็คซ้ำ idempotent จากแถวเดิม
        ss = open_google_sheet(client, SPREADSHEET_KEY)
        ws_top = _ensure_topup_sheet(client)
        recs = ws_top.get_all_records()
        row_idx = None
        rec_top = None
        for idx, rec in enumerate(recs, start=2):
            if str(rec.get("TxID", "")).strip().upper() == txid.upper():
                row_idx = idx
                rec_top = rec
                break

        if rec_top:
            status_cur = str(rec_top.get("Status", "")).strip().lower()
            if status_cur in ("approved", "paid"):
                return {"ok": True}

        # อัปเดตสถานะ Topups
        ok = update_topup_status_paid(
            txid=txid,
            amount=amount_f,
            provider=provider,
            provider_txn_id=provider_txn_id,
        )
        if not ok and amount_f is None:
            ok = update_topup_status_paid(txid=txid, amount=None, provider=provider, provider_txn_id=provider_txn_id)
        if not ok:
            return JSONResponse({"ok": False, "error": "update_topup_status_paid failed"}, status_code=200)

        # อัปเดต Users (เฉพาะ non-admin)
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
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
