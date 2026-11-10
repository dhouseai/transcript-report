import email.message
import io
import json
import unittest
import urllib.error
from unittest import mock

from transcript_report import lmstudio
from transcript_report.errors import TranscriptReportError
from transcript_report.lmstudio import APIError, LMStudio, LMStudioRunner
from transcript_report.runner import LoadedModel, make_runner


def reply(data):
    """A fake urlopen() response holding `data` as JSON."""
    r = mock.MagicMock()
    r.__enter__.return_value = r
    r.read.return_value = json.dumps(data).encode("utf-8")
    return r


def http_error(code, message):
    body = io.BytesIO(json.dumps({"error": {"message": message}}).encode("utf-8"))
    return urllib.error.HTTPError("http://x", code, "err", email.message.Message(), body)


class Client(unittest.TestCase):
    def test_token_is_sent_only_when_set(self):
        self.assertNotIn("Authorization", LMStudio().request("GET", "/x").headers)
        req = LMStudio(api_token="abc").request("GET", "/x")
        self.assertEqual(req.get_header("Authorization"), "Bearer abc")

    def test_local_and_remote_urls(self):
        self.assertEqual(LMStudio(4321).url, "http://127.0.0.1:4321")
        remote = LMStudio(remote_url="http://pc:1234/")
        self.assertFalse(remote.on_this_computer)
        self.assertEqual(remote.url, "http://pc:1234")

    def test_unauthorised_explains_the_token(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(401, "Unauthorized")), \
             self.assertRaises(APIError) as cm:
            LMStudio().call("GET", "/api/v1/models")
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("\"api_token\" in settings.json", str(cm.exception))

    def test_no_server(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")), \
             self.assertRaises(APIError) as cm:
            LMStudio().call("GET", "/api/v1/models")
        self.assertIsNone(cm.exception.status)
        self.assertIn("Can't reach LM Studio's server at http://127.0.0.1:1234", str(cm.exception))

    def test_error_names_lm_studio_and_gives_its_message(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(400, "bad model")), \
             self.assertRaises(APIError) as cm:
            LMStudio().call("POST", "/api/v1/models/load")
        self.assertEqual(str(cm.exception), "LM Studio error 400 on /api/v1/models/load: bad model")


class Runner(unittest.TestCase):
    def test_make_runner(self):
        runner = make_runner("lmstudio", None, "", "")
        self.assertIsInstance(runner, LMStudioRunner)
        self.assertEqual(runner.url, "http://127.0.0.1:1234")
        self.assertFalse(make_runner("lmstudio", 1234, "http://pc:1234", "").on_this_computer)
        with self.assertRaisesRegex(TranscriptReportError, "Unknown runner 'other'"):
            make_runner("other", None, "", "")

    def test_load_reports_the_context_it_asked_for(self):
        runner = LMStudioRunner(1234)
        with mock.patch.object(lmstudio, "load_model", return_value="pub/m:2") as load:
            inst = runner.load("pub/m", 16384)
        load.assert_called_once_with(runner.client, "pub/m", 16384)
        self.assertEqual(inst, LoadedModel(id="pub/m:2", ctx=16384, parallel=1))

    def test_start_follows_the_server_to_its_port(self):
        runner = LMStudioRunner(1234)
        with mock.patch.object(lmstudio, "lms", return_value=(0, '{"running": true, "port": 5678}')):
            self.assertFalse(runner.start())
        self.assertEqual(runner.url, "http://127.0.0.1:5678")


class Models(unittest.TestCase):
    MODELS = {"models": [
        {"key": "a/one"},
        {"key": "b/two", "loaded_instances": [{"id": "b/two:1",
                                                "config": {"context_length": 16384, "parallel": 4}}]},
        {"no key": True},
    ]}

    def test_keys_and_loaded_model(self):
        client = LMStudio()
        with mock.patch("urllib.request.urlopen", return_value=reply(self.MODELS)):
            self.assertEqual(lmstudio.model_keys(client), ["a/one", "b/two"])
            self.assertIsNone(lmstudio.loaded_model(client, "a/one"))
            inst = lmstudio.loaded_model(client, "b/two")
        assert inst is not None
        self.assertEqual((inst.id, inst.ctx, inst.parallel), ("b/two:1", 16384, 4))

    def test_unload_treats_not_loaded_as_done(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(404, "not loaded")):
            self.assertTrue(lmstudio.unload_model(LMStudio(), "x"))
        with mock.patch("urllib.request.urlopen", side_effect=http_error(500, "broken")), \
             mock.patch("sys.stderr", io.StringIO()):
            self.assertFalse(lmstudio.unload_model(LMStudio(), "x"))


class EnsureServer(unittest.TestCase):
    def test_running_server_is_used_on_its_port(self):
        client = LMStudio(1234)
        status = 'Waking up LM Studio...\n{"running": true, "port": 5678}\n'
        with mock.patch.object(lmstudio, "lms", return_value=(0, status)) as lms:
            self.assertFalse(lmstudio.ensure_server(client))
        self.assertEqual(client.port, 5678)
        lms.assert_called_once()

    def test_stopped_server_is_started(self):
        replies = [(0, '{"running": false}'), (0, "started")]
        with mock.patch.object(lmstudio, "lms", side_effect=replies) as lms:
            self.assertTrue(lmstudio.ensure_server(LMStudio(1234)))
        self.assertEqual(lms.call_args.args, ("server", "start", "--port", "1234"))

    def test_start_failure(self):
        replies = [(0, "not json"), (1, "port in use")]
        with mock.patch.object(lmstudio, "lms", side_effect=replies), \
             self.assertRaises(lmstudio.TranscriptReportError) as cm:
            lmstudio.ensure_server(LMStudio(1234))
        self.assertIn("port in use", str(cm.exception))

    def test_unreachable_remote_server_says_how_to_fix_it(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")), \
             self.assertRaises(APIError) as cm:
            lmstudio.ensure_server(LMStudio(remote_url="http://pc:1234"))
        self.assertIsNone(cm.exception.status)   # so a batch stops rather than skipping a video
        self.assertIn("Serve on Local Network", str(cm.exception))
        self.assertIn("server_url / --server-url", str(cm.exception))

    def test_remote_server_is_only_checked(self):
        with mock.patch.object(lmstudio, "lms") as lms, \
             mock.patch("urllib.request.urlopen", return_value=reply({"models": []})):
            self.assertFalse(lmstudio.ensure_server(LMStudio(remote_url="http://pc:1234")))
        lms.assert_not_called()


if __name__ == "__main__":
    unittest.main()
