# ============================================================================
#  可选：给知识库装 MinerU（更强的 PDF 解析）
#
#  用法（一般不用直接跑它，双击 scripts\install-mineru.cmd 即可）：
#      powershell -ExecutionPolicy Bypass -File scripts\install-mineru.ps1
#      powershell ... -File scripts\install-mineru.ps1 -WithVlm     # 再装 VLM 档
#      powershell ... -File scripts\install-mineru.ps1 -TorchIndex cu128
#      powershell ... -File scripts\install-mineru.ps1 -Force       # 重建 venv
#
#  它做四件事：
#    1. 建独立 venv（.mineru\.venv，Python 3.12；本机没有 Python 时 uv 会自己下）
#    2. 装 mineru[torch]，并把 torch 换成 **CUDA 版**（PyPI 的 Windows torch 是 CPU 版）
#    3. 按档位下模型（basic 约 0.9 GB；-WithVlm 再加 GGUF 1.24 GB）
#    4. 自检（models verify + 打印生效的后端/引擎）
#
#  ⚠ 为什么不用 `mineru[full]`：Windows 上 `full` 拉的是 **LMDeploy**（vLLM 只有
#    Linux），而本机实测 LMDeploy 起来就死 —— `ValueError: high is out of bounds
#    for int32`（engine_loop.py:661），装 triton-windows 也没救。VLM 走
#    **llama-cpp**（随 base 依赖进来的 mineru-llama-cpp，带 ggml-vulkan.dll）
#    + Q8 GGUF 才是 Windows 上能跑的路。
#
#  ⚠ 为什么不改系统环境：不写 PATH、不写注册表、不装系统级 Python，
#    所有东西都在项目目录 `$Root\.mineru` 下（删掉整个目录 = 卸载干净）。
#
#  退出码：0 可用 / 2 装好但没有 CUDA（会退化成 ONNX+CPU，慢但能跑）
#          3 磁盘不足 / 4 下载失败（网络）/ 1 其它失败
# ============================================================================
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12",
    # CUDA 轮子的索引。cu130 对应本机驱动（616.92 / RTX 4060 实测可用）；
    # 老驱动可以传 cu128 或 cu126。
    [string]$TorchIndex = "cu130",
    [switch]$WithVlm,
    [switch]$Force,
    [switch]$SkipModels
)

# 同 install-env.ps1：不要用 "Stop"，uv/pip 把进度写到 stderr，会被当成
# terminating error 让脚本在"正在下载"那一步假死。
$ErrorActionPreference = "Continue"
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Base = Join-Path $Root ".mineru"
$Venv = Join-Path $Base ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Kit = Join-Path $Venv "Scripts\mineru-kit.exe"
$Models = Join-Path $Base "models"
$Home2 = Join-Path $Base "home"
$Cfg = Join-Path $Home2 "config.yaml"
$UvDir = Join-Path $Root ".tools"
$Uv = Join-Path $UvDir "uv.exe"

$IndexUrl = "https://mirrors.aliyun.com/pypi/simple/"
$TorchUrl = "https://download.pytorch.org/whl/$TorchIndex"
$UvMirror = "https://ghproxy.net/https://github.com/astral-sh/uv/releases/latest/download"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Ok($msg)   { Say "  [OK] $msg" "Green" }
function Warn($msg) { Say "  [!!] $msg" "Yellow" }
function Bad($msg)  { Say "  [XX] $msg" "Red" }

function Invoke-Native {
    param([string]$Exe, [string[]]$ArgList, [switch]$Quiet)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @ArgList 2>&1
        $code = $LASTEXITCODE
        # ⚠ 外部程序写 stderr 的每一行会被 PowerShell 包成 ErrorRecord：
        #   · `"$_"` 在多数情况下给的是真文本，但 tqdm 那种"用 \r 刷进度"的
        #     输出会走到 `.Exception.Message` 变成
        #     "System.Management.Automation.RemoteException"（实测：MinerU 下模型
        #     的日志里每一行真文本后面都跟一行这个，把日志刷得没法看）。
        #   真文本在 `.TargetObject` 里，所以优先取它。
        $lines = @($out | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                $t = $_.TargetObject
                if ($t -is [string] -and $t.Trim()) { $t }
                elseif ($_.Exception -and $_.Exception.Message) { $_.Exception.Message }
                else { "$_" }
            } else { "$_" }
        })
        if (-not $Quiet) { $lines | ForEach-Object { Say "       $_" } }
        return @{ Code = $code; Lines = $lines }
    } catch {
        if (-not $Quiet) { Say "       $($_.Exception.Message)" "Yellow" }
        return @{ Code = 1; Lines = @("$($_.Exception.Message)") }
    } finally {
        $ErrorActionPreference = $old
    }
}

