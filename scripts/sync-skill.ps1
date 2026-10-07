# 把本项目 skills\ 下的所有技能同步到 DSH 的用户级技能目录。
#
# 为什么需要手动同步：Windows 建符号链接需要开发者模式，没开就只能复制。
# 复制之后 DSH 读的是副本，改了源文件不会自动生效 —— 改完跑一次这个脚本。
#
# 自动遍历 skills\ 下的每个子目录（每个子目录 = 一个技能，含 SKILL.md），
# 这样以后新增技能不用改本脚本。

$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent          # …\zotero-kb
$skillsDir = Join-Path $root "skills"
$destRoot = Join-Path $env:USERPROFILE ".dsh\skills"

if (-not (Test-Path $skillsDir)) {
    Write-Host "[XX] 找不到技能目录 $skillsDir"
    exit 1
}

$dirs = Get-ChildItem $skillsDir -Directory | Where-Object {
    Test-Path (Join-Path $_.FullName "SKILL.md")
}
if (-not $dirs) {
    Write-Host "[XX] $skillsDir 下没有含 SKILL.md 的技能目录"
    exit 1
}

New-Item -ItemType Directory -Force -Path $destRoot | Out-Null
$n = 0
foreach ($d in $dirs) {
    $dest = Join-Path $destRoot $d.Name
    # 目标已是链接就不动它（链接会自动跟随源文件）
    if (Test-Path $dest) {
        $item = Get-Item $dest -Force
        if ($item.LinkType) {
            Write-Host "[OK] $($d.Name)：已是链接（$($item.LinkType)），跳过"
            continue
        }
    }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    # 复制该技能目录下的全部文件（不止 SKILL.md，可能还有脚本/参考文件）
    Copy-Item (Join-Path $d.FullName "*") $dest -Recurse -Force
    $files = (Get-ChildItem $d.FullName -Recurse -File).Count
    Write-Host "[OK] $($d.Name)：已同步 $files 个文件 -> $dest"
    $n++
}

Write-Host ""
Write-Host "共同步 $n 个技能。新开会话（或重启 DSH）后生效。"
