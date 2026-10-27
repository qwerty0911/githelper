"""Token counting and context-budget helpers."""

import functools
import re

FALLBACK_CHARS_PER_TOKEN = 3
_LINE_NUMBER = re.compile(r"^(\d+): ")


@functools.lru_cache(maxsize=1)
def _encoding():
    try:
        import tiktoken  # pylint: disable=import-outside-toplevel

        return tiktoken.get_encoding("o200k_base")
    except Exception:  # pylint: disable=broad-except
        # Offline or encoding unavailable: fall back to a rough estimate.
        return None


def count_tokens(text: str) -> int:
    """Returns the number of model tokens in `text`."""
    encoding = _encoding()
    if encoding is None:
        return -(-len(text) // FALLBACK_CHARS_PER_TOKEN)
    return len(encoding.encode(text, disallowed_special=()))


def fit_lines(text: str, max_tokens: int) -> str:
    """Keeps whole leading lines within a token budget.

    Whole lines are kept so `N: ` line numbers stay valid for citations.

    Args:
        text: Newline-separated text.
        max_tokens: Token budget.

    Returns:
        The longest prefix of whole lines that fits the budget.
    """
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        cost = count_tokens(line) + 1
        if used + cost > max_tokens:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept)


def add_chunk(
    contents: dict[str, str],
    path: str,
    text: str,
    tokens_left: int,
    *,
    max_file_tokens: int,
) -> int:
    """Appends new numbered lines to a file's entry within the budget.

    Lines already present for `path` are skipped, so overlapping reads do
    not waste budget.

    Args:
        contents: Path to numbered content read so far. Updated in place.
        path: File the lines belong to.
        text: `N: text` numbered lines to add.
        tokens_left: Remaining budget for all files.
        max_file_tokens: Upper bound for this single chunk.

    Returns:
        Tokens used by the added chunk.
    """
    existing = {
        match.group(1)
        for line in contents.get(path, "").splitlines()
        if (match := _LINE_NUMBER.match(line))
    }
    lines = [
        line
        for line in text.splitlines()
        if (match := _LINE_NUMBER.match(line))
        and match.group(1) not in existing
    ]
    chunk = fit_lines("\n".join(lines), min(max_file_tokens, tokens_left))
    if not chunk:
        return 0
    contents[path] = f"{contents[path]}\n{chunk}" if path in contents else chunk
    return count_tokens(chunk)


def tokens_used(contents: dict[str, str]) -> int:
    """Returns the total tokens of all read file contents."""
    return sum(count_tokens(text) for text in contents.values())
