"""Read-only tools that explore a public GitHub repository.

Every read is pinned to one commit so the whole analysis sees a single
snapshot. Repository content is treated as data and is never executed.
"""

import asyncio
import base64
import dataclasses
import fnmatch
import json
import os
import pathlib
import re
import tomllib
from typing import Any
from urllib import parse

import httpx

GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"
IGNORED_PARTS = frozenset(
    {
        ".git",
        ".github",
        ".next",
        ".nuxt",
        ".venv",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "vendor",
    }
)
IGNORED_SUFFIXES = (
    ".7z",
    ".class",
    ".dll",
    ".exe",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".lock",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".svg",
    ".woff",
    ".woff2",
    ".zip",
)
MAX_FILE_BYTES = 160_000
PRIORITY_NAMES = {
    "readme.md": 0,
    "package.json": 0,
    "pyproject.toml": 0,
    "dockerfile": 1,
    "docker-compose.yml": 1,
    "main.py": 1,
    "app.py": 1,
    "index.js": 1,
    "index.ts": 1,
    "main.ts": 1,
    "main.go": 1,
    "app.tsx": 1,
    "main.tsx": 1,
}
LOW_PRIORITY_PARTS = frozenset(
    {
        "test",
        "tests",
        "__tests__",
        "spec",
        "docs",
        "examples",
        "fixtures",
    }
)
STACK_FILES = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "Dockerfile",
        "docker-compose.yml",
        "go.mod",
        "Cargo.toml",
        "pom.xml",
        "build.gradle",
        "Makefile",
    }
)
FIXED_STACK_COMMANDS = {
    "requirements.txt": ("Python", "pip install -r requirements.txt"),
    "docker-compose.yml": ("Docker Compose", "docker compose up"),
    "go.mod": ("Go", "go run ."),
    "Cargo.toml": ("Rust", "cargo run"),
    "pom.xml": ("Java / Maven", "mvn spring-boot:run"),
    "build.gradle": ("Java / Gradle", "./gradlew run"),
}
SEARCH_CONCURRENCY = 8
_LINE_NUMBER = re.compile(r"^(\d+): ")


class GitHubError(Exception):
    """A GitHub request failed in a way the user should be told about."""


@dataclasses.dataclass(frozen=True)
class Repository:
    """A public GitHub repository and an optional branch to analyze."""

    owner: str
    name: str
    branch: str | None = None

    @property
    def full_name(self) -> str:
        """Returns the repository name in `owner/name` form."""
        return f"{self.owner}/{self.name}"


def rank_path(path: str) -> tuple[int, int, str]:
    """Returns a sort key that puts the most useful files first.

    README files, manifests and entrypoints come first, then shallow paths.
    Tests, docs and examples come last.

    Args:
        path: A repository-relative file path.

    Returns:
        A tuple usable as a `sorted` key.
    """
    parts = path.lower().split("/")
    name_rank = PRIORITY_NAMES.get(parts[-1], 2)
    if name_rank == 2 and any(p in LOW_PRIORITY_PARTS for p in parts[:-1]):
        name_rank = 3
    return (name_rank, len(parts), path)


def parse_repository_url(url: str, branch: str | None = None) -> Repository:
    """Parses a github.com repository URL.

    Accepts `https://github.com/owner/repo`, an optional `.git` suffix and
    the `/tree/<branch>` form that browsers show. An explicit `branch`
    argument wins over a branch found in the URL.

    Args:
        url: The URL the user entered.
        branch: Optional branch name.

    Returns:
        The parsed repository.

    Raises:
        ValueError: The URL does not point to a github.com repository.
    """
    parsed = parse.urlparse(url.strip())
    if parsed.scheme not in {"https", "http"} or parsed.hostname not in {
        "github.com",
        "www.github.com",
    }:
        raise ValueError("URL must point to a public github.com repository.")

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    url_branch = None
    if len(parts) > 3 and parts[2] == "tree":
        url_branch = "/".join(parts[3:])
        parts = parts[:2]
    if len(parts) != 2 or any(part in {".", ".."} for part in parts):
        raise ValueError(
            "Use a repository URL in the form https://github.com/owner/repo."
        )

    owner, name = parts
    name = name.removesuffix(".git")
    if not owner or not name:
        raise ValueError("Both repository owner and name are required.")
    return Repository(owner=owner, name=name, branch=branch or url_branch)


