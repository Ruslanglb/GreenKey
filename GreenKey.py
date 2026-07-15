# -*- coding: utf-8 -*-
"""
GreenKey - автоматическое удаление зелёного ИЛИ синего фона с картинок (принтов).
Аналог Keylight + Advanced Spill Suppressor + Roto Brush "Decontaminate Edge Colors".

Всё автоматически, без настроек:
  - тип фона (зелёный/синий) определяется сам по краям картинки;
  - цвет фона тоже определяется сам (работает на любой зелёный/синий);
  - устойчивый ключ по доминантному каналу: зелёный G>max(R,B) или синий B>max(R,G)
    (не трогает жёлтый/бирюзу/телесный/пурпур);
  - despill (подавление зелёного/синего перелива);
  - деконтаминация краёв: убирает цветную кайму, восстанавливая чистый цвет рисунка;
  - пакетная обработка папки в датированную папку вывода (кнопка «Папка → вырезать всё»).
"""

import os
import gc
import shutil
import datetime
import tempfile
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import numpy as np
from PIL import Image, ImageTk, ImageFilter

OUTPUT_DIR = r"E:\BG"
IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")
PREVIEW_MAX = 900

# ============ ПАРАМЕТРЫ ИЗ ПРЕСЕТОВ Green.ffx / blue.ffx ============
# Только два эффекта, как в After Effects: Keylight (1.2) + Advanced Spill Suppressor.
# Keylight, Screen Colour для двух типов фона (из пресетов):
SCREEN_COLOUR_GREEN = (11 / 255., 163 / 255., 77 / 255.)   # Green.ffx: RGB 11,163,77
SCREEN_COLOUR_BLUE = (0 / 255., 89 / 255., 255 / 255.)     # blue.ffx:  RGB 0,89,255
SCREEN_COLOUR = SCREEN_COLOUR_GREEN  # значение по умолчанию (зелёный)
CLIP_BLACK = 0.09       # Clip Black = 9  (matte < 9%  -> 0)  (одинаково для обоих)
CLIP_WHITE = 0.76       # Clip White = 76 (matte > 76% -> 1)  (одинаково для обоих)
# Advanced Spill Suppressor (Method = Standard):
SPILL_SUPPRESSION = 1.00  # Suppression = 100
DECON_MIN = 0.4           # ограничение знаменателя деконтаминации
SPILL_DARK = 0.30         # ниже этой яркости -> navy ((R+B)/2)
SPILL_LIGHT = 0.48        # выше этой яркости -> деконтаминация (белый край без синевы)
# Анти-алиасинг: супер-сэмплинг ТОЛЬКО матовой маски (alpha).
# RGB не трогается и деления нет -> цветных колец/крапинок не будет.
ANTIALIAS = True
AA_SS = 4                 # максимальный множитель супер-сэмплинга маски
AA_TARGET = 6000          # к какому рабочему разрешению маски стремимся (глаже край)
AA_MAX_SS_SIDE = 6300     # жёсткий предел стороны при супер-сэмплинге (память)
ALPHA_FEATHER = 0.3       # ОЧЕНЬ лёгкое сглаживание (апскейл уже даёт AA; больше — мылит край)
# Рендер в повышенном разрешении — убирает «лесенку» на низком исходнике (и лучше для печати):
OUTPUT_MAX = 8192         # апскейл до 8К по большей стороне при СОХРАНЕНИИ (гладкий край)
PREVIEW_MAX_SIDE = 4096   # в превью ограничиваем ради скорости окна
OUTPUT_MAX_SCALE = 4.0    # но не больше этого множителя
CONTOUR_CHOKE_SRC = 0.8   # поджать контур на ~столько ПИКСЕЛЕЙ ИСХОДНИКА -> убрать тонкую
                          # зелёную линию по краю (край чуть Ќже, не толще)


