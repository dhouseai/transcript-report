"""transcript-report: turn a video transcript into a structured report with a local model
in LM Studio.

Modules:
    cli.py           the command line (options, the model session, each transcript)
    config.py        settings.json and the home folder, prompts, token budgets, report paths
    transcripts.py   reading YouTube transcripts and .srt/.vtt subtitles
    lmstudio.py      LM Studio's server, loading and unloading the model
    model_call.py    one streamed chat request to LM Studio
    pipeline.py      single-pass or split analysis and the final report
    pdf.py           turning the Markdown report into a PDF (via Edge or Chrome)
    check.py         --check: testing the setup
    helpers.py       VRAM check, token estimate, removing reasoning, progress line
    errors.py        TranscriptReportError, the one-line error messages shown to the user
"""

__version__ = "0.1.0"
