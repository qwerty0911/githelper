import pytest

from app.agent import github
from app.agent import qa
from app.agent import report as report_lib

FILES = {
    "README.md": "1: # demo",
    "app/auth.py": "\n".join(f"{n}: auth line {n}" for n in range(1, 21)),
}


class FakeTools:
    def __init__(self):
        self.read = []

    async def get_repo_tree(self):
        return list(FILES)

    async def read_file(
        self, path, *, start_line=1, end_line=None, max_chars=18_000
    ):
        self.read.append(path)
        lines = FILES[path].splitlines()[start_line - 1 : end_line]
        return "\n".join(lines)[:max_chars]

    async def search_code(self, query, *, paths=None, max_matches=40):
        del query, paths, max_matches
        return [{"path": "app/auth.py", "line": 15, "text": "auth line 15"}]


class FakeModel:
    def __init__(self, plan, citations):
        self.plan = plan
        self.citations = citations
        self.answer_context = None

    def with_structured_output(self, schema):
        model = self

        class Runner:
            async def ainvoke(self, messages, **_):
                if schema is qa.QuestionPlan:
                    return model.plan
                model.answer_context = messages[1][1]
                return qa.Answer(
                    answer="auth.py에서 처리해요.", citations=model.citations
                )

        return Runner()


REPO = github.Repository("o", "r")


async def test_answer_keeps_only_citations_to_read_lines():
    model = FakeModel(
        qa.QuestionPlan(paths=["app/auth.py", "ghost.py"]),
        [
            report_lib.Citation(path="app/auth.py", start_line=2, end_line=4),
            report_lib.Citation(path="ghost.py", start_line=1, end_line=1),
            report_lib.Citation(path="app/auth.py", start_line=50, end_line=60),
        ],
    )
    tools = FakeTools()

    result = await qa.answer_question(
        REPO, "abc", "인증은 어디서?", model=model, tools=tools
    )

    assert result.grounded is True
    assert result.citations == [
        report_lib.Citation(path="app/auth.py", start_line=2, end_line=4)
    ]
    assert "ghost.py" not in tools.read


async def test_answer_without_valid_citations_is_not_grounded():
    model = FakeModel(
        qa.QuestionPlan(paths=["README.md"]),
        [report_lib.Citation(path="nope.py", start_line=1, end_line=1)],
    )

    result = await qa.answer_question(
        REPO, "abc", "what?", model=model, tools=FakeTools()
    )

    assert result.grounded is False
    assert result.citations == []


async def test_search_results_are_read_into_context():
    model = FakeModel(qa.QuestionPlan(search_queries=["auth"]), [])
    await qa.answer_question(
        REPO, "abc", "auth?", model=model, tools=FakeTools()
    )

    assert "15: auth line 15" in model.answer_context


async def test_empty_plan_falls_back_to_top_files():
    model = FakeModel(qa.QuestionPlan(), [])
    tools = FakeTools()
    await qa.answer_question(REPO, "abc", "overview?", model=model, tools=tools)

    assert tools.read[0] == "README.md"


@pytest.mark.parametrize("question", ["   ", "x" * (qa.MAX_QUESTION_CHARS + 1)])
async def test_rejects_empty_or_long_questions(question):
    with pytest.raises(ValueError):
        await qa.answer_question(
            REPO, "abc", question, model=FakeModel(None, []), tools=FakeTools()
        )
