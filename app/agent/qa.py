"""Answers follow-up questions about an analyzed repository.

A tool-calling agent searches and reads code at the same commit the report
used, and can draw diagrams: LangGraph workflows and compose services are
extracted from code, other structures are proposed with evidence. The
answer is drafted with citations, which are kept only when the lines were
read, show real content, and a second model call confirms they back their
claim. The answer is then rewritten to say only what the verified lines
and diagrams show.
"""

import os
import re
from typing import Any, NamedTuple

from langchain_core import messages as lc_messages
import pydantic

from app.agent import audience
from app.agent import budget
from app.agent import diagrams as diagrams_lib
from app.agent import github
from app.agent import prompts
from app.agent import report as report_lib
from app.agent import runner
from app.agent import tracing

MAX_QA_CONTEXT_TOKENS = 10_000
MAX_QUESTION_CHARS = 500
# Model turns in the tool loop; each turn may call several tools.
MAX_TOOL_STEPS = 6
MAX_DIAGRAMS = 3
MAX_SEARCH_RESULTS = 20
MAX_TOOL_RESULT_CHARS = 6_000
FALLBACK_PATHS = 3
# Search more of the tree than the report agent does: answers often live in
# small modules (config, db) ranked below the entrypoints.
QA_SEARCH_CANDIDATES = 100
# Longer ranges rarely point at the specific evidence.
MAX_CITATION_LINES = 30
# Bump when prompts or verification change so cached answers are redone.
QA_VERSION = 9
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


class SearchCodeArgs(pydantic.BaseModel):
    """Arguments of the `search_code` tool."""

    pattern: str = pydantic.Field(
        description="코드에 쓰이는 영어 식별자의 파이썬 정규식"
    )


class ReadFileArgs(pydantic.BaseModel):
    """Arguments of the `read_file` tool."""

    path: str = pydantic.Field(description="트리에 있는 파일 경로")
    start_line: int = pydantic.Field(default=1, ge=1)
    end_line: int | None = pydantic.Field(
        default=None, description="마지막 줄. 비우면 끝까지(예산 안에서)"
    )


class ExtractArgs(pydantic.BaseModel):
    """Arguments of the extraction tools."""

    path: str | None = pydantic.Field(
        default=None, description="대상 파일. 비우면 자동으로 찾음"
    )


def _tool(name: str, description: str, args: type[pydantic.BaseModel]):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": args.model_json_schema(),
        },
    }


