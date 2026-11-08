' BiG Equity 360 - start the tray app hidden (no console window). Same as BiG360.exe.
' Arguments are passed through, e.g.:  wscript BiG360.vbs --background
Option Explicit
Dim fso, sh, root, py, args, i
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
py = root & "\runtime\pythonw.exe"
If Not fso.FileExists(py) Then py = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then
    MsgBox "BiG Equity 360: Python runtime not found (runtime\pythonw.exe or .venv\Scripts\pythonw.exe) in " & root, 16, "BiG Equity 360"
    WScript.Quit 1
End If
args = ""
For i = 0 To WScript.Arguments.Count - 1
    args = args & " """ & WScript.Arguments(i) & """"
Next
sh.CurrentDirectory = root & "\backend"
sh.Environment("PROCESS")("BIG360_APP") = "1"
sh.Run """" & py & """ -m app.desktop.tray" & args, 0, False
