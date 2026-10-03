# ============================================================================
#  一键装好知识库的 Python 环境（建 venv + 装依赖）
#
#  用法（一般不用直接跑它，双击 scripts\install-env.cmd 即可）：
#      powershell -ExecutionPolicy Bypass -File scripts\install-env.ps1
#      powershell ... -File scripts\install-env.ps1 -PythonVersion 3.12
#      powershell ... -File scripts\install-env.ps1 -SkipModel     # 先不下载嵌入模型
#
#  为什么用 uv 而不是让用户自己装 Python：
#      实测 uv 能在 **3.3 秒**内自动下载安装一个完整 Python（24 MB），
#      而让非程序员用户去 python.org 下载安装器、记得勾 "Add to PATH"、
#      再手敲三条命令，失败率高得多。
#      uv 是单文件（38 MB）、免安装、不动系统 PATH、不写注册表。
#
#  ⚠ 不变量：这个脚本**不修改系统环境**，所有东西都放在项目目录下。
# ============================================================================
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12",
    [switch]$SkipModel,
    [switch]$Force
)

# ⚠ 不要用 "Stop"：uv / pip 把进度写到 stderr，Stop 会把它当成
#   terminating error，脚本会在"uv 正在下载 Python"这一步直接死掉
#   （本机实测踩过，退出码 1 但其实下载成功）。
#   改用显式检查退出码，见 Invoke-Native。
$ErrorActionPreference = "Continue"
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Venv = Join-Path $Root ".venv"
$UvDir = Join-Path $Root ".tools"
$Uv = Join-Path $UvDir "uv.exe"
$Req = Join-Path $Root "requirements.txt"

# 国内镜像：实测清华源缺 mcp 包，阿里源齐全
$IndexUrl = "https://mirrors.aliyun.com/pypi/simple/"
$UvMirror = "https://ghproxy.net/https://github.com/astral-sh/uv/releases/latest/download"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Ok($msg)   { Say "  [OK] $msg" "Green" }
function Warn($msg) { Say "  [!!] $msg" "Yellow" }
function Bad($msg)  { Say "  [XX] $msg" "Red" }


function Invoke-Native {
    <#
      跑外部程序，把 stdout/stderr 都当普通文本收下来，**只用退出码判断成败**。
      为什么需要：PowerShell 会把外部程序写到 stderr 的每一行当成错误，
      而 uv / pip / python 的进度与警告都走 stderr。
      返回 @{ Code = 退出码; Lines = 输出行 }
    #>
    param([string]$Exe, [string[]]$ArgList, [switch]$Quiet)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @ArgList 2>&1
        $code = $LASTEXITCODE
        $lines = @($out | ForEach-Object { "$_" })
        if (-not $Quiet) { $lines | ForEach-Object { Say "       $_" } }
        return @{ Code = $code; Lines = $lines }
    } catch {
        if (-not $Quiet) { Say "       $($_.Exception.Message)" "Yellow" }
        return @{ Code = 1; Lines = @("$($_.Exception.Message)") }
    } finally {
        $ErrorActionPreference = $old
    }
}

function Test-Python($exe) {
    if (-not (Test-Path $exe)) { return $false }
    try {
        $null = & $exe -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

Say ""
Say "============================================================" "Cyan"
Say "  Zotero 文献知识库 · 安装 Python 环境" "Cyan"
Say "============================================================" "Cyan"
Say "  项目目录：$Root"
Say ""

# ---------------------------------------------------------------- 1. 找 uv
Say "[1/6] 准备 uv（Python 环境管理器）"
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
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            Invoke-WebRequest -Uri $u -OutFile $zip -TimeoutSec 180 -UseBasicParsing
            $sw.Stop()
            Ok ("下载完成（{0:N1} MB，{1:N0} 秒）" -f ((Get-Item $zip).Length/1MB), $sw.Elapsed.TotalSeconds)
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
        exit 1
    }
    Expand-Archive -Path $zip -DestinationPath $UvDir -Force
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $Uv)) { Bad "解压后没找到 uv.exe"; exit 1 }
    Ok "uv 就绪：$Uv"
}
Invoke-Native -Exe $Uv -ArgList @("--version") | Out-Null

