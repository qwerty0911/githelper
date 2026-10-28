import pathlib

from app.agent import diagrams
from app.agent import report as report_lib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def edges_of(diagram):
    return {(e.source, e.target, e.label) for e in diagram.edges}


def test_extracts_this_projects_report_workflow():
    source = (ROOT / "app/agent/runner.py").read_text()

    found = diagrams.extract_langgraph(source, "app/agent/runner.py")
    assert len(found) == 1
    diagram = found[0]

    assert diagram.kind == "langgraph"
    assert [n.id for n in diagram.nodes] == [
        "START",
        "inspect_tree",
        "select_files",
        "read_selected",
        "deepen",
        "write_report",
        "END",
    ]
    assert edges_of(diagram) == {
        ("START", "inspect_tree", ""),
        ("inspect_tree", "select_files", ""),
        ("select_files", "read_selected", ""),
        ("read_selected", "deepen", ""),
        ("deepen", "deepen", "조건"),
        ("deepen", "write_report", "조건"),
        ("write_report", "END", ""),
    }
    assert all(n.verified for n in diagram.nodes)
    # Every non-terminal node cites the line that adds it.
    lines = source.splitlines()
    for node in diagram.nodes[1:-1]:
        assert "add_node" in lines[node.citation.start_line - 1]


def test_conditional_edges_with_path_map_and_router_function():
    source = """
from langgraph.graph import END, START, StateGraph

def route(state):
    if state["ok"]:
        return "tools"
    return END

g = StateGraph(dict)
g.add_node("agent", agent)
g.add_node(tools)
g.add_edge(START, "agent")
g.add_conditional_edges("agent", route)
g.add_conditional_edges("tools", pick, {"again": "agent", "stop": END})
g.add_edge(["agent", "tools"], "agent")
"""
    found = diagrams.extract_langgraph(source, "flow.py")
    assert len(found) == 1
    diagram = found[0]

    assert edges_of(diagram) == {
        ("START", "agent", ""),
        ("agent", "tools", "조건"),
        ("agent", "END", "조건"),
        ("tools", "agent", "again"),
        ("tools", "END", "stop"),
        ("agent", "agent", ""),
        ("tools", "agent", ""),
    }


def test_ignores_files_without_graphs_and_invalid_python():
    assert not diagrams.extract_langgraph("x = 1", "a.py")
    assert not diagrams.extract_langgraph("def (:", "a.py")


def test_separate_graph_variables_become_separate_diagrams():
    source = (
        'a.add_node("x", f)\na.add_edge("x", "x")\n'
        'b.add_node("y", f)\nb.set_finish_point("y")\n'
    )
    titles = [d.title for d in diagrams.extract_langgraph(source, "m.py")]
    assert titles == [
        "LangGraph 워크플로우 (m.py · a)",
        "LangGraph 워크플로우 (m.py · b)",
    ]


def test_extracts_this_projects_compose_services():
    text = (ROOT / "docker-compose.yml").read_text()

    diagram = diagrams.extract_services(text, "docker-compose.yml")

    assert [n.id for n in diagram.nodes] == ["mongo", "api", "worker"]
    assert {(e.source, e.target) for e in diagram.edges} == {
        ("api", "mongo"),
        ("worker", "mongo"),
    }
    assert diagram.nodes[0].label == "mongo (mongo:7 · port 27017:27017)"
    lines = text.splitlines()
    assert lines[diagram.nodes[1].citation.start_line - 1].strip() == "api:"


def test_compose_without_services_or_invalid_yaml_gives_nothing():
    assert diagrams.extract_services("version: '3'", "c.yml") is None
    assert diagrams.extract_services("services: [", "c.yml") is None


def test_mermaid_escapes_labels_and_uses_generated_ids():
    diagram = diagrams.Diagram(
        title="t",
        kind="model",
        nodes=[
            diagrams.DiagramNode(
                id='x"];click x call evil()//',
                label='A "quoted" <b>#1</b> `x` | y',
                verified=True,
            ),
            diagrams.DiagramNode(id="b", label="B\nnext line"),
        ],
        edges=[
            diagrams.DiagramEdge(
                source='x"];click x call evil()//',
                target="b",
                label='go"|',
            )
        ],
    )

    source = diagram.to_mermaid()

    assert "click" not in source and "evil" not in source
    assert '"' not in source.replace('["', "").replace('"]', "").replace(
        '|"', ""
    ).replace('"|', "")
    assert "<b>" not in source
    assert "#quot;quoted#quot;" in source
    assert 'n1["B next line"]' in source
    assert "n0 -.->" in source  # Unverified edge is dashed.
    assert "class n1 unverified" in source


def test_model_diagram_verifies_each_part_against_read_lines():
    files = {"a.py": "1: def login():\n2:     check()"}
    args = diagrams.DrawDiagramArgs(
        title="로그인",
        nodes=[
            diagrams.ProposedNode(
                id="login", label="login()", path="a.py", start_line=1
            ),
            diagrams.ProposedNode(
                id="db", label="DB", path="a.py", start_line=9
            ),
            diagrams.ProposedNode(id="login", label="duplicate"),
        ],
        edges=[
            diagrams.ProposedEdge(
                source="login", target="db", path="a.py", start_line=2
            ),
            diagrams.ProposedEdge(source="login", target="ghost"),
        ],
    )

    diagram = diagrams.build_model_diagram(args, files)

    assert [(n.id, n.verified) for n in diagram.nodes] == [
        ("login", True),
        ("db", False),
    ]
    assert diagram.nodes[0].citation == report_lib.Citation(
        path="a.py", start_line=1, end_line=1
    )
    assert [(e.target, e.verified) for e in diagram.edges] == [("db", True)]


