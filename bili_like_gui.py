# -*- coding: utf-8 -*-
"""
B站直播自动点赞 - 图形界面版 (原生窗口, 非网页)
复用 bili_like_api.py 的核心逻辑, 仅提供窗口操作界面。

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


class QRLoginDialog(tk.Toplevel):
    """扫码登录弹窗: 显示二维码图片 + 状态, 后台线程轮询"""

    def __init__(self, master, app: core.BiliLikeApi, evq: queue.Queue):
        super().__init__(master)
        self.title("扫码登录 - 哔哩哔哩")
        self.resizable(False, False)
        self.configure(bg="#fb7299")
        self.evq = evq
        self.app = app
        self.key = None

        box = tk.Frame(self, bg="white", padx=14, pady=12)
        box.pack(padx=1, pady=1)
        self.img_label = tk.Label(box, bg="white",
                                  text="正在获取二维码...", fg="#666",
                                  font=("Microsoft YaHei", 10))
        self.img_label.pack()
        self.tip = tk.Label(box, text="请用 哔哩哔哩手机App 扫码登录",
                            bg="white", fg="#333", font=("Microsoft YaHei", 10, "bold"))
        self.tip.pack(pady=(8, 2))
        tk.Label(box, text="二维码有效期约 3 分钟", bg="white", fg="#999",
                 font=("Microsoft YaHei", 8)).pack()

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.transient(master)
        self.grab_set()
        self._alive = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            r = self.app.s.get(core.QR_GENERATE, timeout=10).json()
            if r.get("code") != 0:
                self.evq.put(("log", f"[login] 获取二维码失败: {r.get('message')}"))
                self.evq.append_done = None
                self.evq.put(("qr_done", False))
                return
            url, self.key = r["data"]["url"], r["data"]["qrcode_key"]
            img = qrcode.make(url).convert("RGB").resize((230, 230))
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
                    self.evq.put(("qr_status", "二维码已过期, 请关闭后重新扫码"))
                    self.evq.put(("qr_done", False))
                    return
                elif code == 86090:
                    self.evq.put(("qr_status", "已扫码, 请在手机上确认..."))
                time.sleep(2)
            if self._alive:
                self.evq.put(("qr_status", "等待超时, 请关闭后重新扫码"))
                self.evq.put(("qr_done", False))
        except Exception as e:
            self.evq.put(("log", f"[login] 出错: {e}"))
            self.evq.put(("qr_done", False))

    def _close(self):
        self._alive = False
        self.grab_release()
        self.destroy()


class CookieDialog(tk.Toplevel):
    """粘贴 Cookie 登录弹窗"""

    def __init__(self, master, app: core.BiliLikeApi, evq: queue.Queue):
        super().__init__(master)
        self.title("粘贴 Cookie 登录")
        self.resizable(False, False)
        self.app = app
        self.evq = evq

        tk.Label(self, text="浏览器打开 bilibili.com 并登录后:\n"
                            "F12 -> 网络(Network) -> 任选请求 -> 请求标头 -> 复制 Cookie 整行粘贴到这里",
                 justify="left", font=("Microsoft YaHei", 9)).pack(padx=12, pady=(10, 4))
        self.txt = tk.Text(self, width=70, height=7, wrap="char", font=("Consolas", 9))
        self.txt.pack(padx=12, pady=4)
        bar = tk.Frame(self)
        bar.pack(pady=(0, 10))
        ttk.Button(bar, text="保存并登录", command=self._save).pack(side="left", padx=6)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left", padx=6)
        self.transient(master)
        self.grab_set()

    def _save(self):
        raw = self.txt.get("1.0", "end").strip()
        if not raw:
            return
        n = self.app.set_cookie_string(raw)
        ok, uname = self.app.logged_in()
        self.evq.put(("log", f"[cookie] 已读取 {n} 个字段, 登录状态: "
                             f"{uname if ok else '无效, 请检查是否复制完整'}"))
        self.evq.put(("account", (ok, uname)))
        if ok:
            self.destroy()
        else:
            messagebox.showwarning("登录失败", "Cookie 无效或已过期, 请重新复制完整的 Cookie",
                                   parent=self)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.evq: queue.Queue = queue.Queue()
        self.app: core.BiliLikeApi = None
        self._build_ui()
        self._poll()
        threading.Thread(target=self._init_backend, daemon=True).start()

    # ---------- UI ----------
    def _build_ui(self):
        self.root.title("BiliLiveLike - B站直播自动点赞")
        self.root.geometry("660x560")
        self.root.minsize(560, 460)
        self.root.configure(bg="#f6f7f8")

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass

        # ---- 账号栏 ----
        acc = tk.Frame(self.root, bg="#fb7299", padx=12, pady=8)
        acc.pack(fill="x")
        self.acc_label = tk.Label(acc, text="账号: 检查登录中...", bg="#fb7299",
                                  fg="white", font=("Microsoft YaHei", 10, "bold"))
        self.acc_label.pack(side="left")
        ttk.Button(acc, text="扫码登录", command=self._qr_login).pack(side="right", padx=4)
        ttk.Button(acc, text="粘贴Cookie", command=self._cookie_login).pack(side="right", padx=4)

        # ---- 房间与控制 ----
        ctl = tk.LabelFrame(self.root, text=" 直播间 ", bg="#f6f7f8",
                            font=("Microsoft YaHei", 9), padx=10, pady=8)
        ctl.pack(fill="x", padx=12, pady=(10, 6))
        row1 = tk.Frame(ctl, bg="#f6f7f8")
        row1.pack(fill="x")
        tk.Label(row1, text="房间号或链接:", bg="#f6f7f8",
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self.room_var = tk.StringVar(value=core.load_config().get("room", ""))
        room_entry = ttk.Entry(row1, textvariable=self.room_var, width=28)
        room_entry.pack(side="left", padx=8)
        self.start_btn = ttk.Button(row1, text="开始点赞", command=self._start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(row1, text="停止", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        row2 = tk.Frame(ctl, bg="#f6f7f8")
        row2.pack(fill="x", pady=(8, 0))
        tk.Label(row2, text="间隔(秒):", bg="#f6f7f8",
                 font=("Microsoft YaHei", 9)).pack(side="left")
        cfg = core.load_config()
        self.iv_min = tk.StringVar(value=str(cfg.get("interval_min", 5.0)))
        self.iv_max = tk.StringVar(value=str(cfg.get("interval_max", 8.0)))
        self.ck_min = tk.StringVar(value=str(cfg.get("click_min", 10)))
        self.ck_max = tk.StringVar(value=str(cfg.get("click_max", 20)))
        self.max_likes = tk.StringVar(value=str(cfg.get("max_likes", 1000)))
        for var, w in [(self.iv_min, 5), (self.iv_max, 5)]:
            ttk.Entry(row2, textvariable=var, width=w).pack(side="left", padx=3)
        tk.Label(row2, text="  每次连击:", bg="#f6f7f8",
                 font=("Microsoft YaHei", 9)).pack(side="left")
        for var in (self.ck_min, self.ck_max):
            ttk.Entry(row2, textvariable=var, width=4).pack(side="left", padx=3)
        tk.Label(row2, text="  单场上限:", bg="#f6f7f8",
                 font=("Microsoft YaHei", 9)).pack(side="left")
        ttk.Entry(row2, textvariable=self.max_likes, width=7).pack(side="left", padx=3)

        # ---- 日志 ----
        logf = tk.LabelFrame(self.root, text=" 日志 ", bg="#f6f7f8",
                             font=("Microsoft YaHei", 9))
        logf.pack(fill="both", expand=True, padx=12, pady=6)
        self.log_txt = scrolledtext.ScrolledText(logf, height=12, state="disabled",
                                                 font=("Consolas", 9), bg="#1e1e1e",
                                                 fg="#d4d4d4", insertbackground="#d4d4d4")
        self.log_txt.pack(fill="both", expand=True)

        # ---- 状态栏 ----
        self.status_var = tk.StringVar(value="状态: 空闲 | 已点赞: 0 (0 次请求)")
        sb = tk.Label(self.root, textvariable=self.status_var, anchor="w",
                      bg="#e9ebee", fg="#444", font=("Microsoft YaHei", 9), padx=10, pady=4)
        sb.pack(fill="x", side="bottom")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 后台初始化 ----------
    def _init_backend(self):
        if core.requests is None:
            self.evq.put(("fatal", "缺少 requests 库, 请执行: pip install requests \"qrcode[pil]\""))
            return
        self.app = core.BiliLikeApi()
        ok, uname = self.app.logged_in()
        self.evq.put(("account", (ok, uname)))
        if not ok:
            self.evq.put(("log", "[init] 未登录, 请点右上角「扫码登录」或「粘贴Cookie」"))

    # ---------- 事件处理 ----------
    def _poll(self):
        try:
            while True:
                kind, payload = self.evq.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "account":
                    ok, uname = payload
                    if ok:
                        self.acc_label.config(text=f"账号: {uname} ✓")
                    else:
                        self.acc_label.config(text="账号: 未登录")
                elif kind == "qr_image" and ImageTk is not None:
                    if hasattr(self, "_qr_dialog") and self._qr_dialog and self._qr_dialog.winfo_exists():
                        photo = ImageTk.PhotoImage(payload, master=self._qr_dialog)
                        self._qr_dialog.img_label.config(image=photo, text="")
                        self._qr_dialog.img_label.image = photo
                elif kind == "qr_status":
                    if hasattr(self, "_qr_dialog") and self._qr_dialog and self._qr_dialog.winfo_exists():
                        self._qr_dialog.tip.config(text=payload)
                elif kind == "qr_done":
                    if hasattr(self, "_qr_dialog") and self._qr_dialog and self._qr_dialog.winfo_exists():
                        self._qr_dialog.grab_release()
                        self._qr_dialog.destroy()
                elif kind == "fatal":
                    messagebox.showerror("错误", payload)
                    self.root.destroy()
                    return
        except queue.Empty:
            pass

        # 状态栏刷新
        if self.app:
            s = self.app.stats
            state = "运行中" if self.app.running else "空闲"
            dur = ""
            if s.get("start") and self.app.running:
                dur = f" | 已运行 {int(time.time() - s['start'])}s"
            self.status_var.set(f"状态: {state} | 已点赞: {s['likes']} "
                                f"({s['clicks']} 次请求){dur}")
            running = self.app.running
        else:
            running = False
        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")

        self.root.after(400, self._poll)

    def _append_log(self, line):
        self.log_txt.config(state="normal")
        self.log_txt.insert("end", line + "\n")
        self.log_txt.see("end")
        # 控制日志体积
        if int(self.log_txt.index("end-1c").split(".")[0]) > 800:
            self.log_txt.delete("1.0", "300.0")
        self.log_txt.config(state="disabled")

    # ---------- 动作 ----------
    def _qr_login(self):
        if core.qrcode is None or ImageTk is None:
            messagebox.showwarning("缺少依赖", "二维码功能需要 qrcode 和 pillow 库\n"
                                               "命令行执行: pip install \"qrcode[pil]\"\n"
                                               "或使用「粘贴Cookie」方式登录")
            return
        if not self.app:
            messagebox.showinfo("稍等", "程序还在初始化, 请稍候几秒再试")
            return
        self._qr_dialog = QRLoginDialog(self.root, self.app, self.evq)

    def _cookie_login(self):
        if not self.app:
            messagebox.showinfo("稍等", "程序还在初始化, 请稍候几秒再试")
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
            messagebox.showwarning("参数不对", "间隔/连击/上限请填写数字")
            return False
        if cfg["interval_min"] <= 0 or cfg["interval_min"] > cfg["interval_max"]:
            messagebox.showwarning("参数不对", "间隔需要: 最小间隔 > 0 且 ≤ 最大间隔")
            return False
        if cfg["click_min"] <= 0 or cfg["click_min"] > cfg["click_max"]:
            messagebox.showwarning("参数不对", "连击需要: 下限 > 0 且 ≤ 上限")
            return False
        core.save_config(cfg)
        return True

    def _start(self):
        if not self.app:
            return
        room = core.normalize_room(self.room_var.get())
        if not room:
            messagebox.showwarning("房间号不对",
                                   "请填写房间号(纯数字)或直播间链接\n"
                                   "例如: 1796290755 或 https://live.bilibili.com/1796290755")
            return
        if not self._apply_settings():
            return
        self.room_var.set(room)
        threading.Thread(target=self.app.start, args=(room,), daemon=True).start()

    def _stop(self):
        if self.app:
            self.app.stop()
            self._append_log("[ui] 正在停止...")

    def _on_close(self):
        if self.app:
            self.app.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    # 截获 print 输出 -> GUI 日志 (任务循环在后台线程里用 print 打日志)
    q: queue.Queue = queue.Queue()
    writer = QueueWriter(q)
    sys.stdout = writer
    sys.stderr = writer
    # 把队列接到事件队列
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
