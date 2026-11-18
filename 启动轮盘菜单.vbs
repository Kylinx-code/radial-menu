Option Explicit
Dim shell, fso, here, pyw, script, cmd, candidates, i, rc
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)

' Pick an interpreter that actually has Pillow + pywin32 installed.
' WorkBuddy's bundled Python may be reset (packages wiped), so probe first.
candidates = Array( _
  shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"), _
  shell.ExpandEnvironmentStrings("%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"), _
  "pythonw.exe")
pyw = "pythonw.exe"
For i = 0 To UBound(candidates)
  If fso.FileExists(candidates(i)) Or candidates(i) = "pythonw.exe" Then
    rc = shell.Run("""" & candidates(i) & """ -c ""import PIL,win32gui,pythoncom""", 0, True)
    If rc = 0 Then
      pyw = candidates(i)
      Exit For
    End If
  End If
Next

script = fso.BuildPath(here, "radial_click.py")
cmd = """" & pyw & """ """ & script & """"
shell.CurrentDirectory = here
shell.Run cmd, 0, False
