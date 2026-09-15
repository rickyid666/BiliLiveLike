# -*- coding: utf-8 -*-
"""
B站直播自动点赞 - 图形界面版 (原生窗口, 非网页)
视觉: 统一设计系统(配色/字体层级/间距规范/组件状态), 功能与交互逻辑不变。

依赖: pip install requests "qrcode[pil]"
"""

import queue
import sys
import threading
import time

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

import bili_like_api as core

try:
    import qrcode
except ImportError:
    qrcode = None

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None


# ============================================================
#  设计系统 (Design Tokens)
# ============================================================
class C:
    """色彩体系"""
    BG = "#F1F3F5"          # 应用底色
    CARD = "#FFFFFF"        # 卡片面
    CARD_ALT = "#F7F8FA"    # 次级面(输入/内嵌区)
    BORDER = "#E3E6EA"      # 分隔线/描边
    BORDER_STRONG = "#D2D6DB"

    TEXT = "#1F2329"        # 主文字
    TEXT_2 = "#5B6169"      # 次要文字
    TEXT_3 = "#8A9099"      # 辅助/占位文字

    PRIMARY = "#FB7299"     # 品牌色(B站粉)
    PRIMARY_HOVER = "#F0577F"
    PRIMARY_PRESS = "#DC4468"
    PRIMARY_SOFT = "#FFF0F4"   # 品牌色浅底
    PRIMARY_SOFT_HOVER = "#FFE3EB"
    ON_PRIMARY = "#FFFFFF"

    SUCCESS = "#2FA84F"
    SUCCESS_SOFT = "#EAF7EE"
    WARN = "#D98A16"
    WARN_SOFT = "#FFF6E8"
    DANGER = "#E0484D"
    DANGER_SOFT = "#FDECEE"
    DANGER_SOFT_HOVER = "#FBDCDF"

    DISABLED_BG = "#EFF0F2"
    DISABLED_FG = "#9AA0A8"

    LOG_BG = "#15171C"
    LOG_FG = "#D5D8DD"
    LOG_OK = "#5FD08A"
    LOG_ERR = "#FF7B7F"
    LOG_WARN = "#F0C060"
    LOG_DIM = "#7B838E"

    HEADER = "#FB7299"
    HEADER_DEEP = "#F0577F"


class S:
    """间距规范 (4px 基准)"""
    XS, SM, MD, LG, XL = 4, 8, 12, 20, 28


class F:
    """字体层级"""
    FAMILY = ("Microsoft YaHei UI", 10)
    TITLE = ("Microsoft YaHei UI", 14, "bold")
    SECTION = ("Microsoft YaHei UI", 10, "bold")
    BODY = ("Microsoft YaHei UI", 10)
    LABEL = ("Microsoft YaHei UI", 9)
    CAPTION = ("Microsoft YaHei UI", 8)
    MONO = ("Consolas", 9)


class QueueWriter:
    """把 print 输出转发到 GUI 日志队列 (线程安全)"""

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


