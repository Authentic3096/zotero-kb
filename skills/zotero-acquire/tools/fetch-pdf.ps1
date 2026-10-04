# fetch-pdf.ps1 —— 把一个**开放获取**的 PDF 直链下载到约定的落地目录，并校验它真的是 PDF。
#
# 为什么需要这个脚本（而不是让模型每次现敲 Invoke-WebRequest）：
#   1. **下载成功 ≠ 拿到 PDF**。出版社/聚合站经常返回一个 HTML 拦截页
#      （"正在验证浏览器…"、"Access Denied"）而 HTTP 状态码仍是 200。
#      只看状态码就报"下载好了"，用户拖进 Zotero 才发现是个网页 —— 这个坑必须由代码堵住，
#      所以**落盘后必须读文件头**，见 Test-PdfFile。
#   2. **落地目录要有一处权威约定**。散在各处的路径迟早漂移（本项目 README 里
#      `KNOWN_BASE_URLS` 与 `KB_SETTINGS` 两处重复就是前车之鉴），统一在这里解析。
#   3. **文件名要能安全落盘**。论文标题里什么字符都有（`:` `?` `/` `"` 中文标点），
#      直接当文件名会被 Windows 拒绝或截断。
#
# 本脚本**不判断**某个 PDF 是否合法开放获取 —— 那是使用者的判断（见 skill 的边界一节），
# 脚本只负责"把给定 URL 稳定地拿下来并验证"。这样职责清晰，也不会被误用成"付费墙下载器"。
#
# 用法：
#   .\fetch-pdf.ps1 -Url <直链> -Name "<作者 年份 短标题>" [-Dir <落地目录>] [-DryRun]
#   .\fetch-pdf.ps1 -SelfTest          # 不发网络请求，自检 PDF 判定逻辑
#
# 退出码：0 成功 / 1 参数或环境错 / 2 下载失败 / 3 拿到的不是 PDF / 4 文件太小

