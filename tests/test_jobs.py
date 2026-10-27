import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from app import jobs, worker
from app.agent.github import GitHubError, GitHubTools, Repository
from app.agent.report import Citation, ProjectReport, ReportSection
from app.agent.runner import ReportResult
from app.db import ensure_indexes
from app.main import app, get_db

REPO = Repository("owner", "project")


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["test"]
    await ensure_indexes(database)
    return database


@pytest.fixture(autouse=True)
def pinned_commit(monkeypatch):
    async def resolve_ref(self):
        self.commit_sha = "abcdef1234567"
        return self.commit_sha

    monkeypatch.setattr(GitHubTools, "resolve_ref", resolve_ref)


def make_generate(calls: list):
    async def generate(repository, *, on_progress=None, **_):
        calls.append(repository.full_name)
        for node in ("inspect_tree", "write_report"):
            if on_progress:
                await on_progress(node)
        report = ProjectReport(
            project_name="demo",
            summary="s",
            sections=[
                ReportSection(
                    title="t",
                    content="c",
                    citations=[Citation(path="README.md", start_line=1, end_line=1)],
                )
            ],
        )
        return ReportResult(report, "<html>demo</html>", "abcdef1234567")

    return generate


async def test_claim_is_exclusive(db):
    await jobs.create_job(db, REPO)
    first = await jobs.claim_job(db)
    second = await jobs.claim_job(db)

    assert first["status"] == jobs.RUNNING
    assert second is None


async def test_worker_completes_job_and_records_progress(db):
    calls = []
    job_id = await jobs.create_job(db, REPO)

    assert await worker.run_once(db, make_generate(calls)) is True

    job = await db.jobs.find_one({"_id": jobs.parse_object_id(job_id)})
    assert job["status"] == jobs.DONE
    assert job["commit_sha"] == "abcdef1234567"
    steps = [event["step"] for event in job["events"]]
    assert steps == ["queued", "running", "resolved", "inspect_tree", "write_report", "done"]
    assert await worker.run_once(db, make_generate(calls)) is False


async def test_same_repo_and_commit_reuses_cached_report(db):
    calls = []
    await jobs.create_job(db, REPO)
    await worker.run_once(db, make_generate(calls))
    second_id = await jobs.create_job(db, REPO)
    await worker.run_once(db, make_generate(calls))

    assert calls == ["owner/project"]  # generated once
    second = await db.jobs.find_one({"_id": jobs.parse_object_id(second_id)})
    assert second["status"] == jobs.DONE
    assert second["cached"] is True
    assert await db.reports.count_documents({}) == 1


async def test_known_errors_fail_job_with_message(db):
    async def generate(repository, **_):
        raise GitHubError("Repository or requested file was not found or is not public.")

    job_id = await jobs.create_job(db, REPO)
    await worker.run_once(db, generate)

    job = await db.jobs.find_one({"_id": jobs.parse_object_id(job_id)})
    assert job["status"] == jobs.FAILED
    assert "not public" in job["error"]


async def test_unexpected_errors_do_not_leak_details(db):
    async def generate(repository, **_):
        raise KeyError("secret-internal-detail")

    job_id = await jobs.create_job(db, REPO)
    await worker.run_once(db, generate)

    job = await db.jobs.find_one({"_id": jobs.parse_object_id(job_id)})
    assert job["status"] == jobs.FAILED
    assert "secret-internal-detail" not in job["error"]


async def test_api_job_lifecycle(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            bad = await client.post("/jobs", json={"url": "https://example.com/a/b"})
            assert bad.status_code == 422

            created = await client.post(
                "/jobs", json={"url": "https://github.com/owner/project"}
            )
            assert created.status_code == 202
            job_id = created.json()["id"]

            queued = (await client.get(f"/jobs/{job_id}")).json()
            assert queued["status"] == "queued"
            assert queued["repo"] == "owner/project"
            assert (await client.get(f"/jobs/{job_id}/report")).status_code == 404

            await worker.run_once(db, make_generate([]))

            done = (await client.get(f"/jobs/{job_id}")).json()
            assert done["status"] == "done" and done["report_ready"] is True
            report = await client.get(f"/jobs/{job_id}/report")
            assert report.status_code == 200
            assert report.text == "<html>demo</html>"

            assert (await client.get("/jobs/not-an-id")).status_code == 404
    finally:
        app.dependency_overrides.clear()


async def test_sse_streams_events_then_end(db):
    job_id = await jobs.create_job(db, REPO)
    await worker.run_once(db, make_generate([]))
    app.dependency_overrides[get_db] = lambda: db
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/jobs/{job_id}/events")
            missing = await client.get("/jobs/000000000000000000000000/events")
    finally:
        app.dependency_overrides.clear()

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: progress") == 6
    assert response.text.rstrip().endswith('data: {"status": "done", "error": null}')
    assert missing.status_code == 404
