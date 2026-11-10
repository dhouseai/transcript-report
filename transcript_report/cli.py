"""The command line: parsing options, the model session, and analysing each transcript.

Run it as `transcript-report` (when installed), `python -m transcript_report`, or
`python analyse_video.py` from a clone of the repository.
"""

import argparse
import io
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from . import __version__
from .check import run_check
from .config import (
    HOME_ENV,
    REPORT_FORMATS,
    RUNNERS,
    SAMPLING_DEFAULTS,
    ModelChoice,
    Prompts,
    RunOptions,
    Settings,
    app_home,
    choose_runner,
    choose_server,
    find_resumable,
    find_transcript,
    init_home,
    list_transcripts,
    load_prompts,
    load_settings,
    move_to_done,
    normalise_url,
    notes_path_for,
    report_path,
    resolve_model,
    safe_name,
    unused_path,
)
from .errors import TranscriptReportError
from .helpers import check_vram, finish_progress, progress, say, warn
from .http_client import APIError
from .pdf import write_pdf
from .pipeline import MIN_SECTION_WORDS, ChatFn, analyse
from .runner import LoadedModel, Runner, make_runner, runner_class
from .transcripts import read_transcript


def parse_args(argv: list[str] | None = None, prog: str | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=prog,
        description="Analyse a video transcript with a local model in LM Studio or Ollama.",
        epilog='The default format is "report_format" in settings.json. Settings and the default '
               f'folders are in {app_home()} (set {HOME_ENV} to use another folder).')
    p.add_argument("transcript", type=Path, nargs="?",
                   help="Transcript (.txt, .srt or .vtt) in the transcripts folder, or a path to one")
    p.add_argument("--all", action="store_true",
                   help="Analyse every transcript waiting in the transcripts folder")
    p.add_argument("--init", action="store_true",
                   help="Create settings.json and the transcripts and reports folders, then exit")
    p.add_argument("--check", action="store_true",
                   help="Test the setup (settings, the runner, the model, PDF browser) and exit")
    p.add_argument("--resume", action="store_true",
                   help="Continue a long video's stopped run from its saved section notes")
    p.add_argument("--title", default="", help="Video title / channel, gives the model context")
    p.add_argument("--model", help="Name from \"models\" in settings.json, or the runner's model key "
                                   "(see `lms ls` or `ollama list`)")
    p.add_argument("--runner", choices=RUNNERS,
                   help="The program that runs the model (default: the model's \"runner\" in settings.json, "
                        "else the \"runner\" setting, else lmstudio)")
    p.add_argument("--ctx", type=int, help="Context window (default: the model's \"ctx\" in settings.json)")
    p.add_argument("--chunk-words", type=int, default=10000, help="Words per section for long videos")
    p.add_argument("--single-max-words", type=int, default=15000,
                   help="Longer transcripts are split into sections (~100 min of speech)")
    p.add_argument("--temperature", type=float,
                   help="Sampling temperature (default: the model's \"sampling\" in settings.json, "
                        f"else {SAMPLING_DEFAULTS['temperature']})")
    p.add_argument("--think", action="store_true", help="Let the model reason first (slower)")
    p.add_argument("--port", type=int,
                   help="Port for the runner's server on this computer (default: 1234 for LM Studio, "
                        "11434 for Ollama)")
    p.add_argument("--server-url", metavar="URL",
                   help="The runner on another computer, e.g. http://192.168.1.20:1234 for LM Studio or "
                        "http://192.168.1.20:11434 for Ollama (default: \"server_url\" in settings.json, "
                        "for the \"runner\" setting's runner; else this computer)")
    p.add_argument("--out", type=Path, help="Report path or folder (default: <reports folder>/<video name>/"
                   "<video name>_report.md or .pdf). Ending it in .pdf or .md also picks the format")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--pdf", action="store_const", const="pdf", dest="format",
                     help="Save the report as a PDF")
    fmt.add_argument("--md", action="store_const", const="md", dest="format",
                     help="Save the report as Markdown")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)

    if args.server_url:
        try:
            args.server_url = normalise_url(args.server_url, "--server-url")
        except TranscriptReportError as e:
            p.error(str(e))
    if args.check or args.init:
        return args
    if args.all == bool(args.transcript):
        p.error("give one transcript, or --all for every transcript in the transcripts folder")
    if args.all and args.title:
        p.error("--title can't be used with --all (it would be the same for every video)")
    if args.chunk_words < MIN_SECTION_WORDS:
        p.error(f"--chunk-words must be at least {MIN_SECTION_WORDS}")
    if args.single_max_words < 1:
        p.error("--single-max-words must be at least 1")
    if args.ctx is not None and args.ctx <= 0:
        p.error("--ctx must be a positive number of tokens")
    if args.temperature is not None and args.temperature < 0:
        p.error("--temperature can't be negative")
    if args.out and not args.out.is_dir():
        if args.out.suffix and args.out.suffix.lower() not in (".md", ".pdf"):
            p.error("--out must end in .md or .pdf, or be a folder")
        if args.all:
            p.error("with --all, --out must be an existing folder")
    return args