TOOLS = [
    _tool(
        "search_code",
        "정규식으로 코드를 검색해 일치하는 줄(경로:줄: 내용)을 돌려줍니다.",
        SearchCodeArgs,
    ),
    _tool(
        "read_file",
        "파일의 줄 범위를 줄 번호와 함께 읽습니다.",
        ReadFileArgs,
    ),
    _tool(
        "extract_langgraph",
        "Python 코드에서 LangGraph StateGraph의 노드와 엣지를 정확히 "
        "추출해 워크플로우 그림을 만듭니다.",
        ExtractArgs,
    ),
    _tool(
        "extract_services",
        "docker-compose 파일에서 서비스와 의존 관계를 추출해 구성도를 "
        "만듭니다.",
        ExtractArgs,
    ),
    _tool(
        "draw_diagram",
        "아키텍처나 흐름을 노드와 엣지로 그립니다. 각 노드와 엣지에는 "
        "읽은 파일의 경로와 줄 번호를 근거로 넣으세요. 근거가 확인되지 "
        "않은 부분은 점선으로 그려집니다.",
        diagrams_lib.DrawDiagramArgs,
    ),
]


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
        grounded: Whether at least one citation survived verification or a
            diagram was read from code.
        diagrams: Diagrams made while answering.
    """

    answer: str
    citations: list[Evidence]
    grounded: bool
    diagrams: tuple[diagrams_lib.Diagram, ...] = ()


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


def diagram_evidence(
    diagrams: list[diagrams_lib.Diagram], files: dict[str, str]
) -> str:
    """Lists each diagram edge with the code line that defines it.

    Diagrams read from code are facts about the code, so the drafting and
    rewriting steps get every edge with its exact line instead of a short
    summary they might treat as unconfirmed.

    Args:
        diagrams: Diagrams made while answering.
        files: Path to numbered content the agent read.

    Returns:
        A text block, or an empty string when there are no diagrams.
    """
    blocks = []
    for diagram in diagrams:
        source = (
            "코드에서 자동 추출, 모든 연결이 코드로 확인됨"
            if diagram.kind != "model"
            else "AI가 그린 그림, '미확인' 연결은 근거 없음"
        )
        lines = [f"[그림] {diagram.title} ({source})"]
        names = {node.id: node.label for node in diagram.nodes}
        for edge in diagram.edges:
            label = f" ({edge.label})" if edge.label else ""
            status = "확인됨" if edge.verified else "미확인"
            where = ""
            if edge.citation is not None:
                code = cited_text(files, edge.citation).partition(": ")[2]
                where = (
                    f" — {edge.citation.path}:{edge.citation.start_line}"
                    f" `{code.strip()[:120]}`"
                )
            lines.append(
                f"- {names.get(edge.source, edge.source)} -> "
                f"{names.get(edge.target, edge.target)}{label} "
                f"[{status}]{where}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


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
    diagram_notes: str = "",
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
        diagram_notes: Diagram edges with their code lines, from
            `diagram_evidence`.

    Returns:
        The revised answer text.
    """
    result = await model.with_structured_output(RevisedAnswer).ainvoke(
        [
            ("system", prompts.revise_answer(level)),
            (
                "human",
                f"질문: {question}\n\n초안 답변:\n{draft}\n\n"
                f"확인된 근거:\n{evidence_block(citations, files)}"
                + (f"\n\n그림 근거:\n{diagram_notes}" if diagram_notes else ""),
            ),
        ],
        config=config,
    )
    return result.answer


