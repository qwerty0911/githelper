"""Answers follow-up questions about an analyzed repository.

The agent plans which files to read and what to search for, reads them at
the same commit the report used, and answers with citations that are
verified against the lines it actually read.
"""

from typing import Any, NamedTuple

import pydantic

from app.agent import audience
from app.agent import github
from app.agent import prompts
from app.agent import report as report_lib
from app.agent import runner
from app.agent import tracing

MAX_QA_CONTEXT_TOKENS = 10_000
MAX_QA_PATHS = 5
MAX_QUESTION_CHARS = 500
FALLBACK_PATHS = 3


class QuestionPlan(pydantic.BaseModel):
    """Where the agent wants to look to answer a question."""

    paths: list[str] = pydantic.Field(
        default_factory=list,
        description=(
            f"답을 찾기 위해 읽을 트리 안의 파일 경로. 최대 {MAX_QA_PATHS}개"
        ),
    )
    search_queries: list[str] = pydantic.Field(
        default_factory=list,
        description="찾아볼 코드 패턴(파이썬 정규식). 최대 3개",
    )


class Answer(pydantic.BaseModel):
    """The LLM's answer before verification."""

    answer: str = pydantic.Field(description="질문에 대한 한국어 답변")
    citations: list[report_lib.Citation] = pydantic.Field(
        default_factory=list,
        description="답변의 근거가 되는 파일 경로와 라인 범위",
    )


class AnswerResult(NamedTuple):
    """A verified answer.

    Attributes:
        answer: Answer text.
        citations: Citations that point at lines the agent read.
        grounded: Whether at least one citation survived verification.
    """

    answer: str
    citations: list[report_lib.Citation]
    grounded: bool


def _report_context(report: dict[str, Any] | None) -> str:
    if not report:
        return "(없음)"
    lines = [f"요약: {report.get('summary', '')}"]
    for step in report.get("reading_order", []):
        lines.append(f"- 읽기 순서: {step.get('path')} ({step.get('reason')})")
    for section in report.get("sections", []):
        lines.append(f"- 섹션: {section.get('title')}")
    return "\n".join(lines)


async def answer_question(
    repository: github.Repository,
    commit_sha: str,
    question: str,
    *,
    level: audience.Level = audience.DEFAULT_LEVEL,
    report: dict[str, Any] | None = None,
    model: Any = None,
    tools: github.GitHubTools | None = None,
) -> AnswerResult:
    """Answers a question about a repository at a pinned commit.

    Args:
        repository: The analyzed repository.
        commit_sha: Commit the report was generated from.
        question: The user's question.
        level: Reader level the answer is written for.
        report: The stored report, used as a map of the project.
        model: Optional chat model, mainly for tests.
        tools: Optional GitHub tools, mainly for tests.

    Returns:
        The verified answer.

    Raises:
        ValueError: The question is empty or too long.
        RuntimeError: No model is configured.
        github.GitHubError: The repository cannot be read.
    """
    question = question.strip()
    if not question or len(question) > MAX_QUESTION_CHARS:
        raise ValueError(
            f"Questions must be 1 to {MAX_QUESTION_CHARS} characters long."
        )
    model = model or runner.create_model()
    owns_tools = tools is None
    tools = tools or github.GitHubTools(repository, commit_sha=commit_sha)
    config = tracing.tracing_config(
        repo=repository.full_name,
        branch=repository.branch,
        run_name="github_helper_question",
    )
    try:
        tree = await tools.get_repo_tree()
        plan = await model.with_structured_output(QuestionPlan).ainvoke(
            [
                (
                    "system",
                    prompts.plan_question(MAX_QA_PATHS),
                ),
                (
                    "human",
                    f"질문: {question}\n\n프로젝트 개요:\n"
                    f"{_report_context(report)}\n\n파일 트리:\n"
                    + "\n".join(tree[:300]),
                ),
            ],
            config=config,
        )

        contents: dict[str, str] = {}
        left = MAX_QA_CONTEXT_TOKENS
        tree_paths = set(tree)
        paths = [path for path in plan.paths if path in tree_paths]
        for path in paths[:MAX_QA_PATHS]:
            if left <= 0:
                break
            left -= await runner.read_into(tools, contents, path, left)
        if left > 0:
            left -= await runner.search_into(
                tools,
                contents,
                plan.search_queries,
                tree[: runner.SEARCH_CANDIDATES],
                left,
            )
        if not contents:
            for path in tree[:FALLBACK_PATHS]:
                left -= await runner.read_into(tools, contents, path, left)

        draft = await model.with_structured_output(Answer).ainvoke(
            [
                (
                    "system",
                    prompts.answer_question(level),
                ),
                (
                    "human",
                    f"저장소: {repository.full_name}\n질문: {question}\n\n"
                    f"파일 내용:\n{contents}",
                ),
            ],
            config=config,
        )
    finally:
        if owns_tools:
            await tools.close()
        tracing.flush_traces()

    citations = report_lib.filter_citations(draft.citations, contents)
    return AnswerResult(draft.answer, citations, bool(citations))
