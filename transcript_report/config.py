"""settings.json, prompts, token budgets, run options, and finding transcripts and report paths.

Nothing is read at import time: main() calls load_settings() and load_prompts() and passes
the results down, so the other modules can be imported (and tested) without a settings.json.

Where settings.json and the default folders live (the "home" folder, see app_home()):
  - $TRANSCRIPT_REPORT_HOME, if it's set;
  - the project folder, when running from a clone of the repository;
  - otherwise (installed with pip/pipx) ~/transcript-report.
"""

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .errors import TranscriptReportError

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"                         # the built-in prompts
EXAMPLE_SETTINGS_FILE = PACKAGE_DIR / "settings.example.json"
SETTINGS_NAME = "settings.json"
HOME_ENV = "TRANSCRIPT_REPORT_HOME"
RUNNERS = ("lmstudio", "ollama")   # the programs that can run the model (see runner.make_runner)
RUNNER_CHOICES = " or ".join(f"\"{r}\"" for r in RUNNERS)
# Said where a model key is asked for before it's known which runner it's for.
KEY_HINT = "run `lms ls` for LM Studio, `ollama list` for Ollama"

DEFAULT_CTX = 32768       # context size when settings.json doesn't give one
REPORT_TOKENS = 6000      # room reserved for the final report
NOTES_TOKENS = 1500       # room reserved for each section's notes
THINK_TOKENS = 4000       # extra output room per call with --think (reasoning counts against the limit)
SAFETY_TOKENS = 400
MIN_FREE_VRAM_MB = 2048   # below this, Windows starts using shared memory and it slows right down
REPORT_FORMATS = ("md", "pdf")
DEFAULT_CHUNK_WORDS = 10000        # words per section for long videos
DEFAULT_SINGLE_MAX_WORDS = 15000   # longer transcripts are split into sections
WORDS_PER_MINUTE = 150             # speaking rate, for a video's length in minutes
TRANSCRIPT_SUFFIXES = (".txt", ".srt", ".vtt")
MAX_NAME_CHARS = 80       # report folder/file names are cut to this, to stay under Windows' MAX_PATH

# Sampling settings sent with every request. A model's "sampling" in settings.json overrides
# these; --temperature overrides the temperature. Tuned for Qwen-style models.
SAMPLING_DEFAULTS: dict[str, float] = {
    "temperature": 0.4, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "repeat_penalty": 1.05,
}

FOLDER_DEFAULTS = {
    "transcripts_folder": "transcripts",         # where the transcripts are read from
    "done_folder": "transcripts/done",           # transcripts move here once their report is saved
    "reports_folder": "reports",                 # reports are saved in <this>/<video name>/
}
STRING_DEFAULTS = {
    **FOLDER_DEFAULTS,
    "prompts_folder": "",    # your own prompt files; empty, or any file missing there: the built-in ones
    "report_format": "md",   # "md" or "pdf": what the report is saved as when --pdf/--md isn't given
    "default_model": "",     # name from "models" used when --model isn't given
    "runner": "lmstudio",    # "lmstudio" or "ollama": runs the models that don't set their own "runner"
    "server_url": "",        # e.g. http://192.168.1.20:1234 for the "runner" setting's runner on
                             # another computer; empty: on this computer, started and stopped as needed
    "api_token": "",         # for a server that asks for one; empty: none, or $LM_API_TOKEN for LM Studio
}
KNOWN_SETTINGS = (*STRING_DEFAULTS, "models")
SETTING_KINDS = {   # what each setting must be, for error messages
    **dict.fromkeys(FOLDER_DEFAULTS, "a folder path"),
    "prompts_folder": "a folder path, or empty",
    "report_format": " or ".join(f"\"{f}\"" for f in REPORT_FORMATS),
    "default_model": "a model name from \"models\"",
    "runner": RUNNER_CHOICES,
    "server_url": "an address such as \"http://192.168.1.20:1234\", or empty",
    "api_token": "an API token, or empty",
}
MODEL_FIELDS = ("key", "ctx", "runner", "sampling", "notes")