Say ""
Say "============================================================" "Cyan"
Say "  Zotero 文献知识库 · 可选组件：MinerU（更强的 PDF 解析）" "Cyan"
Say "============================================================" "Cyan"
Say "  项目目录：$Root"
Say "  安装位置：$Base"
Say ""

# ---------------------------------------------------------------- 0. 磁盘
Say "[0/7] 检查磁盘空间"
$drive = (Get-Item $Root).PSDrive
$freeGB = [math]::Round($drive.Free / 1GB, 1)
$needGB = if ($WithVlm) { 8 } else { 6 }
if ($freeGB -lt $needGB) {
    Bad "磁盘不够：$($drive.Name): 只剩 $freeGB GB，至少要 $needGB GB"
    Say "       把项目移到空间大的盘，或先清理磁盘再重跑。" "White"
    exit 3
}
Ok "$($drive.Name): 剩余 $freeGB GB（需要约 $needGB GB）"

# ---------------------------------------------------------------- 1. uv
Say ""
Say "[1/7] 准备 uv（Python 环境管理器）"
$needUv = $true
if ((Test-Path $Uv) -and -not $Force) {
    $needUv = $false
    Ok "已下载过：$Uv"
} else {
    $sysUv = Get-Command uv -ErrorAction SilentlyContinue
    if ($sysUv) { $Uv = $sysUv.Source; $needUv = $false; Ok "用系统的 uv：$Uv" }
}
if ($needUv) {
    New-Item -ItemType Directory -Force -Path $UvDir | Out-Null
    $zip = Join-Path $UvDir "uv.zip"
    $urls = @(
        "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip",
        "$UvMirror/uv-x86_64-pc-windows-msvc.zip"
    )
    $got = $false
    foreach ($u in $urls) {
        try {
            Say "       下载 uv …（约 17 MB）"
            Invoke-WebRequest -Uri $u -OutFile $zip -TimeoutSec 180 -UseBasicParsing
            $got = $true
            break
        } catch {
            Warn "这个地址不行，换下一个：$($u.Split('/')[2])"
        }
    }
    if (-not $got) {
        Bad "uv 下载失败。请检查网络，或手动装一个 uv 后重跑："
        Say "        winget install --id astral-sh.uv" "White"
        Say "        或 pip install uv" "White"
        exit 4
    }
    Expand-Archive -Path $zip -DestinationPath $UvDir -Force
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $Uv)) { Bad "解压后没找到 uv.exe"; exit 1 }
    Ok "uv 就绪：$Uv"
}

# 全部落项目目录：缓存、Python、镜像都指到这里，别去占 C 盘
$env:UV_DEFAULT_INDEX = $IndexUrl
$env:UV_INDEX_URL = $IndexUrl
$env:UV_PYTHON_INSTALL_MIRROR = "https://ghproxy.net/https://github.com/astral-sh/python-build-standalone/releases/download"
$env:UV_CACHE_DIR = Join-Path $Base "uv-cache"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Base "python"
New-Item -ItemType Directory -Force -Path $Base, $env:UV_CACHE_DIR, $env:UV_PYTHON_INSTALL_DIR | Out-Null

# ---------------------------------------------------------------- 2. venv
Say ""
Say "[2/7] 创建独立虚拟环境（.mineru\.venv，Python $PythonVersion）"
if ((Test-Path $Py) -and -not $Force) {
    Ok "已存在，跳过（要重建加 -Force）"
} else {
    if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
    Invoke-Native -Exe $Uv -ArgList @("venv", $Venv, "--python", $PythonVersion) | Out-Null
    if (-not (Test-Path $Py)) {
        Warn "本机没有 Python $PythonVersion，让 uv 下载一个（约 24 MB）…"
        Invoke-Native -Exe $Uv -ArgList @("python", "install", $PythonVersion) | Out-Null
        Invoke-Native -Exe $Uv -ArgList @("venv", $Venv, "--python", $PythonVersion) | Out-Null
    }
    if (-not (Test-Path $Py)) { Bad "建 venv 失败"; exit 1 }
    Ok "venv 建好：$Venv"
}
$pyver = (Invoke-Native -Exe $Py -ArgList @("-V") -Quiet).Lines -join " "
Ok "$pyver"