def setup_styles(root: tk.Tk):
    """统一 ttk 组件样式 (clam 主题才能完全自定义配色)"""
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except Exception:
        pass

    st.configure(".", background=C.BG, foreground=C.TEXT, font=F.BODY,
                 borderwidth=0, focuscolor=C.BG)

    # ---- 按钮: 主要 ----
    st.configure("Primary.TButton",
                 background=C.PRIMARY, foreground=C.ON_PRIMARY,
                 font=("Microsoft YaHei UI", 10, "bold"),
                 padding=(S.LG, S.SM + 1), relief="flat", borderwidth=0)
    st.map("Primary.TButton",
           background=[("pressed", C.PRIMARY_PRESS), ("active", C.PRIMARY_HOVER),
                       ("disabled", C.DISABLED_BG)],
           foreground=[("disabled", C.DISABLED_FG)])

    # ---- 按钮: 次要(浅底) ----
    st.configure("Soft.TButton",
                 background=C.PRIMARY_SOFT, foreground=C.PRIMARY_PRESS,
                 font=("Microsoft YaHei UI", 9, "bold"),
                 padding=(S.MD, S.SM - 1), relief="flat", borderwidth=0)
    st.map("Soft.TButton",
           background=[("pressed", C.PRIMARY_SOFT_HOVER), ("active", C.PRIMARY_SOFT_HOVER),
                       ("disabled", C.DISABLED_BG)],
           foreground=[("disabled", C.DISABLED_FG)])

    # ---- 按钮: 危险(停止) ----
    st.configure("Danger.TButton",
                 background=C.DANGER_SOFT, foreground=C.DANGER,
                 font=("Microsoft YaHei UI", 10, "bold"),
                 padding=(S.LG, S.SM + 1), relief="flat", borderwidth=0)
    st.map("Danger.TButton",
           background=[("pressed", C.DANGER_SOFT_HOVER), ("active", C.DANGER_SOFT_HOVER),
                       ("disabled", C.DISABLED_BG)],
           foreground=[("disabled", C.DISABLED_FG)])

    # ---- 输入框 ----
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

    # ---- 卡片容器 ----
    st.configure("Card.TFrame", background=C.CARD)
    st.configure("Bg.TFrame", background=C.BG)
    st.configure("Card.TLabel", background=C.CARD, foreground=C.TEXT)
    st.configure("CardDim.TLabel", background=C.CARD, foreground=C.TEXT_2)
    st.configure("CardHint.TLabel", background=C.CARD, foreground=C.TEXT_3)
    st.configure("Bg.TLabel", background=C.BG, foreground=C.TEXT_2)


class Card(tk.Frame):
    """带描边的白卡片容器 (1px 边框 + 内边距规范)"""

    def __init__(self, master, title=None, **kw):
        super().__init__(master, bg=C.CARD, highlightbackground=C.BORDER,
                         highlightcolor=C.BORDER, highlightthickness=1, bd=0)
        self.body = tk.Frame(self, bg=C.CARD)
        self.body.pack(fill="both", expand=True, padx=S.LG, pady=S.LG)
        if title:
            tk.Label(self, text=title, bg=C.CARD, fg=C.TEXT,
                     font=F.SECTION, anchor="w").pack(
                fill="x", padx=S.LG, pady=(S.MD, 0), before=self.body)
            tk.Frame(self, bg=C.BORDER, height=1).pack(
                fill="x", pady=(S.SM, 0), before=self.body)