def blob_url(
    repo: str,
    commit_sha: str | None,
    path: str,
    start: int | None = None,
    end: int | None = None,
) -> str:
    """Builds a github.com link to a file or line range at a pinned commit.

    Args:
        repo: Repository in `owner/name` form.
        commit_sha: Commit to link to. Falls back to `HEAD` when unknown.
        path: Repository-relative file path.
        start: Optional first line of the range.
        end: Optional last line of the range. Defaults to `start`.

    Returns:
        A URL such as `https://github.com/o/r/blob/<sha>/a.py#L1-L5`.
    """
    anchor = ""
    if start is not None:
        end = end or start
        anchor = f"#L{start}" if start == end else f"#L{start}-L{end}"
    return (
        f"{GITHUB_WEB}/{repo}/blob/{commit_sha or 'HEAD'}/"
        f"{parse.quote(path)}{anchor}"
    )


def paths_matching(pattern: str, paths: list[str]) -> list[str]:
    """Returns the paths that match a shell-style glob pattern."""
    return [path for path in paths if fnmatch.fnmatch(path, pattern)]


def numbered_line_set(numbered: str) -> set[int]:
    """Returns the line numbers present in `N: text` formatted content."""
    return {
        int(match.group(1))
        for line in numbered.splitlines()
        if (match := _LINE_NUMBER.match(line))
    }


def _check_path(path: str) -> None:
    if path.startswith("/") or ".." in path.split("/"):
        raise ValueError("File path must stay within the repository.")


