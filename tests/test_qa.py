import re

import pytest

from app.agent import github
from app.agent import qa

FILES = {
    "README.md": "1: # demo",
    "app/auth.py": "\n".join(f"{n}: auth line {n}" for n in range(1, 21)),
    "app/config.py": "1: /**\n2: \n3: TOKEN = os.getenv('TOKEN')\n4: }",
}


class FakeTools:
    def __init__(self):
        self.read = []
        self.search_paths = None

    async def get_repo_tree(self):
        return list(FILES)

    async def read_file(
        self, path, *, start_line=1, end_line=None, max_chars=18_000
    ):
        self.read.append(path)
        lines = FILES[path].splitlines()[start_line - 1 : end_line]
        return "\n".join(lines)[:max_chars]

    async def search_code(self, query, *, paths=None, max_matches=40):
        del query, max_matches
        self.search_paths = paths
        return [{"path": "app/auth.py", "line": 15, "text": "auth line 15"}]


def evidence(path, start, end, claim="claim"):
    return qa.Evidence(path=path, start_line=start, end_line=end, claim=claim)


class FakeModel:
    """Plans, answers, then marks citations whose claim is 'ok' supported."""

    def __init__(self, plan, citations):
        self.plan = plan
        self.citations = citations
        self.answer_context = None
        self.support_input = None
        self.revise_input = None

    def with_structured_output(self, schema):
        model = self

        class Runner:
            async def ainvoke(self, messages, **_):
                if schema is qa.QuestionPlan:
                    return model.plan
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
                return qa.Answer(
                    answer="auth.py에서 처리해요.", citations=model.citations
                )

        return Runner()


REPO = github.Repository("o", "r")


@pytest.fixture(autouse=True)
def no_real_checker(monkeypatch):
    # .env may set a check model; tests must never build a real one.
    monkeypatch.delenv(qa.CHECK_MODEL_ENV, raising=False)


async def test_keeps_only_citations_that_were_read_and_supported():
    model = FakeModel(
        qa.QuestionPlan(paths=["app/auth.py", "ghost.py"]),
        [
            evidence("app/auth.py", 2, 4, "ok"),
            evidence("ghost.py", 1, 1, "ok"),
            evidence("app/auth.py", 50, 60, "ok"),
            evidence("app/auth.py", 5, 5, "unrelated"),
        ],
    )
    tools = FakeTools()

    result = await qa.answer_question(
        REPO, "abc", "인증은 어디서?", model=model, tools=tools
    )

    assert result.grounded is True
    assert result.citations == [evidence("app/auth.py", 2, 4, "ok")]
    assert result.citations[0].claim == "ok"
    assert "ghost.py" not in tools.read


async def test_checker_sees_claim_and_exact_cited_lines():
    model = FakeModel(
        qa.QuestionPlan(paths=["app/auth.py"]),
        [evidence("app/auth.py", 3, 4, "ok")],
    )
    await qa.answer_question(REPO, "abc", "q", model=model, tools=FakeTools())

    assert "주장: ok" in model.support_input
    assert "3: auth line 3\n4: auth line 4" in model.support_input
    assert "5: auth line 5" not in model.support_input


async def test_answer_is_not_grounded_when_checker_rejects_everything():
    model = FakeModel(
        qa.QuestionPlan(paths=["app/auth.py"]),
        [evidence("app/auth.py", 1, 1, "unrelated")],
    )

    result = await qa.answer_question(
        REPO, "abc", "what?", model=model, tools=FakeTools()
    )

    assert result.grounded is False
    assert not result.citations


async def test_search_covers_more_of_the_tree_and_feeds_context():
    model = FakeModel(qa.QuestionPlan(search_queries=["auth"]), [])
    tools = FakeTools()
    await qa.answer_question(REPO, "abc", "auth?", model=model, tools=tools)

    assert "15: auth line 15" in model.answer_context
    assert tools.search_paths == list(FILES)[: qa.QA_SEARCH_CANDIDATES]