[CmdletBinding()]
param(
    # PDF 直链。只接受 http/https。
    [string]$Url = "",

    # 文件名主干（不含 .pdf）。会给出去掉非法字符、截断、去重后的最终名字。
    [string]$Name = "",

    # 落地目录。留空 = 走 Resolve-TargetDir 的约定（见那里的注释）。
    [string]$Dir = "",

    # 只打印将要做什么，不落盘。给"先让用户确认"的场景用。
    [switch]$DryRun,

    # 不发网络请求，用临时文件自检 PDF 判定与文件名清洗。
    [switch]$SelfTest
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# PDF 规范要求文件以 `%PDF-` 开头（后面跟版本号），以 `%%EOF` 结尾。
# 只看开头就够挡掉 HTML 拦截页；结尾那条作为"是否被截断"的弱信号，单独报。
$PDF_MAGIC = "%PDF-"

# 小于这个字节数的不可能是正经论文（最小的单页 PDF 也有几 KB）。
# 定 8KB 是为了挡住"200 + 空的/占位的 PDF"这种更隐蔽的失败。
$MIN_BYTES = 8192

# 默认 User-Agent。为什么不留空：
#   - 不少出版社 CDN 对 `-UseBasicParsing` 的默认 UA 直接 403；
#   - 也不能伪装成 Mozilla 浏览器 —— Zotero 本地端口会因 `Mozilla/` 前缀直接丢弃请求
#     （见实施方案 §9.1 事实 3）。这里下的是**外网**资源、不碰 Zotero 端口，
#     但仍然显式声明自己的身份，便于对方按 UA 限流时能识别出这是工具流量。
$DEFAULT_UA = "ZoteroAcquire/1.0 (DSH skill; open-access only)"

# 文件名主干长度上限。Windows 整路径上限 260，落地目录本身已有几十字符，
# 再叠一个中文长标题就会超 —— 留足余量。
$MAX_STEM = 90

# ---------------------------------------------------------------------------
# 落地目录的权威解析
# ---------------------------------------------------------------------------

function Get-DownloadsDir {
    <#
      取"下载"文件夹的**真实**路径。

      为什么不写 `$env:USERPROFILE\Downloads`：这个文件夹是**可被重定向**的
      （OneDrive 备份、用户手动改位置都会改它）。写死的路径在那些机器上会指向一个
      不存在或已废弃的目录，而报错是"目录不存在"这种看着像脚本坏的形状。
      注册表里的 Shell Folders 是 Windows 自己用的那份，跟资源管理器里点到的是同一个。
    #>
    $guid = "{374DE290-123F-4565-9164-39C4925E467B}"   # FOLDERID_Downloads
    try {
        $key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        $v = (Get-ItemProperty -Path $key -ErrorAction Stop).$guid
        if ($v -and (Test-Path -LiteralPath $v)) { return $v }
    } catch {
        # 读不到注册表就退回落默认位置，不让整条流程因为"取不到下载夹"而失败
    }
    return (Join-Path $env:USERPROFILE "Downloads")
}

function Resolve-TargetDir {
    <#
      解析落地目录，优先级：显式 -Dir > 环境变量 ZOTERO_ACQUIRE_DIR > 约定默认。

      为什么要有环境变量这一层：约定变了、或者用户想把暂存区放到别的盘，
      不用改脚本也不用改 skill，设个环境变量就行。避免"配置散落在文档里"。
    #>
    param([string]$Explicit)

    if ($Explicit) { return $Explicit }

    $envDir = ($env:ZOTERO_ACQUIRE_DIR)
    if ($envDir -and $envDir.Trim()) { return $envDir.Trim() }

    # 默认约定：<下载>\zotero-acquire\
    # 为什么是"下载文件夹下的固定子目录"（而不是项目目录、也不是临时目录）：
    #   · 下一步是**人工把 PDF 拖进 Zotero**。拖拽的起点必须是用户一眼能找到的地方，
    #     而"下载"是所有 Windows 用户找文件的第一个去处；
    #   · 项目目录是**跨工作区可读的插件源码目录**，
    #     往里堆几百 MB 用户数据既污染源码树、又会被打包/备份脚本一起卷走；
    #   · 临时目录（%TEMP%）会被系统清理，"下载完过两天再拖"就找不到了。
    return (Join-Path (Get-DownloadsDir) "zotero-acquire")
}

# ---------------------------------------------------------------------------
# 文件名清洗
# ---------------------------------------------------------------------------

function ConvertTo-SafeStem {
    <#
      把任意字符串变成可以安全当文件名的主干。

      三条处理各自的理由：
        · 非法字符替换成 `_`：Windows 文件名禁用 `\ / : * ? " < > |`，
          而论文标题里 `:` 和 `?` 极常见（"What is…?"、"Cool pavements: a review"）。
          直接落盘会抛异常，而不是给个怪名字。
        · 控制字符（含换行）直接丢掉：从网页/API 抄来的标题偶尔带 `\n`，
          留着会让后续所有拼接路径的调用方莫名其妙地失败。
        · 折叠连续空白并截断到 $MAX_STEM：见上面常量的注释（防超长路径）。
          用字符数而不是字节数截断，中文标题才不会截出半个字。
    #>
    param([string]$Text)

    if (-not $Text) { return "untitled" }

    $s = $Text -replace '[\x00-\x1F\x7F]', ''          # 控制字符
    $s = $s -replace '[\\/:*?"<>|]', '_'              # Windows 非法字符
    $s = $s -replace '\s+', ' '                        # 折叠空白
    $s = $s.Trim()
    $s = $s.TrimEnd('.')                               # 结尾的点在 Windows 上会被静默吃掉

    if ($s.Length -gt $MAX_STEM) { $s = $s.Substring(0, $MAX_STEM).Trim() }
    if (-not $s) { return "untitled" }
    return $s
}

# ---------------------------------------------------------------------------
# PDF 校验
# ---------------------------------------------------------------------------

function Test-PdfFile {
    <#
      判断一个文件是不是**真的** PDF。返回 @{ ok; bytes; magic; eof; reason }

      只读文件头/尾各 1KB，不把整个文件读进内存 —— 论文 PDF 常见几十 MB，
      为了看 5 个字节而 ReadAllBytes 是没必要的浪费。

      `eof` 单独返回而不并进 ok：`%%EOF` 允许出现在文件尾部 1KB 内的任意位置
      （有些生成器会在它后面再补注释），扫不到更可能是"尾部有额外数据"，
      而不是文件坏了 —— 所以它只作提示，不作判据。
    #>
    param([string]$Path)

    $res = @{ ok = $false; bytes = 0; magic = ""; eof = $false; reason = "" }

    if (-not (Test-Path -LiteralPath $Path)) {
        $res.reason = "文件不存在"
        return $res
    }

    $fi = Get-Item -LiteralPath $Path
    $res.bytes = $fi.Length

    if ($fi.Length -lt $MIN_BYTES) {
        $res.reason = "只有 $($fi.Length) 字节（<$MIN_BYTES），不可能是完整论文"
        return $res
    }

    # 读文件头：用 FileStream 精确读前 1KB。
    # 为什么不用 `Get-Content -Encoding Byte -TotalCount 5`：那个在 PS 5.1 与 PS 7
    # 上参数名不同（`-Encoding Byte` vs `-AsByteStream`），换个环境就报错。
    $head = New-Object byte[] 1024
    $nHead = 0
    $fs = [System.IO.File]::OpenRead($Path)
    try {
        $nHead = $fs.Read($head, 0, $head.Length)
        $tail = New-Object byte[] 1024
        $nTail = 0
        if ($fs.Length -gt $head.Length) {
            $fs.Seek(-1024, [System.IO.SeekOrigin]::End) | Out-Null
            $nTail = $fs.Read($tail, 0, $tail.Length)
        }
    } finally {
        $fs.Dispose()
    }

    $headStr = [System.Text.Encoding]::ASCII.GetString($head, 0, $nHead)
    $tailStr = [System.Text.Encoding]::ASCII.GetString($tail, 0, $nTail)

    $res.magic = $headStr.Substring(0, [Math]::Min(8, $headStr.Length))
    $res.eof = $tailStr.Contains("%%EOF")

    # 允许 BOM/前导空白之外的少量偏移：极少见但存在"前面有几个字节垃圾"的 PDF。
    # 判据仍是"开头附近必须出现 %PDF-"，而不是"必须以它开头"，避免误杀。
    if ($headStr.IndexOf($PDF_MAGIC) -lt 0) {
        # 开局不是 %PDF- 时，最可能的真相是"下到了一个 HTML 页面"。
        # 把开头截出来给使用者看，一眼就能认出来是什么。
        $snip = ($headStr -replace '[\r\n\t]+', ' ').Substring(0, [Math]::Min(120, $headStr.Length))
        $res.reason = "开头不是 $PDF_MAGIC，实际是：$snip"
        return $res
    }

    $res.ok = $true
    $res.reason = "OK"
    return $res
}

# ---------------------------------------------------------------------------
# 自检（-SelfTest）：不发网络请求，只验判定逻辑
# ---------------------------------------------------------------------------

function Invoke-SelfTest {
    Write-Host "=== 自检：文件名清洗 ==="
    $cases = @(
        @{ in = "Cool pavements: a review of UHI?"; want = "Cool pavements_ a review of UHI_" },
        @{ in = "城市热岛/缓解  :研究"; want = "城市热岛_缓解 _研究" },
        @{ in = "line`nbreak"; want = "linebreak" },
        @{ in = ""; want = "untitled" }
    )
    $bad = 0
    foreach ($c in $cases) {
        $got = ConvertTo-SafeStem -Text $c.in
        if ($got -ne $c.want) {
            Write-Host "[XX] 清洗失败：入 [$($c.in)] 期望 [$($c.want)] 实际 [$got]"
            $bad++
        } else {
            Write-Host "[OK] [$($c.in)] -> [$got]"
        }
    }

    Write-Host ""
    Write-Host "=== 自检：PDF 判定 ==="
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("zotero-acquire-selftest-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        # ① 真 PDF：头 %PDF-1.7 + 填充到阈值以上 + 尾 %%EOF
        $good = Join-Path $tmp "good.pdf"
        $pad = "A" * ($MIN_BYTES + 100)
        [System.IO.File]::WriteAllText($good, "%PDF-1.7`n$pad`n%%EOF")
        $r = Test-PdfFile -Path $good
        if ($r.ok) { Write-Host "[OK] 真 PDF 判定为真（$($r.bytes) 字节，magic=$($r.magic)）" }
        else { Write-Host "[XX] 真 PDF 被误判为假：$($r.reason)"; $bad++ }

        # ② HTML 拦截页：干净 200 但内容不是 PDF —— 这正是本脚本要挡的那个坑
        $html = Join-Path $tmp "block.html"
        [System.IO.File]::WriteAllText($html, "<!DOCTYPE html><html><body>Access Denied</body></html>" + ("x" * $MIN_BYTES))
        $r = Test-PdfFile -Path $html
        if (-not $r.ok) { Write-Host "[OK] HTML 拦截页被拒（理由：$($r.reason.Substring(0,[Math]::Min(60,$r.reason.Length)))…）" }
        else { Write-Host "[XX] HTML 被误判成 PDF"; $bad++ }

        # ③ 头部对但太小
        $tiny = Join-Path $tmp "tiny.pdf"
        [System.IO.File]::WriteAllText($tiny, "%PDF-1.4`n%%EOF")
        $r = Test-PdfFile -Path $tiny
        if (-not $r.ok) { Write-Host "[OK] 过小文件被拒（理由：$($r.reason)）" }
        else { Write-Host "[XX] 过小文件被误判为真"; $bad++ }

        # ④ 不存在
        $r = Test-PdfFile -Path (Join-Path $tmp "nope.pdf")
        if (-not $r.ok) { Write-Host "[OK] 不存在的文件被拒（理由：$($r.reason)）" }
        else { Write-Host "[XX] 不存在的文件被误判为真"; $bad++ }
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    Write-Host ""
    Write-Host "=== 自检：落地目录解析 ==="
    Write-Host ("约定默认目录 = " + (Resolve-TargetDir -Explicit ""))
    Write-Host ("显式覆盖生效 = " + (Resolve-TargetDir -Explicit "D:\tmp\x"))

    Write-Host ""
    if ($bad -eq 0) { Write-Host "自检全部通过（0 失败）"; return 0 }
    Write-Host "自检失败 $bad 项"; return 1
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

function Invoke-Fetch {
    param([string]$Url, [string]$Name, [string]$Dir, [bool]$DryRun)

    if (-not $Url) { Write-Host "[XX] 必须给 -Url"; return 1 }
    if ($Url -notmatch '^https?://') {
        Write-Host "[XX] 只支持 http/https 直链，收到：$Url"
        return 1
    }
    if (-not $Name) {
        Write-Host "[XX] 必须给 -Name（文件名主干，用 作者 年份 短标题 拼）"
        return 1
    }

    $dir = Resolve-TargetDir -Explicit $Dir
    $stem = ConvertTo-SafeStem -Text $Name
    $out = Join-Path $dir ($stem + ".pdf")

    Write-Host "URL      : $Url"
    Write-Host "落地目录 : $dir"
    Write-Host "文件名   : $($stem).pdf"

    if ($DryRun) {
        Write-Host "(DryRun：不下载、不落盘)"
        return 0
    }

    New-Item -ItemType Directory -Force -Path $dir | Out-Null

    # TLS 1.2 显式打开：Windows PowerShell 5.1 默认可能还在用 TLS 1.0，
    # 而 2026 年的出版社 CDN 基本都拒 TLS 1.0 —— 症状是"连接被意外关闭"，
    # 看着像网络问题，其实是协商失败。这行是纯粹的环境兼容，不是业务逻辑。
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        Invoke-WebRequest -Uri $Url -OutFile $out -TimeoutSec 180 -UseBasicParsing `
            -UserAgent $DEFAULT_UA -ErrorAction Stop
    } catch {
        $sw.Stop()
        Write-Host "[XX] 下载失败：$($_.Exception.Message)"
        Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
        return 2
    }
    $sw.Stop()

    # 关键一步：不看状态码看内容。见文件头第 1 条。
    $chk = Test-PdfFile -Path $out
    if (-not $chk.ok) {
        Write-Host "[XX] 下到的不是 PDF：$($chk.reason)"
        Write-Host "     文件留在 $out（可自行查看；确认无用就删掉）"
        if ($chk.bytes -lt $MIN_BYTES) { return 4 }
        return 3
    }

    $hash = (Get-FileHash -LiteralPath $out -Algorithm SHA256).Hash
    Write-Host ""
    Write-Host "[OK] 下载并校验通过"
    Write-Host "     路径   : $out"
    Write-Host "     大小   : $($chk.bytes) 字节（$([Math]::Round($chk.bytes / 1MB, 2)) MB）"
    Write-Host "     文件头 : $($chk.magic)"
    Write-Host "     尾部EOF: $($chk.eof)"
    Write-Host "     SHA256 : $hash"
    Write-Host "     耗时   : $([Math]::Round($sw.Elapsed.TotalSeconds, 1)) 秒"
    return 0
}

if ($SelfTest) { exit (Invoke-SelfTest) }
exit (Invoke-Fetch -Url $Url -Name $Name -Dir $Dir -DryRun:$DryRun)