class QRLoginDialog(tk.Toplevel):
    """扫码登录弹窗: 显示二维码图片 + 状态, 后台线程轮询"""

    def __init__(self, master, app: core.BiliLikeApi, evq: queue.Queue):
        super().__init__(master, bg=C.CARD)
        self.title("扫码登录")
        self.resizable(False, False)
        self.evq = evq
        self.app = app
        self.key = None
        self._alive = True

        head = tk.Frame(self, bg=C.HEADER)
        head.pack(fill="x")
        tk.Label(head, text="扫码登录", bg=C.HEADER, fg="white",
                 font=F.TITLE).pack(side="left", padx=S.LG, pady=S.MD)

        wrap = tk.Frame(self, bg=C.CARD, padx=S.XL, pady=S.XL)
        wrap.pack()
        self.qr_box = tk.Frame(wrap, bg=C.CARD_ALT, highlightbackground=C.BORDER,
                               highlightthickness=1)
        self.qr_box.pack()
        self.img_label = tk.Label(self.qr_box, bg=C.CARD_ALT, fg=C.TEXT_3,
                                  text="正在获取二维码...", font=F.LABEL,
                                  width=26, height=12)
        self.img_label.pack(padx=S.SM, pady=S.SM)

        self.tip = tk.Label(wrap, text="用 哔哩哔哩 App 扫描二维码", bg=C.CARD,
                            fg=C.TEXT, font=F.SECTION)
        self.tip.pack(pady=(S.MD, S.XS))
        tk.Label(wrap, text="二维码有效期约 3 分钟", bg=C.CARD, fg=C.TEXT_3,
                 font=F.CAPTION).pack()

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.transient(master)
        self.grab_set()
        self._center(master)
        threading.Thread(target=self._worker, daemon=True).start()

    def _center(self, master):
        self.update_idletasks()
        try:
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            pass

    def _worker(self):
        try:
            r = self.app.s.get(core.QR_GENERATE, timeout=10).json()
            if r.get("code") != 0:
                self.evq.put(("log", f"[login] 获取二维码失败: {r.get('message')}"))
                self.evq.put(("qr_done", False))
                return
            url, self.key = r["data"]["url"], r["data"]["qrcode_key"]
            img = qrcode.make(url).convert("RGB").resize((228, 228))
            self.evq.put(("qr_image", img))
            t0 = time.time()
            while self._alive and time.time() - t0 < 180:
                p = self.app.s.get(core.QR_POLL,
                                   params={"qrcode_key": self.key}, timeout=10).json()
                code = p.get("data", {}).get("code")
                if code == 0:
                    self.app._save_cookies()
                    ok, uname = self.app.logged_in()
                    self.evq.put(("log", f"[login] 登录成功! 欢迎你, {uname}"))
                    self.evq.put(("account", (ok, uname)))
                    self.evq.put(("qr_done", True))
                    return
                elif code == 86038:
                    self.evq.put(("log", "[login] 二维码已过期, 请重新扫码"))
                    self.evq.put(("qr_status", "二维码已过期, 请重新扫码"))
                    self.evq.put(("qr_done", False))
                    return
                elif code == 86090:
                    self.evq.put(("qr_status", "已扫码, 请在手机上确认"))
                time.sleep(2)
            if self._alive:
                self.evq.put(("qr_status", "等待超时, 请重新扫码"))
                self.evq.put(("qr_done", False))
        except Exception as e:
            self.evq.put(("log", f"[login] 出错: {e}"))
            self.evq.put(("qr_done", False))

    def _close(self):
        self._alive = False
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class CookieDialog(tk.Toplevel):
    """粘贴 Cookie 登录弹窗"""

    def __init__(self, master, app: core.BiliLikeApi, evq: queue.Queue):
        super().__init__(master, bg=C.CARD)
        self.title("粘贴 Cookie 登录")
        self.resizable(False, False)
        self.app = app
        self.evq = evq

        head = tk.Frame(self, bg=C.HEADER)
        head.pack(fill="x")
        tk.Label(head, text="粘贴 Cookie 登录", bg=C.HEADER, fg="white",
                 font=F.TITLE).pack(side="left", padx=S.LG, pady=S.MD)

        wrap = tk.Frame(self, bg=C.CARD, padx=S.LG, pady=S.LG)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text="浏览器打开 bilibili.com 登录后：F12 → 网络 → 任一请求\n"
                            "→ 请求标头 → 复制 Cookie 整行，粘贴到下方",
                 justify="left", bg=C.CARD, fg=C.TEXT_2, font=F.LABEL).pack(anchor="w")
        box = tk.Frame(wrap, bg=C.CARD_ALT, highlightbackground=C.BORDER,
                       highlightthickness=1)
        box.pack(fill="both", expand=True, pady=S.MD)
        self.txt = tk.Text(box, width=66, height=7, wrap="char", font=F.MONO,
                           bg=C.CARD_ALT, fg=C.TEXT, relief="flat",
                           insertbackground=C.PRIMARY, padx=S.SM, pady=S.SM)
        self.txt.pack(fill="both", expand=True)

        bar = tk.Frame(wrap, bg=C.CARD)
        bar.pack(fill="x")
        ttk.Button(bar, text="保存并登录", style="Primary.TButton",
                   command=self._save).pack(side="right")
        ttk.Button(bar, text="取消", style="Soft.TButton",
                   command=self.destroy).pack(side="right", padx=(0, S.SM))
        self.transient(master)
        self.grab_set()

    def _save(self):
        raw = self.txt.get("1.0", "end").strip()
        if not raw:
            return
        n = self.app.set_cookie_string(raw)
        ok, uname = self.app.logged_in()
        self.evq.put(("log", f"[cookie] 已读取 {n} 个字段, 登录状态: "
                             f"{uname if ok else '无效，请检查是否复制完整'}"))
        self.evq.put(("account", (ok, uname)))
        if ok:
            self.destroy()
        else:
            messagebox.showwarning("登录失败", "Cookie 无效或已过期，请重新复制完整的 Cookie",
                                   parent=self)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.evq: queue.Queue = queue.Queue()
        self.app: core.BiliLikeApi = None
        self._qr_dialog = None
        self._build_ui()
        self._poll()
        threading.Thread(target=self._init_backend, daemon=True).start()

    # ---------- 布局 ----------
    def _build_ui(self):
        r = self.root
        r.title("BiliLiveLike · B站直播自动点赞")
        r.geometry("920x640")
        r.minsize(600, 520)
        r.configure(bg=C.BG)
        self._wide = None

        # ===== 顶部品牌栏 =====
        head = tk.Frame(r, bg=C.HEADER)
        head.pack(fill="x")
        inner = tk.Frame(head, bg=C.HEADER)
        inner.pack(fill="x", padx=S.XL, pady=(S.MD, S.MD))

        brand = tk.Frame(inner, bg=C.HEADER)
        brand.pack(side="left")
        tk.Label(brand, text="BiliLiveLike", bg=C.HEADER, fg="white",
                 font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        tk.Label(brand, text="直播间自动点赞 · 纯接口后台运行", bg=C.HEADER,
                 fg="#FFE1EA", font=F.CAPTION).pack(anchor="w")

        actions = tk.Frame(inner, bg=C.HEADER)
        actions.pack(side="right")
        ttk.Button(actions, text="扫码登录", style="Soft.TButton",
                   command=self._qr_login).pack(side="left", padx=(0, S.SM))
        ttk.Button(actions, text="粘贴 Cookie", style="Soft.TButton",
                   command=self._cookie_login).pack(side="left")

        # 账号状态条 (品牌色深一档, 强化层级)
        status_strip = tk.Frame(r, bg=C.HEADER_DEEP)
        status_strip.pack(fill="x")
        self.acc_dot = tk.Label(status_strip, text="●", bg=C.HEADER_DEEP,
                                fg="#FFD3DF", font=("Segoe UI", 9))
        self.acc_dot.pack(side="left", padx=(S.XL, S.XS), pady=S.SM)
        self.acc_label = tk.Label(status_strip, text="正在检查登录状态…",
                                  bg=C.HEADER_DEEP, fg="white", font=F.LABEL)
        self.acc_label.pack(side="left", pady=S.SM)

        # ===== 主体 (宽度自适应: 宽屏左右分栏 / 窄屏上下堆叠) =====
        main = tk.Frame(r, bg=C.BG)
        self.main = main

        # --- 控制卡片 ---
        ctrl = Card(main, title="直播间")
        self.ctrl_card = ctrl
        ctrl.grid(row=0, column=0, sticky="ew")
        b = ctrl.body

        row = tk.Frame(b, bg=C.CARD)
        row.pack(fill="x")
        tk.Label(row, text="房间号 / 链接", bg=C.CARD, fg=C.TEXT_2,
                 font=F.LABEL).pack(anchor="w")
        line = tk.Frame(row, bg=C.CARD)
        line.pack(fill="x", pady=(S.XS, 0))
        self.room_var = tk.StringVar(value=core.load_config().get("room", ""))
        ttk.Entry(line, textvariable=self.room_var, style="Modern.TEntry",
                  font=F.BODY).pack(side="left", fill="x", expand=True)
        self.start_btn = ttk.Button(line, text="开始点赞", style="Primary.TButton",
                                    command=self._start)
        self.start_btn.pack(side="left", padx=(S.SM, 0))
        self.stop_btn = ttk.Button(line, text="停止", style="Danger.TButton",
                                   command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(S.SM, 0))

        # --- 参数区 (内嵌次级面) ---
        phead = tk.Frame(b, bg=C.CARD)
        phead.pack(fill="x", pady=(S.MD, S.XS))
        tk.Label(phead, text="运行参数", bg=C.CARD, fg=C.TEXT_2,
                 font=F.LABEL).pack(side="left")
        tk.Label(phead, text="请求为随机节奏，间隔越大越稳妥", bg=C.CARD,
                 fg=C.TEXT_3, font=F.CAPTION).pack(side="right")
        param_wrap = tk.Frame(b, bg=C.CARD_ALT, highlightbackground=C.BORDER,
                              highlightthickness=1)
        param_wrap.pack(fill="x")
        cfg = core.load_config()
        self.iv_min = tk.StringVar(value=str(cfg.get("interval_min", 5.0)))
        self.iv_max = tk.StringVar(value=str(cfg.get("interval_max", 8.0)))
        self.ck_min = tk.StringVar(value=str(cfg.get("click_min", 10)))
        self.ck_max = tk.StringVar(value=str(cfg.get("click_max", 20)))
        self.max_likes = tk.StringVar(value=str(cfg.get("max_likes", 1000)))

        pg = tk.Frame(param_wrap, bg=C.CARD_ALT)
        pg.pack(fill="x", padx=S.MD, pady=S.MD)
        self._param(pg, "请求间隔", self.iv_min, "秒", self.iv_max, "秒")
        self._param(pg, "每次连击", self.ck_min, "赞", self.ck_max, "赞")
        self._param(pg, "单场上限", self.max_likes, "赞", None, None)

        # --- 日志卡片 ---
        logcard = Card(main, title="运行日志")
        self.log_card = logcard
        logcard.grid(row=1, column=0, sticky="nsew", pady=(S.MD, 0))
        log_box = tk.Frame(logcard.body, bg=C.LOG_BG)
        log_box.pack(fill="both", expand=True)
        self.log_txt = scrolledtext.ScrolledText(
            log_box, height=10, state="disabled", font=F.MONO,
            bg=C.LOG_BG, fg=C.LOG_FG, relief="flat", insertbackground=C.LOG_FG,
            padx=S.MD, pady=S.MD, spacing1=1)
        self.log_txt.pack(fill="both", expand=True)
        self.log_txt.tag_config("ok", foreground=C.LOG_OK)
        self.log_txt.tag_config("err", foreground=C.LOG_ERR)
        self.log_txt.tag_config("warn", foreground=C.LOG_WARN)
        self.log_txt.tag_config("dim", foreground=C.LOG_DIM)

        # ===== 底部状态栏 =====
        bar = tk.Frame(r, bg=C.CARD, highlightbackground=C.BORDER,
                       highlightthickness=1)
        bar.pack(fill="x", side="bottom")
        bar_in = tk.Frame(bar, bg=C.CARD)
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
        self.hint_label = tk.Label(bar_in, text="主播开播时点赞才有效",
                                   bg=C.CARD, fg=C.TEXT_3, font=F.CAPTION)
        self.hint_label.pack(side="right")

        # 状态栏先占位, 再让主体区填充剩余空间 (避免被日志卡片挤出窗口)
        main.pack(fill="both", expand=True, padx=S.XL, pady=(S.LG, S.MD))
        r.bind("<Configure>", self._on_resize)
        self._relayout(920)
        r.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_resize(self, event):
        if event.widget is self.root:
            self._relayout(event.width)

    def _relayout(self, width):
        """宽屏: 左控制 / 右日志 ; 窄屏: 上下堆叠"""
        wide = width >= 800
        if wide == self._wide:
            return
        self._wide = wide
        m = self.main
        if wide:
            m.columnconfigure(0, weight=0, minsize=380)
            m.columnconfigure(1, weight=1)
            m.rowconfigure(0, weight=1)
            m.rowconfigure(1, weight=0)
            self.ctrl_card.grid(row=0, column=0, sticky="new", padx=(0, S.MD), pady=0)
            self.log_card.grid(row=0, column=1, sticky="nsew", padx=0, pady=0)
        else:
            m.columnconfigure(0, weight=1, minsize=0)
            m.columnconfigure(1, weight=0, minsize=0)
            m.rowconfigure(0, weight=0)
            m.rowconfigure(1, weight=1)
            self.ctrl_card.grid(row=0, column=0, columnspan=2, sticky="ew",
                                padx=0, pady=(0, S.MD))
            self.log_card.grid(row=1, column=0, columnspan=2, sticky="nsew",
                               padx=0, pady=0)

    def _param(self, parent, label, var_a, unit_a, var_b, unit_b):
        """一行参数: 标签 + 输入框(可带范围) + 单位"""
        row = tk.Frame(parent, bg=C.CARD_ALT)
        row.pack(fill="x", pady=(0, S.SM))
        tk.Label(row, text=label, bg=C.CARD_ALT, fg=C.TEXT_2, font=F.LABEL,
                 width=8, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var_a, style="Modern.TEntry",
                  width=6, font=F.LABEL).pack(side="left")
        tk.Label(row, text=unit_a, bg=C.CARD_ALT, fg=C.TEXT_3,
                 font=F.CAPTION).pack(side="left", padx=(S.XS, S.SM))
        if var_b is not None:
            tk.Label(row, text="~", bg=C.CARD_ALT, fg=C.TEXT_3,
                     font=F.LABEL).pack(side="left")
            ttk.Entry(row, textvariable=var_b, style="Modern.TEntry",
                      width=6, font=F.LABEL).pack(side="left", padx=(S.SM, 0))
            tk.Label(row, text=unit_b, bg=C.CARD_ALT, fg=C.TEXT_3,
                     font=F.CAPTION).pack(side="left", padx=(S.XS, 0))

    # ---------- 后台初始化 ----------
    def _init_backend(self):
        if core.requests is None:
            self.evq.put(("fatal", "缺少 requests 库，请执行: pip install requests \"qrcode[pil]\""))
            return
        self.app = core.BiliLikeApi()
        ok, uname = self.app.logged_in()
        self.evq.put(("account", (ok, uname)))
        if not ok:
            self.evq.put(("log", "[init] 未登录，请点右上角「扫码登录」或「粘贴 Cookie」"))

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
                        self.acc_label.config(text=f"已登录 · {uname}")
                        self.acc_dot.config(fg="#B4F0C6")
                    else:
                        self.acc_label.config(text="未登录 · 请扫码或粘贴 Cookie")
                        self.acc_dot.config(fg="#FFD3DF")
                elif kind == "qr_image" and ImageTk is not None:
                    if self._qr_dialog and self._qr_dialog.winfo_exists():
                        photo = ImageTk.PhotoImage(payload, master=self._qr_dialog)
                        self._qr_dialog.img_label.config(image=photo, text="")
                        self._qr_dialog.img_label.image = photo
                elif kind == "qr_status":
                    if self._qr_dialog and self._qr_dialog.winfo_exists():
                        self._qr_dialog.tip.config(text=payload,
                                                   fg=C.PRIMARY_PRESS)
                elif kind == "qr_done":
                    if self._qr_dialog and self._qr_dialog.winfo_exists():
                        try:
                            self._qr_dialog.grab_release()
                        except Exception:
                            pass
                        self._qr_dialog.destroy()
                    self._qr_dialog = None
                elif kind == "fatal":
                    messagebox.showerror("错误", payload)
                    self.root.destroy()
                    return
        except queue.Empty:
            pass

        if self.app:
            s = self.app.stats
            running = self.app.running
            if running:
                self.state_dot.config(fg=C.SUCCESS)
                self.state_label.config(text="运行中", fg=C.SUCCESS)
            else:
                self.state_dot.config(fg=C.TEXT_3)
                self.state_label.config(text="空闲", fg=C.TEXT)
            dur = f" · 已运行 {int(time.time() - s['start'])}s" if (s.get("start") and running) else ""
            self.metric_label.config(text=f"已点赞 {s['likes']} · {s['clicks']} 次请求{dur}")
        else:
            running = False

        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")
        self.root.after(400, self._poll)

    def _append_log(self, line):
        tag = None
        if "OK" in line and "赞" in line:
            tag = "ok"
        elif "错误" in line or "失败" in line or "异常" in line or "失效" in line or "未登录" in line:
            tag = "err"
        elif "风控" in line or "放慢" in line or "已过期" in line or "上限" in line:
            tag = "warn"
        elif line.startswith("[init]") or line.startswith("[ui]"):
            tag = "dim"
        self.log_txt.config(state="normal")
        self.log_txt.insert("end", line + "\n", tag or "")
        self.log_txt.see("end")
        if int(self.log_txt.index("end-1c").split(".")[0]) > 800:
            self.log_txt.delete("1.0", "300.0")
        self.log_txt.config(state="disabled")

    # ---------- 动作 ----------
    def _qr_login(self):
        if core.qrcode is None or ImageTk is None:
            messagebox.showwarning("缺少依赖", "二维码功能需要 qrcode 和 pillow 库\n"
                                               "命令行执行: pip install \"qrcode[pil]\"\n"
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


def main():
    root = tk.Tk()
    setup_styles(root)

    # 截获 print 输出 -> GUI 日志
    q: queue.Queue = queue.Queue()
    writer = QueueWriter(q)
    sys.stdout = writer
    sys.stderr = writer

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