REPORT_PROMPT_FILE = "report_prompt.md"
NOTES_PROMPT_FILE = "section_notes_prompt.md"
# The merge prompt reuses the notes format: everything in section_notes_prompt.md after this line.
NOTES_FORMAT_MARKER = "Output only the notes, in exactly this format:"
# pipeline.final_user() tells the model to use these sections of the notes for two of the scores.
NOTES_HEADINGS_USED = ("### Style evidence", "### Padding")


@dataclass(frozen=True)
class ModelEntry:
    key: str                  # the model's key in its runner (`lms ls`, `ollama list`)
    ctx: int = DEFAULT_CTX    # context size to load it with
    sampling: dict[str, float] = field(default_factory=dict)   # overrides SAMPLING_DEFAULTS
    runner: str = ""          # one of RUNNERS; empty: the "runner" setting


@dataclass(frozen=True)
class ModelChoice:
    name: str                 # what the user called it
    key: str                  # the model's key in its runner
    ctx: int                  # context size from settings.json (or DEFAULT_CTX)
    listed: bool              # found in "models" in settings.json
    sampling: dict[str, float] = field(default_factory=dict)
    runner: str = ""          # the model's "runner" in settings.json, or empty


@dataclass(frozen=True)
class Settings:
    transcripts_dir: Path
    done_dir: Path
    reports_dir: Path
    prompts_dir: Path = PROMPTS_DIR
    report_format: str = "md"
    default_model: str = ""
    models: dict[str, ModelEntry] = field(default_factory=dict)
    source: Path | None = None   # the settings.json read, or None if there wasn't one
    runner: str = "lmstudio"     # for models without their own "runner"
    server_url: str = ""         # the "runner" setting's runner elsewhere; empty for this computer
    api_token: str = ""          # sent as "Authorization: Bearer ..." if set; only what settings.json says


@dataclass(frozen=True)
class Prompts:
    report: str   # system prompt for the report
    notes: str    # system prompt for one section's notes
    merge: str    # system prompt for merging several sections' notes


@dataclass
class RunOptions:
    """What the analysis needs to know about this run (from the command line and the model)."""
    ctx: int
    title: str = ""
    chunk_words: int = DEFAULT_CHUNK_WORDS
    single_max_words: int = DEFAULT_SINGLE_MAX_WORDS
    think: bool = False
    sampling: dict[str, float] = field(default_factory=lambda: dict(SAMPLING_DEFAULTS))

    def output_tokens(self, base: int) -> int:
        """Output allowance for a call: `base`, plus room for reasoning with --think."""
        return base + (THINK_TOKENS if self.think else 0)


def app_home() -> Path:
    """The folder holding settings.json, and the default transcripts and reports folders."""
    env = os.environ.get(HOME_ENV, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    checkout = PACKAGE_DIR.parent
    if (checkout / "pyproject.toml").is_file() and (checkout / "analyse_video.py").is_file():
        return checkout   # running from a clone (or an editable install): use the project folder
    return Path.home() / "transcript-report"


def settings_file() -> Path:
    return app_home() / SETTINGS_NAME


def normalise_url(url: str, where: str) -> str:
    """A runner's server address, checked and without a trailing slash."""
    url = url.strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s/]+", url):
        raise TranscriptReportError(f"{where} must look like http://<computer>:<port>, e.g. "
                                    f"http://192.168.1.20:1234 (got '{url}').")
    return url


