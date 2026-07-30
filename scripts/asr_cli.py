#!/usr/bin/env python3
"""Cross-platform local/cloud transcription and evidence-preserving audit CLI."""

from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import difflib
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable


SILICONFLOW_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
SILICONFLOW_MODELS = {
    "sensevoice": "FunAudioLLM/SenseVoiceSmall",
    "telespeech": "TeleAI/TeleSpeechASR",
}
SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{16,}")
TEXT_SUFFIXES = {".json", ".txt", ".srt", ".vtt", ".tsv", ".md", ".log"}
_DLL_HANDLES: list[Any] = []
SCRIPT_DIR = Path(__file__).resolve().parent
REQUIREMENT_FILES = {
    "base": SCRIPT_DIR / "requirements-base.txt",
    "macos": SCRIPT_DIR / "requirements-macos.txt",
    "nvidia": SCRIPT_DIR / "requirements-nvidia.txt",
}
PACKAGE_INDEXES = (
    ("official", "https://pypi.org/simple"),
    ("tsinghua", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("aliyun", "https://mirrors.aliyun.com/pypi/simple"),
    ("tencent", "https://mirrors.cloud.tencent.com/pypi/simple"),
    ("ustc", "https://pypi.mirrors.ustc.edu.cn/simple"),
)
CPU_COMPUTE_TYPE_ORDER = ("int8", "int8_float32", "int16", "float32")
CUDA_COMPUTE_TYPE_ORDER = (
    "float16",
    "int8_float16",
    "int8",
    "int8_float32",
    "bfloat16",
    "float32",
)


class AsrError(RuntimeError):
    pass


def eprint(*values: object) -> None:
    print(*values, file=sys.stderr)


def json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def json_read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_command(
    command: list[str],
    *,
    capture: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def requirement_parser() -> Any:
    try:
        from packaging.requirements import Requirement
    except ImportError:
        try:
            from pip._vendor.packaging.requirements import Requirement
        except ImportError as exc:
            raise AsrError(
                "Cannot evaluate dependency versions because packaging is unavailable."
            ) from exc
    return Requirement


def read_requirements(groups: Iterable[str]) -> list[str]:
    requirements: list[str] = []
    for group in groups:
        path = REQUIREMENT_FILES[group]
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line:
                requirements.append(line)
    return requirements


def evaluate_requirements(
    requirements: Iterable[str],
    *,
    version_getter: Callable[[str], str | None] = package_version,
) -> list[dict[str, Any]]:
    Requirement = requirement_parser()
    status: list[dict[str, Any]] = []
    for spec in requirements:
        requirement = Requirement(spec)
        installed = version_getter(requirement.name)
        satisfied = bool(
            installed
            and (
                not requirement.specifier
                or requirement.specifier.contains(installed, prereleases=True)
            )
        )
        status.append(
            {
                "name": requirement.name,
                "required": str(requirement.specifier) or "any",
                "spec": spec,
                "installed": installed,
                "satisfied": satisfied,
            }
        )
    return status


def required_groups(profile: str, report: dict[str, Any]) -> list[str]:
    if profile not in {"auto", "local", "cloud"}:
        raise ValueError(f"Unsupported profile: {profile}")
    groups = ["base"]
    if profile == "cloud":
        return groups
    if report.get("apple_silicon"):
        groups.append("macos")
        return groups
    nvidia = report.get("nvidia")
    if nvidia and nvidia.get("memory_gb", 0) >= 4:
        groups.append("nvidia")
    return groups


def select_cuda_compute_type(
    memory_gb: float,
    supported: Iterable[str] | None = None,
) -> str:
    available = set(supported or ())
    if memory_gb >= 8:
        order = CUDA_COMPUTE_TYPE_ORDER
    else:
        order = (
            "int8_float16",
            "int8",
            "float16",
            "int8_float32",
            "float32",
        )
    if not available:
        return "float16" if memory_gb >= 8 else "int8_float16"
    for compute_type in order:
        if compute_type in available:
            return compute_type
    return sorted(available)[0]


def select_cpu_compute_type(supported: Iterable[str] | None = None) -> str:
    available = set(supported or ())
    if not available:
        return "int8"
    for compute_type in CPU_COMPUTE_TYPE_ORDER:
        if compute_type in available:
            return compute_type
    return sorted(available)[0]


def probe_package_index(url: str, timeout: float = 3.0) -> float | None:
    target = url.rstrip("/") + "/faster-whisper/"
    request = urllib.request.Request(
        target,
        headers={"User-Agent": "video-transcription-audit-installer/1"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1024)
            if getattr(response, "status", 200) >= 400:
                return None
    except (OSError, urllib.error.URLError):
        return None
    return time.monotonic() - started


def select_fastest_index(
    candidates: Iterable[tuple[str, str]] = PACKAGE_INDEXES,
    *,
    probe: Callable[[str, float], float | None] = probe_package_index,
    timeout: float = 3.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidate_list = list(candidates)
    elapsed_values: list[float | None] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, len(candidate_list))
    ) as executor:
        futures = [
            executor.submit(probe, url, timeout)
            for _, url in candidate_list
        ]
        for future in futures:
            try:
                elapsed_values.append(future.result())
            except Exception:
                elapsed_values.append(None)
    results: list[dict[str, Any]] = []
    available: list[tuple[float, str, str]] = []
    for (name, url), elapsed in zip(candidate_list, elapsed_values):
        rounded = round(elapsed, 4) if elapsed is not None else None
        results.append(
            {
                "name": name,
                "url": url,
                "elapsed_seconds": rounded,
            }
        )
        if elapsed is not None:
            available.append((elapsed, name, url))
    if not available:
        raise AsrError("No configured Python package index is reachable.")
    elapsed, name, url = min(available, key=lambda item: item[0])
    return (
        {
            "name": name,
            "url": url,
            "elapsed_seconds": round(elapsed, 4),
        },
        results,
    )


def format_timestamp(seconds: float, separator: str = ",") -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return (
        f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}"
        f"{separator}{milliseconds:03d}"
    )


def short_timestamp(seconds: float) -> str:
    whole = max(0, round(float(seconds)))
    hours, remainder = divmod(whole, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def total_memory_gb() -> float | None:
    try:
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return round(status.ullTotalPhys / (1024**3), 2)
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round((pages * page_size) / (1024**3), 2)
    except (AttributeError, OSError, ValueError):
        return None


def detect_nvidia() -> dict[str, Any] | None:
    if not command_exists("nvidia-smi"):
        return None
    query = [
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = run_command(query)
    except (OSError, subprocess.CalledProcessError):
        return None
    line = next((item.strip() for item in result.stdout.splitlines() if item.strip()), "")
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 3:
        return None
    try:
        memory_mb = int(float(parts[2]))
    except ValueError:
        return None
    return {
        "name": parts[0],
        "driver_version": parts[1],
        "memory_mb": memory_mb,
        "memory_gb": round(memory_mb / 1024, 2),
    }


def platform_report() -> dict[str, Any]:
    system = platform.system()
    machine = platform.machine().lower()
    return {
        "system": system,
        "release": platform.release(),
        "machine": machine,
        "python": platform.python_version(),
        "executable": sys.executable,
        "memory_gb": total_memory_gb(),
        "apple_silicon": system == "Darwin" and machine in {"arm64", "aarch64"},
        "nvidia": detect_nvidia(),
    }


def choose_backend(
    report: dict[str, Any],
    *,
    api_key_present: bool,
    profile: str = "auto",
) -> dict[str, str]:
    if profile not in {"auto", "local", "cloud"}:
        raise ValueError(f"Unsupported profile: {profile}")
    if profile == "cloud":
        return {
            "backend": "siliconflow",
            "model": SILICONFLOW_MODELS["sensevoice"],
            "device": "cloud",
            "compute_type": "provider",
        }
    if report.get("apple_silicon"):
        memory = report.get("memory_gb") or 0
        model = (
            "mlx-community/whisper-large-v3-mlx-4bit"
            if memory >= 16
            else "mlx-community/whisper-small-mlx"
        )
        return {
            "backend": "mlx-whisper",
            "model": model,
            "device": "metal",
            "compute_type": "mlx",
        }

    nvidia = report.get("nvidia")
    ctranslate2 = report.get("ctranslate2")
    cuda_runtime_ready = ctranslate2 is None or bool(
        ctranslate2.get("import_ok")
        and ctranslate2.get("cuda_device_count", 0) > 0
        and ctranslate2.get("supported_compute_types", {}).get("cuda")
    )
    if nvidia and nvidia.get("memory_gb", 0) >= 4 and cuda_runtime_ready:
        supported = None
        if ctranslate2:
            supported = ctranslate2.get("supported_compute_types", {}).get("cuda")
        compute_type = select_cuda_compute_type(
            float(nvidia["memory_gb"]),
            supported,
        )
        return {
            "backend": "faster-whisper",
            "model": "large-v3",
            "device": "cuda",
            "compute_type": compute_type,
        }

    if profile == "auto" and api_key_present:
        return {
            "backend": "siliconflow",
            "model": SILICONFLOW_MODELS["sensevoice"],
            "device": "cloud",
            "compute_type": "provider",
        }

    cpu_supported = None
    if ctranslate2 and ctranslate2.get("import_ok"):
        cpu_supported = ctranslate2.get("supported_compute_types", {}).get("cpu")
    return {
        "backend": "faster-whisper",
        "model": "small",
        "device": "cpu",
        "compute_type": select_cpu_compute_type(cpu_supported),
    }


def asr_home() -> Path:
    configured = os.environ.get("ASR_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".video-transcription-audit"


def configure_nvidia_library_paths() -> list[str]:
    roots: list[Path] = []
    for site_path in sys.path:
        candidate = Path(site_path) / "nvidia"
        if candidate.is_dir():
            roots.append(candidate)
    paths: list[Path] = []
    for root in roots:
        for relative in (
            "cublas/bin",
            "cudnn/bin",
            "cuda_nvrtc/bin",
            "cublas/lib",
            "cudnn/lib",
            "cuda_nvrtc/lib",
        ):
            candidate = root / relative
            if candidate.is_dir():
                paths.append(candidate)

    added: list[str] = []
    for path in paths:
        value = str(path)
        if os.name == "nt" and hasattr(os, "add_dll_directory"):
            _DLL_HANDLES.append(os.add_dll_directory(value))
        added.append(value)

    if added:
        os.environ["PATH"] = os.pathsep.join(added + [os.environ.get("PATH", "")])
        if platform.system() == "Linux":
            os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(
                added + [os.environ.get("LD_LIBRARY_PATH", "")]
            )
    return added


def probe_media_tool(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    if not path:
        return {"path": None, "version": None, "ok": False}
    try:
        result = run_command([path, "-version"])
        version = next(
            (line.strip() for line in result.stdout.splitlines() if line.strip()),
            None,
        )
        return {"path": path, "version": version, "ok": True}
    except (OSError, subprocess.CalledProcessError) as exc:
        return {
            "path": path,
            "version": None,
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


def probe_import(module_name: str) -> dict[str, Any]:
    try:
        module = importlib.import_module(module_name)
        return {
            "ok": True,
            "version": getattr(module, "__version__", None),
            "error": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "version": None,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


def probe_ctranslate2() -> dict[str, Any]:
    added_paths = configure_nvidia_library_paths()
    imported = probe_import("ctranslate2")
    result: dict[str, Any] = {
        "import_ok": imported["ok"],
        "version": imported["version"],
        "error": imported["error"],
        "cuda_device_count": 0,
        "supported_compute_types": {},
        "library_paths_added": added_paths,
    }
    if not imported["ok"]:
        return result
    try:
        module = importlib.import_module("ctranslate2")
        cpu_types = sorted(module.get_supported_compute_types("cpu"))
        cuda_count = int(module.get_cuda_device_count())
        supported: dict[str, list[str]] = {"cpu": cpu_types}
        if cuda_count > 0:
            supported["cuda"] = sorted(module.get_supported_compute_types("cuda"))
        result.update(
            {
                "cuda_device_count": cuda_count,
                "supported_compute_types": supported,
            }
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return result


def probe_pip_check() -> dict[str, Any]:
    try:
        result = run_command(
            [sys.executable, "-m", "pip", "check"],
            check=False,
        )
    except OSError as exc:
        return {
            "ok": False,
            "output": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
    output = "\n".join(
        item for item in (result.stdout.strip(), result.stderr.strip()) if item
    )
    return {
        "ok": result.returncode == 0,
        "output": output[:1000],
    }


def doctor_report(profile: str = "auto") -> dict[str, Any]:
    report = platform_report()
    key_present = bool(os.environ.get("SILICONFLOW_API_KEY"))
    groups = required_groups(profile, report)
    requirement_status = evaluate_requirements(read_requirements(groups))
    media_tools = {
        "ffmpeg": probe_media_tool("ffmpeg"),
        "ffprobe": probe_media_tool("ffprobe"),
    }
    imports = {
        "requests": probe_import("requests"),
        "faster_whisper": probe_import("faster_whisper"),
    }
    if "macos" in groups:
        imports["mlx_whisper"] = probe_import("mlx_whisper")
    ctranslate2 = probe_ctranslate2()
    imports["ctranslate2"] = {
        "ok": ctranslate2["import_ok"],
        "version": ctranslate2["version"],
        "error": ctranslate2["error"],
    }
    pip_check = probe_pip_check()
    missing: list[str] = []
    incompatible: list[str] = []
    warnings: list[str] = []
    for item in requirement_status:
        if item["satisfied"]:
            continue
        if item["installed"] is None:
            missing.append(item["spec"])
        else:
            incompatible.append(
                f"{item['spec']} (installed {item['installed']})"
            )
    for module_name, status in imports.items():
        if not status["ok"]:
            missing.append(f"import:{module_name}")
    for tool_name, status in media_tools.items():
        if not status["ok"]:
            missing.append(tool_name)
    cuda_ready = True
    if "nvidia" in groups:
        cuda_ready = bool(
            ctranslate2["import_ok"]
            and ctranslate2["cuda_device_count"] > 0
            and ctranslate2["supported_compute_types"].get("cuda")
        )
        if not cuda_ready:
            warnings.append(
                "NVIDIA was detected, but CTranslate2 cannot use CUDA; "
                "runtime selection will fall back to cloud or CPU."
            )
    if profile == "cloud" and not key_present:
        warnings.append(
            "SILICONFLOW_API_KEY is not configured; installation can complete "
            "but cloud transcription is not runtime-ready."
        )
    requirements_ready = all(item["satisfied"] for item in requirement_status)
    imports_ready = all(item["ok"] for item in imports.values())
    media_tools_ready = all(item["ok"] for item in media_tools.values())
    python_ready = sys.version_info >= (3, 9)
    install_ready = bool(
        python_ready
        and requirements_ready
        and imports_ready
        and media_tools_ready
        and pip_check["ok"]
        and cuda_ready
    )
    report.update(
        {
            "profile": profile,
            "asr_home": str(asr_home()),
            "ffmpeg": media_tools["ffmpeg"]["path"],
            "ffprobe": media_tools["ffprobe"]["path"],
            "media_tools": media_tools,
            "packages": {
                name: package_version(name)
                for name in (
                    "faster-whisper",
                    "ctranslate2",
                    "requests",
                    "mlx-whisper",
                    "nvidia-cublas-cu12",
                    "nvidia-cudnn-cu12",
                    "nvidia-cuda-nvrtc-cu12",
                )
            },
            "siliconflow_key_present": key_present,
            "required_groups": groups,
            "requirements": requirement_status,
            "requirements_ready": requirements_ready,
            "imports": imports,
            "imports_ready": imports_ready,
            "ctranslate2": ctranslate2,
            "pip_check": pip_check,
            "python_ready": python_ready,
            "media_tools_ready": media_tools_ready,
            "cuda_ready": cuda_ready,
            "install_ready": install_ready,
            "missing": sorted(set(missing)),
            "incompatible": sorted(set(incompatible)),
            "warnings": warnings,
        }
    )
    report["selection"] = choose_backend(
        report,
        api_key_present=key_present,
        profile=profile,
    )
    report["ready"] = bool(
        install_ready
        and (
            report["selection"]["backend"] != "siliconflow"
            or key_present
        )
    )
    return report


def print_doctor(report: dict[str, Any]) -> None:
    print(f"System: {report['system']} {report['release']} ({report['machine']})")
    print(f"Python: {report['python']} at {report['executable']}")
    print(f"Memory: {report.get('memory_gb') or 'unknown'} GB")
    nvidia = report.get("nvidia")
    if nvidia:
        print(
            f"NVIDIA: {nvidia['name']}, {nvidia['memory_gb']} GB, "
            f"driver {nvidia['driver_version']}"
        )
    else:
        print("NVIDIA: not detected")
    print(f"FFmpeg: {report['ffmpeg'] or 'missing'}")
    print(f"FFprobe: {report['ffprobe'] or 'missing'}")
    print(
        "SiliconFlow key: "
        + ("configured" if report["siliconflow_key_present"] else "not configured")
    )
    selection = report["selection"]
    print(
        "Auto selection: "
        f"{selection['backend']} / {selection['model']} / "
        f"{selection['device']} / {selection['compute_type']}"
    )
    if report.get("missing"):
        print("Missing: " + ", ".join(report["missing"]))
    if report.get("incompatible"):
        print("Incompatible: " + ", ".join(report["incompatible"]))
    for warning in report.get("warnings", []):
        print(f"Warning: {warning}")
    print(
        "Install-ready: "
        + ("yes" if report.get("install_ready") else "no")
    )
    print(f"Ready: {'yes' if report['ready'] else 'no'}")


def require_media_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not command_exists(name)]
    if missing:
        raise AsrError(
            f"Missing required tools: {', '.join(missing)}. Run the setup script."
        )


def probe_media(path: Path) -> dict[str, Any]:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,format_name:stream=index,codec_type,codec_name,"
            "sample_rate,channels,width,height",
            "-of",
            "json",
            str(path),
        ]
    )
    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise AsrError(f"Could not determine media duration: {path}")
    data["source_path"] = str(path.resolve())
    data["duration_seconds"] = duration
    return data


def normalize_audio(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(destination),
    ]
    try:
        run_command(command)
    except subprocess.CalledProcessError as exc:
        raise AsrError(f"FFmpeg audio normalization failed: {exc.stderr.strip()}") from exc


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_match_text(text: str) -> str:
    return "".join(
        char.lower()
        for char in text
        if char.isalnum() or "\u4e00" <= char <= "\u9fff"
    )


def normalize_segment(segment: dict[str, Any], index: int) -> dict[str, Any]:
    words = []
    for word in segment.get("words") or []:
        words.append(
            {
                "start": float(word.get("start") or 0),
                "end": float(word.get("end") or 0),
                "word": str(word.get("word") or word.get("text") or ""),
                "probability": (
                    float(word["probability"])
                    if word.get("probability") is not None
                    else None
                ),
            }
        )
    return {
        "id": index,
        "start": float(segment.get("start") or 0),
        "end": float(segment.get("end") or 0),
        "text": clean_text(str(segment.get("text") or "")),
        "avg_logprob": (
            float(segment["avg_logprob"])
            if segment.get("avg_logprob") is not None
            else None
        ),
        "no_speech_prob": (
            float(segment["no_speech_prob"])
            if segment.get("no_speech_prob") is not None
            else None
        ),
        "alignment_confidence": (
            float(segment["alignment_confidence"])
            if segment.get("alignment_confidence") is not None
            else None
        ),
        "words": words,
    }


def write_bundle(
    raw_dir: Path,
    stem: str,
    metadata: dict[str, Any],
    segments: list[dict[str, Any]],
) -> dict[str, str]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    normalized = [
        normalize_segment(segment, index)
        for index, segment in enumerate(segments, start=1)
        if clean_text(str(segment.get("text") or ""))
    ]
    metadata = dict(metadata)
    metadata["segments"] = normalized

    paths = {
        "json": raw_dir / f"{stem}.json",
        "txt": raw_dir / f"{stem}.txt",
        "srt": raw_dir / f"{stem}.srt",
        "vtt": raw_dir / f"{stem}.vtt",
        "tsv": raw_dir / f"{stem}.tsv",
    }
    json_write(paths["json"], metadata)
    paths["txt"].write_text(
        "\n".join(segment["text"] for segment in normalized) + "\n",
        encoding="utf-8",
    )

    srt_blocks = []
    vtt_blocks = ["WEBVTT", ""]
    tsv_lines = ["start\tend\ttext"]
    for index, segment in enumerate(normalized, start=1):
        start_srt = format_timestamp(segment["start"], ",")
        end_srt = format_timestamp(segment["end"], ",")
        start_vtt = format_timestamp(segment["start"], ".")
        end_vtt = format_timestamp(segment["end"], ".")
        srt_blocks.append(
            f"{index}\n{start_srt} --> {end_srt}\n{segment['text']}"
        )
        vtt_blocks.append(
            f"{start_vtt} --> {end_vtt}\n{segment['text']}\n"
        )
        tsv_lines.append(
            f"{round(segment['start'] * 1000)}\t"
            f"{round(segment['end'] * 1000)}\t{segment['text']}"
        )

    paths["srt"].write_text("\n\n".join(srt_blocks) + "\n", encoding="utf-8")
    paths["vtt"].write_text("\n".join(vtt_blocks), encoding="utf-8")
    paths["tsv"].write_text("\n".join(tsv_lines) + "\n", encoding="utf-8")
    return {key: str(value.resolve()) for key, value in paths.items()}


def transcribe_faster(
    audio_path: Path,
    *,
    model_name: str,
    device: str,
    compute_type: str,
    language: str,
    initial_prompt: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    configure_nvidia_library_paths()
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AsrError("faster-whisper is not installed. Run setup.") from exc

    model_path = Path(model_name).expanduser()
    model_reference = str(model_path.resolve()) if model_path.exists() else model_name
    download_root = asr_home() / "models" / "faster-whisper"
    download_root.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    model = WhisperModel(
        model_reference,
        device=device,
        compute_type=compute_type,
        download_root=str(download_root),
    )
    generator, info = model.transcribe(
        str(audio_path),
        language=language or None,
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
        initial_prompt=initial_prompt,
    )
    segments = []
    for segment in generator:
        words = [
            {
                "start": word.start,
                "end": word.end,
                "word": word.word,
                "probability": word.probability,
            }
            for word in (segment.words or [])
        ]
        segments.append(
            {
                "start": segment.start,
                "end": segment.end,
                "text": segment.text,
                "avg_logprob": segment.avg_logprob,
                "no_speech_prob": segment.no_speech_prob,
                "words": words,
            }
        )
    elapsed = time.perf_counter() - started
    metadata = {
        "backend": "faster-whisper",
        "model": model_reference,
        "device": device,
        "compute_type": compute_type,
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
        "duration_after_vad": info.duration_after_vad,
        "timestamp_source": "native-word",
        "elapsed_seconds": round(elapsed, 3),
    }
    return segments, metadata


def transcribe_mlx(
    audio_path: Path,
    *,
    model_name: str,
    language: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        import mlx_whisper
    except ImportError as exc:
        raise AsrError("mlx-whisper is not installed. Run setup on Apple Silicon.") from exc

    started = time.perf_counter()
    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=model_name,
        language=language or None,
        word_timestamps=True,
    )
    elapsed = time.perf_counter() - started
    segments = []
    for item in result.get("segments") or []:
        segments.append(
            {
                "start": item.get("start", 0),
                "end": item.get("end", 0),
                "text": item.get("text", ""),
                "avg_logprob": item.get("avg_logprob"),
                "no_speech_prob": item.get("no_speech_prob"),
                "words": item.get("words") or [],
            }
        )
    metadata = {
        "backend": "mlx-whisper",
        "model": model_name,
        "device": "metal",
        "compute_type": "mlx",
        "language": result.get("language") or language,
        "timestamp_source": "native-word",
        "elapsed_seconds": round(elapsed, 3),
    }
    return segments, metadata


def split_sentences(text: str) -> list[str]:
    pieces = re.findall(r".+?(?:[。！？!?；;\n]+|$)", text, flags=re.S)
    return [clean_text(piece) for piece in pieces if clean_text(piece)]


def make_cloud_chunks(
    audio_path: Path,
    *,
    duration: float,
    work_dir: Path,
) -> list[dict[str, Any]]:
    max_duration = 3300.0
    max_size = 49 * 1024 * 1024
    if duration <= max_duration and audio_path.stat().st_size <= max_size:
        return [{"path": audio_path, "start": 0.0, "end": duration}]

    chunks: list[dict[str, Any]] = []
    cloud_dir = work_dir / "cloud-parts"
    cloud_dir.mkdir(parents=True, exist_ok=True)
    start = 0.0
    index = 1
    while start < duration:
        end = min(duration, start + 3000.0)
        destination = cloud_dir / f"part-{index:03d}.mp3"
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(start),
                "-to",
                str(end),
                "-i",
                str(audio_path),
                "-codec:a",
                "copy",
                str(destination),
            ]
        )
        if destination.stat().st_size > max_size:
            raise AsrError(
                f"Cloud chunk remains above 50 MB after normalization: {destination}"
            )
        chunks.append({"path": destination, "start": start, "end": end})
        start = end
        index += 1
    return chunks


def siliconflow_request(
    audio_path: Path,
    *,
    model_name: str,
    api_key: str,
    max_attempts: int = 4,
) -> dict[str, Any]:
    try:
        import requests
    except ImportError as exc:
        raise AsrError("requests is not installed. Run setup.") from exc

    retry_statuses = {429, 503, 504}
    last_message = ""
    for attempt in range(1, max_attempts + 1):
        try:
            with audio_path.open("rb") as handle:
                response = requests.post(
                    SILICONFLOW_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={
                        "file": (audio_path.name, handle, "audio/mpeg"),
                        "model": (None, model_name),
                    },
                    timeout=(30, 600),
                )
        except requests.RequestException as exc:
            last_message = f"network error: {type(exc).__name__}"
            if attempt == max_attempts:
                raise AsrError(
                    f"SiliconFlow request failed after {attempt} attempts: {last_message}"
                ) from exc
            time.sleep(min(2 ** (attempt - 1), 8))
            continue

        trace_id = response.headers.get("x-siliconcloud-trace-id")
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError as exc:
                raise AsrError("SiliconFlow returned invalid JSON.") from exc
            text = clean_text(str(payload.get("text") or ""))
            if not text:
                raise AsrError("SiliconFlow returned an empty transcript.")
            return {
                "text": text,
                "status_code": response.status_code,
                "trace_id": trace_id,
                "attempts": attempt,
            }

        try:
            error_payload = response.json()
            message = clean_text(
                str(error_payload.get("message") or error_payload.get("code") or "")
            )
        except (ValueError, AttributeError):
            message = response.text[:200]
        last_message = f"HTTP {response.status_code}: {message or 'request failed'}"
        if response.status_code not in retry_statuses or attempt == max_attempts:
            raise AsrError(
                f"SiliconFlow request failed: {last_message}"
                + (f" (trace {trace_id})" if trace_id else "")
            )
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else min(2 ** (attempt - 1), 8)
        except ValueError:
            delay = min(2 ** (attempt - 1), 8)
        time.sleep(delay)
    raise AsrError(f"SiliconFlow request failed: {last_message}")


def build_character_timeline(
    segments: list[dict[str, Any]],
) -> tuple[str, list[float]]:
    characters: list[str] = []
    times: list[float] = []
    for segment in segments:
        words = segment.get("words") or []
        if words:
            entries = words
        else:
            entries = [
                {
                    "word": segment.get("text", ""),
                    "start": segment.get("start", 0),
                    "end": segment.get("end", 0),
                }
            ]
        for entry in entries:
            normalized = normalize_match_text(str(entry.get("word") or ""))
            if not normalized:
                continue
            start = float(entry.get("start") or 0)
            end = max(start, float(entry.get("end") or start))
            span = max(end - start, 0.001)
            for index, char in enumerate(normalized):
                characters.append(char)
                times.append(start + span * (index / max(len(normalized), 1)))
    return "".join(characters), times


def align_cloud_text(
    cloud_text: str,
    skeleton_segments: list[dict[str, Any]],
    *,
    duration: float,
) -> tuple[list[dict[str, Any]], float]:
    skeleton_text, skeleton_times = build_character_timeline(skeleton_segments)
    cloud_normalized = normalize_match_text(cloud_text)
    if not cloud_normalized:
        return [], 0.0

    matcher = difflib.SequenceMatcher(
        None,
        skeleton_text,
        cloud_normalized,
        autojunk=False,
    )
    mapped: dict[int, float] = {}
    matched = 0
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            skeleton_index = block.a + offset
            cloud_index = block.b + offset
            if skeleton_index < len(skeleton_times):
                mapped[cloud_index] = skeleton_times[skeleton_index]
                matched += 1

    sentences = split_sentences(cloud_text) or [cloud_text]
    segments: list[dict[str, Any]] = []
    cursor = 0
    previous_end = 0.0
    total_chars = max(len(cloud_normalized), 1)
    for sentence in sentences:
        normalized = normalize_match_text(sentence)
        start_index = cursor
        end_index = min(total_chars, cursor + len(normalized))
        sentence_times = [
            mapped[index]
            for index in range(start_index, end_index)
            if index in mapped
        ]
        if sentence_times:
            start = min(sentence_times)
            end = max(sentence_times)
            confidence = len(sentence_times) / max(len(normalized), 1)
        else:
            start = duration * (start_index / total_chars)
            end = duration * (end_index / total_chars)
            confidence = 0.0
        start = max(previous_end, start)
        end = max(start + 0.25, min(duration, end + 0.25))
        previous_end = end
        segments.append(
            {
                "start": start,
                "end": end,
                "text": sentence,
                "alignment_confidence": round(confidence, 4),
                "words": [],
            }
        )
        cursor = end_index

    if segments:
        segments[-1]["end"] = max(segments[-1]["end"], min(duration, previous_end))
    return segments, matched / total_chars


def transcribe_siliconflow(
    audio_path: Path,
    *,
    model_name: str,
    language: str,
    duration: float,
    raw_dir: Path,
    timestamp_model: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    del language  # SiliconFlow's documented transcription request has no language field.
    api_key = os.environ.get("SILICONFLOW_API_KEY")
    if not api_key:
        raise AsrError("SILICONFLOW_API_KEY is not configured.")

    started = time.perf_counter()
    chunks = make_cloud_chunks(audio_path, duration=duration, work_dir=raw_dir.parent)
    responses = []
    cloud_texts = []
    for chunk in chunks:
        response = siliconflow_request(
            chunk["path"],
            model_name=model_name,
            api_key=api_key,
        )
        responses.append(
            {
                "start": chunk["start"],
                "end": chunk["end"],
                "text": response["text"],
                "trace_id": response["trace_id"],
                "attempts": response["attempts"],
            }
        )
        cloud_texts.append(response["text"])
    cloud_text = "\n".join(cloud_texts)

    skeleton_segments: list[dict[str, Any]] = []
    skeleton_metadata: dict[str, Any] | None = None
    alignment_error: str | None = None
    try:
        skeleton_segments, skeleton_metadata = transcribe_faster(
            audio_path,
            model_name=timestamp_model,
            device="cpu",
            compute_type="int8",
            language="zh",
        )
        write_bundle(
            raw_dir,
            "timestamp-skeleton",
            skeleton_metadata,
            skeleton_segments,
        )
    except Exception as exc:  # Alignment is helpful but cloud transcription must survive it.
        alignment_error = f"{type(exc).__name__}: {str(exc)[:200]}"

    if skeleton_segments:
        segments, alignment_ratio = align_cloud_text(
            cloud_text,
            skeleton_segments,
            duration=duration,
        )
        timestamp_source = "aligned-local-whisper"
    else:
        sentences = split_sentences(cloud_text) or [cloud_text]
        total = max(sum(len(normalize_match_text(item)) for item in sentences), 1)
        cursor = 0
        segments = []
        for sentence in sentences:
            length = len(normalize_match_text(sentence))
            start = duration * (cursor / total)
            cursor += length
            end = duration * (cursor / total)
            segments.append(
                {
                    "start": start,
                    "end": max(start + 0.25, end),
                    "text": sentence,
                    "alignment_confidence": 0.0,
                    "words": [],
                }
            )
        alignment_ratio = 0.0
        timestamp_source = "proportional-estimate"

    elapsed = time.perf_counter() - started
    metadata = {
        "backend": "siliconflow",
        "model": model_name,
        "device": "cloud",
        "compute_type": "provider",
        "timestamp_source": timestamp_source,
        "timestamp_model": timestamp_model,
        "alignment_ratio": round(alignment_ratio, 4),
        "alignment_error": alignment_error,
        "duration": duration,
        "elapsed_seconds": round(elapsed, 3),
        "provider_responses": responses,
    }
    return segments, metadata


def primary_stem(backend: str, model_name: str) -> str:
    short_model = model_name.replace("\\", "/").rstrip("/").split("/")[-1]
    value = f"{backend}-{short_model}".lower()
    return re.sub(r"[^a-z0-9._-]+", "-", value).strip("-")


def select_explicit_backend(
    backend: str,
    report: dict[str, Any],
    model: str | None,
    siliconflow_model: str,
) -> dict[str, str]:
    if backend == "auto":
        selection = choose_backend(
            report,
            api_key_present=bool(os.environ.get("SILICONFLOW_API_KEY")),
        )
        if model:
            selection["model"] = model
        return selection
    if backend == "faster-whisper":
        nvidia = report.get("nvidia")
        device = "cuda" if nvidia and nvidia.get("memory_gb", 0) >= 4 else "cpu"
        compute_type = (
            "float16"
            if device == "cuda" and nvidia["memory_gb"] >= 8
            else "int8_float16"
            if device == "cuda"
            else "int8"
        )
        return {
            "backend": backend,
            "model": model or ("large-v3" if device == "cuda" else "small"),
            "device": device,
            "compute_type": compute_type,
        }
    if backend == "mlx-whisper":
        return {
            "backend": backend,
            "model": model or "mlx-community/whisper-large-v3-mlx-4bit",
            "device": "metal",
            "compute_type": "mlx",
        }
    if backend == "siliconflow":
        return {
            "backend": backend,
            "model": SILICONFLOW_MODELS[siliconflow_model],
            "device": "cloud",
            "compute_type": "provider",
        }
    raise AsrError(f"Unsupported backend: {backend}")


def write_run_manifest(
    output_dir: Path,
    run_data: dict[str, Any],
    raw_paths: dict[str, str],
) -> None:
    del raw_paths
    hashes = {}
    for item in (output_dir / "raw").rglob("*"):
        if item.is_file():
            hashes[str(item.relative_to(output_dir))] = hash_file(item)
    run_data["raw_hashes"] = hashes
    json_write(output_dir / "run.json", run_data)


def command_transcribe(args: argparse.Namespace) -> int:
    source = Path(args.input).expanduser()
    if not source.is_file():
        raise AsrError(f"Input file does not exist: {source}")
    output_dir = Path(args.output_dir).expanduser()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise AsrError(
            f"Output directory is not empty: {output_dir}. "
            "Use a new directory to preserve raw evidence."
        )

    require_media_tools()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = output_dir / "source"
    raw_dir = output_dir / "raw"
    source_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    media = probe_media(source)
    audio_path = source_dir / "audio_16k.mp3"
    normalize_audio(source, audio_path)
    media["normalized_audio"] = str(audio_path.resolve())
    json_write(source_dir / "media.json", media)

    report = doctor_report()
    selection = select_explicit_backend(
        args.backend,
        report,
        args.model,
        args.siliconflow_model,
    )
    backend = selection["backend"]
    eprint(
        f"Selected {backend} / {selection['model']} / "
        f"{selection['device']} / {selection['compute_type']}"
    )

    if backend == "faster-whisper":
        segments, metadata = transcribe_faster(
            audio_path,
            model_name=selection["model"],
            device=selection["device"],
            compute_type=selection["compute_type"],
            language=args.language,
            initial_prompt=args.initial_prompt,
        )
    elif backend == "mlx-whisper":
        segments, metadata = transcribe_mlx(
            audio_path,
            model_name=selection["model"],
            language=args.language,
        )
    else:
        segments, metadata = transcribe_siliconflow(
            audio_path,
            model_name=selection["model"],
            language=args.language,
            duration=media["duration_seconds"],
            raw_dir=raw_dir,
            timestamp_model=args.timestamp_model,
        )

    if not segments:
        raise AsrError("The selected backend returned no transcript segments.")
    stem = primary_stem(backend, selection["model"])
    raw_paths = write_bundle(raw_dir, stem, metadata, segments)
    run_data = {
        "schema_version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "input": str(source.resolve()),
        "output_dir": str(output_dir.resolve()),
        "normalized_audio": str(audio_path.resolve()),
        "duration_seconds": media["duration_seconds"],
        "language": args.language,
        "primary": {
            "backend": backend,
            "model": selection["model"],
            "device": selection["device"],
            "compute_type": selection["compute_type"],
            "timestamp_source": metadata.get("timestamp_source"),
            "json": str(Path(raw_paths["json"]).relative_to(output_dir)),
            "srt": str(Path(raw_paths["srt"]).relative_to(output_dir)),
            "txt": str(Path(raw_paths["txt"]).relative_to(output_dir)),
        },
        "fallbacks": [],
        "audit_requested": bool(args.audit),
    }
    write_run_manifest(output_dir, run_data, raw_paths)

    if args.audit:
        audit_args = argparse.Namespace(run_dir=str(output_dir), secondary="auto")
        command_prepare_audit(audit_args)

    print(
        json.dumps(
            {
                "output_dir": str(output_dir.resolve()),
                "backend": backend,
                "model": selection["model"],
                "segment_count": len(segments),
                "timestamp_source": metadata.get("timestamp_source"),
                "audit": bool(args.audit),
            },
            ensure_ascii=False,
        )
    )
    return 0


def repeated_text(text: str) -> bool:
    normalized = normalize_match_text(text)
    if len(normalized) < 12:
        return False
    for size in range(2, min(10, len(normalized) // 3 + 1)):
        for index in range(0, len(normalized) - size * 3 + 1):
            token = normalized[index : index + size]
            if token * 3 in normalized:
                return True
    return False


def segment_reasons(segment: dict[str, Any]) -> list[str]:
    reasons = []
    avg_logprob = segment.get("avg_logprob")
    no_speech_prob = segment.get("no_speech_prob")
    alignment_confidence = segment.get("alignment_confidence")
    words = segment.get("words") or []
    probabilities = [
        word["probability"]
        for word in words
        if word.get("probability") is not None
    ]
    if avg_logprob is not None and avg_logprob < -0.8:
        reasons.append(f"low avg_logprob {avg_logprob:.2f}")
    if no_speech_prob is not None and no_speech_prob > 0.35:
        reasons.append(f"high no_speech_prob {no_speech_prob:.2f}")
    if probabilities:
        low_ratio = sum(value < 0.5 for value in probabilities) / len(probabilities)
        if low_ratio >= 0.25:
            reasons.append(f"{low_ratio:.0%} low-probability words")
    if alignment_confidence is not None and alignment_confidence < 0.55:
        reasons.append(f"low alignment {alignment_confidence:.2f}")
    if repeated_text(segment.get("text", "")):
        reasons.append("repeated text")
    if not clean_text(segment.get("text", "")):
        reasons.append("empty text")
    return reasons


def overlapping_text(
    target: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    pieces = []
    target_start = float(target["start"])
    target_end = float(target["end"])
    for candidate in candidates:
        overlap = min(target_end, float(candidate["end"])) - max(
            target_start, float(candidate["start"])
        )
        if overlap > 0:
            pieces.append(candidate["text"])
    return clean_text(" ".join(pieces))


def collect_full_secondary(
    run_dir: Path,
    run_data: dict[str, Any],
    primary_segments: list[dict[str, Any]],
    *,
    requested: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    api_key = os.environ.get("SILICONFLOW_API_KEY")
    if not api_key:
        return [], None

    primary = run_data["primary"]
    if requested in SILICONFLOW_MODELS:
        model_key = requested
    elif primary["backend"] == "siliconflow":
        model_key = "telespeech"
    else:
        model_key = "sensevoice"
    model_name = SILICONFLOW_MODELS[model_key]
    audio_path = Path(run_data["normalized_audio"])
    duration = float(run_data["duration_seconds"])
    chunks = make_cloud_chunks(
        audio_path,
        duration=duration,
        work_dir=run_dir / "audit-secondary-work",
    )
    texts = []
    responses = []
    for chunk in chunks:
        response = siliconflow_request(
            chunk["path"],
            model_name=model_name,
            api_key=api_key,
        )
        texts.append(response["text"])
        responses.append(
            {
                "start": chunk["start"],
                "end": chunk["end"],
                "trace_id": response["trace_id"],
                "attempts": response["attempts"],
                "text": response["text"],
            }
        )

    secondary_segments, ratio = align_cloud_text(
        "\n".join(texts),
        primary_segments,
        duration=duration,
    )
    metadata = {
        "backend": "siliconflow",
        "model": model_name,
        "device": "cloud",
        "compute_type": "provider",
        "timestamp_source": "aligned-primary",
        "alignment_ratio": round(ratio, 4),
        "duration": duration,
        "provider_responses": responses,
        "purpose": "full-pass disagreement detection",
    }
    paths = write_bundle(
        run_dir / "raw",
        f"audit-secondary-{model_key}",
        metadata,
        secondary_segments,
    )
    return secondary_segments, {
        "model_key": model_key,
        "model": model_name,
        "paths": paths,
        "alignment_ratio": ratio,
    }


def merge_suspects(
    suspects: list[dict[str, Any]],
    *,
    duration: float,
    padding: float = 2.0,
) -> list[dict[str, Any]]:
    intervals = []
    for item in suspects:
        intervals.append(
            {
                "start": max(0.0, float(item["start"]) - padding),
                "end": min(duration, float(item["end"]) + padding),
                "segment_ids": [item["id"]],
                "reasons": list(item["reasons"]),
                "primary_texts": [item["text"]],
            }
        )
    intervals.sort(key=lambda item: item["start"])
    merged: list[dict[str, Any]] = []
    for item in intervals:
        if merged and item["start"] <= merged[-1]["end"] + 0.5:
            merged[-1]["end"] = max(merged[-1]["end"], item["end"])
            merged[-1]["segment_ids"].extend(item["segment_ids"])
            merged[-1]["reasons"].extend(item["reasons"])
            merged[-1]["primary_texts"].extend(item["primary_texts"])
        else:
            merged.append(item)
    return merged


def clip_audio(source: Path, destination: Path, start: float, end: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(start),
            "-to",
            str(end),
            "-i",
            str(source),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "96k",
            str(destination),
        ]
    )


def transcribe_secondary_clip(
    clip: Path,
    *,
    primary_backend: str,
    secondary: str,
) -> dict[str, Any]:
    api_key = os.environ.get("SILICONFLOW_API_KEY")
    selected = secondary
    if selected == "auto":
        if api_key:
            selected = "telespeech" if primary_backend == "siliconflow" else "sensevoice"
        else:
            selected = "local-small"

    if selected in SILICONFLOW_MODELS:
        if not api_key:
            return {
                "backend": selected,
                "text": "",
                "error": "SILICONFLOW_API_KEY is not configured",
            }
        response = siliconflow_request(
            clip,
            model_name=SILICONFLOW_MODELS[selected],
            api_key=api_key,
        )
        return {
            "backend": "siliconflow",
            "model": SILICONFLOW_MODELS[selected],
            "text": response["text"],
            "trace_id": response["trace_id"],
        }

    try:
        segments, metadata = transcribe_faster(
            clip,
            model_name="small",
            device="cpu",
            compute_type="int8",
            language="zh",
        )
        return {
            "backend": "faster-whisper",
            "model": "small",
            "text": " ".join(item["text"].strip() for item in segments),
            "metadata": metadata,
        }
    except Exception as exc:
        return {
            "backend": "faster-whisper",
            "model": "small",
            "text": "",
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


def conservative_outputs(
    run_dir: Path,
    segments: list[dict[str, Any]],
    suspect_ids: set[int],
) -> None:
    txt_lines = []
    srt_blocks = []
    for index, segment in enumerate(segments, start=1):
        text = (
            f"[听不清 {short_timestamp(segment['start'])}]"
            if segment["id"] in suspect_ids
            else segment["text"]
        )
        txt_lines.append(text)
        srt_blocks.append(
            f"{index}\n"
            f"{format_timestamp(segment['start'])} --> "
            f"{format_timestamp(segment['end'])}\n{text}"
        )
    (run_dir / "transcript.corrected.txt").write_text(
        "\n".join(txt_lines) + "\n",
        encoding="utf-8",
    )
    (run_dir / "transcript.corrected.srt").write_text(
        "\n\n".join(srt_blocks) + "\n",
        encoding="utf-8",
    )


def command_prepare_audit(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser()
    run_path = run_dir / "run.json"
    if not run_path.is_file():
        raise AsrError(f"run.json not found: {run_dir}")
    audit_path = run_dir / "transcript.audit.md"
    if audit_path.exists():
        raise AsrError(
            f"Audit already exists: {audit_path}. Use a new transcription run."
        )

    run_data = json_read(run_path)
    primary_path = run_dir / run_data["primary"]["json"]
    primary_data = json_read(primary_path)
    segments = primary_data.get("segments") or []
    secondary_segments, secondary_full = collect_full_secondary(
        run_dir,
        run_data,
        segments,
        requested=args.secondary,
    )
    suspects = []
    for segment in segments:
        reasons = segment_reasons(segment)
        if secondary_segments:
            candidate = overlapping_text(segment, secondary_segments)
            primary_normalized = normalize_match_text(segment.get("text", ""))
            candidate_normalized = normalize_match_text(candidate)
            if primary_normalized and candidate_normalized:
                agreement = difflib.SequenceMatcher(
                    None,
                    primary_normalized,
                    candidate_normalized,
                    autojunk=False,
                ).ratio()
                if agreement < 0.92:
                    reasons.append(f"model disagreement {agreement:.2f}")
        if reasons:
            suspects.append({**segment, "reasons": reasons})

    intervals = merge_suspects(
        suspects,
        duration=float(run_data["duration_seconds"]),
    )
    audio_path = Path(run_data["normalized_audio"])
    clip_root = run_dir / "audit-clips"
    audit_rows = []
    for index, interval in enumerate(intervals, start=1):
        clip_dir = clip_root / f"{index:03d}"
        clip_path = clip_dir / "clip.mp3"
        clip_audio(audio_path, clip_path, interval["start"], interval["end"])
        clip_secondary = args.secondary
        if clip_secondary == "auto" and (
            secondary_full and secondary_full["model_key"] == "sensevoice"
        ):
            clip_secondary = "telespeech"
        result = transcribe_secondary_clip(
            clip_path,
            primary_backend=run_data["primary"]["backend"],
            secondary=clip_secondary,
        )
        json_write(clip_dir / "secondary.json", result)
        (clip_dir / "secondary.txt").write_text(
            clean_text(result.get("text", "")) + "\n",
            encoding="utf-8",
        )
        audit_rows.append(
            {
                "start": interval["start"],
                "end": interval["end"],
                "segment_ids": interval["segment_ids"],
                "reasons": sorted(set(interval["reasons"])),
                "primary": " ".join(interval["primary_texts"]),
                "secondary": clean_text(result.get("text", "")),
                "secondary_backend": result.get("model") or result.get("backend"),
                "clip": str(clip_path.relative_to(run_dir)),
                "error": result.get("error"),
            }
        )

    suspect_ids = {item["id"] for item in suspects}
    conservative_outputs(run_dir, segments, suspect_ids)

    lines = [
        "# Transcript Audit",
        "",
        f"- Primary: `{run_data['primary']['backend']} / {run_data['primary']['model']}`",
        f"- Timestamp source: `{run_data['primary'].get('timestamp_source')}`",
        f"- Disputed intervals: {len(audit_rows)}",
        "- Raw files are immutable. Edit only corrected outputs and the decision column.",
        "",
        "| Time | Reason | Primary | Secondary | Decision |",
        "|---|---|---|---|---|",
    ]
    for row in audit_rows:
        start = short_timestamp(row["start"])
        end = short_timestamp(row["end"])
        reason = "; ".join(row["reasons"]).replace("|", "\\|")
        primary = row["primary"].replace("|", "\\|").replace("\n", " ")
        secondary = (row["secondary"] or row.get("error") or "").replace(
            "|", "\\|"
        ).replace("\n", " ")
        marker = f"[听不清 {start}]"
        lines.append(
            f"| {start}–{end} | {reason} | {primary} | "
            f"{secondary} | unresolved: `{marker}` |"
        )
    if not audit_rows:
        lines.append("| — | No automatic concerns | — | — | confirmed |")
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    run_data["audit"] = {
        "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "secondary": args.secondary,
        "suspect_segment_count": len(suspects),
        "interval_count": len(audit_rows),
        "unresolved_count": len(audit_rows),
        "full_secondary": (
            {
                "model": secondary_full["model"],
                "alignment_ratio": round(secondary_full["alignment_ratio"], 4),
                "json": str(
                    Path(secondary_full["paths"]["json"]).relative_to(run_dir)
                ),
            }
            if secondary_full
            else None
        ),
    }
    if secondary_full:
        for path in secondary_full["paths"].values():
            item = Path(path)
            run_data.setdefault("raw_hashes", {})[
                str(item.relative_to(run_dir))
            ] = hash_file(item)
    json_write(run_path, run_data)
    print(
        json.dumps(
            {
                "run_dir": str(run_dir.resolve()),
                "suspect_segments": len(suspects),
                "audit_intervals": len(audit_rows),
                "unresolved": len(audit_rows),
            },
            ensure_ascii=False,
        )
    )
    return 0


SRT_TIMESTAMP = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> "
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)


def parse_srt_time(groups: Iterable[str]) -> tuple[float, float]:
    values = [int(value) for value in groups]
    start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
    end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
    return start, end


def validate_srt(path: Path, duration: float) -> list[str]:
    errors = []
    blocks = [
        block
        for block in re.split(r"\r?\n\r?\n", path.read_text(encoding="utf-8"))
        if block.strip()
    ]
    previous_end = 0.0
    for expected, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if len(lines) < 3:
            errors.append(f"SRT cue {expected} has fewer than three lines")
            continue
        try:
            actual = int(lines[0])
        except ValueError:
            errors.append(f"SRT cue {expected} has invalid number")
            continue
        if actual != expected:
            errors.append(f"SRT cue expected {expected}, found {actual}")
        match = SRT_TIMESTAMP.match(lines[1])
        if not match:
            errors.append(f"SRT cue {expected} has invalid timestamp")
            continue
        start, end = parse_srt_time(match.groups())
        if end <= start:
            errors.append(f"SRT cue {expected} ends before it starts")
        if start + 0.05 < previous_end:
            errors.append(f"SRT cue {expected} overlaps or moves backwards")
        if end > duration + 2:
            errors.append(f"SRT cue {expected} exceeds media duration")
        previous_end = end
    if not blocks:
        errors.append("SRT contains no cues")
    return errors


def command_validate(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser()
    run_path = run_dir / "run.json"
    errors: list[str] = []
    warnings: list[str] = []
    if not run_path.is_file():
        raise AsrError(f"run.json not found: {run_dir}")
    run_data = json_read(run_path)

    required = [
        run_dir / run_data["primary"]["json"],
        run_dir / run_data["primary"]["srt"],
        run_dir / run_data["primary"]["txt"],
        run_dir / "source" / "media.json",
    ]
    if run_data.get("audit_requested") or run_data.get("audit"):
        required.extend(
            [
                run_dir / "transcript.corrected.txt",
                run_dir / "transcript.corrected.srt",
                run_dir / "transcript.audit.md",
            ]
        )
    for path in required:
        if not path.is_file():
            errors.append(f"Missing required file: {path.relative_to(run_dir)}")

    for relative, expected_hash in (run_data.get("raw_hashes") or {}).items():
        path = run_dir / relative
        if not path.is_file():
            errors.append(f"Raw evidence missing: {relative}")
        elif hash_file(path) != expected_hash:
            errors.append(f"Raw evidence changed: {relative}")

    srt_path = (
        run_dir / "transcript.corrected.srt"
        if (run_dir / "transcript.corrected.srt").is_file()
        else run_dir / run_data["primary"]["srt"]
    )
    if srt_path.is_file():
        errors.extend(validate_srt(srt_path, float(run_data["duration_seconds"])))

    for path in run_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"Not valid UTF-8: {path.relative_to(run_dir)}")
            continue
        if "\ufffd" in text:
            errors.append(f"Replacement character found: {path.relative_to(run_dir)}")
        if SECRET_PATTERN.search(text):
            errors.append(f"Possible API key leaked: {path.relative_to(run_dir)}")

    timestamp_source = run_data["primary"].get("timestamp_source")
    if timestamp_source and timestamp_source != "native-word":
        warnings.append(f"Timestamps are {timestamp_source}, not native word timestamps")
    unresolved = int((run_data.get("audit") or {}).get("unresolved_count") or 0)
    if unresolved:
        warnings.append(f"{unresolved} audit intervals remain unresolved")

    result = {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "run_dir": str(run_dir.resolve()),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local/cloud transcription with evidence-preserving audit."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Inspect the current environment.")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.add_argument(
        "--profile",
        choices=("auto", "local", "cloud"),
        default="auto",
    )
    doctor.add_argument(
        "--install-check",
        action="store_true",
        help="Exit according to installation readiness instead of runtime readiness.",
    )

    probe_index = subparsers.add_parser(
        "probe-index",
        help="Select the fastest reachable configured Python package index.",
    )
    probe_index.add_argument("--json", action="store_true", dest="as_json")
    probe_index.add_argument("--timeout", type=float, default=3.0)

    transcribe = subparsers.add_parser("transcribe", help="Transcribe a local file.")
    transcribe.add_argument("input")
    transcribe.add_argument("--output-dir", required=True)
    transcribe.add_argument(
        "--backend",
        choices=("auto", "faster-whisper", "mlx-whisper", "siliconflow"),
        default="auto",
    )
    transcribe.add_argument("--model")
    transcribe.add_argument("--language", default="zh")
    transcribe.add_argument("--initial-prompt")
    transcribe.add_argument(
        "--siliconflow-model",
        choices=tuple(SILICONFLOW_MODELS),
        default="sensevoice",
    )
    transcribe.add_argument("--timestamp-model", default="tiny")
    transcribe.add_argument("--audit", action="store_true")

    audit = subparsers.add_parser(
        "prepare-audit",
        help="Create disputed clips and conservative corrected outputs.",
    )
    audit.add_argument("run_dir")
    audit.add_argument(
        "--secondary",
        choices=("auto", "sensevoice", "telespeech", "local-small"),
        default="auto",
    )

    validate = subparsers.add_parser("validate", help="Validate a transcription run.")
    validate.add_argument("run_dir")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "doctor":
        report = doctor_report(args.profile)
        if args.as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_doctor(report)
        readiness_field = "install_ready" if args.install_check else "ready"
        return 0 if report[readiness_field] else 1
    if args.command == "probe-index":
        selected, results = select_fastest_index(timeout=args.timeout)
        if args.as_json:
            print(
                json.dumps(
                    {"selected": selected, "results": results},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(selected["url"])
        return 0
    if args.command == "transcribe":
        return command_transcribe(args)
    if args.command == "prepare-audit":
        return command_prepare_audit(args)
    if args.command == "validate":
        return command_validate(args)
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AsrError as exc:
        eprint(f"error: {exc}")
        raise SystemExit(1)
