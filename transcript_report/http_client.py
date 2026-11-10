"""JSON over HTTP to a model server's REST API, with failures turned into APIError."""

import json
import urllib.error
import urllib.request
from http.client import HTTPException, HTTPResponse

from .errors import TranscriptReportError


class APIError(TranscriptReportError):
    """A REST call to the model server failed. `status` is the HTTP status, or None if there was no reply."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class JSONClient:
    """A small client for a model server's REST API at `base_url`, called `name`
    ("LM Studio") in messages; `auth_hint` says how to set a token when the server asks for one."""

    def __init__(self, base_url: str, api_token: str = "", name: str = "the model server",
                 auth_hint: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.name = name
        self.auth_hint = auth_hint

    @property
    def url(self) -> str:
        return self.base_url

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
                hint = ("\n" + self.auth_hint if not self.api_token
                        else "\nCheck that the API token is still valid.")
            raise APIError(f"{self.name} error {e.code} on {path}: {error_message(read_error(e))}"
                           + hint, e.code) from None
        except urllib.error.URLError as e:
            raise APIError(f"Can't reach {self.name}'s server at {self.url} ({e.reason}).") from None
        except TimeoutError:
            raise APIError(f"{self.name}'s server didn't answer {path} within {timeout:.0f}s.") from None
        except (OSError, HTTPException) as e:   # e.g. the connection was reset
            raise self.lost_connection(path, e) from None

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
        """Call the REST API and return the JSON reply. Raises APIError if it fails."""
        with self.open(method, path, body, timeout) as r:
            try:
                raw = r.read()
            except (OSError, HTTPException) as e:
                raise self.lost_connection(path, e) from None
        try:
            reply = json.loads(raw or b"{}")
        except ValueError:
            raise APIError(f"{self.name} sent a reply to {path} that isn't JSON: {raw[:200]!r}") from None
        return reply if isinstance(reply, dict) else {}

    def lost_connection(self, path: str, e: Exception) -> APIError:
        return APIError(f"Lost the connection to {self.name} during {path} ({e}).")


def read_error(e: urllib.error.HTTPError) -> bytes:
    """The body of an HTTP error reply (empty if there isn't one), closing the connection."""
    try:
        return e.read() if e.fp else b""
    except (OSError, HTTPException):
        return b""
    finally:
        e.close()


def error_text(err: object) -> str:
    """The message in a server error: {"message": ...} (LM Studio), or a plain value."""
    return err.get("message", str(err)) if isinstance(err, dict) else str(err)


def error_message(raw: bytes) -> str:
    """The server's own error message from an error reply, or the raw text."""
    try:
        return error_text(json.loads(raw).get("error"))
    except (ValueError, AttributeError):
        return raw.decode("utf-8", "replace")
