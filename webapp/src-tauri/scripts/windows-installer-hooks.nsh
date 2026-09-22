; Remove only a startup entry owned by the installation being removed.
!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $UpdateMode <> 1
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Ryn"
  StrCmp $0 '$\"$INSTDIR\Ryn.exe$\" --background' 0 +2
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Ryn"
  StrCmp $0 '$\"$INSTDIR\Ryn.exe$\"' 0 +2
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Ryn"
  ${EndIf}
!macroend
