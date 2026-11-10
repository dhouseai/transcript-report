"""Ollama's server, loading and unloading the model in it, and its chat requests (/api/chat,
streamed as NDJSON)."""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path

from .config import RunOptions
from .errors import TranscriptReportError
from .helpers import NO_WINDOW, finish_progress, progress, say, warn
from .http_client import APIError, JSONClient, error_text
from .model_call import Reply, finish_reply, is_context_error, too_long
from .runner import LoadedModel

OLLAMA_PORT = 11434
KEEP_ALIVE = "30m"    # how long Ollama keeps the model loaded after a request, if it isn't unloaded
MIN_KEEP_ALIVE_S = 300
START_WAIT_S = 20     # how long a server this run starts has to answer
if sys.platform == "win32":
    OLLAMA_EXE = (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
                  / "Programs" / "Ollama" / "ollama.exe")
else:
    OLLAMA_EXE = Path("/usr/local/bin/ollama")
AUTH_HINT = ("Ollama itself has no API tokens, so this is probably a proxy in front of it: set "
             "\"api_token\" in settings.json to the token it expects (it's only sent when \"runner\" is "
             "\"ollama\").")


class Ollama(JSONClient):
    """A small client for Ollama's REST API: on this computer (127.0.0.1:<port>), or at `remote_url`."""

    def __init__(self, port: int = OLLAMA_PORT, remote_url: str = "", api_token: str = ""):
        # api_token: only for a server behind a proxy that asks for one
        super().__init__(remote_url, api_token, "Ollama", AUTH_HINT)
        self.port = port

    @property
    def on_this_computer(self) -> bool:
        return not self.base_url

    @property
    def url(self) -> str:
        return self.base_url or f"http://127.0.0.1:{self.port}"


class OllamaRunner:
    """Ollama as the Runner the command line uses."""

    name = "Ollama"
    list_hint = "run `ollama list`"
    cli = "Ollama's ollama command"

    def __init__(self, port: int | None = None, url: str = "", token: str = ""):
        self.client = Ollama(OLLAMA_PORT if port is None else port, url, token)
        self.server: subprocess.Popen | None = None   # the `ollama serve` this run started
        self.expiry: dict[str, datetime | None] = {}    # when copies loaded by someone else expire

    @property
    def on_this_computer(self) -> bool:
        return self.client.on_this_computer

    @property
    def url(self) -> str:
        return self.client.url

    def start(self) -> bool:
        """Make sure Ollama's server is running. Returns True if this run started it. A server
        on another computer can't be started from here, so it's only checked that it answers."""
        if not self.on_this_computer:
            try:
                self.client.call("GET", "/api/version", timeout=10)
            except APIError as e:
                raise APIError(
                    f"{e}\nStart Ollama on that computer with OLLAMA_HOST=0.0.0.0 set, so it accepts "
                    f"connections from other computers, and check server_url / --server-url.", e.status) from None
            return False
        if server_answers(self.client):
            return False
        progress("Starting Ollama's server...")
        # The server this run starts listens only on this computer, on the port the client calls.
        env = {**os.environ, "OLLAMA_HOST": f"127.0.0.1:{self.client.port}"}
        # A new session on macOS and Linux, so Ctrl+C reaches only this program, which then
        # unloads the model before stopping the server. CREATE_NO_WINDOW does that on Windows.
        self.server = subprocess.Popen([str(find_ollama()), "serve"], env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=NO_WINDOW, start_new_session=True)
        try:
            self.wait_for_server(self.server)
        except BaseException:
            # Callers stop the server only once start() has returned True, and Ctrl+C can come
            # before that; detached as it is, the server would otherwise outlive this program.
            finish_progress()
            self.stop()
            raise
        progress("Starting Ollama's server: ready", end="\n")
        return True

    def wait_for_server(self, server: subprocess.Popen) -> None:
        deadline = time.monotonic() + START_WAIT_S
        while time.monotonic() < deadline:
            code = server.poll()
            if code is not None:
                raise TranscriptReportError(f"`ollama serve` stopped straight away (exit code {code}). "
                                            f"Run it yourself to see why.")
            if server_answers(self.client):
                return
            time.sleep(0.5)
        raise TranscriptReportError(f"Started `ollama serve`, but it didn't answer at {self.url} "
                                    f"within {START_WAIT_S}s.")

    def stop(self) -> None:
        """Stop the server this run started; one that was already running is left alone."""
        if self.server is None:
            return
        server, self.server = self.server, None
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()

    def find_cli(self) -> str:
        return str(find_ollama())

    def version(self) -> str:
        """The server's version, for --check's Ollama line (so not part of Runner)."""
        return str(self.client.call("GET", "/api/version").get("version") or "unknown")

    def model_keys(self) -> list[str]:
        return model_keys(self.client)

    def has_model(self, key: str) -> bool:
        return any(same_model(name, key) for name in model_keys(self.client))

    def loaded(self, key: str) -> LoadedModel | None:
        entry = find_loaded(self.client, key)
        if entry is None:
            return None
        loaded = as_loaded_model(entry)
        self.expiry[loaded.id] = parse_expiry(entry.get("expires_at"))
        return loaded

    def load(self, key: str, ctx: int) -> LoadedModel:
        loaded = load_model(self.client, key, ctx)
        self.expiry.pop(loaded.id, None)
        return loaded

    def unload(self, model_id: str) -> bool:
        self.expiry.pop(model_id, None)
        return unload_model(self.client, model_id)

    def chat(self, model_id: str, opts: RunOptions, system: str, user: str,
             max_tokens: int, label: str) -> str:
        # Every request sets how long the model stays loaded after it. A copy this run didn't
        # load keeps the expiry it had, so the time left is worked out afresh for each request.
        keep_alive = keep_alive_left(self.expiry[model_id]) if model_id in self.expiry else KEEP_ALIVE
        reply = stream_chat(self.client, model_id, opts, system, user, max_tokens, label, keep_alive)
        return finish_reply(reply, opts, max_tokens, label)


