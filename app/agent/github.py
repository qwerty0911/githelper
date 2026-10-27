import base64
import fnmatch
import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote, urlparse

import httpx

GITHUB_API = "https://api.github.com"
IGNORED_PARTS = {
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
IGNORED_SUFFIXES = {
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
}
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
LOW_PRIORITY_PARTS = {"test", "tests", "__tests__", "spec", "docs", "examples", "fixtures"}


def rank_path(path: str) -> tuple[int, int, str]:
    """Sort key: README/manifests/entrypoints first, then shallow paths, tests/docs last."""
    parts = path.lower().split("/")
    name_rank = PRIORITY_NAMES.get(parts[-1], 2)
    if name_rank == 2 and any(part in LOW_PRIORITY_PARTS for part in parts[:-1]):
        name_rank = 3
    return (name_rank, len(parts), path)


class GitHubError(Exception):
    pass


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str
    branch: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


def parse_repository_url(url: str, branch: str | None = None) -> Repository:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"https", "http"} or parsed.hostname not in {
        "github.com",
        "www.github.com",
    }:
        raise ValueError("URL must point to a public github.com repository.")

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2 or any(part in {".", ".."} for part in parts):
        raise ValueError("Use a repository URL in the form https://github.com/owner/repo.")

    owner, name = parts
    if name.endswith(".git"):
        name = name[:-4]
    if not owner or not name:
        raise ValueError("Both repository owner and name are required.")
    return Repository(owner=owner, name=name, branch=branch)