def load_settings(path: Path | None = None) -> Settings:
    """The defaults, overridden by settings.json (in the home folder unless `path` is given)
    if it exists. Raises TranscriptReportError on mistakes."""
    path = path or settings_file()
    raw: object = {}
    exists = path.exists()
    if exists:
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as e:
            raise TranscriptReportError(f"{path.name} isn't valid JSON: {e}\n"
                                        f"(In paths, use / or a doubled backslash \\\\.)") from None
    if not isinstance(raw, dict):
        raise TranscriptReportError(f"{path.name} must be a JSON object: {{\"setting\": value, ...}}.")

    values = dict(STRING_DEFAULTS)
    models: dict[str, ModelEntry] = {}
    for key, value in raw.items():
        if key.startswith("_"):
            continue   # notes such as "_help"
        if key == "models":
            models = parse_models(value, path.name)
            continue
        if key not in values:
            raise TranscriptReportError(f"Unknown setting '{key}' in {path.name}. "
                                        f"Known settings: {', '.join(KNOWN_SETTINGS)}")
        if not isinstance(value, str) or (key in FOLDER_DEFAULTS and not value.strip()):
            raise TranscriptReportError(f"Setting '{key}' in {path.name} must be {SETTING_KINDS[key]}.")
        values[key] = value.strip()

    report_format = values["report_format"].lower()
    if report_format not in REPORT_FORMATS:
        raise TranscriptReportError(f"Setting 'report_format' in {path.name} must be {SETTING_KINDS['report_format']}.")
    runner = values["runner"].lower()
    if runner not in RUNNERS:
        raise TranscriptReportError(f"Setting 'runner' in {path.name} must be {SETTING_KINDS['runner']}.")
    url = values["server_url"]
    if url:
        url = normalise_url(url, f"Setting 'server_url' in {path.name}")
    root = path.parent
    return Settings(
        transcripts_dir=resolve_folder(values["transcripts_folder"], root),
        done_dir=resolve_folder(values["done_folder"], root),
        reports_dir=resolve_folder(values["reports_folder"], root),
        prompts_dir=resolve_folder(values["prompts_folder"], root) if values["prompts_folder"] else PROMPTS_DIR,
        report_format=report_format,
        default_model=values["default_model"],
        models=models,
        source=path if exists else None,
        runner=runner,
        server_url=url,
        # Only settings.json's token: $LM_API_TOKEN is read by the LM Studio runner itself, so it
        # never reaches another runner's server.
        api_token=values["api_token"],
    )


def init_home(home: Path | None = None) -> tuple[Path, bool, Settings]:
    """Create the home folder with a settings.json (copied from the example, unless one is
    already there) and the transcripts and reports folders.
    Returns (settings.json path, whether it was created now, the settings)."""
    home = home or app_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / SETTINGS_NAME
    created = not path.exists()
    if created:
        shutil.copyfile(EXAMPLE_SETTINGS_FILE, path)
    settings = load_settings(path)
    for d in (settings.transcripts_dir, settings.done_dir, settings.reports_dir):
        d.mkdir(parents=True, exist_ok=True)
    return path, created, settings


def parse_models(models: object, file_name: str) -> dict[str, ModelEntry]:
    where = f"\"models\" in {file_name}"
    if not isinstance(models, dict):
        raise TranscriptReportError(f"{where} must be {{\"name\": {{\"key\": ..., \"ctx\": ...}}, ...}}.")
    parsed = {}
    for name, m in models.items():
        if not isinstance(m, dict) or not isinstance(m.get("key"), str) or not m["key"].strip():
            raise TranscriptReportError(f"Model '{name}' in {where} needs a \"key\": the model's key in its "
                                        f"runner ({KEY_HINT}).")
        unknown = [k for k in m if k not in MODEL_FIELDS and not k.startswith("_")]
        if unknown:
            raise TranscriptReportError(f"Model '{name}' in {where}: unknown field '{unknown[0]}'. "
                                        f"Known fields: {', '.join(MODEL_FIELDS)}")
        ctx = m.get("ctx", DEFAULT_CTX)
        if not isinstance(ctx, int) or isinstance(ctx, bool) or ctx <= 0:
            raise TranscriptReportError(f"Model '{name}' in {where}: \"ctx\" must be a whole number of tokens.")
        runner = m.get("runner", "")
        if "runner" in m and not (isinstance(runner, str) and runner.strip().lower() in RUNNERS):
            raise TranscriptReportError(f"Model '{name}' in {where}: \"runner\" must be {RUNNER_CHOICES}.")
        parsed[name] = ModelEntry(key=m["key"].strip(), ctx=ctx,
                                  sampling=parse_sampling(m.get("sampling", {}), f"Model '{name}' in {where}"),
                                  runner=runner.strip().lower())
    return parsed