def find_ollama() -> Path:
    found = shutil.which("ollama")
    if found:
        return Path(found)
    if OLLAMA_EXE.exists():
        return OLLAMA_EXE
    raise TranscriptReportError(f"Ollama's ollama command not found on PATH or at {OLLAMA_EXE}.\n"
                                f"Install Ollama from ollama.com.")


def server_answers(client: Ollama) -> bool:
    try:
        client.call("GET", "/api/version", timeout=5)
    except APIError:
        return False
    return True


def full_name(name: str) -> str:
    """A model name with its tag: Ollama takes a bare `qwen3` to mean `qwen3:latest`."""
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


def same_model(a: str, b: str) -> bool:
    return full_name(a) == full_name(b)


def model_keys(client: Ollama) -> list[str]:
    """Names of all the models Ollama has downloaded, as it lists them (with their tags)."""
    names = (m.get("name") or m.get("model") for m in client.call("GET", "/api/tags").get("models", []))
    return [name for name in names if name]


def find_loaded(client: Ollama, key: str) -> dict | None:
    """The model's entry in /api/ps (its loaded copy), or None."""
    for m in client.call("GET", "/api/ps").get("models", []):
        name = m.get("name") or m.get("model") or ""
        if name and same_model(name, key):
            return m
    return None


def as_loaded_model(entry: dict) -> LoadedModel:
    return LoadedModel(id=entry.get("name") or entry["model"], ctx=int(entry.get("context_length") or 0),
                       parallel=0, size=int(entry.get("size") or 0), size_vram=int(entry.get("size_vram") or 0))


def loaded_model(client: Ollama, key: str) -> LoadedModel | None:
    """The loaded copy of the model, or None."""
    entry = find_loaded(client, key)
    return as_loaded_model(entry) if entry else None


_EXPIRES_RE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)")


