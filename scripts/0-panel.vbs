' 双击启动知识库管理面板（不弹控制台黑窗口）。
'
' 为什么用 VBS：pythonw.exe 启动虽然也没窗口，但出错时什么都看不到；
' 这里先把 Python 的报错重定向到日志文件，再用 pythonw 起，兼顾"干净"和"可排查"。
Option Explicit

Dim fso, shell, root, pyw, py, gui, logDir, logFile, cmd, already
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))

' 加载共享工具（KbFindDir / KbLogDir / KbReadAll ...）
Dim libPath
libPath = root & "\scripts\_kbtools.vbs"
If Not fso.FileExists(libPath) Then
    MsgBox "缺文件：" & libPath, 16, "知识库管理面板"
    WScript.Quit 1
End If
ExecuteGlobal KbReadAllFile(libPath)

pyw = root & "\.venv\Scripts\pythonw.exe"
py = root & "\.venv\Scripts\python.exe"
gui = root & "\tools\gui.py"
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
               "     点「浏览…」指定 python.exe 的完整路径", 16, "知识库管理面板"
        WScript.Quit 1
    End If
End If

logFile = logDir & "\gui-error.log"

' 已在运行就不再开一个（用 WMI 查命令行，比 tasklist 精确）
already = False
On Error Resume Next
Dim wmi, procs, p
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT CommandLine FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'")
For Each p In procs
    If Not IsNull(p.CommandLine) Then
        If InStr(p.CommandLine, "tools\gui.py") > 0 Then already = True
    End If
Next
On Error Goto 0

If already Then
    MsgBox "管理面板已经在运行了（请看任务栏）。", 64, "知识库管理面板"
    WScript.Quit 0
End If

cmd = "cmd /c """"" & pyw & """ """ & gui & """ > """ & logFile & """ 2>&1"""
shell.Run cmd, 0, False

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
