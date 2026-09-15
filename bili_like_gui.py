# -*- coding: utf-8 -*-
"""
B站直播自动点赞 - 图形界面版 (原生窗口)
UI: Canvas 自绘控件 —— 渐变头部 / 圆角卡片 / 圆角按钮 / 圆角日志面板
     + 深色模式 (带过渡动画) + 动效 (淡入、悬停渐变、状态呼吸、日志高亮)
功能与交互逻辑保持不变。

依赖: pip install requests "qrcode[pil]"
"""

import json
import queue
import sys
import threading
import time

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, scrolledtext

import bili_like_api as core
from ui_kit import blend, color_distance, gradient, round_rect, shadow

try:
    import qrcode
except ImportError:
    qrcode = None

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None


# ============================================================
#  调色板 (浅色 / 深色)
# ============================================================
PALETTES = {
    "light": {
        "BG": "#EEF0F3", "CARD": "#FFFFFF", "CARD_ALT": "#F6F7F9",
        "BORDER": "#E4E7EB", "BORDER_STRONG": "#D3D7DC",
        "TEXT": "#1B1F24", "TEXT_2": "#596066", "TEXT_3": "#8B9298",
        "PRIMARY": "#FB7299", "PRIMARY_HOVER": "#F45C87", "PRIMARY_PRESS": "#DE466D",
        "PRIMARY_SOFT": "#FFF0F4", "PRIMARY_SOFT_HOVER": "#FFE2EA", "ON_PRIMARY": "#FFFFFF",
        "SUCCESS": "#2FA84F", "SUCCESS_DIM": "#7FD79A",
        "WARN": "#C97D10", "DANGER": "#DC3D42",
        "DANGER_SOFT": "#FDECEE", "DANGER_SOFT_HOVER": "#FADDE0",
        "DISABLED_BG": "#EFF0F2", "DISABLED_FG": "#A6ACB4",
        "HEADER_FROM": "#FF8FB2", "HEADER_TO": "#EE4E77", "STRIP": "#E0456C",
        "STRIP_DOT": "#B6F2C8", "STRIP_DOT_OFF": "#FFD2DF",
        "LOG_BG": "#14161B", "LOG_FG": "#D7DAE0", "LOG_SB": "#4C5563",
        "LOG_OK": "#63D38F", "LOG_ERR": "#FF8085", "LOG_WARN": "#EFC469",
        "LOG_DIM": "#78808B", "LOG_FLASH": "#FFFFFF",
        "WELL": "#F6F7F9",
        "SOFT_BTN": "#FFFFFF", "SOFT_BTN_HOVER": "#FFF3F7", "SOFT_BTN_ACTIVE": "#FFE5ED",
        "SOFT_BTN_FG": "#DE466D",
        "GHOST_BTN": "#F6F7F9", "GHOST_BTN_HOVER": "#E9EBEE", "GHOST_BTN_ACTIVE": "#E1E4E8",
    },
    "dark": {
        "BG": "#0F1115", "CARD": "#181B21", "CARD_ALT": "#20242C",
        "BORDER": "#2A2F38", "BORDER_STRONG": "#39404B",
        "TEXT": "#E9EBEF", "TEXT_2": "#A7AEBA", "TEXT_3": "#7C8593",
        "PRIMARY": "#FB7299", "PRIMARY_HOVER": "#FF8AAD", "PRIMARY_PRESS": "#E8608A",
        "PRIMARY_SOFT": "#2C1E24", "PRIMARY_SOFT_HOVER": "#3A2630", "ON_PRIMARY": "#FFFFFF",
        "SUCCESS": "#4FC97A", "SUCCESS_DIM": "#2E7A4B",
        "WARN": "#E0A63F", "DANGER": "#FF7B80",
        "DANGER_SOFT": "#2E1D1F", "DANGER_SOFT_HOVER": "#3B2427",
        "DISABLED_BG": "#23272F", "DISABLED_FG": "#6B7280",
        "HEADER_FROM": "#C2416A", "HEADER_TO": "#7E2843", "STRIP": "#6E2339",
        "STRIP_DOT": "#7FE0A4", "STRIP_DOT_OFF": "#C9748F",
        "LOG_BG": "#0A0C10", "LOG_FG": "#CED3DA", "LOG_SB": "#3A424E",
        "LOG_OK": "#5FC98A", "LOG_ERR": "#FF7B80", "LOG_WARN": "#E3B45F",
        "LOG_DIM": "#6C7583", "LOG_FLASH": "#FFFFFF",
        "WELL": "#20242C",
        "SOFT_BTN": "#FFFFFF", "SOFT_BTN_HOVER": "#FFF3F7", "SOFT_BTN_ACTIVE": "#FFE5ED",
        "SOFT_BTN_FG": "#C7395F",
        "GHOST_BTN": "#20242C", "GHOST_BTN_HOVER": "#2A2F38", "GHOST_BTN_ACTIVE": "#333944",
    },
}


class C:
    """当前生效的颜色 (由 set_palette 填充, 动画期间逐帧变化)"""


def set_palette(name, t=1.0, base=None):
    pal = PALETTES[name]
    for k, v in pal.items():
        cur = base.get(k, v) if base else v
        setattr(C, k, blend(cur, v, t) if t < 1.0 else v)


set_palette("light")


class S:
    XS, SM, MD, LG, XL = 4, 8, 12, 20, 28


class F:
    BRAND = ("Microsoft YaHei UI", 15, "bold")
    BRAND_SUB = ("Microsoft YaHei UI", 8)
    SECTION = ("Microsoft YaHei UI", 10, "bold")
    BODY = ("Microsoft YaHei UI", 10)
    BTN = ("Microsoft YaHei UI", 10, "bold")
    BTN_SM = ("Microsoft YaHei UI", 9, "bold")
    LABEL = ("Microsoft YaHei UI", 9)
    CAPTION = ("Microsoft YaHei UI", 8)
    MONO = ("Consolas", 9)
    QR_TIP = ("Microsoft YaHei UI", 10, "bold")


# ---- 主题切换时按颜色值重映射 (用于内联创建的原生控件) ----
COLOR_OPTS = ("bg", "fg", "activebackground", "activeforeground", "troughcolor",
              "highlightbackground", "highlightcolor", "insertbackground",
              "selectbackground", "selectforeground")


def iter_widgets(w):
    yield w
    for c in w.winfo_children():
        yield from iter_widgets(c)


