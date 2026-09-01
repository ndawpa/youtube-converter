import tempfile
import unittest
from pathlib import Path

from clip_engine import (
    TranscriptSegment,
    build_render_command,
    build_silence_removal_command,
    boost_candidates_with_events,
    boost_candidates_with_audio_energy,
    compress_transcript_timeline,
    intersect_ranges,
    infer_mood_energy,
    estimate_music_blocks,
    fit_music_blocks,
    parse_silence_output,
    parse_spectral_output,
    parse_scene_output,
    parse_subtitle_file,
    rank_clip_candidates,
    write_ass_subtitles,
)


class ClipEngineTests(unittest.TestCase):
    def test_mood_and_energy_are_inferred_from_speech(self):
        mood, energy = infer_mood_energy(
            "Atenção! Perigo urgente! Isso foi um choque!", "impact"
        )
        self.assertEqual((mood, energy), ("dramatic", "high"))

    def test_parses_vtt_with_relative_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "captions.vtt"
            path.write_text(
                "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nFirst line\n\n"
                "00:00:05.000 --> 00:00:09.000\nSecond line\n"
            )
            segments = parse_subtitle_file(path)

        self.assertEqual(segments[0], TranscriptSegment(1, 4, "First line"))
        self.assertEqual(segments[1].end, 9)

    def test_ranks_non_overlapping_interesting_windows(self):
        segments = [
            TranscriptSegment(index * 10, index * 10 + 9,
                              "Esta é uma explicação comum." if index < 5 else
                              "Atenção: este é o principal segredo e o melhor resultado!")
            for index in range(10)
        ]
        clips = rank_clip_candidates(segments, "highlights", count=2, min_duration=20, max_duration=35)

        self.assertTrue(clips)
        self.assertGreater(clips[-1].score, 1)
        if len(clips) == 2:
            self.assertLessEqual(clips[0].end, clips[1].start + 5)

    def test_silence_parser_returns_audible_ranges(self):
        stderr = "silence_start: 2.0\nsilence_end: 4.5 | silence_duration: 2.5\nsilence_start: 9.0"
        self.assertEqual(parse_silence_output(stderr, 12), [(0.0, 2.0), (4.5, 9.0)])

    def test_music_frames_are_grouped(self):
        rows = [
            {"time": second, "flatness": 0.2, "flux": 0.2, "rms": 0.1}
            for second in range(15)
        ]
        self.assertEqual(estimate_music_blocks(rows, minimum_duration=8), [(0.0, 14.0)])
        self.assertEqual(
            fit_music_blocks([(0, 140)], minimum_duration=20, maximum_duration=60, count=2),
            [(0, 60), (60, 120)],
        )

    def test_spectral_metadata_is_parsed(self):
        output = """frame:0 pts:0 pts_time:1.5
lavfi.astats.Overall.RMS_level=-20
lavfi.aspectralstats.1.flatness=0.2
lavfi.aspectralstats.1.flux=0.1
"""
        self.assertEqual(parse_spectral_output(output), [{
            "time": 1.5, "rms": 0.1, "flatness": 0.2, "flux": 0.1,
        }])

    def test_scene_changes_boost_candidate_score(self):
        output = "lavfi.scd.score=12.0\nlavfi.scd.time=15.2\nlavfi.scd.time=22.0"
        scenes = parse_scene_output(output)
        candidate = rank_clip_candidates([
            TranscriptSegment(10, 20, "Este é o principal resultado!"),
            TranscriptSegment(20, 30, "Uma conclusão importante."),
        ], min_duration=15, max_duration=30, count=1)[0]
        boosted = boost_candidates_with_events([candidate], scenes)[0]
        self.assertGreater(boosted.score, candidate.score)
        self.assertIn("2 mudanças visuais", boosted.reason)

    def test_audio_energy_boosts_candidate(self):
        candidate = rank_clip_candidates([
            TranscriptSegment(0, 15, "Um resultado importante."),
            TranscriptSegment(15, 30, "A principal conclusão!"),
        ], count=1, min_duration=20, max_duration=35)[0]
        boosted = boost_candidates_with_audio_energy(
            [candidate], [{"time": 10, "rms": .2}, {"time": 20, "rms": .3}]
        )[0]
        self.assertGreater(boosted.score, candidate.score)

    def test_ass_subtitles_are_rebased_to_clip_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.ass"
            write_ass_subtitles(path, [TranscriptSegment(12, 15, "Hello")], 10, 20, "highlight")
            content = path.read_text()

        self.assertIn("Dialogue: 0,0:00:02.00,0:00:05.00", content)
        self.assertIn("Hello", content)

    def test_highlight_subtitles_use_word_karaoke_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.ass"
            write_ass_subtitles(path, [TranscriptSegment(0, 2, "Two words")], 0, 3, "highlight")
            self.assertIn(r"{\k100}Two {\k100}words", path.read_text())

    def test_render_command_builds_vertical_video_with_ducked_music(self):
        command = build_render_command(
            Path("source.mp4"), Path("clip.mp4"), 10, 40,
            aspect_ratio="9:16", subtitles=Path("clip.ass"), music=Path("music.mp3"),
        )
        filters = command[command.index("-filter_complex") + 1]

        self.assertIn("crop=1080:1920", filters)
        self.assertIn("setsar=1", filters)
        self.assertIn("sidechaincompress", filters)
        self.assertIn("loudnorm", filters)
        self.assertIn("aresample=48000", filters)
        self.assertIn("ass=", filters)
        self.assertIn("2", command[command.index("-threads") + 1])

    def test_silence_removal_rebases_video_audio_and_transcript(self):
        ranges = intersect_ranges([(0, 3), (5, 9), (12, 15)], 2, 13)
        self.assertEqual(ranges, [(2, 3), (5, 9), (12, 13)])
        command = build_silence_removal_command(Path("source.mp4"), Path("clean.mp4"), ranges)
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("concat=n=3:v=1:a=1", filters)
        segments = compress_transcript_timeline(
            [TranscriptSegment(5, 7, "Words")], ranges
        )
        self.assertEqual(segments, [TranscriptSegment(1, 3, "Words")])


if __name__ == "__main__":
    unittest.main()