def parse_sampling(sampling: object, where: str) -> dict[str, float]:
    if not isinstance(sampling, dict):
        raise TranscriptReportError(f"{where}: \"sampling\" must be {{\"temperature\": 0.4, ...}}.")
    for k, v in sampling.items():
        if k not in SAMPLING_DEFAULTS:
            raise TranscriptReportError(f"{where}: unknown sampling setting '{k}'. "
                                        f"Known: {', '.join(SAMPLING_DEFAULTS)}")
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            raise TranscriptReportError(f"{where}: sampling setting '{k}' must be a number of 0 or more.")
    return dict(sampling)


def resolve_folder(value: str, root: Path) -> Path:
    """A settings path: absolute as given, otherwise relative to `root` (settings.json's folder)."""
    p = Path(value).expanduser()
    return p if p.is_absolute() else root / p


def load_prompts(prompts_dir: Path = PROMPTS_DIR) -> Prompts:
    """Read the prompt files and build the merge prompt from the notes format. A file that's
    missing from a custom prompts folder falls back to the built-in one."""
    def read(name: str) -> str:
        path = prompts_dir / name
        if not path.is_file() and prompts_dir != PROMPTS_DIR:
            path = PROMPTS_DIR / name
        try:
            return path.read_text(encoding="utf-8-sig")
        except OSError as e:
            raise TranscriptReportError(f"Can't read the prompt file {path} ({e}).") from None

    report, notes = read(REPORT_PROMPT_FILE), read(NOTES_PROMPT_FILE)
    _, found, notes_format = notes.partition(NOTES_FORMAT_MARKER)
    if not found:
        raise TranscriptReportError(f"{NOTES_PROMPT_FILE} must contain the line \"{NOTES_FORMAT_MARKER}\" "
                                    f"followed by the notes format (the merge step reuses it).")
    missing = [h for h in NOTES_HEADINGS_USED if h not in notes_format]
    if missing:
        raise TranscriptReportError(f"{NOTES_PROMPT_FILE} is missing {', '.join(missing)}: the final "
                                    f"report step refers to these headings.")
    merge = (
        "You are given notes from several consecutive sections of the same video, all in the "
        "same fixed format. Merge them into ONE set of notes in exactly that format. Combine "
        "duplicates, but keep every distinct action, claim, resource, term and piece of evidence. "
        "Keep the total under 900 words. Output only the merged notes.\n\n" + notes_format
    )
    return Prompts(report=report, notes=notes, merge=merge)


def find_transcript(given: Path, transcripts_dir: Path) -> Path:
    """Use the path as given if it exists, otherwise look in the transcripts folder.
    Without an extension, .txt, .srt and .vtt are tried."""
    candidates = [given, transcripts_dir / given.name]
    if not given.suffix:
        for suffix in TRANSCRIPT_SUFFIXES:
            candidates += [given.with_name(given.name + suffix), transcripts_dir / (given.name + suffix)]
    for path in candidates:
        if path.is_file():
            return path
    raise TranscriptReportError(f"Transcript not found: {given} (also looked in {transcripts_dir})")


def list_transcripts(transcripts_dir: Path) -> list[Path]:
    """The transcripts waiting in the transcripts folder (not its subfolders), by name."""
    if not transcripts_dir.is_dir():
        return []
    return sorted((p for p in transcripts_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in TRANSCRIPT_SUFFIXES),
                  key=lambda p: p.name.lower())


def move_to_done(transcript: Path, settings: Settings) -> Path | None:
    """Move a finished transcript from the transcripts folder into the done folder.
    Files from anywhere else are left where they are. Returns the new path, or None.
    Raises OSError if the move fails."""
    if transcript.resolve().parent != settings.transcripts_dir.resolve():
        return None
    settings.done_dir.mkdir(parents=True, exist_ok=True)
    # A transcript with this name was done before: keep both.
    target = unused_path(settings.done_dir / transcript.stem, transcript.suffix)
    return Path(shutil.move(transcript, target))   # works across drives, unlike Path.replace


