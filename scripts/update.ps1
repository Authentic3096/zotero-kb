# ============================================================================
#  升级便携包：只覆盖程序文件，保留 .venv、知识库数据与 Zotero 设置
#
#  用法（一般双击 scripts\update.cmd 即可）：
#      powershell -ExecutionPolicy Bypass -File scripts\update.ps1
#      powershell ... -File scripts\update.ps1 -Target "C:\zotero-kb"
#
#  典型流程：下载新的 *-win64.zip → 解压到新文件夹 → 运行新文件夹里的
#  scripts\update.cmd → 按提示输入旧的项目目录。
#
#  ⚠ 不删除用户数据：知识库默认在 Zotero 数据目录下（不在项目目录里），
#    本脚本也**不碰** .venv / .tools / kb-location.json / .env / bundle。
# ============================================================================
[CmdletBinding()]
param(
    [string]$Target = "",
    [switch]$Force
)

# ⚠ 不用 Stop：uv / pip 把进度写到 stderr，Stop 会把它当成 terminating error。
$ErrorActionPreference = "Continue"
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Source = Split-Path -Parent $PSScriptRoot
$IndexUrl = "https://mirrors.aliyun.com/pypi/simple/"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Ok($msg)   { Say "  [OK] $msg" "Green" }
function Warn($msg) { Say "  [!!] $msg" "Yellow" }
function Bad($msg)  { Say "  [XX] $msg" "Red" }

Say ""
Say "============================================================" "Cyan"
Say "  Zotero 文献知识库 · 升级便携包" "Cyan"
Say "============================================================" "Cyan"
Say "  新包目录：$Source"

if (-not $Target) {
    Say ""
    Say "  请输入**已安装**的项目目录（例如 C:\zotero-kb）。"
    Say "  直接回车 = 就更新当前这个目录。"
    $Target = Read-Host "  旧项目目录"
    if ([string]::IsNullOrWhiteSpace($Target)) { $Target = $Source }
}
$Target = $Target.Trim().Trim('"')
if (-not (Test-Path -LiteralPath $Target)) {
    Bad "目标目录不存在：$Target"
    exit 1
}
# 用 GetFullPath 而不是 Resolve-Path：后者会把长用户名折叠成 AUTHEN~1，
# 显示和后续排错都不直观。
$Target = [System.IO.Path]::GetFullPath($Target)
Say "  旧包目录：$Target"

if (-not (Test-Path (Join-Path $Target "offline\schemas.py"))) {
    Bad "目标目录不像知识库项目（缺 offline\schemas.py）：$Target"
    Say "       请选对目录，或先按 QUICKSTART 装一次。"
    exit 1
}

$same = ($Target -eq $Source)
if ($same) {
    Warn "新旧目录相同：文件已经是新的，下面只检查依赖与插件。"
} else {
    Say ""
    Say "[1/3] 复制程序文件（不碰 .venv / .tools / 用户数据）"
    $dirs = @("offline", "online", "tools", "scripts", "skills")
    foreach ($d in $dirs) {
        $src = Join-Path $Source $d
        $dst = Join-Path $Target $d
        if (-not (Test-Path $src)) { Warn "新包里没有 $d，跳过"; continue }
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        Copy-Item (Join-Path $src "*") $dst -Recurse -Force
        Ok "$d"
    }
    foreach ($f in @("requirements.txt", "README.md", "INSTALL.md",
                     "QUICKSTART.md", "VERSION", "LICENSE")) {
        $src = Join-Path $Source $f
        if (Test-Path $src) {
            Copy-Item $src (Join-Path $Target $f) -Force
            Ok $f
        }
    }
    # 清掉旧字节码，避免升级后仍加载旧 .pyc（Python 会自己重建）
    Get-ChildItem $Target -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

Say ""
Say "[2/3] 检查 Python 依赖"
$Py = Join-Path $Target ".venv\Scripts\python.exe"
$Req = Join-Path $Target "requirements.txt"
$Stamp = Join-Path $Target ".venv\requirements.sha256"
if (-not (Test-Path $Py)) {
    Warn "目标目录里没有 .venv。请先运行：$Target\scripts\install-env.cmd"
} elseif (-not (Test-Path $Req)) {
    Warn "找不到 requirements.txt，跳过依赖检查"
} else {
    $hash = (Get-FileHash $Req -Algorithm SHA256).Hash.ToLower()
    $old = ""
    if (Test-Path $Stamp) {
        try { $old = (Get-Content $Stamp -Raw).Trim().ToLower() } catch {}
    }
    if (-not $Force -and $hash -eq $old) {
        Ok "requirements.txt 没变，跳过依赖安装"
    } else {
        $Uv = Join-Path $Target ".tools\uv.exe"
        if (-not (Test-Path $Uv)) {
            $sysUv = Get-Command uv -ErrorAction SilentlyContinue
            if ($sysUv) { $Uv = $sysUv.Source }
        }
        if (-not (Test-Path $Uv)) {
            Warn "找不到 uv，无法自动更新依赖。请先运行：$Target\scripts\install-env.cmd"
        } else {
            Say "       依赖有变化，运行 uv pip install …"
            & $Uv pip install --python $Py -r $Req --index-url $IndexUrl
            if ($LASTEXITCODE -ne 0) {
                Bad "依赖安装失败（退出码 $LASTEXITCODE）"
                Say ('       可手动重试：& "' + $Py + '" -m pip install -r "' + $Req + '" -i ' + $IndexUrl) "White"
                exit 1
            }
            Set-Content -Path $Stamp -Value $hash -Encoding ASCII
            Ok "依赖已更新"
        }
    }
}

Say ""
Say "[3/3] 插件 xpi"
$xpi = @(Get-ChildItem (Join-Path $Source "zotero-plugin") -Filter "zotero-kb-*.xpi" -File -ErrorAction SilentlyContinue |
         Sort-Object Name -Descending | Select-Object -First 1)
if ($xpi.Count -gt 0) {
    Ok "新 xpi：$($xpi[0].FullName)"
} else {
    Warn "新包里没有 xpi；到 Releases 单独下载 zotero-kb-<版本>.xpi"
}

Say ""
Say "============================================================" "Cyan"
Say "  程序文件已更新" "Green"
Say "============================================================" "Cyan"
Say ""
Say "  还差两步："
Say "    1) 装新插件：Zotero → 工具 → 插件 → 齿轮 → Install Plugin From File…"
Say "       选上面的 xpi；装完**完全退出 Zotero 再打开**。"
Say "    2) 重新打开管理面板，确认「知识库结构」页数字正常。"
Say ""
Say "  .venv、知识库数据（index.db / edits / trash）与 Zotero 设置都保留着。"
Say ""
exit 0
