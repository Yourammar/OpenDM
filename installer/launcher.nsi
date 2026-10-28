; OpenDM.exe | tiny launcher that starts the bundled Python runtime without a console window
Target amd64-unicode
!include "FileFunc.nsh"

!ifndef VERSION
  !define VERSION "1.2.0"
!endif

Name "OpenDM"
OutFile "${OUTFILE}"
Icon "opendm.ico"
SilentInstall silent
RequestExecutionLevel user
ManifestDPIAware true

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "OpenDM"
VIAddVersionKey "FileDescription" "OpenDM Download Manager"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "MIT License"
VIAddVersionKey "OriginalFilename" "OpenDM.exe"

Section
  ${GetParameters} $0
  IfFileExists "$EXEDIR\runtime\pythonw.exe" +3
    MessageBox MB_ICONSTOP "OpenDM files are missing. Please reinstall OpenDM."
    Quit
  ; lets the app point "Start with Windows" at this exe
  System::Call 'kernel32::SetEnvironmentVariable(t "OPENDM_LAUNCHER", t "$EXEPATH")'
  ; -E -s: ignore PYTHON* variables and user site-packages from any other Python on this PC
  Exec '"$EXEDIR\runtime\pythonw.exe" -E -s "$EXEDIR\app\OpenDM.pyw" $0'
SectionEnd
