import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from transcript_report import check, lmstudio, ollama
from transcript_report.config import ModelEntry, Settings


def settings(model: ModelEntry | None = None, runner: str = "lmstudio") -> Settings:
    """Settings whose default model "m" is `model` (an LM Studio key by default)."""
    return Settings(Path("t"), Path("d"), Path("r"), default_model="m", models={"m": model or ModelEntry("pub/m")},
                    source=Path("settings.json"), runner=runner)


def run_check(keys=("pub/m",), browser: Path | None = Path("edge.exe"), url_arg: str | None = None,
              model_arg: str | None = None):
    """run_check() with LM Studio, the browser and the GPU faked."""
    with mock.patch.object(check, "load_settings", return_value=settings()), \
         mock.patch.object(lmstudio, "find_lms", return_value=Path("lms")), \
         mock.patch.object(lmstudio, "ensure_server", return_value=True), \
         mock.patch.object(lmstudio, "model_keys", return_value=list(keys)), \
         mock.patch.object(lmstudio, "stop_server") as stop, \
         mock.patch.object(check, "find_browser", return_value=browser), \
         mock.patch.object(check, "vram_info", return_value=(1000, 12000)), \
         contextlib.redirect_stdout(io.StringIO()) as out:
        code = check.run_check(model_arg, None, None, url_arg)
    return code, out.getvalue(), stop


def run_ollama_check(s: Settings, runner_arg: str | None = None, port: int | None = None,
                     keys=("qwen3:14b",)):
    """run_check() on this computer with Ollama already running and the browser and the GPU faked."""
    with mock.patch.object(check, "load_settings", return_value=s), \
         mock.patch.object(ollama, "find_ollama", return_value=Path("ollama")), \
         mock.patch.object(ollama, "server_answers", return_value=True), \
         mock.patch.object(ollama, "model_keys", return_value=list(keys)), \
         mock.patch.object(ollama.OllamaRunner, "version", return_value="0.12.0"), \
         mock.patch.object(check, "find_browser", return_value=Path("edge.exe")), \
         mock.patch.object(check, "vram_info", return_value=(1000, 12000)), \
         contextlib.redirect_stdout(io.StringIO()) as out:
        code = check.run_check(None, runner_arg, port, None)
    return code, out.getvalue()


class RunCheck(unittest.TestCase):
    def test_all_set(self):
        code, out, stop = run_check()
        self.assertEqual(code, 0)
        self.assertIn("[  ok  ] LM Studio's lms command: lms", out)
        self.assertIn("[  ok  ] LM Studio server: http://127.0.0.1:1234 (started for this check)", out)
        self.assertIn("[  ok  ] model downloaded: pub/m", out)
        self.assertNotIn("version", out)
        self.assertIn("All set.", out)
        stop.assert_called_once()   # the server was started for the check

    def test_missing_model_fails(self):
        code, out, _ = run_check(keys=["other/model"])
        self.assertEqual(code, 1)
        self.assertIn("[ FAIL ] model downloaded", out)

    def test_a_name_not_in_models_is_said_to_be_taken_as_a_key(self):
        # e.g. --model ol-think before it's added to settings.json
        _, out, _ = run_check(model_arg="ol-think")
        self.assertIn("[  ok  ] model setting: ol-think isn't in \"models\" in settings.json, so it's taken as a "
                      "model key", out)
        self.assertIn("[  ok  ] model setting: m -> pub/m", run_check()[1])

    def test_server_on_another_computer(self):
        code, out, _ = run_check(url_arg="http://pc:1234")
        self.assertEqual(code, 0)
        self.assertIn("[  ok  ] LM Studio server: http://pc:1234 (on another computer)", out)
        self.assertNotIn("lms command", out)

    def test_no_browser_is_only_a_warning_for_markdown(self):
        code, out, _ = run_check(browser=None)
        self.assertEqual(code, 0)
        self.assertIn("[ warn ] PDF browser", out)


class OllamaCheck(unittest.TestCase):
    """--check builds the runner chosen by --runner, the model or the setting."""

    def test_models_runner(self):
        code, out = run_ollama_check(settings(ModelEntry("qwen3:14b", runner="ollama")))
        self.assertEqual(code, 0)
        self.assertIn("[  ok  ] Ollama's ollama command: ollama", out)
        self.assertIn("[  ok  ] Ollama server: http://127.0.0.1:11434 (already running)", out)
        self.assertIn("[  ok  ] Ollama version: 0.12.0", out)
        self.assertIn("[  ok  ] model downloaded: qwen3:14b", out)

    def test_runner_setting_and_port(self):
        _, out = run_ollama_check(settings(ModelEntry("qwen3:14b"), runner="ollama"), port=5000)
        self.assertIn("Ollama server: http://127.0.0.1:5000", out)

    def test_bare_name_is_found_and_the_list_shows_each_model_once(self):
        keys = ["qwen3:latest", "llama3.2:3b"]
        code, out = run_ollama_check(settings(ModelEntry("qwen3", runner="ollama")), keys=keys)
        self.assertEqual(code, 0)
        self.assertIn("[  ok  ] model downloaded: qwen3", out)
        code, out = run_ollama_check(settings(ModelEntry("gemma3", runner="ollama")), keys=keys)
        self.assertEqual(code, 1)
        self.assertIn("Downloaded: qwen3:latest, llama3.2:3b\n", out)

    def test_runner_flag_wins(self):
        _, out = run_ollama_check(settings(ModelEntry("qwen3:14b", runner="lmstudio")), "ollama")
        self.assertIn("Ollama server", out)


if __name__ == "__main__":
    unittest.main()
