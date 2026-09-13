"""Finding the inputs for each video.

Frames only. A transcript is used if one happens to be present, but nothing
requires it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import config


@dataclass
class VideoSource:
    video_id: str
    frame: Path
    transcript: str = ""          # empty in the frames-only setup
    transcript_path: Path | None = None

    @property
    def has_transcript(self) -> bool:
        return bool(self.transcript.strip())


_TIMESTAMP = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->")
_CUE_INDEX = re.compile(r"^\d+$")
_TAGS = re.compile(r"</?[cvi][^>]*>")


def clean_transcript(text: str, suffix: str) -> str:
    """Strip VTT/SRT scaffolding down to plain spoken text."""
    if suffix.lower() not in (".vtt", ".srt"):
        return text.strip()
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or line.startswith("NOTE "):
            continue
        if _TIMESTAMP.match(line) or _CUE_INDEX.match(line):
            continue
        line = _TAGS.sub("", line)
        if lines and line == lines[-1]:      # VTT scroll-up repeats
            continue
        lines.append(line)
    return " ".join(lines).strip()


def _first_existing(folder: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        p = folder / name
        if p.exists():
            return p
    return None


def load_video(folder: Path) -> VideoSource | None:
    frame = _first_existing(folder, config.FRAME_NAMES)
    if frame is None:
        print(f"  ! {folder.name}: no frame image ({'/'.join(config.FRAME_NAMES)})")
        return None

    src = VideoSource(folder.name, frame)

    tpath = _first_existing(folder, config.TRANSCRIPT_NAMES)
    if tpath is not None:
        text = clean_transcript(tpath.read_text(encoding="utf-8", errors="replace"), tpath.suffix)
        if text:
            src.transcript = text
            src.transcript_path = tpath
    return src


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def nested_discover(input_dir: Path) -> list[VideoSource]:
    """One subdirectory per video: input/video001/frame.png"""
    out = []
    for folder in sorted(p for p in input_dir.iterdir() if p.is_dir()):
        src = load_video(folder)
        if src:
            out.append(src)
    return out


def flat_discover(input_dir: Path) -> list[VideoSource]:
    """One flat directory of images named <video_id>.png.

    This is what a synced cloud folder looks like — Google Drive for Desktop,
    Box Drive, OneDrive, or rclone all mirror a folder of frames as a flat
    directory. Nobody exports 3,000 files into 3,000 subdirectories.
    """
    out = []
    for path in sorted(input_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
            continue
        src = VideoSource(path.stem, path)
        # Pick up a sibling transcript if one happens to exist: video001.txt
        for ext in (".txt", ".vtt", ".srt"):
            sibling = path.with_suffix(ext)
            if sibling.exists():
                text = clean_transcript(
                    sibling.read_text(encoding="utf-8", errors="replace"), ext)
                if text:
                    src.transcript, src.transcript_path = text, sibling
                break
        out.append(src)
    return out


def audit(found: list[VideoSource]) -> list[str]:
    """Problems that would corrupt a run. Empty list means the input is clean.

    The filename stem becomes the video id, and ids are sanitised to
    ^[A-Za-z0-9_-]+$ for the Batches API. Two files can therefore collapse onto
    one id -- "9HW30 1.png" and "9HW30-1.png" both become "9HW30-1" -- and the
    second would silently overwrite the first's extraction. At 3,000 files that
    is data loss nobody notices.
    """
    import questions as Q

    problems, seen = [], {}
    for src in found:
        ident = Q.question_id(src.video_id, 1)[:-3]
        if ident in seen and seen[ident] != src.video_id:
            problems.append(
                f"id collision: '{seen[ident]}' and '{src.video_id}' both become "
                f"'{ident}' — rename one")
        seen[ident] = src.video_id

        if len(ident) > 58:   # 64-char custom_id ceiling, less room for "-q99"
            problems.append(
                f"name too long for a batch id ({len(ident)} chars): '{src.video_id}'")
        if not src.frame.exists() or src.frame.stat().st_size == 0:
            problems.append(f"empty or missing file: {src.frame}")
    return problems


def discover(input_dir: Path | None = None, layout: str | None = None) -> list[VideoSource]:
    """Every usable video under the input directory, sorted by id.

    Layout is auto-detected by default: loose image files mean flat,
    subdirectories mean nested. Override with MW_LAYOUT=flat|nested if you
    have both and the guess is wrong.
    """
    input_dir = input_dir or config.INPUT_DIR
    if not input_dir.exists():
        raise FileNotFoundError(f"input directory not found: {input_dir}")

    layout = layout or config.INPUT_LAYOUT
    if layout == "auto":
        loose = sum(1 for p in input_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
        subdirs = sum(1 for p in input_dir.iterdir() if p.is_dir())
        layout = "flat" if loose > subdirs else "nested"

    found = flat_discover(input_dir) if layout == "flat" else nested_discover(input_dir)
    print(f"  {len(found)} videos found ({layout} layout in {input_dir})")

    for problem in audit(found):
        print(f"  ! {problem}")
    return found
