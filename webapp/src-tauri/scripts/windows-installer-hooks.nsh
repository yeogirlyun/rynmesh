; Small optional component selection. Editor ZIP/WASM files are never bundled.
; This records a preference only: the app reviews and downloads after installation.
!include "nsDialogs.nsh"
!include "LogicLib.nsh"
Var CraftDialog
Var CraftPdf
Var CraftWord
Var CraftExcel
Var CraftPpt
Var CraftPdfState
Var CraftWordState
Var CraftExcelState
Var CraftPptState
Var CraftSelectionShown
Var CraftSelected

; Tauri includes hooks before MUI_LANGUAGE defines its language constants.
LangString CraftTitle 1033 "Choose your tools"
LangString CraftTitle 2052 "选择常用工具"
LangString CraftHelp 1033 "Free optional plugins. After installation, review and download them in Apps. They are not included in this installer."
LangString CraftHelp 2052 "可选插件免费使用。安装完成后，在应用页确认并下载，不包含在主安装包中。"
LangString CraftMore 1033 "More tools can be added or removed later. Agent uses enabled plugins by default."
LangString CraftMore 2052 "其他工具可以稍后添加或卸载。Agent 默认可使用已启用插件。"

Page custom CraftChooseTools CraftLeaveTools
Function CraftChooseTools
  IfSilent 0 +2
    Abort
  ; An upgrade keeps previous choices and never queues another download.
  ClearErrors
  ReadRegStr $0 HKCU "Software\Ryn\Craft" "SelectedPlugins"
  IfErrors +2 0
    Abort
  nsDialogs::Create 1018
  Pop $CraftDialog
  StrCmp $CraftDialog error 0 +2
    Abort
  !insertmacro MUI_HEADER_TEXT "$(CraftTitle)" "Ryn"
  ${NSD_CreateLabel} 0 0 100% 35u "$(CraftHelp)"
  Pop $0
  ${NSD_CreateCheckbox} 8u 45u 90% 14u "PDF"
  Pop $CraftPdf
  ${NSD_Check} $CraftPdf
  ${NSD_CreateCheckbox} 8u 67u 90% 14u "Word"
  Pop $CraftWord
  ${NSD_Check} $CraftWord
  ${NSD_CreateCheckbox} 8u 89u 90% 14u "Excel"
  Pop $CraftExcel
  ${NSD_Check} $CraftExcel
  ${NSD_CreateCheckbox} 8u 111u 90% 14u "PPT"
  Pop $CraftPpt
  ${NSD_Check} $CraftPpt
  ${If} $CraftSelectionShown == 1
    ${NSD_SetState} $CraftPdf $CraftPdfState
    ${NSD_SetState} $CraftWord $CraftWordState
    ${NSD_SetState} $CraftExcel $CraftExcelState
    ${NSD_SetState} $CraftPpt $CraftPptState
  ${EndIf}
  ${NSD_CreateLabel} 0 140u 100% 35u "$(CraftMore)"
  Pop $0
  StrCpy $CraftSelectionShown 1
  nsDialogs::Show
FunctionEnd

Function CraftLeaveTools
  StrCpy $CraftSelected ""
  ${NSD_GetState} $CraftPdf $0
  StrCpy $CraftPdfState $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $CraftSelected "pdfcraft;"
  ${EndIf}
  ${NSD_GetState} $CraftWord $0
  StrCpy $CraftWordState $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $CraftSelected "$CraftSelectedwordcraft;"
  ${EndIf}
  ${NSD_GetState} $CraftExcel $0
  StrCpy $CraftExcelState $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $CraftSelected "$CraftSelectedgridcraft;"
  ${EndIf}
  ${NSD_GetState} $CraftPpt $0
  StrCpy $CraftPptState $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $CraftSelected "$CraftSelecteddeckcraft;"
  ${EndIf}
  StrLen $0 $CraftSelected
  ${If} $0 > 0
    StrCpy $CraftSelected $CraftSelected -1
  ${EndIf}
FunctionEnd

!macro NSIS_HOOK_POSTINSTALL
  ${If} $CraftSelectionShown == 1
    WriteRegStr HKCU "Software\Ryn\Craft" "SelectedPlugins" "$CraftSelected"
  ${EndIf}
!macroend

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
