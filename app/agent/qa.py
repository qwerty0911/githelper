"""Answers follow-up questions about an analyzed repository.

The agent plans which files to read and what to search for, reads them at
the same commit the report used, and answers with citations. Citations are
kept only when the lines were actually read, show real content, and a
second model call confirms they back the claim they are cited for. The
answer is then rewritten to say only what those verified lines show.
"""

import os
import re
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
# Search more of the tree than the report agent does: answers often live in
# small modules (config, db) ranked below the entrypoints.
QA_SEARCH_CANDIDATES = 100
# Longer ranges rarely point at the specific evidence.
MAX_CITATION_LINES = 30
# Bump when prompts or verification change so cached answers are redone.
QA_VERSION = 3
# Optional stronger model for the support check and the rewrite. These
# steps judge evidence, so they benefit most from a better model while the
# planning and drafting steps stay on the cheaper `OPENAI_MODEL`.
CHECK_MODEL_ENV = "OPENAI_QA_CHECK_MODEL"
_HAS_CONTENT = re.compile(r"\w")
_ASCII_WORD = re.compile(r"[A-Za-z_]")


def code_queries(queries: list[str]) -> list[str]:
    """Keeps search queries that can match code.

    The planner sometimes searches for natural-language words (for example
    Korean) that never appear in source, which wastes the search budget.

    Args:
        queries: Regular expressions proposed by the planner.

    Returns:
        Queries that contain at least one ASCII letter or underscore.
    """
    return [query for query in queries if _ASCII_WORD.search(query)]


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
        description=(
            "코드에 실제로 쓰이는 영어 식별자나 키워드의 파이썬 정규식. "
            "한국어 단어는 코드에 없으니 쓰지 마세요. 최대 3개. "
            "예: getenv|environ, useState|from 'react', login|auth"
        ),
    )


class Evidence(report_lib.Citation):
    """A citation plus the fact it is supposed to show."""

    claim: str = pydantic.Field(description="이 줄들이 보여주는 사실 한 문장")


class Answer(pydantic.BaseModel):
    """The LLM's answer before verification."""

    answer: str = pydantic.Field(description="질문에 대한 한국어 답변")
    citations: list[Evidence] = pydantic.Field(
        default_factory=list,
        description="답의 사실을 직접 보여주는 파일 경로, 라인 범위, 주장",
    )


class RevisedAnswer(pydantic.BaseModel):
    """An answer rewritten to stay within its verified evidence."""

    answer: str = pydantic.Field(description="근거로 확인되는 내용만 담은 답변")


class Verdict(pydantic.BaseModel):
    """Whether one citation backs its claim."""

    index: int = pydantic.Field(description="검사한 항목 번호")
    supports: bool = pydantic.Field(
        description="인용한 줄이 주장을 직접 보여주면 true"
    )


class SupportCheck(pydantic.BaseModel):
    """Verdicts for every citation."""

    verdicts: list[Verdict] = pydantic.Field(default_factory=list)


class AnswerResult(NamedTuple):
    """A verified answer.

    Attributes:
        answer: Answer text.
        citations: Citations that point at read lines and back their claim.
        grounded: Whether at least one citation survived verification.
    """

    answer: str
    citations: list[Evidence]
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


def cited_text(files: dict[str, str], citation: report_lib.Citation) -> str:
    """Returns the numbered lines a citation points at.

    Args:
        files: Path to `N: text` numbered content the agent read.
        citation: A citation that `filter_citations` already accepted.

    Returns:
        The cited lines, still prefixed with their line numbers.
    """
    lines = []
    for line in files.get(citation.path, "").splitlines():
        number, separator, _ = line.partition(": ")
        if (
            separator
            and number.isdigit()
            and citation.start_line <= int(number) <= citation.end_line
        ):
            lines.append(line)
    return "\n".join(lines)


def drop_weak_citations(
    citations: list[Evidence], files: dict[str, str]
) -> list[Evidence]:
    """Drops citations that cannot show anything specific.

    A citation is weak when its range is longer than `MAX_CITATION_LINES`
    or its lines hold no letters or digits (blank lines, `/**`, braces).

    Args:
        citations: Citations that point at read lines.
        files: Path to numbered content the agent read.

    Returns:
        The citations worth checking for support.
    """
    kept = []
    for citation in citations:
        if citation.end_line - citation.start_line + 1 > MAX_CITATION_LINES:
            continue
        text = "\n".join(
            line.partition(": ")[2]
            for line in cited_text(files, citation).splitlines()
        )
        if _HAS_CONTENT.search(text):
            kept.append(citation)
    return kept