class GitHubTools:
    """GitHub REST API helpers used by the agent.

    Attributes:
        repository: The repository being analyzed.
        branch: The branch being analyzed, resolved to the default branch on
            first use when not given.
        commit_sha: The pinned commit, resolved on first use when not given.
        client: The HTTP client used for GitHub requests.
    """

    def __init__(
        self,
        repository: Repository,
        client: httpx.AsyncClient | None = None,
        *,
        commit_sha: str | None = None,
    ) -> None:
        """Initializes the tools.

        Args:
            repository: Repository to read.
            client: Optional HTTP client, mainly for tests. When omitted a
                client is created and closed by `close()`.
            commit_sha: Optional commit to pin to instead of resolving the
                branch head.
        """
        self.repository = repository
        self.branch: str | None = repository.branch
        self.commit_sha: str | None = commit_sha
        self._text_cache: dict[str, str] = {}
        self._owns_client = client is None
        headers = {"Accept": "application/vnd.github+json"}
        token = os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = client or httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(20.0),
            follow_redirects=False,
        )

    async def close(self) -> None:
        """Closes the HTTP client if this object created it."""
        if self._owns_client:
            await self.client.aclose()

    @property
    def _repo_url(self) -> str:
        return (
            f"{GITHUB_API}/repos/{parse.quote(self.repository.owner)}/"
            f"{parse.quote(self.repository.name)}"
        )

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        not_found: str = (
            "Repository or requested file was not found or is not public."
        ),
    ) -> dict[str, Any]:
        response = await self.client.get(url, params=params)
        if response.status_code == 404:
            raise GitHubError(not_found)
        if response.status_code in (403, 429):
            raise GitHubError(
                "GitHub API rate limit reached. Set GITHUB_TOKEN and try again."
            )
        if response.is_error:
            raise GitHubError(
                f"GitHub API returned HTTP {response.status_code}."
            )
        return response.json()

    async def resolve_ref(self) -> str:
        """Pins the analysis to one commit.

        Returns:
            The commit SHA every later read uses.

        Raises:
            GitHubError: The repository or branch cannot be read.
        """
        if self.commit_sha:
            return self.commit_sha
        if self.branch is None:
            repo = await self._get_json(self._repo_url)
            self.branch = repo["default_branch"]
        commit = await self._get_json(
            f"{self._repo_url}/commits/{parse.quote(self.branch, safe='')}",
            not_found=f"Branch '{self.branch}' was not found.",
        )
        self.commit_sha = commit["sha"]
        return self.commit_sha

    async def get_repo_tree(
        self, *, max_depth: int = 8, max_files: int = 300
    ) -> list[str]:
        """Lists readable files, most useful first.

        Args:
            max_depth: Skip files nested deeper than this.
            max_files: Maximum number of paths to return.

        Returns:
            Repository-relative file paths ranked by `rank_path`.
        """
        await self.resolve_ref()
        payload = await self._get_json(
            f"{self._repo_url}/git/trees/{self.commit_sha}",
            params={"recursive": "1"},
        )
        paths = [
            item["path"]
            for item in payload.get("tree", [])
            if item.get("type") == "blob"
            and item.get("path")
            and self._include_path(item["path"], max_depth)
        ]
        return sorted(paths, key=rank_path)[:max_files]

    @staticmethod
    def _include_path(path: str, max_depth: int) -> bool:
        parts = path.split("/")
        if len(parts) > max_depth:
            return False
        if any(part.lower() in IGNORED_PARTS for part in parts):
            return False
        return not path.lower().endswith(IGNORED_SUFFIXES)

    async def _read_text(self, path: str) -> str:
        _check_path(path)
        if path in self._text_cache:
            return self._text_cache[path]
        ref = await self.resolve_ref()
        payload = await self._get_json(
            f"{self._repo_url}/contents/{parse.quote(path, safe='/')}",
            params={"ref": ref},
            not_found=f"File '{path}' was not found or is not public.",
        )
        if (
            payload.get("type") != "file"
            or payload.get("size", MAX_FILE_BYTES + 1) > MAX_FILE_BYTES
        ):
            raise GitHubError(
                f"File '{path}' is not a readable text file or exceeds the "
                "size limit."
            )
        if payload.get("encoding") != "base64":
            raise GitHubError(
                f"GitHub returned an unsupported encoding for '{path}'."
            )
        try:
            content = base64.b64decode(payload["content"]).decode("utf-8")
        except (KeyError, UnicodeDecodeError, ValueError) as error:
            raise GitHubError(f"File '{path}' is not valid UTF-8 text.") from (
                error
            )
        self._text_cache[path] = content
        return content

    async def read_text(self, path: str) -> str:
        """Reads a whole file as raw text, for parsers that need it.

        Args:
            path: Repository-relative file path.

        Returns:
            The file contents without line numbers.

        Raises:
            ValueError: The path leaves the repository.
            GitHubError: The file cannot be read.
        """
        return await self._read_text(path)

    async def read_file(
        self,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        max_chars: int = 18_000,
    ) -> str:
        """Reads a file as `N: text` numbered lines.

        Args:
            path: Repository-relative file path.
            start_line: First line to return, 1-based.
            end_line: Last line to return, inclusive. `None` reads to the end.
            max_chars: Truncate the numbered output to this many characters.

        Returns:
            The selected lines prefixed with their line numbers.

        Raises:
            ValueError: The path or line range is invalid.
            GitHubError: The file cannot be read.
        """
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise ValueError(
                "Line range must start at 1 and end after its start."
            )
        _check_path(path)
        content = await self._read_text(path)
        selected = content.splitlines()[start_line - 1 : end_line]
        numbered = "\n".join(
            f"{number}: {line}"
            for number, line in enumerate(selected, start=start_line)
        )
        return numbered[:max_chars]

    async def search_code(
        self,
        query: str,
        *,
        paths: list[str] | None = None,
        max_matches: int = 40,
    ) -> list[dict[str, int | str]]:
        """Searches file contents with a regular expression.

        Files are fetched concurrently, but matches keep the order of
        `paths`, so results are deterministic.

        Args:
            query: A Python regular expression.
            paths: Files to search. Defaults to the top of the ranked tree.
            max_matches: Stop after this many matching lines.

        Returns:
            Dicts with `path`, `line` and `text` keys.

        Raises:
            re.error: `query` is not a valid regular expression.
        """
        pattern = re.compile(query)
        if paths is None:
            paths = await self.get_repo_tree(max_files=100)
        candidates = [
            path
            for path in paths[:100]
            if self._include_path(path, max_depth=8)
        ]
        semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)

        async def read(path: str) -> str:
            async with semaphore:
                try:
                    return await self.read_file(path, max_chars=24_000)
                except GitHubError:
                    return ""

        texts = await asyncio.gather(*(read(path) for path in candidates))
        matches: list[dict[str, int | str]] = []
        for path, numbered in zip(candidates, texts, strict=True):
            for line in numbered.splitlines():
                number, separator, text = line.partition(": ")
                if separator and pattern.search(text):
                    matches.append(
                        {"path": path, "line": int(number), "text": text}
                    )
                    if len(matches) >= max_matches:
                        return matches
        return matches

    async def detect_stack(self, paths: list[str]) -> dict[str, list[str]]:
        """Extracts technologies and run commands from root manifests.

        Args:
            paths: The repository tree.

        Returns:
            A dict with `files`, `technologies`, `commands` and `contents`
            (numbered manifest text used as evidence).
        """
        found = [
            path
            for path in paths
            if pathlib.PurePosixPath(path).name in STACK_FILES
            and "/" not in path
        ][:8]
        technologies: set[str] = set()
        commands: set[str] = set()
        evidence: list[str] = []
        for path in found:
            # Parse the full text; only the evidence for the LLM is truncated.
            try:
                content = await self._read_text(path)
            except GitHubError:
                continue
            _collect_stack(path, content, technologies, commands)
            numbered = await self.read_file(path, max_chars=8_000)
            evidence.append(f"--- {path} ---\n{numbered}")
        return {
            "files": found,
            "technologies": sorted(technologies),
            "commands": sorted(commands),
            "contents": evidence,
        }


