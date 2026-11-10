"""The interface the command line uses for the program that runs the model (LM Studio, Ollama)."""

from dataclasses import dataclass
from typing import Protocol

from .config import RUNNERS, RunOptions
from .errors import TranscriptReportError


@dataclass(frozen=True)
class LoadedModel:
    id: str             # sent with each request and to unload it; LM Studio: the instance id, Ollama: the model name
    ctx: int            # 0 if the runner didn't say
    parallel: int       # 0 if the runner didn't say
    size: int = 0       # bytes; 0 if the runner didn't say
    size_vram: int = 0  # bytes of it on the GPU; 0 if the runner didn't say


class Runner(Protocol):
    name: str        # "LM Studio" / "Ollama", used in messages
    list_hint: str   # how to see the downloaded models' keys, e.g. "run `lms ls`"
    cli: str         # the command-line program --check looks for, e.g. "LM Studio's lms command"

    @property
    def on_this_computer(self) -> bool:
        """Whether the runner is on this computer (so it can be started and its GPU checked)."""
        ...

    @property
    def url(self) -> str: ...

    def start(self) -> bool:
        """Make sure the server is running. Returns True if this run started it."""
        ...

    def stop(self) -> None: ...

    def find_cli(self) -> str:
        """Where `cli` is, for --check. Raises TranscriptReportError if it's missing."""
        ...

    def model_keys(self) -> list[str]: ...

    def has_model(self, key: str) -> bool:
        """Whether the model is downloaded, by the runner's own rules for names."""
        ...

    def loaded(self, key: str) -> LoadedModel | None:
        """An already-loaded copy of the model, or None."""
        ...

    def load(self, key: str, ctx: int) -> LoadedModel:
        """Load the model. The result has the context it was really loaded with."""
        ...

    def unload(self, model_id: str) -> bool:
        """Unload the model. Returns False (after a warning) if it may still be loaded."""
        ...

    def chat(self, model_id: str, opts: RunOptions, system: str, user: str,
             max_tokens: int, label: str) -> str:
        """Send one request and return the reply text, with any reasoning removed."""
        ...


def make_runner(kind: str, port: int | None, url: str, token: str) -> Runner:
    """The runner for `kind`: on this computer (on `port`, or its usual one), or at `url`."""
    # Imported here because the runners import LoadedModel from this module.
    from .lmstudio import LMStudioRunner  # noqa: PLC0415
    from .ollama import OllamaRunner  # noqa: PLC0415

    if kind == "lmstudio":
        return LMStudioRunner(port, url, token)
    if kind == "ollama":
        return OllamaRunner(port, url, token)
    raise TranscriptReportError(f"Unknown runner '{kind}'. Known: {', '.join(RUNNERS)}")
