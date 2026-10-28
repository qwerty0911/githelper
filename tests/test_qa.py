import re

from langchain_core import messages as lc_messages
import pytest

from app.agent import diagrams
from app.agent import github
from app.agent import qa

GRAPH_SOURCE = """\
from langgraph.graph import StateGraph, END

graph = StateGraph(dict)
graph.add_node("load", load)
graph.add_node("answer", answer)
graph.set_entry_point("load")
graph.add_edge("load", "answer")
graph.add_edge("answer", END)
"""

COMPOSE = """\
services:
  db:
    image: mongo:7
  api:
    build: .
    depends_on:
      - db
"""

FILES = {
    "README.md": "# demo",
    "app/auth.py": "\n".join(f"auth line {n}" for n in range(1, 21)),
    "app/config.py": "/**\n\nTOKEN = os.getenv('TOKEN')\n}",
    "app/graph.py": GRAPH_SOURCE,
    "docker-compose.yml": COMPOSE,
}


def numbered(path, start=1, end=None):
    lines = FILES[path].splitlines()[start - 1 : end]
    return "\n".join(f"{n}: {line}" for n, line in enumerate(lines, start))


class FakeTools:
    def __init__(self):
        self.read = []
        self.search_paths = None
        self.searches = []

    async def get_repo_tree(self):
        return list(FILES)

    async def read_file(
        self, path, *, start_line=1, end_line=None, max_chars=18_000
    ):
        self.read.append(path)
        return numbered(path, start_line, end_line)[:max_chars]

    async def read_text(self, path):
        return FILES[path]

    async def search_code(self, query, *, paths=None, max_matches=40):
        self.searches.append(query)
        self.search_paths = paths
        pattern = re.compile(query)
        matches = []
        for path in paths or list(FILES):
            for number, line in enumerate(FILES[path].splitlines(), 1):
                if pattern.search(line):
                    matches.append({"path": path, "line": number, "text": line})
        return matches[:max_matches]


def call(name, **args):
    return {"name": name, "args": args, "id": f"call_{name}_{len(args)}"}


def evidence(path, start, end, claim="claim"):
    return qa.Evidence(path=path, start_line=start, end_line=end, claim=claim)


class FakeModel:
    """Scripted tool loop, then drafts; supports citations claimed 'ok'."""

    def __init__(self, steps, citations, answer="auth.py에서 처리해요."):
        self.steps = list(steps)
        self.citations = citations
        self.answer = answer
        self.agent_calls = 0
        self.tool_results = []
        self.answer_context = None
        self.support_input = None
        self.revise_input = None

    def bind_tools(self, tools):
        assert {t["function"]["name"] for t in tools} >= {
            "search_code",
            "read_file",
            "extract_langgraph",
            "extract_services",
            "draw_diagram",
        }
        model = self

        class Agent:
            async def ainvoke(self, history, **_):
                model.agent_calls += 1
                model.tool_results = [
                    m.content
                    for m in history
                    if isinstance(m, lc_messages.ToolMessage)
                ]
                calls = model.steps.pop(0) if model.steps else []
                return lc_messages.AIMessage(content="", tool_calls=calls)

        return Agent()

    def with_structured_output(self, schema):
        model = self

        class Runner:
            async def ainvoke(self, messages, **_):
                if schema is qa.SupportCheck:
                    model.support_input = messages[1][1]
                    items = re.findall(
                        r"^\[(\d+)\] 주장: (.*)$",
                        model.support_input,
                        re.MULTILINE,
                    )
                    return qa.SupportCheck(
                        verdicts=[
                            qa.Verdict(index=int(i), supports=claim == "ok")
                            for i, claim in items
                        ]
                    )
                if schema is qa.RevisedAnswer:
                    model.revise_input = messages[1][1]
                    return qa.RevisedAnswer(answer="revised")
                model.answer_context = messages[1][1]
                return qa.Answer(answer=model.answer, citations=model.citations)

        return Runner()


REPO = github.Repository("o", "r")


