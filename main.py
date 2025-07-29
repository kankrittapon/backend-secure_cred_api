from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
import os

app = FastAPI()

# Map Token -> Secret File Name
TOKEN_FILE_MAP = {
    "69fa5371392bdfe7160f378ef4b10bb6": "branchs.json",        # ถ้ามีในอนาคต
    "1582b63313475631d732f4d1aed9a534": "times.json",          # ถ้ามีในอนาคต
    "a48bca796db6089792a2d9047c7ebf78": "ithitec.json",
    "0857df816fa1952d96c6b76762510516": "pmrocket.json",
    "8155bfa0c8faaed0a7917df38f0238b6": "rocketbooking.json",
    "a2htZW5odWFrdXltYWV5ZWQ=": "credentials.json"  # เปลี่ยนตามต้องการ
}

# Path ที่แพลตฟอร์มโหลด Secret Files เข้าไว้
SECRET_PATH_PREFIX = "/etc/secrets"

@app.get("/")
def root():
    return {"message": "API is running"}

@app.get("/get-credentials")
async def get_credentials(request: Request):
    token = request.headers.get("X-API-Token")
    print(f"Received token: '{token}'")

    if not token or token not in TOKEN_FILE_MAP:
        raise HTTPException(status_code=403, detail="Unauthorized or unknown API token")

    filename = TOKEN_FILE_MAP[token]
    filepath = os.path.join(SECRET_PATH_PREFIX, filename)

    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"{filename} not found in /etc/secrets")

    return FileResponse(
        filepath,
        media_type="application/json",
        filename=filename
    )
