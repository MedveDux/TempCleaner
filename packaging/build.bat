@echo off
rem ==========================================================================
rem  Сборка TempCleaner.exe и установщика TempCleaner-Setup.exe.
rem  Запускать на Windows. Требуется:
rem    - Python 3.8+ с python.org (в PATH как "py" или "python");
rem    - NSIS (makensis в PATH) — https://nsis.sourceforge.io/
rem  Результат: packaging\TempCleaner-Setup.exe
rem ==========================================================================
setlocal
cd /d "%~dp0"

rem PyInstaller собирает из .py — делаем временную копию из .pyw
copy /Y "..\temp_cleaner.pyw" "temp_cleaner.py" >nul

echo [1/3] Установка/обновление PyInstaller...
py -m pip install --upgrade pyinstaller || python -m pip install --upgrade pyinstaller || goto :err

echo [2/3] Сборка exe...
py -m PyInstaller --noconfirm --clean --windowed ^
   --name TempCleaner --icon "..\assets\icon.ico" ^
   "temp_cleaner.py" || python -m PyInstaller --noconfirm --clean --windowed ^
   --name TempCleaner --icon "..\assets\icon.ico" "temp_cleaner.py" || goto :err

rem не тащим в установщик рабочие файлы, если вдруг создались при отладке
del /Q "dist\TempCleaner\temp_cleaner_config.json" 2>nul
del /Q "dist\TempCleaner\temp_cleaner_log.txt" 2>nul

echo [3/3] Сборка установщика (NSIS)...
makensis installer.nsi || goto :err

del /Q "temp_cleaner.py" 2>nul
echo.
echo Готово: "%~dp0TempCleaner-Setup.exe"
pause
exit /b 0

:err
echo.
echo ОШИБКА сборки. Проверьте, что установлены Python и NSIS и они видны в PATH.
pause
exit /b 1
