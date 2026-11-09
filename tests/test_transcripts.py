import tempfile
import unittest
from pathlib import Path

from transcript_report.transcripts import clean_transcript, parse_subtitles, read_transcript, spoken_time


class SpokenTime(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(spoken_time("0:07"), "7 seconds")
        self.assertEqual(spoken_time("1:05"), "1 minute, 5 seconds")
        self.assertEqual(spoken_time("2:00"), "2 minutes")
        self.assertEqual(spoken_time("1:02:03"), "1 hour, 2 minutes, 3 seconds")
        self.assertEqual(spoken_time("1:00:00"), "1 hour")


class CleanTranscript(unittest.TestCase):
    def test_youtube_lines(self):
        raw = ("I applied to my own job\n"
               "0:077 secondsa senior role\n"
               "10:1310 minutes, 13 secondsIt's\n"
               "2:002 minutesyou\n"
               "1:02:031 hour, 2 minutes, 3 secondsdeep\n")
        self.assertEqual(clean_transcript(raw),
                         "I applied to my own job\na senior role\nIt's\nyou\ndeep")

    def test_spoken_numbers_after_timestamp_are_kept(self):
        raw = ("1:051 minute, 5 seconds5 minutes later we left\n"
               "2:102 minutes, 10 seconds10 seconds is all it takes")
        self.assertEqual(clean_transcript(raw), "5 minutes later we left\n10 seconds is all it takes")

    def test_bom_bare_timestamps_and_blank_lines(self):
        raw = "﻿0:055 secondshello\n\n0:30\n   \nplain"
        self.assertEqual(clean_transcript(raw), "hello\nplain")

    def test_time_in_speech_is_left_alone(self):
        self.assertEqual(clean_transcript("3:45 pm is when we met"), "3:45 pm is when we met")


class Subtitles(unittest.TestCase):
    def test_srt(self):
        srt = ("1\n00:00:01,000 --> 00:00:03,000\n<i>Hello</i> there\n\n"
               "2\n00:00:03,000 --> 00:00:05,000\n[Music]\n\n"
               "3\n00:00:05,000 --> 00:00:07,000\nTom &amp; Jerry\n")
        self.assertEqual(parse_subtitles(srt), "Hello there\nTom & Jerry")

    def test_youtube_auto_vtt_rolling_lines(self):
        vtt = ("WEBVTT\nKind: captions\nLanguage: en\n\n"
               "00:00:00.000 --> 00:00:02.000 align:start position:0%\n"
               "hello<00:00:00.500><c> there</c>\n\n"
               "00:00:02.000 --> 00:00:04.000 align:start position:0%\n"
               "hello there\nhow are<00:00:03.000><c> you</c>\n\n"
               "NOTE a comment\nthat spans lines\n\n"
               "00:00:04.000 --> 00:00:06.000\nhow are you\nfine thanks\n")
        self.assertEqual(parse_subtitles(vtt), "hello there\nhow are you\nfine thanks")

    def test_read_transcript_picks_parser_by_extension(self):
        with tempfile.TemporaryDirectory() as d:
            srt = Path(d, "a.srt")
            srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
            txt = Path(d, "a.txt")
            txt.write_text("0:077 secondsHi", encoding="utf-8")
            self.assertEqual(read_transcript(srt), "Hi")
            self.assertEqual(read_transcript(txt), "Hi")


if __name__ == "__main__":
    unittest.main()
