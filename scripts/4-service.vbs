' 后台启动知识库本地服务（给 Zotero 插件用）。
'
' 设计要点（踩过的坑）：
'   · **不要用 MsgBox 报"已启动"** —— 那会阻塞脚本，看起来像启动失败
'     （实测：双击后服务确实起来了，但脚本卡在弹窗上，用户以为没成功）。
'     状态请到管理面板的「服务状态」看，或直接访问 /health。
'   · 直接跑 pythonw.exe（不经 cmd），由 Python 自己写日志到
'     <知识库>\logs\localserver.log —— pythonw 没有控制台，靠 cmd 重定向拿不到输出。
'   · **日志目录不能写死成 <项目>\kb\logs**：知识库默认跟着 Zotero 数据目录走，
'     用户还能在设置里改到别处。用 KbLogDir() 现算（见 _kbtools.vbs）。
Option Explicit

Dim fso, shell, root, pyw, py, script, logDir, already
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))

Dim libPath
libPath = root & "\scripts\_kbtools.vbs"
If Not fso.FileExists(libPath) Then
    MsgBox "缺文件：" & libPath, 16, "知识库本地服务"
    WScript.Quit 1
End If
ExecuteGlobal KbReadAllFile(libPath)

pyw = root & "\.venv\Scripts\pythonw.exe"
py = root & "\.venv\Scripts\python.exe"
script = root & "\online\localserver.py"
logDir = KbLogDir(fso, root)
KbEnsureDir fso, logDir

If Not fso.FileExists(pyw) Then
    If fso.FileExists(py) Then
        pyw = py
    Else
        MsgBox "找不到 Python 环境：" & vbCrLf & pyw & vbCrLf & vbCrLf & _
               "两种办法：" & vbCrLf & _
               "  1) 看 README 的「安装」一节，建一次 .venv" & vbCrLf & _
               "  2) 在 Zotero 里：设置 → 文献知识库 → 运行环境，" & vbCrLf & _
               "     点「浏览…」指定 python.exe 的完整路径", 16, "知识库本地服务"
        WScript.Quit 1
    End If
End If

' 已在运行就不再启一个（否则端口冲突，第二个进程会静默退出）
already = False
On Error Resume Next
Dim wmi, procs, p
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT CommandLine FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'")
For Each p In procs
    If Not IsNull(p.CommandLine) Then
        If InStr(p.CommandLine, "localserver.py") > 0 Then already = True
    End If
Next
On Error Goto 0

If Not already Then
    shell.CurrentDirectory = root
    shell.Run """" & pyw & """ """ & script & """", 0, False
End If

' ---- 本地辅助：读文件（加载共享库本身要用，不能放在共享库里）----
Function KbReadAllFile(path)
    Dim st
    On Error Resume Next
    Set st = CreateObject("ADODB.Stream")
    st.Type = 2
    st.Charset = "utf-8"
    st.Open
    st.LoadFromFile path
    KbReadAllFile = st.ReadText(-1)
    st.Close
    If Err.Number <> 0 Then
        KbReadAllFile = ""
        Err.Clear
    End If
    On Error Goto 0
End Function
