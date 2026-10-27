"""LangGraph agent that explores a repository and writes a reading guide.

Flow: inspect_tree -> select_files -> read_selected -> deepen (loop)
-> write_report. The LLM only returns structured data; every claim is
verified against what was read before rendering.
"""

from collections.abc import Awaitable, Callable
import os
import re
from typing import Any, NamedTuple, TypedDict

import dotenv
import langchain_openai
from langgraph import graph as langgraph_graph
import pydantic

from app.agent import audience
from app.agent import budget
from app.agent import github
from app.agent import prompts
from app.agent import report as report_lib
from app.agent import tracing

dotenv.load_dotenv()

MAX_SELECTED_FILES = 10
MAX_CONTEXT_TOKENS = 14_000
MAX_FILE_TOKENS = 4_500
MAX_DEEPEN_ROUNDS = 2
MAX_SEARCH_QUERIES = 3
MAX_EXTRA_PATHS = 3
SEARCH_CANDIDATES = 40
SEARCH_WINDOW = 10

ProgressCallback = Callable[[str], Awaitable[None]]


class FileSelection(pydantic.BaseModel):
    """Files the agent wants to read first."""

    paths: list[str] = pydantic.Field(
        description="가이드를 만드는 데 꼭 필요한 저장소 파일 경로"
    )


class DeepenRequest(pydantic.BaseModel):
    """What else the agent wants to look at before writing."""

    done: bool = pydantic.Field(
        description="지금 읽은 내용만으로 가이드를 쓰기에 충분하면 true"
    )
    search_queries: list[str] = pydantic.Field(
        default_factory=list,
        description="더 확인할 코드 패턴(정규식). 최대 3개",
    )
    extra_paths: list[str] = pydantic.Field(
        default_factory=list, description="추가로 읽을 트리 안의 파일 경로"
    )


class ReportResult(NamedTuple):
    """The verified report and its rendered HTML."""

    report: report_lib.ProjectReport
    html: str
    commit_sha: str | None


class AgentState(TypedDict, total=False):
    """LangGraph state shared between nodes."""

    repository: github.Repository
    level: audience.Level
    commit_sha: str | None
    deepen_rounds: int
    exploration_done: bool
    tree: list[str]
    stack: dict[str, list[str]]
    selected_paths: list[str]
    file_contents: dict[str, str]
    report: report_lib.ProjectReport
    html: str


DEFAULT_MODEL = "gpt-4o-mini"
# Reasoning models only accept the default temperature.
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def resolve_model_name(override: str | None = None) -> str:
    """Returns the model to use: `override`, else `OPENAI_MODEL`."""
    return override or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL


def create_model(override: str | None = None) -> Any:
    """Creates a chat model from environment settings.

    Args:
        override: Optional model name. Defaults to `OPENAI_MODEL`.

    Returns:
        A LangChain chat model.

    Raises:
        RuntimeError: `OPENAI_API_KEY` is not set.
    """
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required to generate a report.")
    name = resolve_model_name(override)
    if name.startswith(_REASONING_PREFIXES):
        return langchain_openai.ChatOpenAI(model=name)
    return langchain_openai.ChatOpenAI(model=name, temperature=0)


async def read_into(
    tools: github.GitHubTools,
    contents: dict[str, str],
    path: str,
    tokens_left: int,
    *,
    start_line: int = 1,
    end_line: int | None = None,
) -> int:
    """Reads a file (or range) into `contents` within the budget.

    Args:
        tools: GitHub tools.
        contents: Path to numbered content read so far. Updated in place.
        path: File to read.
        tokens_left: Remaining token budget.
        start_line: First line to read.
        end_line: Last line to read, or `None` for the whole file.

    Returns:
        Tokens used. Unreadable files use none.
    """
    try:
        text = await tools.read_file(
            path, start_line=start_line, end_line=end_line, max_chars=200_000
        )
    except (github.GitHubError, ValueError):
        return 0
    return budget.add_chunk(
        contents, path, text, tokens_left, max_file_tokens=MAX_FILE_TOKENS
    )


async def search_into(
    tools: github.GitHubTools,
    contents: dict[str, str],
    queries: list[str],
    candidates: list[str],
    tokens_left: int,
) -> int:
    """Runs code searches and reads a window around each match.

    Args:
        tools: GitHub tools.
        contents: Path to numbered content read so far. Updated in place.
        queries: Regular expressions. Invalid ones are skipped.
        candidates: Files to search.
        tokens_left: Remaining token budget.

    Returns:
        Tokens used.
    """
    used = 0
    for query in queries[:MAX_SEARCH_QUERIES]:
        try:
            matches = await tools.search_code(query, paths=candidates)
        except (re.error, github.GitHubError):
            continue
        for match in matches[:10]:
            if tokens_left - used <= 0:
                return used
            line = int(match["line"])
            used += await read_into(
                tools,
                contents,
                str(match["path"]),
                tokens_left - used,
                start_line=max(1, line - SEARCH_WINDOW),
                end_line=line + SEARCH_WINDOW,
            )
    return used


