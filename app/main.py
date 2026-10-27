from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app import jobs
from app.agent.github import parse_repository_url
from app.db import ensure_indexes, get_db

load_dotenv()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await ensure_indexes(get_db())
    yield


app = FastAPI(title="GitHub Helper", version="0.1.0", lifespan=lifespan)


class JobRequest(BaseModel):
    url: str
    branch: str | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
async def create_job(request: JobRequest, db=Depends(get_db)) -> dict[str, str]:
    try:
        repository = parse_repository_url(request.url, request.branch or None)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"id": await jobs.create_job(db, repository)}


async def _load_job(db, job_id: str) -> dict:
    object_id = jobs.parse_object_id(job_id)
    job = await db.jobs.find_one({"_id": object_id}) if object_id else None
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, db=Depends(get_db)) -> dict:
    return jobs.serialize_job(await _load_job(db, job_id))


@app.get("/jobs/{job_id}/report", response_class=HTMLResponse)
async def get_job_report(job_id: str, db=Depends(get_db)) -> str:
    job = await _load_job(db, job_id)
    report = await db.reports.find_one({"_id": job["report_id"]}) if job.get("report_id") else None
    if report is None:
        raise HTTPException(status_code=404, detail="Report is not ready.")
    return report["html"]
