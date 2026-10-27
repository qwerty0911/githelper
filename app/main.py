import asyncio
import json
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
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


SSE_POLL_SECONDS = 1.0


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/jobs/{job_id}/events")
async def stream_job_events(job_id: str, db=Depends(get_db)) -> StreamingResponse:
    await _load_job(db, job_id)  # 404 before the stream starts

    async def stream():
        sent = 0
        while True:
            job = jobs.serialize_job(await _load_job(db, job_id))
            for event in job["events"][sent:]:
                yield _sse("progress", event)
            sent = len(job["events"])
            if job["status"] in (jobs.DONE, jobs.FAILED):
                yield _sse("end", {"status": job["status"], "error": job["error"]})
                return
            await asyncio.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
