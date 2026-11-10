"""transcript-report: turn a video transcript into a structured report with a local model
in LM Studio or Ollama.

Modules:
    cli.py           the command line (options, the model session, each transcript)
    config.py        settings.json and the home folder, prompts, token budgets, report paths
    transcripts.py   reading YouTube transcripts and .srt/.vtt subtitles
    runner.py        the Runner interface: what the command line needs from LM Studio or Ollama
    lmstudio.py      LM Studio's server, loading and unloading the model
    ollama.py        Ollama's server, loading and unloading the model, and its chat requests
    http_client.py   JSON over HTTP to the server's REST API, and APIError
    model_call.py    one streamed chat request to LM Studio, and the checks on every runner's reply
    pipeline.py      single-pass or split analysis and the final report
    pdf.py           turning the Markdown report into a PDF (via Edge or Chrome)
    check.py         --check: testing the setup
    helpers.py       status messages, VRAM check, token estimate, removing reasoning, progress line
    errors.py        TranscriptReportError, the one-line error messages shown to the user
"""

__version__ = "0.2.0"