# ---------------------------------------------------------------- 3. mineru[torch]
Say ""
Say "[3/7] 安装 MinerU（mineru[torch]，约 1.5 GB，主要来自 torch）"
Say "       用镜像：$IndexUrl"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$code = (Invoke-Native -Exe $Uv -ArgList @(
    "pip", "install", "--python", $Py, "mineru[torch]", "--index-url", $IndexUrl)).Code
$sw.Stop()
if ($code -ne 0) {
    Bad "MinerU 安装失败（退出码 $code）"
    Say "       可以手动重试：" "White"
    Say "         `"$Uv`" pip install --python `"$Py`" `"mineru[torch]`" --index-url $IndexUrl" "White"
    exit 4
}
Ok ("mineru 装好（{0:N0} 秒）" -f $sw.Elapsed.TotalSeconds)

# ---------------------------------------------------------------- 4. CUDA torch
Say ""
Say "[4/7] 把 torch 换成 CUDA 版（PyPI 的 Windows torch 是 CPU 版）"
$cudaProbe = "import torch;print('CUDAVER', torch.version.cuda or 'cpu')"
$cudaVer = ((Invoke-Native -Exe $Py -ArgList @("-c", $cudaProbe) -Quiet).Lines |
            Where-Object { $_ -match "CUDAVER" } | Select-Object -Last 1)
$cudaVer = if ($cudaVer) { ($cudaVer -replace "CUDAVER", "").Trim() } else { "" }
if ($cudaVer -and $cudaVer -ne "cpu") {
    Ok "torch 已经是 CUDA 版：$cudaVer"
} else {
    Say "       当前是 CPU 版，从 $TorchUrl 装 CUDA 轮子（约 2 GB）"
    # ⚠ 两步都不能少：
    #   ① 把 uv 的默认索引（阿里云）**临时清空** —— 否则阿里云上的
    #      `torch 2.14.1`（CPU）也被当成候选，解析器认为"已满足"就不换了
    #      （实测：装完只花 3 秒、什么都没换、CUDA 仍不可用）；
    #   ② 加 `--upgrade` —— 不升级的话"已安装的版本已满足要求"，同样不动。
    $savedIdx = $env:UV_DEFAULT_INDEX
    $savedUv = $env:UV_INDEX_URL
    $env:UV_DEFAULT_INDEX = ""
    $env:UV_INDEX_URL = ""
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $code = (Invoke-Native -Exe $Uv -ArgList @(
        "pip", "install", "--python", $Py,
        "--index-url", $TorchUrl, "--upgrade",
        "--reinstall-package", "torch", "--reinstall-package", "torchvision",
        "torch", "torchvision")).Code
    $sw.Stop()
    $env:UV_DEFAULT_INDEX = $savedIdx
    $env:UV_INDEX_URL = $savedUv
    $after = ((Invoke-Native -Exe $Py -ArgList @("-c", $cudaProbe) -Quiet).Lines |
              Where-Object { $_ -match "CUDAVER" } | Select-Object -Last 1)
    $after = if ($after) { ($after -replace "CUDAVER", "").Trim() } else { "" }
    if ($code -eq 0 -and $after -and $after -ne "cpu") {
        Ok ("CUDA torch 装好（{0:N0} 秒，{1}）" -f $sw.Elapsed.TotalSeconds, $after)
    } elseif ($code -eq 0) {
        Warn "装是装完了，但 torch 还是 CPU 版（$after）—— 会退化成 ONNX+CPU"
        Say "       老驱动可以换索引重跑：-TorchIndex cu128（或 cu126）" "White"
    } else {
        Warn "CUDA 版 torch 没装上（退出码 $code）—— 不影响可用性，会退化成 ONNX+CPU（慢）"
    }
}
$cudaOk = $false
$probe = (Invoke-Native -Exe $Py -ArgList @(
    "-c", "import torch;print('CUDA_OK' if torch.cuda.is_available() else 'CUDA_NO')") -Quiet)
if (($probe.Lines -join " ") -match "CUDA_OK") {
    $cudaOk = $true
    $gpu = (Invoke-Native -Exe $Py -ArgList @(
        "-c", "import torch;print(torch.cuda.get_device_name(0))") -Quiet).Lines -join " "
    Ok "CUDA 可用：$gpu"
} else {
    Warn "CUDA 不可用 —— 小模型会走 ONNX/CPU，能跑但慢很多（大概 0.05 页/秒）"
}

