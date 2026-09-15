# -*- coding: utf-8 -*-
"""
B站直播自动点赞 - 网页界面版本地服务
================================================================
在本地起一个小型 HTTP 服务, 浏览器打开即用 (桌面端 / 手机端响应式)。
功能与桌面版一致, 复用 bili_like_api.py 的核心逻辑。

用法:
  python web_server.py            # 仅本机访问 http://127.0.0.1:8848
  python web_server.py --lan      # 允许局域网访问 (手机同 WiFi 可用)
  python web_server.py --port 9000

接口 (前后端约定):
  GET  /                    -> 页面
  GET  /api/status          -> 账号 / 运行状态 / 统计
  GET  /api/log?since=N     -> 增量日志
  POST /api/qr              -> 生成扫码登录二维码(返回 dataURI)
  GET  /api/qr/state        -> 扫码进度
  POST /api/cookie          -> {cookie: "..."} 粘贴 Cookie 登录
  GET  /api/room?q=...      -> 房间解析 (房间号/链接)
  POST /api/start           -> {room, interval_min, ...}
  POST /api/stop
"""

import argparse
import base64
import io
import json
import os
import queue
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import bili_like_api as core

try:
    import qrcode
except ImportError:
    qrcode = None


# ============================================================
#  运行状态
# ============================================================
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.app = None
        self.qr_state = "idle"      # idle / waiting / scanned / success / expired / error
        self.qr_msg = ""
        self.qr_key = ""
        self.logs = []              # [(index, text)]
        self.max_logs = 800

    # ---------- 日志 ----------
    def add_log(self, line):
        with self.lock:
            self.logs.append((len(self.logs), line))
            if len(self.logs) > self.max_logs:
                self.logs = self.logs[-self.max_logs:]

    def logs_since(self, since):
        with self.lock:
            items = self.logs
            out = [{"i": i, "text": t} for i, t in items if i >= since]
            nxt = (items[-1][0] + 1) if items else since
            return out, nxt


ST = State()


class LogWriter:
    """把核心逻辑里的 print 收进日志缓冲, 同时透传到控制台"""

    def __init__(self, real):
        self.real = real
        self._buf = ""

    def write(self, s):
        if self.real:
            try:
                self.real.write(s)
            except Exception:
                pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                ST.add_log(line)

    def flush(self):
        if self.real:
            try:
                self.real.flush()
            except Exception:
                pass


# ============================================================
#  业务封装
# ============================================================
_APP_LOCK = threading.Lock()


def ensure_app():
    """获取全局唯一的 BiliLikeApi 实例(懒加载 + 双检锁)

    这个锁不是可有可无的优化, 它修的是一个真实竞态:
    ThreadingHTTPServer 每个请求跑一个线程, 启动时还有一个 refresh_account 线程,
    BiliLikeApi 的构造函数里要做网络预取(耗时数秒), 竞态窗口很大。
    没有锁时两个线程都会各自 new 一个实例, 后写的覆盖 ST.app ——
    极端情况下一个请求把房间加到 A 实例, 下一个请求操作的是 B 实例,
    对外表现就是"刚加进去的房间莫名消失"。
    (网页端 e2e 测试曾以约 1/3 的概率复现此现象)
    """
    if ST.app is None:
        with _APP_LOCK:
            if ST.app is None:            # 双检: 保证只构造一次
                ST.app = core.BiliLikeApi()
    return ST.app


def qr_start():
    """生成二维码并起线程轮询扫码结果"""
    app = ensure_app()
    if qrcode is None:
        return {"ok": False, "error": "服务器缺少 qrcode 库, 请 pip install \"qrcode[pil]\""}
    r = app.s.get(core.QR_GENERATE, timeout=10).json()
    if r.get("code") != 0:
        return {"ok": False, "error": f"获取二维码失败: {r.get('message')}"}

    url, key = r["data"]["url"], r["data"]["qrcode_key"]
    img = qrcode.make(url).convert("RGB").resize((240, 240))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    with ST.lock:
        ST.qr_state, ST.qr_msg, ST.qr_key = "waiting", "请用哔哩哔哩 App 扫码", key

    def poll():
        t0 = time.time()
        while time.time() - t0 < 180:
            try:
                p = app.s.get(core.QR_POLL, params={"qrcode_key": key}, timeout=10).json()
                code = p.get("data", {}).get("code")
                if code == 0:
                    app._save_cookies()
                    ok, uname = app.logged_in()
                    with ST.lock:
                        ST.qr_state, ST.qr_msg = "success", f"登录成功 · {uname}"
                    ST.add_log(f"[login] 登录成功! 欢迎你, {uname}")
                    return
                if code == 86038:
                    with ST.lock:
                        ST.qr_state, ST.qr_msg = "expired", "二维码已过期, 请重新获取"
                    ST.add_log("[login] 二维码已过期")
                    return
                if code == 86090:
                    with ST.lock:
                        ST.qr_state, ST.qr_msg = "scanned", "已扫码, 请在手机上确认"
            except Exception as e:
                ST.add_log(f"[login] 轮询异常: {e}")
            time.sleep(2)
        with ST.lock:
            ST.qr_state, ST.qr_msg = "expired", "等待超时, 请重新获取"

    threading.Thread(target=poll, daemon=True).start()
    return {"ok": True, "img": data_uri}


