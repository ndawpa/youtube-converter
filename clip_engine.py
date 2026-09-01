"""Analysis and rendering helpers for automatic short-form clips.

The ranking code is intentionally deterministic and lightweight. Deployments can
replace ``rank_clip_candidates`` with an external semantic model without changing
the API or rendering pipeline.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable


OBJECTIVE_TERMS = {
    "highlights": {"important", "best", "main", "principal", "segredo", "resultado", "conclusão"},
    "educational": {"porque", "como", "exemplo", "aprenda", "passo", "dica", "significa", "portanto"},
    "funny": {"engraçado", "risada", "absurdo", "inacreditável", "haha", "erro", "surpresa"},
    "impact": {"nunca", "sempre", "mudou", "verdade", "atenção", "impossível", "precisa", "agora"},
    "shorts": {"como", "segredo", "erro", "melhor", "nunca", "por quê", "resultado"},
    "music": set(),
}

MOOD_TERMS = {
    "happy": {"feliz", "alegria", "sucesso", "conquista", "divertido", "happy", "joy", "win"},
    "calm": {"calma", "respira", "paz", "tranquilo", "reflexão", "calm", "peace", "relax"},
    "dramatic": {"urgente", "perigo", "choque", "inacreditável", "tensão", "urgent", "danger", "shock"},
    "dark": {"triste", "perda", "medo", "problema", "fracasso", "sad", "loss", "fear", "failure"},
    "inspiring": {"aprenda", "mudança", "futuro", "possível", "objetivo", "learn", "change", "future", "possible"},
}


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class ClipCandidate:
    start: float
    end: float
    score: float
    title: str
    reason: str
    transcript: str
    kind: str = "speech"

    def to_dict(self) -> dict:
        return asdict(self)


def timestamp_seconds(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        parts.insert(0, "0")
    hours, minutes, seconds = map(float, parts)
    return hours * 3600 + minutes * 60 + seconds


def infer_mood_energy(text: str, objective: str = "highlights") -> tuple[str, str]:
    words = re.findall(r"[\wÀ-ÿ]+", text.lower())
    scores = {mood: sum(word in terms for word in words) for mood, terms in MOOD_TERMS.items()}
    objective_default = {
        "educational": "calm", "funny": "happy", "impact": "dramatic",
        "shorts": "inspiring", "highlights": "inspiring",
    }.get(objective, "inspiring")
    mood = max(scores, key=scores.get) if max(scores.values(), default=0) else objective_default
    intensity = text.count("!") * 2 + text.count("?") + scores["dramatic"] + scores["happy"]
    energy = "high" if intensity >= 5 else "low" if objective == "educational" and intensity == 0 else "medium"
    return mood, energy


def parse_subtitle_file(path: Path) -> list[TranscriptSegment]:
    timestamp = re.compile(
        r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s+-->\s+"
        r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})"
    )
    result: list[TranscriptSegment] = []
    start = end = None
    lines: list[str] = []

    def flush():
        nonlocal lines
        text = " ".join(lines).strip()
        if start is not None and end is not None and text:
            result.append(TranscriptSegment(start, end, text))
        lines = []

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        match = timestamp.search(line)
        if match:
            flush()
            start, end = timestamp_seconds(match.group("start")), timestamp_seconds(match.group("end"))
        elif start is not None and line and not line.isdigit() and not line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            clean = re.sub(r"<[^>]+>", "", line)
            if clean and (not lines or clean != lines[-1]):
                lines.append(clean)
    flush()
    return result


def _window_score(segments: list[TranscriptSegment], objective: str) -> tuple[float, str]:
    text = " ".join(item.text for item in segments)
    words = re.findall(r"[\wÀ-ÿ]+", text.lower())
    terms = OBJECTIVE_TERMS.get(objective, OBJECTIVE_TERMS["highlights"])
    keyword_hits = sum(word in terms for word in words)
    punctuation = text.count("!") * 1.5 + text.count("?")
    density = min(len(words) / max(1, segments[-1].end - segments[0].start) / 2.5, 1.5)
    completeness = 1 if re.search(r"[.!?]$", text) else 0
    score = keyword_hits * 1.8 + punctuation + density + completeness
    reason = f"{keyword_hits} palavras relevantes; densidade {density:.1f}"
    return score, reason


def rank_clip_candidates(segments: Iterable[TranscriptSegment], objective: str = "highlights",
                         count: int = 3, min_duration: float = 25,
                         max_duration: float = 60) -> list[ClipCandidate]:
    items = list(segments)
    candidates: list[ClipCandidate] = []
    for left in range(len(items)):
        window: list[TranscriptSegment] = []
        for right in range(left, len(items)):
            window.append(items[right])
            duration = window[-1].end - window[0].start
            if duration < min_duration:
                continue
            if duration > max_duration:
                break
            raw_score, reason = _window_score(window, objective)
            before_gap = max(0, window[0].start - items[left - 1].end) if left else 0
            after_gap = max(0, items[right + 1].start - window[-1].end) if right + 1 < len(items) else 0
            pause_bonus = min(before_gap + after_gap, 3) * 0.2
            raw_score += pause_bonus
            reason += f"; pausas de borda {before_gap + after_gap:.1f}s"
            text = " ".join(segment.text for segment in window)
            title_words = re.findall(r"[\wÀ-ÿ]+", text)[:9]
            candidates.append(ClipCandidate(
                start=round(window[0].start, 3), end=round(window[-1].end, 3),
                score=round(raw_score, 3), title=" ".join(title_words) or "Clip",
                reason=reason, transcript=text,
            ))

    selected: list[ClipCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        overlap = any(min(candidate.end, old.end) - max(candidate.start, old.start) > 5 for old in selected)
        if not overlap:
            selected.append(candidate)
        if len(selected) == count:
            break
    return sorted(selected, key=lambda item: item.start)


def parse_silence_output(stderr: str, duration: float) -> list[tuple[float, float]]:
    """Return audible ranges from FFmpeg silencedetect output."""
    events = [(kind, float(value)) for kind, value in re.findall(
        r"silence_(start|end):\s*([0-9.]+)", stderr
    )]
    audible: list[tuple[float, float]] = []
    cursor = 0.0
    in_silence = False
    for kind, position in events:
        if kind == "start" and position > cursor:
            audible.append((cursor, position))
            in_silence = True
        elif kind == "end":
            cursor = position
            in_silence = False
    if not in_silence and cursor < duration:
        audible.append((cursor, duration))
    return [(round(start, 3), round(end, 3)) for start, end in audible if end - start >= 0.35]


def detect_audible_ranges(path: Path, duration: float, noise_db: int = -38,
                          minimum_silence: float = 0.7) -> list[tuple[float, float]]:
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
        "-af", f"silencedetect=noise={noise_db}dB:d={minimum_silence}",
        "-f", "null", "-",
    ], capture_output=True, text=True, check=False)
    return parse_silence_output(result.stderr, duration)


def estimate_music_blocks(spectral_rows: Iterable[dict], minimum_duration: float = 8) -> list[tuple[float, float]]:
    """Group frames likely to contain music using spectral stability.

    Music tends to sustain spectral energy and have lower frame-to-frame flux
    than speech. Rows are deliberately generic so a future ML classifier can
    feed the same grouping function.
    """
    frames = sorted(spectral_rows, key=lambda row: row["time"])
    blocks: list[tuple[float, float]] = []
    start = previous = None
    for row in frames:
        likely_music = row.get("flatness", 1) < 0.35 and row.get("flux", 1) < 0.45 and row.get("rms", 0) > 0.01
        timestamp = float(row["time"])
        if likely_music and start is None:
            start = timestamp
        if likely_music:
            previous = timestamp
        elif start is not None:
            if previous is not None and previous - start >= minimum_duration:
                blocks.append((round(start, 2), round(previous, 2)))
            start = previous = None
    if start is not None and previous is not None and previous - start >= minimum_duration:
        blocks.append((round(start, 2), round(previous, 2)))
    return blocks


def fit_music_blocks(blocks: Iterable[tuple[float, float]], minimum_duration: float,
                     maximum_duration: float, count: int) -> list[tuple[float, float]]:
    fitted = []
    for start, end in blocks:
        cursor = start
        while end - cursor >= minimum_duration and len(fitted) < count:
            clip_end = min(cursor + maximum_duration, end)
            if clip_end - cursor >= minimum_duration:
                fitted.append((round(cursor, 3), round(clip_end, 3)))
            cursor = clip_end
        if len(fitted) == count:
            break
    return fitted


def parse_spectral_output(stderr: str) -> list[dict]:
    rows: list[dict] = []
    current: dict = {}
    for line in stderr.splitlines():
        time_match = re.search(r"pts_time:([0-9.]+)", line)
        if time_match:
            if "time" in current:
                rows.append(current)
            current = {"time": float(time_match.group(1))}
            continue
        value_match = re.search(r"lavfi\.(?:aspectralstats\.1\.(flatness|flux)|astats\.Overall\.RMS_level)=([^\s]+)", line)
        if not value_match or "time" not in current:
            continue
        name, raw = value_match.groups()
        try:
            value = float(raw)
        except ValueError:
            continue
        if name is None:  # RMS level is reported in dBFS
            current["rms"] = 10 ** (value / 20)
        else:
            current[name] = value
    if "time" in current:
        rows.append(current)
    return rows


def analyze_spectral_frames(path: Path) -> list[dict]:
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
        "-af", (
            "aresample=16000,asetnsamples=n=16000,astats=metadata=1:reset=1,"
            "aspectralstats=measure=flatness+flux,ametadata=print"
        ),
        "-f", "null", "-",
    ], capture_output=True, text=True, check=False)
    return parse_spectral_output(result.stderr)


def parse_scene_output(stderr: str) -> list[float]:
    return [float(value) for value in re.findall(r"lavfi\.scd\.time=([0-9.]+)", stderr)]


def detect_scene_changes(path: Path, threshold: float = 10) -> list[float]:
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
        "-vf", f"scdet=threshold={threshold},metadata=print", "-an", "-f", "null", "-",
    ], capture_output=True, text=True, check=False)
    return parse_scene_output(result.stderr)


def boost_candidates_with_events(candidates: Iterable[ClipCandidate],
                                 scene_changes: Iterable[float]) -> list[ClipCandidate]:
    scenes = list(scene_changes)
    result = []
    for candidate in candidates:
        changes = sum(candidate.start <= timestamp <= candidate.end for timestamp in scenes)
        boost = min(changes * 0.18, 1.5)
        reason = f"{candidate.reason}; {changes} mudanças visuais"
        result.append(replace(candidate, score=round(candidate.score + boost, 3), reason=reason))
    return sorted(result, key=lambda item: item.start)


def boost_candidates_with_audio_energy(candidates: Iterable[ClipCandidate],
                                       spectral_rows: Iterable[dict]) -> list[ClipCandidate]:
    rows = list(spectral_rows)
    result = []
    for candidate in candidates:
        values = [row.get("rms", 0) for row in rows if candidate.start <= row.get("time", -1) <= candidate.end]
        energy = sum(values) / len(values) if values else 0
        boost = min(energy * 4, 1.2)
        result.append(replace(
            candidate, score=round(candidate.score + boost, 3),
            reason=f"{candidate.reason}; energia vocal {energy:.2f}",
        ))
    return sorted(result, key=lambda item: item.start)


def seconds_to_ass(value: float) -> str:
    hours = int(value // 3600)
    minutes = int(value % 3600 // 60)
    seconds = value % 60
    return f"{hours}:{minutes:02d}:{seconds:05.2f}"


def write_ass_subtitles(path: Path, segments: Iterable[TranscriptSegment], clip_start: float,
                        clip_end: float, style: str = "bold") -> Path:
    styles = {
        "bold": ("Arial", 54, "&H00FFFFFF", "&H00000000", 3),
        "minimal": ("Arial", 40, "&H00FFFFFF", "&H80000000", 1),
        "highlight": ("Arial", 52, "&H0000FFFF", "&H00000000", 3),
    }
    font, size, primary, outline, border = styles.get(style, styles["bold"])
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: Default,{font},{size},{primary},&H000000FF,{outline},&H80000000,-1,0,0,0,100,100,0,0,{border},3,0,2,60,60,150,1\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    events = []
    for segment in segments:
        start, end = max(segment.start, clip_start), min(segment.end, clip_end)
        if end <= start:
            continue
        text = segment.text.replace("{", "(").replace("}", ")").replace("\n", r"\N")
        if style == "highlight":
            words = text.split()
            word_duration = max(1, round((end - start) * 100 / max(1, len(words))))
            text = " ".join(rf"{{\k{word_duration}}}{word}" for word in words)
        events.append(f"Dialogue: 0,{seconds_to_ass(start - clip_start)},{seconds_to_ass(end - clip_start)},Default,,0,0,0,,{text}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path


def tempo_distance(track_bpm: float | None, target_bpm: float) -> float:
    if not track_bpm:
        return 1.0
    return min(abs(track_bpm - target_bpm), abs(track_bpm * 2 - target_bpm), abs(track_bpm / 2 - target_bpm))


def video_layout_filter(aspect_ratio: str, reframe: str = "center") -> str:
    dimensions = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1280, 720)}
    if aspect_ratio not in dimensions:
        raise ValueError("Aspect ratio must be 9:16, 1:1 or 16:9")
    width, height = dimensions[aspect_ratio]
    if reframe == "fit":
        return (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    return (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1")


def build_render_command(source: Path, output: Path, start: float, end: float,
                         aspect_ratio: str = "9:16", reframe: str = "center",
                         subtitles: Path | None = None, music: Path | None = None,
                         music_volume: float = 0.16) -> list[str]:
    duration = end - start
    if duration <= 0:
        raise ValueError("Clip end must be greater than its start")
    video_filter = video_layout_filter(aspect_ratio, reframe)
    if subtitles:
        escaped = str(subtitles).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        video_filter += f",ass='{escaped}'"

    command = ["ffmpeg", "-y", "-nostdin", "-ss", str(start), "-t", str(duration), "-i", str(source)]
    filter_parts = [f"[0:v]{video_filter}[vout]"]
    if music:
        command += ["-stream_loop", "-1", "-i", str(music)]
        fade_out = max(duration - 1.2, 0)
        filter_parts += [
            "[0:a]asplit=2[speech][sidechain]",
            f"[1:a]atrim=0:{duration},asetpts=PTS-STARTPTS,volume={music_volume},"
            f"afade=t=in:st=0:d=0.8,afade=t=out:st={fade_out}:d=1.2[music]",
            "[music][sidechain]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=500[ducked]",
            "[speech][ducked]amix=inputs=2:duration=first:normalize=0,"
            "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[aout]",
        ]
    else:
        filter_parts.append("[0:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[aout]")
    command += [
        "-filter_complex", ";".join(filter_parts), "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-threads", "2",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", "-shortest", str(output),
    ]
    return command


def intersect_ranges(ranges: Iterable[tuple[float, float]], start: float,
                     end: float) -> list[tuple[float, float]]:
    return [(max(left, start), min(right, end)) for left, right in ranges
            if min(right, end) - max(left, start) >= 0.1]


def build_silence_removal_command(source: Path, output: Path,
                                  ranges: Iterable[tuple[float, float]]) -> list[str]:
    selected = list(ranges)
    if not selected:
        raise ValueError("No audible ranges remain in this clip")
    filters: list[str] = []
    concat_inputs = []
    for index, (start, end) in enumerate(selected):
        filters += [
            f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{index}]",
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{index}]",
        ]
        concat_inputs.append(f"[v{index}][a{index}]")
    filters.append(f"{''.join(concat_inputs)}concat=n={len(selected)}:v=1:a=1[vout][aout]")
    return [
        "ffmpeg", "-y", "-nostdin", "-i", str(source), "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "23", "-threads", "2", "-c:a", "aac", "-b:a", "160k", str(output),
    ]


def compress_transcript_timeline(segments: Iterable[TranscriptSegment],
                                 ranges: Iterable[tuple[float, float]]) -> list[TranscriptSegment]:
    result: list[TranscriptSegment] = []
    elapsed = 0.0
    for range_start, range_end in ranges:
        for segment in segments:
            start, end = max(segment.start, range_start), min(segment.end, range_end)
            if end <= start:
                continue
            result.append(TranscriptSegment(
                elapsed + start - range_start,
                elapsed + end - range_start,
                segment.text,
            ))
        elapsed += range_end - range_start
    return result