def _collect_stack(
    filename: str,
    content: str,
    technologies: set[str],
    commands: set[str],
) -> None:
    """Adds what one manifest says about the stack to the given sets."""
    if filename in FIXED_STACK_COMMANDS:
        technology, command = FIXED_STACK_COMMANDS[filename]
        technologies.add(technology)
        commands.add(command)
    elif filename == "package.json":
        try:
            manifest = json.loads(content)
        except json.JSONDecodeError:
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        technologies.add("JavaScript / Node.js")
        package_manager = str(manifest.get("packageManager", "npm"))
        technologies.add(package_manager)
        prefix = package_manager.split("@", 1)[0]
        scripts = manifest.get("scripts", {})
        if isinstance(scripts, dict):
            commands.update(f"{prefix} run {script}" for script in scripts)
    elif filename == "pyproject.toml":
        try:
            manifest = tomllib.loads(content)
        except tomllib.TOMLDecodeError:
            manifest = {}
        technologies.add("Python")
        scripts = manifest.get("project", {}).get("scripts", {})
        commands.update(f"{name} (project script)" for name in scripts)
    elif filename == "Dockerfile":
        technologies.add("Docker")
        commands.update(
            line.strip()
            for line in content.splitlines()
            if re.match(r"^(CMD|ENTRYPOINT)\s", line.strip(), re.IGNORECASE)
        )
    elif filename == "Makefile":
        technologies.add("Make")
        commands.update(
            f"make {match.group(1)}"
            for line in content.splitlines()
            if (match := re.match(r"^([A-Za-z0-9_-]+):", line))
            and match.group(1) not in {"all", "clean", "install"}
        )
