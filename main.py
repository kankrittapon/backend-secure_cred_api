from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import os
import json

app = FastAPI()

API_TOKEN = os.getenv("API_TOKEN", "default-token")
CREDENTIALS_PATH = "/etc/secrets/credentials.json"

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

    with open(CREDENTIALS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    return JSONResponse(content=data)
