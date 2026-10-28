import datetime

import httpx
import mongomock_motor
import pytest

from app import db as db_lib
from app import jobs
from app import main
from app import worker
from app.agent import diagrams as diagrams_lib
from app.agent import github
from app.agent import qa
from app.agent import report as report_lib
from app.agent import runner

REPO = github.Repository("owner", "project")
SHA = "abcdef1234567"


@pytest.fixture(name="db")
async def db_fixture():
    database = mongomock_motor.AsyncMongoMockClient()["test"]
    await db_lib.ensure_indexes(database)
    return database


@pytest.fixture(autouse=True)
def pinned_commit(monkeypatch):
    async def resolve_ref(self):
        self.commit_sha = SHA
        return self.commit_sha

    monkeypatch.setattr(github.GitHubTools, "resolve_ref", resolve_ref)


def make_generate(calls: list):
    async def generate(repository, *, on_progress=None, **kwargs):
        calls.append((repository.full_name, kwargs))
        for node in ("inspect_tree", "write_report"):
            if on_progress:
                await on_progress(node)
        report = report_lib.ProjectReport(
            project_name="demo",
            summary="s",
            sections=[
                report_lib.ReportSection(
                    title="t",
                    content="c",
                    citations=[
                        report_lib.Citation(
                            path="README.md", start_line=1, end_line=1
                        )
                    ],
                )
            ],
        )
        return runner.ReportResult(report, "<html>demo</html>", SHA)

    return generate


