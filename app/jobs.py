from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.agent.github import Repository

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    # Mongo returns naive datetimes (UTC); mark them so clients don't read them as local time.
    return moment.replace(tzinfo=UTC).isoformat() if moment.tzinfo is None else moment.isoformat()


def parse_object_id(value: str) -> ObjectId | None:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


async def create_job(db, repository: Repository) -> str:
    now = _now()
    result = await db.jobs.insert_one(
        {
            "owner": repository.owner,
            "name": repository.name,
            "branch": repository.branch,
            "status": QUEUED,
            "events": [{"at": now, "step": "queued", "message": "대기열에 등록됨"}],
            "error": None,
            "report_id": None,
            "commit_sha": None,
            "cached": False,
            "created_at": now,
            "updated_at": now,
        }
    )
    return str(result.inserted_id)


async def claim_job(db) -> dict[str, Any] | None:
    """Atomically move the oldest queued job to running so only one worker gets it."""
    now = _now()
    return await db.jobs.find_one_and_update(
        {"status": QUEUED},
        {
            "$set": {"status": RUNNING, "updated_at": now},
            "$push": {"events": {"at": now, "step": "running", "message": "작업 시작"}},
        },
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


async def add_event(db, job_id: ObjectId, step: str, message: str, **fields: Any) -> None:
    now = _now()
    await db.jobs.update_one(
        {"_id": job_id},
        {
            "$set": {"updated_at": now, **fields},
            "$push": {"events": {"at": now, "step": step, "message": message}},
        },
    )


async def complete_job(db, job_id: ObjectId, report_id: ObjectId, *, cached: bool) -> None:
    await add_event(
        db,
        job_id,
        "done",
        "캐시된 리포트를 재사용함" if cached else "리포트 생성 완료",
        status=DONE,
        report_id=report_id,
        cached=cached,
    )


async def fail_job(db, job_id: ObjectId, error: str) -> None:
    await add_event(db, job_id, "failed", error, status=FAILED, error=error)


async def find_cached_report(db, repo: str, commit_sha: str) -> dict[str, Any] | None:
    return await db.reports.find_one({"repo": repo, "commit_sha": commit_sha})


async def save_report(
    db, *, repo: str, branch: str | None, commit_sha: str, report: dict, html: str
) -> ObjectId:
    try:
        result = await db.reports.insert_one(
            {
                "repo": repo,
                "branch": branch,
                "commit_sha": commit_sha,
                "report": report,
                "html": html,
                "created_at": _now(),
            }
        )
        return result.inserted_id
    except DuplicateKeyError:  # another worker finished the same repo+commit first
        existing = await find_cached_report(db, repo, commit_sha)
        return existing["_id"]


def serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(job["_id"]),
        "repo": f"{job['owner']}/{job['name']}",
        "branch": job.get("branch"),
        "status": job["status"],
        "events": [
            {"at": _iso(e["at"]), "step": e["step"], "message": e["message"]}
            for e in job.get("events", [])
        ],
        "error": job.get("error"),
        "commit_sha": job.get("commit_sha"),
        "cached": job.get("cached", False),
        "report_ready": job["status"] == DONE,
    }
