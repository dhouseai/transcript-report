import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from transcript_report import check, lmstudio
from transcript_report.config import ModelEntry, Settings


def run_check(keys=("pub/m",), browser: Path | None = Path("edge.exe")):
    """run_check() on this computer with LM Studio, the browser and the GPU faked."""
    settings = Settings(Path("t"), Path("d"), Path("r"), default_model="m",
                        models={"m": ModelEntry("pub/m")}, source=Path("settings.json"))
    with mock.patch.object(check, "load_settings", return_value=settings), \
         mock.patch.object(lmstudio, "find_lms", return_value=Path("lms")), \
         mock.patch.object(lmstudio, "ensure_server", return_value=True), \
         mock.patch.object(lmstudio, "model_keys", return_value=list(keys)), \
         mock.patch.object(lmstudio, "stop_server") as stop, \
         mock.patch.object(check, "find_browser", return_value=browser), \
         mock.patch.object(check, "vram_info", return_value=(1000, 12000)), \
         contextlib.redirect_stdout(io.StringIO()) as out:
        code = check.run_check(None, 1234, None)
    return code, out.getvalue(), stop


class RunCheck(unittest.TestCase):
    def test_all_set(self):
        code, out, stop = run_check()
        self.assertEqual(code, 0)
        self.assertIn("[  ok  ] LM Studio's lms command: lms", out)
        self.assertIn("[  ok  ] LM Studio server: http://127.0.0.1:1234 (started for this check)", out)
        self.assertIn("[  ok  ] model downloaded: pub/m", out)
        self.assertIn("All set.", out)
        stop.assert_called_once()   # the server was started for the check

    def test_missing_model_fails(self):
        code, out, _ = run_check(keys=["other/model"])
        self.assertEqual(code, 1)
        self.assertIn("[ FAIL ] model downloaded", out)

    def test_no_browser_is_only_a_warning_for_markdown(self):
        code, out, _ = run_check(browser=None)
        self.assertEqual(code, 0)
        self.assertIn("[ warn ] PDF browser", out)


if __name__ == "__main__":
    unittest.main()
