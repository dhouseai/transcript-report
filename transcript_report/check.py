"""--check: test the setup (settings, prompts, LM Studio, the model, a browser, the GPU)
without running an analysis, and say what to fix."""
# ruff: noqa: T201  (the check results are this module's output, printed to stdout)

import platform
import sys
from collections.abc import Callable

from . import __version__
from .config import ModelChoice, app_home, list_transcripts, load_prompts, load_settings, resolve_model
from .errors import TranscriptReportError
from .helpers import vram_info
from .lmstudio import LMStudio, ensure_server, find_lms, model_keys, stop_server
from .pdf import find_browser

OK, WARN, FAIL = "  ok  ", " warn ", " FAIL "


def run_check(model_arg: str | None, port: int, url_arg: str | None) -> int:
    """Print one line per check. Returns 1 if anything failed, else 0."""
    failed = False

    def line(state: str, what: str, detail: str = "") -> None:
        nonlocal failed
        failed |= state == FAIL
        print(f"[{state}] {what}" + (f": {detail}" if detail else ""))

    print(f"transcript-report {__version__}, Python {platform.python_version()} on {sys.platform}")
    print(f"Home folder: {app_home()}\n")

    try:
        settings = load_settings()
    except TranscriptReportError as e:
        line(FAIL, "settings.json", str(e))
        return 1
    if settings.source:
        line(OK, "settings.json", str(settings.source))
    else:
        line(WARN, "settings.json", "not found, so the defaults are used. Run with --init to create one.")

    try:
        load_prompts(settings.prompts_dir)
        line(OK, "prompts", str(settings.prompts_dir))
    except TranscriptReportError as e:
        line(FAIL, "prompts", str(e))

    waiting = list_transcripts(settings.transcripts_dir)
    if settings.transcripts_dir.is_dir():
        line(OK, "transcripts folder", f"{settings.transcripts_dir} ({len(waiting)} waiting)")
    else:
        line(WARN, "transcripts folder", f"{settings.transcripts_dir} doesn't exist yet")

    try:
        model = resolve_model(model_arg, settings)
        line(OK, "model setting", f"{model.name} -> {model.key}, context {model.ctx:,}")
    except TranscriptReportError as e:
        line(FAIL, "model setting", str(e))
        model = None

    client = LMStudio(port, url_arg or settings.server_url, settings.api_token)
    check_server(client, model, line)

    browser = find_browser()
    if browser:
        line(OK, "PDF browser", str(browser))
    else:
        line(FAIL if settings.report_format == "pdf" else WARN, "PDF browser",
             "Edge or Chrome not found, so reports can only be saved as Markdown")

    if not client.local:
        line(OK, "GPU", "on the LM Studio computer, so not checked from here")
    elif usage := vram_info():
        used, total = usage
        line(OK, "GPU", f"{used:,} / {total:,} MiB VRAM in use")
    else:
        line(WARN, "GPU", "nvidia-smi not found, so the VRAM check is skipped")

    print("\nFix the FAIL lines above, then run --check again." if failed else "\nAll set.")
    return 1 if failed else 0


def check_server(client: LMStudio, model: ModelChoice | None, line: Callable[[str, str, str], None]) -> None:
    """The lms tool (on this computer), the server, and whether the model is downloaded.
    A server started for the check is stopped again."""
    if client.local:
        try:
            line(OK, "LM Studio's lms tool", str(find_lms()))
        except TranscriptReportError as e:
            line(FAIL, "LM Studio's lms tool", str(e))
            return
    started = False
    try:
        started = ensure_server(client)
        where = "on another computer" if not client.local else (
            "started for this check" if started else "already running")
        line(OK, "LM Studio server", f"{client.url} ({where})")
        keys = model_keys(client)
        if model and model.key in keys:
            line(OK, "model downloaded", model.key)
        elif model:
            line(FAIL, "model downloaded", f"LM Studio has no model with the key {model.key}. "
                                           f"Downloaded: {', '.join(keys) or 'none'}")
    except TranscriptReportError as e:
        line(FAIL, "LM Studio server", str(e))
    finally:
        if started:
            stop_server()