_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                   *(f"LPT{i}" for i in range(1, 10))}


def safe_name(stem: str) -> str:
    """A transcript name made safe for a Windows folder and file name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem)
    name = name[:MAX_NAME_CHARS].strip().rstrip(".")   # Windows silently drops trailing dots
    if not name:
        return "video"
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = "_" + name
    return name


def unused_path(base: Path, ext: str = "") -> Path:
    """<base><ext>, or <base>_2<ext>, <base>_3<ext>, ... if that already exists. `ext` is
    passed separately because report names can contain dots ("v1.2 launch")."""
    path, n = base.with_name(base.name + ext), 2
    while path.exists():
        path = base.with_name(f"{base.name}_{n}{ext}")
        n += 1
    return path


def report_path(transcript: Path, reports_dir: Path, fmt: str) -> Path:
    """<reports folder>/<video name>/<video name>_report.<fmt>.
    If that folder already exists, use <video name>_2, _3, ... so nothing is overwritten."""
    stem = safe_name(transcript.stem)
    return unused_path(reports_dir / stem) / f"{stem}_report.{fmt}"


def notes_path_for(report: Path) -> Path:
    """Where a report's section notes are saved: next to it, as <report name>_section_notes.md."""
    return report.with_name(report.stem + "_section_notes.md")


def find_resumable(transcript: Path, reports_dir: Path, fmt: str) -> Path | None:
    """The report path in the most recent report folder for this transcript that has saved
    section notes and no finished report (a run that stopped part-way), or None. Notes are
    kept after a run finishes, so a folder with a report in it must not be resumed: that
    would write over the report."""
    stem = safe_name(transcript.stem)
    if not reports_dir.is_dir():
        return None
    candidates = []
    for d in reports_dir.iterdir():
        if d.is_dir() and re.fullmatch(re.escape(stem) + r"(_\d+)?", d.name):
            report = d / f"{stem}_report.{fmt}"
            notes = notes_path_for(report)
            finished = any(report.with_suffix(f".{f}").exists() for f in REPORT_FORMATS)
            if notes.is_file() and not finished:
                candidates.append((notes.stat().st_mtime, report))
    return max(candidates)[1] if candidates else None


def resolve_model(model_arg: str | None, settings: Settings) -> ModelChoice:
    """--model (or "default_model") as a name from "models" in settings.json, or else taken
    as a model key (model_session() checks that the runner has it once the server is up)."""
    name = model_arg or settings.default_model
    if not name:
        if settings.source is None:
            raise TranscriptReportError(f"No {SETTINGS_NAME} yet (looked in {app_home()}). Run with --init to "
                                        f"create one, then add your model to it. Or pass --model <model key>.")
        raise TranscriptReportError(f"No model chosen. Set \"default_model\" in {settings.source} to a name "
                                    f"from \"models\", or pass --model.")
    if name in settings.models:
        m = settings.models[name]
        if m.key.startswith("REPLACE-"):
            raise TranscriptReportError(f"Model '{name}' in {settings.source or SETTINGS_NAME} still has the example "
                                        f"\"key\". Replace it with your model's key ({KEY_HINT}).")
        return ModelChoice(name=name, key=m.key, ctx=m.ctx, listed=True, sampling=m.sampling, runner=m.runner)
    return ModelChoice(name=name, key=name, ctx=DEFAULT_CTX, listed=False)


def choose_runner(runner_arg: str | None, model: ModelChoice | None, settings: Settings) -> str:
    """Which runner to use: --runner, else the model's "runner" in settings.json, else the
    "runner" setting."""
    return runner_arg or (model.runner if model else "") or settings.runner


def choose_server(url_arg: str | None, settings: Settings, kind: str) -> tuple[str, str]:
    """(server address, API token) for `kind`, the runner chosen for this run (empty address:
    this computer). The "server_url" and "api_token" settings are for the "runner" setting's
    runner; the other runner is on this computer, with no token. --server-url changes only the
    address."""
    if kind == settings.runner:
        return url_arg or settings.server_url, settings.api_token
    return url_arg or "", ""
