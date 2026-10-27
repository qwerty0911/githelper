from app.agent import budget
from app.agent import github
from app.agent import report as report_lib
from app.agent import runner
from app.agent import tracing

Citation = report_lib.Citation

FILES = {
    "README.md": "1: # demo\n2: run it with `python -m demo`",
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
        del tree
        return {"technologies": ["Python"], "commands": []}

    async def read_file(
        self, path, *, start_line=1, end_line=None, max_chars=18_000
    ):
        lines = FILES[path].splitlines()[start_line - 1 : end_line]
        return "\n".join(lines)[:max_chars]

    async def search_code(self, query, *, paths=None, max_matches=40):
        del paths, max_matches
        self.searches.append(query)
        return [{"path": "src/util.py", "line": 30, "text": "line 30"}]


class FakeModel:
    def __init__(self, deepen_answers):
        self.deepen_answers = list(deepen_answers)
        self.deepen_calls = 0
        self.prompts = []

    def with_structured_output(self, schema):
        model = self

        class Runner:
            async def ainvoke(self, messages, **_):
                model.prompts.append(messages[0][1])
                if schema is runner.FileSelection:
                    return runner.FileSelection(
                        paths=["README.md", "src/main.py", "not/in/tree.py"]
                    )
                if schema is runner.DeepenRequest:
                    model.deepen_calls += 1
                    return model.deepen_answers.pop(0)
                return report_lib.ProjectReport(
                    project_name="demo",
                    summary="s",
                    quick_start=["python -m demo", "rm -rf /"],
                    reading_order=[
                        report_lib.ReadingStep(path="src/main.py", reason="a"),
                        report_lib.ReadingStep(path="nope.py", reason="b"),
                    ],
                    sections=[
                        report_lib.ReportSection(
                            title="entry",
                            content="starts in main",
                            citations=[
                                Citation(
                                    path="src/main.py", start_line=1, end_line=2
                                ),
                                Citation(
                                    path="src/util.py",
                                    start_line=28,
                                    end_line=32,
                                ),
                                Citation(
                                    path="src/util.py", start_line=1, end_line=2
                                ),
                            ],
                        )
                    ],
                )

        return Runner()


async def run_graph(model, tools=None, level="beginner"):
    tools = tools or FakeTools()
    graph = runner.build_graph(tools, model=model)
    repository = github.Repository("o", "r")
    result = await graph.ainvoke({"repository": repository, "level": level})
    return result, tools


async def test_agent_stops_when_planner_says_done():
    model = FakeModel([runner.DeepenRequest(done=True)])
    result, tools = await run_graph(model)

    assert model.deepen_calls == 1
    assert tools.searches == []
    assert result["commit_sha"] == "abc123"
    assert list(result["file_contents"]) == ["README.md", "src/main.py"]
    # util.py was never read, so its citations are dropped by verification.
    assert result["report"].sections[0].citations == [
        Citation(path="src/main.py", start_line=1, end_line=2)
    ]


async def test_report_extras_are_grounded_and_linked():
    model = FakeModel([runner.DeepenRequest(done=True)])
    result, _ = await run_graph(model)

    report = result["report"]
    assert report.quick_start == ["python -m demo"]
    assert [step.path for step in report.reading_order] == ["src/main.py"]
    assert (
        "https://github.com/o/r/blob/abc123/src/main.py#L1-L2" in result["html"]
    )


async def test_reader_level_changes_the_report_prompt():
    beginner = FakeModel([runner.DeepenRequest(done=True)])
    senior = FakeModel([runner.DeepenRequest(done=True)])
    await run_graph(beginner, level="beginner")
    await run_graph(senior, level="senior")

    assert "입문자" in beginner.prompts[-1]
    assert "숙련 개발자" in senior.prompts[-1]


async def test_agent_deepens_with_search_and_citations_survive():
    model = FakeModel(
        [
            runner.DeepenRequest(
                done=False,
                search_queries=["start", "("],
                extra_paths=["nope.py"],
            ),
            runner.DeepenRequest(done=True),
        ]
    )
    result, tools = await run_graph(model)

    assert model.deepen_calls == 2
    assert tools.searches == ["start", "("]
    assert "30: line 30" in result["file_contents"]["src/util.py"]
    citations = result["report"].sections[0].citations
    paths = {c.path: (c.start_line, c.end_line) for c in citations}
    assert paths["src/util.py"] == (28, 32)


async def test_agent_stops_after_max_rounds():
    answers = [
        runner.DeepenRequest(done=False, search_queries=["x"]) for _ in range(5)
    ]
    model = FakeModel(answers)
    await run_graph(model)

    assert model.deepen_calls == runner.MAX_DEEPEN_ROUNDS


def test_add_chunk_skips_duplicate_lines_and_respects_budget():
    contents = {"a.py": "1: one\n2: two"}
    used = budget.add_chunk(
        contents, "a.py", "2: two\n3: three", 100, max_file_tokens=100
    )
    assert contents["a.py"] == "1: one\n2: two\n3: three"
    assert used > 0
    long_text = "1: " + "word " * 500
    assert (
        budget.add_chunk(contents, "b.py", long_text, 5, max_file_tokens=100)
        <= 5
    )


def test_fit_lines_keeps_whole_lines():
    text = "\n".join(f"{n}: some line" for n in range(1, 100))
    fitted = budget.fit_lines(text, 30)
    assert budget.count_tokens(fitted) <= 30
    assert all(": " in line for line in fitted.splitlines())


def test_tracing_config_enables_langfuse_only_with_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    config = tracing.tracing_config(repo="o/r", branch=None)
    assert not config["callbacks"]
    assert config["metadata"] == {"repo": "o/r", "branch": "default"}

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:1")
    config = tracing.tracing_config(repo="o/r", branch="main", run_name="q")
    assert len(config["callbacks"]) == 1
    assert config["run_name"] == "q"