@pytest.fixture(autouse=True)
def no_real_checker(monkeypatch):
    # .env may set a check model; tests must never build a real one.
    monkeypatch.delenv(qa.CHECK_MODEL_ENV, raising=False)


async def ask(model, tools=None, question="q", **kwargs):
    return await qa.answer_question(
        REPO, "abc", question, model=model, tools=tools or FakeTools(), **kwargs
    )


async def test_keeps_only_citations_that_were_read_and_supported():
    model = FakeModel(
        [
            [
                call("read_file", path="app/auth.py"),
                call("read_file", path="ghost.py"),
            ]
        ],
        [
            evidence("app/auth.py", 2, 4, "ok"),
            evidence("ghost.py", 1, 1, "ok"),
            evidence("app/auth.py", 50, 60, "ok"),
            evidence("app/auth.py", 5, 5, "unrelated"),
        ],
    )
    tools = FakeTools()

    result = await ask(model, tools)

    assert result.grounded is True
    assert result.citations == [evidence("app/auth.py", 2, 4, "ok")]
    assert "ghost.py" not in tools.read
    assert any("트리에 없습니다" in r for r in model.tool_results)


async def test_checker_sees_claim_and_exact_cited_lines():
    model = FakeModel(
        [[call("read_file", path="app/auth.py")]],
        [evidence("app/auth.py", 3, 4, "ok")],
    )
    await ask(model)

    assert "주장: ok" in model.support_input
    assert "3: auth line 3\n4: auth line 4" in model.support_input
    assert "5: auth line 5" not in model.support_input


async def test_answer_is_not_grounded_when_checker_rejects_everything():
    model = FakeModel(
        [[call("read_file", path="app/auth.py")]],
        [evidence("app/auth.py", 1, 1, "unrelated")],
    )

    result = await ask(model)

    assert result.grounded is False
    assert not result.citations
    assert model.revise_input is None


async def test_search_hits_are_citable_and_cover_more_of_the_tree():
    model = FakeModel(
        [[call("search_code", pattern="getenv")]],
        [evidence("app/config.py", 3, 3, "ok")],
    )
    tools = FakeTools()

    result = await ask(model, tools)

    assert "app/config.py:3:" in model.tool_results[0]
    assert tools.search_paths == list(FILES)[: qa.QA_SEARCH_CANDIDATES]
    assert result.citations == [evidence("app/config.py", 3, 3, "ok")]


async def test_natural_language_search_is_refused():
    model = FakeModel([[call("search_code", pattern="환경 변수")]], [])
    tools = FakeTools()
    await ask(model, tools)

    assert not tools.searches
    assert "영어 식별자" in model.tool_results[0]


async def test_loop_stops_after_max_steps():
    endless = [[call("search_code", pattern="auth")]] * 20
    model = FakeModel(endless, [])
    await ask(model)

    assert model.agent_calls == qa.MAX_TOOL_STEPS


async def test_unknown_tools_and_bad_arguments_are_reported_to_model():
    model = FakeModel(
        [[call("rm_rf"), call("read_file", start_line=0)]],
        [],
    )
    await ask(model)

    assert "도구는 없습니다" in model.tool_results[0]
    assert "인자가 올바르지 않습니다" in model.tool_results[1]


async def test_no_research_falls_back_to_top_files():
    model = FakeModel([], [])
    tools = FakeTools()
    await ask(model, tools)

    assert tools.read[0] == "README.md"


async def test_extract_langgraph_draws_workflow_from_code():
    model = FakeModel([[call("extract_langgraph")]], [], answer="흐름 설명")
    tools = FakeTools()

    result = await ask(model, tools)

    assert len(result.diagrams) == 1
    diagram = result.diagrams[0]
    assert diagram.kind == "langgraph"
    edges = {(e.source, e.target) for e in diagram.edges}
    assert edges == {("START", "load"), ("load", "answer"), ("answer", "END")}
    assert result.grounded is True  # Read from code, even without citations.
    assert (
        "app/graph.py" in tools.searches[0] or "StateGraph" in tools.searches[0]
    )
    # The defining lines become citable and reach the answer and rewrite.
    assert "4: graph.add_node" in model.answer_context
    # Each extracted edge reaches both steps as verified, with its code line.
    edge = (
        "- load -> answer [확인됨] — app/graph.py:7 "
        '`graph.add_edge("load", "answer")`'
    )
    for context in (model.answer_context, model.revise_input):
        assert "그림 근거" in context
        assert "모든 연결이 코드로 확인됨" in context
        assert edge in context
    assert result.answer == "revised"


