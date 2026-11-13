' Create desktop shortcut. Pure ASCII source: the install path is passed as an
' argument so Chinese folder names never go through batch/VBS codepage conversion.
' Usage: cscript //nologo 创建快捷方式.vbs "<install-dir-with-trailing-backslash>"
Option Explicit
Dim shell, link, installDir, desktop, lnkPath, fso
If WScript.Arguments.Count < 1 Then WScript.Quit 1
installDir = WScript.Arguments(0)
If Right(installDir, 1) <> "\" Then installDir = installDir & "\"

Set fso = CreateObject("Scripting.FileSystemObject")
If Not fso.FileExists(installDir & "runtime\pythonw.exe") Then WScript.Quit 2

Set shell = CreateObject("WScript.Shell")
desktop = shell.SpecialFolders("Desktop")
' 微信辅助器.lnk  (built from code points to keep this file pure ASCII)
lnkPath = desktop & "\" & ChrW(&H5FAE) & ChrW(&H4FE1) & ChrW(&H8F85) _
          & ChrW(&H52A9) & ChrW(&H5668) & ".lnk"
Set link = shell.CreateShortcut(lnkPath)
link.TargetPath = installDir & "runtime\pythonw.exe"
link.Arguments = """" & installDir & "app.py" & """"
link.WorkingDirectory = installDir
link.IconLocation = installDir & "assets\app.ico"
link.Description = "WeChat Assistant"
link.Save
WScript.Quit 0
