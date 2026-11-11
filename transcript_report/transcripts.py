"""Reading transcripts: text copied from YouTube's transcript panel, or .srt/.vtt subtitle files."""

import html
import re
from pathlib import Path

# YouTube transcript lines start with a timestamp such as "1:07" followed by its spoken form
# "1 minute, 7 seconds", glued straight onto the first word: "1:071 minute, 7 secondsSo then".
# Parts that are zero are left out of the spoken form ("2:00" -> "2 minutes").
TIMESTAMP_RE = re.compile(r"[ \t]*(\d{1,2}(?::\d{2}){1,2})")
# Used only if the exact spoken form isn't found: each unit at most once, in order.
SPOKEN_FALLBACK_RE = re.compile(r"(?:\d+ hours?(?:, )?)?(?:\d+ minutes?(?:, )?)?(?:\d+ seconds?)?")


def spoken_time(timestamp: str) -> str:
    """"1:07" -> "1 minute, 7 seconds", the way YouTube's transcript writes it out."""
    parts = [int(p) for p in timestamp.split(":")]
    parts = [0] * (3 - len(parts)) + parts
    words = [f"{n} {unit}{'' if n == 1 else 's'}"
             for n, unit in zip(parts, ("hour", "minute", "second"), strict=True) if n]
    return ", ".join(words) or "0 seconds"


def clean_transcript(text: str) -> str:
    """Remove YouTube's timestamps (and their spoken form) and blank lines."""
    lines = []
    for line in text.lstrip("\ufeff").splitlines():
        m = TIMESTAMP_RE.match(line)
        if m:
            rest = line[m.end():]
            spoken = spoken_time(m.group(1))
            if rest.startswith(spoken):
                line = rest[len(spoken):]
            elif not rest.strip():
                line = ""                                   # a timestamp on its own line
            else:
                n = SPOKEN_FALLBACK_RE.match(rest).end()    # every part is optional, so this always matches
                if n:                                       # otherwise it's speech like "3:45 pm"
                    line = rest[n:]
        if line.strip():
            lines.append(line)
    return "\n".join(lines).strip()


_TAG_RE = re.compile(r"<[^>]*>|\{\\[^}]*\}")            # <i>, <c>, <00:00:01.000>, {\an8}
_SOUND_RE = re.compile(r"\[[^\]]*\]|♪")                 # [Music], [Applause], ♪
_SKIP_BLOCKS = ("WEBVTT", "NOTE", "STYLE", "REGION")


def parse_subtitles(text: str) -> str:
    """The spoken text of an .srt or .vtt file: cue numbers, timings, styling and sound tags
    removed. YouTube's auto-generated .vtt repeats each line in the next cue ("rolling"
    captions), so a line identical to the one before it is dropped."""
    lines: list[str] = []
    blocks = re.split(r"\n[ \t]*\n", text.lstrip("\ufeff").replace("\r\n", "\n"))
    for block in blocks:
        block_lines = [b.strip() for b in block.strip().split("\n")]
        if block_lines[0].startswith(_SKIP_BLOCKS):
            continue
        # The text follows the timing line; before it there's only a cue number or id.
        # Without a timing line, the block is text that a stray blank line split off.
        timing = next((n for n, line in enumerate(block_lines) if "-->" in line), -1)
        for line in block_lines[timing + 1:]:
            line = html.unescape(_TAG_RE.sub("", line))
            line = re.sub(r"\s+", " ", _SOUND_RE.sub("", line)).strip()
            if line and (not lines or line != lines[-1]):
                lines.append(line)
    return "\n".join(lines)


def read_transcript(path: Path) -> str:
    """The cleaned text of a transcript file (.srt and .vtt are parsed as subtitles)."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() in (".srt", ".vtt"):
        return parse_subtitles(text)
    return clean_transcript(text)