class ToolBox:
    """Runs the agent's tool calls and records what they found.

    Attributes:
        contents: Path to numbered lines read so far, used to verify
            citations.
        diagrams: Diagrams made so far.
    """

    def __init__(self, tools: github.GitHubTools, tree: list[str]) -> None:
        """Initializes the toolbox.

        Args:
            tools: GitHub tools pinned to the analyzed commit.
            tree: The repository tree.
        """
        self._tools = tools
        self._tree = tree
        self._tree_paths = set(tree)
        self._left = MAX_QA_CONTEXT_TOKENS
        self.contents: dict[str, str] = {}
        self.diagrams: list[diagrams_lib.Diagram] = []

    async def run(self, name: str, raw_args: dict[str, Any]) -> str:
        """Runs one tool call and returns its result as text.

        Errors are returned as text so the model can correct itself.
        """
        try:
            if name == "search_code":
                return await self._search(
                    SearchCodeArgs.model_validate(raw_args)
                )
            if name == "read_file":
                return await self._read(ReadFileArgs.model_validate(raw_args))
            if name == "extract_langgraph":
                return await self._langgraph(
                    ExtractArgs.model_validate(raw_args)
                )
            if name == "extract_services":
                return await self._services(
                    ExtractArgs.model_validate(raw_args)
                )
            if name == "draw_diagram":
                return self._draw(
                    diagrams_lib.DrawDiagramArgs.model_validate(raw_args)
                )
        except pydantic.ValidationError as error:
            return f"오류: 인자가 올바르지 않습니다. {error.errors()[:2]}"
        except (github.GitHubError, ValueError, re.error) as error:
            return f"오류: {error}"
        return f"오류: '{name}' 도구는 없습니다."

    def _remember(self, path: str, numbered: str, *, charge: bool) -> None:
        """Adds lines to `contents` so they can be cited.

        Args:
            path: File the lines belong to.
            numbered: `N: text` lines.
            charge: Whether the lines count against the reading budget.
                Search hits and diagram lines are short, so they stay
                citable even after the budget is spent.
        """
        used = budget.add_chunk(
            self.contents,
            path,
            numbered,
            self._left if charge else runner.MAX_FILE_TOKENS,
            max_file_tokens=runner.MAX_FILE_TOKENS,
        )
        if charge:
            self._left -= used

    async def _search(self, args: SearchCodeArgs) -> str:
        if not code_queries([args.pattern]):
            return "오류: 코드에 쓰이는 영어 식별자로 검색하세요."
        matches = await self._tools.search_code(
            args.pattern,
            paths=self._tree[:QA_SEARCH_CANDIDATES],
            max_matches=MAX_SEARCH_RESULTS,
        )
        if not matches:
            return "일치하는 줄이 없습니다."
        for match in matches:
            self._remember(
                str(match["path"]),
                f"{match['line']}: {match['text']}",
                charge=False,
            )
        return "\n".join(
            f"{m['path']}:{m['line']}: {str(m['text']).strip()[:200]}"
            for m in matches
        )

    async def _read(self, args: ReadFileArgs) -> str:
        if args.path not in self._tree_paths:
            return f"오류: '{args.path}'는 트리에 없습니다."
        if self._left <= 0:
            return "읽기 예산을 다 썼습니다. 지금까지 읽은 내용으로 답하세요."
        text = await self._tools.read_file(
            args.path,
            start_line=args.start_line,
            end_line=args.end_line,
            max_chars=200_000,
        )
        shown = budget.fit_lines(text, min(runner.MAX_FILE_TOKENS, self._left))
        self._remember(args.path, shown, charge=True)
        return shown[:MAX_TOOL_RESULT_CHARS] or "(빈 범위)"

    def _add_diagram(self, diagram: diagrams_lib.Diagram) -> str:
        if len(self.diagrams) >= MAX_DIAGRAMS:
            return "그림은 최대 3개까지 만들 수 있습니다."
        self.diagrams.append(diagram)
        return diagrams_lib.summarize(diagram)

    def _cite_diagram_lines(
        self, diagram: diagrams_lib.Diagram, path: str, text: str
    ) -> None:
        """Makes the lines a diagram was read from citable."""
        lines = text.splitlines()
        numbers = sorted(
            {
                part.citation.start_line
                for part in [*diagram.nodes, *diagram.edges]
                if part.citation is not None
            }
        )
        self._remember(
            path,
            "\n".join(f"{n}: {lines[n - 1]}" for n in numbers),
            charge=False,
        )

    async def _langgraph(self, args: ExtractArgs) -> str:
        if args.path:
            paths = [args.path] if args.path in self._tree_paths else []
        else:
            python = [p for p in self._tree if p.endswith(".py")]
            matches = await self._tools.search_code(
                r"StateGraph\(|\.add_node\(",
                paths=python[:QA_SEARCH_CANDIDATES],
                max_matches=MAX_SEARCH_RESULTS,
            )
            paths = diagrams_lib.rank_graph_files(
                list(dict.fromkeys(str(m["path"]) for m in matches))
            )
        results = []
        for path in paths:
            if len(self.diagrams) >= MAX_DIAGRAMS:
                break
            text = await self._tools.read_text(path)
            for diagram in diagrams_lib.extract_langgraph(text, path):
                self._cite_diagram_lines(diagram, path, text)
                results.append(self._add_diagram(diagram))
        if not results:
            return "LangGraph 그래프를 찾지 못했습니다."
        skipped = [p for p in paths if not any(p in r for r in results)]
        if skipped:
            results.append(
                "그래프가 있지만 그리지 않은 파일: "
                + ", ".join(skipped)
                + " (필요하면 path를 지정해 다시 호출하세요)"
            )
        return "\n".join(results)

    async def _services(self, args: ExtractArgs) -> str:
        path = args.path or next(
            (p for p in diagrams_lib.COMPOSE_FILES if p in self._tree_paths),
            None,
        )
        if path is None or path not in self._tree_paths:
            return "docker-compose 파일을 찾지 못했습니다."
        text = await self._tools.read_text(path)
        diagram = diagrams_lib.extract_services(text, path)
        if diagram is None:
            return f"{path}에서 서비스를 찾지 못했습니다."
        self._cite_diagram_lines(diagram, path, text)
        return self._add_diagram(diagram)

    def _draw(self, args: diagrams_lib.DrawDiagramArgs) -> str:
        if not args.nodes:
            return "오류: 노드가 없습니다."
        diagram = diagrams_lib.build_model_diagram(args, self.contents)
        if diagrams_lib.duplicates(diagram, self.diagrams):
            return (
                "이미 코드에서 추출한 같은 내용의 그림이 있어 다시 그리지 "
                "않았습니다."
            )
        return self._add_diagram(diagram)


async def research(
    model: Any,
    toolbox: ToolBox,
    question: str,
    report: dict[str, Any] | None,
    tree: list[str],
    config: dict[str, Any],
) -> None:
    """Lets the model call tools until it has enough evidence.

    Args:
        model: Chat model that supports tool calling.
        toolbox: Executes tool calls and collects evidence.
        question: The user's question.
        report: The stored report, used as a map of the project.
        tree: The repository tree.
        config: Tracing run config.
    """
    agent = model.bind_tools(TOOLS)
    history: list[Any] = [
        lc_messages.SystemMessage(prompts.tool_agent(MAX_TOOL_STEPS)),
        lc_messages.HumanMessage(
            f"질문: {question}\n\n프로젝트 개요:\n{_report_context(report)}"
            "\n\n파일 트리:\n" + "\n".join(tree[:300])
        ),
    ]
    for _ in range(MAX_TOOL_STEPS):
        reply = await agent.ainvoke(history, config=config)
        history.append(reply)
        calls = getattr(reply, "tool_calls", None) or []
        if not calls:
            return
        for call in calls:
            result = await toolbox.run(call["name"], call.get("args") or {})
            history.append(
                lc_messages.ToolMessage(
                    content=result[:MAX_TOOL_RESULT_CHARS],
                    tool_call_id=call["id"],
                )
            )


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
        model: Optional chat model for the tool loop and drafting, mainly
            for tests.
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
        toolbox = ToolBox(tools, tree)
        await research(model, toolbox, question, report, tree, config)
        contents = toolbox.contents
        if not contents and not toolbox.diagrams:
            for path in tree[:FALLBACK_PATHS]:
                await toolbox.run("read_file", {"path": path})
        diagram_notes = diagram_evidence(toolbox.diagrams, contents)

        draft = await model.with_structured_output(Answer).ainvoke(
            [
                ("system", prompts.answer_question(level)),
                (
                    "human",
                    f"저장소: {repository.full_name}\n질문: {question}\n\n"
                    + (
                        f"그림 근거:\n{diagram_notes}\n\n"
                        if diagram_notes
                        else ""
                    )
                    + f"파일 내용:\n{contents}",
                ),
            ],
            config=config,
        )
        read = report_lib.filter_citations(draft.citations, contents)
        citations = await check_support(
            checker, drop_weak_citations(read, contents), contents, config
        )
        answer = draft.answer
        if citations or toolbox.diagrams:
            answer = await revise_answer(
                checker,
                question,
                draft.answer,
                citations,
                contents,
                level=level,
                config=config,
                diagram_notes=diagram_notes,
            )
    finally:
        if owns_tools:
            await tools.close()
        tracing.flush_traces()

    from_code = any(d.kind != "model" for d in toolbox.diagrams)
    return AnswerResult(
        answer,
        citations,
        bool(citations) or from_code,
        tuple(toolbox.diagrams),
    )