def _col(role):
    """按调色板角色取色 (主题切换后自动跟随); 传入字面颜色则原样返回"""
    return getattr(C, role) if isinstance(role, str) and role in PALETTES["light"] else role


def remap_colors(root, mapping):
    for w in iter_widgets(root):
        for opt in COLOR_OPTS:
            try:
                cur = w.cget(opt)
            except Exception:
                continue
            if isinstance(cur, str) and cur.lower() in mapping:
                new = mapping[cur.lower()]
                if new != cur:
                    try:
                        w.configure(**{opt: new})
                    except Exception:
                        pass
        fn = getattr(w, "apply_theme", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


# ============================================================
#  自绘控件
# ============================================================
class RButton(tk.Canvas):
    """圆角按钮: 默认 / 悬停 / 按下 / 禁用 四态, 悬停带颜色过渡"""

    def __init__(self, master, text, command=None, kind="primary", height=38,
                 radius=10, font=None, parent_bg=None, min_width=84, pad_x=18):
        self._text = text
        self._command = command
        self._kind = kind
        self._font = font or F.BTN
        self._state = "normal"
        self._enabled = True
        self._radius = radius
        self._anim = None
        self._fill_now = None
        f = tkfont.Font(family=self._font[0], size=self._font[1],
                        weight=self._font[2] if len(self._font) > 2 else "normal")
        w = max(min_width, f.measure(text) + pad_x * 2)
        super().__init__(master, width=w, height=height,
                         bg=parent_bg or C.CARD, highlightthickness=0, bd=0)
        self._parent_bg = parent_bg or C.CARD
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    # ---------- 调色 ----------
    def _palette(self):
        P = {
            "primary": {
                "normal": (C.PRIMARY, C.ON_PRIMARY, ""),
                "hover": (C.PRIMARY_HOVER, C.ON_PRIMARY, ""),
                "active": (C.PRIMARY_PRESS, C.ON_PRIMARY, ""),
                "disabled": (C.DISABLED_BG, C.DISABLED_FG, ""),
            },
            "soft": {
                "normal": (C.SOFT_BTN, C.SOFT_BTN_FG, ""),
                "hover": (C.SOFT_BTN_HOVER, C.SOFT_BTN_FG, ""),
                "active": (C.SOFT_BTN_ACTIVE, C.SOFT_BTN_FG, ""),
                "disabled": (blend(C.SOFT_BTN, C.BG, 0.25), blend(C.SOFT_BTN_FG, C.SOFT_BTN, 0.5), ""),
            },
            "danger": {
                "normal": (C.DANGER_SOFT, C.DANGER, ""),
                "hover": (C.DANGER_SOFT_HOVER, C.DANGER, ""),
                "active": (blend(C.DANGER_SOFT_HOVER, C.DANGER, 0.18), C.DANGER, ""),
                "disabled": (C.DISABLED_BG, C.DISABLED_FG, ""),
            },
            "ghost": {
                "normal": (C.GHOST_BTN, C.TEXT_2, C.BORDER),
                "hover": (C.GHOST_BTN_HOVER, C.TEXT, C.BORDER_STRONG),
                "active": (C.GHOST_BTN_ACTIVE, C.TEXT, C.BORDER_STRONG),
                "disabled": (C.DISABLED_BG, C.DISABLED_FG, C.BORDER),
            },
        }
        return P[self._kind][self._state]

    # ---------- 交互 ----------
    def _on_enter(self, _=None):
        if self._enabled:
            self.configure(cursor="hand2")
            self._set_state("hover")

    def _on_leave(self, _=None):
        if self._enabled:
            self.configure(cursor="")
            self._set_state("normal")

    def _on_press(self, _=None):
        if self._enabled:
            self._set_state("active", animate=False)

    def _on_release(self, e=None):
        if not self._enabled:
            return
        inside = 0 <= (e.x if e else 0) <= self.winfo_width() and \
                 0 <= (e.y if e else 0) <= self.winfo_height()
        self._set_state("hover" if inside else "normal")
        if inside and callable(self._command):
            self._command()

    def _set_state(self, state, animate=True):
        target_bg = self._palette_for(state)[0]
        if animate and self._state != state and self._fill_now:
            self._state = state
            self._animate_fill(target_bg)
        else:
            self._state = state
            self._fill_now = target_bg
            self._draw()

    def _palette_for(self, state):
        keep, self._state = self._state, state
        res = self._palette()
        self._state = keep
        return res

    def _animate_fill(self, target, steps=5):
        if self._anim:
            try:
                self.after_cancel(self._anim)
            except Exception:
                pass
            self._anim = None
        start = self._fill_now
        i = [0]

        def step():
            i[0] += 1
            self._fill_now = blend(start, target, i[0] / steps)
            self._draw()
            if i[0] < steps:
                self._anim = self.after(16, step)
            else:
                self._fill_now = None
                self._anim = None

        step()

    def set_enabled(self, flag):
        self._enabled = bool(flag)
        self._state = "normal" if flag else "disabled"
        self._fill_now = None
        self.configure(cursor="hand2" if flag else "")
        self._draw()

    def set_text(self, text):
        self._text = text
        self._draw()

    def set_parent_bg(self, color):
        self._parent_bg = color
        if color and color != self.cget("bg"):
            self.configure(bg=color)
            self._draw()

    def apply_theme(self):
        self.configure(bg=self._parent_bg)
        self._fill_now = None
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        bg, fg, border = self._palette()
        if self._fill_now:
            bg = self._fill_now
        round_rect(self, 0, 0, w - 1, h - 1, self._radius, fill=bg,
                   outline=border or bg, width=1 if border else 0)
        self.create_text(w / 2, h / 2 + 1, text=self._text, fill=fg, font=self._font)


class Card(tk.Frame):
    """圆角卡片 (Canvas 画底 + 投影, 内容自然撑高)"""

    def __init__(self, master, title=None, parent_role="BG", radius=14,
                 pad=S.LG, glass=6):
        super().__init__(master, bg=_col(parent_role))
        self._parent_role = parent_role
        self._radius = radius
        self._glass = glass
        self.cv = tk.Canvas(self, bg=_col(parent_role), highlightthickness=0, bd=0)
        self.cv.place(x=0, y=0, relwidth=1, relheight=1)
        self.cv.bind("<Configure>", lambda e: self._redraw())

        self.inner = tk.Frame(self, bg=C.CARD)
        self.inner.pack(fill="both", expand=True,
                        padx=(glass + pad, glass + pad),
                        pady=(glass + pad, glass + pad + 4))
        self.title_label = None
        self.divider = None
        if title:
            self.title_label = tk.Label(self.inner, text=title, bg=C.CARD,
                                        fg=C.TEXT, font=F.SECTION, anchor="w")
            self.title_label.pack(fill="x")
            self.divider = tk.Frame(self.inner, bg=C.BORDER, height=1)
            self.divider.pack(fill="x", pady=(S.MD, S.MD))
        self.body = tk.Frame(self.inner, bg=C.CARD)
        self.body.pack(fill="both", expand=True)
        self.bind("<Configure>", lambda e: self._redraw())

    def apply_theme(self):
        self.configure(bg=_col(self._parent_role))
        self.cv.configure(bg=_col(self._parent_role))
        self.inner.configure(bg=C.CARD)
        self.body.configure(bg=C.CARD)
        if self.title_label:
            self.title_label.configure(bg=C.CARD, fg=C.TEXT)
        if self.divider:
            self.divider.configure(bg=C.BORDER)
        self._redraw()

    def _redraw(self):
        try:
            w, h = self.winfo_width(), self.winfo_height()
        except Exception:
            return
        if w < 8 or h < 8:
            return
        g = self._glass
        parent = _col(self._parent_role)
        self.cv.delete("all")
        shadow(self.cv, g, 2, w - g, h - g - 2, self._radius, parent)
        round_rect(self.cv, g, 0, w - g, h - g - 2, self._radius,
                   fill=C.CARD, outline=C.BORDER, width=1)


class Panel(tk.Frame):
    """圆角内嵌面板 (次级底色)"""

    def __init__(self, master, parent_role="CARD", radius=11, pad=S.MD):
        super().__init__(master, bg=_col(parent_role))
        self._parent_role = parent_role
        self._radius = radius
        self.cv = tk.Canvas(self, bg=_col(parent_role), highlightthickness=0, bd=0)
        self.cv.place(x=0, y=0, relwidth=1, relheight=1)
        self.cv.bind("<Configure>", lambda e: self._redraw())
        self.body = tk.Frame(self, bg=C.CARD_ALT)
        self.body.pack(fill="both", expand=True, padx=pad, pady=pad)
        self.bind("<Configure>", lambda e: self._redraw())

    def apply_theme(self):
        self.configure(bg=_col(self._parent_role))
        self.cv.configure(bg=_col(self._parent_role))
        self.body.configure(bg=C.CARD_ALT)
        self._redraw()

    def _redraw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 8 or h < 8:
            return
        self.cv.delete("all")
        round_rect(self.cv, 0, 0, w - 1, h - 1, self._radius,
                   fill=C.CARD_ALT, outline=C.BORDER, width=1)


class Console(tk.Frame):
    """圆角深色日志面板 + 自绘细滚动条"""

    def __init__(self, master, parent_role="CARD", radius=12):
        super().__init__(master, bg=_col(parent_role))
        self._parent_role = parent_role
        self.cv = tk.Canvas(self, bg=_col(parent_role), highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Configure>", lambda e: self._redraw())
        self.txt = scrolledtext.ScrolledText(
            self, height=12, state="disabled", font=F.MONO,
            bg=C.LOG_BG, fg=C.LOG_FG, relief="flat", bd=0,
            insertbackground=C.LOG_FG, padx=S.MD, pady=S.MD,
            selectbackground=C.PRIMARY_PRESS, selectforeground="#FFFFFF")
        self.txt.place(x=S.MD, y=S.MD, relwidth=1, relheight=1,
                       width=-2 * S.MD, height=-2 * S.MD)
        try:
            self.txt.vbar.pack_forget()      # scrolledtext 内部是 pack
        except Exception:
            pass
        self._sb_drag = None
        self.cv.bind("<ButtonPress-1>", self._sb_press)
        self.cv.bind("<B1-Motion>", self._sb_drag_move)
        self.cv.bind("<ButtonRelease-1>", lambda e: setattr(self, "_sb_drag", None))
        self.txt.bind("<MouseWheel>", lambda e: self.after(20, self._draw_scrollbar))
        self.txt.bind("<KeyRelease>", lambda e: self._draw_scrollbar())
        self.txt.bind("<Configure>", lambda e: self._draw_scrollbar())
        self._apply_tags()

    def _apply_tags(self):
        self.txt.tag_config("ok", foreground=C.LOG_OK)
        self.txt.tag_config("err", foreground=C.LOG_ERR)
        self.txt.tag_config("warn", foreground=C.LOG_WARN)
        self.txt.tag_config("dim", foreground=C.LOG_DIM)
        self.txt.tag_config("flash", foreground=C.LOG_FLASH)

    def apply_theme(self):
        self.configure(bg=_col(self._parent_role))
        self.cv.configure(bg=_col(self._parent_role))
        self.txt.configure(bg=C.LOG_BG, fg=C.LOG_FG, insertbackground=C.LOG_FG,
                           selectbackground=C.PRIMARY_PRESS)
        self._apply_tags()
        self._redraw()

    # ---- 滚动条 ----
    def _sb_geom(self):
        h = self.cv.winfo_height()
        top, bottom = self.txt.yview()
        if bottom - top >= 0.999:
            return None
        y1 = 10 + (h - 20) * top
        y2 = 10 + (h - 20) * bottom
        return y1, max(y1 + 18, y2)

    def _draw_scrollbar(self):
        self.cv.delete("sb")
        g = self._sb_geom()
        if not g:
            return
        w = self.cv.winfo_width()
        round_rect(self.cv, w - 13, g[0], w - 6, g[1], 3,
                   fill=C.LOG_SB, outline="", tags="sb")

    def _sb_press(self, e):
        self._sb_drag = e.y
        self._sb_jump(e.y)

    def _sb_drag_move(self, e):
        if self._sb_drag is not None:
            self._sb_jump(e.y)

    def _sb_jump(self, y):
        h = self.cv.winfo_height()
        top, bottom = self.txt.yview()
        span = bottom - top
        frac = (y - 10) / max(1, h - 20) - span / 2
        self.txt.yview_moveto(max(0.0, min(1.0 - span, frac)))
        self._draw_scrollbar()

    def _redraw(self):
        w, h = self.cv.winfo_width(), self.cv.winfo_height()
        if w < 8 or h < 8:
            return
        self.cv.delete("all")
        round_rect(self.cv, 0, 0, w - 1, h - 1, 12, fill=C.LOG_BG, outline="")
        self._draw_scrollbar()


class Header(tk.Frame):
    """品牌头: 横向渐变 + 圆角按钮"""

    HEIGHT = 96

    def __init__(self, master, on_qr, on_cookie, on_theme):
        super().__init__(master, bg=C.HEADER_FROM)
        self.cv = tk.Canvas(self, height=self.HEIGHT, bg=C.HEADER_FROM,
                            highlightthickness=0, bd=0)
        self.cv.pack(fill="x")
        self.cv.bind("<Configure>", lambda e: self._redraw())
        self.btn_theme = RButton(self, "深色", on_theme, kind="soft", height=38,
                                 radius=10, font=F.BTN_SM, parent_bg=C.HEADER_TO,
                                 min_width=68, pad_x=14)
        self.btn_qr = RButton(self, "扫码登录", on_qr, kind="soft", height=38,
                              radius=10, font=F.BTN_SM, parent_bg=C.HEADER_TO)
        self.btn_cookie = RButton(self, "粘贴 Cookie", on_cookie, kind="soft",
                                  height=38, radius=10, font=F.BTN_SM,
                                  parent_bg=C.HEADER_TO)

    def apply_theme(self):
        self.configure(bg=C.HEADER_FROM)
        self.cv.configure(bg=C.HEADER_FROM)
        self._redraw()

    def _grad_color(self, x, width):
        w = max(1, width)
        return blend(C.HEADER_FROM, C.HEADER_TO, max(0.0, min(1.0, x / w)))

    def _redraw(self):
        w = self.cv.winfo_width()
        if w < 20:
            return
        self.cv.delete("all")
        gradient(self.cv, 0, 0, w, self.HEIGHT, C.HEADER_FROM, C.HEADER_TO)
        tile = blend(self._grad_color(28, w), "#FFFFFF", 0.2)
        round_rect(self.cv, 28, 26, 72, 70, 13, fill=tile, outline="")
        self.cv.create_text(50, 49, text="👍", font=("Segoe UI Emoji", 17))
        self.cv.create_text(86, 42, text="BiliLiveLike", anchor="w",
                            fill="#FFFFFF", font=F.BRAND)
        self.cv.create_text(86, 63, text="直播间自动点赞 · 纯接口后台运行", anchor="w",
                            fill="#FFE3EC", font=F.BRAND_SUB)
        btns = [self.btn_theme, self.btn_qr, self.btn_cookie]
        widths = [int(b.cget("width")) for b in btns]
        x = w - 28 - sum(widths) - 10 * (len(btns) - 1)
        y = (self.HEIGHT - 38) // 2
        for b, bw in zip(btns, widths):
            b.set_parent_bg(self._grad_color(x + bw / 2, w))
            b.place(x=x, y=y)
            x += bw + 10


class Strip(tk.Frame):
    """账号状态条"""

    def __init__(self, master):
        super().__init__(master, bg=C.STRIP)
        self.inner = tk.Frame(self, bg=C.STRIP)
        self.inner.pack(fill="x", padx=S.XL, pady=S.SM)
        self.dot = tk.Label(self.inner, text="●", bg=C.STRIP, fg=C.STRIP_DOT_OFF,
                            font=("Segoe UI", 9))
        self.dot.pack(side="left")
        self.text = tk.Label(self.inner, text="正在检查登录状态…", bg=C.STRIP,
                             fg="#FFFFFF", font=F.LABEL)
        self.text.pack(side="left", padx=(S.XS, 0))
        self.hint = tk.Label(self.inner, text="点赞走的接口与手动双击点赞一致",
                             bg=C.STRIP, fg="#FFC9D9", font=F.CAPTION)
        self.hint.pack(side="right")

    def apply_theme(self):
        self.configure(bg=C.STRIP)
        self.inner.configure(bg=C.STRIP)
        for w in (self.dot, self.text, self.hint):
            w.configure(bg=C.STRIP)
        self.dot.configure(fg=C.STRIP_DOT_OFF)


class QueueWriter:
    def __init__(self, q: queue.Queue):
        self.q = q
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.q.put(("log", line))

    def flush(self):
        pass


# ============================================================
#  弹窗
# ============================================================
class BaseDialog(tk.Toplevel):
    def __init__(self, master, title, width=460, height=380):
        super().__init__(master, bg=C.CARD)
        self.title(title)
        self.resizable(False, False)
        self.cv = tk.Canvas(self, width=width, height=height, bg=C.CARD,
                            highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Configure>", lambda e: self._paint())
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.transient(master)
        self.grab_set()
        self.attributes("-alpha", 0.0)
        self._fade(0)

    def _fade(self, i, steps=8):
        try:
            self.attributes("-alpha", min(1.0, (i + 1) / steps))
        except Exception:
            return
        if i + 1 < steps:
            self.after(16, lambda: self._fade(i + 1, steps))

    def _paint(self):
        w, h = self.cv.winfo_width(), self.cv.winfo_height()
        if w < 20 or h < 20:
            return
        self.cv.delete("all")
        gradient(self.cv, 0, 0, w, 58, C.HEADER_FROM, C.HEADER_TO)
        self.cv.create_text(20, 30, text=self.title(), anchor="w",
                            fill="#FFFFFF", font=("Microsoft YaHei UI", 12, "bold"))

    def _center(self, master):
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class QRLoginDialog(BaseDialog):
    def __init__(self, master, app, evq):
        super().__init__(master, "扫码登录", 420, 420)
        self.app, self.evq = app, evq
        self._alive = True
        self.key = None

        self.qr_box = tk.Frame(self, bg=C.CARD_ALT, highlightbackground=C.BORDER,
                               highlightthickness=1)
        self.img_label = tk.Label(self.qr_box, bg=C.CARD_ALT, fg=C.TEXT_3,
                                  text="正在获取二维码…", font=F.LABEL,
                                  width=26, height=12)
        self.img_label.pack(padx=S.SM, pady=S.SM)
        self.tip = tk.Label(self, text="用 哔哩哔哩 App 扫描二维码", bg=C.CARD,
                            fg=C.TEXT, font=F.QR_TIP)
        self.sub = tk.Label(self, text="二维码有效期约 3 分钟", bg=C.CARD,
                            fg=C.TEXT_3, font=F.CAPTION)
        self.place_widgets()
        self.cv.bind("<Configure>", lambda e: (self._paint(), self.place_widgets()),
                     add="+")
        self._center(master)
        threading.Thread(target=self._worker, daemon=True).start()

    def place_widgets(self):
        self.qr_box.place(relx=0.5, y=84, anchor="n")
        self.tip.place(relx=0.5, y=352, anchor="n")
        self.sub.place(relx=0.5, y=380, anchor="n")

    def _worker(self):
        try:
            r = self.app.s.get(core.QR_GENERATE, timeout=10).json()
            if r.get("code") != 0:
                self.evq.put(("log", f"[login] 获取二维码失败: {r.get('message')}"))
                self.evq.put(("qr_done", False))
                return
            url, self.key = r["data"]["url"], r["data"]["qrcode_key"]
            img = qrcode.make(url).convert("RGB").resize((224, 224))
            self.evq.put(("qr_image", img))
            t0 = time.time()
            while self._alive and time.time() - t0 < 180:
                p = self.app.s.get(core.QR_POLL,
                                   params={"qrcode_key": self.key}, timeout=10).json()
                code = p.get("data", {}).get("code")
                if code == 0:
                    self.app._save_cookies()
                    ok, uname = self.app.logged_in()
                    self.evq.put(("log", f"[login] 登录成功！欢迎你，{uname}"))
                    self.evq.put(("account", (ok, uname)))
                    self.evq.put(("qr_done", True))
                    return
                if code == 86038:
                    self.evq.put(("log", "[login] 二维码已过期，请重新扫码"))
                    self.evq.put(("qr_status", ("二维码已过期，请重新扫码", C.WARN)))
                    self.evq.put(("qr_done", False))
                    return
                if code == 86090:
                    self.evq.put(("qr_status", ("已扫码，请在手机上确认", C.PRIMARY_PRESS)))
                time.sleep(2)
            if self._alive:
                self.evq.put(("qr_status", ("等待超时，请重新扫码", C.WARN)))
                self.evq.put(("qr_done", False))
        except Exception as e:
            self.evq.put(("log", f"[login] 出错: {e}"))
            self.evq.put(("qr_done", False))

    def _close(self):
        self._alive = False
        super()._close()


class CookieDialog(BaseDialog):
    def __init__(self, master, app, evq):
        super().__init__(master, "粘贴 Cookie 登录", 560, 400)
        self.app, self.evq = app, evq
        self.hint = tk.Label(self, text="浏览器登录 bilibili.com 后：F12 → 网络 → 任一请求\n"
                                        "→ 请求标头 → 复制 Cookie 整行粘贴到下方",
                             justify="left", bg=C.CARD, fg=C.TEXT_2, font=F.LABEL)
        self.hint.place(x=S.LG, y=76)
        self.txt = tk.Text(self, width=64, height=8, wrap="char", font=F.MONO,
                           bg=C.CARD_ALT, fg=C.TEXT, relief="flat", bd=0,
                           insertbackground=C.PRIMARY, padx=S.MD, pady=S.MD,
                           highlightbackground=C.BORDER, highlightthickness=1)
        self.txt.place(x=S.LG, y=134, relwidth=1, width=-2 * S.LG, height=170)
        self.btn_save = RButton(self, "保存并登录", self._save, kind="primary",
                                height=38, radius=10, font=F.BTN_SM)
        self.btn_cancel = RButton(self, "取消", self._close, kind="ghost",
                                  height=38, radius=10, font=F.BTN_SM)
        self.place_buttons()
        self.cv.bind("<Configure>", lambda e: (self._paint(), self.place_buttons()),
                     add="+")
        self._center(master)

    def place_buttons(self):
        w = self.cv.winfo_width() or 560
        bw_s = int(self.btn_save.cget("width"))
        bw_c = int(self.btn_cancel.cget("width"))
        self.btn_save.place(x=w - S.LG - bw_s, y=326)
        self.btn_cancel.place(x=w - S.LG - bw_s - 10 - bw_c, y=326)

    def _save(self):
        raw = self.txt.get("1.0", "end").strip()
        if not raw:
            return
        n = self.app.set_cookie_string(raw)
        ok, uname = self.app.logged_in()
        self.evq.put(("log", f"[cookie] 已读取 {n} 个字段，登录状态: "
                             f"{uname if ok else '无效，请检查是否复制完整'}"))
        self.evq.put(("account", (ok, uname)))
        if ok:
            self._close()
        else:
            messagebox.showwarning("登录失败", "Cookie 无效或已过期，请重新复制完整的 Cookie",
                                   parent=self)


# ============================================================
#  主窗口
# ============================================================
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.evq: queue.Queue = queue.Queue()
        self.app = None
        self._qr_dialog = None
        self._welcomed = False
        self._wide = None
        self._pulse = None
        self._theme_anim = None
        cfg = core.load_config()
        self.theme = cfg.get("theme", "light")
        set_palette(self.theme)
        self._build_ui()
        self._poll()
        self._fade_in()
        threading.Thread(target=self._init_backend, daemon=True).start()

    # ---------- 窗口淡入 ----------
    def _fade_in(self, i=0, steps=10):
        try:
            self.root.attributes("-alpha", min(1.0, (i + 1) / steps))
        except Exception:
            return
        if i + 1 < steps:
            self.root.after(18, lambda: self._fade_in(i + 1, steps))

    # ---------- 布局 ----------
    def _build_ui(self):
        r = self.root
        r.title("BiliLiveLike · B站直播自动点赞")
        r.geometry("940x680")
        r.minsize(620, 560)
        r.configure(bg=C.BG)
        try:
            r.attributes("-alpha", 0.0)
        except Exception:
            pass

        self.header = Header(r, self._qr_login, self._cookie_login, self._toggle_theme)
        self.header.pack(fill="x")
        self.strip = Strip(r)
        self.strip.pack(fill="x")
        self._prime_account_strip()

        main = tk.Frame(r, bg=C.BG)
        self.main = main

        # --- 控制卡片 ---
        ctrl = Card(main, title="直播间", parent_role="BG")
        self.ctrl_card = ctrl
        b = ctrl.body
        self.lbl_room = tk.Label(b, text="房间号 / 直播间链接", bg=C.CARD,
                                 fg=C.TEXT_2, font=F.LABEL)
        self.lbl_room.pack(anchor="w")
        line = tk.Frame(b, bg=C.CARD)
        line.pack(fill="x", pady=(S.SM, 0))
        self.room_var = tk.StringVar(value=core.load_config().get("room", ""))
        self.room_entry = ttk.Entry(line, textvariable=self.room_var,
                                    style="Modern.TEntry", font=F.BODY)
        self.room_entry.pack(side="left", fill="x", expand=True, ipady=3)
        self.start_btn = RButton(line, "开始点赞", self._start, kind="primary",
                                 height=40, radius=10, font=F.BTN)
        self.start_btn.pack(side="left", padx=(S.SM, 0))
        self.stop_btn = RButton(line, "停止", self._stop, kind="danger",
                                height=40, radius=10, font=F.BTN)
        self.stop_btn.set_enabled(False)
        self.stop_btn.pack(side="left", padx=(S.SM, 0))

        # --- 参数 ---
        phead = tk.Frame(b, bg=C.CARD)
        phead.pack(fill="x", pady=(S.LG, S.SM))
        self.lbl_params = tk.Label(phead, text="运行参数", bg=C.CARD,
                                   fg=C.TEXT_2, font=F.LABEL)
        self.lbl_params.pack(side="left")
        self.lbl_params_hint = tk.Label(phead, text="随机节奏，间隔越大越稳妥",
                                        bg=C.CARD, fg=C.TEXT_3, font=F.CAPTION)
        self.lbl_params_hint.pack(side="right")

        well = Panel(b, parent_role="CARD")
        well.pack(fill="x")
        cfg = core.load_config()
        self.iv_min = tk.StringVar(value=str(cfg.get("interval_min", 5.0)))
        self.iv_max = tk.StringVar(value=str(cfg.get("interval_max", 8.0)))
        self.ck_min = tk.StringVar(value=str(cfg.get("click_min", 10)))
        self.ck_max = tk.StringVar(value=str(cfg.get("click_max", 20)))
        self.max_likes = tk.StringVar(value=str(cfg.get("max_likes", 1000)))
        self._param(well.body, "请求间隔", self.iv_min, "秒", self.iv_max, "秒")
        self._param(well.body, "每次连击", self.ck_min, "赞", self.ck_max, "赞")
        self._param(well.body, "单场上限", self.max_likes, "赞", None, None)

        wait_row = tk.Frame(well.body, bg=C.CARD_ALT)
        wait_row.pack(fill="x")
        self.wait_live = tk.BooleanVar(value=bool(cfg.get("wait_live", True)))
        ttk.Checkbutton(wait_row, text="未开播时每分钟自动检查，开播后自动开始",
                        variable=self.wait_live,
                        style="Modern.TCheckbutton").pack(anchor="w")

        # --- 日志 ---
        logcard = Card(main, title="运行日志", parent_role="BG")
        self.log_card = logcard
        self.console = Console(logcard.body, parent_role="CARD")
        self.console.pack(fill="both", expand=True)
        self.log_txt = self.console.txt

        # --- 状态栏 ---
        self.bar = tk.Frame(r, bg=C.CARD, highlightbackground=C.BORDER,
                            highlightthickness=1)
        self.bar.pack(fill="x", side="bottom")
        bar_in = tk.Frame(self.bar, bg=C.CARD)
        self.bar_in = bar_in
        bar_in.pack(fill="x", padx=S.XL, pady=S.SM)
        self.state_dot = tk.Label(bar_in, text="●", bg=C.CARD, fg=C.TEXT_3,
                                  font=("Segoe UI", 9))
        self.state_dot.pack(side="left")
        self.state_label = tk.Label(bar_in, text="空闲", bg=C.CARD, fg=C.TEXT,
                                    font=F.LABEL)
        self.state_label.pack(side="left", padx=(S.XS, S.LG))
        self.metric_label = tk.Label(bar_in, text="已点赞 0 · 0 次请求",
                                     bg=C.CARD, fg=C.TEXT_2, font=F.LABEL)
        self.metric_label.pack(side="left")
        self.bar_hint = tk.Label(bar_in, text="主播开播时点赞才有效", bg=C.CARD,
                                 fg=C.TEXT_3, font=F.CAPTION)
        self.bar_hint.pack(side="right")

        main.pack(fill="both", expand=True, padx=S.XL, pady=(S.LG, S.MD))
        r.bind("<Configure>", self._on_resize)
        self._relayout(940)
        self._append_log("等待开始 · 填入房间号后点击「开始点赞」", "dim")
        r.protocol("WM_DELETE_WINDOW", self._on_close)

    def _param(self, parent, label, var_a, unit_a, var_b, unit_b):
        row = tk.Frame(parent, bg=C.CARD_ALT)
        row.pack(fill="x", pady=(0, S.SM))
        tk.Label(row, text=label, bg=C.CARD_ALT, fg=C.TEXT_2, font=F.LABEL,
                 width=8, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var_a, style="Modern.TEntry", width=6,
                  font=F.LABEL).pack(side="left", ipady=2)
        tk.Label(row, text=unit_a, bg=C.CARD_ALT, fg=C.TEXT_3,
                 font=F.CAPTION).pack(side="left", padx=(S.XS, S.SM))
        if var_b is not None:
            tk.Label(row, text="~", bg=C.CARD_ALT, fg=C.TEXT_3,
                     font=F.LABEL).pack(side="left")
            ttk.Entry(row, textvariable=var_b, style="Modern.TEntry", width=6,
                      font=F.LABEL).pack(side="left", padx=(S.SM, 0), ipady=2)
            tk.Label(row, text=unit_b, bg=C.CARD_ALT, fg=C.TEXT_3,
                     font=F.CAPTION).pack(side="left", padx=(S.XS, 0))

    # ---------- 主题 ----------
    def _toggle_theme(self):
        self.theme = "dark" if self.theme == "light" else "light"
        try:
            core.save_config({**core.load_config(), "theme": self.theme})
        except Exception:
            pass
        self.transition_theme()

    def transition_theme(self, steps=8):
        """逐帧插值调色板形成颜色过渡; 每帧把原生控件的旧颜色重映射到新颜色

        注意: 调色板里存在「同一颜色多个角色」的情况(如 #FFFFFF 既是卡片底色又是按钮白字),
        因此按色差最大的角色决定映射目标, 否则卡片底色会被"没变化的角色"覆盖掉。
        """
        if self._theme_anim:
            try:
                self.root.after_cancel(self._theme_anim)
            except Exception:
                pass
            self._theme_anim = None

        target = PALETTES[self.theme]
        keys = list(target.keys())
        start = {k: getattr(C, k) for k in keys}
        self.header.btn_theme.set_text("浅色" if self.theme == "dark" else "深色")
        prev = dict(start)

        def step(i):
            nonlocal prev
            t = i / steps
            cur = {k: blend(start[k], target[k], t) for k in keys}
            groups = {}
            for k in keys:
                groups.setdefault(prev[k].lower(), []).append(cur[k])
            mapping = {src: max(tvs, key=lambda tv: color_distance(src, tv))
                       for src, tvs in groups.items()}
            for k in keys:
                setattr(C, k, cur[k])
            self.root.configure(bg=C.BG)
            remap_colors(self.root, mapping)
            setup_styles(self.root)
            prev = cur
            if i < steps:
                self._theme_anim = self.root.after(20, lambda: step(i + 1))
            else:
                self._theme_anim = None
                set_palette(self.theme)
                # 收尾: 全量重绘自绘控件, 保证颜色精确落在目标调色板上
                for w in iter_widgets(self.root):
                    fn = getattr(w, "apply_theme", None)
                    if callable(fn):
                        try:
                            fn()
                        except Exception:
                            pass
                setup_styles(self.root)

        step(1)

    # ---------- 初始账号显示 ----------
    def _prime_account_strip(self):
        cached = core.load_config().get("last_uname", "")
        try:
            with open(core.COOKIE_PATH, "r", encoding="utf-8") as f:
                has_session = bool(json.load(f).get("SESSDATA"))
        except Exception:
            has_session = False
        if has_session:
            self.strip.text.config(text=f"已登录 · {cached}" if cached
                                   else "已找到本地登录信息，正在校验…")
            self.strip.dot.config(fg=C.STRIP_DOT if cached else C.STRIP_DOT_OFF)
        else:
            self.strip.text.config(text="未登录 · 请扫码或粘贴 Cookie")
            self.strip.dot.config(fg=C.STRIP_DOT_OFF)
        self.header.btn_theme.set_text("浅色" if self.theme == "dark" else "深色")

    def _on_resize(self, event):
        if event.widget is self.root:
            self._relayout(event.width)

    def _relayout(self, width):
        wide = width >= 820
        if wide == self._wide:
            return
        self._wide = wide
        m = self.main
        if wide:
            m.columnconfigure(0, weight=0, minsize=400)
            m.columnconfigure(1, weight=1)
            m.rowconfigure(0, weight=1)
            m.rowconfigure(1, weight=0)
            self.ctrl_card.grid(row=0, column=0, sticky="new", padx=(0, S.MD))
            self.log_card.grid(row=0, column=1, sticky="nsew")
        else:
            m.columnconfigure(0, weight=1, minsize=0)
            m.columnconfigure(1, weight=0, minsize=0)
            m.rowconfigure(0, weight=0)
            m.rowconfigure(1, weight=1)
            self.ctrl_card.grid(row=0, column=0, columnspan=2, sticky="ew",
                                pady=(0, S.MD))
            self.log_card.grid(row=1, column=0, columnspan=2, sticky="nsew")

    # ---------- 初始化 ----------
    def _init_backend(self):
        if core.requests is None:
            self.evq.put(("fatal", "缺少 requests 库，请执行：pip install requests \"qrcode[pil]\""))
            return
        self.app = core.BiliLikeApi(prefetch=False)   # 先跳过预取, 让状态尽快显示
        ok, uname = self.app.logged_in()
        self.evq.put(("account", (ok, uname)))
        if ok:
            try:
                self.app.cfg["last_uname"] = uname
                core.save_config(self.app.cfg)
            except Exception:
                pass
        self.app.prefetch()      # 后台补 buvid 指纹

    # ---------- 事件循环 ----------
    def _poll(self):
        try:
            while True:
                kind, payload = self.evq.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "account":
                    ok, uname = payload
                    if ok:
                        self.strip.text.config(text=f"已登录 · {uname}")
                        self.strip.dot.config(fg=C.STRIP_DOT)
                        if not self._welcomed:
                            self._welcomed = True
                            self._append_log(f"[init] 已就绪 · 已登录 {uname}", "dim")
                    else:
                        self.strip.text.config(text="未登录 · 请扫码或粘贴 Cookie")
                        self.strip.dot.config(fg=C.STRIP_DOT_OFF)
                        if not self._welcomed:
                            self._welcomed = True
                            self._append_log("[init] 未登录，请点右上角「扫码登录」或「粘贴 Cookie」", "err")
                elif kind == "qr_image" and ImageTk is not None:
                    if self._qr_dialog and self._qr_dialog.winfo_exists():
                        photo = ImageTk.PhotoImage(payload, master=self._qr_dialog)
                        self._qr_dialog.img_label.config(image=photo, text="")
                        self._qr_dialog.img_label.image = photo
                elif kind == "qr_status":
                    msg, color = payload
                    if self._qr_dialog and self._qr_dialog.winfo_exists():
                        self._qr_dialog.tip.config(text=msg, fg=color)
                elif kind == "qr_done":
                    if self._qr_dialog and self._qr_dialog.winfo_exists():
                        self._qr_dialog._close()
                    self._qr_dialog = None
                elif kind == "fatal":
                    messagebox.showerror("错误", payload)
                    self.root.destroy()
                    return
        except queue.Empty:
            pass

        running = False
        if self.app:
            s = self.app.stats
            running = self.app.running
            if running:
                if self._pulse is None:
                    self._start_pulse()
                self.state_label.config(text="运行中", fg=C.SUCCESS)
            else:
                self._stop_pulse()
                self.state_dot.config(fg=C.TEXT_3)
                self.state_label.config(text="空闲", fg=C.TEXT)
            dur = f" · 已运行 {int(time.time() - s['start'])}s" if (s.get("start") and running) else ""
            self.metric_label.config(text=f"已点赞 {s['likes']} · {s['clicks']} 次请求{dur}")
        self.start_btn.set_enabled(not running)
        self.stop_btn.set_enabled(running)
        self.root.after(400, self._poll)

    # ---- 运行中状态点呼吸动画 ----
    def _start_pulse(self):
        def beat(i=0):
            if not (self.app and self.app.running):
                self._pulse = None
                return
            shade = [C.SUCCESS, C.SUCCESS_DIM, C.SUCCESS]
            self.state_dot.config(fg=shade[i % 3])
            self._pulse = self.root.after(600, lambda: beat(i + 1))
        beat()

    def _stop_pulse(self):
        if self._pulse:
            try:
                self.root.after_cancel(self._pulse)
            except Exception:
                pass
            self._pulse = None

    # ---------- 日志 ----------
    def _append_log(self, line, tag=None):
        if tag is None:
            if "OK" in line and "赞" in line:
                tag = "ok"
            elif any(k in line for k in ("错误", "失败", "异常", "失效", "未登录")):
                tag = "err"
            elif any(k in line for k in ("风控", "放慢", "已过期", "上限")):
                tag = "warn"
            elif line.startswith(("[init]", "[ui]")):
                tag = "dim"
        self.log_txt.config(state="normal")
        start_idx = self.log_txt.index("end-1c")
        self.log_txt.insert("end", line + "\n", tag or "")
        self.log_txt.see("end")
        # 新行短暂高亮, 形成"刚写入"的视觉反馈
        self.log_txt.tag_add("flash", start_idx, f"{start_idx} lineend")
        self.root.after(450, lambda: self._unflash(start_idx))
        if int(self.log_txt.index("end-1c").split(".")[0]) > 800:
            self.log_txt.delete("1.0", "300.0")
        self.log_txt.config(state="disabled")
        self.console._draw_scrollbar()

    def _unflash(self, idx):
        try:
            self.log_txt.tag_remove("flash", idx, f"{idx} lineend")
        except Exception:
            pass

    # ---------- 动作 ----------
    def _qr_login(self):
        if core.qrcode is None or ImageTk is None:
            messagebox.showwarning("缺少依赖", "二维码功能需要 qrcode 和 pillow 库\n"
                                               "命令行执行：pip install \"qrcode[pil]\"\n"
                                               "或使用「粘贴 Cookie」方式登录")
            return
        if not self.app:
            messagebox.showinfo("稍等", "程序还在初始化，请稍候几秒再试")
            return
        self._qr_dialog = QRLoginDialog(self.root, self.app, self.evq)

    def _cookie_login(self):
        if not self.app:
            messagebox.showinfo("稍等", "程序还在初始化，请稍候几秒再试")
            return
        CookieDialog(self.root, self.app, self.evq)

    def _apply_settings(self):
        cfg = self.app.cfg
        try:
            cfg["interval_min"] = float(self.iv_min.get())
            cfg["interval_max"] = float(self.iv_max.get())
            cfg["click_min"] = int(self.ck_min.get())
            cfg["click_max"] = int(self.ck_max.get())
            cfg["max_likes"] = int(self.max_likes.get())
        except ValueError:
            messagebox.showwarning("参数不对", "间隔 / 连击 / 上限请填写数字")
            return False
        if cfg["interval_min"] <= 0 or cfg["interval_min"] > cfg["interval_max"]:
            messagebox.showwarning("参数不对", "间隔需要：最小间隔 > 0 且 ≤ 最大间隔")
            return False
        if cfg["click_min"] <= 0 or cfg["click_min"] > cfg["click_max"]:
            messagebox.showwarning("参数不对", "连击需要：下限 > 0 且 ≤ 上限")
            return False
        cfg["wait_live"] = bool(self.wait_live.get())
        cfg["theme"] = self.theme
        core.save_config(cfg)
        return True

    def _start(self):
        if not self.app:
            return
        room = core.normalize_room(self.room_var.get())
        if not room:
            messagebox.showwarning("房间号不对",
                                   "请填写房间号（纯数字）或直播间链接\n"
                                   "例如：1796290755 或 https://live.bilibili.com/1796290755")
            return
        if not self._apply_settings():
            return
        self.room_var.set(room)
        threading.Thread(target=self.app.start, args=(room,), daemon=True).start()

    def _stop(self):
        if self.app:
            self.app.stop()
            self._append_log("[ui] 正在停止…")

    def _on_close(self):
        if self.app:
            self.app.stop()
        self.root.destroy()


def setup_styles(root: tk.Tk):
    """ttk 只保留输入框样式 (按钮等已自绘)"""
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except Exception:
        pass
    st.configure("Modern.TCheckbutton",
                 background=C.CARD_ALT, foreground=C.TEXT_2,
                 focuscolor=C.CARD_ALT, font=F.LABEL,
                 indicatorbackground=C.CARD, indicatorforeground=C.PRIMARY,
                 indicatormargin=(0, 0, S.SM, 0))
    st.map("Modern.TCheckbutton",
           background=[("active", C.CARD_ALT), ("disabled", C.CARD_ALT)],
           foreground=[("disabled", C.DISABLED_FG)],
           indicatorbackground=[("selected", C.PRIMARY),
                                ("active", "selected", C.PRIMARY_HOVER),
                                ("disabled", C.DISABLED_BG)],
           indicatorforeground=[("selected", C.ON_PRIMARY),
                                ("disabled", C.DISABLED_FG)])

    st.configure("Modern.TEntry",
                 fieldbackground=C.CARD_ALT, foreground=C.TEXT,
                 bordercolor=C.BORDER_STRONG, lightcolor=C.CARD_ALT,
                 darkcolor=C.CARD_ALT, insertcolor=C.PRIMARY,
                 padding=(S.SM, S.SM - 2), relief="flat")
    st.map("Modern.TEntry",
           bordercolor=[("focus", C.PRIMARY)],
           fieldbackground=[("focus", C.CARD), ("disabled", C.DISABLED_BG)],
           lightcolor=[("focus", C.CARD), ("disabled", C.DISABLED_BG)],
           darkcolor=[("focus", C.CARD), ("disabled", C.DISABLED_BG)])


def main():
    root = tk.Tk()
    setup_styles(root)

    q: queue.Queue = queue.Queue()
    sys.stdout = QueueWriter(q)
    sys.stderr = sys.stdout

    orig_poll = App._poll

    def patched_poll(self):
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
        except queue.Empty:
            pass
        except Exception:
            pass
        orig_poll(self)

    App._poll = patched_poll
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