# 让 uv 也用国内镜像（环境变量对 uv 生效，不用改用户配置）
$env:UV_DEFAULT_INDEX = $IndexUrl
$env:UV_INDEX_URL = $IndexUrl
$env:UV_PYTHON_INSTALL_MIRROR = "https://ghproxy.net/https://github.com/astral-sh/python-build-standalone/releases/download"

# ---------------------------------------------------------------- 2. 建 venv
Say ""
Say "[2/6] 创建虚拟环境（.venv）"
if ((Test-Path (Join-Path $Venv "Scripts\python.exe")) -and -not $Force) {
    Ok "已存在，跳过（要重建加 -Force）"
} else {
    if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
    # 先用本机已有的 Python；没有就让 uv 自己下一个
    Say "       找 Python $PythonVersion …"
    $pyc = Get-Command py -ErrorAction SilentlyContinue
    $made = $false
    $r = Invoke-Native -Exe $Uv -ArgList @("venv", $Venv, "--python", $PythonVersion)
    $made = Test-Path (Join-Path $Venv "Scripts\python.exe")
    if (-not $made) {
        Warn "本机没有 Python $PythonVersion，让 uv 下载一个（约 24 MB）…"
        Invoke-Native -Exe $Uv -ArgList @("python", "install", $PythonVersion) | Out-Null
        Invoke-Native -Exe $Uv -ArgList @("venv", $Venv, "--python", $PythonVersion) | Out-Null
        $made = Test-Path (Join-Path $Venv "Scripts\python.exe")
    }
    if (-not $made) {
        Bad "建 venv 失败。如果一直失败，可以自己装个 Python 3.10+ 再重跑："
        Say "        https://www.python.org/downloads/windows/" "White"
        exit 1
    }
    Ok "venv 建好：$Venv"
}
$Py = Join-Path $Venv "Scripts\python.exe"
Invoke-Native -Exe $Py -ArgList @("--version") | Out-Null

# ---------------------------------------------------------------- 3. 装依赖
Say ""
Say "[3/6] 安装依赖（约 270 MB，主要来自 onnxruntime 和 pymupdf）"
if (-not (Test-Path $Req)) { Bad "找不到 $Req"; exit 1 }
Say "       用镜像：$IndexUrl"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$code = (Invoke-Native -Exe $Uv -ArgList @(
    "pip", "install", "--python", $Py, "-r", $Req, "--index-url", $IndexUrl)).Code
