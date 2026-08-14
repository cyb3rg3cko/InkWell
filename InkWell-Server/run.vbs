' Launches the Inkwell server on Windows. Two modes, both driven by the
' settings block right below:
'
'   GUI mode (default) -- opens a small window for picking the
'   workspace directory, address, and port. Always serves HTTPS, so
'   this script makes sure a cert/key pair exists first, generating a
'   self-signed one if not.
'
'   Headless mode -- no window at all, fully configured by the
'   INKWELL_* variables below (or your system environment / a Docker
'   container, if you're not using this script for that). This is the
'   same mode `docker compose up` runs -- this script just lets you
'   run it directly on a Windows machine without Docker. Uncomment
'   INKWELL_HEADLESS below to switch into it.

Option Explicit

Dim shell, fso, envVars, scriptDir
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set envVars = shell.Environment("PROCESS")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir

' ---------------------------------------------------------------
' Core connection settings -- uncomment and fill in whichever you
' need. These have to be set before the TLS-certificate check further
' down, since INKWELL_HEADLESS/INKWELL_TLS change whether that check
' even applies -- that's why this block comes first, not grouped with
' the "optional features" block later in this file.
' ---------------------------------------------------------------

' envVars("INKWELL_HEADLESS") = "1"                    ' uncomment to skip the GUI entirely (see the mode explanation above)
' envVars("INKWELL_WORKSPACE_DIR") = ".\workspace"      ' headless mode defaults to /data, meant for Docker -- set this explicitly for a bare-metal run, or it'll likely fail with a permissions error
' envVars("INKWELL_HOST") = "0.0.0.0"                   ' default: 0.0.0.0 (every interface) -- set to "127.0.0.1" for loopback-only
' envVars("INKWELL_PORT") = "8060"                      ' default: 8060
' envVars("INKWELL_TLS") = "1"                          ' headless mode defaults to plain HTTP (0), expecting a reverse proxy in front -- set to "1" to have this process terminate real TLS itself instead
' envVars("INKWELL_TLS_CERT") = "cert.pem"              ' only read if INKWELL_TLS=1 -- defaults to cert.pem next to this script either way
' envVars("INKWELL_TLS_KEY") = "key.pem"                ' only read if INKWELL_TLS=1 -- defaults to key.pem next to this script either way

Dim pythonBin
pythonBin = FindPython(shell)
If pythonBin = "" Then
    WScript.Echo "Couldn't find python or py on your PATH. Install Python 3 and try again."
    WScript.Quit 1
End If

' GUI mode always needs a cert/key pair. Headless mode only needs one
' if INKWELL_TLS=1 was explicitly set above (its default is plain HTTP,
' expecting something else -- a reverse proxy -- to handle real TLS).
Dim needTlsCert
needTlsCert = False
If envVars("INKWELL_HEADLESS") = "" Then
    needTlsCert = True
ElseIf envVars("INKWELL_TLS") = "1" Then
    needTlsCert = True
End If

If needTlsCert Then
    Dim certPath, keyPath
    certPath = envVars("INKWELL_TLS_CERT")
    If certPath = "" Then certPath = "cert.pem"
    keyPath = envVars("INKWELL_TLS_KEY")
    If keyPath = "" Then keyPath = "key.pem"

    If (Not fso.FileExists(certPath)) Or (Not fso.FileExists(keyPath)) Then
        WScript.Echo "No " & certPath & " or " & keyPath & " found -- generating a self-signed certificate..."
        If Not CommandExists(shell, "openssl") Then
            WScript.Echo "openssl is required to generate one automatically. Install openssl (e.g. via Git for Windows), or supply your own cert/key at those paths."
            WScript.Quit 1
        End If
        Dim certCmd
        certCmd = "openssl req -x509 -newkey rsa:2048 -keyout """ & keyPath & """ -out """ & certPath & """ -days 365 -nodes -subj ""/CN=localhost"""
        shell.Run "cmd /c " & certCmd, 1, True
        WScript.Echo "Certificate generated (self-signed -- browsers will warn about it, that's expected)."
    End If
End If

' ---------------------------------------------------------------
' Optional features -- short-term way to configure these without the
' GUI having dedicated fields for them yet (GUI mode) or without
' reaching for Docker/your system environment (headless mode).
' Admin-password reset, at-rest encryption, and speech-to-text are all
' env-var-only under the hood regardless of GUI vs. headless. Uncomment
' and fill in whichever of these you want; this script's environment
' variables are inherited by the Python process it launches.
' See README.md for what each one does and the full list of
' speech-to-text provider options.
' ---------------------------------------------------------------

' envVars("INKWELL_ADMIN_PASSWORD") = "a long, random password -- this can reset ANY account's password"

' envVars("INKWELL_ENCRYPTION_KEY") = "a long, random value -- generate with: openssl rand -base64 32"

' envVars("INKWELL_STT_PROVIDER") = "local"    ' local | openai | groq | google | custom
' envVars("INKWELL_STT_LOCAL_MODEL") = "base"                    ' only used if STT_PROVIDER=local
' envVars("INKWELL_STT_API_KEY") = ""                            ' required for openai/groq/google; optional for custom; unused for local
' envVars("INKWELL_STT_URL") = "http://192.168.1.50:8000/v1"     ' only used if STT_PROVIDER=custom
' envVars("INKWELL_STT_MODEL_NAME") = "whisper-1"                ' only used if STT_PROVIDER=custom

shell.Run """" & pythonBin & """ Python_HTTPS_Server.py", 1, False

' -----------------------------------------------------------------
' Helper functions
' -----------------------------------------------------------------

Function FindPython(shellObj)
    Dim candidates, i
    candidates = Array("python", "py")
    For i = 0 To UBound(candidates)
        If CommandExists(shellObj, candidates(i)) Then
            FindPython = candidates(i)
            Exit Function
        End If
    Next
    FindPython = ""
End Function

Function CommandExists(shellObj, cmdName)
    Dim exec
    On Error Resume Next
    Set exec = shellObj.Exec("cmd /c where " & cmdName)
    If Err.Number <> 0 Then
        CommandExists = False
        Err.Clear
        Exit Function
    End If
    On Error Goto 0
    Do While exec.Status = 0
        WScript.Sleep 50
    Loop
    CommandExists = (exec.ExitCode = 0)
End Function
