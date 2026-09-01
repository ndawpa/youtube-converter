import unittest
import json
import subprocess
import tempfile
from pathlib import Path
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
            "bestvideo[height=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height=1080]+bestaudio/best[height=1080][ext=mp4]",
        )
        self.assertNotIn("height<=1080", opts["format"])

    @patch.object(main, "base_ydl_opts", return_value={})
    def test_best_quality_keeps_best_available_selector(self, _base_opts):
        opts = main.build_ydl_opts("mp4", "best", "video.%(ext)s")

        self.assertEqual(
            opts["format"],
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]",
        )

    def test_webm_selector_only_uses_webm_streams(self):
        selector = main.video_format_for_container("webm", "720p")
        self.assertEqual(selector, "bestvideo[height=720][ext=webm]+bestaudio[ext=webm]/best[height=720][ext=webm]")

    def test_dynamic_4k_quality_requires_exact_height(self):
        self.assertEqual(
            main.video_format_for_quality("2160p"),
            "bestvideo[height=2160]+bestaudio/best[height=2160]",
        )

    @patch.object(main, "base_ydl_opts", return_value={})
    def test_audio_formats_use_ffmpeg_extraction(self, _base_opts):
        request = main.ConvertRequest(url="https://example.test", format="flac")
        opts = main.build_ydl_opts("flac", "best", "audio.%(ext)s", request)

        self.assertEqual(opts["format"], "bestaudio/best")
        self.assertEqual(opts["postprocessors"][0]["preferredcodec"], "flac")

    @patch.object(main, "base_ydl_opts", return_value={})
    def test_cut_chapters_cover_and_metadata_are_configured(self, _base_opts):
        request = main.ConvertRequest(
            url="https://example.test", format="mp4", start_time=10, end_time=20,
            chapter_titles=["Introduction"], split_chapters=True,
            title="Lesson", artist="Teacher", album="Course", year="2026", track="2",
        )
        opts = main.build_ydl_opts("mp4", "720p", "video.%(ext)s", request)

        self.assertIn("download_ranges", opts)
        self.assertTrue(opts["force_keyframes_at_cuts"])
        self.assertTrue(opts["writethumbnail"])
        keys = [processor["key"] for processor in opts["postprocessors"]]
        self.assertEqual(keys, ["FFmpegSplitChapters", "EmbedThumbnail", "FFmpegMetadata"])
        metadata_processor = opts["postprocessors"][-1]
        self.assertFalse(metadata_processor["add_chapters"])
        metadata_args = opts["postprocessor_args"]["metadata"]
        self.assertIn("artist=Teacher", metadata_args)
        self.assertIn("album=Course", metadata_args)

    @patch.object(main, "base_ydl_opts", return_value={})
    def test_full_video_keeps_original_chapters(self, _base_opts):
        request = main.ConvertRequest(
            url="https://example.test", format="mp4", embed_thumbnail=False
        )
        opts = main.build_ydl_opts("mp4", "720p", "video.%(ext)s", request)

        self.assertTrue(opts["postprocessors"][-1]["add_chapters"])

    def test_video_info_lists_formats_subtitles_and_chapters(self):
        result = main._format_info({
            "formats": [
                {"height": 1080, "width": 1920, "fps": 60, "vcodec": "avc1", "ext": "mp4", "filesize": 100},
                {"height": 1080, "width": 1920, "fps": 60, "vcodec": "vp9", "ext": "webm", "filesize": 200},
                {"vcodec": "none", "acodec": "opus"},
            ],
            "subtitles": {"pt-BR": [{}]},
            "automatic_captions": {"en": [{}], "live_chat": [{}]},
            "chapters": [{"title": "Intro", "start_time": 0, "end_time": 10}],
        })

        self.assertEqual(len(result["formats"]), 1)
        self.assertEqual(result["formats"][0]["filesize"], 200)
        self.assertEqual([item["language"] for item in result["subtitles"]], ["en", "pt-BR"])
        self.assertEqual(result["chapters"][0]["title"], "Intro")

    def test_transcript_conversion_to_text_and_json(self):
        vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nHello <b>world</b>\n\n00:00:03.000 --> 00:00:04.000\nAgain\n"
        with tempfile.TemporaryDirectory() as directory:
            text_source = Path(directory) / "captions.vtt"
            text_source.write_text(vtt)
            text_path = main._convert_transcript(text_source, "txt")
            self.assertEqual(text_path.read_text(), "Hello world\nAgain\n")

            json_source = Path(directory) / "captions-2.vtt"
            json_source.write_text(vtt)
            json_path = main._convert_transcript(json_source, "json")
            self.assertEqual(json.loads(json_path.read_text())["segments"][0]["start"], "00:00:01.000")

    def test_asynchronous_job_produces_downloadable_output(self):
        class FakeYoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, _url, download):
                self.assert_download(download)
                output = Path(self.opts["outtmpl"].replace("%(title)s", "Example").replace("%(ext)s", "mp4"))
                output.write_bytes(b"media")
                self.opts["progress_hooks"][0]({
                    "status": "downloading", "downloaded_bytes": 5, "total_bytes": 10,
                    "info_dict": {"title": "Example"},
                })
                return {"title": "Example"}

            def assert_download(self, download):
                if not download:
                    raise AssertionError("expected a download")

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(main, "DOWNLOADS_DIR", Path(directory)), \
                patch.object(main.yt_dlp, "YoutubeDL", FakeYoutubeDL):
            job_id = "job-test"
            main._download_jobs[job_id] = main._DownloadJob()
            request = main.ConvertRequest(
                url="https://example.test", format="mp4", embed_thumbnail=False
            )
            main._run_download_job(job_id, request)
            job = main._download_jobs.pop(job_id)

            self.assertEqual(job.status, "done")
            self.assertEqual(job.progress, 100)
            self.assertEqual(job.filename, "Example.mp4")
            self.assertEqual(job.output_path.read_bytes(), b"media")

    def test_music_selection_prioritizes_mood_energy_and_bpm(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(main, "MUSIC_DIR", Path(directory)), \
                patch.object(main, "MUSIC_MANIFEST", Path(directory) / "tracks.json"):
            tracks = [
                {"id": "slow", "filename": "slow.mp3", "mood": "calm", "energy": "low", "bpm": 70},
                {"id": "match", "filename": "match.mp3", "mood": "inspiring", "energy": "high", "bpm": 136},
            ]
            for track in tracks:
                (Path(directory) / track["filename"]).write_bytes(b"audio")
            main._save_music_tracks(tracks)

            selected = main._select_music_track("auto", "inspiring", "high")

            self.assertEqual(selected.name, "match.mp3")

    def test_ffmpeg_sigkill_has_actionable_error(self):
        error = subprocess.CalledProcessError(-9, ["ffmpeg"], stderr="")
        with patch.object(main.subprocess, "run", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "ran out of memory"):
                main._run_media_command(["ffmpeg"])

    def test_clip_analysis_job_reaches_review_state(self):
        class FakeYoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, _url, download):
                self.assert_download(download)
                template = self.opts["outtmpl"]
                Path(template.replace("%(ext)s", "mp4")).write_bytes(b"video")
                Path(template.replace("%(ext)s", "pt.vtt")).write_text(
                    "WEBVTT\n\n00:00:00.000 --> 00:00:10.000\nIntrodução importante.\n\n"
                    "00:00:10.000 --> 00:00:20.000\nEste é o principal segredo!\n\n"
                    "00:00:20.000 --> 00:00:30.000\nVeja como obter o melhor resultado.\n"
                )
                return {"title": "Example"}

            def assert_download(self, value):
                if not value:
                    raise AssertionError("expected download")

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(main, "DOWNLOADS_DIR", Path(directory)), \
                patch.object(main.yt_dlp, "YoutubeDL", FakeYoutubeDL), \
                patch.object(main, "detect_scene_changes", return_value=[12]), \
                patch.object(main, "analyze_spectral_frames", return_value=[
                    {"time": 12, "rms": .2, "flatness": .2, "flux": .2},
                ]):
            job_id = "clip-analysis"
            main._clip_jobs[job_id] = main._ClipJob()
            request = main.AutoClipRequest(
                url="https://example.test", min_duration=20, max_duration=35
            )
            main._run_clip_analysis(job_id, request)
            job = main._clip_jobs.pop(job_id)

            self.assertEqual(job.status, "ready")
            self.assertEqual(job.title, "Example")
            self.assertTrue(job.candidates)

    def test_clip_render_job_creates_zip(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "DOWNLOADS_DIR", Path(directory)):
            work_dir = Path(directory) / "work"
            work_dir.mkdir()
            source = work_dir / "source.mp4"
            source.write_bytes(b"source")
            job_id = "clip-render"
            main._clip_jobs[job_id] = main._ClipJob(
                status="ready", work_dir=work_dir, source_path=source,
                segments=[main.TranscriptSegment(0, 30, "Words")],
                candidates=[main.ClipCandidate(0, 30, 5, "Title", "Reason", "Words")],
            )

            def fake_render(_job, _candidate, index, _request, _music):
                output = work_dir / f"clip-{index}.mp4"
                output.write_bytes(b"clip")
                return output

            with patch.object(main, "_render_clip", side_effect=fake_render):
                main._run_clip_render(job_id, main.RenderClipsRequest())
            job = main._clip_jobs.pop(job_id)

            self.assertEqual(job.status, "done")
            self.assertTrue(job.output_path.exists())

    def test_music_analysis_does_not_require_transcript(self):
        class FakeYoutubeDL:
            def __init__(self, opts): self.opts = opts
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def extract_info(self, _url, download):
                Path(self.opts["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"video")
                return {"title": "Instrumental"}

        musical_rows = [
            {"time": second, "rms": .1, "flatness": .2, "flux": .2}
            for second in range(20)
        ]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(main, "DOWNLOADS_DIR", Path(directory)), \
                patch.object(main.yt_dlp, "YoutubeDL", FakeYoutubeDL), \
                patch.object(main, "analyze_spectral_frames", return_value=musical_rows), \
                patch.object(main, "which", return_value=None):
            job_id = "music-analysis"
            main._clip_jobs[job_id] = main._ClipJob()
            main._run_clip_analysis(job_id, main.AutoClipRequest(
                url="https://example.test", objective="music", min_duration=8
            ))
            job = main._clip_jobs.pop(job_id)

            self.assertEqual(job.status, "ready")
            self.assertEqual(job.candidates, [])
            self.assertTrue(job.music_blocks)


if __name__ == "__main__":
    unittest.main()
