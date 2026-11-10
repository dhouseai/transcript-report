"""The interface the command line uses for the program that runs the model (LM Studio)."""

from dataclasses import dataclass
from typing import Protocol

from .config import RunOptions
from .errors import TranscriptReportError

RUNNERS = ("lmstudio",)


@dataclass(frozen=True)
class LoadedModel:
    id: str             # sent with each request and to unload it; LM Studio: the instance id
    ctx: int            # 0 if the runner didn't say
    parallel: int       # 0 if the runner didn't say
    size: int = 0       # bytes; 0 if the runner didn't say
    size_vram: int = 0  # bytes of it on the GPU; 0 if the runner didn't say


class Runner(Protocol):
    name: str        # "LM Studio", used in messages
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
    from .lmstudio import LMStudioRunner

    if kind == "lmstudio":
        return LMStudioRunner(port, url, token)
    raise TranscriptReportError(f"Unknown runner '{kind}'. Known: {', '.join(RUNNERS)}")
