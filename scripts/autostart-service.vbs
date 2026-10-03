 ' 开机自启：拉起知识库本地服务（给 Zotero 插件用）。
'
' 为什么要这个文件：服务是后台进程，Zotero / DSH 重启时并不会带上它，
' 本机实测它掉过好几次 —— 表现为插件里"分类建议失败""发送失败"，
' 但其实只是服务没在跑。放进启动文件夹后就不用手动拉了。
'
' ⚠ 用**绝对路径**调真正的启动脚本（scripts\4-service.vbs），
'   不要把那个脚本直接复制到启动文件夹：它按"自己在哪"推算项目根目录，
'   换位置后 root 就算错了、找不到 .venv。
'
' ⚠ 这里的 target **不能写死**：本文件会被 scripts\install-autostart.ps1
'   按本机实际路径重新生成一份放进启动文件夹（这样项目挪了也只需重装一次）。
'   直接双击本文件不会有效果 —— 它是给安装脚本当模板用的。
Option Explicit

Dim shell, target, fso, here, root
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' 本文件在 <项目>\scripts\ 下 → 往上一级就是项目根（同一个推导逻辑，
' 和 4-service.vbs 保持一致，避免两处规则不一样）
here = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(here)
target = fso.BuildPath(root, "scripts\4-service.vbs")

If Not fso.FileExists(target) Then
    ' 找不到就静默退出（开机时弹窗会很烦）。装到启动文件夹的那份
    ' 由 install-autostart.ps1 生成，里面是当时算好的绝对路径。
    WScript.Quit 1
End If

On Error Resume Next
shell.Run """" & target & """", 0, False
On Error Goto 0
