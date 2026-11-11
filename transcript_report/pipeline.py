"""Single-pass or split analysis: sections, notes, merging, final report.

The model is reached only through the `chat` function passed in, so this module can be
tested with a fake one.
"""

import hashlib
import re
from collections.abc import Callable
from pathlib import Path

from .config import (
    NOTES_HEADINGS_USED,
    NOTES_TOKENS,
    REPORT_TOKENS,
    SAFETY_TOKENS,
    WORDS_PER_MINUTE,
    Prompts,
    RunOptions,
)
from .errors import TranscriptReportError
from .helpers import est_tokens, say

# (system prompt, user message, max output tokens, progress label) -> reply text
ChatFn = Callable[[str, str, int, str], str]

MIN_SECTION_WORDS = 100
MERGE_SEP = "\n\n---\n\n"
FINISH = ("\n\nNow write the complete Video Analysis Report using the exact format from "
          "your instructions. Cover the whole video, start to finish.")


def _overlap(words_per_section: int) -> int:
    # Neighbouring sections share their boundary words, so a point made across the cut
    # appears whole in one of them.
    return min(120, words_per_section // 10)


def _split(text: str, words_per_section: int) -> list[str]:
    overlap = _overlap(words_per_section)
    max_unit = words_per_section - overlap        # overlap + one unit always fits
    units = []
    for u in re.split(r"(?<=[.!?])\s+|\n+", text):
        w = u.split()
        units.extend(" ".join(w[i:i + max_unit]) for i in range(0, len(w), max_unit))
    sections, current, count = [], [], 0
    for u in units:
        n = len(u.split())
        if count + n > words_per_section and current:
            sections.append(" ".join(current))
            tail = " ".join(current).split()[-overlap:] if overlap else []
            current, count = ([" ".join(tail)] if tail else []), len(tail)
        current.append(u)
        count += n
    if current:
        sections.append(" ".join(current))
    return sections


def split_sections(text: str, words_per_section: int) -> list[str]:
    """Split on line/sentence boundaries into sections of at most N words, with a small
    overlap. A single line or sentence longer than that (e.g. unpunctuated auto-captions)
    is cut into word chunks, so no section can exceed the limit. Section sizes are evened
    out, so the last one isn't a tiny leftover that costs a whole model call."""
    sections = _split(text, words_per_section)
    if len(sections) < 2:
        return sections
    even = -(-len(text.split()) // len(sections)) + _overlap(words_per_section)   # ceiling division
    for target in (int(even * 1.05), int(even * 1.15)):       # a little slack for sentence ends
        target = max(MIN_SECTION_WORDS, min(words_per_section, target))
        balanced = _split(text, target)
        if len(balanced) == len(sections):
            return balanced
    return sections


def header(opts: RunOptions, word_count: int) -> str:
    h = f"Video length: about {word_count / WORDS_PER_MINUTE:.0f} minutes ({word_count:,} words of transcript)."
    return f"Video title: {opts.title}\n{h}" if opts.title else h


def _budget(opts: RunOptions, system_prompt: str, output_tokens: int) -> int:
    """Input tokens left for a call's user message."""
    return opts.ctx - est_tokens(system_prompt) - opts.output_tokens(output_tokens) - SAFETY_TOKENS


def report_budget(opts: RunOptions, prompts: Prompts) -> int:
    return _budget(opts, prompts.report, REPORT_TOKENS)


def merge_budget(opts: RunOptions, prompts: Prompts) -> int:
    return _budget(opts, prompts.merge, NOTES_TOKENS)


def section_budget(opts: RunOptions, prompts: Prompts) -> int:
    return _budget(opts, prompts.notes, NOTES_TOKENS)


def check_long_path_fits(opts: RunOptions, prompts: Prompts) -> None:
    """Fail before any section work if the context is too small for the split pipeline."""
    need = int(NOTES_TOKENS * 2.5)   # room for at least two notes per merge / in the report
    problems = [name for name, b in (("final report", report_budget(opts, prompts)),
                                     ("merging notes", merge_budget(opts, prompts)),
                                     ("section notes", section_budget(opts, prompts))) if b < need]
    if problems:
        raise TranscriptReportError(f"A {opts.ctx:,}-token context is too small to analyse a long video "
                                    f"(not enough room for: {', '.join(problems)}). Use --ctx 16384 or more"
                                    + (", or leave out --think." if opts.think else "."))


def section_user(opts: RunOptions, words: int, i: int, n: int, section: str) -> str:
    return (f"{header(opts, words)}\nThis is section {i} of {n}.\n\n"
            f"<section>\n{section}\n</section>\n\nWrite the notes for this section now.")


def final_user(opts: RunOptions, words: int, notes: list[str]) -> str:
    # Headings in section_notes_prompt.md (config.load_prompts checks they're still there).
    style, padding = (f"'{h.lstrip('# ')}'" for h in NOTES_HEADINGS_USED)
    return (f"{header(opts, words)}\n\nThe transcript was too long to read in one go, so it "
            f"was split into sections and the notes below were extracted in order. Use the "
            f"{style} and {padding} notes for the AI-generated and density scores."
            f"\n\nSECTION NOTES:\n<notes>\n" + "\n\n".join(notes) + "\n</notes>" + FINISH)


def pick_section_size(opts: RunOptions, prompts: Prompts, text: str, words: int) -> list[str]:
    """Split the transcript so every section's request fits the context, whatever
    --chunk-words says."""
    budget = section_budget(opts, prompts)
    chunk = max(MIN_SECTION_WORDS, min(opts.chunk_words, int(budget / 1.6)))
    while True:
        sections = split_sections(text, chunk)
        n = len(sections)
        if chunk <= MIN_SECTION_WORDS or all(
                est_tokens(section_user(opts, words, i, n, s)) <= budget
                for i, s in enumerate(sections, 1)):
            break
        chunk = max(MIN_SECTION_WORDS, int(chunk * 0.85))
    if chunk < opts.chunk_words:
        say(f"Sections reduced to {chunk:,} words to fit the {opts.ctx:,}-token context.")
    return sections


def condense(opts: RunOptions, prompts: Prompts, notes: list[str], words: int,
             notes_path: Path, chat: ChatFn) -> list[str]:
    """Merge section notes in batches until the final report's user message fits
    alongside the report prompt."""
    budget = report_budget(opts, prompts)
    rounds = 0
    while est_tokens(final_user(opts, words, notes)) > budget and len(notes) > 1:
        rounds += 1
        say(f"Notes too long for one pass; merging (round {rounds})")
        batches: list[list[str]] = []
        batch: list[str] = []
        for note in notes:
            if batch and est_tokens(MERGE_SEP.join(batch + [note])) > merge_budget(opts, prompts):
                batches.append(batch)
                batch = []
            batch.append(note)
        batches.append(batch)
        if all(len(b) == 1 for b in batches):
            break   # can't shrink further by merging
        to_merge = sum(len(b) > 1 for b in batches)
        merged, batch_no = [], 0
        for batch in batches:
            if len(batch) == 1:
                merged.append(batch[0])
                continue
            batch_no += 1
            merged.append(chat(prompts.merge, MERGE_SEP.join(batch), opts.output_tokens(NOTES_TOKENS),
                               f"Merging batch {batch_no}/{to_merge}"))
        notes = merged
    if est_tokens(final_user(opts, words, notes)) > budget:
        raise TranscriptReportError(f"The section notes still don't fit in the {opts.ctx:,}-token context "
                                    f"after merging. Raise --ctx. The section notes are saved in {notes_path}.")
    return notes


NOTES_STAMP = "<!-- transcript-report section notes {} -->"
NOTE_HEADING = "## Notes for section {} of {}"
NOTE_HEADING_RE = re.compile("(?m)^(?=" + re.escape(NOTE_HEADING).replace(r"\{\}", r"\d+") + "$)")


def fingerprint(sections: list[str]) -> str:
    """Identifies this exact split, so saved notes are only reused for the same sections."""
    digest = hashlib.sha256("\n\x1e\n".join(sections).encode("utf-8")).hexdigest()[:16]
    return f"{len(sections)}-{digest}"


def save_notes(path: Path, fp: str, notes: list[str]) -> None:
    # Made here, not before the first section, so a run that fails on it leaves no empty folder.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(NOTES_STAMP.format(fp) + "\n\n" + "\n\n".join(notes), encoding="utf-8")


def load_saved_notes(path: Path, fp: str) -> list[str] | None:
    """Notes saved by an earlier run for the same split: [] if there are none, None if the
    file is from a different split (another transcript, --ctx or --chunk-words)."""
    if not path.is_file():
        return []
    first, _, rest = path.read_text(encoding="utf-8").partition("\n")
    if first.strip() != NOTES_STAMP.format(fp):
        return None
    return [part.strip() for part in NOTE_HEADING_RE.split(rest) if part.strip()]


def analyse(opts: RunOptions, prompts: Prompts, text: str, notes_path: Path, chat: ChatFn,
            resume: bool = False) -> str:
    """Return the report: in one pass if the transcript fits, otherwise via section notes
    (saved to notes_path as each section is done). With resume, sections whose notes an
    earlier run already saved there are skipped."""
    words = len(text.split())
    if report_budget(opts, prompts) <= 0:
        raise TranscriptReportError(f"A {opts.ctx:,}-token context can't even fit the report prompt. Raise --ctx.")

    single_user = f"{header(opts, words)}\n\nFULL TRANSCRIPT:\n<transcript>\n{text}\n</transcript>" + FINISH
    if est_tokens(single_user) <= report_budget(opts, prompts) and words <= opts.single_max_words:
        say(f"Single pass ({words:,} words)")
        return chat(prompts.report, single_user, opts.output_tokens(REPORT_TOKENS), "Writing report")

    check_long_path_fits(opts, prompts)
    sections = pick_section_size(opts, prompts, text, words)
    n = len(sections)
    fp = fingerprint(sections)
    say(f"Long video ({words:,} words): {n} sections. Notes are saved to {notes_path} "
        f"as each section is done (if the run stops, --resume continues from there).")
    notes: list[str] = []
    if resume:
        saved = load_saved_notes(notes_path, fp)
        if saved is None:
            say("The saved section notes are for a different split (another --ctx or "
                "--chunk-words?), so all sections are done again.")
        elif saved:
            notes = saved[:n]
            say(f"Resuming: {len(notes)} of {n} sections already done.")
    for i, sec in enumerate(sections[len(notes):], len(notes) + 1):
        reply = chat(prompts.notes, section_user(opts, words, i, n, sec),
                     opts.output_tokens(NOTES_TOKENS), f"Section {i}/{n} notes")
        notes.append(f"{NOTE_HEADING.format(i, n)}\n\n{reply}")
        save_notes(notes_path, fp, notes)   # keep the work if a later step fails

    notes = condense(opts, prompts, notes, words, notes_path, chat)
    return chat(prompts.report, final_user(opts, words, notes), opts.output_tokens(REPORT_TOKENS),
                "Writing final report")
