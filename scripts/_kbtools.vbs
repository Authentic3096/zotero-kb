' ============================================================
'  共享工具：给 0-panel.vbs / 4-service.vbs 用。
'
'  为什么单独一个文件：VBS 没有 import，以前是每个脚本各写一份，
'  改一处漏一处。现在用 ExecuteGlobal 加载本文件（见调用方开头）。
' ============================================================

' 读整个文本文件。文件可能是 UTF-8（Python 写的 json）或 ANSI（prefs.js）。
' VBS 的 OpenTextFile 只支持 Unicode(UTF-16) 和 ANSI，所以 UTF-8 得自己解。
Function KbReadAll(fso, path)
    Dim st, bytes, i, s, b, cp, b2, b3, n
    KbReadAll = ""
    If Not fso.FileExists(path) Then Exit Function
    On Error Resume Next
    Set st = CreateObject("ADODB.Stream")
    If Err.Number <> 0 Then
        ' 没有 ADODB 就退回纯 ANSI 读（prefs.js 是 ANSI，够用）
        Err.Clear
        Dim ts
        Set ts = fso.OpenTextFile(path, 1, False, 0)
        If Err.Number = 0 Then
            If Not ts.AtEndOfStream Then KbReadAll = ts.ReadAll
            ts.Close
        End If
        Err.Clear
        On Error Goto 0
        Exit Function
    End If
    st.Type = 2                  ' adTypeText
    st.Charset = "utf-8"
    st.Open
    st.LoadFromFile path
    s = st.ReadText(-1)          ' -1 = adReadAll
    st.Close
    If Err.Number <> 0 Then
        Err.Clear
        s = ""
    End If
    On Error Goto 0
    KbReadAll = s
End Function

' 从 JSON 文本里取一个字符串字段的值（不引入 JSON 解析器，够用就行）。
' 只处理 "key": "value" 这一种形态 —— 我们自己的配置文件就是这么写的。
Function KbJsonStr(txt, key)
    Dim p, c, q1, q2, v
    KbJsonStr = ""
    If txt = "" Then Exit Function
    p = InStr(1, txt, """" & key & """")
    If p = 0 Then Exit Function
    q1 = InStr(p + Len(key) + 2, txt, """")
    If q1 = 0 Then Exit Function
    q2 = InStr(q1 + 1, txt, """")
    If q2 <= q1 Then Exit Function
    v = Mid(txt, q1 + 1, q2 - q1 - 1)
    ' JSON 里的反斜杠是转义的（\\ 表示一个 \），还原
    v = Replace(v, "\\", "\")
    v = Replace(v, "\/", "/")
    KbJsonStr = v
End Function

' 决定知识库目录在哪。顺序与 Python 侧 schemas._resolve_kb_dir 一致：
'   1. 环境变量 KB_DIR
'   2. <项目>\kb-location.json
'   3. %APPDATA%\zotero-kb\location.json   （项目被挪走时的后备）
'   4. <Zotero 数据目录>\zotero-kb          （从 prefs.js 读 dataDir）
'   5. <项目>\kb                            （老位置，仅兜底）
' 返回空串表示都没找到，调用方自己兜底。
Function KbFindDir(fso, root)
    Dim sh, v, cands, i, txt, profBase, pf, prof, zdir
    KbFindDir = ""
    Set sh = CreateObject("WScript.Shell")

    ' 1) 环境变量
    On Error Resume Next
    v = sh.ExpandEnvironmentStrings("%KB_DIR%")
    On Error Goto 0
    If v <> "" And v <> "%KB_DIR%" And fso.FolderExists(v) Then
        KbFindDir = v
        Exit Function
    End If

    ' 2)(3) 两个 location.json
    cands = Array(root & "\kb-location.json", _
                  sh.ExpandEnvironmentStrings("%APPDATA%") & "\zotero-kb\location.json")
    For i = 0 To UBound(cands)
        txt = KbReadAll(fso, cands(i))
        If txt <> "" Then
            v = KbJsonStr(txt, "kb_dir")
            If v <> "" And fso.FolderExists(v) Then
                KbFindDir = v
                Exit Function
            End If
        End If
    Next

    ' 4) Zotero 数据目录
    profBase = sh.ExpandEnvironmentStrings("%APPDATA%") & "\Zotero\Zotero\Profiles"
    If fso.FolderExists(profBase) Then
        Set pf = fso.GetFolder(profBase).SubFolders
        For Each prof In pf
            txt = KbReadAll(fso, prof.Path & "\prefs.js")
            If txt <> "" Then
                zdir = KbJsonStr(txt, "extensions.zotero.dataDir")
                If zdir <> "" And fso.FolderExists(zdir & "\zotero-kb") Then
                    KbFindDir = zdir & "\zotero-kb"
                    Exit Function
                End If
            End If
        Next
    End If

    ' 5) 老位置
    If fso.FolderExists(root & "\kb") Then KbFindDir = root & "\kb"
End Function

' 日志目录：<知识库>\logs。知识库找不到时退到 <项目>\logs
' （**不要**退到 <项目>\kb\logs —— 那会凭空造一个假的 kb 目录出来）。
Function KbLogDir(fso, root)
    Dim d
    d = KbFindDir(fso, root)
    If d = "" Then d = root
    KbLogDir = d & "\logs"
End Function

' 确保目录存在（含父级）
Sub KbEnsureDir(fso, path)
    On Error Resume Next
    If Not fso.FolderExists(path) Then fso.CreateFolder path
    On Error Goto 0
End Sub
