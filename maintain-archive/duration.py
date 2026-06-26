import re

_PATTERN = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_duration(text: str) -> float:
    "Parse a duration like 1h30m or 2m15s into seconds. Units must appear in h, m, s order."
    match = _PATTERN.fullmatch(text)
    if not match or text == "":
        raise ValueError(f"invalid duration {text!r}: expected a form like 1h30m, 2m15s or 45s")
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds
