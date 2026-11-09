"""
Run transcript-report from a clone of the repository, without installing it:

    python analyse_video.py "My video.txt" --title "Video title - Channel"
    python analyse_video.py --all
    python analyse_video.py --init      (create settings.json and the folders)
    python analyse_video.py --check     (test the setup)

This is the same as the `transcript-report` command you get from `pipx install`.
From a clone, settings.json and the transcripts and reports folders are in this folder.
See README.md and docs/usage.md.
"""

import sys

if sys.version_info < (3, 10):  # noqa: UP036  (runs on old Pythons to say so clearly)
    sys.exit("transcript-report needs Python 3.10 or newer.")

from transcript_report.cli import main  # noqa: E402  (after the version check)

if __name__ == "__main__":
    main(prog="analyse_video.py")