def parse_expiry(expires_at: object) -> datetime | None:
    """An `expires_at` from /api/ps, or None if it can't be read."""
    m = _EXPIRES_RE.fullmatch(expires_at.strip()) if isinstance(expires_at, str) else None
    if not m:
        return None
    # Go writes up to 9 decimal places; fromisoformat on Python 3.10 takes exactly 6.
    fraction = (m[2] or "")[:6].ljust(6, "0")
    try:
        return datetime.fromisoformat(f"{m[1]}.{fraction}{'+00:00' if m[3] == 'Z' else m[3]}")
    except ValueError:
        return None


def keep_alive_left(expiry: datetime | None) -> str | int:
    """keep_alive for a copy loaded by someone else that expires at `expiry`: the seconds until
    then, -1 if that's over a year away (kept loaded for good), or KEEP_ALIVE if it isn't known."""
    if expiry is None:
        return KEEP_ALIVE
    left = (expiry - datetime.now(timezone.utc)).total_seconds()
    if left > 365 * 24 * 3600:
        return -1
    # At least a few minutes, so a copy about to expire isn't unloaded between this run's requests.
    return max(int(left), MIN_KEEP_ALIVE_S)


def load_model(client: Ollama, key: str, ctx: int) -> LoadedModel:
    """Load the model with this context, and warn if Ollama capped the context or put part of
    the model on the CPU. The result has the context it was really loaded with."""
    progress(f"Loading {key}...")
    t0 = time.monotonic()
    try:
        client.call("POST", "/api/generate", {"model": key, "keep_alive": KEEP_ALIVE, "options": {"num_ctx": ctx}},
                    timeout=900)
        progress(f"Loading {key}: ready ({time.monotonic() - t0:.0f}s)", end="\n")
        loaded = loaded_model(client, key)
    except KeyboardInterrupt:
        finish_progress()
        unload_after_interrupted_load(client, key)
        raise
    if loaded is None:
        warn(f"Ollama doesn't list {key} as loaded; assuming a {ctx:,}-token context.")
        return LoadedModel(id=key, ctx=ctx, parallel=0)
    if 0 < loaded.ctx < ctx:
        warn(f"Ollama capped the context at {loaded.ctx:,}, the model's maximum.")
    if loaded.size and loaded.size_vram < loaded.size:
        on_cpu = (loaded.size - loaded.size_vram) / loaded.size
        warn(f"{on_cpu:.0%} of the model is on the CPU; expect it to be slow. Lower --ctx to fit more of it "
             f"on the GPU.")
    return loaded


def unload_after_interrupted_load(client: Ollama, key: str, wait_s: int = 5) -> None:
    """Ollama may finish loading the model after the load request is closed. Look for a loaded
    copy for a few seconds and unload it."""
    say("Cancelling the model load...")
    try:
        for _ in range(wait_s):
            loaded = loaded_model(client, key)
            if loaded:
                unload_model(client, loaded.id)
                return
            time.sleep(1)
    except (TranscriptReportError, KeyboardInterrupt):
        pass
    say(f"   If Ollama still has {key} loaded, it unloads it by itself within 30 minutes.")


def unload_model(client: Ollama, model_id: str) -> bool:
    """Unload the model. Returns False (after a warning) if it may still be loaded."""
    try:
        client.call("POST", "/api/generate", {"model": model_id, "keep_alive": 0}, timeout=60)
        return True
    except APIError as e:
        if e.status == 404:
            return True   # no such model, so nothing is loaded
        warn(f"couldn't unload {model_id} ({e}). Ollama unloads it by itself within 30 minutes.")
        return False


def parse_ndjson(lines: Iterable[bytes]) -> Iterator[dict]:
    """The JSON objects in a newline-delimited JSON stream."""
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            raise TranscriptReportError(f"Ollama sent a stream line that isn't JSON: {line[:200]}") from None
        if isinstance(msg, dict):
            yield msg


