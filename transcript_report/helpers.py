"""Small helpers: status messages, VRAM check, token estimate, removing reasoning, progress line."""

import re
import subprocess
import sys

from .config import MIN_FREE_VRAM_MB

# Stops a console window flashing up for each helper program on Windows.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def vram_info() -> tuple[int, int] | None:
    """(used, total) MiB on the first NVIDIA GPU, or None if nvidia-smi isn't available."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout.splitlines()[0]
        total, used = (int(x) for x in out.split(","))
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None
    return used, total


def say(message: str) -> None:
    """A status message. They go to stderr, so stdout stays free for piping."""
    print(message, file=sys.stderr)  # noqa: T201  (the program's output)


def warn(message: str) -> None:
    say(f"   WARNING: {message}")


def check_vram(advice: str) -> None:
    """Print GPU memory use once the model is loaded, and warn (adding `advice`) if less than
    MIN_FREE_VRAM_MB is free. Skipped quietly if nvidia-smi isn't available."""
    usage = vram_info()
    if usage is None:
        return
    used, total = usage
    free = total - used
    say(f"   [VRAM: {used:,} / {total:,} MiB used, {free:,} MiB free]")
    if free < MIN_FREE_VRAM_MB:
        warn(f"under {MIN_FREE_VRAM_MB // 1024} GB of VRAM free. Processing may spill into "
             f"shared memory and slow down. Close GPU-heavy apps (games, launchers, browsers with "
             f"video) or {advice}.")


def est_tokens(text: str) -> int:
    """Rough token estimate that errs on the high side for tech jargon."""
    return int(max(len(text.split()) * 1.4, len(text) / 3.6))


def strip_think(text: str) -> str:
    """Remove reasoning, including an unmatched </think> (opening tag added by the chat
    template) or an unclosed <think> (output cut off mid-thought)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    return text.strip()


_line_open = False   # a progress line is showing without a newline after it


def progress(text: str, end: str = "") -> None:
    """Rewrite a single status line in place. When stderr isn't a terminal (redirected to a
    file), only the finished lines (end="\\n") are written."""
    global _line_open
    if not sys.stderr.isatty():
        if end and text:
            say(text)
        return
    print("\r" + text.ljust(78), end=end, file=sys.stderr, flush=True)  # noqa: T201
    _line_open = not end


def finish_progress() -> None:
    """End a progress line that's still open, so the next message starts on its own line."""
    global _line_open
    if _line_open:
        say("")
        _line_open = False
