"""Diagrams extracted from code or proposed by the model.

Diagrams are kept as structured nodes and edges with evidence, never as
model-written Mermaid. Workflows and service maps that can be read straight
from code (LangGraph graphs, docker-compose files) are extracted
deterministically. Diagrams the model proposes are checked node by node
against lines the agent actually read, and unverified parts are drawn
dashed. `Diagram.to_mermaid()` escapes every label.
"""

import ast
import re
from typing import Literal

import pydantic
import yaml

from app.agent import report as report_lib

MAX_NODES = 25
MAX_EDGES = 40
MAX_LABEL_CHARS = 60
WIDE_GRAPH_NODES = 5
START = "START"
END = "END"
# Folders whose graphs are rarely the project's main workflow.
SECONDARY_PARTS = frozenset(
    {
        "legacy",
        "old",
        "deprecated",
        "archive",
        "examples",
        "example",
        "tests",
        "test",
        "docs",
        "notebooks",
    }
)
COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)
_GRAPH_METHODS = frozenset(
    {
        "add_node",
        "add_edge",
        "add_conditional_edges",
        "set_entry_point",
        "set_finish_point",
    }
)

Kind = Literal["langgraph", "services", "model"]
Direction = Literal["LR", "TD"]


class DiagramNode(pydantic.BaseModel):
    """A box in a diagram."""

    id: str
    label: str
    citation: report_lib.Citation | None = None
    verified: bool = False


class DiagramEdge(pydantic.BaseModel):
    """An arrow between two nodes."""

    source: str
    target: str
    label: str = ""
    citation: report_lib.Citation | None = None
    verified: bool = False


class Diagram(pydantic.BaseModel):
    """A diagram with evidence for its parts.

    Attributes:
        title: Shown above the diagram.
        kind: How the diagram was made: read from a LangGraph graph, from a
            compose file, or proposed by the model.
        direction: Mermaid flow direction.
        nodes: Boxes, in display order.
        edges: Arrows between node ids.
    """

    title: str
    kind: Kind
    direction: Direction = "LR"
    nodes: list[DiagramNode]
    edges: list[DiagramEdge] = pydantic.Field(default_factory=list)

    def to_mermaid(self) -> str:
        """Renders the diagram as Mermaid flowchart source.

        Node ids are replaced with generated ones and every label is
        escaped, so repository text can never inject Mermaid syntax.
        Unverified nodes and edges are drawn dashed.
        """
        ids = {node.id: f"n{index}" for index, node in enumerate(self.nodes)}
        lines = [f"flowchart {self.direction}"]
        for node in self.nodes:
            label = _escape(node.label)
            if node.id in (START, END):
                lines.append(f'  {ids[node.id]}(["{label}"])')
            else:
                lines.append(f'  {ids[node.id]}["{label}"]')
        for edge in self.edges:
            if edge.source not in ids or edge.target not in ids:
                continue
            arrow = "-->" if edge.verified else "-.->"
            label = f'|"{_escape(edge.label)}"|' if edge.label else ""
            lines.append(
                f"  {ids[edge.source]} {arrow}{label} {ids[edge.target]}"
            )
        unverified = [ids[node.id] for node in self.nodes if not node.verified]
        if unverified:
            lines.append("  classDef unverified stroke-dasharray: 5 5")
            lines.append(f"  class {','.join(unverified)} unverified")
        return "\n".join(lines)


def _escape(text: str) -> str:
    """Escapes a label for a quoted Mermaid string."""
    text = " ".join(str(text).split())[:MAX_LABEL_CHARS]
    replacements = {
        "#": "#35;",
        '"': "#quot;",
        "<": "#lt;",
        ">": "#gt;",
        "`": "#96;",
        "|": "#124;",
    }
    return "".join(replacements.get(char, char) for char in text)


def _line_citation(path: str, line: int) -> report_lib.Citation:
    return report_lib.Citation(path=path, start_line=line, end_line=line)


# --- LangGraph -------------------------------------------------------------


# LangGraph's START and END constants are these strings, and many projects
# write them literally.
_SENTINELS = {"__start__": START, "__end__": END}


def _name(node: ast.AST) -> str | None:
    """Returns the last part of a name such as `Literal` or `typing.Literal`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _endpoint(node: ast.expr) -> str | None:
    """Returns a node name from a call argument, or `None` if unknown."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _SENTINELS.get(node.value, node.value)
    name = _name(node)
    return name if name in (START, END) else None


def _string_constants(node: ast.AST) -> list[str]:
    """Returns string literals that a router may return, in source order."""
    found = []
    for child in ast.walk(node):
        if isinstance(child, (ast.Constant, ast.Name, ast.Attribute)):
            if (endpoint := _endpoint(child)) is not None:
                found.append(endpoint)
    return found


def _literal_strings(annotation: ast.AST | None, wrapper: str) -> list[str]:
    """Returns the strings of `Literal[...]` inside a `wrapper[...]` type.

    With `wrapper="Literal"` this reads `-> Literal["a", "b"]`; with
    `wrapper="Command"` it reads `-> Command[Literal["a", "__end__"]]`.
    """
    if annotation is None:
        return []
    found = []
    for node in ast.walk(annotation):
        if isinstance(node, ast.Subscript) and _name(node.value) == wrapper:
            for inner in ast.walk(node.slice if wrapper != "Literal" else node):
                if (
                    isinstance(inner, ast.Subscript)
                    and _name(inner.value) == "Literal"
                ):
                    found += _string_constants(inner.slice)
    return list(dict.fromkeys(found))


def _router_targets(
    call: ast.Call,
    functions: dict[str, ast.AST],
) -> list[tuple[str, str]]:
    """Returns (target, label) pairs for an `add_conditional_edges` call.

    An explicit path map wins. Otherwise targets come from an inline
    lambda, or from a router function in the same module: its
    `-> Literal[...]` return type plus the strings it returns.
    """
    path_map = call.args[2] if len(call.args) > 2 else None
    for keyword in call.keywords:
        if keyword.arg in ("path_map", "then"):
            path_map = keyword.value
    if isinstance(path_map, ast.Dict):
        pairs = []
        for key, value in zip(path_map.keys, path_map.values, strict=True):
            target = _endpoint(value)
            label = key.value if isinstance(key, ast.Constant) else ""
            if target:
                pairs.append((target, str(label)))
        return pairs
    if isinstance(path_map, (ast.List, ast.Tuple)):
        return [(t, "") for item in path_map.elts if (t := _endpoint(item))]
    router = call.args[1] if len(call.args) > 1 else None
    if isinstance(router, ast.Lambda):
        return [(target, "") for target in _string_constants(router.body)]
    if isinstance(router, ast.Name) and router.id in functions:
        function = functions[router.id]
        targets = _literal_strings(function.returns, "Literal")
        for child in ast.walk(function):
            if isinstance(child, ast.Return) and child.value is not None:
                targets += _string_constants(child.value)
        return [(target, "") for target in dict.fromkeys(targets)]
    return []


def _command_targets(function: ast.AST) -> list[str]:
    """Returns where a node can jump with `Command`.

    Reads the `-> Command[Literal[...]]` return type and every
    `Command(goto=...)` in the body.
    """
    targets = _literal_strings(function.returns, "Command")
    for child in ast.walk(function):
        if isinstance(child, ast.Call) and _name(child.func) == "Command":
            for keyword in child.keywords:
                if keyword.arg != "goto":
                    continue
                values = (
                    keyword.value.elts
                    if isinstance(keyword.value, (ast.List, ast.Tuple))
                    else [keyword.value]
                )
                targets += [t for v in values if (t := _endpoint(v))]
    return list(dict.fromkeys(targets))


def _compiled_graphs(module: ast.Module) -> dict[str, str]:
    """Maps `sub = builder.compile()` results to their builder variable."""
    compiled = {}
    for node in ast.walk(module):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and _name(node.value.func) == "compile"
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
        ):
            compiled[node.targets[0].id] = node.value.func.value.id
    return compiled