def collect(chunks: Iterable[dict], label: str) -> tuple[list[str], dict | None]:
    """Read the stream: returns (message pieces, the done chunk or None if the stream closed
    without one). Shows progress while it reads."""
    out: list[str] = []
    thinking = 0
    t0 = last = time.monotonic()
    progress(f"{label}: reading input...")
    for msg in chunks:
        if "error" in msg:
            raise TranscriptReportError(f"Ollama error: {error_text(msg['error'])}")
        message = msg.get("message") or {}
        if message.get("thinking"):
            thinking += 1
        if message.get("content"):
            out.append(message["content"])
        if msg.get("done"):
            return out, msg
        now = time.monotonic()
        if now - last >= 0.5:
            last = now
            if thinking and not out:
                state = f"thinking (~{thinking:,} tokens)"   # chunks, roughly one token each
            else:
                state = f"writing... ~{len(out):,} tokens"
            progress(f"{label}: {state} ({now - t0:.0f}s)")
    return out, None


def stream_chat(client: Ollama, model_id: str, opts: RunOptions, system: str, user: str,
                max_tokens: int, label: str, keep_alive: str | int = KEEP_ALIVE) -> Reply:
    """Send one request to Ollama and read its streamed reply."""
    body = {
        "model": model_id,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": True,
        "think": opts.think,       # always sent: a model that can think does so when it's left out
        "keep_alive": keep_alive,
        # Both default to true, which cuts or shifts an input that doesn't fit instead of failing.
        "truncate": False,
        "shift": False,
        # num_ctx every time: without it Ollama picks a context from the VRAM, and reloads the model.
        "options": {"num_ctx": opts.ctx, "num_predict": max_tokens, **opts.sampling},
    }
    t0 = time.monotonic()
    try:
        resp = client.open("POST", "/api/chat", body, timeout=3600)
    except APIError as e:
        if opts.think and e.status == 400 and "does not support thinking" in str(e):
            raise TranscriptReportError(f"{model_id}: this model can't think; run without --think.") from None
        # A plain TranscriptReportError, not an APIError: in a batch, a video too long for the
        # context is skipped, while losing Ollama (an APIError with no status) stops the batch.
        if e.status and is_context_error(str(e)):
            raise TranscriptReportError(f"{too_long(opts)}\n{e}") from None
        raise
    try:
        with resp:
            out, done = collect(parse_ndjson(resp), label)
    except (OSError, HTTPException) as e:   # reset, timeout, Ollama closed mid-reply
        finish_progress()
        raise APIError(f"{label}: lost the connection to Ollama ({e}).") from None

    if done is None:
        finish_progress()
        raise TranscriptReportError(f"{label}: Ollama ended the reply early, without an error. If the input "
                                    f"is long, the likely cause: {too_long(opts)}")

    prompt_tokens = int(done.get("prompt_eval_count") or 0)
    output_tokens = int(done.get("eval_count") or 0)
    hit_limit = done["done_reason"] == "length" if "done_reason" in done else None
    # The reply was cut off by the context, not by max_tokens: it stopped before using its
    # allowance, having filled the context or hit a limit. The backup in case an older Ollama
    # ignores shift: false. A full context counts even when done_reason is "stop", as input and
    # output can't add up to more than the context. A reply that ended by itself with room left,
    # or used its whole allowance, is kept; finish_reply() warns about it.
    if prompt_tokens and output_tokens < max_tokens and (prompt_tokens + output_tokens >= opts.ctx or hit_limit):
        finish_progress()
        raise TranscriptReportError(f"{label}: {too_long(opts)}\n(Ollama read {prompt_tokens:,} tokens of input, "
                                    f"and the reply stopped after {output_tokens:,} of its {max_tokens:,}.)")
    eval_ns = done.get("eval_duration") or 0
    return Reply(
        pieces=out,
        final_text="",   # Ollama sends the text only in pieces
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens or len(out),
        tokens_per_s=output_tokens / (eval_ns / 1e9) if output_tokens and eval_ns else None,
        hit_limit=hit_limit,
        seconds=time.monotonic() - t0)