async def test_extract_services_reads_compose_file():
    model = FakeModel([[call("extract_services")]], [])

    result = await ask(model)

    diagram = result.diagrams[0]
    assert diagram.kind == "services"
    assert [(e.source, e.target) for e in diagram.edges] == [("api", "db")]


async def test_draw_diagram_marks_unread_evidence_unverified():
    model = FakeModel(
        [
            [call("read_file", path="app/auth.py", start_line=1, end_line=5)],
            [
                call(
                    "draw_diagram",
                    title="인증 흐름",
                    nodes=[
                        {
                            "id": "a",
                            "label": "Auth",
                            "path": "app/auth.py",
                            "start_line": 2,
                        },
                        {
                            "id": "b",
                            "label": "Guess",
                            "path": "app/auth.py",
                            "start_line": 15,
                        },
                    ],
                    edges=[
                        {"source": "a", "target": "b"},
                        {"source": "a", "target": "zzz"},
                    ],
                )
            ],
        ],
        [],
    )

    result = await ask(model)

    diagram = result.diagrams[0]
    assert [n.verified for n in diagram.nodes] == [True, False]
    assert len(diagram.edges) == 1 and diagram.edges[0].verified is False
    assert result.grounded is False  # Model diagrams alone do not count.


async def test_diagram_count_is_capped():
    model = FakeModel([[call("extract_services")] * 5], [])
    result = await ask(model)

    assert len(result.diagrams) == qa.MAX_DIAGRAMS
    assert "최대" in model.tool_results[-1]


def test_drop_weak_citations_removes_empty_and_long_ranges():
    files = {
        "app/config.py": numbered("app/config.py"),
        "app/auth.py": "\n".join(
            f"{n}: x" for n in range(1, qa.MAX_CITATION_LINES + 2)
        ),
    }
    citations = [
        evidence("app/config.py", 1, 2),  # "/**" and a blank line
        evidence("app/config.py", 3, 3),  # real code
        evidence("app/auth.py", 1, qa.MAX_CITATION_LINES + 1),
    ]

    kept = qa.drop_weak_citations(citations, files)

    assert kept == [evidence("app/config.py", 3, 3)]


def test_cited_text_returns_only_the_range():
    files = {"app/auth.py": numbered("app/auth.py")}
    text = qa.cited_text(files, evidence("app/auth.py", 2, 3))
    assert text == "2: auth line 2\n3: auth line 3"


async def test_check_support_treats_missing_verdicts_as_unsupported():
    class Partial:
        def with_structured_output(self, schema):
            del schema

            class Runner:
                async def ainvoke(self, messages, **_):
                    del messages
                    return qa.SupportCheck(
                        verdicts=[qa.Verdict(index=1, supports=True)]
                    )

            return Runner()

    files = {"app/auth.py": numbered("app/auth.py")}
    citations = [evidence("app/auth.py", 1, 1), evidence("app/auth.py", 2, 2)]
    kept = await qa.check_support(Partial(), citations, files, {})
    assert kept == [citations[1]]


@pytest.mark.parametrize("question", ["   ", "x" * (qa.MAX_QUESTION_CHARS + 1)])
async def test_rejects_empty_or_long_questions(question):
    with pytest.raises(ValueError):
        await ask(FakeModel([], []), question=question)


