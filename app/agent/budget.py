from functools import lru_cache

FALLBACK_CHARS_PER_TOKEN = 3


@lru_cache(maxsize=1)
def _encoding():
    try:
        import tiktoken

        return tiktoken.get_encoding("o200k_base")
    except Exception:  # offline or encoding unavailable: fall back to a rough estimate
        return None


def count_tokens(text: str) -> int:
    encoding = _encoding()
    if encoding is None:
        return -(-len(text) // FALLBACK_CHARS_PER_TOKEN)
    return len(encoding.encode(text, disallowed_special=()))


def fit_lines(text: str, max_tokens: int) -> str:
    """Keep whole leading lines within the budget so "N: " line numbers stay valid."""
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        cost = count_tokens(line) + 1
        if used + cost > max_tokens:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept)
