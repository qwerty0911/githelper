"""Worker process that claims queued jobs and generates reports.

Run with `python -m app.worker`. Several workers can run at once; MongoDB's
atomic `find_one_and_update` makes sure each job is claimed only once.
"""

import asyncio
import logging
from typing import Any

import dotenv

from app import db as db_lib
from app import jobs
from app.agent import audience
from app.agent import github
from app.agent import runner

dotenv.load_dotenv()
logger = logging.getLogger("worker")

POLL_SECONDS = 2.0
STEP_LABELS = {
    "inspect_tree": "저장소 구조 확인",
    "select_files": "핵심 파일 선택",
    "read_selected": "파일 읽기",
    "deepen": "추가 탐색",
    "write_report": "리포트 작성 및 인용 검증",
}
UNEXPECTED_ERROR = "리포트 생성 중 예기치 않은 오류가 발생했습니다."


async def process_job(
    db, job: dict[str, Any], generate=runner.generate_report
) -> None:
    """Generates (or reuses) the report for one claimed job.

    Known errors are shown to the user; unexpected ones are logged and
    replaced with a generic message so internals do not leak.

    Args:
        db: The application database.
        job: The claimed job document.
        generate: Report generator, replaceable in tests.
    """
    job_id = job["_id"]
    repository = github.Repository(job["owner"], job["name"], job.get("branch"))
    level: audience.Level = job.get("level") or audience.DEFAULT_LEVEL
    attempts = job.get("attempts", 1)
    if attempts > jobs.MAX_ATTEMPTS:
        await jobs.fail_job(
            db, job_id, "작업이 여러 번 중단되어 처리를 멈췄습니다."
        )
        return
    if attempts > 1:
        await jobs.add_event(
            db, job_id, "retry", f"중단된 작업을 다시 시작함 ({attempts}회차)"
        )
    try:
        tools = github.GitHubTools(repository)
        try:
            commit_sha = await tools.resolve_ref()
        finally:
            await tools.close()
        await jobs.add_event(
            db,
            job_id,
            "resolved",
            f"commit {commit_sha[:7]} 고정",
            commit_sha=commit_sha,
        )

        cached = await jobs.find_cached_report(
            db, repository.full_name, commit_sha, level
        )
        if cached:
            await jobs.complete_job(db, job_id, cached["_id"], cached=True)
            return

        async def on_progress(node: str) -> None:
            await jobs.add_event(db, job_id, node, STEP_LABELS.get(node, node))

        # Pass the pinned commit so the report matches the cache key even if
        # the branch moves while the agent runs.
        result = await generate(
            repository,
            level=level,
            commit_sha=commit_sha,
            on_progress=on_progress,
        )
        report_id = await jobs.save_report(
            db,
            repo=repository.full_name,
            branch=repository.branch,
            commit_sha=commit_sha,
            report=result.report.model_dump(),
            html=result.html,
            level=level,
        )
        await jobs.complete_job(db, job_id, report_id, cached=False)
    except (github.GitHubError, ValueError, RuntimeError) as error:
        await jobs.fail_job(db, job_id, str(error))
    except Exception:  # pylint: disable=broad-except
        logger.exception("job %s failed", job_id)
        await jobs.fail_job(db, job_id, UNEXPECTED_ERROR)


async def run_once(db, generate=runner.generate_report) -> bool:
    """Processes one job if any is available.

    Returns:
        Whether a job was processed.
    """
    job = await jobs.claim_job(db)
    if job is None:
        return False
    await process_job(db, job, generate)
    return True


async def main() -> None:
    """Polls for jobs forever."""
    logging.basicConfig(level=logging.INFO)
    db = db_lib.get_db()
    await db_lib.ensure_indexes(db)
    logger.info("worker started")
    while True:
        if not await run_once(db):
            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
