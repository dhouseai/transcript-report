"""--check: test the setup (settings, prompts, the runner, the model, a browser, the GPU)
without running an analysis, and say what to fix."""
# ruff: noqa: T201  (the check results are this module's output, printed to stdout)

import platform
import sys
from collections.abc import Callable

from . import __version__
from .config import (
    ModelChoice,
    app_home,
    choose_runner,
    list_transcripts,
    load_prompts,
    load_settings,
    resolve_model,
)
from .errors import TranscriptReportError
from .helpers import vram_info
from .ollama import OllamaRunner
from .pdf import find_browser
from .runner import Runner, make_runner

OK, WARN, FAIL = "  ok  ", " warn ", " FAIL "


def run_check(model_arg: str | None, runner_arg: str | None, port: int | None, url_arg: str | None) -> int:
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

    runner = make_runner(choose_runner(runner_arg, model, settings), port, url_arg or settings.server_url,
                         settings.api_token)
    check_server(runner, model, line)

    browser = find_browser()
    if browser:
        line(OK, "PDF browser", str(browser))
    else:
        line(FAIL if settings.report_format == "pdf" else WARN, "PDF browser",
             "Edge or Chrome not found, so reports can only be saved as Markdown")

    if not runner.on_this_computer:
        line(OK, "GPU", f"on the {runner.name} computer, so not checked from here")
    elif usage := vram_info():
        used, total = usage
        line(OK, "GPU", f"{used:,} / {total:,} MiB VRAM in use")
    else:
        line(WARN, "GPU", "nvidia-smi not found, so the VRAM check is skipped")

    print("\nFix the FAIL lines above, then run --check again." if failed else "\nAll set.")
    return 1 if failed else 0


def check_server(runner: Runner, model: ModelChoice | None, line: Callable[[str, str, str], None]) -> None:
    """The runner's command-line program (on this computer), the server (and Ollama's version), and
    whether the model is downloaded. A server started for the check is stopped again."""
    if runner.on_this_computer:
        try:
            line(OK, runner.cli, runner.find_cli())
        except TranscriptReportError as e:
            line(FAIL, runner.cli, str(e))
            return
    started = False
    try:
        started = runner.start()
        where = "on another computer" if not runner.on_this_computer else (
            "started for this check" if started else "already running")
        line(OK, f"{runner.name} server", f"{runner.url} ({where})")
        if isinstance(runner, OllamaRunner):
            line(OK, "Ollama version", runner.version())
        if model and runner.has_model(model.key):
            line(OK, "model downloaded", model.key)
        elif model:
            line(FAIL, "model downloaded", f"{runner.name} has no model with the key {model.key}. "
                                           f"Downloaded: {', '.join(runner.model_keys()) or 'none'}")
    except TranscriptReportError as e:
        line(FAIL, f"{runner.name} server", str(e))
    finally:
        if started:
            runner.stop()