$sw.Stop()
if ($code -ne 0) {
    Bad "依赖安装失败（退出码 $code）"
    Say "       可以手动重试：" "White"
    Say "         & `"$Py`" -m pip install -r requirements.txt -i $IndexUrl" "White"
    exit 1
}
Ok ("依赖装好（{0:N0} 秒）" -f $sw.Elapsed.TotalSeconds)

# ---------------------------------------------------------------- 4. 验证
Say ""
Say "[4/6] 验证"
$probe = @'
import importlib, sys
mods = {"mcp": "MCP 服务器", "numpy": "向量计算", "fitz": "读 PDF (pymupdf)",
        "zstandard": "读会话记录"}
bad = []
for m, desc in mods.items():
    try:
        importlib.import_module(m)
        print(f"  [OK] {desc}")
    except Exception as e:
        bad.append(m)
        print(f"  [XX] {desc} 不可用：{e}")
try:
    import fastembed
    print("  [OK] 本地嵌入模型 (fastembed)")
except Exception as e:
    bad.append("fastembed")
    print(f"  [XX] 本地嵌入模型 (fastembed) 不可用：{e}")
sys.exit(1 if bad else 0)
'@
$probeFile = Join-Path $UvDir "_probe.py"
Set-Content -Path $probeFile -Value $probe -Encoding UTF8
$probeOk = (Invoke-Native -Exe $Py -ArgList @("-X", "utf8", $probeFile)).Code -eq 0
Remove-Item $probeFile -Force -ErrorAction SilentlyContinue
if ($probeOk) { Ok "依赖齐全" } else { Warn "有依赖不可用，看上面哪一行是 [XX]" }

# ---------------------------------------------------------------- 5. 嵌入模型
Say ""
Say "[5/6] 嵌入模型（首次构建知识库时要，约 90 MB）"
if ($SkipModel) {
    Warn "按参数跳过。以后跑 scripts\1-convert.cmd 时会自动下载"
} else {
    $env:HF_ENDPOINT = "https://hf-mirror.com"     # 国内镜像
    $env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
    $cache = Join-Path $env:USERPROFILE ".cache\huggingface"
    $have = $false
    if (Test-Path $cache) {
        $have = [bool](Get-ChildItem $cache -Recurse -Directory -ErrorAction SilentlyContinue |
                       Where-Object { $_.Name -like "*bge-small-zh*" } | Select-Object -First 1)
    }
    if ($have) {
        Ok "已经下载过，跳过"
    } else {
        $dl = @'
import sys
try:
    from fastembed import TextEmbedding
    print("  正在下载/加载 BAAI/bge-small-zh-v1.5 …")
    m = TextEmbedding("BAAI/bge-small-zh-v1.5")
    v = list(m.embed(["测试"]))[0]
    print(f"  [OK] 模型可用，向量维度 {len(v)}")
except Exception as e:
    print(f"  [!!] 模型下载失败：{e}")
    print("       不影响安装。以后跑 1-convert.cmd 时会再试一次；")
    print("       国内网络建议设 HF_ENDPOINT=https://hf-mirror.com")
    sys.exit(1)
'@
        $dlFile = Join-Path $UvDir "_model.py"
        Set-Content -Path $dlFile -Value $dl -Encoding UTF8
        $mres = Invoke-Native -Exe $Py -ArgList @("-X", "utf8", $dlFile)
        if ($mres.Code -ne 0) { Warn "模型没下成，不影响安装" }
        Remove-Item $dlFile -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------- 6. 生成 DSH 接入配置
#
# DSH 的 bundle 配置里 command/args 必须是**绝对路径**，所以仓库里放的是
# 带占位符的模板（bundle\cordis.patch.yml.tmpl），这里按本机实际情况生成。
# 不做这一步，别人克隆下来接不进 DSH；而提交一份硬编码路径就是泄露目录结构。
Say ""
Say "[6/6] 生成 DSH 接入配置"
$gen = Join-Path $Root "tools\gen_bundle_config.py"
if (Test-Path $gen) {
    $gres = Invoke-Native -Exe $Py -ArgList @("-X", "utf8", $gen) -Quiet
    if ($gres.Code -eq 0) {
        Ok "已生成 bundle\cordis.patch.yml"
    } else {
        Warn "生成失败（不影响知识库本身；接 DSH 时再手动跑一次）："
        $gres.Lines | ForEach-Object { Say "       $_" }
    }
} else {
    Warn "找不到 tools\gen_bundle_config.py，跳过"
}

# ---------------------------------------------------------------- 收尾
Say ""
Say "============================================================" "Cyan"
Say "  装好了" "Green"
Say "============================================================" "Cyan"
Say ""
Say "  接下来："
Say "    1) 构建知识库     双击 scripts\1-convert.cmd"
Say "    2) 打开管理面板   双击 scripts\0-panel.vbs"
Say "    3) 装 Zotero 插件 工具 → 插件 → 齿轮 → Install Plugin From File…"
Say "         （.xpi 在哪：管理面板「高级 → 插件安装」里点「打包插件 xpi」生成）"
Say ""
Say "  想接进 DSH（让 AI 能检索你的文献）的话："
Say "    见 README 的「接入 DSH」一节，里面有一条命令。"
Say ""
Say "  如果 .venv 不在项目目录里（比如你挪过它），"
Say "  到 Zotero 设置 →「文献知识库」→「运行环境」把 Python 路径填上即可，"
Say "  不用改任何代码。"
Say ""
