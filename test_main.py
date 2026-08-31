import unittest
from unittest.mock import patch

import main


class DownloadOptionsTests(unittest.TestCase):
    @patch.object(main, "cookies_are_authenticated", return_value=False)
    def test_does_not_override_yt_dlp_player_clients(self, _authenticated):
        self.assertNotIn("extractor_args", main.base_ydl_opts())

    @patch.object(main, "base_ydl_opts", return_value={})
    def test_1080p_requires_an_actual_1080p_stream(self, _base_opts):
        opts = main.build_ydl_opts("mp4", "1080p", "video.%(ext)s")

        self.assertEqual(
            opts["format"],
            "bestvideo[height=1080]+bestaudio/best[height=1080]",
        )
        self.assertNotIn("height<=1080", opts["format"])

    @patch.object(main, "base_ydl_opts", return_value={})
    def test_best_quality_keeps_best_available_selector(self, _base_opts):
        opts = main.build_ydl_opts("mp4", "best", "video.%(ext)s")

        self.assertEqual(opts["format"], "bestvideo+bestaudio/best")


if __name__ == "__main__":
    unittest.main()