# ---------------------------------------------------------------- 5. 配置
Say ""
Say "[5/7] 写 MinerU 配置（.mineru\home\config.yaml）"
New-Item -ItemType Directory -Force -Path $Home2, $Models | Out-Null
$cfgText = @"
# MinerU 配置（由 scripts/install-mineru.ps1 生成）
#
# model.base_dir 指到项目里，别让它落进 ~/.mineru（C 盘）。
# source 用 modelscope：本机实测 hf-mirror 的 API 会 403。
# vlm.engine 写死 llama-cpp：Windows 上 auto 会选 LMDeploy，而它在本机
#   起来就死（ValueError: high is out of bounds for int32）。
model:
  base_dir: $($Models -replace '\\', '/')
  source: modelscope
  small_backend: auto
  vlm:
    engine: llama-cpp
"@
# ⚠ 必须用无 BOM 的 UTF-8 写：PowerShell 5.1 的 `Out-File -Encoding UTF8`
#   会加 BOM，YAML 解析器会当场报错。
[System.IO.File]::WriteAllText($Cfg, $cfgText, (New-Object System.Text.UTF8Encoding($false)))
Ok "已写：$Cfg"

# ---------------------------------------------------------------- 6. 模型
Say ""
$env:MINERU_HOME = $Home2
$env:MINERU_MODEL_SOURCE = "modelscope"
if ($SkipModels) {
    Warn "[6/7] 按参数跳过模型下载（首次解析时会自动下）"
} else {
    Say "[6/7] 下载模型（basic 档约 0.9 GB；走 modelscope）"
    $v = Invoke-Native -Exe $Kit -ArgList @("models", "verify", "--tier", "basic") -Quiet
    if ($v.Code -eq 0) {
        Ok "basic 档模型已存在，跳过"
    } else {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $code = (Invoke-Native -Exe $Kit -ArgList @("models", "download", "--tier", "basic")).Code
        $sw.Stop()
        if ($code -ne 0) {
            Bad "模型下载失败（退出码 $code）—— 网络问题的话重跑本脚本即可（已下的会跳过）"
            exit 4
        }
        Ok ("basic 模型下好（{0:N0} 秒）" -f $sw.Elapsed.TotalSeconds)
    }
    if ($WithVlm) {
        Say "       再下 VLM 档（GGUF，约 1.24 GB，只在需要更高质量时用）"
        $v = Invoke-Native -Exe $Kit -ArgList @("models", "verify", "--tier", "standard") -Quiet
        if ($v.Code -eq 0) {
            Ok "VLM 档模型已存在，跳过"
        } else {
            $code = (Invoke-Native -Exe $Kit -ArgList @("models", "download", "--tier", "standard")).Code
            if ($code -ne 0) { Warn "VLM 档没下成（不影响 basic 可用）" } else { Ok "VLM 档模型下好" }
        }
    }
}

# ---------------------------------------------------------------- 7. 自检
Say ""
Say "[7/7] 自检"
$show = Invoke-Native -Exe $Kit -ArgList @("models", "show") -Quiet
$backend = ($show.Lines | Where-Object { $_ -match "小模型后端|VLM 引擎" }) -join " / "
if ($backend) { Ok $backend } else { Warn "models show 输出看不懂，自己跑一次：`"$Kit`" models show" }
$code = (Invoke-Native -Exe $Kit -ArgList @("models", "verify", "--tier", "basic")).Code
if ($code -ne 0) { Bad "basic 档模型不完整，重跑本脚本"; exit 4 }
Ok "basic 档模型完整"

Say ""
Say "============================================================" "Cyan"
if ($cudaOk) { Say "  装好了（GPU 可用）" "Green" } else { Say "  装好了（无 CUDA，将用 CPU）" "Yellow" }
Say "============================================================" "Cyan"
Say ""
Say "  下一步：到面板「运行环境」页点「检测」；或直接构建知识库。"
Say "  填给插件/面板的路径是："
Say "    $Kit" "White"
Say "  想装 VLM 档（更高质量、更慢）："
Say "    scripts\install-mineru.cmd -WithVlm" "White"
Say "  卸载：删掉 $Base 整个目录即可。" "White"
Say ""
if ($cudaOk) { exit 0 } else { exit 2 }
