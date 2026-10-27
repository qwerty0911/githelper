from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

app = FastAPI(title="GitHub Helper", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
