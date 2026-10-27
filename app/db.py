import os
from functools import lru_cache

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

DB_NAME = "github_helper"


@lru_cache(maxsize=1)
def _client() -> AsyncIOMotorClient:
    return AsyncIOMotorClient(os.getenv("MONGODB_URL", "mongodb://localhost:27017"))


def get_db() -> AsyncIOMotorDatabase:
    return _client()[DB_NAME]


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    await db.jobs.create_index([("status", 1), ("created_at", 1)])
    await db.reports.create_index([("repo", 1), ("commit_sha", 1)], unique=True)