def build_graph(
    tools: github.GitHubTools,
    model_name: str | None = None,
    model: Any = None,
):
    """Builds the report-writing graph.

    Args:
        tools: GitHub tools bound to one repository.
        model_name: Optional model override.
        model: Optional pre-built chat model, mainly for tests.

    Returns:
        A compiled LangGraph graph.
    """
    if model is None:
        model = create_model(model_name)

    async def inspect_tree(state: AgentState) -> dict[str, Any]:
        del state  # Unused.
        tree = await tools.get_repo_tree()
        return {
            "tree": tree,
            "stack": await tools.detect_stack(tree),
            "commit_sha": tools.commit_sha,
        }

    async def select_files(state: AgentState) -> dict[str, Any]:
        selectable = state["tree"][:300]
        selector = model.with_structured_output(FileSelection)
        result = await selector.ainvoke(
            [
                (
                    "system",
                    prompts.select_files(MAX_SELECTED_FILES),
                ),
                ("human", "\n".join(selectable)),
            ]
        )
        selected = [path for path in result.paths if path in selectable]
        return {"selected_paths": selected[:MAX_SELECTED_FILES]}

    async def read_selected(state: AgentState) -> dict[str, Any]:
        contents: dict[str, str] = {}
        left = MAX_CONTEXT_TOKENS
        for path in state["selected_paths"]:
            if left <= 0:
                break
            left -= await read_into(tools, contents, path, left)
        return {"file_contents": contents, "deepen_rounds": 0}

    async def deepen(state: AgentState) -> dict[str, Any]:
        contents = dict(state["file_contents"])
        rounds = state["deepen_rounds"] + 1
        left = MAX_CONTEXT_TOKENS - budget.tokens_used(contents)
        if left <= 0:
            return {"exploration_done": True, "deepen_rounds": rounds}

        planner = model.with_structured_output(DeepenRequest)
        request = await planner.ainvoke(
            [
                (
                    "system",
                    prompts.DEEPEN,
                ),
                (
                    "human",
                    f"트리(일부):\n{chr(10).join(state['tree'][:150])}\n\n"
                    f"읽은 내용:\n{contents}",
                ),
            ]
        )
        if request.done:
            return {"exploration_done": True, "deepen_rounds": rounds}

        left -= await search_into(
            tools,
            contents,
            request.search_queries,
            state["tree"][:SEARCH_CANDIDATES],
            left,
        )
        tree = set(state["tree"])
        for path in request.extra_paths[:MAX_EXTRA_PATHS]:
            if left <= 0 or path not in tree or path in contents:
                continue
            left -= await read_into(tools, contents, path, left)

        finished = rounds >= MAX_DEEPEN_ROUNDS or left <= 0
        return {
            "file_contents": contents,
            "deepen_rounds": rounds,
            "exploration_done": finished,
        }

    async def write_report(state: AgentState) -> dict[str, Any]:
        level = state.get("level", audience.DEFAULT_LEVEL)
        generator = model.with_structured_output(report_lib.ProjectReport)
        result = await generator.ainvoke(
            [
                (
                    "system",
                    prompts.write_report(level),
                ),
                (
                    "human",
                    f"저장소: {state['repository'].full_name}\n"
                    f"자동 감지 스택/실행 명령: {state['stack']}\n"
                    "파일 트리(일부):\n"
                    f"{chr(10).join(state['tree'][:150])}\n\n"
                    f"파일 내용:\n{state['file_contents']}",
                ),
            ]
        )
        verified = report_lib.verify_report(
            result,
            state["file_contents"],
            tree=state["tree"],
            known_commands=state["stack"].get("commands", []),
        )
        if not verified.sections:
            raise ValueError(
                "The generated report has no sections with verifiable "
                "citations."
            )
        html = report_lib.render_report(
            verified,
            repo=state["repository"].full_name,
            commit_sha=state.get("commit_sha"),
            level=level,
        )
        return {"report": verified, "html": html}

    graph = langgraph_graph.StateGraph(AgentState)
    graph.add_node("inspect_tree", inspect_tree)
    graph.add_node("select_files", select_files)
    graph.add_node("read_selected", read_selected)
    graph.add_node("deepen", deepen)
    graph.add_node("write_report", write_report)
    graph.set_entry_point("inspect_tree")
    graph.add_edge("inspect_tree", "select_files")
    graph.add_edge("select_files", "read_selected")
    graph.add_edge("read_selected", "deepen")
    graph.add_conditional_edges(
        "deepen",
        lambda state: (
            "write_report" if state.get("exploration_done") else "deepen"
        ),
    )
    graph.add_edge("write_report", langgraph_graph.END)
    return graph.compile()


async def generate_report(
    repository: github.Repository,
    *,
    level: audience.Level = audience.DEFAULT_LEVEL,
    commit_sha: str | None = None,
    model_name: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> ReportResult:
    """Runs the agent end to end for one repository.

    Args:
        repository: Repository to analyze.
        level: Reader level the guide is written for.
        commit_sha: Optional commit to pin to. Resolved from the branch when
            omitted.
        model_name: Optional model override.
        on_progress: Called with each finished graph node name.

    Returns:
        The verified report, its HTML and the analyzed commit.
    """
    tools = github.GitHubTools(repository, commit_sha=commit_sha)
    try:
        graph = build_graph(tools, model_name)
        config = tracing.tracing_config(
            repo=repository.full_name, branch=repository.branch
        )
        state: dict[str, Any] = {}
        async for update in graph.astream(
            {"repository": repository, "level": level},
            config=config,
            stream_mode="updates",
        ):
            for node, output in update.items():
                state.update(output)
                if on_progress:
                    await on_progress(node)
        return ReportResult(
            state["report"], state["html"], state.get("commit_sha")
        )
    finally:
        await tools.close()
        tracing.flush_traces()
