import base64
import json

import httpx
import pytest

from app.agent.github import GitHubTools, Repository, parse_repository_url, rank_path


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/owner/project", Repository("owner", "project")),
        ("https://github.com/owner/project.git", Repository("owner", "project")),
    ],
)
def test_parse_repository_url(url, expected):
    assert parse_repository_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com.evil.example/owner/project",
        "https://example.com/owner/project",
        "https://github.com/owner/project/issues",
        "https://github.com/owner/../project",
    ],
)
def test_parse_repository_url_rejects_non_repository_urls(url):
    with pytest.raises(ValueError):
        parse_repository_url(url)


def test_tree_filter_skips_generated_and_binary_files():
    assert GitHubTools._include_path("src/main.py", max_depth=8)
    assert not GitHubTools._include_path("node_modules/pkg/index.js", max_depth=8)
    assert not GitHubTools._include_path("assets/logo.png", max_depth=8)
    assert not GitHubTools._include_path("a/b/c/d.py", max_depth=3)


@pytest.mark.asyncio
async def test_read_file_rejects_traversal():
    tools = GitHubTools(Repository("owner", "project"))
    try:
        with pytest.raises(ValueError):
            await tools.read_file("../secret")
    finally:
        await tools.close()


def test_rank_path_puts_entrypoints_first_and_tests_last():
    paths = ["tests/test_a.py", "a/b/c/deep.py", "src/main.py", "README.md", "zzz.py", "aaa/x.py"]
    ranked = sorted(paths, key=rank_path)
    assert ranked[0] == "README.md"
    assert ranked[1] == "src/main.py"
    assert ranked[-1] == "tests/test_a.py"
    assert ranked.index("zzz.py") < ranked.index("aaa/x.py")


def _mock_github(files, tree_paths):
    def respond(request):
        path = request.url.path
        if path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "deadbeef"})
        if path.endswith("/git/trees/deadbeef"):
            return httpx.Response(
                200, json={"tree": [{"type": "blob", "path": p} for p in tree_paths]}
            )
        if path.endswith("/owner/project"):
            return httpx.Response(200, json={"default_branch": "main"})
        name = path.split("/contents/")[1]
        assert request.url.params["ref"] == "deadbeef"
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


@pytest.mark.asyncio
async def test_detect_stack_extracts_package_scripts():
    content = json.dumps(
        {
            "packageManager": "pnpm@9.0.0",
            "scripts": {"dev": "vite", "test": "vitest"},
        }
    )
    client = _mock_github({"package.json": content}, ["package.json"])
    tools = GitHubTools(Repository("owner", "project"), client=client)
    result = await tools.detect_stack(["package.json"])
    await client.aclose()

    assert "JavaScript / Node.js" in result["technologies"]
    assert "pnpm@9.0.0" in result["technologies"]
    assert result["commands"] == ["pnpm run dev", "pnpm run test"]


@pytest.mark.asyncio
async def test_tree_is_ranked_before_truncation_and_pins_commit_sha():
    paths = [f"aaa/file{i}.py" for i in range(5)] + ["README.md"]
    client = _mock_github({}, paths)
    tools = GitHubTools(Repository("owner", "project"), client=client)
    tree = await tools.get_repo_tree(max_files=2)
    await client.aclose()

    assert tree[0] == "README.md"
    assert len(tree) == 2
    assert tools.commit_sha == "deadbeef"


@pytest.mark.asyncio
async def test_detect_stack_survives_manifest_longer_than_evidence_limit():
    scripts = {f"script{i}": "x" * 50 for i in range(300)}
    content = json.dumps({"scripts": scripts})
    assert len(content) > 8_000
    client = _mock_github({"package.json": content}, ["package.json"])
    tools = GitHubTools(Repository("owner", "project"), client=client)
    result = await tools.detect_stack(["package.json"])
    await client.aclose()

    assert len(result["commands"]) == 300


@pytest.mark.asyncio
async def test_detect_stack_ignores_malformed_manifest():
    client = _mock_github({"package.json": "{not json"}, ["package.json"])
    tools = GitHubTools(Repository("owner", "project"), client=client)
    result = await tools.detect_stack(["package.json"])
    await client.aclose()

    assert result["commands"] == []
