from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
import os

app = FastAPI()

API_TOKEN = "123456SECRET"
CREDENTIALS_PATH = "safe_folder/credentials.json"

@app.get("/")
def root():
    return {"status": "API is running"}

@app.get("/get-credentials")
async def get_credentials(request: Request):
    token = request.headers.get("X-API-Token")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Unauthorized")

    if not os.path.exists(CREDENTIALS_PATH):
        raise HTTPException(status_code=404, detail="Credentials not found")

    return FileResponse(
        CREDENTIALS_PATH,
        media_type="application/json",
        filename="credentials.json"
    )
