# video-transcription-audit

一个面向 Codex/Agent 的跨平台音视频转录 Skill。它不仅生成字幕，还保留原始模型结果、时间戳、置信度、文件哈希和复核记录，适合中文视频转录、字幕生成、多模型校对与争议片段核验。

## 核心能力

- 支持 Windows、macOS 和 Linux。
- 支持本地 `faster-whisper`、Apple Silicon `mlx-whisper`。
- 支持硅基流动 `FunAudioLLM/SenseVoiceSmall` 和 `TeleAI/TeleSpeechASR`。
- 自动检测系统、CPU、内存、NVIDIA GPU/显存、Python 和 FFmpeg。
- 安装前验证依赖版本、模块导入、`pip check` 和 CTranslate2 CUDA 能力。
- 已有兼容环境直接复用，不调用 pip，也不访问软件包索引。
- 自动选择本地 GPU、Apple MLX、硅基流动或本地 CPU。
- 输出 TXT、SRT、VTT、TSV、JSON 及媒体元数据。
- 使用第二模型定位低置信度、重复、异常空白和模型分歧区间。
- 自动切出争议音频，无法确认时标记为 `[听不清 HH:MM:SS]`。
- 保留 `raw/` 原始证据并记录哈希，校订不会覆盖原始转录。
- 校验字幕连续性、时长覆盖、UTF-8、原始文件哈希和密钥泄漏。

## 环境必备

