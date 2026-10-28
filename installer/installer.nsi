; OpenDM Setup | NSIS script (MIT License)
Target amd64-unicode
SetCompressor /SOLID lzma
SetCompressorDictSize 64

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

!ifndef VERSION
  !define VERSION "1.2.0"
!endif
!define APPNAME "OpenDM"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\OpenDM"
!define RUNKEY "Software\Microsoft\Windows\CurrentVersion\Run"

Name "${APPNAME} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\OpenDM"
InstallDirRegKey HKCU "${UNINSTKEY}" "InstallLocation"
RequestExecutionLevel user
ManifestDPIAware true
BrandingText "OpenDM ${VERSION} | open source"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "OpenDM"
VIAddVersionKey "FileDescription" "OpenDM Setup"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "MIT License"

!define MUI_ICON "opendm.ico"
!define MUI_UNICON "opendm.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "wizard.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "wizard.bmp"
!define MUI_ABORTWARNING
!define MUI_COMPONENTSPAGE_SMALLDESC

!define MUI_WELCOMEPAGE_TITLE "Welcome to OpenDM Setup"
!define MUI_WELCOMEPAGE_TEXT "OpenDM is a free, open source download manager.$\r$\n$\r$\n\
- Download button on videos and pictures in your browser$\r$\n\
- Instagram, YouTube, Facebook, X, TikTok and 1000+ sites$\r$\n\
- Fast multi-connection downloads with pause and resume$\r$\n$\r$\n\
Everything it needs is included. No admin rights required.$\r$\n$\r$\n\
Click Next to continue."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${STAGE}\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "OpenDM is installed"
!define MUI_FINISHPAGE_TEXT "One more step: add the OpenDM extension to Chrome or Edge so the download button appears on videos and pictures. Leave the box below ticked and Setup will open a short guide."
!define MUI_FINISHPAGE_RUN "$INSTDIR\OpenDM.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start OpenDM now"
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Set up the browser extension now"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION OpenExtensionSetup
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ------------------------------------------------------------------ helpers

!macro QUIT_APP
  ; ask a running OpenDM to save its list and exit, so files are not locked
  nsExec::Exec `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "try { Invoke-RestMethod -Method Post -Uri http://127.0.0.1:9614/quit -Headers @{'X-OpenDM'='quit'} -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null; Start-Sleep -Milliseconds 1500 } catch {}"`
  Pop $0
!macroend

Function OpenExtensionSetup
  ExecShell "open" "$INSTDIR\extension-setup.html"
  Exec `"$WINDIR\explorer.exe" /select,"$INSTDIR\extension"`
  ; open the browser's extensions page directly when we can find the browser
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" ""
  ${If} $0 == ""
    ReadRegStr $0 HKLM "Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" ""
  ${EndIf}
  ${If} $0 != ""
  ${AndIf} ${FileExists} "$0"
    Exec `"$0" chrome://extensions/`
    Return
  ${EndIf}
  ReadRegStr $0 HKLM "Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe" ""
  ${If} $0 != ""
  ${AndIf} ${FileExists} "$0"
    Exec `"$0" edge://extensions/`
  ${EndIf}
FunctionEnd

; ------------------------------------------------------------------ sections

Section "OpenDM app (required)" SecMain
  SectionIn RO
  !insertmacro QUIT_APP
  ; replace the bundled runtime cleanly on upgrade
  RMDir /r "$INSTDIR\runtime"
  RMDir /r "$INSTDIR\app"

  SetOutPath "$INSTDIR"
  File "${STAGE}\OpenDM.exe"
  File "${STAGE}\opendm.ico"
  File "${STAGE}\extension-setup.html"
  File "${STAGE}\LICENSE"
  File "${STAGE}\README.md"
  SetOutPath "$INSTDIR\app"
  File /r "${STAGE}\app\*"
  SetOutPath "$INSTDIR\extension"
  File /r "${STAGE}\extension\*"
  SetOutPath "$INSTDIR\runtime"
  File /r "${STAGE}\runtime\*"
  SetOutPath "$INSTDIR"

  WriteUninstaller "$INSTDIR\Uninstall OpenDM.exe"

  CreateDirectory "$SMPROGRAMS\OpenDM"
  CreateShortcut "$SMPROGRAMS\OpenDM\OpenDM.lnk" "$INSTDIR\OpenDM.exe" "" "$INSTDIR\opendm.ico" 0
  CreateShortcut "$SMPROGRAMS\OpenDM\Browser extension setup.lnk" "$INSTDIR\extension-setup.html" "" "$INSTDIR\opendm.ico" 0
  CreateShortcut "$SMPROGRAMS\OpenDM\Uninstall OpenDM.lnk" "$INSTDIR\Uninstall OpenDM.exe"

  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "OpenDM"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "OpenDM (open source)"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\opendm.ico"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" `"$INSTDIR\Uninstall OpenDM.exe"`
  WriteRegStr HKCU "${UNINSTKEY}" "QuietUninstallString" `"$INSTDIR\Uninstall OpenDM.exe" /S`
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINSTKEY}" "EstimatedSize" "$0"
SectionEnd

Section "Desktop shortcut" SecDesktop
  CreateShortcut "$DESKTOP\OpenDM.lnk" "$INSTDIR\OpenDM.exe" "" "$INSTDIR\opendm.ico" 0
SectionEnd

Section "Start with Windows (runs in the tray)" SecAutostart
  WriteRegStr HKCU "${RUNKEY}" "OpenDM" `"$INSTDIR\OpenDM.exe" --tray`
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "The download manager, its browser extension files, and a private copy of Python, FFmpeg and Deno."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "Put an OpenDM icon on the desktop."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecAutostart} "Start OpenDM quietly in the tray when you sign in, so the browser button always works."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ------------------------------------------------------------------ uninstall

Section "Uninstall"
  !insertmacro QUIT_APP
  Delete "$DESKTOP\OpenDM.lnk"
  RMDir /r "$SMPROGRAMS\OpenDM"
  DeleteRegValue HKCU "${RUNKEY}" "OpenDM"
  DeleteRegKey HKCU "${UNINSTKEY}"

  RMDir /r "$INSTDIR\runtime"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\extension"
  Delete "$INSTDIR\OpenDM.exe"
  Delete "$INSTDIR\opendm.ico"
  Delete "$INSTDIR\extension-setup.html"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\Uninstall OpenDM.exe"
  RMDir "$INSTDIR"

  IfSilent done
  MessageBox MB_YESNO|MB_ICONQUESTION "Also delete your OpenDM settings and download list?$\r$\n(Your downloaded files are NOT touched either way.)" IDNO done
    RMDir /r "$PROFILE\.opendm"
  done:
  IfSilent +2
    MessageBox MB_OK "OpenDM was removed. To remove the browser extension, open chrome://extensions and click Remove on OpenDM."
SectionEnd