async def test_answer_is_rewritten_from_verified_evidence_only():
    model = FakeModel(
        [[call("read_file", path="app/auth.py")]],
        [evidence("app/auth.py", 2, 2, "ok"), evidence("app/auth.py", 6, 6)],
    )

    result = await ask(model)

    assert result.answer == "revised"
    assert "2: auth line 2" in model.revise_input
    assert "6: auth line 6" not in model.revise_input
    assert "auth.py에서 처리해요." in model.revise_input  # The draft.


async def test_checker_model_runs_check_and_rewrite():
    drafter = FakeModel(
        [[call("read_file", path="app/auth.py")]],
        [evidence("app/auth.py", 2, 2, "ok")],
    )
    checker = FakeModel([], [])

    result = await ask(drafter, checker=checker)

    assert result.answer == "revised"
    assert drafter.support_input is None and drafter.revise_input is None
    assert "주장: ok" in checker.support_input
    assert checker.revise_input is not None


def test_code_queries_drops_natural_language_searches():
    queries = ["환경 변수", "getenv|environ", "설정", "_KEY"]
    assert qa.code_queries(queries) == ["getenv|environ", "_KEY"]


def test_cache_version_tracks_both_models(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "small")
    monkeypatch.delenv(qa.CHECK_MODEL_ENV, raising=False)
    plain = qa.cache_version()
    monkeypatch.setenv(qa.CHECK_MODEL_ENV, "strong")

    assert plain == f"{qa.QA_VERSION}/small/small"
    assert qa.cache_version() == f"{qa.QA_VERSION}/small/strong"


def test_tool_schemas_are_valid_function_definitions():
    names = [tool["function"]["name"] for tool in qa.TOOLS]
    assert names == [
        "search_code",
        "read_file",
        "extract_langgraph",
        "extract_services",
        "draw_diagram",
    ]
    draw = qa.TOOLS[-1]["function"]["parameters"]
    assert draw["required"] == ["title", "nodes"]
    assert diagrams.DrawDiagramArgs.model_json_schema()["type"] == "object"


async def test_draw_diagram_repeating_extracted_diagram_is_skipped():
    model = FakeModel(
        [
            [call("extract_services")],
            [
                call(
                    "draw_diagram",
                    title="아키텍처",
                    nodes=[
                        {"id": "api", "label": "API"},
                        {"id": "database", "label": "db"},
                    ],
                )
            ],
        ],
        [],
    )

    result = await ask(model)

    assert [d.kind for d in result.diagrams] == ["services"]
    assert "다시 그리지 않았습니다" in model.tool_results[-1]


async def test_extract_langgraph_prefers_main_code_over_legacy():
    legacy = (
        'old = StateGraph(S)\nold.add_node("x", f)\nold.add_edge(START, "x")\n'
    )
    files = {"src/legacy/graph.py": legacy, **FILES}

    class Tools(FakeTools):
        async def get_repo_tree(self):
            return list(files)

        async def read_text(self, path):
            return files[path]

        async def search_code(self, query, *, paths=None, max_matches=40):
            pattern = re.compile(query)
            return [
                {"path": p, "line": n, "text": line}
                for p in paths
                for n, line in enumerate(files[p].splitlines(), 1)
                if pattern.search(line)
            ][:max_matches]

    model = FakeModel([[call("extract_langgraph")]], [])

    result = await ask(model, Tools())

    assert [d.title for d in result.diagrams] == [
        "LangGraph 워크플로우 (app/graph.py · graph)",
        "LangGraph 워크플로우 (src/legacy/graph.py · old)",
    ]


def test_diagram_evidence_marks_model_edges_without_evidence():
    diagram = diagrams.Diagram(
        title="t",
        kind="model",
        nodes=[
            diagrams.DiagramNode(id="a", label="A"),
            diagrams.DiagramNode(id="b", label="B"),
        ],
        edges=[diagrams.DiagramEdge(source="a", target="b", label="calls")],
    )

    text = qa.diagram_evidence([diagram], {})

    assert "AI가 그린 그림" in text
    assert "- A -> B (calls) [미확인]" in text
    assert qa.diagram_evidence([], {}) == ""