def choose_out_path(out: Path | None, fmt_flag: str | None, settings: Settings,
                    transcript: Path) -> Path:
    """Where to save the report. Format: --out's extension, else --pdf/--md, else settings.json."""
    fmt = fmt_flag or settings.report_format
    if out is None:
        return report_path(transcript, settings.reports_dir, fmt)
    if out.is_dir():
        # A folder is shared by many reports (and by --all), so never overwrite one in it.
        # An unfinished run's report doesn't exist yet, so --resume still finds its notes.
        return unused_path(out / f"{safe_name(transcript.stem)}_report", f".{fmt}")
    if out.suffix.lower() in (f".{f}" for f in REPORT_FORMATS):
        return out
    return out.with_suffix("." + fmt)


def save_report(report: str, out_path: Path, title: str) -> Path:
    """Write the report as Markdown or PDF. For a PDF the Markdown is written first, so the
    report is safe whatever happens while making the PDF; it's removed once the PDF exists.
    That draft never replaces an existing file: it gets a _2, _3, ... name if needed."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf = out_path.suffix.lower() == ".pdf"
    md_path = unused_path(out_path.with_suffix(""), ".md") if pdf else out_path.with_suffix(".md")
    md_path.write_text(report, encoding="utf-8")
    if not pdf:
        return md_path
    progress("Making PDF...")
    try:
        write_pdf(report, out_path, title)
    except (RuntimeError, OSError) as e:
        finish_progress()
        warn(f"couldn't make the PDF ({e}). Saved the report as Markdown instead.")
        return md_path
    progress("Making PDF: done", end="\n")
    md_path.unlink(missing_ok=True)
    return out_path


def use_loaded_copy(existing: LoadedModel, model: ModelChoice, opts: RunOptions,
                    ctx_arg: int | None, server: str) -> None:
    """Take the context size from a copy of the model that's already loaded in `server`
    ("LM Studio"), and warn about anything in its setup that could make requests fail."""
    say(f"Using {model.key}, already loaded in {server} "
        f"(context {existing.ctx or 'unknown'}). It stays loaded afterwards.")
    if existing.ctx:
        if ctx_arg and ctx_arg != existing.ctx:
            warn(f"--ctx {ctx_arg:,} is ignored: the loaded copy has a {existing.ctx:,}-token "
                 f"context. Unload it in {server} to use a different size.")
        opts.ctx = existing.ctx
    else:
        warn(f"{server} didn't report the loaded copy's context size; assuming {opts.ctx:,}.")
    if existing.parallel > 1:
        warn(f"the loaded copy allows {existing.parallel} parallel requests, which can split "
             f"its context. If requests fail as too long, reload it with 1.")


@contextmanager
def model_session(runner: Runner, model: ModelChoice, opts: RunOptions,
                  ctx_arg: int | None) -> Iterator[str]:
    """Start the server and load the model (or use an already-loaded copy); yields the
    id to send requests to. Afterwards frees the VRAM again, but leaves alone anything that was
    already running."""
    started_server = loaded_here = False
    model_id = ""
    try:
        started_server = runner.start()
        if not model.listed and not runner.has_model(model.key):
            raise TranscriptReportError(
                f"'{model.name}' isn't in \"models\" in settings.json or an {runner.name} model key "
                f"(downloaded: {', '.join(runner.model_keys()) or 'none'}).")
        existing = runner.loaded(model.key)
        if existing:
            model_id = existing.id
            use_loaded_copy(existing, model, opts, ctx_arg, runner.name)
            vram_advice = f"unload other models in {runner.name}"
        else:
            say(f"Model: {model.name}  ({model.key}, context {opts.ctx:,})")
            loaded = runner.load(model.key, opts.ctx)
            model_id = loaded.id
            loaded_here = True
            opts.ctx = loaded.ctx or opts.ctx
            vram_advice = f"lower --ctx (now {opts.ctx:,})"
        if runner.on_this_computer:   # nvidia-smi only sees this computer's GPU
            check_vram(vram_advice)
        yield model_id
    finally:
        finish_progress()
        if loaded_here:
            runner.unload(model_id)
        if started_server:
            runner.stop()


def analyse_transcript(transcript: Path, text: str, args: argparse.Namespace, settings: Settings,
                       prompts: Prompts, opts: RunOptions, chat_fn: ChatFn) -> None:
    """Analyse one transcript, save the report and move the transcript to the done folder."""
    start = time.monotonic()
    fmt = args.format or settings.report_format
    out_path = None
    if args.resume and args.out is None:
        out_path = find_resumable(transcript, settings.reports_dir, fmt)
        if out_path is None:
            say("No saved section notes found for this transcript; starting from the beginning.")
    out_path = out_path or choose_out_path(args.out, args.format, settings, transcript)

    report = analyse(opts, prompts, text, notes_path_for(out_path), chat_fn, resume=args.resume)
    saved = save_report(report, out_path, transcript.stem)
    say(f"Report saved to {saved}  ({(time.monotonic() - start) / 60:.1f} min)")
    try:
        moved = move_to_done(transcript, settings)
    except OSError as e:
        warn(f"couldn't move the transcript to the done folder ({e}).")
    else:
        if moved:
            say(f"Transcript moved to {moved}")


def run_init(runner_arg: str | None) -> int:
    path, created, settings = init_home()
    if created:
        list_hint = runner_class(choose_runner(runner_arg, None, settings)).list_hint
        print(f"Created {path}\nOpen it and set your model's \"key\" ({list_hint} to see them) "
              f"and \"ctx\", then run with --check.")
    else:
        print(f"{path} already exists, so it was left as it is.")
    print(f"Put transcripts in: {settings.transcripts_dir}\nReports are saved in: {settings.reports_dir}")
    return 0


def read_jobs(args: argparse.Namespace, settings: Settings) -> list[tuple[Path, str]]:
    """(transcript, its text) for each transcript to analyse. With --all, empty ones are skipped."""
    if args.all:
        files = list_transcripts(settings.transcripts_dir)
        if not files:
            raise TranscriptReportError(f"No transcripts (.txt, .srt or .vtt) waiting in {settings.transcripts_dir}.")
    else:
        files = [find_transcript(args.transcript, settings.transcripts_dir)]
    jobs = []
    for f in files:
        text = read_transcript(f)
        if text:
            jobs.append((f, text))
        elif args.all:
            warn(f"{f.name} is empty; skipped.")
        else:
            raise TranscriptReportError("Transcript is empty.")
    return jobs


def run(argv: list[str] | None = None, prog: str | None = None) -> int:
    args = parse_args(argv, prog)
    if args.init:
        return run_init(args.runner)
    if args.check:
        return run_check(args.model, args.runner, args.port, args.server_url)

    settings = load_settings()
    prompts = load_prompts(settings.prompts_dir)
    jobs = read_jobs(args, settings)
    model = resolve_model(args.model, settings)
    sampling = {**SAMPLING_DEFAULTS, **model.sampling}
    if args.temperature is not None:
        sampling["temperature"] = args.temperature
    opts = RunOptions(ctx=args.ctx or model.ctx, title=args.title, chunk_words=args.chunk_words,
                      single_max_words=args.single_max_words, think=args.think, sampling=sampling)

    start = time.monotonic()
    failed: list[str] = []
    # On this computer, starting the server may point the runner at another port.
    kind = choose_runner(args.runner, model, settings)
    runner = make_runner(kind, args.port, *choose_server(args.server_url, settings, kind))
    with model_session(runner, model, opts, args.ctx) as model_id:
        chat_fn = partial(runner.chat, model_id, opts)
        for n, (transcript, text) in enumerate(jobs, 1):
            if len(jobs) > 1:
                say(f"\n[{n}/{len(jobs)}] {transcript.name}")
            try:
                analyse_transcript(transcript, text, args, settings, prompts, opts, chat_fn)
            except TranscriptReportError as e:
                # One video failing (e.g. too long for the context) shouldn't stop a batch,
                # but losing the server altogether should.
                if len(jobs) == 1 or (isinstance(e, APIError) and e.status is None):
                    raise
                finish_progress()
                say(f"   FAILED: {e}")
                failed.append(transcript.name)

    if len(jobs) > 1:
        done = len(jobs) - len(failed)
        say(f"\n{done} of {len(jobs)} transcripts analysed in "
            f"{(time.monotonic() - start) / 60:.1f} min.")
        if failed:
            say("Failed: " + ", ".join(failed))
    return 1 if failed else 0


def main(argv: list[str] | None = None, prog: str | None = None) -> None:
    # Windows consoles default to cp1252, which can't print emoji/arrows in the report.
    # Some IDE consoles replace the streams with objects that can't be reconfigured, and
    # under pythonw they are None.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        code = run(argv, prog)
    except TranscriptReportError as e:
        finish_progress()
        sys.exit(str(e))
    except KeyboardInterrupt:
        finish_progress()
        say("Stopped.")
        sys.exit(130)
    sys.exit(code)