# ====================== ОПРЕДЕЛЕНИЕ ТИПА И ЦВЕТА ФОНА ======================
def detect_bg(arr):
    """Определить фон по рамке изображения. arr: float32 HxWx3 в 0..1.
    Возвращает (screen_colour, key) где key=1 -> зелёный, key=2 -> синий."""
    h, w = arr.shape[:2]
    m = max(3, min(h, w) // 25)
    ring = np.concatenate([
        arr[:m].reshape(-1, 3), arr[-m:].reshape(-1, 3),
        arr[:, :m].reshape(-1, 3), arr[:, -m:].reshape(-1, 3)])
    med = np.median(ring, axis=0)
    r, g, b = float(med[0]), float(med[1]), float(med[2])
    greenness = g - max(r, b)   # насколько доминирует зелёный
    blueness = b - max(r, g)    # насколько доминирует синий
    # что сильнее выражено на рамке — то и убираем
    if blueness > 0.04 and blueness >= greenness:
        return (r, g, b), 2     # синий фон
    if greenness > 0.04:
        return (r, g, b), 1     # зелёный фон
    return SCREEN_COLOUR_GREEN, 1   # непонятно -> дефолт зелёный


def key_from_colour(S):
    """По цвету экрана S (r,g,b) определить доминантный канал: 1=зелёный, 2=синий."""
    r, g, b = S
    return 2 if (b - max(r, g)) > (g - max(r, b)) else 1


# ====================== ЯДРО: Keylight + Advanced Spill Suppressor ======================
def process(pil_rgb, override_bg=None, preview=False):
    """Keylight (кей) + Advanced Spill Suppressor. preview=True — быстрее (ниже разрешение).
    Тип фона (зелёный/синий) и его цвет определяются автоматически, либо задаются
    пипеткой через override_bg (r,g,b в 0..1)."""
    pil_rgb = pil_rgb.convert("RGB")
    # Апскейл ДО кея: жёсткий край исходника при LANCZOS-увеличении становится плавным,
    # и кей идёт уже по мягкому краю -> нет «лесенки». Выше разрешение = лучше для печати.
    # preview=True -> ограничиваем разрешение ради скорости (окно не тормозит).
    w0, h0 = pil_rgb.size
    omax = PREVIEW_MAX_SIDE if preview else OUTPUT_MAX
    oscale = max(1.0, min(OUTPUT_MAX_SCALE, omax / max(w0, h0)))
    if oscale > 1.001:
        pil_rgb = pil_rgb.resize((round(w0 * oscale), round(h0 * oscale)), Image.LANCZOS)
    w, h = pil_rgb.size
    arr = np.asarray(pil_rgb, dtype=np.float32) / 255.0
    R, G, B = arr[..., 0], arr[..., 1], arr[..., 2]
    # --- выбор цвета экрана и доминантного канала (зелёный=1 / синий=2) ---
    if override_bg:
        S = np.array(override_bg, dtype=np.float32)
        key = key_from_colour(override_bg)          # тип берём из выбранного цвета
    else:
        s_tuple, key = detect_bg(arr)               # авто: тип + цвет по рамке
        S = np.array(s_tuple, dtype=np.float32)

    # -------- 1) KEYLIGHT: screen matte (alpha) --------
    # Считаем маску, при AA — в увеличенном разрешении и усредняем обратно (гладкий край).
    ss = 1
    if ANTIALIAS and AA_SS > 1:
        ss = min(AA_SS, max(1, round(AA_TARGET / max(w, h))))
        if max(w, h) * ss > AA_MAX_SS_SIDE:            # предел по памяти
            ss = max(1, AA_MAX_SS_SIDE // max(w, h))
    if ss > 1:
        big = np.asarray(pil_rgb.resize((w * ss, h * ss), Image.LANCZOS))  # uint8
        a_big = _screen_matte(big[..., 0].astype(np.float32) / 255.0,
                              big[..., 1].astype(np.float32) / 255.0,
                              big[..., 2].astype(np.float32) / 255.0, S, key)
        aimg = Image.fromarray((a_big * 255.0 + 0.5).astype(np.uint8), "L") \
            .resize((w, h), Image.BOX)   # area-усреднение -> антиалиасинг маски
    else:
        a = _screen_matte(R, G, B, S, key)
        aimg = Image.fromarray((a * 255.0 + 0.5).astype(np.uint8), "L")
    # choke: убрать тонкую цветную линию по контуру (эрозия внешнего края внутрь).
    # _fast_erode == MinFilter(2*choke+1), но без медленного рангового фильтра PIL.
    choke = int(round(CONTOUR_CHOKE_SRC * oscale))
    if choke > 0:
        aimg = _fast_erode(aimg, choke)
    if ALPHA_FEATHER > 0:                 # добить остаточную «лесенку» (только alpha)
        aimg = aimg.filter(ImageFilter.GaussianBlur(radius=ALPHA_FEATHER))

    # -------- 2) ADVANCED SPILL SUPPRESSOR --------
    # dom  — доминирование ключевого канала (зелёного или синего) над двумя другими;
    # avg  — среднее двух НЕключевых каналов (для зелёного = (R+B)/2, для синего = (R+G)/2).
    dom = _key_dom(R, G, B, key)          # >0 только где ключевой цвет доминирует
    other_avg = 0.5 * (R + B) if key == 1 else 0.5 * (R + G)
    keyish = dom > 0.0
    # Гибрид по яркости пикселя:
    #   тёмные загрязнённые точки -> ключевой канал к среднему соседей (как navy в AE);
    #   яркие (края белого) -> деконтаминация = чистый цвет без цветного налёта.
    # navy-вариант (Standard suppression)
    std = arr.copy()
    std[..., key] = np.where(keyish,
                             np.clip(arr[..., key] - SPILL_SUPPRESSION * (arr[..., key] - other_avg), 0.0, 1.0),
                             arr[..., key])
    # decon-вариант (вычет фона)
    k = max(float(S[key] - max(S[(key + 1) % 3], S[(key + 2) % 3])), 1e-3)
    screen = np.clip(dom / k, 0.0, 1.0)[..., None]
    fg = np.clip(1.0 - screen, DECON_MIN, 1.0)
    decon = np.clip((arr - screen * S) / fg, 0.0, 1.0)
    # вес по яркости: 0 (тёмное -> navy) .. 1 (яркое -> decon)
    luma = (0.299 * R + 0.587 * G + 0.114 * B)[..., None]
    wgt = np.clip((luma - SPILL_DARK) / max(SPILL_LIGHT - SPILL_DARK, 1e-3), 0.0, 1.0)
    blended = std * (1.0 - wgt) + decon * wgt
    work = np.where(keyish[..., None], blended, arr)

    rgb8 = (np.clip(work, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    out = Image.fromarray(rgb8, mode="RGB").convert("RGBA")
    out.putalpha(aimg)
    return out, tuple(int(x * 255) for x in S), key


def _key_dom(R, G, B, key):
    """Доминирование ключевого канала над двумя другими (>0 = фон). key: 1=зелёный, 2=синий."""
    if key == 2:
        return B - np.maximum(R, G)
    return G - np.maximum(R, B)


def _fast_erode(aimg, r):
    """Быстрая эрозия (min-фильтр) квадратом (2r+1)x(2r+1) через numpy.
    Min по квадрату разделим (min по строкам, затем по столбцам), поэтому результат
    ВНУТРИ КАДРА бит-в-бит совпадает с ImageFilter.MinFilter(2r+1), но в разы быстрее
    на больших изображениях (у PIL это медленный ранговый фильтр). Края — edge-clamp."""
    a = np.asarray(aimg, dtype=np.uint8)
    for axis in (0, 1):
        acc = a.copy()
        for s in range(1, r + 1):
            for d in (s, -s):                       # сдвиг на +d/-d вдоль оси, с зажимом краёв
                sh = np.roll(a, d, axis=axis)
                if axis == 0:
                    if d > 0:
                        sh[:d, :] = a[0:1, :]
                    else:
                        sh[d:, :] = a[-1:, :]
                else:
                    if d > 0:
                        sh[:, :d] = a[:, 0:1]
                    else:
                        sh[:, d:] = a[:, -1:]
                acc = np.minimum(acc, sh)
        a = acc
    return Image.fromarray(a, "L")


def _screen_matte(R, G, B, S, key):
    """Keylight screen matte -> alpha (1=объект, 0=фон). R,G,B,S в 0..1. key: 1=зел., 2=син."""
    dom = _key_dom(R, G, B, key)                      # >0 только где ключевой цвет доминирует
    k = max(float(S[key] - max(S[(key + 1) % 3], S[(key + 2) % 3])), 1e-3)  # «чистота» экрана
    screen = np.clip(dom / k, 0.0, 1.0)               # доля экрана
    m = 1.0 - screen
    return np.clip((m - CLIP_BLACK) / (CLIP_WHITE - CLIP_BLACK), 0.0, 1.0)


def composite_checker(rgba, cell=12):
    w, h = rgba.size
    arr = np.asarray(rgba, dtype=np.float32)
    rgb = arr[..., :3] / 255.0
    al = arr[..., 3:4] / 255.0
    yy, xx = np.mgrid[0:h, 0:w]
    chec0 = (((xx // cell) + (yy // cell)) % 2)[..., None].astype(np.float32)
    bg = np.repeat(0.6 + 0.25 * chec0, 3, axis=2)
    comp = rgb * al + bg * (1.0 - al)
    return Image.fromarray((comp * 255).astype(np.uint8), "RGB")


def next_dated_name(existing):
    """Имя вида YYYY-MM-DD_NN, где NN — первый свободный (01, 02…) среди existing."""
    date = datetime.date.today().isoformat()          # 2026-07-15
    have = set(existing)
    n = 1
    while f"{date}_{n:02d}" in have:
        n += 1
    return f"{date}_{n:02d}"


def make_dated_dir(parent):
    """Создать папку вывода вида <parent>\\YYYY-MM-DD_01 (при занятости — _02, _03…)."""
    existing = [d for d in os.listdir(parent) if os.path.isdir(os.path.join(parent, d))]
    path = os.path.join(parent, next_dated_name(existing))
    os.makedirs(path)
    return path


# ====================== ОБЛАКО ЧЕРЕЗ RCLONE ======================
# Поддержка Яндекс.Диска и Облака Mail.ru (и любых других remote) через утилиту rclone.
# Вход в аккаунты делает сам rclone (команда `rclone config`) — программа только копирует
# папки туда/обратно и не хранит паролей.
RCLONE = "rclone"                                   # ожидается в PATH
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW: не мигать консолью под pythonw


def _run_rclone(args):
    """Запустить rclone с аргументами -> CompletedProcess (stdout/stderr как текст)."""
    return subprocess.run([RCLONE, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)


def rclone_available():
    """Установлен ли rclone (есть ли в PATH)."""
    try:
        return _run_rclone(["version"]).returncode == 0
    except (FileNotFoundError, OSError):
        return False


def rclone_listremotes():
    """Список настроенных remote (['yandex:', 'mailru:', …]) или []."""
    try:
        r = _run_rclone(["listremotes"])
        return [x.strip() for x in r.stdout.splitlines() if x.strip()]
    except (FileNotFoundError, OSError):
        return []


def rclone_list_dirs(remote_path):
    """Имена подпапок в remote_path (для нумерации датированной папки на облаке)."""
    r = _run_rclone(["lsf", "--dirs-only", remote_path])
    if r.returncode != 0:
        return []
    return [d.rstrip("/") for d in r.stdout.splitlines() if d.strip()]


def rclone_img_includes():
    """--include аргументы rclone для картинок (оба регистра расширения)."""
    args = []
    for e in IMG_EXT:
        ext = e[1:]
        args += ["--include", f"*.{ext}", "--include", f"*.{ext.upper()}"]
    return args


# ====================== GUI (минимальный, без настроек) ======================
class App:
    def __init__(self, root):
        self.root = root
        root.title("GreenKey v12 — авто зелёный/синий фон, пакет по папке")
        root.geometry("1100x740")
        root.minsize(880, 560)

        self._batch_running = False
        self.files = []
        self.idx = -1
        self.src = None
        self.preview_src = None
        self.scale = 1.0
        self.out_dir = OUTPUT_DIR
        self.override_bg = None   # None = авто; иначе (r,g,b) 0..1 из пипетки
        self.pick_mode = False
        self._img_x = self._img_y = 0

        # состояние вида (зум/панорама)
        self._base_img = None     # готовое превью (composite_checker) в полном разрешении
        self.zoom = 1.0           # 1.0 = вписано в окно
        self.view_cx = 0.0        # центр вида в координатах base-изображения
        self.view_cy = 0.0
        self._view_eff = 1.0      # экранных пикселей на 1 пиксель base
        self._view_left = 0.0
        self._view_top = 0.0
        self._pan_last = None

        self._build()

    def _build(self):
        left = ttk.Frame(self.root, padding=10)
        left.pack(side="left", fill="y")
        right = ttk.Frame(self.root, padding=6)
        right.pack(side="right", fill="both", expand=True)

        ttk.Button(left, text="Открыть картинки…",
                   command=self.open_files).pack(fill="x")
        ttk.Button(left, text="Открыть папку…",
                   command=self.open_folder).pack(fill="x", pady=(4, 8))

        self.lst = tk.Listbox(left, height=12, width=26, exportselection=False)
        self.lst.pack(fill="x")
        self.lst.bind("<<ListboxSelect>>", self._on_list)
        nav = ttk.Frame(left)
        nav.pack(fill="x", pady=(4, 10))
        ttk.Button(nav, text="◀", width=4,
                   command=lambda: self.step(-1)).pack(side="left")
        ttk.Button(nav, text="▶", width=4,
                   command=lambda: self.step(1)).pack(side="left", padx=4)

        batch = ttk.LabelFrame(left, text="Пакет: папка → авто", padding=8)
        batch.pack(fill="x", pady=(0, 6))
        self.batch_btn = ttk.Button(batch, text="Папка → вырезать всё",
                                    command=self.batch_folder_auto)
        self.batch_btn.pack(fill="x")
        self.prog = ttk.Progressbar(batch, mode="determinate", maximum=100, value=0)
        self.prog.pack(fill="x", pady=(6, 0))
        ttk.Label(batch, text="Выберите папку с картинками —\nпрограмма сама уберёт зелёный/\nсиний фон и сложит PNG в новую\nпапку с датой (…_01).",
                  foreground="#666").pack(anchor="w", pady=(4, 0))

        cloud = ttk.LabelFrame(left, text="Облако (rclone): Яндекс/Mail.ru", padding=8)
        cloud.pack(fill="x", pady=(0, 6))
        ttk.Label(cloud, text="Папка-источник (remote:путь):").pack(anchor="w")
        self.cloud_src = ttk.Entry(cloud)
        self.cloud_src.pack(fill="x")
        self.cloud_src.insert(0, "yandex:")
        self.cloud_link = tk.BooleanVar(value=True)
        ttk.Checkbutton(cloud, text="дать ссылку на результат",
                        variable=self.cloud_link).pack(anchor="w", pady=(2, 0))
        self.cloud_btn = ttk.Button(cloud, text="Облако → вырезать всё",
                                    command=self.cloud_folder_auto)
        self.cloud_btn.pack(fill="x", pady=(4, 0))
        remotes = rclone_listremotes()
        hint = ("Настроено: " + " ".join(remotes)) if remotes else \
            "rclone не найден. Установите rclone.org\nи выполните: rclone config"
        ttk.Label(cloud, text=hint, foreground="#666", wraplength=200).pack(anchor="w", pady=(2, 0))

        info = ttk.LabelFrame(left, text="Определённый фон", padding=8)
        info.pack(fill="x", pady=4)
        self.sw = tk.Canvas(info, width=36, height=22, relief="sunken", bd=1)
        self.sw.pack(side="left")
        self.bg_lbl = ttk.Label(info, text="—")
        self.bg_lbl.pack(side="left", padx=6)

        fb = ttk.Frame(left)
        fb.pack(fill="x", pady=2)
        ttk.Button(fb, text="Пипетка по фону",
                   command=self.start_pick).pack(side="left")
        ttk.Button(fb, text="Сброс",
                   command=self.reset_auto).pack(side="left", padx=4)
        ttk.Label(left, text="(пипетка — только если фон\nубрался не полностью)",
                  foreground="#666").pack(anchor="w")

        out = ttk.LabelFrame(left, text="Сохранение (PNG, прозрачный фон)",
                             padding=8)
        out.pack(fill="x", pady=10)
        self.out_lbl = ttk.Label(out, text=self.out_dir, foreground="#0a58ca",
                                 wraplength=190)
        self.out_lbl.pack(anchor="w")
        ttk.Button(out, text="Папка вывода…",
                   command=self.choose_out).pack(fill="x", pady=2)
        ttk.Button(out, text="Сохранить эту",
                   command=self.save_one).pack(fill="x")
        ttk.Button(out, text="Сохранить ВСЕ",
                   command=self.save_all).pack(fill="x", pady=2)

        self.canvas = tk.Canvas(right, bg="#333", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._canvas_click)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        # зум колёсиком к курсору
        self.canvas.bind("<MouseWheel>", self._on_wheel)          # Windows/macOS
        self.canvas.bind("<Button-4>", self._on_wheel)            # Linux вверх
        self.canvas.bind("<Button-5>", self._on_wheel)            # Linux вниз
        # панорама зажатым средним колесом
        self.canvas.bind("<ButtonPress-2>", self._pan_start)
        self.canvas.bind("<B2-Motion>", self._pan_move)
        self.canvas.bind("<ButtonRelease-2>", self._pan_end)
        # двойной клик — сброс зума
        self.canvas.bind("<Double-Button-1>", lambda e: (self._reset_view(),
                                                         self._redraw()))
        self.status = ttk.Label(right, text="Откройте картинки с зелёным или синим фоном "
                                            "(тип определится сам). Колесо — зум, "
                                            "зажатое колесо — двигать.",
                                anchor="w")
        self.status.pack(fill="x")

    # ---------- файлы ----------
    def open_files(self):
        fs = filedialog.askopenfilenames(
            title="Выберите картинки",
            filetypes=[("Изображения", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp"),
                       ("Все файлы", "*.*")])
        if fs:
            self._set_files(list(fs))

    def open_folder(self):
        d = filedialog.askdirectory(title="Папка с картинками")
        if not d:
            return
        fs = [os.path.join(d, f) for f in sorted(os.listdir(d))
              if f.lower().endswith(IMG_EXT)]
        if not fs:
            messagebox.showinfo("Пусто", "В папке нет картинок.")
            return
        self._set_files(fs)

    def _set_files(self, fs):
        self.files = fs
        self.lst.delete(0, "end")
        for f in fs:
            self.lst.insert("end", os.path.basename(f))
        self.load(0)

    def _on_list(self, _):
        sel = self.lst.curselection()
        if sel:
            self.load(sel[0])

    def step(self, d):
        if self.files:
            self.load((self.idx + d) % len(self.files))

    def load(self, i):
        if not self.files:
            return
        self.idx = i % len(self.files)
        self.override_bg = None  # для новой картинки — снова авто
        self.lst.selection_clear(0, "end")
        self.lst.selection_set(self.idx)
        try:
            self.src = Image.open(self.files[self.idx]).convert("RGB")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не открыть файл:\n{e}")
            return
        w, h = self.src.size
        self.scale = min(1.0, PREVIEW_MAX / max(w, h))
        self.preview_src = (self.src if self.scale == 1.0 else
                            self.src.resize((max(1, int(w * self.scale)),
                                             max(1, int(h * self.scale))),
                                            Image.LANCZOS))
        self._show()

    def _render(self):
        """Обработать текущий кадр в base-изображение (полное разрешение)."""
        if self.src is None:
            self._base_img = None
            return
        rgba, bg, key = process(self.src, self.override_bg, preview=True)
        self.sw.configure(bg="#%02x%02x%02x" % bg)
        kind = "синий" if key == 2 else "зелёный"
        self.bg_lbl.config(text="%s  RGB%s%s" % (kind, bg, "" if self.override_bg is None
                                                 else "  (пипетка)"))
        self._base_img = composite_checker(rgba)
        if self.files:
            self.status.config(
                text=f"{os.path.basename(self.files[self.idx])}  "
                     f"[{self.idx + 1}/{len(self.files)}]  "
                     f"{self.src.size[0]}×{self.src.size[1]}  "
                     f"(зум {self.zoom:.1f}× — колесо; двигать — зажатое колесо)")

    def _reset_view(self):
        if self._base_img is not None:
            bw, bh = self._base_img.size
            self.zoom = 1.0
            self.view_cx, self.view_cy = bw / 2.0, bh / 2.0

    def _refresh(self):
        """Пересчитать кадр, сохранив текущий зум/позицию."""
        self._render()
        self._redraw()

    def _show(self):
        """Полное обновление: пересчёт + сброс вида."""
        self._render()
        self._reset_view()
        self._redraw()

    def _redraw(self):
        """Нарисовать base-изображение с текущим зумом/панорамой."""
        if self._base_img is None:
            return
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        bw, bh = self._base_img.size
        fit = min(cw / bw, ch / bh)
        eff = fit * self.zoom                         # экранных пикс на 1 base-пикс
        vw = min(bw, cw / eff)                         # видимая область в base-координатах
        vh = min(bh, ch / eff)
        cx = min(max(self.view_cx, vw / 2), bw - vw / 2)
        cy = min(max(self.view_cy, vh / 2), bh - vh / 2)
        self.view_cx, self.view_cy = cx, cy
        left, top = cx - vw / 2, cy - vh / 2
        box = (int(left), int(top),
               int(np.ceil(left + vw)), int(np.ceil(top + vh)))
        box = (max(0, box[0]), max(0, box[1]), min(bw, box[2]), min(bh, box[3]))
        crop = self._base_img.crop(box)
        tw, th = max(1, int(crop.width * eff)), max(1, int(crop.height * eff))
        flt = Image.LANCZOS if eff < 1.0 else Image.NEAREST
        disp = crop.resize((tw, th), flt)
        self._tk = ImageTk.PhotoImage(disp)
        self.canvas.delete("all")
        self._img_x = (cw - tw) // 2
        self._img_y = (ch - th) // 2
        self._view_eff = eff
        self._view_left, self._view_top = box[0], box[1]
        self.canvas.create_image(self._img_x, self._img_y, anchor="nw",
                                 image=self._tk)

    def _canvas_to_src(self, ex, ey):
        """Координаты на canvas -> пиксель исходной картинки (base == размер src)."""
        bx = self._view_left + (ex - self._img_x) / self._view_eff
        by = self._view_top + (ey - self._img_y) / self._view_eff
        return bx, by

    def _on_wheel(self, e):
        if self._base_img is None:
            return
        delta = e.delta if e.delta != 0 else (120 if getattr(e, "num", 0) == 4 else -120)
        factor = 1.25 if delta > 0 else 1 / 1.25
        new_zoom = min(max(self.zoom * factor, 1.0), 32.0)
        if new_zoom == self.zoom:
            return
        # base-точка под курсором до зума
        bx, by = self._canvas_to_src(e.x, e.y)
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        bw, bh = self._base_img.size
        fit = min(cw / bw, ch / bh)
        eff_new = fit * new_zoom
        # держим точку под курсором на месте
        self.view_cx = bx + (cw / 2.0 - e.x) / eff_new
        self.view_cy = by + (ch / 2.0 - e.y) / eff_new
        self.zoom = new_zoom
        self._redraw()
        if self.files:
            self.status.config(text=f"зум {self.zoom:.1f}×  (двойной клик — сброс)")

    def _pan_start(self, e):
        self._pan_last = (e.x, e.y)
        self.canvas.config(cursor="fleur")

    def _pan_move(self, e):
        if self._pan_last is None or self._base_img is None:
            return
        dx = e.x - self._pan_last[0]
        dy = e.y - self._pan_last[1]
        self._pan_last = (e.x, e.y)
        self.view_cx -= dx / self._view_eff
        self.view_cy -= dy / self._view_eff
        self._redraw()

    def _pan_end(self, e):
        self._pan_last = None
        self.canvas.config(cursor="")

    # ---------- пипетка (fallback) ----------
    def start_pick(self):
        if self.src is None:
            return
        self.pick_mode = True
        self.canvas.config(cursor="crosshair")
        self.status.config(text="Кликните по зелёному фону…")

    def _canvas_click(self, e):
        if not self.pick_mode or self.src is None:
            return
        bx, by = self._canvas_to_src(e.x, e.y)
        w, h = self.src.size
        # base-изображение может быть крупнее исходника (апскейл) -> переводим по масштабу
        rx = w / self._base_img.width
        ry = h / self._base_img.height
        ox, oy = int(bx * rx), int(by * ry)
        if 0 <= ox < w and 0 <= oy < h:
            r, g, b = self.src.getpixel((ox, oy))
            self.override_bg = (r / 255., g / 255., b / 255.)
        self.pick_mode = False
        self.canvas.config(cursor="")
        self._refresh()   # сохранить текущий зум/позицию

    def reset_auto(self):
        self.override_bg = None
        self._refresh()

    # ---------- сохранение ----------
    def choose_out(self):
        d = filedialog.askdirectory(title="Папка для сохранения",
                                    initialdir=self.out_dir)
        if d:
            self.out_dir = d
            self.out_lbl.config(text=d)

    def _out_path(self, src_path):
        stem = os.path.splitext(os.path.basename(src_path))[0]
        return os.path.join(self.out_dir, stem + "_key.png")

    def save_one(self):
        if self.src is None:
            return
        os.makedirs(self.out_dir, exist_ok=True)
        default = os.path.basename(self._out_path(self.files[self.idx]))
        path = filedialog.asksaveasfilename(
            title="Сохранить PNG", initialdir=self.out_dir,
            initialfile=default, defaultextension=".png",
            filetypes=[("PNG", "*.png")])
        if not path:
            return
        out, _, _ = process(self.src, self.override_bg)
        out.save(path)
        self.status.config(text="Сохранено: " + path)

    def save_all(self):
        if not self.files:
            return
        os.makedirs(self.out_dir, exist_ok=True)
        n = 0
        for f in self.files:
            try:
                img = Image.open(f).convert("RGB")
                out, _, _ = process(img)   # каждая картинка — свой авто-фон (зел./син.)
                out.save(self._out_path(f), compress_level=3)
                n += 1
                del img, out
                if n % 4 == 0:
                    gc.collect()
                self.status.config(text=f"Обработка… {n}/{len(self.files)}")
                self.root.update_idletasks()
            except Exception:
                pass
        messagebox.showinfo("Готово", f"Сохранено {n} файл(ов) в:\n{self.out_dir}")

    def batch_folder_auto(self):
        """Выбрать папку -> авто-убрать фон (зел./син.) у всех картинок ->
        сложить PNG в новую датированную папку <источник>\\YYYY-MM-DD_01.
        Тяжёлая обработка идёт в фоновом потоке, чтобы окно не зависало."""
        if self._batch_running:
            return
        d = filedialog.askdirectory(title="Папка с картинками (любой фон)")
        if not d:
            return
        files = [os.path.join(d, f) for f in sorted(os.listdir(d))
                 if f.lower().endswith(IMG_EXT) and os.path.isfile(os.path.join(d, f))]
        if not files:
            messagebox.showinfo("Пусто", "В папке нет картинок.")
            return
        out_dir = make_dated_dir(d)   # <папка>\YYYY-MM-DD_01
        self._batch_running = True
        self.batch_btn.config(state="disabled", text="Обработка…")
        self.prog.config(maximum=len(files), value=0)
        # daemon-поток: не держит процесс при закрытии окна
        threading.Thread(target=self._batch_worker, args=(files, out_dir),
                         daemon=True).start()

    def _batch_worker(self, files, out_dir):
        """Фон: обработать все файлы. Виджеты трогаем ТОЛЬКО через root.after (Tk не потокобезопасен)."""
        total = len(files)
        n = 0
        errs = []
        for i, f in enumerate(files, 1):
            name = os.path.basename(f)
            kind = ""
            try:
                img = Image.open(f).convert("RGB")
                out, _, key = process(img)          # авто-детект зелёный/синий на каждую
                kind = "синий" if key == 2 else "зелёный"
                stem = os.path.splitext(name)[0]
                # compress_level=3 -> быстрее кодирование PNG (без потери качества)
                out.save(os.path.join(out_dir, stem + "_key.png"), compress_level=3)
                n += 1
                del img, out
            except Exception as e:
                errs.append(f"{name}: {e}")
            if i % 4 == 0:            # держим память ровной на длинных пачках (200+ фото)
                gc.collect()
            self.root.after(0, self._batch_progress, i, total, name, kind)
        gc.collect()
        self.root.after(0, self._batch_done, n, total, out_dir, errs)

    def _batch_progress(self, i, total, name, kind):
        self.prog.config(value=i)
        self.status.config(text=f"Пакет: {i}/{total}  ({kind})  — {name}")

    def _batch_done(self, n, total, out_dir, errs):
        self._batch_running = False
        self.batch_btn.config(state="normal", text="Папка → вырезать всё")
        self.prog.config(value=0)
        msg = f"Готово: {n} из {total} картинок.\n\nПапка вывода:\n{out_dir}"
        if errs:
            msg += "\n\nНе удалось:\n" + "\n".join(errs[:8])
            if len(errs) > 8:
                msg += f"\n…и ещё {len(errs) - 8}"
        self.status.config(text=f"Пакет завершён: {n}/{total} → {out_dir}")
        messagebox.showinfo("Пакетная обработка", msg)

    # ---------- облако (rclone): Яндекс.Диск / Облако Mail.ru ----------
    def _ui(self, fn):
        """Выполнить обновление виджета в главном потоке (Tk не потокобезопасен)."""
        self.root.after(0, fn)

    def cloud_folder_auto(self):
        """Скачать папку из облака по rclone, убрать фон, залить результат обратно
        в датированную папку и (по галочке) выдать публичную ссылку."""
        if self._batch_running:
            return
        src = self.cloud_src.get().strip().rstrip("/")
        if ":" not in src or not src.split(":", 1)[1]:
            messagebox.showinfo("Облако",
                                "Укажите папку вида  remote:путь\n"
                                "Например:  yandex:Принты   или   mailru:foto")
            return
        if not rclone_available():
            messagebox.showerror(
                "rclone не найден",
                "Не найден rclone.\n\n"
                "1) Скачайте с rclone.org/downloads (положите rclone.exe в PATH)\n"
                "2) Настройте аккаунты:  rclone config  (yandex / mailru)\n"
                "3) Проверьте:  rclone listremotes")
            return
        self._batch_running = True
        self.cloud_btn.config(state="disabled", text="Работаю с облаком…")
        self.batch_btn.config(state="disabled")
        self.prog.config(value=0)
        threading.Thread(target=self._cloud_worker,
                         args=(src, self.cloud_link.get()), daemon=True).start()

    def _cloud_worker(self, src, want_link):
        tmp = tempfile.mkdtemp(prefix="greenkey_")
        tmp_in = os.path.join(tmp, "in")
        tmp_out = os.path.join(tmp, "out")
        os.makedirs(tmp_in)
        os.makedirs(tmp_out)
        try:
            # --- 1) скачать картинки из облака (только верхний уровень) ---
            self._ui(lambda: self.status.config(text=f"Облако: скачивание из {src}…"))
            r = _run_rclone(["copy", src, tmp_in, "--max-depth", "1", *rclone_img_includes()])
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip() or "не удалось скачать (rclone copy)")
            files = sorted(os.path.join(tmp_in, f) for f in os.listdir(tmp_in)
                           if f.lower().endswith(IMG_EXT)
                           and os.path.isfile(os.path.join(tmp_in, f)))
            if not files:
                self._ui(lambda: (self._cloud_reset(),
                                  self.status.config(text="Облако: картинок не найдено"),
                                  messagebox.showinfo("Облако",
                                                      f"В папке {src} нет картинок.")))
                return
            # --- 2) убрать фон локально ---
            total = len(files)
            n = 0
            errs = []
            self._ui(lambda: self.prog.config(maximum=total, value=0))
            for i, f in enumerate(files, 1):
                name = os.path.basename(f)
                try:
                    img = Image.open(f).convert("RGB")
                    out, _, _ = process(img)
                    stem = os.path.splitext(name)[0]
                    out.save(os.path.join(tmp_out, stem + "_key.png"), compress_level=3)
                    n += 1
                    del img, out
                except Exception as e:
                    errs.append(f"{name}: {e}")
                if i % 4 == 0:
                    gc.collect()
                self._ui(lambda i=i, name=name: (
                    self.prog.config(value=i),
                    self.status.config(text=f"Облако: обработка {i}/{total} — {name}")))
            gc.collect()
            if n == 0:
                raise RuntimeError("ни одной картинки не удалось обработать")
            # --- 3) залить результат в датированную папку облака ---
            self._ui(lambda: self.status.config(text="Облако: загрузка результата…"))
            dest = f"{src}/{next_dated_name(rclone_list_dirs(src))}"
            r2 = _run_rclone(["copy", tmp_out, dest])
            if r2.returncode != 0:
                raise RuntimeError(r2.stderr.strip() or "не удалось загрузить (rclone copy)")
            link = ""
            if want_link:
                rl = _run_rclone(["link", dest])
                if rl.returncode == 0:
                    link = rl.stdout.strip()
            self._ui(lambda: self._cloud_done(n, total, dest, link, errs))
        except Exception as e:
            msg = str(e)
            self._ui(lambda: self._cloud_error(msg))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _cloud_reset(self):
        self._batch_running = False
        self.cloud_btn.config(state="normal", text="Облако → вырезать всё")
        self.batch_btn.config(state="normal")
        self.prog.config(value=0)

    def _cloud_done(self, n, total, dest, link, errs=()):
        self._cloud_reset()
        msg = f"Готово: {n} из {total} картинок.\n\nВыгружено в облако:\n{dest}"
        if link:
            msg += f"\n\nСсылка на результат:\n{link}"
        if errs:
            msg += "\n\nНе удалось:\n" + "\n".join(list(errs)[:6])
            if len(errs) > 6:
                msg += f"\n…и ещё {len(errs) - 6}"
        self.status.config(text=f"Облако готово: {n}/{total} → {dest}")
        messagebox.showinfo("Облако", msg)

    def _cloud_error(self, msg):
        self._cloud_reset()
        self.status.config(text="Облако: ошибка")
        messagebox.showerror("Облако (rclone)", "Не удалось:\n" + msg)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
