#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Temp Cleaner — аккуратная очистка папки временных файлов Windows.

Кнопки:
  «Очистить сейчас»                — почистить %TEMP% немедленно;
  «Раз в 7 дней» / «Раз в 30 дней» — автоочистка: задание Планировщика
      (ежедневно в 12:00) плюс запись в автозагрузке, которая при каждом
      входе в Windows проверяет, не пора ли чистить, и сразу завершается.
      Оба механизма зовут «--clean-if-due»: очистка идёт, только если
      с последней прошло больше выбранного срока. Приложение при этом
      не висит в памяти — его запускает сама система и оно закрывается;
  «Выкл»                           — отключить автоочистку (задание и
      запись в автозагрузке удаляются).

Правила безопасности (собраны по рекомендациям из интернета):
  • удаляется только СОДЕРЖИМОЕ папки Temp — сама папка остаётся на месте;
  • файлы моложе 24 часов не трогаются (могут быть нужны установщикам и
    работающим программам);
  • файлы, занятые программами, пропускаются — ничего не удаляется «силой»;
  • исключения не удаляются никогда: список задаётся прямо в приложении
    (по умолчанию claude*, anthropic*) и хранится в temp_cleaner_config.json,
    поэтому действует и при автоочистке по расписанию;
  • приложение отказывается чистить папку, если в её пути нет папки
    с именем Temp/Tmp, если это корень диска или домашняя папка;
  • внутрь символьных ссылок и junction-папок скрипт не заходит.

Тихий запуск без окна (его использует Планировщик):
  pythonw temp_cleaner.pyw --clean
"""

import fnmatch
import json
import os
import queue
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

# ============================= НАСТРОЙКИ =============================

APP_NAME = "Temp Cleaner"
TASK_NAME = "TempCleanerAuto"          # имя задания в Планировщике Windows

# Не трогать файлы моложе стольких часов (можно переопределить переменной
# окружения TEMP_CLEANER_MIN_AGE_HOURS — используется для тестов).
# Отрицательные значения запрещены: они сдвинули бы отсечку в будущее
# и удаляли бы только что созданные файлы работающих программ.
try:
    MIN_AGE_HOURS = max(0, int(os.environ.get("TEMP_CLEANER_MIN_AGE_HOURS", "24")))
except ValueError:
    MIN_AGE_HOURS = 24

# Исключения по умолчанию (маски имён, * — любые символы, регистр не важен).
# Рабочий список хранится в конфиге и редактируется прямо в приложении.
DEFAULT_EXCLUDE = (
    "claude*",
    "anthropic*",
)
MAX_PATTERN_LEN = 80

# =====================================================================


def temp_root() -> str:
    """Папка Temp ТЕКУЩЕГО пользователя (у каждого своя, берётся из %TEMP%).
    Короткое имя вида MIKHAI~1 разворачивается в полное."""
    p = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    try:
        if p and os.path.isdir(p):
            return os.path.realpath(p)
    except OSError:
        pass
    return p


def check_root(path: str):
    """Возвращает текст ошибки или None, если папку чистить безопасно.

    Правила: путь должен существовать, не быть корнем диска и не быть
    домашней папкой, а среди составляющих пути должна быть папка с именем
    ровно «Temp» или «Tmp» (регистр не важен). Символьные ссылки
    раскрываются до реального пути ДО проверки.
    """
    if not path or not os.path.isdir(path):
        return "Папка Temp не найдена"
    real = os.path.realpath(path)
    _drive, tail = os.path.splitdrive(real)
    parts = [p for p in tail.replace("/", "\\").split("\\") if p]
    if not parts:
        return "Отказ: получен корень диска"
    if not any(p.lower() in ("temp", "tmp") for p in parts):
        return "Отказ: в пути нет папки с именем Temp"
    home = os.path.realpath(os.path.expanduser("~")).rstrip("\\/").lower()
    if real.rstrip("\\/").lower() == home:
        return "Отказ: это домашняя папка"
    return None


def _is_reparse(st) -> bool:
    attr = getattr(st, "st_file_attributes", 0)
    return bool(attr & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _excluded(name: str, patterns_low) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, p) for p in patterns_low)


def _clean_dir(path: str, cutoff: float, s: dict, dry: bool, patterns_low) -> bool:
    """Чистит папку. Возвращает True, если после очистки она останется пустой."""
    empty = True
    try:
        entries = list(os.scandir(path))
    except OSError:
        s["locked"] += 1
        return False

    for e in entries:
        if _excluded(e.name, patterns_low):
            s["excluded"] += 1
            empty = False
            continue
        try:
            st_ = e.stat(follow_symlinks=False)
        except OSError:
            s["locked"] += 1
            empty = False
            continue

        try:
            is_dir = e.is_dir(follow_symlinks=False)
            is_link = e.is_symlink() or _is_reparse(st_)
        except OSError:
            is_dir, is_link = False, False

        # Символьная ссылка / junction: удаляем только саму ссылку,
        # внутрь не заходим, чтобы не почистить чужую папку.
        if is_link:
            if st_.st_ctime < cutoff:
                if dry:
                    s["files"] += 1
                else:
                    try:
                        (os.rmdir if is_dir else os.unlink)(e.path)
                        s["files"] += 1
                    except FileNotFoundError:
                        pass  # уже исчезла сама — не считаем ошибкой
                    except OSError:
                        s["locked"] += 1
                        empty = False
            else:
                s["fresh"] += 1
                empty = False
            continue

        if is_dir:
            # Перепроверяем прямо перед спуском, что это всё ещё обычная
            # папка, а не подменённая в последний момент ссылка (защита от
            # гонки: внутрь ссылок не спускаемся никогда).
            try:
                st2 = os.lstat(e.path)
            except FileNotFoundError:
                continue  # папка уже исчезла
            except OSError:
                s["locked"] += 1
                empty = False
                continue
            if (not stat.S_ISDIR(st2.st_mode)) or (
                    getattr(st2, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                s["fresh"] += 1
                empty = False
                continue
            sub_empty = _clean_dir(e.path, cutoff, s, dry, patterns_low)
            # папку удаляем, только если она опустела и сама достаточно старая
            if sub_empty and st_.st_ctime < cutoff:
                if dry:
                    s["dirs"] += 1
                else:
                    try:
                        os.rmdir(e.path)
                        s["dirs"] += 1
                    except FileNotFoundError:
                        pass  # уже исчезла — не считаем ошибкой
                    except OSError:
                        s["locked"] += 1
                        empty = False
            else:
                empty = False
            continue

        # обычный файл: свежие не трогаем
        if st_.st_mtime >= cutoff or st_.st_ctime >= cutoff:
            s["fresh"] += 1
            empty = False
            continue

        size = st_.st_size
        if dry:
            s["files"] += 1
            s["bytes"] += size
            continue
        try:
            os.remove(e.path)
            s["files"] += 1
            s["bytes"] += size
        except FileNotFoundError:
            pass  # файл уже исчез сам — не считаем ошибкой
        except PermissionError:
            # снимаем "только чтение" и пробуем ещё раз;
            # занятые программами файлы всё равно не удалятся — и хорошо
            try:
                os.chmod(e.path, stat.S_IWRITE)
                os.remove(e.path)
                s["files"] += 1
                s["bytes"] += size
            except OSError:
                s["locked"] += 1
                empty = False
        except OSError:
            s["locked"] += 1
            empty = False
    return empty


def run_clean(dry: bool = False, patterns=None) -> dict:
    root = temp_root()
    err = check_root(root)
    if err:
        return {"error": err, "root": root or "?"}
    if patterns is None:
        patterns = get_excludes()
        if patterns is None:
            # файл настроек есть, но прочитать его не удалось: лучше НЕ чистить,
            # чем почистить без пользовательских исключений
            return {"error": "файл настроек повреждён или недоступен — "
                             "очистка отменена, чтобы не задеть исключения",
                    "root": root}
    s = {"files": 0, "dirs": 0, "bytes": 0, "locked": 0, "fresh": 0,
         "excluded": 0, "root": root}
    cutoff = time.time() - MIN_AGE_HOURS * 3600
    patterns_low = [p.lower() for p in patterns]
    old_limit = sys.getrecursionlimit()
    try:
        if sys.version_info >= (3, 11):
            # на 3.11+ глубокая рекурсия безопасна (кадры в куче),
            # на старых версиях оставляем стандартный предел
            sys.setrecursionlimit(max(old_limit, 8000))
        _clean_dir(root, cutoff, s, dry, patterns_low)
    except (RecursionError, MemoryError):
        s["aborted"] = True  # прервано на сверхглубокой вложенности
    finally:
        sys.setrecursionlimit(old_limit)
    return s


# ========================= КОНФИГ И ЖУРНАЛ ==========================


_CFG_LOCK = threading.RLock()   # защищает читай-меняй-сохраняй от гонок потоков


def app_dir() -> Path:
    """Папка приложения: рядом с exe (сборка PyInstaller) или со скриптом.
    ВАЖНО для exe: __file__ там указывает во временную распаковку,
    писать настройки туда нельзя."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _probe_writable(d: Path) -> bool:
    """Настоящая проверка записи: os.access на Windows не учитывает права."""
    probe = d / (".tc_probe_%d.tmp" % os.getpid())
    try:
        with open(probe, "w") as f:
            f.write("1")
        os.remove(probe)
        return True
    except OSError:
        return False


