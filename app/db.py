"""MongoDB connection and index setup."""

import functools
import os

from motor import motor_asyncio
from pymongo import errors

DEFAULT_DB_NAME = "github_helper"
# Reports were once unique per (repo, commit). They are now also keyed by
# reader level, so the old index has to go.
_LEGACY_REPORT_INDEX = "repo_1_commit_sha_1"


@functools.lru_cache(maxsize=1)
def _client() -> motor_asyncio.AsyncIOMotorClient:
    return motor_asyncio.AsyncIOMotorClient(
        os.getenv("MONGODB_URL", "mongodb://localhost:27017")
    )


def get_db() -> motor_asyncio.AsyncIOMotorDatabase:
    """Returns the application database, named by `MONGODB_DB`."""
    return _client()[os.getenv("MONGODB_DB", DEFAULT_DB_NAME)]


async def ensure_indexes(db: motor_asyncio.AsyncIOMotorDatabase) -> None:
    """Creates the indexes the app relies on, migrating old ones.

    Args:
        db: The application database.
    """
    await db.jobs.create_index([("status", 1), ("created_at", 1)])
    await db.jobs.create_index([("owner", 1), ("name", 1), ("status", 1)])
    try:
        await db.reports.drop_index(_LEGACY_REPORT_INDEX)
    except errors.OperationFailure:
        pass  # Already migrated or never created.
    await db.reports.create_index(
        [("repo", 1), ("commit_sha", 1), ("level", 1)], unique=True
    )
    await db.reports.create_index([("created_at", -1)])
    await db.questions.create_index([("job_id", 1), ("created_at", 1)])
    await db.questions.create_index([("report_id", 1), ("normalized", 1)])
