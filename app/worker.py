import asyncio
import logging

from dotenv import load_dotenv

from app import jobs
from app.agent.github import GitHubError, GitHubTools, Repository
from app.agent.runner import generate_report
from app.db import ensure_indexes, get_db

load_dotenv()
logger = logging.getLogger("worker")

POLL_SECONDS = 2.0
STEP_LABELS = {
    "inspect_tree": "저장소 구조 확인",
    "select_files": "핵심 파일 선택",
    "read_selected": "파일 읽기",
    "deepen": "추가 탐색",
    "write_report": "리포트 작성 및 인용 검증",
}


async def process_job(db, job: dict, generate=generate_report) -> None:
    job_id = job["_id"]
    repository = Repository(job["owner"], job["name"], job.get("branch"))
    try:
        tools = GitHubTools(repository)
        try:
            commit_sha = await tools.resolve_ref()
        finally:
            await tools.close()
        await jobs.add_event(
            db, job_id, "resolved", f"commit {commit_sha[:7]} 고정", commit_sha=commit_sha
        )

        cached = await jobs.find_cached_report(db, repository.full_name, commit_sha)
        if cached:
            await jobs.complete_job(db, job_id, cached["_id"], cached=True)
            return

        async def on_progress(node: str) -> None:
            await jobs.add_event(db, job_id, node, STEP_LABELS.get(node, node))

        result = await generate(repository, on_progress=on_progress)
        report_id = await jobs.save_report(
            db,
            repo=repository.full_name,
            branch=repository.branch,
            commit_sha=result.commit_sha or commit_sha,
            report=result.report.model_dump(),
            html=result.html,
        )
        await jobs.complete_job(db, job_id, report_id, cached=False)
    except (GitHubError, ValueError, RuntimeError) as error:
        await jobs.fail_job(db, job_id, str(error))
    except Exception:
        logger.exception("job %s failed", job_id)
        await jobs.fail_job(db, job_id, "리포트 생성 중 예기치 않은 오류가 발생했습니다.")


async def run_once(db, generate=generate_report) -> bool:
    job = await jobs.claim_job(db)
    if job is None:
        return False
    await process_job(db, job, generate)
    return True


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    db = get_db()
    await ensure_indexes(db)
    logger.info("worker started")
    while True:
        if not await run_once(db):
            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
