DEFAULT_MAX_CHARS = 30_000


def truncate(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Keep the head and tail of long output and say how much was cut from the middle."""
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    head, tail = text[:keep], text[-keep:]
    cut = text[keep:-keep]
    note = f"\n[... {len(cut):,} chars / {cut.count(chr(10)):,} lines cut ...]\n"
    return head + note + tail
