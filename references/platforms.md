# Platform Installation Notes

## Shared Layout

- Runtime: `${ASR_HOME}/venv`
- Models: `${ASR_HOME}/models`
- Default `ASR_HOME`: `~/.video-transcription-audit`
- Override the location by setting `ASR_HOME` before running setup or transcription.
- Advanced existing environments can set `ASR_PYTHON` to a specific Python 3.9+ executable.
- Setup validates `ASR_PYTHON` first, then `${ASR_HOME}/venv`; it does not scan or modify arbitrary system or Conda environments.
- An install-ready environment is reused without pip or package-index access.

## Windows

- Bootstrap with PowerShell 7 when available.
- Use `winget` for Python and FFmpeg when missing.
- NVIDIA runtime libraries are installed in the virtual environment.
- `asr_cli.py` adds the package DLL directories before importing CTranslate2.

## Linux

- Supported package managers: `apt-get`, `dnf`, and `pacman`.
- NVIDIA pip library directories are prepended to `LD_LIBRARY_PATH` in the CLI process.
- The setup script does not modify shell profile files.

## macOS

- Use Homebrew for Python and FFmpeg when missing.
- Apple Silicon installs `mlx-whisper`.
- Intel Macs use the CPU `faster-whisper` fallback.

## CUDA Compatibility

The tested NVIDIA dependency family is CUDA 12 with cuDNN 9. The doctor imports CTranslate2 after adding the packaged NVIDIA library directories, checks `get_cuda_device_count()`, and reads supported CPU/CUDA compute types. Do not silently downgrade CTranslate2 to accommodate an older system CUDA installation. If the probe fails or disagrees with `nvidia-smi`, report the failure and use SiliconFlow or CPU instead of claiming GPU acceleration.

## Package Indexes

- Official PyPI is the default.
- Set `-IndexUrl` or `--index-url` only for an index the user explicitly trusts.
- `-ProbeMirrors` or `--probe-mirrors` tests the bundled HTTPS indexes concurrently and selects the fastest reachable result.
- Mirror probing happens only after preflight determines that installation is required.
- The setup scripts never add pip `--trusted-host`.

## Troubleshooting Order

1. Run `doctor --profile auto --install-check --json`.
2. Confirm `ffmpeg` and `ffprobe`.
3. Inspect `requirements`, `imports`, `pip_check`, and `install_ready`.
4. Confirm `nvidia-smi`, available VRAM, and the CTranslate2 CUDA probe.
5. Use `--force` only when versions are satisfied but imports remain broken.
6. Run a short local test before downloading or processing a long video.
7. If cloud calls fail, inspect the HTTP status and trace ID without printing the key.
