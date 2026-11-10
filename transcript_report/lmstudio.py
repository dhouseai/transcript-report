"""Starting LM Studio's local server, and loading and unloading the model in it.

LM Studio is used instead of running llama-server directly because its programs are
signed, so Windows Smart App Control lets them run.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import RunOptions
from .errors import TranscriptReportError
from .helpers import NO_WINDOW, finish_progress, progress, say, warn
from .http_client import APIError, JSONClient
from .model_call import chat
from .runner import LoadedModel

LMSTUDIO_PORT = 1234
TOKEN_ENV = "LM_API_TOKEN"   # LM Studio's API token, when settings.json has no "api_token"
LMS_EXE = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if sys.platform == "win32" else "lms")
AUTH_HINT = ("The server requires an API token: create one in LM Studio's server settings and set "
             f"\"api_token\" in settings.json (or the {TOKEN_ENV} environment variable).")


class LMStudio(JSONClient):
    """A small client for LM Studio's REST API: on this computer (127.0.0.1:<port>, with the
    server started and stopped through the `lms` command as needed), or at `remote_url`."""

    def __init__(self, port: int = LMSTUDIO_PORT, remote_url: str = "", api_token: str = ""):
        # api_token: for a server with "Require Authentication" turned on
        super().__init__(remote_url, api_token, "LM Studio", AUTH_HINT)
        self.port = port

    @property
    def on_this_computer(self) -> bool:
        return not self.base_url

    @property
    def url(self) -> str:
        return self.base_url or f"http://127.0.0.1:{self.port}"


class LMStudioRunner:
    """LM Studio as the Runner the command line uses, through the functions below."""

    name = "LM Studio"
    list_hint = "run `lms ls`"
    cli = "LM Studio's lms command"

    def __init__(self, port: int | None = None, url: str = "", token: str = ""):
        token = token or os.environ.get(TOKEN_ENV, "").strip()
        self.client = LMStudio(LMSTUDIO_PORT if port is None else port, url, token)

    @property
    def on_this_computer(self) -> bool:
        return self.client.on_this_computer

    @property
    def url(self) -> str:
        return self.client.url

    def start(self) -> bool:
        return ensure_server(self.client)

    def stop(self) -> None:
        stop_server()

    def find_cli(self) -> str:
        return str(find_lms())

    def model_keys(self) -> list[str]:
        return model_keys(self.client)

    def has_model(self, key: str) -> bool:
        return key in model_keys(self.client)

    def loaded(self, key: str) -> LoadedModel | None:
        return loaded_model(self.client, key)

    def load(self, key: str, ctx: int) -> LoadedModel:
        # Loaded with exactly this context and one request slot (see load_model).
        return LoadedModel(id=load_model(self.client, key, ctx), ctx=ctx, parallel=1)

    def unload(self, model_id: str) -> bool:
        return unload_model(self.client, model_id)

    def chat(self, model_id: str, opts: RunOptions, system: str, user: str,
             max_tokens: int, label: str) -> str:
        return chat(self.client, model_id, opts, system, user, max_tokens, label)


def find_lms() -> Path:
    if LMS_EXE.exists():
        return LMS_EXE
    found = shutil.which("lms")
    if found:
        return Path(found)
    raise TranscriptReportError(f"LM Studio's lms command not found at {LMS_EXE} or on PATH.\n"
                                f"Install LM Studio and open it once so it sets up the command.")


def lms(*cmd: str, timeout: int = 120) -> tuple[int, str]:
    """Run LM Studio's `lms` command. Returns (exit code, output); -1 if it timed out."""
    try:
        r = subprocess.run([str(find_lms()), *cmd], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return -1, f"`lms {' '.join(cmd)}` didn't finish within {timeout}s."
    return r.returncode, r.stdout + r.stderr


def ensure_server(client: LMStudio) -> bool:
    """Make sure LM Studio's server is running and point the client at its port.
    Returns True if this run started it (so it should stop it again). A server on another
    computer can't be started from here, so it's only checked that it answers."""
    if not client.on_this_computer:
        try:
            client.call("GET", "/api/v1/models", timeout=10)
        except APIError as e:
            raise APIError(
                f"{e}\nStart LM Studio's server on that computer, with \"Serve on Local Network\" "
                f"turned on in its server settings, and check server_url / --server-url.", e.status) from None
        return False
    _, out = lms("server", "status", "--json")
    status = {}
    for line in reversed(out.splitlines()):   # the JSON line comes after any "Waking up..." text
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            status = parsed
            break
    if status.get("running"):
        client.port = int(status.get("port") or client.port)
        return False
    code, out = lms("server", "start", "--port", str(client.port))
    if code != 0:
        raise TranscriptReportError(f"Couldn't start LM Studio's server on port {client.port}:\n{out.strip()}")
    return True


def stop_server() -> None:
    code, out = lms("server", "stop")
    if code != 0:
        warn(f"couldn't stop LM Studio's server ({out.strip()}). "
             f"Stop it with `lms server stop`.")


def model_keys(client: LMStudio) -> list[str]:
    """Keys of all the models LM Studio has downloaded."""
    return [m["key"] for m in client.call("GET", "/api/v1/models").get("models", []) if "key" in m]


def loaded_model(client: LMStudio, key: str) -> LoadedModel | None:
    """An already-loaded copy of the model, or None."""
    for m in client.call("GET", "/api/v1/models").get("models", []):
        if m.get("key") == key and m.get("loaded_instances"):
            inst = m["loaded_instances"][0]
            config = inst.get("config") or {}
            return LoadedModel(id=inst.get("id", key),
                               ctx=int(config.get("context_length") or 0),
                               parallel=int(config.get("parallel") or 0))
    return None


def load_model(client: LMStudio, key: str, ctx: int) -> str:
    """Load the model fully on the GPU with flash attention and a single request slot.
    Returns the id to send requests to (LM Studio's instance id) and to unload afterwards."""
    body = {"model": key, "context_length": ctx, "flash_attention": True,
            "offload_kv_cache_to_gpu": True, "parallel": 1}
    progress(f"Loading {key}...")
    t0 = time.monotonic()
    try:
        reply = client.call("POST", "/api/v1/models/load", body, timeout=900)
    except KeyboardInterrupt:
        finish_progress()
        unload_after_interrupted_load(client, key)
        raise
    progress(f"Loading {key}: ready ({time.monotonic() - t0:.0f}s)", end="\n")
    return reply.get("instance_id", key)


def unload_after_interrupted_load(client: LMStudio, key: str, wait_s: int = 5) -> None:
    """Closing the load request makes LM Studio cancel the load, but it may already have
    finished. Look for a loaded copy for a few seconds and unload it."""
    say("Cancelling the model load...")
    try:
        for _ in range(wait_s):
            inst = loaded_model(client, key)
            if inst:
                unload_model(client, inst.id)
                return
            time.sleep(1)
    except (TranscriptReportError, KeyboardInterrupt):
        pass
    say(f"   If LM Studio still shows {key} as loaded, unload it there to free the VRAM.")


def unload_model(client: LMStudio, model_id: str) -> bool:
    """Unload the model. Returns False (after a warning) if it may still be loaded."""
    try:
        client.call("POST", "/api/v1/models/unload", {"instance_id": model_id}, timeout=60)
        return True
    except APIError as e:
        if e.status == 404:
            return True   # not loaded (any more): nothing to free
        warn(f"couldn't unload {model_id} ({e}). Unload it in LM Studio "
             f"to free the VRAM.")
        return False
