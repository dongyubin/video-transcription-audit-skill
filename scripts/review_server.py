#!/usr/bin/env python3
"""Lightweight local subtitle review server for video-transcription-audit."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable


EVIDENCE_DIR_NAME = "_审计证据"
DELIVERY_TEXT_NAME = "01_转录文本.txt"
DELIVERY_SRT_NAME = "02_字幕.srt"
DELIVERY_REVIEW_NAME = "03_待确认内容.md"
AUDIT_REPORT_NAME = "transcript.audit.md"
AUDIT_STATE_NAME = "audit.json"
REVIEW_DIR_NAME = "review"
WAVEFORM_NAME = "waveform.u8"
WAVEFORM_META_NAME = "waveform.meta.json"
EDIT_LOG_NAME = "edits.jsonl"
SAVE_TRANSACTION_NAME = "save-transaction.json"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
AUDIO_SUFFIXES = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}
SRT_TIME = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> "
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)
SHORT_TIME = re.compile(r"^(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?$")


class ReviewError(RuntimeError):
    pass


class ReviewConflict(ReviewError):
    pass


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def parse_srt_clock(match: re.Match[str]) -> tuple[float, float]:
    values = [int(value) for value in match.groups()]
    start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
    end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
    return start, end


def parse_srt_text(text: str) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    blocks = [
        block
        for block in re.split(r"\r?\n\r?\n", text.strip())
        if block.strip()
    ]
    for expected, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if len(lines) < 3:
            raise ReviewError(f"SRT cue {expected} has fewer than three lines")
        try:
            cue_id = int(lines[0])
        except ValueError as exc:
            raise ReviewError(f"SRT cue {expected} has an invalid number") from exc
        match = SRT_TIME.match(lines[1])
        if not match:
            raise ReviewError(f"SRT cue {expected} has an invalid timestamp")
        start, end = parse_srt_clock(match)
        cues.append(
            {
                "id": cue_id,
                "start": start,
                "end": end,
                "text": "\n".join(lines[2:]),
            }
        )
    if not cues:
        raise ReviewError("SRT contains no cues")
    return cues


def format_srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_short_time(seconds: float) -> str:
    total = max(0, int(float(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_short_time(value: str) -> float:
    match = SHORT_TIME.match(value.strip())
    if not match:
        raise ReviewError(f"Invalid short timestamp: {value}")
    hours, minutes, seconds = (int(match.group(i)) for i in range(1, 4))
    fraction = match.group(4) or ""
    millis = int(fraction.ljust(3, "0")) if fraction else 0
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def validate_cues(
    cues: Iterable[dict[str, Any]],
    duration: float,
) -> list[str]:
    errors: list[str] = []
    cue_list = list(cues)
    previous_end = 0.0
    expected_id = 1
    for cue in cue_list:
        cue_id = cue.get("id")
        if cue_id != expected_id:
            errors.append(f"Cue expected id {expected_id}, found {cue_id}")
        expected_id += 1
        try:
            start = float(cue["start"])
            end = float(cue["end"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"Cue {cue_id} has invalid times")
            continue
        if not (start >= 0 and end > start):
            errors.append(f"Cue {cue_id} has an invalid time range")
        if start + 0.05 < previous_end:
            errors.append(f"Cue {cue_id} overlaps the previous cue")
        if end > float(duration) + 0.05:
            errors.append(f"Cue {cue_id} exceeds media duration")
        text = cue.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"Cue {cue_id} has empty text")
        previous_end = end
    if not cue_list:
        errors.append("Subtitle list is empty")
    return errors


def cues_to_srt(cues: Iterable[dict[str, Any]]) -> str:
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n"
            f"{format_srt_time(cue['start'])} --> "
            f"{format_srt_time(cue['end'])}\n"
            f"{str(cue['text']).strip()}"
        )
    return "\n\n".join(blocks) + "\n"


def cues_to_text(cues: Iterable[dict[str, Any]]) -> str:
    return "\n".join(str(cue["text"]).replace("\n", " ").strip() for cue in cues) + "\n"


def expected_peak_count(duration: float, peaks_per_second: int) -> int:
    return max(1, int(round(float(duration) * int(peaks_per_second))))


class PeakAccumulator:
    def __init__(self, sample_rate: int, peaks_per_second: int):
        if sample_rate <= 0 or peaks_per_second <= 0:
            raise ValueError("Sample rate and peak rate must be positive")
        self.samples_per_peak = max(1, int(round(sample_rate / peaks_per_second)))
        self.current_peak = 0
        self.current_count = 0
        self.pending = b""
        self.peaks = bytearray()

    def feed(self, data: bytes) -> None:
        data = self.pending + data
        complete = len(data) - (len(data) % 2)
        self.pending = data[complete:]
        if not complete:
            return
        for (sample,) in struct.iter_unpack("<h", data[:complete]):
            self.current_peak = max(self.current_peak, min(32767, abs(sample)))
            self.current_count += 1
            if self.current_count >= self.samples_per_peak:
                self.peaks.append(round(self.current_peak / 32767 * 255))
                self.current_peak = 0
                self.current_count = 0

    def finish(self) -> bytes:
        if self.current_count:
            self.peaks.append(round(self.current_peak / 32767 * 255))
            self.current_peak = 0
            self.current_count = 0
        return bytes(self.peaks)


def pcm_to_u8_peaks(
    pcm: bytes,
    *,
    sample_rate: int,
    peaks_per_second: int,
) -> bytes:
    accumulator = PeakAccumulator(sample_rate, peaks_per_second)
    accumulator.feed(pcm)
    return accumulator.finish()


def default_probe(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ReviewError("FFprobe is required to verify the review media.")
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode:
        raise ReviewError(
            f"FFprobe could not read review media: {completed.stderr.strip()}"
        )
    payload = json.loads(completed.stdout)
    return {"duration_seconds": float(payload["format"]["duration"])}


def resolve_run_path(value: str | os.PathLike[str], evidence_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = evidence_dir / path
    return path


def select_media(
    run_data: dict[str, Any],
    evidence_dir: Path,
    *,
    override: str | os.PathLike[str] | None,
    probe: Callable[[Path], dict[str, Any]] = default_probe,
) -> dict[str, Any]:
    duration = float(run_data["duration_seconds"])
    candidates: list[tuple[Path, str]] = []
    if override:
        candidates.append((Path(override).expanduser(), "override"))
    else:
        input_value = run_data.get("input")
        if input_value:
            candidates.append((resolve_run_path(input_value, evidence_dir), "input"))
        normalized = run_data.get("normalized_audio")
        if normalized:
            candidates.append(
                (resolve_run_path(normalized, evidence_dir), "normalized-audio")
            )
        candidates.append((evidence_dir / "source" / "audio_16k.mp3", "normalized-audio"))

    selected: tuple[Path, str] | None = None
    seen: set[str] = set()
    for candidate, source in candidates:
        key = os.path.normcase(str(candidate.resolve(strict=False)))
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            selected = (candidate.resolve(), source)
            break
    if not selected:
        if override:
            raise ReviewError(f"Review media does not exist: {override}")
        raise ReviewError(
            "The original media and normalized review audio are both missing."
        )

    path, source = selected
    media_duration = float(probe(path)["duration_seconds"])
    if abs(media_duration - duration) > 2.0:
        raise ReviewError(
            f"Review media duration {media_duration:.3f}s does not match "
            f"the run duration {duration:.3f}s."
        )
    kind = "audio" if path.suffix.lower() in AUDIO_SUFFIXES else "video"
    return {
        "path": path,
        "kind": kind,
        "source": source,
        "duration": media_duration,
        "mime": mimetypes.guess_type(path.name)[0]
        or ("audio/mpeg" if kind == "audio" else "video/mp4"),
    }


def ensure_waveform(
    audio_path: Path,
    review_dir: Path,
    *,
    duration: float,
    peaks_per_second: int = 20,
    sample_rate: int = 16000,
) -> tuple[Path, dict[str, Any]]:
    review_dir.mkdir(parents=True, exist_ok=True)
    peak_path = review_dir / WAVEFORM_NAME
    meta_path = review_dir / WAVEFORM_META_NAME
    source_hash = hash_file(audio_path)
    expected = {
        "schema_version": 1,
        "source_sha256": source_hash,
        "duration_seconds": round(float(duration), 6),
        "sample_rate": sample_rate,
        "peaks_per_second": peaks_per_second,
        "encoding": "uint8-max-absolute",
    }
    if peak_path.is_file() and meta_path.is_file():
        try:
            current = json_read(meta_path)
            if all(current.get(key) == value for key, value in expected.items()):
                if peak_path.stat().st_size == int(current.get("peak_count") or -1):
                    return peak_path, current
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ReviewError("FFmpeg is required to generate the review waveform.")
    process = subprocess.Popen(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(audio_path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout
    accumulator = PeakAccumulator(sample_rate, peaks_per_second)
    for chunk in iter(lambda: process.stdout.read(64 * 1024), b""):
        accumulator.feed(chunk)
    stderr = process.stderr.read() if process.stderr else b""
    return_code = process.wait()
    if return_code:
        raise ReviewError(
            "FFmpeg waveform generation failed: "
            + stderr.decode("utf-8", errors="replace")[:500]
        )
    peaks = accumulator.finish()
    meta = {
        **expected,
        "peak_count": len(peaks),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    atomic_write(peak_path, peaks)
    atomic_write(meta_path, json_bytes(meta))
    return peak_path, meta


def split_markdown_row(line: str) -> list[str]:
    if not line.startswith("|"):
        return []
    placeholder = "\u0000"
    protected = line.replace("\\|", placeholder)
    return [
        item.strip().replace(placeholder, "|")
        for item in protected.strip().strip("|").split("|")
    ]


def escape_markdown_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def parse_audit_report(text: str) -> list[dict[str, Any]]:
    rows = []
    for line_index, line in enumerate(text.splitlines()):
        cells = split_markdown_row(line)
        if len(cells) != 5 or cells[0] in {"Time", "---", "—"}:
            continue
        if "–" not in cells[0]:
            continue
        start_text, end_text = cells[0].split("–", 1)
        try:
            start = parse_short_time(start_text)
            end = parse_short_time(end_text)
        except ReviewError:
            continue
        decision = cells[4]
        rows.append(
            {
                "id": f"audit-{len(rows) + 1}",
                "lineIndex": line_index,
                "start": start,
                "end": end,
                "timeRange": cells[0],
                "reason": cells[1],
                "primary": cells[2],
                "secondary": cells[3],
                "decision": decision,
                "unresolved": decision.strip().lower().startswith("unresolved"),
            }
        )
    return rows


def cue_ids_for_interval(
    cues: Iterable[dict[str, Any]],
    start: float,
    end: float,
) -> list[int]:
    return [
        int(cue["id"])
        for cue in cues
        if float(cue["start"]) < end and float(cue["end"]) > start
    ]


def update_audit_report(
    text: str,
    audit_rows: list[dict[str, Any]],
    cues: list[dict[str, Any]],
    confirmed_ids: set[str],
) -> tuple[str, list[dict[str, Any]]]:
    lines = text.splitlines()
    cue_map = {int(cue["id"]): cue for cue in cues}
    for row in audit_rows:
        row["cueIds"] = cue_ids_for_interval(cues, row["start"], row["end"])
        if row["id"] not in confirmed_ids:
            continue
        reviewed = " ".join(
            str(cue_map[cue_id]["text"]).replace("\n", " ").strip()
            for cue_id in row["cueIds"]
            if cue_id in cue_map
        ).strip()
        row["decision"] = f"confirmed by visual review: {reviewed or 'confirmed'}"
        row["unresolved"] = False
        cells = split_markdown_row(lines[row["lineIndex"]])
        cells[4] = row["decision"]
        lines[row["lineIndex"]] = "| " + " | ".join(
            escape_markdown_cell(cell) for cell in cells
        ) + " |"
    return "\n".join(lines) + "\n", audit_rows


def review_summary(audit_rows: list[dict[str, Any]]) -> str:
    unresolved = [row for row in audit_rows if row.get("unresolved")]
    lines = ["# 待确认内容", "", "- 状态：审计完成"]
    if not unresolved:
        lines.extend(["- 待确认：0 处", "", "无需人工确认。"])
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            f"- 待确认：{len(unresolved)} 处",
            "",
            f"以下 {len(unresolved)} 处内容需要确认：",
        ]
    )
    for index, row in enumerate(unresolved, start=1):
        lines.extend(
            [
                "",
                f"## {index}. {row['timeRange']}",
                "",
                f"- 原因：{row['reason'] or '模型结果不一致'}",
                f"- 主模型候选：{row['primary'] or '无'}",
                f"- 复核模型候选：{row['secondary'] or '无'}",
            ]
        )
    return "\n".join(lines) + "\n"


def refresh_review_summary(
    existing: str,
    audit_rows: list[dict[str, Any]],
) -> str:
    unresolved = [row for row in audit_rows if row.get("unresolved")]
    if not unresolved:
        return review_summary(audit_rows)
    parts = re.split(r"(?m)^##\s+\d+\.\s+(.+?)\r?\n", existing)
    blocks: dict[str, str] = {}
    for index in range(1, len(parts), 2):
        if index + 1 < len(parts):
            blocks[parts[index].strip()] = parts[index + 1].strip()
    if not blocks:
        return review_summary(audit_rows)
    lines = [
        "# 待确认内容",
        "",
        "- 状态：审计完成",
        f"- 待确认：{len(unresolved)} 处",
        "",
        f"以下 {len(unresolved)} 处内容需要确认：",
    ]
    for index, row in enumerate(unresolved, start=1):
        time_range = str(row["timeRange"])
        body = blocks.get(time_range)
        if body is None:
            body = "\n".join(
                [
                    f"- 原因：{row['reason'] or '模型结果不一致'}",
                    f"- 主模型候选：{row['primary'] or '无'}",
                    f"- 复核模型候选：{row['secondary'] or '无'}",
                ]
            )
        lines.extend(["", f"## {index}. {time_range}", "", body])
    return "\n".join(lines).rstrip() + "\n"


def compute_revision(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class ReviewPaths:
    root: Path
    evidence: Path
    transcript: Path
    subtitles: Path
    review_summary: Path
    audit_report: Path
    audit_state: Path
    run_manifest: Path
    review_dir: Path
    edit_log: Path


def review_paths(run_dir: Path) -> ReviewPaths:
    root = Path(run_dir).expanduser().resolve()
    evidence = root / EVIDENCE_DIR_NAME
    if not (evidence / "run.json").is_file():
        if (root / "run.json").is_file():
            raise ReviewError(
                "Visual review requires the delivery/evidence layout. "
                "Run `organize` on this legacy result first."
            )
        raise ReviewError(f"run.json not found under {evidence}")
    return ReviewPaths(
        root=root,
        evidence=evidence,
        transcript=root / DELIVERY_TEXT_NAME,
        subtitles=root / DELIVERY_SRT_NAME,
        review_summary=root / DELIVERY_REVIEW_NAME,
        audit_report=evidence / AUDIT_REPORT_NAME,
        audit_state=evidence / AUDIT_STATE_NAME,
        run_manifest=evidence / "run.json",
        review_dir=evidence / REVIEW_DIR_NAME,
        edit_log=evidence / REVIEW_DIR_NAME / EDIT_LOG_NAME,
    )


def recover_save_transaction(paths: ReviewPaths) -> None:
    manifest_path = paths.review_dir / SAVE_TRANSACTION_NAME
    if not manifest_path.is_file():
        return
    manifest = json_read(manifest_path)
    transaction_dir = paths.review_dir / str(manifest["transaction"])
    for item in manifest.get("files") or []:
        target = paths.root / item["target"]
        backup = transaction_dir / "backup" / item["backup"]
        if backup.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, target)
        elif not item.get("existed") and target.exists():
            target.unlink()
    if transaction_dir.exists():
        shutil.rmtree(transaction_dir)
    manifest_path.unlink(missing_ok=True)


class ReviewSession:
    def __init__(
        self,
        run_dir: Path,
        *,
        media_override: str | os.PathLike[str] | None = None,
        probe: Callable[[Path], dict[str, Any]] = default_probe,
    ):
        self.paths = review_paths(run_dir)
        self.paths.review_dir.mkdir(parents=True, exist_ok=True)
        recover_save_transaction(self.paths)
        self.run_data = json_read(self.paths.run_manifest)
        self.media = select_media(
            self.run_data,
            self.paths.evidence,
            override=media_override,
            probe=probe,
        )
        self.waveform_path: Path | None = None
        self.waveform_meta: dict[str, Any] | None = None

    @property
    def revision_paths(self) -> list[Path]:
        return [
            self.paths.transcript,
            self.paths.subtitles,
            self.paths.review_summary,
            self.paths.audit_report,
            self.paths.audit_state,
            self.paths.run_manifest,
        ]

    def current_revision(self) -> str:
        return compute_revision(self.revision_paths)

    def load_cues(self) -> list[dict[str, Any]]:
        return parse_srt_text(self.paths.subtitles.read_text(encoding="utf-8"))

    def load_audit(self, cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.paths.audit_report.is_file():
            return []
        rows = parse_audit_report(
            self.paths.audit_report.read_text(encoding="utf-8")
        )
        state_intervals: list[dict[str, Any]] = []
        if self.paths.audit_state.is_file():
            try:
                payload = json_read(self.paths.audit_state)
                state_intervals = list(payload.get("intervals") or [])
            except (OSError, ValueError, json.JSONDecodeError):
                state_intervals = []
        for index, row in enumerate(rows):
            if index < len(state_intervals):
                stored = state_intervals[index]
                for key in ("clip", "tertiary", "error", "secondary_backend"):
                    if stored.get(key) is not None:
                        row[key] = stored[key]
            row["cueIds"] = cue_ids_for_interval(cues, row["start"], row["end"])
        return rows

    def prepare_waveform(self) -> tuple[Path, dict[str, Any]]:
        normalized = resolve_run_path(
            self.run_data["normalized_audio"],
            self.paths.evidence,
        )
        self.waveform_path, self.waveform_meta = ensure_waveform(
            normalized,
            self.paths.review_dir,
            duration=float(self.run_data["duration_seconds"]),
        )
        return self.waveform_path, self.waveform_meta

    def session_payload(self) -> dict[str, Any]:
        cues = self.load_cues()
        audit = self.load_audit(cues)
        unresolved_ids = {
            cue_id
            for row in audit
            if row["unresolved"]
            for cue_id in row["cueIds"]
        }
        for cue in cues:
            cue["unresolved"] = int(cue["id"]) in unresolved_ids
        return {
            "revision": self.current_revision(),
            "duration": float(self.run_data["duration_seconds"]),
            "title": self.paths.root.name,
            "media": {
                "kind": self.media["kind"],
                "name": self.media["path"].name,
                "source": self.media["source"],
                "mime": self.media["mime"],
            },
            "cues": cues,
            "audit": audit,
            "unresolvedCount": sum(1 for row in audit if row["unresolved"]),
            "waveform": self.waveform_meta,
        }

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        supplied_revision = str(payload.get("revision") or "")
        if supplied_revision != self.current_revision():
            raise ReviewConflict(
                "Subtitle files changed outside this review session. Reload first."
            )
        original_cues = self.load_cues()
        submitted = payload.get("cues")
        if not isinstance(submitted, list):
            raise ReviewError("cues must be an array")
        cues = [
            {
                "id": item.get("id"),
                "start": round(float(item.get("start")), 3),
                "end": round(float(item.get("end")), 3),
                "text": str(item.get("text") or "").strip(),
            }
            for item in submitted
        ]
        if len(cues) != len(original_cues):
            raise ReviewError("Adding or removing subtitle cues is not supported.")
        errors = validate_cues(cues, float(self.run_data["duration_seconds"]))
        if errors:
            raise ReviewError("; ".join(errors))

        audited = self.paths.audit_report.is_file()
        audit_text = (
            self.paths.audit_report.read_text(encoding="utf-8")
            if audited
            else ""
        )
        audit_rows = self.load_audit(original_cues) if audited else []
        valid_confirmations = {
            row["id"] for row in audit_rows if row["unresolved"]
        }
        requested_confirmations = {
            str(item) for item in (payload.get("confirmedAuditIds") or [])
        }
        if not requested_confirmations.issubset(valid_confirmations):
            raise ReviewError("An audit confirmation is missing or already resolved.")
        if audited:
            updated_audit, audit_rows = update_audit_report(
                audit_text,
                audit_rows,
                cues,
                requested_confirmations,
            )
        else:
            updated_audit = ""
        unresolved_count = sum(1 for row in audit_rows if row["unresolved"])
        audit_state = {
            "schema_version": 1,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "intervals": [
                {
                    key: row[key]
                    for key in (
                        "start",
                        "end",
                        "reason",
                        "primary",
                        "secondary",
                        "tertiary",
                        "clip",
                        "secondary_backend",
                        "error",
                        "decision",
                    )
                    if row.get(key) is not None
                }
                for row in audit_rows
            ],
        }
        run_data = json_read(self.paths.run_manifest)
        if run_data.get("audit") is not None:
            run_data["audit"]["unresolved_count"] = unresolved_count
        run_data["review"] = {
            "last_saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "editor": "visual-review",
            "unresolved_count": unresolved_count,
        }

        changes = []
        for before, after in zip(original_cues, cues):
            normalized_before = {
                "id": int(before["id"]),
                "start": round(float(before["start"]), 3),
                "end": round(float(before["end"]), 3),
                "text": str(before["text"]),
            }
            if normalized_before != after:
                changes.append({"before": normalized_before, "after": after})
        log_entry = {
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "previous_revision": supplied_revision,
            "changes": changes,
            "confirmed_audit_ids": sorted(requested_confirmations),
        }
        existing_log = (
            self.paths.edit_log.read_bytes()
            if self.paths.edit_log.is_file()
            else b""
        )
        updated_log = existing_log + (
            json.dumps(log_entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")

        contents = {
            self.paths.transcript: cues_to_text(cues).encode("utf-8"),
            self.paths.subtitles: cues_to_srt(cues).encode("utf-8"),
            self.paths.run_manifest: json_bytes(run_data),
            self.paths.edit_log: updated_log,
        }
        if audited:
            contents[self.paths.review_summary] = refresh_review_summary(
                self.paths.review_summary.read_text(encoding="utf-8")
                if self.paths.review_summary.is_file()
                else "",
                audit_rows,
            ).encode("utf-8")
            contents[self.paths.audit_report] = updated_audit.encode("utf-8")
            contents[self.paths.audit_state] = json_bytes(audit_state)
        self._replace_transaction(contents)
        return {
            "revision": self.current_revision(),
            "unresolvedCount": unresolved_count,
            "changedCueCount": len(changes),
            "confirmedAuditCount": len(requested_confirmations),
        }

    def _replace_transaction(self, contents: dict[Path, bytes]) -> None:
        self.paths.review_dir.mkdir(parents=True, exist_ok=True)
        transaction_name = f".save-{secrets.token_hex(8)}"
        transaction_dir = self.paths.review_dir / transaction_name
        staged_dir = transaction_dir / "staged"
        backup_dir = transaction_dir / "backup"
        staged_dir.mkdir(parents=True)
        backup_dir.mkdir()
        file_records = []
        for index, (target, content) in enumerate(contents.items()):
            staged = staged_dir / str(index)
            staged.write_bytes(content)
            if target == self.paths.subtitles:
                errors = validate_cues(
                    parse_srt_text(content.decode("utf-8")),
                    float(self.run_data["duration_seconds"]),
                )
                if errors:
                    shutil.rmtree(transaction_dir)
                    raise ReviewError("; ".join(errors))
            relative = os.path.relpath(target, self.paths.root)
            file_records.append(
                {
                    "target": relative,
                    "staged": str(index),
                    "backup": str(index),
                    "existed": target.is_file(),
                }
            )
        manifest = {
            "transaction": transaction_name,
            "files": file_records,
        }
        manifest_path = self.paths.review_dir / SAVE_TRANSACTION_NAME
        atomic_write(manifest_path, json_bytes(manifest))
        try:
            for record in file_records:
                target = self.paths.root / record["target"]
                target.parent.mkdir(parents=True, exist_ok=True)
                backup = backup_dir / record["backup"]
                if target.is_file():
                    shutil.copy2(target, backup)
                os.replace(staged_dir / record["staged"], target)
        except Exception:
            for record in reversed(file_records):
                target = self.paths.root / record["target"]
                backup = backup_dir / record["backup"]
                if backup.is_file():
                    os.replace(backup, target)
                elif not record["existed"] and target.exists():
                    target.unlink()
            raise
        finally:
            manifest_path.unlink(missing_ok=True)
            if transaction_dir.exists():
                shutil.rmtree(transaction_dir)


def parse_byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match:
        raise ReviewError("Invalid Range header")
    first, last = match.groups()
    if not first and not last:
        raise ReviewError("Invalid Range header")
    if first:
        start = int(first)
        end = int(last) if last else size - 1
    else:
        length = int(last)
        start = max(0, size - length)
        end = size - 1
    if start >= size or start > end:
        raise ReviewError("Requested range is outside the media")
    return start, min(end, size - 1)


def write_file_response(
    handler: BaseHTTPRequestHandler,
    path: Path,
    *,
    content_type: str,
    allow_range: bool,
    send_body: bool,
) -> None:
    size = path.stat().st_size
    try:
        byte_range = (
            parse_byte_range(handler.headers.get("Range"), size)
            if allow_range
            else None
        )
    except ReviewError:
        handler.send_error(416)
        return
    if byte_range:
        start, end = byte_range
        handler.send_response(206)
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        content_length = end - start + 1
    else:
        start, end = 0, size - 1
        handler.send_response(200)
        content_length = size
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(content_length))
    if allow_range:
        handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    if not send_body:
        return
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = content_length
        while remaining:
            block = handle.read(min(64 * 1024, remaining))
            if not block:
                break
            handler.wfile.write(block)
            remaining -= len(block)


def create_review_http_server(
    session: ReviewSession,
    assets_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> tuple[ThreadingHTTPServer, str]:
    assets_root = Path(assets_dir).resolve()
    if not (assets_root / "index.html").is_file():
        raise ReviewError(f"Review UI assets are incomplete: {assets_root}")
    token = secrets.token_urlsafe(24)
    session.prepare_waveform()

    class ReviewHandler(BaseHTTPRequestHandler):
        server_version = "VideoTranscriptionReview/1"

        def log_message(self, _format: str, *args: object) -> None:
            return

        def _route(self) -> str | None:
            host_header = self.headers.get("Host", "")
            if not (
                host_header.startswith("127.0.0.1:")
                or host_header.startswith("localhost:")
            ):
                self.send_error(403)
                return None
            path = urllib.parse.urlsplit(self.path).path
            prefix = f"/{token}"
            if path != prefix and not path.startswith(prefix + "/"):
                self.send_error(404)
                return None
            return path[len(prefix) :] or "/"

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )

        def end_headers(self) -> None:
            self._security_headers()
            super().end_headers()

        def do_HEAD(self) -> None:
            self._handle_get(send_body=False)

        def do_GET(self) -> None:
            self._handle_get(send_body=True)

        def _handle_get(self, *, send_body: bool) -> None:
            route = self._route()
            if route is None:
                return
            if route == "/api/session":
                content = json_bytes(
                    {
                        **session.session_payload(),
                        "basePath": f"/{token}",
                    }
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if send_body:
                    self.wfile.write(content)
                return
            if route == "/media":
                write_file_response(
                    self,
                    session.media["path"],
                    content_type=session.media["mime"],
                    allow_range=True,
                    send_body=send_body,
                )
                return
            if route == "/waveform":
                assert session.waveform_path
                write_file_response(
                    self,
                    session.waveform_path,
                    content_type="application/octet-stream",
                    allow_range=False,
                    send_body=send_body,
                )
                return
            relative = "index.html" if route == "/" else route.lstrip("/")
            decoded = urllib.parse.unquote(relative)
            candidate = (assets_root / decoded).resolve()
            try:
                candidate.relative_to(assets_root)
            except ValueError:
                self.send_error(404)
                return
            if not candidate.is_file():
                self.send_error(404)
                return
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            if candidate.suffix in {".html", ".js", ".css"}:
                content_type += "; charset=utf-8"
            write_file_response(
                self,
                candidate,
                content_type=content_type,
                allow_range=False,
                send_body=send_body,
            )

        def do_POST(self) -> None:
            route = self._route()
            if route is None:
                return
            if route != "/api/save":
                self.send_error(404)
                return
            try:
                content_length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self.send_error(400)
                return
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                self.send_error(413)
                return
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                result = session.save(payload)
                content = json_bytes(result)
                self.send_response(200)
            except ReviewConflict as exc:
                content = json_bytes({"error": str(exc), "code": "revision-conflict"})
                self.send_response(409)
            except (ReviewError, ValueError, TypeError, json.JSONDecodeError) as exc:
                content = json_bytes({"error": str(exc), "code": "invalid-review"})
                self.send_response(400)
            except Exception as exc:
                content = json_bytes(
                    {
                        "error": f"Save failed: {type(exc).__name__}",
                        "code": "save-failed",
                    }
                )
                self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer((host, int(port)), ReviewHandler)
    server.daemon_threads = True
    return server, token


def serve_review(
    run_dir: Path,
    assets_dir: Path,
    *,
    media_override: str | os.PathLike[str] | None = None,
    port: int = 0,
    open_browser: bool = True,
) -> dict[str, Any]:
    session = ReviewSession(run_dir, media_override=media_override)
    server, token = create_review_http_server(
        session,
        assets_dir,
        host="127.0.0.1",
        port=port,
    )
    url = f"http://127.0.0.1:{server.server_port}/{token}/"
    result = {
        "url": url,
        "run_dir": str(session.paths.root),
        "media": str(session.media["path"]),
        "media_kind": session.media["kind"],
        "waveform_bytes": session.waveform_path.stat().st_size
        if session.waveform_path
        else 0,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return result