async def test_empty_plan_falls_back_to_top_files():
    model = FakeModel(qa.QuestionPlan(), [])
    tools = FakeTools()
    await qa.answer_question(REPO, "abc", "overview?", model=model, tools=tools)

    assert tools.read[0] == "README.md"


def test_drop_weak_citations_removes_empty_and_long_ranges():
    files = {
        "app/config.py": FILES["app/config.py"],
        "app/auth.py": FILES["app/auth.py"],
    }
    citations = [
        evidence("app/config.py", 1, 2),  # "/**" and a blank line
        evidence("app/config.py", 3, 3),  # real code
        evidence("app/auth.py", 1, qa.MAX_CITATION_LINES + 1),
    ]
    files["app/auth.py"] = "\n".join(
        f"{n}: x" for n in range(1, qa.MAX_CITATION_LINES + 2)
    )

    kept = qa.drop_weak_citations(citations, files)

    assert kept == [evidence("app/config.py", 3, 3)]


def test_cited_text_returns_only_the_range():
    text = qa.cited_text(FILES, evidence("app/auth.py", 2, 3))
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

    citations = [evidence("app/auth.py", 1, 1), evidence("app/auth.py", 2, 2)]
    kept = await qa.check_support(Partial(), citations, FILES, {})
    assert kept == [citations[1]]


@pytest.mark.parametrize("question", ["   ", "x" * (qa.MAX_QUESTION_CHARS + 1)])
async def test_rejects_empty_or_long_questions(question):
    with pytest.raises(ValueError):
        await qa.answer_question(
            REPO, "abc", question, model=FakeModel(None, []), tools=FakeTools()
        )


async def test_answer_is_rewritten_from_verified_evidence_only():
    model = FakeModel(
        qa.QuestionPlan(paths=["app/auth.py"]),
        [evidence("app/auth.py", 2, 2, "ok"), evidence("app/auth.py", 6, 6)],
    )

    result = await qa.answer_question(
        REPO, "abc", "q", model=model, tools=FakeTools()
    )

    assert result.answer == "revised"
    assert "2: auth line 2" in model.revise_input
    assert "6: auth line 6" not in model.revise_input
    assert "auth.py에서 처리해요." in model.revise_input  # The draft.


async def test_unsupported_answer_is_kept_but_not_rewritten():
    model = FakeModel(
        qa.QuestionPlan(paths=["app/auth.py"]),
        [evidence("app/auth.py", 2, 2, "nope")],
    )

    result = await qa.answer_question(
        REPO, "abc", "q", model=model, tools=FakeTools()
    )

    assert result.answer == "auth.py에서 처리해요."
    assert model.revise_input is None
    assert result.grounded is False


def test_code_queries_drops_natural_language_searches():
    queries = ["환경 변수", "getenv|environ", "설정", "_KEY"]
    assert qa.code_queries(queries) == ["getenv|environ", "_KEY"]


async def test_checker_model_runs_check_and_rewrite():
    drafter = FakeModel(
        qa.QuestionPlan(paths=["app/auth.py"]),
        [evidence("app/auth.py", 2, 2, "ok")],
    )
    checker = FakeModel(None, [])

    result = await qa.answer_question(
        REPO, "abc", "q", model=drafter, checker=checker, tools=FakeTools()
    )

    assert result.answer == "revised"
    assert drafter.support_input is None and drafter.revise_input is None
    assert "주장: ok" in checker.support_input
    assert checker.revise_input is not None


def test_cache_version_tracks_both_models(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "small")
    monkeypatch.delenv(qa.CHECK_MODEL_ENV, raising=False)
    plain = qa.cache_version()
    monkeypatch.setenv(qa.CHECK_MODEL_ENV, "strong")

    assert plain == f"{qa.QA_VERSION}/small/small"
    assert qa.cache_version() == f"{qa.QA_VERSION}/small/strong"