class GitHubTools:
    def __init__(
        self,
        repository: Repository,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.repository = repository
        self.branch: str | None = repository.branch
        self.commit_sha: str | None = None
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
        if self._owns_client:
            await self.client.aclose()

    async def _get_json(self, url: str, *, params: dict[str, str] | None = None) -> dict:
        response = await self.client.get(url, params=params)
        if response.status_code == 404:
            raise GitHubError("Repository or requested file was not found or is not public.")
        if response.status_code == 403:
            raise GitHubError("GitHub API rate limit reached. Set GITHUB_TOKEN and try again.")
        if response.is_error:
            raise GitHubError(f"GitHub API returned HTTP {response.status_code}.")
        return response.json()

    async def get_repo_tree(
        self,
        *,
        max_depth: int = 8,
        max_files: int = 300,
    ) -> list[str]:
        await self.resolve_ref()
        tree_url = f"{self._repo_url}/git/trees/{self.commit_sha}"
        payload = await self._get_json(tree_url, params={"recursive": "1"})
        paths = [
            item["path"]
            for item in payload.get("tree", [])
            if item.get("type") == "blob"
            and item.get("path")
            and self._include_path(item["path"], max_depth)
        ]
        return sorted(paths, key=rank_path)[:max_files]

    @property
    def _repo_url(self) -> str:
        return (
            f"{GITHUB_API}/repos/{quote(self.repository.owner)}/"
            f"{quote(self.repository.name)}"
        )

    async def resolve_ref(self) -> str:
        """Pin the analysis to one commit so every later read sees the same snapshot."""
        if self.commit_sha:
            return self.commit_sha
        if self.branch is None:
            self.branch = (await self._get_json(self._repo_url))["default_branch"]
        commit = await self._get_json(f"{self._repo_url}/commits/{quote(self.branch, safe='')}")
        self.commit_sha = commit["sha"]
        return self.commit_sha

    @staticmethod
    def _include_path(path: str, max_depth: int) -> bool:
        parts = path.split("/")
        if len(parts) > max_depth or any(part.lower() in IGNORED_PARTS for part in parts):
            return False
        return not any(path.lower().endswith(suffix) for suffix in IGNORED_SUFFIXES)

    async def _read_text(self, path: str) -> str:
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("File path must stay within the repository.")
        if path in self._text_cache:
            return self._text_cache[path]
        ref = await self.resolve_ref()
        url = f"{self._repo_url}/contents/{quote(path, safe='/')}"
        params = {"ref": ref}
        response = await self.client.get(url, params=params)
        if response.status_code == 404:
            raise GitHubError(f"File '{path}' was not found or is not public.")
        if response.status_code == 403:
            raise GitHubError("GitHub API rate limit reached. Set GITHUB_TOKEN and try again.")
        if response.is_error:
            raise GitHubError(f"GitHub API returned HTTP {response.status_code}.")
        payload = response.json()
        if (
            payload.get("type") != "file"
            or payload.get("size", MAX_FILE_BYTES + 1) > MAX_FILE_BYTES
        ):
            raise GitHubError(
                f"File '{path}' is not a readable text file or exceeds the size limit."
            )
        if payload.get("encoding") != "base64":
            raise GitHubError(f"GitHub returned an unsupported encoding for '{path}'.")
        try:
            content = base64.b64decode(payload["content"], validate=False).decode("utf-8")
        except (KeyError, UnicodeDecodeError, ValueError) as error:
            raise GitHubError(f"File '{path}' is not valid UTF-8 text.") from error
        self._text_cache[path] = content
        return content

    async def read_file(
        self,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        max_chars: int = 18_000,
    ) -> str:
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise ValueError("Line range must start at 1 and end after its start.")
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("File path must stay within the repository.")

        content = await self._read_text(path)

        lines = content.splitlines()
        selected = lines[start_line - 1 : end_line]
        numbered = "\n".join(
            f"{line_number}: {line}"
            for line_number, line in enumerate(selected, start=start_line)
        )
        return numbered[:max_chars]

    async def search_code(
        self,
        query: str,
        *,
        paths: list[str] | None = None,
        max_matches: int = 40,
    ) -> list[dict[str, int | str]]:
        pattern = re.compile(query)
        candidates = paths if paths is not None else await self.get_repo_tree(max_files=100)
        matches: list[dict[str, int | str]] = []
        for path in candidates[:100]:
            if not self._include_path(path, max_depth=8):
                continue
            numbered = await self.read_file(path, max_chars=24_000)
            for line in numbered.splitlines():
                line_number, separator, text = line.partition(": ")
                if separator and pattern.search(text):
                    matches.append({"path": path, "line": int(line_number), "text": text})
                    if len(matches) >= max_matches:
                        return matches
        return matches

    async def detect_stack(self, paths: list[str]) -> dict[str, list[str]]:
        files = {
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
        found = [
            path
            for path in paths
            if PurePosixPath(path).name in files
            and PurePosixPath(path).parent == PurePosixPath(".")
        ][:8]
        technologies: set[str] = set()
        commands: set[str] = set()
        evidence: list[str] = []
        for path in found:
            # Parse the full text; only the evidence shown to the LLM is truncated.
            try:
                content = await self._read_text(path)
            except GitHubError:
                continue
            numbered = await self.read_file(path, max_chars=8_000)
            filename = PurePosixPath(path).name
            if filename == "package.json":
                try:
                    manifest = json.loads(content)
                except json.JSONDecodeError:
                    manifest = {}
                technologies.add("JavaScript / Node.js")
                package_manager = str(manifest.get("packageManager", "npm"))
                technologies.add(package_manager)
                command_prefix = package_manager.split("@", 1)[0]
                for script in manifest.get("scripts", {}):
                    commands.add(f"{command_prefix} run {script}")
            elif filename == "pyproject.toml":
                try:
                    manifest = tomllib.loads(content)
                except tomllib.TOMLDecodeError:
                    manifest = {}
                technologies.add("Python")
                project = manifest.get("project", {})
                for command in project.get("scripts", {}):
                    commands.add(f"{command} (project script)")
            elif filename == "requirements.txt":
                technologies.add("Python")
                commands.add("pip install -r requirements.txt")
            elif filename == "Dockerfile":
                technologies.add("Docker")
                commands.update(
                    line.strip()
                    for line in content.splitlines()
                    if re.match(r"^(CMD|ENTRYPOINT)\s", line.strip(), re.IGNORECASE)
                )
            elif filename == "docker-compose.yml":
                technologies.add("Docker Compose")
                commands.add("docker compose up")
            elif filename == "go.mod":
                technologies.add("Go")
                commands.add("go run .")
            elif filename == "Cargo.toml":
                technologies.add("Rust")
                commands.add("cargo run")
            elif filename == "pom.xml":
                technologies.add("Java / Maven")
                commands.add("mvn spring-boot:run")
            elif filename == "build.gradle":
                technologies.add("Java / Gradle")
                commands.add("./gradlew run")
            elif filename == "Makefile":
                technologies.add("Make")
                commands.update(
                    f"make {match.group(1)}"
                    for line in content.splitlines()
                    if (match := re.match(r"^([A-Za-z0-9_-]+):", line))
                    and match.group(1) not in {"all", "clean", "install"}
                )
            evidence.append(f"--- {path} ---\n{numbered}")
        return {
            "files": found,
            "technologies": sorted(technologies),
            "commands": sorted(commands),
            "contents": evidence,
        }


def paths_matching(pattern: str, paths: list[str]) -> list[str]:
    return [path for path in paths if fnmatch.fnmatch(path, pattern)]
