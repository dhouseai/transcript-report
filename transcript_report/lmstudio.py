"""Starting LM Studio's local server, and loading and unloading the model in it.

LM Studio is used instead of running llama-server directly because its programs are
signed, so Windows Smart App Control lets them run.
"""

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.client import HTTPException, HTTPResponse
from pathlib import Path

from .config import DEFAULT_PORT, TOKEN_ENV
from .errors import TranscriptReportError
from .helpers import NO_WINDOW, finish_progress, progress, say, warn

LMS_EXE = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if sys.platform == "win32" else "lms")


class APIError(TranscriptReportError):
    """An LM Studio REST call failed. `status` is the HTTP status, or None if there was no reply."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class LoadedInstance:
    id: str
    ctx: int        # 0 if LM Studio didn't say
    parallel: int   # 0 if LM Studio didn't say


class LMStudio:
    """A small client for LM Studio's REST API: on this computer (127.0.0.1:<port>, with the
    server started and stopped through the `lms` tool as needed), or at `remote_url`."""

    def __init__(self, port: int = DEFAULT_PORT, remote_url: str = "", api_token: str = ""):
        self.port = port
        self.remote_url = remote_url.rstrip("/")
        self.api_token = api_token   # for a server with "Require Authentication" turned on

    @property
    def local(self) -> bool:
        return not self.remote_url

    @property
    def url(self) -> str:
        return self.remote_url or f"http://127.0.0.1:{self.port}"

    def request(self, method: str, path: str, body: dict | None = None) -> urllib.request.Request:
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return urllib.request.Request(
            self.url + path, method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=headers)

    def open(self, method: str, path: str, body: dict | None = None,
             timeout: float = 30) -> HTTPResponse:
        """Send a request and return the open response. Raises APIError if it fails."""
        try:
            return urllib.request.urlopen(self.request(method, path, body), timeout=timeout)
        except urllib.error.HTTPError as e:
            hint = ""
            if e.code in (401, 403):
                hint = ("\nThe server requires an API token: create one in LM Studio's server "
                        "settings and set \"lmstudio_api_token\" in settings.json (or the "
                        f"{TOKEN_ENV} environment variable)."
                        if not self.api_token else "\nCheck that the API token is still valid.")
            raise APIError(f"LM Studio error {e.code} on {path}: {error_message(read_error(e))}"
                           + hint, e.code) from None
        except urllib.error.URLError as e:
            raise APIError(f"Can't reach LM Studio's server at {self.url} ({e.reason}).") from None
        except TimeoutError:
            raise APIError(f"LM Studio's server didn't answer {path} within {timeout:.0f}s.") from None
        except (OSError, HTTPException) as e:   # e.g. the connection was reset
            raise lost_connection(path, e) from None

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
        """Call the REST API and return the JSON reply. Raises APIError if it fails."""
        with self.open(method, path, body, timeout) as r:
            try:
                raw = r.read()
            except (OSError, HTTPException) as e:
                raise lost_connection(path, e) from None
        try:
            reply = json.loads(raw or b"{}")
        except ValueError:
            raise APIError(f"LM Studio sent a reply to {path} that isn't JSON: {raw[:200]!r}") from None
        return reply if isinstance(reply, dict) else {}


def lost_connection(path: str, e: Exception) -> APIError:
    return APIError(f"Lost the connection to LM Studio during {path} ({e}).")


def read_error(e: urllib.error.HTTPError) -> bytes:
    """The body of an HTTP error reply (empty if there isn't one), closing the connection."""
    try:
        return e.read() if e.fp else b""
    except (OSError, HTTPException):
        return b""
    finally:
        e.close()


def error_text(err: object) -> str:
    """The message in an LM Studio error: {"message": ...}, or a plain value."""
    return err.get("message", str(err)) if isinstance(err, dict) else str(err)


def error_message(raw: bytes) -> str:
    """LM Studio's own error message from an error reply, or the raw text."""
    try:
        return error_text(json.loads(raw).get("error"))
    except (ValueError, AttributeError):
        return raw.decode("utf-8", "replace")


def find_lms() -> Path:
    if LMS_EXE.exists():
        return LMS_EXE
    found = shutil.which("lms")
    if found:
        return Path(found)
    raise TranscriptReportError(f"LM Studio's lms tool not found at {LMS_EXE} or on PATH.\n"
                                f"Install LM Studio and open it once so it sets up the tool.")


def lms(*cmd: str, timeout: int = 120) -> tuple[int, str]:
    """Run LM Studio's `lms` command-line tool. Returns (exit code, output); -1 if it timed out."""
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
    if not client.local:
        try:
            client.call("GET", "/api/v1/models", timeout=10)
        except APIError as e:
            raise APIError(
                f"{e}\nStart LM Studio's server on that computer, with \"Serve on Local Network\" "
                f"turned on in its server settings, and check lmstudio_url / --lmstudio-url.", e.status) from None
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


def loaded_instance(client: LMStudio, key: str) -> LoadedInstance | None:
    """An already-loaded copy of the model, or None."""
    for m in client.call("GET", "/api/v1/models").get("models", []):
        if m.get("key") == key and m.get("loaded_instances"):
            inst = m["loaded_instances"][0]
            config = inst.get("config") or {}
            return LoadedInstance(id=inst.get("id", key),
                                  ctx=int(config.get("context_length") or 0),
                                  parallel=int(config.get("parallel") or 0))
    return None


def load_model(client: LMStudio, key: str, ctx: int) -> str:
    """Load the model fully on the GPU with flash attention and a single request slot.
    Returns the instance id to send requests to and to unload afterwards."""
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
            inst = loaded_instance(client, key)
            if inst:
                unload_model(client, inst.id)
                return
            time.sleep(1)
    except (TranscriptReportError, KeyboardInterrupt):
        pass
    say(f"   If LM Studio still shows {key} as loaded, unload it there to free the VRAM.")


def unload_model(client: LMStudio, instance_id: str) -> bool:
    """Unload the model. Returns False (after a warning) if it may still be loaded."""
    try:
        client.call("POST", "/api/v1/models/unload", {"instance_id": instance_id}, timeout=60)
        return True
    except APIError as e:
        if e.status == 404:
            return True   # not loaded (any more): nothing to free
        warn(f"couldn't unload {instance_id} ({e}). Unload it in LM Studio "
             f"to free the VRAM.")
        return False
