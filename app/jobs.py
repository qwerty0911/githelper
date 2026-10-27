"""Job queue, report cache and question history stored in MongoDB.

Job status moves `queued -> running -> done | failed`. Progress events are
appended to the job document so the API can stream them.
"""

import datetime
from typing import Any

import bson
from bson import errors as bson_errors
import pymongo
from pymongo import errors as pymongo_errors

from app.agent import audience
from app.agent import github

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
ACTIVE_STATUSES = (QUEUED, RUNNING)
# A running job whose worker stopped sending progress for this long is
# assumed dead and handed to another worker.
STALE_AFTER = datetime.timedelta(minutes=15)
MAX_ATTEMPTS = 3
RECENT_LIMIT = 8

Document = dict[str, Any]


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _iso(moment: datetime.datetime) -> str:
    # Mongo returns naive UTC datetimes; mark them so clients do not read
    # them as local time.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    return moment.isoformat()


def parse_object_id(value: str) -> bson.ObjectId | None:
    """Parses an ObjectId string, returning `None` when it is invalid."""
    try:
        return bson.ObjectId(value)
    except (bson_errors.InvalidId, TypeError):
        return None


def normalize_question(question: str) -> str:
    """Returns a cache key that ignores case and extra whitespace."""
    return " ".join(question.lower().split())


async def find_active_job(
    db, repository: github.Repository, level: audience.Level
) -> str | None:
    """Returns the id of a queued or running job for the same request.

    Args:
        db: The application database.
        repository: Requested repository and branch.
        level: Requested reader level.

    Returns:
        The job id, or `None` when no identical job is in progress.
    """
    job = await db.jobs.find_one(
        {
            "owner": repository.owner,
            "name": repository.name,
            "branch": repository.branch,
            "level": level,
            "status": {"$in": list(ACTIVE_STATUSES)},
        },
        sort=[("created_at", -1)],
    )
    return str(job["_id"]) if job else None


async def create_job(
    db,
    repository: github.Repository,
    level: audience.Level = audience.DEFAULT_LEVEL,
) -> str:
    """Queues a report job.

    Args:
        db: The application database.
        repository: Repository and branch to analyze.
        level: Reader level.

    Returns:
        The new job id.
    """
    now = _now()
    result = await db.jobs.insert_one(
        {
            "owner": repository.owner,
            "name": repository.name,
            "branch": repository.branch,
            "level": level,
            "status": QUEUED,
            "attempts": 0,
            "events": [
                {"at": now, "step": "queued", "message": "대기열에 등록됨"}
            ],
            "error": None,
            "report_id": None,
            "commit_sha": None,
            "cached": False,
            "created_at": now,
            "updated_at": now,
        }
    )
    return str(result.inserted_id)


async def claim_job(
    db, *, stale_after: datetime.timedelta = STALE_AFTER
) -> Document | None:
    """Atomically hands the oldest available job to one worker.

    A job is available when it is queued, or when it is running but has not
    reported progress for `stale_after` (its worker most likely died).

    Args:
        db: The application database.
        stale_after: How long a running job may stay silent.

    Returns:
        The claimed job after the update, or `None` when nothing is waiting.
    """
    now = _now()
    return await db.jobs.find_one_and_update(
        {
            "$or": [
                {"status": QUEUED},
                {"status": RUNNING, "updated_at": {"$lt": now - stale_after}},
            ]
        },
        {
            "$set": {"status": RUNNING, "updated_at": now},
            "$inc": {"attempts": 1},
            "$push": {
                "events": {"at": now, "step": "running", "message": "작업 시작"}
            },
        },
        sort=[("created_at", 1)],
        return_document=pymongo.ReturnDocument.AFTER,
    )


async def add_event(
    db, job_id: bson.ObjectId, step: str, message: str, **fields: Any
) -> None:
    """Appends a progress event and updates job fields.

    Args:
        db: The application database.
        job_id: Job to update.
        step: Machine-readable step name.
        message: Message shown to the user.
        **fields: Extra job fields to set.
    """
    now = _now()
    await db.jobs.update_one(
        {"_id": job_id},
        {
            "$set": {"updated_at": now, **fields},
            "$push": {"events": {"at": now, "step": step, "message": message}},
        },
    )


async def complete_job(
    db, job_id: bson.ObjectId, report_id: bson.ObjectId, *, cached: bool
) -> None:
    """Marks a job done and links its report."""
    await add_event(
        db,
        job_id,
        "done",
        "캐시된 리포트를 재사용함" if cached else "리포트 생성 완료",
        status=DONE,
        report_id=report_id,
        cached=cached,
    )


async def fail_job(db, job_id: bson.ObjectId, error: str) -> None:
    """Marks a job failed with a message that is safe to show users."""
    await add_event(db, job_id, "failed", error, status=FAILED, error=error)