async def check_support(
    model: Any,
    citations: list[Evidence],
    files: dict[str, str],
    config: dict[str, Any],
) -> list[Evidence]:
    """Keeps only citations whose lines back their claim.

    Args:
        model: Chat model.
        citations: Citations to check.
        files: Path to numbered content the agent read.
        config: Tracing run config.

    Returns:
        The supported citations, in their original order. Citations the
        checker skips count as unsupported.
    """
    if not citations:
        return []
    items = "\n\n".join(
        f"[{index}] 주장: {citation.claim}\n"
        f"인용 ({citation.path}:{citation.start_line}-{citation.end_line}):\n"
        f"{cited_text(files, citation)}"
        for index, citation in enumerate(citations)
    )
    result = await model.with_structured_output(SupportCheck).ainvoke(
        [("system", prompts.check_support()), ("human", items)],
        config=config,
    )
    supported = {
        verdict.index for verdict in result.verdicts if verdict.supports
    }
    return [
        citation
        for index, citation in enumerate(citations)
        if index in supported
    ]


def evidence_block(citations: list[Evidence], files: dict[str, str]) -> str:
    """Formats verified citations with their claims and exact lines."""
    return "\n\n".join(
        f"- {citation.path}:{citation.start_line}-{citation.end_line} "
        f"({citation.claim})\n{cited_text(files, citation)}"
        for citation in citations
    )


async def revise_answer(
    model: Any,
    question: str,
    draft: str,
    citations: list[Evidence],
    files: dict[str, str],
    *,
    level: audience.Level,
    config: dict[str, Any],
) -> str:
    """Rewrites a draft so it only says what the verified evidence shows.

    Args:
        model: Chat model.
        question: The user's question.
        draft: The first answer.
        citations: Citations that passed the support check.
        files: Path to numbered content the agent read.
        level: Reader level.
        config: Tracing run config.

    Returns:
        The revised answer text.
    """
    result = await model.with_structured_output(RevisedAnswer).ainvoke(
        [
            ("system", prompts.revise_answer(level)),
            (
                "human",
                f"질문: {question}\n\n초안 답변:\n{draft}\n\n"
                f"확인된 근거:\n{evidence_block(citations, files)}",
            ),
        ],
        config=config,
    )
    return result.answer


def check_model_name() -> str:
    """Returns the model used for the support check and the rewrite."""
    return runner.resolve_model_name(os.getenv(CHECK_MODEL_ENV))


def cache_version() -> str:
    """Returns the answer-cache key for the current pipeline and models.

    Changing the prompts (`QA_VERSION`) or either model gives a new key, so
    cached answers never hide the effect of a change.
    """
    return f"{QA_VERSION}/{runner.resolve_model_name()}/{check_model_name()}"


async def answer_question(
    repository: github.Repository,
    commit_sha: str,
    question: str,
    *,
    level: audience.Level = audience.DEFAULT_LEVEL,
    report: dict[str, Any] | None = None,
    model: Any = None,
    checker: Any = None,
    tools: github.GitHubTools | None = None,
) -> AnswerResult:
    """Answers a question about a repository at a pinned commit.

    Args:
        repository: The analyzed repository.
        commit_sha: Commit the report was generated from.
        question: The user's question.
        level: Reader level the answer is written for.
        report: The stored report, used as a map of the project.
        model: Optional chat model for planning and drafting, mainly for
            tests.
        checker: Optional chat model for the support check and rewrite.
            Defaults to `OPENAI_QA_CHECK_MODEL` when set, else `model`.
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
    if checker is None:
        checker = (
            runner.create_model(check_model_name())
            if os.getenv(CHECK_MODEL_ENV)
            else model
        )
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
                code_queries(plan.search_queries),
                tree[:QA_SEARCH_CANDIDATES],
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
        read = report_lib.filter_citations(draft.citations, contents)
        citations = await check_support(
            checker, drop_weak_citations(read, contents), contents, config
        )
        answer = draft.answer
        if citations:
            answer = await revise_answer(
                checker,
                question,
                draft.answer,
                citations,
                contents,
                level=level,
                config=config,
            )
    finally:
        if owns_tools:
            await tools.close()
        tracing.flush_traces()

    return AnswerResult(answer, citations, bool(citations))
