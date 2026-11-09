import io
import json
import unittest
import urllib.error
from unittest import mock

from transcript_report import lmstudio
from transcript_report.lmstudio import APIError, LMStudio


def reply(data):
    """A fake urlopen() response holding `data` as JSON."""
    r = mock.MagicMock()
    r.__enter__.return_value = r
    r.read.return_value = json.dumps(data).encode("utf-8")
    return r


def http_error(code, message):
    body = io.BytesIO(json.dumps({"error": {"message": message}}).encode("utf-8"))
    return urllib.error.HTTPError("http://x", code, "err", {}, body)


class Client(unittest.TestCase):
    def test_token_is_sent_only_when_set(self):
        self.assertNotIn("Authorization", LMStudio().request("GET", "/x").headers)
        req = LMStudio(api_token="abc").request("GET", "/x")
        self.assertEqual(req.get_header("Authorization"), "Bearer abc")

    def test_local_and_remote_urls(self):
        self.assertEqual(LMStudio(4321).url, "http://127.0.0.1:4321")
        remote = LMStudio(remote_url="http://pc:1234/")
        self.assertFalse(remote.local)
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


class Models(unittest.TestCase):
    MODELS = {"models": [
        {"key": "a/one"},
        {"key": "b/two", "loaded_instances": [{"id": "b/two:1",
                                                "config": {"context_length": 16384, "parallel": 4}}]},
        {"no key": True},
    ]}

    def test_keys_and_loaded_instance(self):
        client = LMStudio()
        with mock.patch("urllib.request.urlopen", return_value=reply(self.MODELS)):
            self.assertEqual(lmstudio.model_keys(client), ["a/one", "b/two"])
            self.assertIsNone(lmstudio.loaded_instance(client, "a/one"))
            inst = lmstudio.loaded_instance(client, "b/two")
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
