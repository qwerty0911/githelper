import base64
import json

import httpx
import pytest

from app.agent import github


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/owner/project",
            github.Repository("owner", "project"),
        ),
        (
            "https://github.com/owner/project.git",
            github.Repository("owner", "project"),
        ),
        (
            "https://github.com/owner/project/tree/main",
            github.Repository("owner", "project", "main"),
        ),
        (
            "https://github.com/owner/project/tree/feature/x",
            github.Repository("owner", "project", "feature/x"),
        ),
    ],
)
def test_parse_repository_url(url, expected):
    assert github.parse_repository_url(url) == expected


def test_explicit_branch_wins_over_url_branch():
    repository = github.parse_repository_url(
        "https://github.com/owner/project/tree/main", "dev"
    )
    assert repository.branch == "dev"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com.evil.example/owner/project",
        "https://example.com/owner/project",
        "https://github.com/owner/project/issues",
        "https://github.com/owner/project/tree",
        "https://github.com/owner/../project",
    ],
)
def test_parse_repository_url_rejects_non_repository_urls(url):
    with pytest.raises(ValueError):
        github.parse_repository_url(url)


def test_blob_url_links_to_pinned_commit_and_lines():
    assert github.blob_url("o/r", "abc", "src/a b.py", 3, 7) == (
        "https://github.com/o/r/blob/abc/src/a%20b.py#L3-L7"
    )
    assert github.blob_url("o/r", "abc", "a.py", 3, 3).endswith("#L3")
    assert github.blob_url("o/r", None, "a.py") == (
        "https://github.com/o/r/blob/HEAD/a.py"
    )


def test_tree_filter_skips_generated_and_binary_files():
    include = github.GitHubTools._include_path  # pylint: disable=protected-access
    assert include("src/main.py", max_depth=8)
    assert not include("node_modules/pkg/index.js", max_depth=8)
    assert not include("assets/logo.png", max_depth=8)
    assert not include("a/b/c/d.py", max_depth=3)


async def test_read_file_rejects_traversal():
    tools = github.GitHubTools(github.Repository("owner", "project"))
    try:
        with pytest.raises(ValueError):
            await tools.read_file("../secret")
    finally:
        await tools.close()


def test_rank_path_puts_entrypoints_first_and_tests_last():
    paths = [
        "tests/test_a.py",
        "a/b/c/deep.py",
        "src/main.py",
        "README.md",
        "zzz.py",
        "aaa/x.py",
    ]
    ranked = sorted(paths, key=github.rank_path)
    assert ranked[0] == "README.md"
    assert ranked[1] == "src/main.py"
    assert ranked[-1] == "tests/test_a.py"
    assert ranked.index("zzz.py") < ranked.index("aaa/x.py")


def _mock_github(files, tree_paths, *, status_codes=None):
    status_codes = status_codes or {}

    def respond(request):
        path = request.url.path
        if path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "deadbeef"})
        if path.endswith("/git/trees/deadbeef"):
            return httpx.Response(
                200,
                json={
                    "tree": [{"type": "blob", "path": p} for p in tree_paths]
                },
            )
        if path.endswith("/owner/project"):
            return httpx.Response(200, json={"default_branch": "main"})
        name = path.split("/contents/")[1]
        assert request.url.params["ref"] == "deadbeef"
        if name in status_codes:
            return httpx.Response(status_codes[name])
        content = files[name]
        return httpx.Response(
            200,
            json={
                "type": "file",
                "size": len(content),
                "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode(),
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


def _tools(client, **kwargs):
    return github.GitHubTools(
        github.Repository("owner", "project"), client=client, **kwargs
    )


async def test_detect_stack_extracts_package_scripts():
    content = json.dumps(
        {
            "packageManager": "pnpm@9.0.0",
            "scripts": {"dev": "vite", "test": "vitest"},
        }
    )
    client = _mock_github({"package.json": content}, ["package.json"])
    result = await _tools(client).detect_stack(["package.json"])
    await client.aclose()

    assert "JavaScript / Node.js" in result["technologies"]
    assert "pnpm@9.0.0" in result["technologies"]
    assert result["commands"] == ["pnpm run dev", "pnpm run test"]


async def test_detect_stack_ignores_nested_manifests():
    client = _mock_github({}, [])
    result = await _tools(client).detect_stack(["web/package.json"])
    await client.aclose()

    assert result["files"] == []


async def test_tree_is_ranked_before_truncation_and_pins_commit_sha():
    paths = [f"aaa/file{i}.py" for i in range(5)] + ["README.md"]
    client = _mock_github({}, paths)
    tools = _tools(client)
    tree = await tools.get_repo_tree(max_files=2)
    await client.aclose()

    assert tree[0] == "README.md"
    assert len(tree) == 2
    assert tools.commit_sha == "deadbeef"


async def test_given_commit_sha_skips_branch_resolution():
    def respond(request):
        assert "/commits/" not in request.url.path
        return httpx.Response(200, json={"tree": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    tools = _tools(client, commit_sha="deadbeef")
    assert await tools.resolve_ref() == "deadbeef"
    assert await tools.get_repo_tree() == []
    await client.aclose()


async def test_detect_stack_survives_manifest_longer_than_evidence_limit():
    scripts = {f"script{i}": "x" * 50 for i in range(300)}
    content = json.dumps({"scripts": scripts})
    assert len(content) > 8_000
    client = _mock_github({"package.json": content}, ["package.json"])
    result = await _tools(client).detect_stack(["package.json"])
    await client.aclose()

    assert len(result["commands"]) == 300


async def test_detect_stack_ignores_malformed_manifest():
    client = _mock_github({"package.json": "{not json"}, ["package.json"])
    result = await _tools(client).detect_stack(["package.json"])
    await client.aclose()

    assert result["commands"] == []


async def test_search_code_keeps_path_order_and_skips_unreadable_files():
    files = {f"f{i}.py": f"x = {i}\ndef target():\n    pass" for i in range(12)}
    client = _mock_github(files, list(files), status_codes={"f3.py": 404})
    matches = await _tools(client).search_code(r"def target", paths=list(files))
    await client.aclose()

    assert [m["path"] for m in matches] == [
        f"f{i}.py" for i in range(12) if i != 3
    ]
    assert all(m["line"] == 2 for m in matches)


async def test_rate_limit_is_reported_clearly():
    client = _mock_github({}, [], status_codes={"a.py": 429})
    with pytest.raises(github.GitHubError, match="rate limit"):
        await _tools(client).read_file("a.py")
    await client.aclose()
