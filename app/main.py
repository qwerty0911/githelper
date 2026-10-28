"""HTTP API: jobs, progress streaming, reports and follow-up questions."""

import asyncio
import contextlib
import json
from typing import Any

import dotenv
import fastapi
from fastapi import responses
import pydantic

from app import db as db_lib
from app import jobs
from app.agent import audience
from app.agent import github
from app.agent import qa

dotenv.load_dotenv()

SSE_POLL_SECONDS = 1.0
QUESTION_TIMEOUT_SECONDS = 120.0
# Each question runs an LLM agent, so cap how many run at the same time.
MAX_CONCURRENT_QUESTIONS = 4
_question_slots = asyncio.Semaphore(MAX_CONCURRENT_QUESTIONS)


@contextlib.asynccontextmanager
async def lifespan(_: fastapi.FastAPI):
    """Creates indexes before the app starts serving."""
    await db_lib.ensure_indexes(db_lib.get_db())
    yield


app = fastapi.FastAPI(title="GitHub Helper", version="0.2.0", lifespan=lifespan)


class JobRequest(pydantic.BaseModel):
    """Body of `POST /jobs`."""

    url: str
    branch: str | None = None
    level: audience.Level = audience.DEFAULT_LEVEL


class QuestionRequest(pydantic.BaseModel):
    """Body of `POST /jobs/{job_id}/questions`."""

    question: str = pydantic.Field(
        min_length=1, max_length=qa.MAX_QUESTION_CHARS
    )


def get_answerer():
    """Returns the question-answering function, replaceable in tests."""
    return qa.answer_question


@app.get("/health")
async def health() -> dict[str, str]:
    """Reports that the API is up."""
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
async def create_job(
    request: JobRequest, db=fastapi.Depends(db_lib.get_db)
) -> dict[str, Any]:
    """Queues a report job, or returns an identical job already in progress."""
    try:
        repository = github.parse_repository_url(
            request.url, (request.branch or "").strip() or None
        )
    except ValueError as error:
        raise fastapi.HTTPException(status_code=422, detail=str(error)) from (
            error
        )
    existing = await jobs.find_active_job(db, repository, request.level)
    if existing:
        return {"id": existing, "deduplicated": True}
    job_id = await jobs.create_job(db, repository, request.level)
    return {"id": job_id, "deduplicated": False}


@app.get("/reports/recent")
async def recent_reports(
    db=fastapi.Depends(db_lib.get_db),
) -> list[dict[str, Any]]:
    """Lists recently analyzed repositories."""
    return await jobs.recent_reports(db)


async def _load_job(db, job_id: str) -> dict[str, Any]:
    object_id = jobs.parse_object_id(job_id)
    job = await db.jobs.find_one({"_id": object_id}) if object_id else None
    if job is None:
        raise fastapi.HTTPException(status_code=404, detail="Job not found.")
    return job


async def _load_report(db, job: dict[str, Any]) -> dict[str, Any]:
    report_id = job.get("report_id")
    report = (
        await db.reports.find_one({"_id": report_id}) if report_id else None
    )
    if report is None:
        raise fastapi.HTTPException(
            status_code=404, detail="Report is not ready."
        )
    return report


@app.get("/jobs/{job_id}")
async def get_job(
    job_id: str, db=fastapi.Depends(db_lib.get_db)
) -> dict[str, Any]:
    """Returns a job's status and progress events."""
    return jobs.serialize_job(await _load_job(db, job_id))


@app.get("/jobs/{job_id}/report", response_class=responses.HTMLResponse)
async def get_job_report(job_id: str, db=fastapi.Depends(db_lib.get_db)) -> str:
    """Returns the finished report as HTML."""
    report = await _load_report(db, await _load_job(db, job_id))
    return report["html"]


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/jobs/{job_id}/events")
async def stream_job_events(
    job_id: str, db=fastapi.Depends(db_lib.get_db)
) -> responses.StreamingResponse:
    """Streams progress events as Server-Sent Events until the job ends."""
    await _load_job(db, job_id)  # Fail with 404 before the stream starts.

    async def stream():
        sent = 0
        while True:
            job = jobs.serialize_job(await _load_job(db, job_id))
            for event in job["events"][sent:]:
                yield _sse("progress", event)
            sent = len(job["events"])
            if job["status"] in (jobs.DONE, jobs.FAILED):
                yield _sse(
                    "end", {"status": job["status"], "error": job["error"]}
                )
                return
            await asyncio.sleep(SSE_POLL_SECONDS)

    return responses.StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/jobs/{job_id}/questions")
async def list_questions(
    job_id: str, db=fastapi.Depends(db_lib.get_db)
) -> list[dict[str, Any]]:
    """Returns the questions asked about a job's report."""
    job = await _load_job(db, job_id)
    repo = f"{job['owner']}/{job['name']}"
    return [
        jobs.serialize_question(document, repo, job.get("commit_sha"))
        for document in await jobs.list_questions(db, job["_id"])
    ]


@app.post("/jobs/{job_id}/questions")
async def ask_question(
    job_id: str,
    request: QuestionRequest,
    db=fastapi.Depends(db_lib.get_db),
    answerer=fastapi.Depends(get_answerer),
) -> dict[str, Any]:
    """Answers a follow-up question about a finished report.

    The same question about the same report is answered from cache.
    """
    job = await _load_job(db, job_id)
    report = await _load_report(db, job)
    repository = github.Repository(job["owner"], job["name"], job.get("branch"))
    question = request.question.strip()
    if not question:
        raise fastapi.HTTPException(
            status_code=422, detail="Question must not be empty."
        )

    previous = await jobs.find_answer(
        db, report["_id"], question, qa.cache_version()
    )
    if previous is not None:
        document = await jobs.save_question(
            db,
            job_id=job["_id"],
            report_id=report["_id"],
            question=question,
            answer=previous["answer"],
            citations=previous["citations"],
            grounded=previous["grounded"],
            cached=True,
            version=qa.cache_version(),
            diagrams=previous.get("diagrams", []),
        )
        return jobs.serialize_question(
            document, repository.full_name, report["commit_sha"]
        )

    try:
        async with _question_slots:
            result = await asyncio.wait_for(
                answerer(
                    repository,
                    report["commit_sha"],
                    question,
                    level=report.get("level") or audience.DEFAULT_LEVEL,
                    report=report.get("report"),
                ),
                timeout=QUESTION_TIMEOUT_SECONDS,
            )
    except TimeoutError as error:
        raise fastapi.HTTPException(
            status_code=504, detail="답변 생성 시간이 초과되었어요."
        ) from error
    except github.GitHubError as error:
        raise fastapi.HTTPException(status_code=502, detail=str(error)) from (
            error
        )
    except ValueError as error:
        raise fastapi.HTTPException(status_code=422, detail=str(error)) from (
            error
        )
    except RuntimeError as error:
        raise fastapi.HTTPException(status_code=503, detail=str(error)) from (
            error
        )

    document = await jobs.save_question(
        db,
        job_id=job["_id"],
        report_id=report["_id"],
        question=question,
        answer=result.answer,
        citations=[citation.model_dump() for citation in result.citations],
        grounded=result.grounded,
        cached=False,
        version=qa.cache_version(),
        diagrams=[diagram.model_dump() for diagram in result.diagrams],
    )
    return jobs.serialize_question(
        document, repository.full_name, report["commit_sha"]
    )