async def find_cached_report(
    db,
    repo: str,
    commit_sha: str,
    level: audience.Level = audience.DEFAULT_LEVEL,
) -> Document | None:
    """Returns a stored report for the same repo, commit and level."""
    return await db.reports.find_one(
        {"repo": repo, "commit_sha": commit_sha, "level": level}
    )


async def save_report(
    db,
    *,
    repo: str,
    branch: str | None,
    commit_sha: str,
    report: dict[str, Any],
    html: str,
    level: audience.Level = audience.DEFAULT_LEVEL,
) -> bson.ObjectId:
    """Stores a report, reusing one another worker saved first.

    Returns:
        The id of the stored report.
    """
    try:
        result = await db.reports.insert_one(
            {
                "repo": repo,
                "branch": branch,
                "commit_sha": commit_sha,
                "level": level,
                "report": report,
                "html": html,
                "created_at": _now(),
            }
        )
        return result.inserted_id
    except pymongo_errors.DuplicateKeyError:
        existing = await find_cached_report(db, repo, commit_sha, level)
        return existing["_id"]


async def recent_reports(db, limit: int = RECENT_LIMIT) -> list[Document]:
    """Lists recently analyzed repositories, newest first, one per repo.

    Args:
        db: The application database.
        limit: Maximum number of repositories.

    Returns:
        Dicts with `repo`, `branch`, `level`, `commit_sha`, `project_name`,
        `summary` and `created_at`.
    """
    cursor = db.reports.aggregate(
        [
            {"$sort": {"created_at": -1}},
            {
                "$group": {
                    "_id": "$repo",
                    "branch": {"$first": "$branch"},
                    "level": {"$first": "$level"},
                    "commit_sha": {"$first": "$commit_sha"},
                    "project_name": {"$first": "$report.project_name"},
                    "summary": {"$first": "$report.summary"},
                    "created_at": {"$first": "$created_at"},
                }
            },
            {"$sort": {"created_at": -1}},
            {"$limit": limit},
        ]
    )
    return [
        {
            "repo": item["_id"],
            "branch": item.get("branch"),
            "level": item.get("level") or audience.DEFAULT_LEVEL,
            "commit_sha": item.get("commit_sha"),
            "project_name": item.get("project_name"),
            "summary": item.get("summary"),
            "created_at": _iso(item["created_at"]),
        }
        async for item in cursor
    ]


async def find_answer(
    db, report_id: bson.ObjectId, question: str, version: str
) -> Document | None:
    """Returns an earlier answer to the same question about a report.

    Args:
        db: The application database.
        report_id: Report the question is about.
        question: The question as asked.
        version: Q&A cache key (pipeline and model versions). Answers
            from other versions are ignored so changes are not hidden by
            the cache.

    Returns:
        The latest matching question document, or `None`.
    """
    return await db.questions.find_one(
        {
            "report_id": report_id,
            "normalized": normalize_question(question),
            "qa_version": version,
        },
        sort=[("created_at", -1)],
    )


async def save_question(
    db,
    *,
    job_id: bson.ObjectId,
    report_id: bson.ObjectId,
    question: str,
    answer: str,
    citations: list[dict[str, Any]],
    grounded: bool,
    cached: bool,
    version: str,
) -> Document:
    """Stores a question and its answer in a job's history.

    Returns:
        The stored document.
    """
    document = {
        "job_id": job_id,
        "report_id": report_id,
        "question": question,
        "normalized": normalize_question(question),
        "answer": answer,
        "citations": citations,
        "grounded": grounded,
        "cached": cached,
        "qa_version": version,
        "created_at": _now(),
    }
    result = await db.questions.insert_one(document)
    document["_id"] = result.inserted_id
    return document


async def list_questions(db, job_id: bson.ObjectId) -> list[Document]:
    """Returns a job's questions, oldest first."""
    cursor = db.questions.find({"job_id": job_id}).sort("created_at", 1)
    return [document async for document in cursor]


def serialize_question(
    document: Document, repo: str, commit_sha: str | None
) -> Document:
    """Converts a stored question to its API form with github.com links."""
    return {
        "id": str(document["_id"]),
        "question": document["question"],
        "answer": document["answer"],
        "grounded": document["grounded"],
        "cached": document.get("cached", False),
        "citations": [
            {
                **citation,
                "url": github.blob_url(
                    repo,
                    commit_sha,
                    citation["path"],
                    citation["start_line"],
                    citation["end_line"],
                ),
            }
            for citation in document["citations"]
        ],
        "created_at": _iso(document["created_at"]),
    }


def serialize_job(job: Document) -> Document:
    """Converts a stored job to its API form."""
    return {
        "id": str(job["_id"]),
        "repo": f"{job['owner']}/{job['name']}",
        "branch": job.get("branch"),
        "level": job.get("level") or audience.DEFAULT_LEVEL,
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
