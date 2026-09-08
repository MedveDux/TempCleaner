# Сборка exe и установщика

Сборка выполняется на **Windows**. В результате получается
`packaging\TempCleaner-Setup.exe`, который загружается в GitHub Releases.

## Требования

- **Python 3.8+** с [python.org](https://www.python.org/downloads/)
  (модуль `tkinter` входит в стандартную установку). При установке
  отметьте «Add python.exe to PATH».
- **NSIS** — [nsis.sourceforge.io](https://nsis.sourceforge.io/). После установки
  добавьте папку NSIS в `PATH`, чтобы команда `makensis` работала из консоли
  (обычно `C:\Program Files (x86)\NSIS`).

PyInstaller ставить вручную не нужно — `build.bat` устанавливает его сам.

## Быстрый способ

Двойной клик по `packaging\build.bat` (или запуск из консоли). Скрипт:

1. делает временную копию `temp_cleaner.pyw` → `temp_cleaner.py`;
2. устанавливает/обновляет PyInstaller;
3. собирает `packaging\dist\TempCleaner\TempCleaner.exe` (режим *onedir*, без консоли, с иконкой);
4. собирает `packaging\TempCleaner-Setup.exe` установщиком NSIS.

## Вручную (по шагам)

```bat
cd packaging
copy /Y ..\temp_cleaner.pyw temp_cleaner.py
py -m pip install --upgrade pyinstaller
py -m PyInstaller --noconfirm --clean --windowed --name TempCleaner --icon ..\assets\icon.ico temp_cleaner.py
makensis installer.nsi
```

## Обновление версии

Номер версии установщика задаётся в `installer.nsi`:

```nsi
!define VERSION "1.4"
```

При выпуске новой версии обновите его здесь и добавьте запись в
[`../CHANGELOG.md`](../CHANGELOG.md).

## Заметки

- Сборка *onedir* (папка `TempCleaner` с `.exe` и `_internal`) стартует
  быстрее, чем один большой `.exe`, а установщик всё равно упаковывает всё
  в один `TempCleaner-Setup.exe`.
- Программа не подписана сертификатом, поэтому у пользователей возможно
  предупреждение SmartScreen. Это ожидаемо; подпись — отдельная платная
  процедура и в этом проекте не используется.
- Артефакты сборки (`dist/`, `build/`, `temp_cleaner.py`, `TempCleaner-Setup.exe`)
  добавлены в `.gitignore` и в репозиторий не попадают.
