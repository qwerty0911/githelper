from app.agent.budget import count_tokens, fit_lines
from app.agent.github import Repository
from app.agent.report import Citation, ProjectReport, ReportSection
from app.agent.runner import DeepenRequest, FileSelection, add_chunk, build_graph

FILES = {
    "README.md": "1: # demo\n2: run it",
    "src/main.py": "1: import util\n2: util.start()",
    "src/util.py": "\n".join(f"{n}: line {n}" for n in range(1, 41)),
}


class FakeTools:
    commit_sha = "abc123"

    def __init__(self):
        self.searches = []

    async def get_repo_tree(self):
        return list(FILES)

    async def detect_stack(self, tree):
        return {"technologies": ["Python"], "commands": []}

    async def read_file(self, path, *, start_line=1, end_line=None, max_chars=18_000):
        lines = FILES[path].splitlines()[start_line - 1 : end_line]
        return "\n".join(lines)[:max_chars]

    async def search_code(self, query, *, paths=None, max_matches=40):
        self.searches.append(query)
        return [{"path": "src/util.py", "line": 30, "text": "line 30"}]


class FakeModel:
    def __init__(self, deepen_answers):
        self.deepen_answers = list(deepen_answers)
        self.deepen_calls = 0

    def with_structured_output(self, schema):
        model = self

        class Runner:
            async def ainvoke(self, messages):
                if schema is FileSelection:
                    return FileSelection(paths=["README.md", "src/main.py", "not/in/tree.py"])
                if schema is DeepenRequest:
                    model.deepen_calls += 1
                    return model.deepen_answers.pop(0)
                return ProjectReport(
                    project_name="demo",
                    summary="s",
                    sections=[
                        ReportSection(
                            title="entry",
                            content="starts in main",
                            citations=[
                                Citation(path="src/main.py", start_line=1, end_line=2),
                                Citation(path="src/util.py", start_line=28, end_line=32),
                                Citation(path="src/util.py", start_line=1, end_line=2),
                            ],
                        )
                    ],
                )

        return Runner()


async def run_graph(model, tools=None):
    tools = tools or FakeTools()
    graph = build_graph(tools, model=model)
    return await graph.ainvoke({"repository": Repository("o", "r")}), tools


async def test_agent_stops_when_planner_says_done():
    model = FakeModel([DeepenRequest(done=True)])
    result, tools = await run_graph(model)

    assert model.deepen_calls == 1
    assert tools.searches == []
    assert result["commit_sha"] == "abc123"
    assert list(result["file_contents"]) == ["README.md", "src/main.py"]
    # util.py was never read, so its citations are dropped by verification
    assert result["report"].sections[0].citations == [
        Citation(path="src/main.py", start_line=1, end_line=2)
    ]


async def test_agent_deepens_with_search_and_citations_survive():
    model = FakeModel(
        [
            DeepenRequest(done=False, search_queries=["start", "("], extra_paths=["nope.py"]),
            DeepenRequest(done=True),
        ]
    )
    result, tools = await run_graph(model)

    assert model.deepen_calls == 2
    assert tools.searches == ["start", "("]
    assert "30: line 30" in result["file_contents"]["src/util.py"]
    paths = {c.path: (c.start_line, c.end_line) for c in result["report"].sections[0].citations}
    assert paths["src/util.py"] == (28, 32)


async def test_agent_stops_after_max_rounds():
    answers = [DeepenRequest(done=False, search_queries=["x"]) for _ in range(5)]
    model = FakeModel(answers)
    await run_graph(model)

    assert model.deepen_calls == 2


def test_add_chunk_skips_duplicate_lines_and_respects_budget():
    contents = {"a.py": "1: one\n2: two"}
    used = add_chunk(contents, "a.py", "2: two\n3: three", tokens_left=100)
    assert contents["a.py"] == "1: one\n2: two\n3: three"
    assert used > 0
    assert add_chunk(contents, "b.py", "1: " + "word " * 500, tokens_left=5) <= 5


def test_fit_lines_keeps_whole_lines():
    text = "\n".join(f"{n}: some line" for n in range(1, 100))
    fitted = fit_lines(text, 30)
    assert count_tokens(fitted) <= 30
    assert all(": " in line for line in fitted.splitlines())