def _config_dir() -> Path:
    d = app_dir()
    if _probe_writable(d):
        return d
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    d2 = base / "TempCleaner"
    try:
        d2.mkdir(parents=True, exist_ok=True)
    except OSError:
        return d
    if _probe_writable(d2):
        return d2
    # некуда писать — save_cfg честно вернёт False, интерфейс предупредит
    return d


CONFIG_FILE = _config_dir() / "temp_cleaner_config.json"
LOG_FILE = _config_dir() / "temp_cleaner_log.txt"


def read_cfg():
    """dict — конфиг прочитан; {} — конфига ещё нет; None — файл есть,
    но прочитать/разобрать не удалось (повреждён или занят)."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None


def load_cfg() -> dict:
    c = read_cfg()
    return c if isinstance(c, dict) else {}


def save_cfg(cfg: dict) -> bool:
    """Атомарная запись (временный файл + замена). Возвращает успех, чтобы
    интерфейс мог честно предупредить, а не молча потерять настройки."""
    tmp = CONFIG_FILE.with_name("%s.%d.tmp" % (CONFIG_FILE.name, os.getpid()))
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        for _ in range(3):   # os.replace может на миг упереться в антивирус
            try:
                os.replace(tmp, CONFIG_FILE)
                return True
            except OSError:
                time.sleep(0.1)
        return False
    except OSError:
        return False
    finally:
        try:
            if tmp.exists():
                os.remove(tmp)
        except OSError:
            pass


def _sanitize_patterns(items) -> list:
    out, seen = [], set()
    for p in items if isinstance(items, list) else []:
        if not isinstance(p, str):
            continue
        p = p.strip()
        # слишком длинную маску НЕ обрезаем: усечённая маска могла бы
        # перестать защищать то, что защищала полная
        if not p or len(p) > MAX_PATTERN_LEN:
            continue
        if p.lower() in seen:
            continue
        seen.add(p.lower())
        out.append(p)
    return out


def get_excludes():
    """Действующий список исключений; None — конфиг повреждён/недоступен
    (в этом случае чистить нельзя)."""
    cfg = read_cfg()
    if cfg is None:
        return None
    ex = cfg.get("exclude")
    if not isinstance(ex, list):
        return list(DEFAULT_EXCLUDE)
    return _sanitize_patterns(ex)


def _safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def append_log(s: dict) -> None:
    line = "{}  удалено: файлов {}, папок {}, {}; пропущено занятых {}\n".format(
        datetime.now().strftime("%d.%m.%Y %H:%M"),
        s["files"], s["dirs"], fmt_size(s["bytes"]), s["locked"])
    try:
        with _CFG_LOCK:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
            if LOG_FILE.stat().st_size > 262144:   # журнал не разрастается
                txt = LOG_FILE.read_text(encoding="utf-8", errors="replace")
                LOG_FILE.write_text(
                    "\n".join(txt.splitlines()[-200:]) + "\n",
                    encoding="utf-8")
    except OSError:
        pass


def remember_run(s: dict) -> dict:
    with _CFG_LOCK:
        cfg = load_cfg()
        cfg["last_run"] = time.time()
        cfg["last_items"] = s["files"] + s["dirs"]
        cfg["last_bytes"] = s["bytes"]
        save_cfg(cfg)
    append_log(s)
    return cfg


# =========================== ФОРМАТИРОВАНИЕ =========================


def fmt_size(b: float) -> str:
    b = float(b)
    if b >= 1 << 30:
        v, u = b / (1 << 30), "ГБ"
    elif b >= 1 << 20:
        v, u = b / (1 << 20), "МБ"
    elif b >= 1 << 10:
        v, u = b / (1 << 10), "КБ"
    else:
        return "%d Б" % int(b)
    txt = ("%.1f" % v).replace(".", ",")
    if txt.endswith(",0"):
        txt = txt[:-2]
    return "%s %s" % (txt, u)


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    d = n % 10
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


def fmt_int(n: int) -> str:
    return "{:,}".format(int(n)).replace(",", " ")


# ========================= ПЛАНИРОВЩИК WINDOWS ======================


def _run_hidden(args):
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    r = subprocess.run(args, capture_output=True, creationflags=flags)
    # консольные утилиты Windows пишут в OEM-кодировке (для русской — cp866)
    enc = "cp866" if os.name == "nt" else "utf-8"
    return {"rc": r.returncode,
            "out": (r.stdout or b"").decode(enc, "replace").strip(),
            "err": (r.stderr or b"").decode(enc, "replace").strip()}


def _pythonw() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


def _self_command(flag: str) -> str:
    """Команда запуска самого приложения с нужным флагом."""
    if getattr(sys, "frozen", False):
        return '"{}" {}'.format(Path(sys.executable).resolve(), flag)
    return '"{}" "{}" {}'.format(_pythonw(), Path(__file__).resolve(), flag)


def _autorun_set(enable: bool):
    """Запись в автозагрузке (HKCU\\...\\Run): при каждом входе в Windows
    приложение тихо проверяет, не пора ли чистить, и сразу закрывается —
    постоянно в памяти ничего не висит. Возвращает (ok, сообщение)."""
    if os.name != "nt":
        return False, "только в Windows"
    try:
        import winreg
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE)
        try:
            if enable:
                winreg.SetValueEx(key, "TempCleaner", 0, winreg.REG_SZ,
                                  _self_command("--clean-if-due"))
            else:
                try:
                    winreg.DeleteValue(key, "TempCleaner")
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True, ""
    except OSError as exc:
        return False, str(exc)


def schedule_set(days):
    """days: 7/30 — включить, None/0 — выключить. Возвращает (ok, сообщение).

    Включает сразу два механизма, оба зовут «--clean-if-due» (очистка
    выполняется, только если с последней прошло больше N дней):
      • задание Планировщика — каждый день в 12:00, если компьютер включён;
      • запись в автозагрузке — проверка при каждом входе в Windows.
    Так очистка не теряется, даже если в 12:00 компьютер был выключен.
    """
    if os.name != "nt":
        return False, "Планировщик доступен только в Windows"
    try:
        if not days:
            _autorun_set(False)
            r = _run_hidden(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
            if r["rc"] == 0:
                return True, ""
            # удаление не удалось: если задания и так нет — это успех,
            # если оно осталось — честно сообщаем, а не делаем вид, что выключили
            q = _run_hidden(["schtasks", "/Query", "/TN", TASK_NAME])
            if q["rc"] != 0:
                return True, ""
            return False, r["err"] or r["out"] or "не удалось удалить задание"
        tr = _self_command("--clean-if-due")
        r = _run_hidden(["schtasks", "/Create", "/F", "/TN", TASK_NAME,
                         "/SC", "DAILY", "/ST", "12:00", "/TR", tr])
        if r["rc"] != 0:
            return False, r["err"] or r["out"] or "не удалось создать задание"
        ok2, msg2 = _autorun_set(True)
        if not ok2:
            return True, "но запись в автозагрузку не создана: %s" % msg2
        return True, ""
    except OSError as exc:
        return False, str(exc)


# ============================ ТИХИЙ РЕЖИМ ===========================

if "--clean-if-due" in sys.argv:
    # Тихий запуск из автозагрузки/Планировщика: чистим, только если
    # с последней очистки прошло больше выбранного срока. Иначе мгновенно
    # выходим — никакого постоянного процесса в памяти.
    _cfg = read_cfg()
    if not isinstance(_cfg, dict):
        sys.exit(1)   # конфиг повреждён — не чистим (fail-closed)
    # отмечаем сам факт проверки — интерфейс покажет её внизу окна,
    # чтобы было видно, что автозагрузка работает
    with _CFG_LOCK:
        _c2 = load_cfg()
        _c2["last_check"] = time.time()
        save_cfg(_c2)
    _days = _safe_int(_cfg.get("schedule_days", 0))
    try:
        _last = float(_cfg.get("last_run", 0))
    except (TypeError, ValueError):
        _last = 0.0
    # запас в 1 час, чтобы запуск «ровно через 7 дней» не пропускался
    if _days > 0 and time.time() - _last >= _days * 86400 - 3600:
        _s = run_clean(dry=False)
        if "error" in _s:
            sys.exit(1)
        remember_run(_s)
    sys.exit(0)

if "--clean" in sys.argv:
    _s = run_clean(dry=False)
    if "error" in _s:
        sys.exit(1)   # Планировщик увидит ненулевой «последний результат»
    remember_run(_s)
    sys.exit(0)


# =============================== GUI ================================

import tkinter as tk  # noqa: E402
import tkinter.font as tkfont  # noqa: E402

BG      = "#0d1117"
CARD    = "#161d27"
CARD_HI = "#1c2532"
LINE    = "#232e3d"
ACCENT  = "#3d7dff"
ACCENT_H = "#5a92ff"
TEXT    = "#e9eef5"
MUTED   = "#8d99ac"
GOOD    = "#3ecf8e"
WARN    = "#e8b34b"

FAMILY = "Segoe UI" if os.name == "nt" else "DejaVu Sans"


def F(size, bold=False):
    return (FAMILY, size, "bold" if bold else "normal")


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class RoundButton(tk.Canvas):
    """Кнопка со скруглёнными углами, hover-подсветкой и режимом «выбрана»."""

    def __init__(self, master, text, command=None, width=170, height=48,
                 radius=14, bg=CARD, fg=MUTED, hover=CARD_HI,
                 sel_bg=ACCENT, sel_fg="#ffffff", sel_hover=ACCENT_H,
                 font=None, parent_bg=BG):
        super().__init__(master, width=width, height=height, bg=parent_bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.cmd = command
        self.w, self.h, self.r = width, height, radius
        self.colors = dict(bg=bg, fg=fg, hover=hover, sel_bg=sel_bg,
                           sel_fg=sel_fg, sel_hover=sel_hover)
        self.font = font or F(11)
        self.text = text
        self.selected = False
        self.enabled = True
        self._hover = False
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _draw(self):
        self.delete("all")
        c = self.colors
        if self.selected:
            fill = c["sel_hover"] if (self._hover and self.enabled) else c["sel_bg"]
            fg = c["sel_fg"]
        else:
            fill = c["hover"] if (self._hover and self.enabled) else c["bg"]
            fg = c["fg"]
        if not self.enabled:
            fg = MUTED
        round_rect(self, 1, 1, self.w - 1, self.h - 1, self.r, fill=fill, outline="")
        self.create_text(self.w // 2, self.h // 2, text=self.text, fill=fg,
                         font=self.font)

    def _on_enter(self, _):
        self._hover = True
        self._draw()

    def _on_leave(self, _):
        self._hover = False
        self._draw()

    def _on_click(self, _):
        if self.enabled and self.cmd:
            self.cmd()

    def set_selected(self, val):
        self.selected = bool(val)
        self._draw()

    def set_enabled(self, val):
        self.enabled = bool(val)
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self._draw()

    def set_text(self, text):
        self.text = text
        self._draw()


ICON_B64 = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAALSElEQVR42t2bf2xdZRnHP897zrk/2q7dWqibDgZlgzHGYAzIdD8KAxwgJhrp/MOYaAwSo0b5yyjZbq8hgonGRKOJif5h/EOyakRRgsIIHTBgZYuMdbCNDRT5Vdb9YN1u7z3nvI9/vPest1vb3W69t8WTnNzennPee57v831+vu8rTOpQ6dqMGehH2vegPT0SMxOOnJpOMAC93cSIaLWPymQE79kwWuCV92k2fTFpjk6f7AWw2/PyYeX/ujar19OPkhd73gB0damXaPr2b2v6ZCvrBdZZZZlaFolhllq0ejCn7FAEQQmNzx7goHhs9QyPPXW/vH8KiA0Ts3TCl+7Mqd+bl+jW72lL2MB9Cl80HovFgMblU6ffAowPYpw0cZH3MTwahfxk2wOyN5dTk+9GxzMLGY/yuRySz4tds1HXi8/P/RSXxyWIQ+IEfXE6EKYZBBUs6lhoPDwvDXHIMVE29nbLLxI/MZZJyJjDKSCiazdpTny6UYhLRCIYxDmbGXsoqhAbg+9nIC6xuXiMb7zwMzk8FghnAODsBrtmI79ONXFPcYgYRWSmCz4GEEAUNBGEBfpSRe7Y8iCHndQj5mBOt/meDRKvup9cqol7iscpCZiPnPBOtYIQlIYoBRluKAU8jIh29YyWRUZrXuLVG/UWP+CJKCQW8KbBu1cdv7V6NoRBI0HpBN3PPiD5yuhgErtf0o/e+pC2iPBrVQTFzFThrUI8OcfrhwViL+D+1Rt1Rc8GiXM5NacA6Mzh5fNiS0N8N8hyWRSWHd5M07rAcAiLP0540xIKJ0tgpDpzUAvGIxDlp6dbCqjKyvvIBI3sNgGX2hCdid7eM3D4BOTv5ljHXKKv/JK25qxjRLXkEQ8bFln+/IOyG1VjOjepD6KpLLf6GTri0swUXoAwhgubsWuXMLxkPqVPtGJLUfV2qor1U/i+x1cBVtyLZ4beLT/vsR5BEeyMdHoCxRCuXUDYOovY97C3Xk1hOARTvbpMHAHCzSu+rsFd84hNxxEnsCpLNMns6qTRqt9anJCRhc9ez4lkiJuXUvC9snlIVb8pGoNaLpszh4Z8Xqzp6cF25rRJYZFGIFp7ALQKLy7ibB7gZAkGh5z2V11BqYyfLFtAuPQiwsMnYKjoxvWMe3YCZ6gmRcPJDIsAfBCNixrgMbtc2EgtaXyyCMsWwEVt8OhLMGscJ1YMoRDCrDTcuJDiZ5ZTWHc1w76H2oos7lf3MPjULjJb+snsOED68BCSCiAbjAu+eh6+iWgpAwBeGo1joppXbWU7vv1aWHoRPNI3/r2XtBN9ehmFdUsZvqSdMMHQnpa+ZgL0zhWcvHMFJ/87iL99P+l/7iL72tsEdjw2K+A7ec05muU5HbGFlkZYvRgWzYMFF0ApGpuybU3EHXOJLmhm3Hr+dG89bw7xonmEc+cQGzNxpqjWyevXrWY3cGIYru+Aj7W47ysvh83bIB2c6RP6DpB+bi/ptiZ01WKGb1tG4YbLKGVSo02gUEL6DpDu7Sez/XUybx9xuVFDagJfUJki1jWOR9B51UjYuukq+NMLYzdVGlLQmIZihDy2k+yjO8hes4DwN/cy6Psjyv3mb2jb+QaBbyAVQHNmJF2uSjH1iuFhDO0tcNs1I2Z4zSUwv3VsM7DqTMYT5yjnNMDL/yZ4fj+p8uO6698Eu98imNPo7gk899wkMsP6AVAowXWXQltT+SWt0/Kqxe7aeDm9ln2HlmP9k7vIJtee3k02ikf8y7m050y96B/Hjv5JHpDIe9f1zgecTWvWQiYFz+0lM3gcD2DbXjIp3107Z99UD+2XIpjfBp+8YnRmp8DCue4shuXGJuMzIfBgcAjpe530mwMEBz/ASwecV0uy5gCYcvKzejHMbnBUTezdWifUqitguHT2l0kyvcdfJvu3nY7+5jyDd80BUHXaTugvYyQea69y9D6rGajzGy+/SerPL9LYmHaAzlgAjHEveHGb8/iJSVReV+DyebBorguTXhVvFFmkFCEyBambqYXDM8YJElt45wjcssxprpL+lc4t8FxS9P4xFy698vMywW/IFOWt/lTaehLvCwUn7PxWuO0m+PyNZVOQsZ9Thc/d6Cq+nQfhrUPuWjbtwNFJxva6ASDiBIgtnCiWk51m+NQVsPZK99nadPYxAD7RCpvuduM8swe27IaX34QPjjs2NKTc51SD4Z+r0FZd6BoOoTkL13VA5xK4bRlc2Dy6AErYcTZnadWlv7cvd+fAMdj+OjyxyzHj6AnwPciWwbB6/nOTkwZgOHSZWzZw8Xvl5XDHcvd3pbdOKO+Z6oH1xDlFW/YV7S1w1wp3vjEA2/bC1ldhz1tw6DhkAhc9pJ4AdLTDmivh5qVO6MAbrUFTZsi5vpUwAlrlmJe2u/NLa+DgADzdDy/ug33vnl8onHQUaG+BJfNdZpcIn2RqMtVdBXHDnW7zF1/g6or5bSNOtG4M2LbXoX9hs3uJtUvgxoUOmET42JZDlZk8HuqaFa74MSPp8Ymi8wO9e5xfeOuQM5PG9PmFxEkD0JiGpozzBVt2wxOvwIWzYPmlzlav63D3nKsTHEvorXug7wD8d9A1TrIBtDQ4wK2tsxO06tTkifP+Sdf2yVdgyyuOljdc5phxOhjVOMGJhG7KuPtO1fxTEA7POQ9I6vSkJ9+cdVocOAZ/fBH+vN2BcV0HfG0dfHzOmalwonlwGeNvn3LC11roKU+FEzCsQuC7qq8p40LV75523d9EiLEYJQKPbHf3Hjrunp3d4MZKOkO1Wos05bWA6ggYvgdzZ8Nzr0IxKvcC9cyCKYzhhX2uWZq0tWopdN2qwaTQee0d2HHwzJBmy9Fi37uw/z2n8bjOM5N16QipQm//iLlUmg7A1v5yQ2QalmPUHACrLnffvt9Fi6SgqaT/c3vLDRH+DwFQhZQP/xmEf71ZEfMr6P/6e64xqvb/EIBTCb7CX/tGvs8E+tcNAFXX3Nh50DU9koJpuuk/GoAargtQddHggw+dL0ic43TSP5kcNQDBMIqcmoKuzQ+W219JNJhO+qsjnJse7+pS78mH+NDAXuOXFx7X0Ax2vAGHh9z3Z1+rM/0VNQYTFykEhlcBzMCAW/etwgEx1CDbHm0G7x2FZ151uf++d+tOfxUPUN7JtnIcVAw3nULnKZHyOrEamkHgOc3/fYebB6gn/RWsCcAYtj3+HSl25vBMb7dbgREVeCwscMh4mPJK65qkxo0ZeOkAbH7eTWnXOfU1GqEWfg/QfhVqENGuzeo9/1MZUOUvfgZRqNlmKMEtdyuGUze5UaUJWi+FRCH7PEOvqkrPBokNQE8/iqoY+HFc5Lh4SK1YkIAg9U98rPEQo2zqzUu0obxs3uUBebFdPZitD8j+KOT7QQZPpfarxuoY86NUA35YoGfrA7K5crn8KD0km6RWb9I/phr4QmmIkgipj7TwSuSn8W3EGyW4fj0crdxENSoV7u3GraMvcU9YoC/VSAolrFVorIfm/TS+Wo5EIRteyMvEW2YQ0Tzw7ENyJPyQ26Mi//AbCQBRJf4Iad2qEgeN+GrZXyqwftuP5KWuzerlz7ZpChi1xWzNRu0Wn/uNhx+79bgRbh/Recz/1CLHc1U2YLwUxhiIIx4uDPOtvodkcLxNlDIBjJLrdnsHV/1AV/oZfmhj1vlpPI3AulXXqjq95iGAeBgxbgOljQHLK1ge7P2h/AEgl1OTH2cb7dm3zlYgty6nyyLhy6J0WrdtdrYJptdDqIKNOQa8I4YX1fKHuXvY0tPj9gVNtGuUaik81kCdP9PZ4QcsDLI0RcPUcc1p+YjA90EtcZBlT+tCjlZSvHLP85QduZyazpy6mnEGHp059bu61JvM+/0PzAII1T5u8cAAAAAASUVORK5CYII="


class App:
    PAD = 24        # внешние поля
    COL = 386       # ширина каждой из двух колонок
    GAP = 28        # расстояние между колонками
    W = PAD * 2 + COL * 2 + GAP  # 848 — широкое невысокое окно

    def __init__(self, root: tk.Tk):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        raw = read_cfg()
        self._cfg_broken = raw is None
        self.cfg = raw if isinstance(raw, dict) else {}
        if self._cfg_broken or "exclude" not in self.cfg:
            # первый запуск или конфиг повреждён — фиксируем список заново
            self.cfg["exclude"] = list(DEFAULT_EXCLUDE)
            with _CFG_LOCK:
                save_cfg(self.cfg)
        self.excludes = _sanitize_patterns(self.cfg.get("exclude"))

        root.title(APP_NAME)
        root.configure(bg=BG)
        root.resizable(False, False)
        try:
            root.iconphoto(True, tk.PhotoImage(data=ICON_B64))
        except tk.TclError:
            pass

        self._build()
        self._fit(center=True)
        self.root.after(80, self._poll)

        if self._cfg_broken:
            self._flash("Файл настроек был повреждён — восстановлен "
                        "стандартный список исключений", WARN)

        # при старте: пересоздать задание (вдруг файл переносили) и догнать
        # пропущенную автоочистку, затем посчитать, сколько можно освободить
        self._startup()

    # ---------- каркас интерфейса ----------

    def _build(self):
        P, COL, GAP = self.PAD, self.COL, self.GAP
        full = self.W - 2 * P

        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=P, pady=(20, 2))
        tk.Label(head, text="Очистка Temp", bg=BG, fg=TEXT,
                 font=F(19, True)).pack(anchor="w")
        path = temp_root()
        if len(path) > 90:
            path = path[:44] + "…" + path[-43:]
        tk.Label(head, text=path, bg=BG, fg=MUTED, font=F(9)).pack(anchor="w")

        body = tk.Frame(self.root, bg=BG)
        body.pack(padx=P, pady=(14, 0), anchor="w")
        left = tk.Frame(body, bg=BG)
        right = tk.Frame(body, bg=BG)
        left.grid(row=0, column=0, sticky="nw")
        right.grid(row=0, column=1, sticky="nw", padx=(GAP, 0))

        # ===== левая колонка: карточка, очистка, расписание =====
        self.card = tk.Canvas(left, width=COL, height=138, bg=BG,
                              highlightthickness=0, bd=0)
        self.card.pack(anchor="w")
        round_rect(self.card, 1, 1, COL - 1, 137, 16, fill=CARD, outline="")
        self.card.create_text(20, 25, anchor="w", text="МОЖНО ОСВОБОДИТЬ",
                              fill=MUTED, font=(FAMILY, 8, "bold"))
        self.v_size = self.card.create_text(20, 57, anchor="w", text="…",
                                            fill=TEXT, font=F(25, True))
        # длинная строка статистики переносится, а не обрезается
        self.v_sub = self.card.create_text(20, 84, anchor="nw",
                                           text="считаю размер…",
                                           fill=MUTED, font=F(9),
                                           width=COL - 40)
        # круглая кнопка «обновить» (иконка дугой — не зависит от шрифтов)
        rr = round_rect(self.card, COL - 52, 16, COL - 16, 52, 18,
                        fill=CARD_HI, outline="")
        cx, cy, ir = COL - 34, 34, 7
        a1 = self.card.create_arc(cx - ir, cy - ir, cx + ir, cy + ir,
                                  start=55, extent=305, style="arc",
                                  outline=MUTED, width=2)
        a2 = self.card.create_polygon(cx + ir - 4, cy - 5, cx + ir + 4, cy - 5,
                                      cx + ir, cy + 2, fill=MUTED, outline="")
        for tag in (rr, a1, a2):
            self.card.tag_bind(tag, "<Button-1>", lambda _e: self.rescan())
            self.card.tag_bind(tag, "<Enter>",
                               lambda _e: self.card.configure(cursor="hand2"))
            self.card.tag_bind(tag, "<Leave>",
                               lambda _e: self.card.configure(cursor=""))

        self.btn_clean = RoundButton(
            left, "Очистить сейчас", command=self.clean_now,
            width=COL, height=52, radius=15,
            bg=ACCENT, fg="#ffffff", hover=ACCENT_H, font=F(12, True))
        self.btn_clean.pack(anchor="w", pady=(14, 0))

        tk.Label(left, text="АВТОМАТИЧЕСКАЯ ОЧИСТКА", bg=BG, fg=MUTED,
                 font=(FAMILY, 8, "bold")).pack(anchor="w", pady=(20, 8))

        row = tk.Frame(left, bg=BG)
        row.pack(anchor="w")
        gap = 8
        w1 = 86
        w2 = (COL - w1 - 2 * gap) // 2   # при COL=386 кнопки по 143px
        self.btn_off = RoundButton(row, "Выкл", width=w1, height=44, radius=13,
                                   command=lambda: self.set_mode(0))
        self.btn_7 = RoundButton(row, "Раз в 7 дней", width=w2, height=44,
                                 radius=13, command=lambda: self.set_mode(7))
        self.btn_30 = RoundButton(row, "Раз в 30 дней", width=w2, height=44,
                                  radius=13, command=lambda: self.set_mode(30))
        self.btn_off.pack(side="left")
        self.btn_7.pack(side="left", padx=gap)
        self.btn_30.pack(side="left")

        self.lbl_mode = tk.Label(left, text="", bg=BG, fg=MUTED, font=F(9),
                                 wraplength=COL, justify="left")
        self.lbl_mode.pack(anchor="w", pady=(8, 0))

        # ===== правая колонка: исключения =====
        tk.Label(right, text="ИСКЛЮЧЕНИЯ — ЭТО НЕ УДАЛЯЕТСЯ", bg=BG,
                 fg=MUTED, font=(FAMILY, 8, "bold")).pack(anchor="w",
                                                          pady=(4, 8))
        exrow = tk.Frame(right, bg=BG)
        exrow.pack(fill="x")
        self.ent = tk.Entry(exrow, bg=CARD, fg=TEXT, insertbackground=TEXT,
                            relief="flat", font=F(10), highlightthickness=1,
                            highlightbackground=LINE, highlightcolor=ACCENT)
        self.ent.pack(side="left", fill="x", expand=True, ipady=7,
                      padx=(0, 8))
        self.ent.bind("<Return>", lambda _e: self.add_exclude())
        self.btn_add = RoundButton(exrow, "Добавить", width=96, height=36,
                                   radius=12, bg=CARD_HI, fg=TEXT,
                                   hover="#242f3f", font=F(10),
                                   command=self.add_exclude)
        self.btn_add.pack(side="left")

        self.chip_wrap = tk.Frame(right, bg=BG)
        self.chip_wrap.pack(fill="x", pady=(10, 0))
        self._render_chips()

        tk.Label(
            right,
            text=("Маски имён файлов и папок, регистр не важен: * — любые "
                  "символы. Например, nvidia* защитит temp-файлы NVIDIA. "
                  "Крестик на плашке удаляет её из списка. Не удаляются "
                  "также: файлы моложе 24 часов, занятые программами и "
                  "сама папка Temp."),
            bg=BG, fg=MUTED, font=F(9), wraplength=COL, justify="left",
        ).pack(anchor="w", pady=(10, 0))

        # ===== низ на всю ширину =====
        tk.Frame(self.root, bg=LINE, height=1).pack(fill="x", padx=P,
                                                    pady=(16, 10))
        # высота 2 строки зарезервирована, чтобы длинные сообщения
        # не обрезались нижним краем окна
        self.lbl_status = tk.Label(self.root, text="", bg=BG, fg=MUTED,
                                   font=F(9), wraplength=full,
                                   justify="left", anchor="nw", height=2)
        self.lbl_status.pack(anchor="w", fill="x", padx=P, pady=(0, 12))

        self._show_mode(_safe_int(self.cfg.get("schedule_days", 0)))
        self._show_last()

    def _fit(self, center=False):
        """Подгоняет высоту окна под содержимое (список исключений растёт)."""
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        if center:
            x = (self.root.winfo_screenwidth() - self.W) // 2
            y = max(20, (self.root.winfo_screenheight() - h) // 2 - 30)
            self.root.geometry("%dx%d+%d+%d" % (self.W, h, x, y))
            return
        # позицию берём из строки геометрии; координаты могут быть
        # отрицательными (окно на левом мониторе) — поэтому regex, а не split
        m = re.search(r"([+-]\d+[+-]\d+)$", self.root.geometry())
        if m:
            self.root.geometry("%dx%d%s" % (self.W, h, m.group(1)))
        else:
            self.root.geometry("%dx%d" % (self.W, h))

    # ---------- исключения ----------

    def _render_chips(self):
        for w in self.chip_wrap.winfo_children():
            w.destroy()
        inner = self.COL
        if not self.excludes:
            tk.Label(self.chip_wrap, text="список пуст — исключений нет",
                     bg=BG, fg=MUTED, font=F(9)).pack(anchor="w")
            return
        fnt = tkfont.Font(family=FAMILY, size=9)
        row = tk.Frame(self.chip_wrap, bg=BG)
        row.pack(anchor="w")
        used = 0
        for pat in self.excludes:
            w = min(fnt.measure(pat), inner - 60) + 44
            if used and used + w > inner:
                row = tk.Frame(self.chip_wrap, bg=BG)
                row.pack(anchor="w", pady=(6, 0))
                used = 0
            self._chip(row, pat, w).pack(side="left", padx=(0, 6))
            used += w + 6

    def _chip(self, parent, pat, w):
        h = 28
        c = tk.Canvas(parent, width=w, height=h, bg=BG, highlightthickness=0)
        round_rect(c, 1, 1, w - 1, h - 1, 13, fill=CARD, outline="")
        c.create_text(12, h // 2, anchor="w", text=pat, fill=TEXT,
                      font=(FAMILY, 9), width=w - 40)
        xid = c.create_text(w - 15, h // 2, text="×", fill=MUTED,
                            font=(FAMILY, 11))
        c.tag_bind(xid, "<Button-1>",
                   lambda _e, p=pat: self.remove_exclude(p))
        c.tag_bind(xid, "<Enter>",
                   lambda _e: (c.itemconfigure(xid, fill="#ff8484"),
                               c.configure(cursor="hand2")))
        c.tag_bind(xid, "<Leave>",
                   lambda _e: (c.itemconfigure(xid, fill=MUTED),
                               c.configure(cursor="")))
        return c

    def _save_excludes(self):
        with _CFG_LOCK:
            cfg = load_cfg()
            cfg["exclude"] = list(self.excludes)
            saved = save_cfg(cfg)
            self.cfg = cfg
        if not saved:
            self._flash("Список действует в этом окне, но сохранить его "
                        "не удалось — нет прав на запись рядом с программой. "
                        "Перенесите её, например, в Документы.", WARN)
        self._render_chips()
        self._fit()
        self.rescan()

    def add_exclude(self):
        pat = self.ent.get().strip().strip('"').strip()
        if not pat:
            return
        if len(pat) > MAX_PATTERN_LEN:
            self._flash("Слишком длинная маска (больше %d символов)"
                        % MAX_PATTERN_LEN, WARN)
            return
        if "\\" in pat or "/" in pat:
            self._flash("В маске должно быть имя файла или папки, "
                        "без \\ и /", WARN)
            return
        if pat.lower() in (p.lower() for p in self.excludes):
            self.ent.delete(0, "end")
            return
        self.excludes.append(pat)
        self.ent.delete(0, "end")
        self._save_excludes()

    def remove_exclude(self, pat):
        self.excludes = [p for p in self.excludes if p != pat]
        self._save_excludes()

    # ---------- фоновые задачи ----------

    def _bg(self, fn, cb):
        def work():
            try:
                res = fn()
            except Exception as exc:  # noqa: BLE001 — показываем любую ошибку
                res = {"error": str(exc)}
            self.q.put((cb, res))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                cb, res = self.q.get_nowait()
                cb(res)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _startup(self):
        days = _safe_int(self.cfg.get("schedule_days", 0))
        if days and os.name == "nt":
            # освежаем задание в Планировщике (файл могли переносить)
            self._bg(lambda: schedule_set(days), lambda _r: None)
            try:
                last = float(self.cfg.get("last_run", 0))
            except (TypeError, ValueError):
                last = 0.0
            if time.time() - last > days * 86400:
                # догоняем пропущенную автоочистку (компьютер был выключен)
                self.busy = True
                self.btn_clean.set_enabled(False)
                self._tick = 0
                self._animate()
                self._flash("Выполняю пропущенную автоочистку…", MUTED)
                pats = list(self.excludes)
                self._bg(lambda: run_clean(dry=False, patterns=pats),
                         self._catchup_done)
                return
        self.rescan()

    def _catchup_done(self, s):
        self.busy = False
        self.btn_clean.set_enabled(True)
        self.btn_clean.set_text("Очистить сейчас")
        if "error" in s:
            self._flash("Автоочистка не выполнена: %s" % s["error"], WARN)
        else:
            self.cfg = remember_run(s)
            self._flash("Пропущенная автоочистка выполнена: освобождено %s ✓"
                        % fmt_size(s["bytes"]), GOOD)
        self.rescan()

    # ---------- действия ----------

    def rescan(self):
        if self.busy:
            return
        self.card.itemconfigure(self.v_size, text="…")
        self.card.itemconfigure(self.v_sub, text="считаю размер…")
        pats = list(self.excludes)
        self._bg(lambda: run_clean(dry=True, patterns=pats), self._scan_done)

    def _scan_done(self, s):
        if "error" in s:
            self.card.itemconfigure(self.v_size, text="—")
            self.card.itemconfigure(self.v_sub, text=s["error"])
            return
        n = s["files"] + s["dirs"]
        self.card.itemconfigure(self.v_size, text=fmt_size(s["bytes"]))
        # две короткие строки — гарантированно помещаются в карточку
        self.card.itemconfigure(
            self.v_sub,
            text="%s %s старше %d ч\nсвежих %s · исключено %s" % (
                fmt_int(n), plural(n, "объект", "объекта", "объектов"),
                MIN_AGE_HOURS, fmt_int(s["fresh"]), fmt_int(s["excluded"])))

    def clean_now(self):
        if self.busy:
            return
        self.busy = True
        self.btn_clean.set_enabled(False)
        self._tick = 0
        self._animate()
        pats = list(self.excludes)
        self._bg(lambda: run_clean(dry=False, patterns=pats),
                 self._clean_done)

    def _animate(self):
        if not self.busy:
            return
        self._tick += 1
        self.btn_clean.set_text("Очищаю" + "." * (self._tick % 4))
        self.root.after(350, self._animate)

    def _clean_done(self, s):
        self.busy = False
        self.btn_clean.set_enabled(True)
        self.btn_clean.set_text("Очистить сейчас")
        if "error" in s:
            self._flash("Ошибка: %s" % s["error"], WARN)
            return
        self.cfg = remember_run(s)
        n = s["files"] + s["dirs"]
        msg = "Готово: удалено %s %s, освобождено %s" % (
            fmt_int(n), plural(n, "объект", "объекта", "объектов"),
            fmt_size(s["bytes"]))
        if s["locked"]:
            msg += " · %s занятых пропущено" % fmt_int(s["locked"])
        if s.get("aborted"):
            self._flash(msg + " · прервано на сверхглубокой вложенности", WARN)
        else:
            self._flash(msg + " ✓", GOOD)
        self.rescan()

    def set_mode(self, days):
        if self.busy:
            return
        for b in (self.btn_off, self.btn_7, self.btn_30):
            b.set_enabled(False)

        def done(res):
            for b in (self.btn_off, self.btn_7, self.btn_30):
                b.set_enabled(True)
            ok, msg = res if isinstance(res, tuple) else (False, str(res))
            if ok:
                with _CFG_LOCK:
                    cfg = load_cfg()   # не затираем свежие изменения
                    cfg["schedule_days"] = days
                    saved = save_cfg(cfg)
                    self.cfg = cfg
                self._show_mode(days)
                if not saved:
                    self._flash("Режим применён, но настройку не удалось "
                                "записать в файл (нет прав на запись)", WARN)
            else:
                self._show_mode(_safe_int(self.cfg.get("schedule_days", 0)))
                self._flash("Не получилось: %s" % msg, WARN)

        self._bg(lambda: schedule_set(days), done)

    # ---------- отображение состояния ----------

    def _show_mode(self, days):
        self.btn_off.set_selected(days == 0)
        self.btn_7.set_selected(days == 7)
        self.btn_30.set_selected(days == 30)
        if days:
            self.lbl_mode.configure(
                text="Автоочистка включена: раз в %d дней. Windows сам "
                     "проверит при входе в систему и ежедневно в 12:00 — "
                     "приложение держать открытым не нужно." % days, fg=GOOD)
        else:
            self.lbl_mode.configure(text="Автоочистка выключена", fg=MUTED)

    def _show_last(self):
        def _ts(key):
            try:
                v = float(self.cfg.get(key))
                return v if v > 0 else None
            except (TypeError, ValueError):
                return None

        ts, lc = _ts("last_run"), _ts("last_check")
        if not ts:
            txt = "Очистка ещё не выполнялась"
        else:
            n = _safe_int(self.cfg.get("last_items", 0))
            txt = "Последняя очистка: %s · удалено %s %s · освобождено %s" % (
                datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M"),
                fmt_int(n), plural(n, "объект", "объекта", "объектов"),
                fmt_size(_safe_int(self.cfg.get("last_bytes", 0))))
        # показываем тихие автопроверки (вход в систему / 12:00),
        # при которых чистить было ещё рано — видно, что автозагрузка живёт
        if lc and (not ts or lc > ts + 90):
            txt += "\nПоследняя автопроверка: %s — чистить было ещё рано" % (
                datetime.fromtimestamp(lc).strftime("%d.%m.%Y %H:%M"))
        self.lbl_status.configure(text=txt, fg=MUTED)

    def _flash(self, text, color):
        self.lbl_status.configure(text=text, fg=color)
        self._flash_n = getattr(self, "_flash_n", 0) + 1
        n = self._flash_n
        # возвращаем обычный статус, только если это последнее сообщение
        self.root.after(7000, lambda: (n == self._flash_n
                                       and self._show_last()))


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