def client_for(db, answerer=None):
    main.app.dependency_overrides[db_lib.get_db] = lambda: db
    if answerer is not None:
        main.app.dependency_overrides[main.get_answerer] = lambda: answerer
    transport = httpx.ASGITransport(app=main.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    main.app.dependency_overrides.clear()


async def get_job(db, job_id):
    return await db.jobs.find_one({"_id": jobs.parse_object_id(job_id)})


async def test_claim_is_exclusive(db):
    await jobs.create_job(db, REPO)
    first = await jobs.claim_job(db)
    second = await jobs.claim_job(db)

    assert first["status"] == jobs.RUNNING
    assert first["attempts"] == 1
    assert second is None


async def test_stale_running_job_is_reclaimed(db):
    job_id = await jobs.create_job(db, REPO)
    await jobs.claim_job(db)
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    await db.jobs.update_one(
        {"_id": jobs.parse_object_id(job_id)}, {"$set": {"updated_at": old}}
    )

    reclaimed = await jobs.claim_job(db)

    assert str(reclaimed["_id"]) == job_id
    assert reclaimed["attempts"] == 2


async def test_worker_retries_then_gives_up(db):
    job_id = await jobs.create_job(db, REPO)
    await db.jobs.update_one(
        {"_id": jobs.parse_object_id(job_id)},
        {"$set": {"attempts": jobs.MAX_ATTEMPTS}},
    )

    await worker.run_once(db, make_generate([]))

    job = await get_job(db, job_id)
    assert job["status"] == jobs.FAILED
    assert "여러 번" in job["error"]


async def test_worker_completes_job_and_records_progress(db):
    calls = []
    job_id = await jobs.create_job(db, REPO, "junior")

    assert await worker.run_once(db, make_generate(calls)) is True

    job = await get_job(db, job_id)
    assert job["status"] == jobs.DONE
    assert job["commit_sha"] == SHA
    steps = [event["step"] for event in job["events"]]
    assert steps == [
        "queued",
        "running",
        "resolved",
        "inspect_tree",
        "write_report",
        "done",
    ]
    # The worker passes the pinned commit and level to the agent.
    assert calls[0][1]["commit_sha"] == SHA
    assert calls[0][1]["level"] == "junior"
    assert await worker.run_once(db, make_generate(calls)) is False


async def test_same_repo_commit_and_level_reuses_cached_report(db):
    calls = []
    await jobs.create_job(db, REPO)
    await worker.run_once(db, make_generate(calls))
    second_id = await jobs.create_job(db, REPO)
    await worker.run_once(db, make_generate(calls))
    await jobs.create_job(db, REPO, "senior")
    await worker.run_once(db, make_generate(calls))

    assert len(calls) == 2  # beginner once, senior once
    second = await get_job(db, second_id)
    assert second["status"] == jobs.DONE
    assert second["cached"] is True
    assert await db.reports.count_documents({}) == 2


async def test_legacy_unique_index_is_dropped(db):
    await db.reports.drop_indexes()
    await db.reports.create_index([("repo", 1), ("commit_sha", 1)], unique=True)
    await db_lib.ensure_indexes(db)

    names = set((await db.reports.index_information()).keys())
    assert "repo_1_commit_sha_1" not in names
    assert "repo_1_commit_sha_1_level_1" in names


async def test_known_errors_fail_job_with_message(db):
    async def generate(repository, **_):
        del repository
        raise github.GitHubError(
            "Repository or requested file was not found or is not public."
        )

    job_id = await jobs.create_job(db, REPO)
    await worker.run_once(db, generate)

    job = await get_job(db, job_id)
    assert job["status"] == jobs.FAILED
    assert "not public" in job["error"]


async def test_unexpected_errors_do_not_leak_details(db):
    async def generate(repository, **_):
        del repository
        raise KeyError("secret-internal-detail")

    job_id = await jobs.create_job(db, REPO)
    await worker.run_once(db, generate)

    job = await get_job(db, job_id)
    assert job["status"] == jobs.FAILED
    assert "secret-internal-detail" not in job["error"]


async def test_api_job_lifecycle(db):
    async with client_for(db) as client:
        bad = await client.post(
            "/jobs", json={"url": "https://example.com/a/b"}
        )
        assert bad.status_code == 422
        bad_level = await client.post(
            "/jobs",
            json={"url": "https://github.com/owner/project", "level": "guru"},
        )
        assert bad_level.status_code == 422

        created = await client.post(
            "/jobs", json={"url": "https://github.com/owner/project"}
        )
        assert created.status_code == 202
        job_id = created.json()["id"]

        queued = (await client.get(f"/jobs/{job_id}")).json()
        assert queued["status"] == "queued"
        assert queued["repo"] == "owner/project"
        assert queued["level"] == "beginner"
        assert (await client.get(f"/jobs/{job_id}/report")).status_code == 404

        await worker.run_once(db, make_generate([]))

        done = (await client.get(f"/jobs/{job_id}")).json()
        assert done["status"] == "done" and done["report_ready"] is True
        report = await client.get(f"/jobs/{job_id}/report")
        assert report.status_code == 200
        assert report.text == "<html>demo</html>"

        assert (await client.get("/jobs/not-an-id")).status_code == 404


async def test_api_deduplicates_identical_active_jobs(db):
    body = {"url": "https://github.com/owner/project/tree/dev"}
    async with client_for(db) as client:
        first = (await client.post("/jobs", json=body)).json()
        second = (await client.post("/jobs", json=body)).json()
        other_level = (
            await client.post("/jobs", json=body | {"level": "senior"})
        ).json()

    assert first["deduplicated"] is False
    assert second == {"id": first["id"], "deduplicated": True}
    assert other_level["id"] != first["id"]
    job = await get_job(db, first["id"])
    assert job["branch"] == "dev"


async def test_recent_reports_lists_latest_per_repo(db):
    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    repositories = (REPO, github.Repository("other", "lib"), REPO)
    for index, repository in enumerate(repositories):
        report_id = await jobs.save_report(
            db,
            repo=repository.full_name,
            branch=None,
            commit_sha=f"sha{index}",
            report={"project_name": repository.name, "summary": "s"},
            html="",
        )
        await db.reports.update_one(
            {"_id": report_id},
            {"$set": {"created_at": start + datetime.timedelta(minutes=index)}},
        )

    async with client_for(db) as client:
        recent = (await client.get("/reports/recent")).json()

    assert [item["repo"] for item in recent] == ["owner/project", "other/lib"]
    assert recent[0]["project_name"] == "project"


async def test_sse_streams_events_then_end(db):
    job_id = await jobs.create_job(db, REPO)
    await worker.run_once(db, make_generate([]))
    async with client_for(db) as client:
        response = await client.get(f"/jobs/{job_id}/events")
        missing = await client.get("/jobs/000000000000000000000000/events")

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: progress") == 6
    assert response.text.rstrip().endswith(
        'data: {"status": "done", "error": null}'
    )
    assert missing.status_code == 404


def make_answerer(calls: list):
    async def answerer(repository, commit_sha, question, **kwargs):
        calls.append((repository.full_name, commit_sha, question, kwargs))
        return qa.AnswerResult(
            "README에 있어요.",
            [report_lib.Citation(path="README.md", start_line=1, end_line=1)],
            True,
        )

    return answerer


async def test_questions_are_answered_cached_and_listed(db):
    calls = []
    job_id = await jobs.create_job(db, REPO, "junior")
    async with client_for(db, make_answerer(calls)) as client:
        early = await client.post(
            f"/jobs/{job_id}/questions", json={"question": "어디서 시작해?"}
        )
        assert early.status_code == 404  # Report not ready yet.

        await worker.run_once(db, make_generate([]))
        first = await client.post(
            f"/jobs/{job_id}/questions", json={"question": "어디서 시작해?"}
        )
        again = await client.post(
            f"/jobs/{job_id}/questions", json={"question": "  어디서  시작해? "}
        )
        history = (await client.get(f"/jobs/{job_id}/questions")).json()
        too_long = await client.post(
            f"/jobs/{job_id}/questions",
            json={"question": "x" * (qa.MAX_QUESTION_CHARS + 1)},
        )

    assert first.status_code == 200
    body = first.json()
    assert body["grounded"] is True and body["cached"] is False
    assert body["citations"][0]["url"] == (
        f"https://github.com/owner/project/blob/{SHA}/README.md#L1"
    )
    assert again.json()["cached"] is True
    assert len(calls) == 1
    assert calls[0][1] == SHA
    assert calls[0][3]["level"] == "junior"
    assert [item["question"] for item in history] == [
        "어디서 시작해?",
        "어디서  시작해?",
    ]
    assert too_long.status_code == 422


async def test_question_errors_map_to_http_status(db):
    async def broken(*_, **__):
        raise github.GitHubError("GitHub API rate limit reached.")

    job_id = await jobs.create_job(db, REPO)
    await worker.run_once(db, make_generate([]))
    async with client_for(db, broken) as client:
        response = await client.post(
            f"/jobs/{job_id}/questions", json={"question": "hi"}
        )

    assert response.status_code == 502
    assert "rate limit" in response.json()["detail"]


async def test_answers_from_older_qa_versions_are_not_reused(db):
    calls = []
    job_id = await jobs.create_job(db, REPO)
    await worker.run_once(db, make_generate([]))
    job = await get_job(db, job_id)
    await jobs.save_question(
        db,
        job_id=job["_id"],
        report_id=job["report_id"],
        question="어디서 시작해?",
        answer="old answer",
        citations=[],
        grounded=False,
        cached=False,
        version="1/old-model/old-model",
    )

    async with client_for(db, make_answerer(calls)) as client:
        response = await client.post(
            f"/jobs/{job_id}/questions", json={"question": "어디서 시작해?"}
        )

    assert response.json()["cached"] is False
    assert response.json()["answer"] == "README에 있어요."
    assert len(calls) == 1


async def test_question_diagrams_are_stored_and_served_as_mermaid(db):
    diagram = diagrams_lib.Diagram(
        title="흐름",
        kind="langgraph",
        nodes=[
            diagrams_lib.DiagramNode(
                id="load",
                label="load",
                citation=report_lib.Citation(
                    path="app/graph.py", start_line=4, end_line=4
                ),
                verified=True,
            ),
            diagrams_lib.DiagramNode(id="END", label="END", verified=True),
        ],
        edges=[
            diagrams_lib.DiagramEdge(source="load", target="END", verified=True)
        ],
    )

    async def answerer(*_, **__):
        return qa.AnswerResult("그림 참고", [], True, (diagram,))

    job_id = await jobs.create_job(db, REPO)
    await worker.run_once(db, make_generate([]))
    async with client_for(db, answerer) as client:
        first = (
            await client.post(
                f"/jobs/{job_id}/questions", json={"question": "흐름 그려줘"}
            )
        ).json()
        again = (
            await client.post(
                f"/jobs/{job_id}/questions", json={"question": "흐름 그려줘"}
            )
        ).json()

    for body in (first, again):
        [served] = body["diagrams"]
        assert served["title"] == "흐름"
        assert served["mermaid"].startswith("flowchart LR")
        assert 'n1(["END"])' in served["mermaid"]
        assert served["nodes"][0]["citation"]["url"] == (
            f"https://github.com/owner/project/blob/{SHA}/app/graph.py#L4"
        )
        assert served["nodes"][1]["citation"] is None
    assert again["cached"] is True