def _read_graphs(module: ast.Module) -> dict[str, dict]:
    """Collects nodes, edges and routes per graph variable, in line order."""
    functions = {
        node.name: node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    compiled = _compiled_graphs(module)
    graphs: dict[str, dict] = {}
    calls = sorted(
        (
            node
            for node in ast.walk(module)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _GRAPH_METHODS
            and isinstance(node.func.value, ast.Name)
        ),
        key=lambda node: node.lineno,
    )
    for call in calls:
        graph = graphs.setdefault(
            call.func.value.id,
            {"nodes": {}, "edges": [], "routes": [], "subgraphs": {}},
        )
        method, args, line = call.func.attr, call.args, call.lineno
        if method == "add_node" and args:
            name = _endpoint(args[0])
            runnable = args[1] if len(args) > 1 else None
            if name is None and isinstance(args[0], ast.Name):
                name, runnable = args[0].id, args[0]  # add_node(function)
            if not name:
                continue
            graph["nodes"].setdefault(name, line)
            target = runnable.id if isinstance(runnable, ast.Name) else None
            if target in compiled:
                graph["subgraphs"][name] = compiled[target]
            elif target in functions:
                jumps = _command_targets(functions[target])
                if jumps:
                    graph["routes"].append(
                        (
                            name,
                            [(t, "Command") for t in jumps],
                            functions[target].lineno,
                        )
                    )
        elif method == "add_edge" and len(args) >= 2:
            sources = (
                args[0].elts
                if isinstance(args[0], (ast.List, ast.Tuple))
                else [args[0]]
            )
            target = _endpoint(args[1])
            for item in sources:
                if (source_name := _endpoint(item)) and target:
                    graph["edges"].append((source_name, target, "", line))
        elif method == "set_entry_point" and args:
            if target := _endpoint(args[0]):
                graph["edges"].append((START, target, "", line))
        elif method == "set_finish_point" and args:
            if source_name := _endpoint(args[0]):
                graph["edges"].append((source_name, END, "", line))
        elif method == "add_conditional_edges" and args:
            if source_name := _endpoint(args[0]):
                graph["routes"].append(
                    (source_name, _router_targets(call, functions), line)
                )
    return graphs


def _graph_diagram(path: str, variable: str, graph: dict) -> Diagram:
    known = set(graph["nodes"]) | {START, END}
    edges = [
        DiagramEdge(
            source=source_name,
            target=target,
            label=label,
            citation=_line_citation(path, line),
            verified=True,
        )
        for source_name, target, label, line in graph["edges"]
        if source_name in known and target in known
    ]
    for source_name, targets, line in graph["routes"]:
        seen = set()
        for target, label in targets:
            if target in known and target not in seen:
                seen.add(target)
                edges.append(
                    DiagramEdge(
                        source=source_name,
                        target=target,
                        label=label or "조건",
                        citation=_line_citation(path, line),
                        verified=True,
                    )
                )
    used = {edge.source for edge in edges} | {edge.target for edge in edges}
    nodes = []
    if START in used:
        nodes.append(DiagramNode(id=START, label=START, verified=True))
    for name, line in graph["nodes"].items():
        subgraph = graph["subgraphs"].get(name)
        nodes.append(
            DiagramNode(
                id=name,
                label=f"{name} (서브그래프: {subgraph})" if subgraph else name,
                citation=_line_citation(path, line),
                verified=True,
            )
        )
    if END in used:
        nodes.append(DiagramNode(id=END, label=END, verified=True))
    return Diagram(
        title=f"LangGraph 워크플로우 ({path} · {variable})",
        kind="langgraph",
        # Long chains get too small left-to-right in the answer panel.
        direction="TD" if len(nodes) > WIDE_GRAPH_NODES else "LR",
        nodes=nodes[:MAX_NODES],
        edges=edges[:MAX_EDGES],
    )


def extract_langgraph(source: str, path: str) -> list[Diagram]:
    """Reads LangGraph `StateGraph` wiring from Python source.

    Supports `add_node`, `add_edge`, `add_conditional_edges` (path maps,
    lambdas, and router functions via their returns and `Literal` return
    types), `Command` routing (`-> Command[Literal[...]]` and
    `Command(goto=...)`), `set_entry_point`, `set_finish_point`, the
    `"__start__"`/`"__end__"` strings, and compiled subgraphs used as nodes.

    Args:
        source: Python source code.
        path: Repository path, used for citations.

    Returns:
        One diagram per graph variable that has nodes. Graphs that embed
        other graphs come first, since they are usually the main workflow.
        Every node and edge cites the line that defines it.
    """
    try:
        module = ast.parse(source)
    except SyntaxError:
        return []
    graphs = {
        variable: graph
        for variable, graph in _read_graphs(module).items()
        if graph["nodes"]
    }
    ordered = sorted(graphs, key=lambda v: not graphs[v]["subgraphs"])
    return [_graph_diagram(path, v, graphs[v]) for v in ordered]


def rank_graph_files(paths: list[str]) -> list[str]:
    """Orders files that define graphs so main code comes before the rest.

    Files under folders such as `legacy/` or `examples/` go last; the
    original order is kept otherwise.
    """
    return sorted(
        paths,
        key=lambda path: any(
            part.lower() in SECONDARY_PARTS for part in path.split("/")[:-1]
        ),
    )


# --- docker-compose --------------------------------------------------------


def extract_services(text: str, path: str) -> Diagram | None:
    """Reads services and `depends_on` links from a compose file.

    Args:
        text: Compose file contents.
        path: Repository path, used for citations.

    Returns:
        A diagram with one node per service and an arrow from each service
        to what it depends on, or `None` if the file has no services.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict) or not services:
        return None
    lines = text.splitlines()

    def line_of(name: str) -> int | None:
        pattern = re.compile(rf"^\s+{re.escape(str(name))}\s*:\s*$")
        for number, line in enumerate(lines, start=1):
            if pattern.match(line):
                return number
        return None

    nodes, edges = [], []
    for name, spec in list(services.items())[:MAX_NODES]:
        spec = spec if isinstance(spec, dict) else {}
        details = []
        if spec.get("image"):
            details.append(str(spec["image"]))
        elif spec.get("build") is not None:
            details.append("build")
        ports = spec.get("ports") or []
        if isinstance(ports, list) and ports:
            details.append(f"port {', '.join(str(p) for p in ports[:2])}")
        label = f"{name} ({' · '.join(details)})" if details else str(name)
        line = line_of(name)
        citation = _line_citation(path, line) if line else None
        nodes.append(
            DiagramNode(
                id=str(name),
                label=label,
                citation=citation,
                verified=citation is not None,
            )
        )
        depends = spec.get("depends_on") or []
        if isinstance(depends, dict):
            depends = list(depends)
        for dependency in depends if isinstance(depends, list) else []:
            edges.append(
                DiagramEdge(
                    source=str(name),
                    target=str(dependency),
                    label="depends_on",
                    citation=citation,
                    verified=citation is not None,
                )
            )
    known = {node.id for node in nodes}
    return Diagram(
        title=f"서비스 구성 ({path})",
        kind="services",
        direction="TD",
        nodes=nodes,
        edges=[e for e in edges if e.target in known][:MAX_EDGES],
    )


# --- Model-proposed diagrams -----------------------------------------------


class ProposedNode(pydantic.BaseModel):
    """A node the model wants to draw, with optional evidence."""

    id: str = pydantic.Field(description="짧은 영문 식별자")
    label: str = pydantic.Field(description="상자에 보일 이름")
    path: str | None = pydantic.Field(
        default=None, description="이 노드의 근거 파일 (읽은 파일만)"
    )
    start_line: int | None = None
    end_line: int | None = None


class ProposedEdge(pydantic.BaseModel):
    """An edge the model wants to draw, with optional evidence."""

    source: str = pydantic.Field(description="출발 노드 id")
    target: str = pydantic.Field(description="도착 노드 id")
    label: str = pydantic.Field(default="", description="화살표 설명")
    path: str | None = pydantic.Field(
        default=None, description="이 연결의 근거 파일 (예: 호출하는 줄)"
    )
    start_line: int | None = None
    end_line: int | None = None


class DrawDiagramArgs(pydantic.BaseModel):
    """Arguments of the `draw_diagram` tool."""

    title: str = pydantic.Field(description="그림 제목")
    direction: Direction = "LR"
    nodes: list[ProposedNode]
    edges: list[ProposedEdge] = pydantic.Field(default_factory=list)


def _verified_citation(
    path: str | None,
    start: int | None,
    end: int | None,
    files: dict[str, str],
) -> report_lib.Citation | None:
    if not path or start is None:
        return None
    try:
        citation = report_lib.Citation(
            path=path, start_line=start, end_line=end or start
        )
    except pydantic.ValidationError:
        return None
    verified = report_lib.filter_citations([citation], files)
    return verified[0] if verified else None


def build_model_diagram(
    args: DrawDiagramArgs, files: dict[str, str]
) -> Diagram:
    """Turns a model-proposed diagram into a checked one.

    Nodes and edges keep their citation only when it points at lines the
    agent read; the rest are marked unverified and drawn dashed. Edges to
    unknown nodes are dropped, and sizes are capped.

    Args:
        args: The `draw_diagram` tool arguments.
        files: Path to numbered content the agent read.

    Returns:
        The diagram to show.
    """
    nodes: list[DiagramNode] = []
    seen: set[str] = set()
    for proposed in args.nodes:
        if proposed.id in seen or len(nodes) >= MAX_NODES:
            continue
        seen.add(proposed.id)
        citation = _verified_citation(
            proposed.path, proposed.start_line, proposed.end_line, files
        )
        nodes.append(
            DiagramNode(
                id=proposed.id,
                label=proposed.label or proposed.id,
                citation=citation,
                verified=citation is not None,
            )
        )
    edges = []
    for proposed in args.edges:
        if proposed.source not in seen or proposed.target not in seen:
            continue
        citation = _verified_citation(
            proposed.path, proposed.start_line, proposed.end_line, files
        )
        edges.append(
            DiagramEdge(
                source=proposed.source,
                target=proposed.target,
                label=proposed.label,
                citation=citation,
                verified=citation is not None,
            )
        )
    return Diagram(
        title=args.title,
        kind="model",
        direction=args.direction,
        nodes=nodes,
        edges=edges[:MAX_EDGES],
    )


def _normalize(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", text.lower())


def duplicates(proposed: Diagram, existing: list[Diagram]) -> bool:
    """Tells whether a model diagram mostly repeats an extracted one.

    A node matches when its normalized id or label contains, or is
    contained in, an extracted node id (so "MongoDB" matches "mongo").

    Args:
        proposed: A diagram the model drew.
        existing: Diagrams made so far.

    Returns:
        True when at least two thirds of the proposed nodes match the nodes
        of one extracted diagram.
    """
    names = [
        {_normalize(node.id), _normalize(node.label)} - {""}
        for node in proposed.nodes
    ]
    for diagram in existing:
        if diagram.kind == "model":
            continue
        ids = {_normalize(node.id) for node in diagram.nodes} - {""}
        matched = sum(
            any(a in b or b in a for a in candidates for b in ids)
            for candidates in names
        )
        if names and matched * 3 >= len(names) * 2:
            return True
    return False


def summarize(diagram: Diagram) -> str:
    """Describes a diagram in text for the model and for answer rewriting."""
    verified_nodes = sum(node.verified for node in diagram.nodes)
    names = {node.id: node.label for node in diagram.nodes}
    edges = "; ".join(
        f"{names.get(e.source, e.source)} -> {names.get(e.target, e.target)}"
        + (f" ({e.label})" if e.label else "")
        + ("" if e.verified else " [근거 미확인]")
        for e in diagram.edges
    )
    return (
        f"[그림] {diagram.title} — 노드 {len(diagram.nodes)}개 "
        f"(근거 확인 {verified_nodes}개). 연결: {edges or '없음'}"
    )
