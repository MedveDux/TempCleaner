; Temp Cleaner — установщик (NSIS, без прав администратора).
; Собирается из packaging\build.bat; ожидает готовую сборку в packaging\dist\TempCleaner.
Unicode true
!define APPNAME "Temp Cleaner"
!define VERSION "1.4"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\TempCleaner"

Name "${APPNAME}"
OutFile "TempCleaner-Setup.exe"
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\TempCleaner"
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!define MUI_ICON "..\assets\icon.ico"
!define MUI_UNICON "..\assets\icon.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\TempCleaner.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Запустить Temp Cleaner"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"

Section "Установка"
  SetOutPath "$INSTDIR"
  File /r "dist\TempCleaner\*.*"
  CreateShortCut "$DESKTOP\Temp Cleaner.lnk" "$INSTDIR\TempCleaner.exe"
  CreateDirectory "$SMPROGRAMS\Temp Cleaner"
  CreateShortCut "$SMPROGRAMS\Temp Cleaner\Temp Cleaner.lnk" "$INSTDIR\TempCleaner.exe"
  CreateShortCut "$SMPROGRAMS\Temp Cleaner\Удалить Temp Cleaner.lnk" "$INSTDIR\Uninstall.exe"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\TempCleaner.exe"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  ; убираем задание автоочистки из Планировщика и запись в автозагрузке
  ExecWait 'schtasks /Delete /F /TN "TempCleanerAuto"'
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "TempCleaner"
  Delete "$DESKTOP\Temp Cleaner.lnk"
  RMDir /r "$SMPROGRAMS\Temp Cleaner"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTKEY}"
SectionEnd
