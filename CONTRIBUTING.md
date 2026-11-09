# Contributing

Bug reports, reports of how it runs on macOS and Linux, and pull requests are all welcome.

## Reporting a problem

Open an issue and include the output of `python -m transcript_report --check`; the bug report
template asks for the rest. If a report came out badly rather than the program failing, say
which model, quantisation and `ctx` you used, since most report quality comes from the model.

## Working on the code

```
git clone https://github.com/<owner>/transcript-report
cd transcript-report
python -m pip install -e .
python -m pip install ruff
```

From a clone, settings.json and the transcripts and reports folders are kept in the project
folder (they're ignored by git). Run `python analyse_video.py --init` to create them.

Before opening a pull request:

```
python -m unittest discover -s tests -t .
ruff check .
```

The tests need no GPU or LM Studio: the model and LM Studio's API are replaced by fakes.
GitHub Actions runs them on Windows, macOS and Linux with Python 3.10–3.14.

Guidelines:

- **No dependencies.** Only the Python standard library is used, so installing stays a
  single `pip install`. Please keep it that way.
- **Errors the user can fix** raise `TranscriptReportError` with a message saying what to do;
  `main()` shows it without a traceback.
- **Prompt changes** change every report, so include a before/after example report for the
  same transcript and model in the pull request.
- Add a line to `CHANGELOG.md` under the unreleased version for anything a user would notice.