def test_model_diagram_size_is_capped():
    args = diagrams.DrawDiagramArgs(
        title="big",
        nodes=[
            diagrams.ProposedNode(id=f"n{i}", label=str(i))
            for i in range(diagrams.MAX_NODES + 10)
        ],
    )
    assert len(diagrams.build_model_diagram(args, {}).nodes) == (
        diagrams.MAX_NODES
    )


def test_summary_marks_unverified_edges():
    diagram = diagrams.Diagram(
        title="t",
        kind="model",
        nodes=[
            diagrams.DiagramNode(id="a", label="A", verified=True),
            diagrams.DiagramNode(id="b", label="B"),
        ],
        edges=[diagrams.DiagramEdge(source="a", target="b", label="calls")],
    )
    assert diagrams.summarize(diagram) == (
        "[그림] t — 노드 2개 (근거 확인 1개). "
        "연결: A -> B (calls) [근거 미확인]"
    )


def test_model_diagram_repeating_an_extracted_one_is_a_duplicate():
    compose = diagrams.extract_services(
        (ROOT / "docker-compose.yml").read_text(), "docker-compose.yml"
    )

    def model(*labels):
        return diagrams.Diagram(
            title="m",
            kind="model",
            nodes=[
                diagrams.DiagramNode(id=label.lower(), label=label)
                for label in labels
            ],
        )

    assert diagrams.duplicates(model("MongoDB", "API", "Worker"), [compose])
    assert not diagrams.duplicates(
        model("main.py", "jobs.py", "qa.py", "GitHub"), [compose]
    )
    # Model diagrams never block each other.
    assert not diagrams.duplicates(model("A"), [model("A")])


def only_diagram(source, path="g.py"):
    found = diagrams.extract_langgraph(source, path)
    assert len(found) == 1
    return found[0]


def test_sentinel_strings_and_literal_return_types_are_understood():
    # Shape of langchain-ai/react-agent: "__start__" string and a router
    # that declares `-> Literal["__end__", "tools"]`.
    source = """
from typing import Literal

def route(state) -> Literal["__end__", "tools"]:
    if not state.calls:
        return "__end__"
    return "tools"

b = StateGraph(State)
b.add_node(call_model)
b.add_node("tools", ToolNode(TOOLS))
b.add_edge("__start__", "call_model")
b.add_conditional_edges("call_model", route)
b.add_edge("tools", "call_model")
"""
    assert edges_of(only_diagram(source)) == {
        ("START", "call_model", ""),
        ("call_model", "END", "조건"),
        ("call_model", "tools", "조건"),
        ("tools", "call_model", ""),
    }


def test_literal_return_type_adds_targets_the_body_never_returns():
    source = """
def route(state) -> Literal["reflect", "__end__"]:
    return "reflect"

w = StateGraph(S)
w.add_node(agent)
w.add_node(reflect)
w.add_conditional_edges("agent", route)
"""
    assert edges_of(only_diagram(source)) == {
        ("agent", "reflect", "조건"),
        ("agent", "END", "조건"),
    }


def test_command_routing_and_subgraph_nodes():
    # Shape of langchain-ai/open_deep_research: nodes jump with Command,
    # and a compiled graph is used as a node of the main graph.
    source = """
async def clarify(state) -> Command[Literal["brief", "__end__"]]:
    if state.ok:
        return Command(goto="brief")
    return Command(goto=END)

async def brief(state) -> Command[Literal["research"]]:
    return Command(goto="research")

def worker(state):
    return Command(goto=["worker_tools"])

sub = StateGraph(S)
sub.add_node("worker", worker)
sub.add_node("worker_tools", tools)
sub.add_edge(START, "worker")
research_graph = sub.compile()

main = StateGraph(S)
main.add_node("clarify", clarify)
main.add_node("brief", brief)
main.add_node("research", research_graph)
main.add_edge(START, "clarify")
main.add_edge("research", END)
"""
    first, second = diagrams.extract_langgraph(source, "dr.py")

    assert first.title.endswith("· main)")  # The graph using a subgraph.
    assert edges_of(first) == {
        ("START", "clarify", ""),
        ("research", "END", ""),
        ("clarify", "brief", "Command"),
        ("clarify", "END", "Command"),
        ("brief", "research", "Command"),
    }
    labels = {node.id: node.label for node in first.nodes}
    assert labels["research"] == "research (서브그래프: sub)"
    assert edges_of(second) == {
        ("START", "worker", ""),
        ("worker", "worker_tools", "Command"),
    }
    # Command edges cite the node function's definition line.
    lines = source.splitlines()
    command_edge = next(e for e in first.edges if e.label == "Command")
    assert "async def clarify" in lines[command_edge.citation.start_line - 1]


def test_graph_files_outside_legacy_and_examples_come_first():
    paths = [
        "src/legacy/graph.py",
        "examples/demo.py",
        "src/app/graph.py",
        "tests/test_graph.py",
        "src/app/sub.py",
    ]
    assert diagrams.rank_graph_files(paths) == [
        "src/app/graph.py",
        "src/app/sub.py",
        "src/legacy/graph.py",
        "examples/demo.py",
        "tests/test_graph.py",
    ]


def test_long_workflows_are_drawn_top_down():
    source = (ROOT / "app/agent/runner.py").read_text()
    long_graph = diagrams.extract_langgraph(source, "runner.py")[0]
    short_graph = only_diagram('g.add_node("a", f)\ng.add_edge(START, "a")\n')

    assert long_graph.direction == "TD"
    assert short_graph.direction == "LR"
