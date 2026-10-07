# 把知识库本地服务装进 Windows 启动文件夹（开机自动跑）。
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Remove
#
# 为什么需要：本地服务（:8765）是后台进程，Zotero/DSH 重启不会带上它。
# 本机实测它掉过好几次，现象是插件里"分类建议失败"，但真实原因只是服务没跑。
#
# 做法：往启动文件夹生成一个 .vbs 包装脚本，里面用**绝对路径**调真正的
# 启动脚本（scripts\4-service.vbs）。
#
# ⚠ 为什么是"生成"而不是"复制现成的"：
#   启动文件夹里的脚本不能用相对路径推算项目根（它自己的位置和项目无关），
#   所以必须写绝对路径。而绝对路径**不能提交进仓库**（那是开发机的路径，
#   别人拿到就指向不存在的目录）。所以 scripts\autostart-service.vbs 是
#   按"自己在哪"推算的模板，这里把它**改写成带本机绝对路径的版本**再放进
#   启动文件夹。
#   因此：项目挪了位置，重跑一次本脚本即可（不用手改任何文件）。
#
# 不用 .lnk 快捷方式 —— 建快捷方式要 COM/WScript.Shell，脚本里不稳；
# 放一个 vbs 更直接，双击也能手动跑。

param([switch]$Remove)

$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent          # …\zotero-kb
$src  = Join-Path $PSScriptRoot "autostart-service.vbs"
$startup = [Environment]::GetFolderPath('Startup')
$dest = Join-Path $startup "zotero-kb-service.vbs"

if ($Remove) {
    if (Test-Path $dest) {
        Remove-Item $dest -Force
        Write-Host "[OK] 已移除自启：$dest"
    } else {
        Write-Host "[--] 本来就没装：$dest"
    }
    exit 0
}

if (-not (Test-Path $src)) {
    Write-Host "[XX] 找不到 $src"
    exit 1
}

$service = Join-Path $root "scripts\4-service.vbs"
if (-not (Test-Path $service)) {
    Write-Host "[XX] 找不到 $service —— 项目结构不对？"
    exit 1
}

# 把模板里的"按自身位置推算"整段换成写死的绝对路径。
$body = Get-Content $src -Raw
$generated = @"
' 开机自启：拉起知识库本地服务（给 Zotero 插件用）。
'
' ⚠ 本文件由 scripts\install-autostart.ps1 自动生成，不要手改 ——
'   项目挪了位置就重跑一次那个脚本。
'
' 这里用绝对路径是**故意的**：启动文件夹里的脚本无法从自身位置推算项目根。
Option Explicit

Dim shell, target, fso
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

target = "$service"

If Not fso.FileExists(target) Then
    ' 找不到就静默退出（开机时弹窗会很烦）
    WScript.Quit 1
End If

On Error Resume Next
shell.Run """" & target & """", 0, False
On Error Goto 0
"@

Set-Content -Path $dest -Value $generated -Encoding UTF8
Write-Host "[OK] 已安装自启："
Write-Host "     $dest"
Write-Host "     → 指向 $service"
Write-Host "     下次登录 Windows 会自动拉起知识库服务（:8765）。"
Write-Host ""
Write-Host "现在也可以立刻跑一次："
Write-Host "     wscript `"$dest`""
