import unittest

from transcript_report.helpers import est_tokens, strip_think


class StripThink(unittest.TestCase):
    def test_reasoning_is_removed(self):
        for text, expected in (("<think>x</think> a", "a"),
                               ("reasoning</think>answer", "answer"),   # opening tag in the template
                               ("answer<think>cut off", "answer"),      # cut off mid-thought
                               (" plain ", "plain")):
            with self.subTest(text=text):
                self.assertEqual(strip_think(text), expected)


class EstTokens(unittest.TestCase):
    def test_errs_high(self):
        self.assertGreaterEqual(est_tokens("one two three"), 3 * 1.4 - 1)   # by words
        jargon = "kubernetes-operator-sdk " * 15                           # few words, many characters
        self.assertGreaterEqual(est_tokens(jargon), len(jargon) / 3.6 - 1)
        self.assertGreater(est_tokens(jargon), len(jargon.split()) * 1.4)


if __name__ == "__main__":
    unittest.main()
