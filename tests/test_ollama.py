import contextlib
import email.message
import io
import json
import subprocess
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from transcript_report import ollama
from transcript_report.config import RunOptions
from transcript_report.errors import TranscriptReportError
from transcript_report.http_client import APIError
from transcript_report.ollama import Ollama, OllamaRunner
from transcript_report.runner import LoadedModel, make_runner


class Server:
    """Stands in for urlopen(): answers "METHOD /path" from `routes` with a dict (sent as
    JSON), a ready response, an exception to raise, or a list of those in turn. Records each
    request as (route, body)."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[tuple[str, dict | None]] = []

    def __call__(self, req, timeout=None):
        route = f"{req.get_method()} {urllib.parse.urlsplit(req.full_url).path}"
        self.requests.append((route, json.loads(req.data) if req.data else None))
        answer = self.routes[route]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return io.BytesIO(json.dumps(answer).encode("utf-8")) if isinstance(answer, dict) else answer

    def bodies(self, route):
        return [body for r, body in self.requests if r == route]


def serve(routes):
    server = Server(routes)
    return server, mock.patch("urllib.request.urlopen", side_effect=server)


def http_error(code, message):
    body = io.BytesIO(json.dumps({"error": message}).encode("utf-8"))
    return urllib.error.HTTPError("http://x", code, "err", email.message.Message(), body)


def ndjson(*chunks):
    """A streamed reply holding these chunks, as Ollama sends them."""
    return io.BytesIO(b"".join(json.dumps(c).encode("utf-8") + b"\n" for c in chunks))


def text(content):
    return {"message": {"role": "assistant", "content": content}, "done": False}


def thought(thinking):
    return {"message": {"role": "assistant", "content": "", "thinking": thinking}, "done": False}


def done(prompt_tokens=100, tokens=10, reason="stop", eval_ns=2_000_000_000):
    return {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": reason,
            "prompt_eval_count": prompt_tokens, "eval_count": tokens, "eval_duration": eval_ns}


REFUSED = urllib.error.URLError("refused")


class Client(unittest.TestCase):
    def test_local_and_remote_urls(self):
        self.assertEqual(Ollama().url, "http://127.0.0.1:11434")
        remote = Ollama(remote_url="http://pc:11434/")
        self.assertFalse(remote.on_this_computer)
        self.assertEqual(remote.url, "http://pc:11434")

    def test_token_is_sent_only_when_set(self):
        self.assertNotIn("Authorization", Ollama().request("GET", "/x").headers)
        self.assertEqual(Ollama(api_token="abc").request("GET", "/x").get_header("Authorization"), "Bearer abc")

    def test_lm_studios_token_is_not_sent(self):
        with mock.patch.dict("os.environ", {"LM_API_TOKEN": "lm-studio-token"}):
            self.assertNotIn("Authorization", OllamaRunner().client.request("GET", "/x").headers)

    def test_error_names_ollama_and_gives_its_message(self):
        _, urlopen = serve({"POST /api/generate": http_error(404, 'model "nope" not found')})
        with urlopen, self.assertRaises(APIError) as cm:
            Ollama().call("POST", "/api/generate", {})
        self.assertEqual(str(cm.exception), 'Ollama error 404 on /api/generate: model "nope" not found')


class Runner(unittest.TestCase):
    def test_make_runner(self):
        runner = make_runner("ollama", None, "", "")
        self.assertIsInstance(runner, OllamaRunner)
        self.assertEqual((runner.name, runner.list_hint, runner.cli),
                         ("Ollama", "run `ollama list`", "Ollama's ollama command"))
        self.assertEqual(runner.url, "http://127.0.0.1:11434")
        self.assertEqual(make_runner("ollama", 5000, "", "").url, "http://127.0.0.1:5000")
        self.assertFalse(make_runner("ollama", None, "http://pc:11434", "").on_this_computer)

    def test_version(self):
        _, urlopen = serve({"GET /api/version": [{"version": "0.12.0"}, {}]})
        with urlopen:
            self.assertEqual(OllamaRunner().version(), "0.12.0")
            self.assertEqual(OllamaRunner().version(), "unknown")


class StartStop(unittest.TestCase):
    """start() and stop() with `ollama serve` and the /api/version poll faked."""

    def start(self, runner, versions, poll=None):
        server, urlopen = serve({"GET /api/version": versions})
        process = mock.Mock()
        process.poll.return_value = poll
        with urlopen, mock.patch.object(ollama, "find_ollama", return_value=Path("ollama")), \
             mock.patch("subprocess.Popen", return_value=process) as popen, \
             mock.patch("time.sleep"), contextlib.redirect_stderr(io.StringIO()):
            started = runner.start()
        return started, popen, process

    def test_running_server_is_used_and_left_running(self):
        runner = OllamaRunner()
        started, popen, _ = self.start(runner, [{"version": "0.12.0"}])
        self.assertFalse(started)
        popen.assert_not_called()
        runner.stop()   # nothing to stop

    def test_stopped_server_is_started_then_stopped(self):
        runner = OllamaRunner(5000)
        started, popen, process = self.start(runner, [REFUSED, REFUSED, {"version": "0.12.0"}])
        self.assertTrue(started)
        self.assertEqual(popen.call_args.args[0], ["ollama", "serve"])
        self.assertEqual(popen.call_args.kwargs["env"]["OLLAMA_HOST"], "127.0.0.1:5000")
        runner.stop()
        process.terminate.assert_called_once()
        process.kill.assert_not_called()
        runner.stop()   # only once
        process.terminate.assert_called_once()

    def test_server_that_wont_stop_is_killed(self):
        runner = OllamaRunner()
        _, _, process = self.start(runner, [REFUSED, {"version": "0.12.0"}])
        process.wait.side_effect = [subprocess.TimeoutExpired("ollama", 10), 0]
        runner.stop()
        process.kill.assert_called_once()

    def test_serve_exiting_at_once(self):
        with self.assertRaisesRegex(TranscriptReportError, "exit code 1"):
            self.start(OllamaRunner(), REFUSED, poll=1)

    def test_ctrl_c_while_waiting_stops_the_server(self):
        runner = OllamaRunner()
        process = mock.Mock()
        process.poll.return_value = None
        _, urlopen = serve({"GET /api/version": [REFUSED, REFUSED]})
        with urlopen, mock.patch.object(ollama, "find_ollama", return_value=Path("ollama")), \
             mock.patch("subprocess.Popen", return_value=process), \
             mock.patch("time.sleep", side_effect=KeyboardInterrupt), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            runner.start()
        process.terminate.assert_called_once()
        self.assertIsNone(runner.server)

    def test_server_that_never_answers_is_stopped(self):
        runner = OllamaRunner()
        with mock.patch.object(ollama, "START_WAIT_S", 0), \
             self.assertRaisesRegex(TranscriptReportError, "didn't answer at http://127.0.0.1:11434"):
            self.start(runner, [REFUSED])
        self.assertIsNone(runner.server)

    def test_remote_server_is_only_checked(self):
        started, popen, _ = self.start(OllamaRunner(url="http://pc:11434"), [{"version": "0.12.0"}])
        self.assertFalse(started)
        popen.assert_not_called()

    def test_unreachable_remote_server_says_how_to_fix_it(self):
        with self.assertRaises(APIError) as cm:
            self.start(OllamaRunner(url="http://pc:11434"), [REFUSED])
        self.assertIsNone(cm.exception.status)   # so a batch stops rather than skipping a video
        self.assertIn("OLLAMA_HOST=0.0.0.0", str(cm.exception))

    def test_missing_command(self):
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.object(ollama, "OLLAMA_EXE", Path("no/such/ollama")), \
             self.assertRaisesRegex(TranscriptReportError, "ollama command not found .*\n.*ollama.com"):
            OllamaRunner().find_cli()


PS = {"models": [{"name": "qwen3:latest", "model": "qwen3:latest", "size": 8_000, "size_vram": 8_000,
                  "context_length": 16384, "expires_at": "2026-11-10T12:00:00Z"}]}


class Models(unittest.TestCase):
    def test_name_matching(self):
        for a, b, same in (("qwen3", "qwen3:latest", True), ("qwen3:8b", "qwen3", False),
                           ("user/m", "user/m:latest", True), ("host:5000/m", "host:5000/m:latest", True)):
            with self.subTest(a=a, b=b):
                self.assertEqual(ollama.same_model(a, b), same)

    def test_keys_are_listed_as_ollama_names_them(self):
        tags = {"models": [{"name": "qwen3:latest"}, {"model": "llama3.2:3b"}, {"no name": True}]}
        _, urlopen = serve({"GET /api/tags": tags})
        with urlopen:
            self.assertEqual(OllamaRunner().model_keys(), ["qwen3:latest", "llama3.2:3b"])

    def test_has_model_accepts_a_bare_name_for_latest(self):
        _, urlopen = serve({"GET /api/tags": {"models": [{"name": "qwen3:latest"}, {"name": "llama3.2:3b"}]}})
        with urlopen:
            runner = OllamaRunner()
            for key, found in (("qwen3", True), ("qwen3:latest", True), ("llama3.2:3b", True),
                               ("llama3.2", False), ("qwen3:8b", False)):
                with self.subTest(key=key):
                    self.assertEqual(runner.has_model(key), found)

    def test_loaded_copy_from_ps(self):
        _, urlopen = serve({"GET /api/ps": PS})
        with urlopen:
            runner = OllamaRunner()
            self.assertEqual(runner.loaded("qwen3"),
                             LoadedModel(id="qwen3:latest", ctx=16384, parallel=0, size=8_000, size_vram=8_000))
            self.assertIsNone(runner.loaded("qwen3:8b"))

    def test_unload(self):
        server, urlopen = serve({"POST /api/generate": {"done_reason": "unload"}})
        with urlopen:
            self.assertTrue(OllamaRunner().unload("qwen3:latest"))
        self.assertEqual(server.bodies("POST /api/generate"), [{"model": "qwen3:latest", "keep_alive": 0}])

    def test_unload_treats_not_found_as_done(self):
        _, urlopen = serve({"POST /api/generate": http_error(404, 'model "x" not found')})
        with urlopen:
            self.assertTrue(OllamaRunner().unload("x"))
        _, urlopen = serve({"POST /api/generate": http_error(500, "broken")})
        with urlopen, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(OllamaRunner().unload("x"))
        self.assertIn("unloads it by itself within 30 minutes", err.getvalue())


class Load(unittest.TestCase):
    def load(self, ps, ctx=16384, generate=None):
        server, urlopen = serve({"POST /api/generate": generate or {"done_reason": "load"}, "GET /api/ps": ps})
        with urlopen, contextlib.redirect_stderr(io.StringIO()) as err:
            loaded = OllamaRunner().load("qwen3", ctx)
        return loaded, err.getvalue(), server

    def test_load_request_and_the_context_from_ps(self):
        loaded, err, server = self.load(PS)
        self.assertEqual(server.bodies("POST /api/generate"),
                         [{"model": "qwen3", "keep_alive": "30m", "options": {"num_ctx": 16384}}])
        self.assertEqual((loaded.id, loaded.ctx), ("qwen3:latest", 16384))
        self.assertNotIn("WARNING", err)

    def test_capped_context_warns(self):
        loaded, err, _ = self.load(PS, ctx=65536)
        self.assertEqual(loaded.ctx, 16384)
        self.assertIn("Ollama capped the context at 16,384, the model's maximum", err)

    def test_part_on_the_cpu_warns(self):
        ps = {"models": [{**PS["models"][0], "size_vram": 6_000}]}
        _, err, _ = self.load(ps)
        self.assertIn("25% of the model is on the CPU", err)

    def interrupt_load(self, ps_replies):
        server, urlopen = serve({"POST /api/generate": [KeyboardInterrupt(), {"done_reason": "unload"}],
                                 "GET /api/ps": ps_replies})
        with urlopen, mock.patch("time.sleep"), contextlib.redirect_stderr(io.StringIO()) as err, \
             self.assertRaises(KeyboardInterrupt):
            OllamaRunner().load("qwen3", 16384)
        return server, err.getvalue()

    def test_ctrl_c_during_the_load_unloads_the_copy_once_it_appears(self):
        server, err = self.interrupt_load([{"models": []}, PS])
        self.assertEqual(server.bodies("POST /api/generate")[-1], {"model": "qwen3:latest", "keep_alive": 0})
        self.assertNotIn("30 minutes", err)

    def test_ctrl_c_during_the_load_with_no_copy_appearing_says_it_expires(self):
        server, err = self.interrupt_load([{"models": []}] * 5)
        self.assertEqual(len(server.bodies("POST /api/generate")), 1)   # only the load
        self.assertIn("unloads it by itself within 30 minutes", err)


def expires_in(seconds):
    when = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


class KeepAlive(unittest.TestCase):
    """Requests keep a copy loaded by someone else loaded as long as it already was."""

    def keep_alive_sent(self, expires_at=None, load=False):
        entry = {**PS["models"][0], "expires_at": expires_at}
        server, urlopen = serve({"GET /api/ps": {"models": [entry]}, "POST /api/generate": {},
                                 "POST /api/chat": ndjson(text("Hi"), done())})
        runner = OllamaRunner()
        with urlopen, contextlib.redirect_stderr(io.StringIO()):
            model_id = runner.load("qwen3", 16384).id if load else runner.loaded("qwen3").id
            runner.chat(model_id, RunOptions(ctx=16384), "system", "user", 500, "Test")
        return server.bodies("POST /api/chat")[0]["keep_alive"]

    def test_a_copy_this_run_loaded_gets_the_usual_time(self):
        self.assertEqual(self.keep_alive_sent(expires_in(60), load=True), "30m")

    def test_a_copy_loaded_elsewhere_keeps_its_time_left(self):
        self.assertAlmostEqual(self.keep_alive_sent(expires_in(7200)), 7200, delta=5)

    def test_the_time_left_is_worked_out_for_each_request(self):
        start = datetime.now(timezone.utc)

        class AnHourLater(datetime):
            @classmethod
            def now(cls, tz=None):
                return start + timedelta(hours=1)

        expires = (start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        server, urlopen = serve({"GET /api/ps": {"models": [{**PS["models"][0], "expires_at": expires}]},
                                 "POST /api/chat": [ndjson(text("Hi"), done()), ndjson(text("Hi"), done())]})
        runner = OllamaRunner()
        opts = RunOptions(ctx=16384)
        with urlopen, contextlib.redirect_stderr(io.StringIO()):
            model_id = runner.loaded("qwen3").id
            runner.chat(model_id, opts, "system", "user", 500, "Test")
            with mock.patch.object(ollama, "datetime", AnHourLater):
                runner.chat(model_id, opts, "system", "user", 500, "Test")
        first, second = (body["keep_alive"] for body in server.bodies("POST /api/chat"))
        self.assertAlmostEqual(first, 7200, delta=5)
        self.assertAlmostEqual(second, 3600, delta=5)

    def test_a_copy_kept_for_good_stays_so(self):
        self.assertEqual(self.keep_alive_sent("2318-11-10T12:00:00+01:00"), -1)

    def test_a_copy_about_to_expire_lasts_the_run(self):
        self.assertEqual(self.keep_alive_sent(expires_in(10)), ollama.MIN_KEEP_ALIVE_S)

    def test_an_unreadable_expiry_gets_the_usual_time(self):
        for expires_at in (None, "soon", "2026-13-45T00:00:00Z"):
            with self.subTest(expires_at=expires_at):
                self.assertEqual(self.keep_alive_sent(expires_at), "30m")


class BrokenStream(io.BytesIO):
    """A reply stream whose connection drops after the first chunk."""

    def __iter__(self):
        yield json.dumps(text("Hi")).encode("utf-8") + b"\n"
        raise ConnectionResetError("reset by peer")


class Chat(unittest.TestCase):
    """chat() with Ollama's reply replaced by a canned stream."""

    def send(self, reply, max_tokens=500, think=False):
        server, urlopen = serve({"POST /api/chat": reply})
        opts = RunOptions(ctx=8192, think=think)
        with urlopen, contextlib.redirect_stderr(io.StringIO()) as err:
            out = OllamaRunner().chat("qwen3:latest", opts, "system", "user", max_tokens, "Test")
        self.body = server.bodies("POST /api/chat")[0]
        return out, err.getvalue()

    def test_normal_reply(self):
        out, err = self.send(ndjson(text("Hi"), text(" there"), done()))
        self.assertEqual(out, "Hi there")
        self.assertIn("done - 10 tokens, 5.0 tok/s", err)
        self.assertNotIn("WARNING", err)

    def test_thinking_then_content(self):
        out, _ = self.send(ndjson(thought("Let me see"), thought("..."), text("The answer"), done()), think=True)
        self.assertEqual(out, "The answer")
        self.assertTrue(self.body["think"])

    def test_request_body(self):
        self.send(ndjson(text("Hi"), done()))
        self.assertEqual(self.body["messages"], [{"role": "system", "content": "system"},
                                                 {"role": "user", "content": "user"}])
        self.assertIs(self.body["think"], False)   # sent even without --think
        self.assertIs(self.body["truncate"], False)
        self.assertIs(self.body["shift"], False)
        self.assertEqual((self.body["stream"], self.body["keep_alive"]), (True, "30m"))
        options = self.body["options"]
        self.assertEqual((options["num_ctx"], options["num_predict"], options["temperature"]), (8192, 500, 0.4))

    def test_length_stop_warns_of_a_cut_off(self):
        _, err = self.send(ndjson(text("Part"), done(tokens=500, reason="length")))
        self.assertIn("hit the token limit", err)

    def test_only_thinking_when_cut_off_suggests_dropping_think(self):
        with self.assertRaisesRegex(TranscriptReportError, "ran out of tokens while thinking; try without --think"):
            self.send(ndjson(thought("hmm"), done(tokens=500, reason="length")), think=True)

    def test_error_line(self):
        with self.assertRaisesRegex(TranscriptReportError, "Ollama error: model runner crashed"):
            self.send(ndjson(text("Hi"), {"error": "model runner crashed"}))

    def test_stream_ending_without_done(self):
        with self.assertRaisesRegex(TranscriptReportError, "ended the reply early"):
            self.send(ndjson(text("Hi")))

    def test_no_stop_reason_falls_back_to_the_whole_allowance(self):
        last = done(tokens=500)
        del last["done_reason"]
        _, err = self.send(ndjson(text("Part"), last))
        self.assertIn("hit the token limit", err)

    def test_reply_filling_the_context_is_too_long_and_not_an_api_error(self):
        with self.assertRaises(TranscriptReportError) as cm:
            self.send(ndjson(text("Hi"), done(prompt_tokens=7900, tokens=292, reason="length")))
        self.assertNotIsInstance(cm.exception, APIError)   # so --all skips just this video
        self.assertIn("too long for the 8,192-token context", str(cm.exception))

    def test_stopping_at_a_limit_before_the_allowance_is_too_long(self):
        with self.assertRaisesRegex(TranscriptReportError, "too long"):
            self.send(ndjson(text("Hi"), done(prompt_tokens=3000, tokens=40, reason="length")))

    def test_reply_that_used_its_allowance_as_the_context_filled_is_kept(self):
        out, err = self.send(ndjson(text("Part"), done(prompt_tokens=7692, tokens=500, reason="length")))
        self.assertEqual(out, "Part")
        self.assertIn("hit the token limit", err)

    def test_reply_that_ended_by_itself_near_the_context_is_kept(self):
        out, err = self.send(ndjson(text("Hi"), done(prompt_tokens=7700, tokens=10)))
        self.assertEqual(out, "Hi")
        self.assertIn("nearly filled the context window", err)

    def test_model_that_cant_think(self):
        with self.assertRaises(TranscriptReportError) as cm:
            self.send(http_error(400, '"gemma3:latest" does not support thinking'), think=True)
        self.assertNotIsInstance(cm.exception, APIError)
        self.assertIn("this model can't think; run without --think", str(cm.exception))

    def test_lost_connection_stops_a_batch(self):
        with self.assertRaises(APIError) as cm:
            self.send(BrokenStream())
        self.assertIsNone(cm.exception.status)
        self.assertIn("lost the connection to Ollama", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
