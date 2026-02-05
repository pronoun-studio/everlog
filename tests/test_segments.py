import unittest


class TestSegmentsExtraction(unittest.TestCase):
    def test_extracts_concrete_snippets_and_keywords(self) -> None:
        from everlog.segments import build_segments

        ocr_text = """
        /Users/arima/DEV/everytimecapture/macos_app/dist/everlog.app/Contents/MacOS/everlog 2>&1
        platform.openai.com/settings/proj_Example1234567890/api-keys
        NAME
        CodeX_CLIをAPI利用する
        """

        events = [
            {
                "id": "evt",
                "ts": "2026-02-05T00:00:11+09:00",
                "interval_sec": 60,
                "active_app": "Cursor",
                "window_title": "",
                "browser": None,
                "ocr_text": ocr_text,
                "excluded": False,
            }
        ]

        segs = build_segments(events, default_interval_sec=60)
        self.assertEqual(len(segs), 1)
        seg = segs[0]

        # Snippets should prefer concrete anchors over UI noise.
        joined_snips = "\n".join(seg.ocr_snippets)
        self.assertIn("platform.openai.com/settings/", joined_snips)
        self.assertTrue(any("everlog.app" in s for s in seg.ocr_snippets + seg.keywords))
        self.assertTrue(any("2>&1" in s for s in seg.ocr_snippets + seg.keywords))

    def test_redacts_api_key_like_strings(self) -> None:
        from everlog.segments import build_segments

        ocr_text = "API Keys sk-1234567890abcdefghijklmn more text"
        events = [
            {
                "id": "evt",
                "ts": "2026-02-05T00:00:11+09:00",
                "interval_sec": 60,
                "active_app": "Cursor",
                "window_title": "",
                "browser": None,
                "ocr_text": ocr_text,
                "excluded": False,
            }
        ]

        seg = build_segments(events, default_interval_sec=60)[0]
        hay = " ".join(seg.keywords + seg.ocr_snippets)
        self.assertIn("sk-…", hay)
        self.assertNotIn("sk-1234567890abcdefghijklmn", hay)


if __name__ == "__main__":
    unittest.main()