- [硅基流动 SiliconFlow](https://cloud.siliconflow.cn/i/DFpRRhZo)(实名获得 **¥16** 元代金券，有效期 **180** 天)
- [TikHub](https://user.tikhub.io/register?ref=hHPxjjq2):在 TikTok、抖音、Instagram、X 等平台直接采集数据

## 自动后端选择

使用 `--backend auto` 时按以下顺序选择：

1. Apple Silicon：`mlx-whisper`。
2. Windows/Linux NVIDIA GPU（至少 4 GB 显存）：`faster-whisper`。
3. 已配置 `SILICONFLOW_API_KEY`：硅基流动 `SenseVoiceSmall`。
4. 其他情况：CPU 上运行 `faster-whisper small/int8`。

NVIDIA GPU 的默认档位：

| 显存 | 模型 | 计算类型 |
| --- | --- | --- |
| 8 GB 及以上 | `large-v3` | `float16` |
| 4 GB 到 8 GB | `large-v3` | `int8_float16` |

GPU 选择不仅依据 `nvidia-smi`。CTranslate2 还必须能识别 CUDA 设备并报告对应计算类型；如果驱动检测和实际运行库能力不一致，将降级到硅基流动或 CPU。

模型权重按需下载，不包含在仓库中。运行环境默认安装到 `~/.video-transcription-audit/`，也可以通过 `ASR_HOME` 指定其他位置。

## 安装为 Skill

### Windows

```powershell
git clone https://github.com/dongyubin/video-transcription-audit-skill.git "$HOME\.agents\skills\video-transcription-audit"
Set-Location "$HOME\.agents\skills\video-transcription-audit"
pwsh -File scripts/setup.ps1 -Profile auto
```

安装前只查看将执行的操作：

```powershell
pwsh -File scripts/setup.ps1 -Profile auto -DryRun
```

### macOS / Linux

```bash
git clone https://github.com/dongyubin/video-transcription-audit-skill.git \
  ~/.agents/skills/video-transcription-audit
cd ~/.agents/skills/video-transcription-audit
bash scripts/setup.sh --profile auto
```

安装前只查看将执行的操作：

```bash
bash scripts/setup.sh --profile auto --dry-run
```

安装脚本支持 `auto`、`local`、`cloud` 三种配置，并可重复执行。`auto` 会根据当前硬件选择依赖，`local` 禁止云端回退，`cloud` 固定使用硅基流动。

安装器优先验证显式设置的 `ASR_PYTHON`，其次验证 `ASR_HOME/venv`。环境满足要求时直接退出；缺少单个依赖时只修复对应 requirement。损坏或版本过低的已有环境不会被自动删除。

安装选项：

| Windows | macOS/Linux | 作用 |
| --- | --- | --- |
| `-DryRun` | `--dry-run` | 只执行预检并展示必要操作 |
| `-Force` | `--force` | 强制重新解析并修复依赖，不删除环境 |
| `-IndexUrl URL` | `--index-url URL` | 使用用户明确指定的软件包索引 |
| `-ProbeMirrors` | `--probe-mirrors` | 仅在需要安装时并发测速 HTTPS 镜像 |

默认使用官方 PyPI，不会自动添加 pip `--trusted-host`。`-IndexUrl` 与 `-ProbeMirrors` 不能同时使用。

## 配置硅基流动

API 密钥只从环境变量读取，不要写入仓库、脚本或命令参数。

PowerShell 当前会话：

```powershell
$env:SILICONFLOW_API_KEY = "你的 API 密钥"
```

Bash 当前会话：

```bash
export SILICONFLOW_API_KEY="你的 API 密钥"
```

转录报告和 `run.json` 不会保存密钥。`validate` 还会扫描疑似泄漏的密钥格式。

## 使用方法

### 1. 环境诊断

Windows：

```powershell
pwsh -File scripts/asr.ps1 doctor --profile auto --install-check
pwsh -File scripts/asr.ps1 doctor --profile auto --install-check --json
```

macOS / Linux：

```bash
bash scripts/asr.sh doctor --profile auto --install-check
bash scripts/asr.sh doctor --profile auto --install-check --json
```

`doctor` 会报告 requirements、实际导入结果、FFmpeg/FFprobe 版本、`pip check`、CUDA 设备数、支持的计算类型、缺失项和 `install_ready`。省略 `--install-check` 时，退出码表示所选后端是否已经具备运行条件；例如 `cloud` profile 没有密钥时，安装可以完成但运行检查会失败。

### 2. 转录并生成审计结果

Windows：

```powershell
pwsh -File scripts/asr.ps1 transcribe "D:\video\示例.mp4" `
  --backend auto `
  --language zh `
  --output-dir "D:\transcripts\示例" `
  --audit
```

macOS / Linux：

```bash
bash scripts/asr.sh transcribe "/data/video/示例.mp4" \
  --backend auto \
  --language zh \
  --output-dir "/data/transcripts/示例" \
  --audit
```

输出目录必须是一个尚不存在的新目录，避免误覆盖原始证据。不需要复核时可省略 `--audit`。

指定硅基流动模型：

```powershell
pwsh -File scripts/asr.ps1 transcribe "D:\video\示例.mp4" `
  --backend siliconflow `
  --siliconflow-model sensevoice `
  --language zh `
  --output-dir "D:\transcripts\示例-sensevoice"
```

`--siliconflow-model` 可选 `sensevoice` 或 `telespeech`。云端模型只返回纯文本，本工具会用本地轻量 Whisper 生成时间轴并对齐文本，元数据会明确标记为 `aligned`，不会冒充云端原生词级时间戳。

### 3. 为已有转录准备复核

```powershell
pwsh -File scripts/asr.ps1 prepare-audit "D:\transcripts\示例" --secondary auto
```

```bash
bash scripts/asr.sh prepare-audit "/data/transcripts/示例" --secondary auto
```

`--secondary` 可选 `auto`、`sensevoice`、`telespeech` 或 `local-small`。

### 4. 验证结果

```powershell
pwsh -File scripts/asr.ps1 validate "D:\transcripts\示例"
```

```bash
bash scripts/asr.sh validate "/data/transcripts/示例"
```

## 输出结构

```text
OUTPUT_DIR/
├── source/
│   ├── audio_16k.mp3
│   └── media.json
├── raw/
│   ├── <模型>.json
│   ├── <模型>.txt
│   ├── <模型>.srt
│   ├── <模型>.vtt
│   └── <模型>.tsv
├── audit-clips/
├── transcript.corrected.txt
├── transcript.corrected.srt
├── transcript.audit.md
└── run.json
```

- `source/`：标准化音频和媒体信息。
- `raw/`：不可修改的原始模型结果。
- `audit-clips/`：带上下文的争议区间音频。
- `transcript.corrected.*`：允许人工继续校订的保守版本。
- `transcript.audit.md`：模型分歧、证据和最终处理记录。
- `run.json`：环境、模型、设备、耗时、降级和原始文件哈希。

## 校订原则

1. 原始转录只保存，不覆盖。
2. 只有专有名词证据、模型共识或清晰音频上下文支持时才修改。
3. 不能因为一句话“更通顺”就认定它是原话。
4. 无法可靠确认时保留 `[听不清 HH:MM:SS]`。
5. 每次修改都应在 `transcript.audit.md` 中说明证据。

详细规则见 [references/correction-policy.md](references/correction-policy.md)。

## 平台与依赖

| 平台 | 本地后端 | 状态 |
| --- | --- | --- |
| Windows 10/11 + NVIDIA | `faster-whisper` + CUDA 12 + cuDNN 9 | 已在 RTX 4070 实机验证安装、CUDA 探针和重复运行 |
| Windows CPU | `faster-whisper small/int8` | 支持 |
| Apple Silicon | `mlx-whisper` | 已做安装逻辑和 dry-run 验证，仍需对应实机确认 |
| Linux + NVIDIA/CPU | `faster-whisper` | 已做安装逻辑和 dry-run 验证，仍需对应实机确认 |

基础要求：

- Python 3.9 或更高版本。
- FFmpeg 和 FFprobe。
- NVIDIA 本地加速需要 CUDA 12 与 cuDNN 9 兼容运行库。
- Apple Silicon 使用 `mlx-whisper`。

安装或 GPU 加载失败时见 [references/platforms.md](references/platforms.md)。

当前自动化测试覆盖后端/profile 选择、显存与计算类型、依赖版本、镜像测速、中文路径、云端重试、SRT 和原始文件哈希。Windows 实机还验证了：

- CTranslate2 识别到 CUDA 设备并返回支持的计算类型。
- 设置 `PIP_NO_INDEX=1` 后重复运行安装器仍成功，且 `pip freeze` 不变。
- 删除单个依赖后只修复该 requirement，不重复处理 NVIDIA 依赖组。

## 硅基流动分段与重试

工具会按接口限制预留余量：单段最长约 55 分钟、最大 49 MB。超限媒体会自动分段，并对 `429`、`503`、`504` 做有限次数的退避重试。接口模型或限制发生变化时，以 `doctor` 和服务端返回结果为准。

## 隐私与边界

- 使用本地后端时，音视频不上传到云端。
- 使用硅基流动后端或云端复核时，相应音频或争议片段会发送给硅基流动。
- 本 Skill 只处理本地音视频文件。
- 不包含视频下载、说话人分离、翻译、字幕烧录或内容分析。
- 评测音频、单元测试、缓存、模型权重和运行结果未包含在本仓库的最小发行集合中。

## 项目结构

```text
.
├── SKILL.md
├── README.md
├── references/
│   ├── correction-policy.md
│   └── platforms.md
└── scripts/
    ├── asr_cli.py
    ├── asr.ps1
    ├── asr.sh
    ├── setup.ps1
    ├── setup.sh
    ├── requirements-base.txt
    ├── requirements-macos.txt
    └── requirements-nvidia.txt
```