def collect_status():
    app = ST.app
    if app is None:
        return {"ok": True, "ready": False, "logged_in": False, "uname": "",
                "running": False, "likes": 0, "clicks": 0, "room": "",
                "elapsed": 0, "qr_state": ST.qr_state, "qr_msg": ST.qr_msg}
    s = app.stats
    running = app.running
    elapsed = int(time.time() - s["start"]) if (s.get("start") and running) else 0
    return {
        "ok": True, "ready": True,
        "logged_in": bool(getattr(app, "_was_logged_in", False)),
        "uname": getattr(app, "_uname", ""),
        "running": running,
        "likes": s["likes"], "clicks": s["clicks"],
        "room": app.cfg.get("room", ""),
        "wait_live": app.cfg.get("wait_live", True),
        "global_gap": app.cfg.get("global_gap", 4.0),
        "rooms": app.manager.snapshot(),
        "elapsed": elapsed,
        "qr_state": ST.qr_state, "qr_msg": ST.qr_msg,
    }


def refresh_account():
    app = ensure_app()
    ok, uname = app.logged_in()
    app._was_logged_in, app._uname = ok, uname
    return ok, uname


def build_handler(html_path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BiliLiveLike"

        def log_message(self, *a):   # 静默访问日志, 避免刷屏
            pass

        # ---------- 工具 ----------
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def _json_body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8") if n else "{}"
                return json.loads(raw or "{}")
            except Exception:
                return {}

        # ---------- 路由 ----------
        def do_GET(self):
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)

            if path in ("/", "/index.html"):
                try:
                    with open(html_path, "r", encoding="utf-8") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                except Exception as e:
                    return self._send(500, {"ok": False, "error": f"页面读取失败: {e}"})

            if path == "/api/status":
                # 每次轮询顺带轻量校验登录态 (频率够低, 不会触发风控)
                st = collect_status()
                return self._send(200, st)

            if path == "/api/log":
                since = int((qs.get("since") or ["0"])[0])
                lines, nxt = ST.logs_since(since)
                return self._send(200, {"ok": True, "lines": lines, "next": nxt})

            if path == "/api/qr/state":
                with ST.lock:
                    return self._send(200, {"ok": True, "state": ST.qr_state,
                                            "msg": ST.qr_msg})

            if path == "/api/room":
                raw = (qs.get("q") or [""])[0]
                app = ensure_app()
                info, err = app.resolve_room(raw)
                if not info:
                    return self._send(200, {"ok": False, "error": err})
                return self._send(200, {"ok": True, **info})

            if path == "/api/rooms":
                app = ensure_app()
                return self._send(200, {"ok": True, "rooms": app.manager.snapshot(),
                                        "global_gap": app.cfg.get("global_gap", 4.0)})

            if path == "/api/account":
                ok, uname = refresh_account()
                return self._send(200, {"ok": True, "logged_in": ok, "uname": uname})

            return self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            path = u.path
            body = self._json_body()

            if path == "/api/qr":
                return self._send(200, qr_start())

            if path == "/api/cookie":
                raw = (body.get("cookie") or "").strip()
                if not raw:
                    return self._send(200, {"ok": False, "error": "Cookie 内容为空"})
                app = ensure_app()
                n = app.set_cookie_string(raw)
                ok, uname = app.logged_in()
                app._was_logged_in, app._uname = ok, uname
                if ok:
                    ST.add_log(f"[cookie] 登录成功! 欢迎你, {uname}")
                return self._send(200, {"ok": ok, "fields": n, "uname": uname,
                                        "error": "" if ok else "Cookie 无效或已过期"})

            if path == "/api/rooms/add":
                app = ensure_app()
                raw = (body.get("room") or "").strip()
                if not raw:
                    return self._send(200, {"ok": False, "error": "请填写房间号"})
                ok, bad = app.manager.add_many([raw])
                if bad:
                    return self._send(200, {"ok": False, "error": bad[0][1]})
                return self._send(200, {"ok": True, "rooms": app.manager.snapshot()})

            if path == "/api/rooms/remove":
                app = ensure_app()
                raw = (body.get("room") or "").strip()
                done = app.manager.remove(raw)
                return self._send(200, {"ok": done, "rooms": app.manager.snapshot(),
                                        "error": "" if done else "没找到该房间"})

            if path == "/api/rooms/start":
                app = ensure_app()
                ok, uname = refresh_account()
                if not ok:
                    return self._send(200, {"ok": False, "error": "未登录，请先扫码或粘贴 Cookie"})
                cfg = app.cfg
                try:
                    cfg["interval_min"] = max(1.0, float(body.get("interval_min", 5)))
                    cfg["interval_max"] = max(cfg["interval_min"],
                                              float(body.get("interval_max", 8)))
                    cfg["click_min"] = max(1, int(body.get("click_min", 10)))
                    cfg["click_max"] = max(cfg["click_min"], int(body.get("click_max", 20)))
                    cfg["max_likes"] = max(0, int(body.get("max_likes", 1000)))
                    cfg["wait_live"] = bool(body.get("wait_live", True))
                    cfg["global_gap"] = max(0.0, float(body.get("global_gap", 4.0)))
                except (TypeError, ValueError):
                    return self._send(200, {"ok": False, "error": "参数请填写数字"})
                core.save_config(cfg)
                app.manager.gate.min_gap = cfg["global_gap"]
                targets = body.get("rooms") or []
                if targets:
                    started = 0
                    for t in targets:
                        task, err = app.manager.start(t)
                        if task:
                            started += 1
                    return self._send(200, {"ok": True, "started": started,
                                            "rooms": app.manager.snapshot()})
                if not app.manager.list():
                    return self._send(200, {"ok": False, "error": "任务列表是空的，先添加房间号"})
                n = app.manager.start_all()
                return self._send(200, {"ok": True, "started": n,
                                        "rooms": app.manager.snapshot()})

            if path == "/api/rooms/stop":
                app = ensure_app()
                raw = (body.get("room") or "").strip()
                n = app.manager.stop(raw or None)
                return self._send(200, {"ok": True, "stopped": n,
                                        "rooms": app.manager.snapshot()})

            if path == "/api/start":
                app = ensure_app()
                if app.running:
                    return self._send(200, {"ok": False, "error": "已在运行中"})
                ok, uname = refresh_account()
                if not ok:
                    return self._send(200, {"ok": False, "error": "未登录, 请先扫码或粘贴 Cookie"})
                room = core.normalize_room(body.get("room") or "")
                if not room:
                    return self._send(200, {"ok": False, "error": "房间号格式不对"})
                try:
                    cfg = app.cfg
                    cfg["interval_min"] = max(1.0, float(body.get("interval_min", 5)))
                    cfg["interval_max"] = max(cfg["interval_min"],
                                              float(body.get("interval_max", 8)))
                    cfg["click_min"] = max(1, int(body.get("click_min", 10)))
                    cfg["click_max"] = max(cfg["click_min"], int(body.get("click_max", 20)))
                    cfg["max_likes"] = max(0, int(body.get("max_likes", 1000)))
                    cfg["wait_live"] = bool(body.get("wait_live", True))
                except (TypeError, ValueError):
                    return self._send(200, {"ok": False, "error": "参数请填写数字"})
                core.save_config(cfg)
                app.start(room)
                return self._send(200, {"ok": True, "room": room, "uname": uname})

            if path == "/api/stop":
                app = ensure_app()
                app.stop()
                return self._send(200, {"ok": True})

            return self._send(404, {"ok": False, "error": "not found"})

    return Handler


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    ap = argparse.ArgumentParser(description="B站直播自动点赞 - 网页界面版")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--lan", action="store_true", help="允许局域网访问 (手机同 WiFi)")
    args = ap.parse_args()

    # 日志接管 (必须在创建 app 之前)
    sys.stdout = LogWriter(sys.__stdout__)
    sys.stderr = sys.stdout

    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, "web", "index.html"),
             os.path.join(getattr(sys, "_MEIPASS", here), "web", "index.html")]
    html_path = next((p for p in cands if os.path.exists(p)), cands[0])

    host = "0.0.0.0" if args.lan else "127.0.0.1"

    # 先在主线程把 app 建起来再开始监听:
    # 构造函数里有网络预取(数秒), 放在这里可以让"启动线程 / 第一个请求线程"都不可能
    # 撞进构造窗口, 顺带消掉首个请求的额外延迟。
    ensure_app()

    srv = ThreadingHTTPServer((host, args.port), build_handler(html_path))
    url = f"http://127.0.0.1:{args.port}"
    # 用核心里的编码安全 print: stdout 被重定向/cp1252 区域时, 中文横幅不能把服务打崩
    p = core._safe_print
    p("=" * 56)
    p("  B站直播自动点赞 · 网页界面版")
    p("=" * 56)
    p(f"  本机访问: {url}")
    if args.lan:
        p(f"  手机访问: http://{local_ip()}:{args.port}   (需同一 WiFi)")
    p("  按 Ctrl+C 退出")
    p("-" * 56)

    threading.Thread(target=lambda: (time.sleep(0.5), refresh_account(),
                                     ST.add_log("[init] 服务已启动")),
                     daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        p("\n[bye] 已退出")


if __name__ == "__main__":
    main()
