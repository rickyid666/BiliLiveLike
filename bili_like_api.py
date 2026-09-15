# -*- coding: utf-8 -*-
"""
B站直播自动点赞工具 - 纯接口后台版
================================================================
不弹任何窗口、不模拟鼠标、不碰你的屏幕:
  - 登录: 终端里显示二维码, 用 B 站手机 App 扫码 (或直接粘贴浏览器 Cookie)
  - 点赞: 调用 B 站官方直播间点赞接口 (和你在直播间双击点赞走的是同一个接口)
  - 循环: 随机间隔 + 随机连击数, 尽量贴近真人节奏

依赖: pip install requests qrcode

控制台命令:
  login    扫码登录 (终端显示二维码)
  paste    粘贴 Cookie 登录 (浏览器 F12 -> Network -> 复制 Cookie 请求头)
  whoami   检查登录状态
  go [房间号或链接]  开始点赞 (不带参数用上次房间)
  stop     停止点赞
  status   查看状态
  quit     退出
"""

import json
import os
import random
import re
import sys
import threading
import time

try:
    import requests
except ImportError:
    requests = None

try:
    import qrcode
except ImportError:
    qrcode = None

def _app_dir():
    """兼容 PyInstaller 打包: exe 运行时文件要放在 exe 旁边, 而不是临时解压目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _app_dir()
COOKIE_PATH = os.path.join(BASE_DIR, "cookies.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config_api.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DEFAULT_CONFIG = {
    "room": "",
    "interval_min": 5.0,    # 两次请求最小间隔(秒)
    "interval_max": 8.0,    # 两次请求最大间隔(秒)
    "click_min": 10,        # 每次请求的连击数下限 (相当于一次顶 10~20 赞)
    "click_max": 20,
    "max_likes": 1000,      # 单场直播点赞上限(B站单场点赞获取上限约1000)
}

QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
ROOM_INIT = "https://api.live.bilibili.com/room/v1/Room/room_init?id={}"
NAV = "https://api.bilibili.com/x/web-interface/nav"
LIKE_REPORT = "https://api.live.bilibili.com/xlive/app-ucenter/v1/like_info_v3/like/likeReportV3"


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def normalize_room(raw: str):
    raw = raw.strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d+", raw):
        return raw
    m = re.search(r"live\.bilibili\.com/(?:h5/)?(\d+)", raw)
    return m.group(1) if m else ""


def print_qr_console(qr):
    """GBK/UTF-8 控制台都安全的二维码打印 (只用全块字符, 反色适配深色终端)"""
    try:
        for row in qr.get_matrix():
            print("".join("  " if v else "██" for v in row))
    except UnicodeEncodeError:
        # 极端情况: 控制台连 █ 都打不出来, 退化为纯 ASCII
        for row in qr.get_matrix():
            print("".join("  " if v else "##" for v in row))


class BiliLikeApi:
    def __init__(self, prefetch=True):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.cfg = load_config()
        self.running = False
        self.paused = False
        self.stats = {"likes": 0, "clicks": 0, "room": "", "start": None}
        self._stop = threading.Event()
        self._uid = ""
        self._csrf = ""
        self._load_cookies()
        # 预取 buvid3/buvid4, 降低风控概率 (GUI 等场景可关掉以免拖慢启动)
        if prefetch:
            self.prefetch()

    def prefetch(self):
        try:
            self.s.get("https://www.bilibili.com/", timeout=10)
        except Exception:
            pass

    # ---------- Cookie ----------
    def _save_cookies(self):
        data = {c.name: c.value for c in self.s.cookies}
        with open(COOKIE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load_cookies(self):
        try:
            with open(COOKIE_PATH, "r", encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    self.s.cookies.set(k, v, domain=".bilibili.com")
        except Exception:
            pass

    def set_cookie_string(self, raw: str):
        """解析浏览器复制的 Cookie 请求头"""
        raw = raw.strip()
        if raw.startswith("Cookie:"):
            raw = raw[7:]
        n = 0
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                self.s.cookies.set(k.strip(), v.strip(), domain=".bilibili.com")
                n += 1
        if n:
            self._save_cookies()
        return n

    def logged_in(self):
        try:
            r = self.s.get(NAV, timeout=10).json()
            return r.get("data", {}).get("isLogin", False), r.get("data", {}).get("uname", "")
        except Exception:
            return False, ""

    # ---------- 登录 ----------
    def login_qr(self):
        if qrcode is None:
            print("[login] 缺少 qrcode 库, 请先: pip install qrcode  (或用 paste 命令粘贴Cookie)")
            return False
        r = self.s.get(QR_GENERATE, timeout=10).json()
        if r.get("code") != 0:
            print("[login] 获取二维码失败:", r)
            return False
        url, key = r["data"]["url"], r["data"]["qrcode_key"]
        print("[login] 请用 哔哩哔哩手机App 扫描下方二维码登录 (有效期约3分钟):")
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        print_qr_console(qr)
        print("[login] 等待扫码确认...")
        t0 = time.time()
        while time.time() - t0 < 180:
            p = self.s.get(QR_POLL, params={"qrcode_key": key}, timeout=10).json()
            code = p.get("data", {}).get("code")
            if code == 0:
                self._save_cookies()
                ok, uname = self.logged_in()
                print(f"[login] 登录成功! 欢迎你, {uname}")
                return True
            elif code == 86038:
                print("[login] 二维码已过期, 请重新输入 login")
                return False
            elif code == 86090:
                print("[login] 已扫码, 请在手机上确认...")
            time.sleep(2)
        print("[login] 等待超时")
        return False

    # ---------- 点赞 ----------
    def resolve_room(self, raw: str):
        room = normalize_room(raw or self.cfg["room"])
        if not room:
            return None, "房间号格式不对"
        r = self.s.get(ROOM_INIT.format(room), timeout=10).json()
        if r.get("code") != 0:
            return None, f"房间解析失败: {r.get('message')}"
        d = r["data"]
        return {"room_id": d["room_id"], "anchor_id": d["uid"],
                "live_status": d.get("live_status", 0), "short": room}, ""

    def _like_once(self, info):
        click_time = random.randint(self.cfg["click_min"], self.cfg["click_max"])
        data = {
            "click_time": click_time,
            "room_id": info["room_id"],
            "anchor_id": info["anchor_id"],
            "uid": self._uid,
            "csrf_token": self._csrf,
            "csrf": self._csrf,
            "visit_id": "%032x" % random.getrandbits(128),
        }
        headers = {
            "Referer": f"https://live.bilibili.com/{info['short']}",
            "Origin": "https://live.bilibili.com",
        }
        r = self.s.post(LIKE_REPORT, data=data, headers=headers, timeout=10).json()
        return r, click_time

    def _task_loop(self, raw_room: str):
        info, err = self.resolve_room(raw_room)
        if not info:
            print(f"[task] {err}")
            self.running = False
            return
        if not info["live_status"]:
            print("[task] 该直播间当前未开播, 点赞无效. 任务停止")
            self.running = False
            return
        self.stats.update({"room": info["room_id"], "start": time.time()})
        print(f"[task] 目标房间 {info['room_id']} (开播中), 开始点赞")
        print(f"[task] 间隔 {self.cfg['interval_min']}~{self.cfg['interval_max']}s, "
              f"每次连击 {self.cfg['click_min']}~{self.cfg['click_max']} 赞, "
              f"上限 {self.cfg['max_likes']}")
        while not self._stop.is_set():
            delay = random.uniform(self.cfg["interval_min"], self.cfg["interval_max"])
            end = time.time() + delay
            while time.time() < end and not self._stop.is_set():
                time.sleep(0.1)
            if self._stop.is_set():
                break
            try:
                r, click_time = self._like_once(info)
            except Exception as e:
                print(f"[task] 网络异常: {e}, 重试中")
                time.sleep(3)
                continue
            self.stats["clicks"] += 1
            code = r.get("code")
            if code == 0:
                self.stats["likes"] += click_time
                print(f"[task] 第{self.stats['clicks']}次请求 OK (+{click_time} 赞, "
                      f"累计 {self.stats['likes']})")
            elif code == -101:
                print("[task] 登录已失效! 请重新 login 或 paste. 任务停止")
                break
            elif code == -352:
                print("[task] 触发风控(-352), 自动放慢速度...")
                self.cfg["interval_min"] = min(self.cfg["interval_min"] + 3, 30)
                self.cfg["interval_max"] = min(self.cfg["interval_max"] + 5, 60)
                save_config(self.cfg)
                time.sleep(10)
            else:
                print(f"[task] 返回异常 code={code} message={r.get('message')}")
                time.sleep(3)
            limit = self.cfg["max_likes"]
            if limit and self.stats["likes"] >= limit:
                print(f"[task] 已达单场点赞上限({limit}), 任务完成, 自动停止")
                break
        self.running = False
        print("[task] 点赞已停止")

    def start(self, room: str = ""):
        if self.running:
            print("[task] 已在运行中")
            return
        ok, uname = self.logged_in()
        if not ok:
            print("[task] 未登录! 请先输入 login 扫码 或 paste 粘贴Cookie")
            return
        self._uid = self.s.cookies.get("DedeUserID", "")
        self._csrf = self.s.cookies.get("bili_jct", "")
        target = normalize_room(room) or self.cfg["room"]
        if room:
            self.cfg["room"] = target
            save_config(self.cfg)
        self._stop.clear()
        self.running = True
        threading.Thread(target=self._task_loop, args=(target,), daemon=True).start()

    def stop(self):
        if self.running:
            self._stop.set()
            print("[task] 正在停止...")

    def status(self):
        ok, uname = self.logged_in()
        dur = f", 已运行 {int(time.time()-self.stats['start'])}s" if self.stats["start"] else ""
        print(f"[status] 账号: {uname if ok else '未登录'} | 房间: {self.cfg['room'] or '未设置'} | "
              f"已点赞: {self.stats['likes']} ({self.stats['clicks']} 次请求){dur} | "
              f"状态: {'运行中' if self.running else '空闲'}{'(暂停)' if self.paused else ''}")


def _setup_console():
    """Windows 控制台适配: 切 UTF-8 代码页, 防止中文/二维码字符乱码或报错"""
    if os.name == "nt":
        os.system("chcp 65001 >nul")
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _setup_console()
    if requests is None:
        print("[error] 缺少依赖库 requests, 请在命令行执行:")
        print("        pip install requests qrcode")
        input("按回车退出...")
        return
    print("=" * 56)
    print("  B站直播自动点赞 - 纯接口后台版 (无窗口/无鼠标模拟)")
    print("=" * 56)
    app = BiliLikeApi()
    ok, uname = app.logged_in()
    print(f"[init] 登录状态: {uname if ok else '未登录 (先输入 login 或 paste)'}")
    print()
    print("命令: login 扫码 | paste 粘贴Cookie | go [房间号] 开始 | stop | status | quit")
    print("-" * 56)
    while True:
        try:
            cmd = input(">> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        low = cmd.lower()
        if low in ("quit", "q", "exit"):
            break
        elif low == "login":
            app.login_qr()
        elif low == "paste":
            print("粘贴浏览器里的 Cookie 请求头内容后回车:")
            try:
                raw = input("Cookie> ")
            except EOFError:
                continue
            n = app.set_cookie_string(raw)
            ok2, uname2 = app.logged_in()
            print(f"[paste] 已读取 {n} 个字段, 登录状态: {uname2 if ok2 else '无效, 请检查是否复制完整'}")
        elif low == "whoami":
            app.status()
        elif low == "go" or low.startswith("go "):
            app.start(cmd[2:].strip())
        elif low == "stop":
            app.stop()
        elif low == "status":
            app.status()
        else:
            print("未知命令. 可用: login / paste / go [房间号] / stop / status / quit")
    app.stop()
    print("[bye] 已退出")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        print("[error] 发生未预期的错误 (见上方信息), 可截图反馈")
        try:
            input("按回车退出...")
        except Exception:
            pass
    finally:
        # 双击运行时防止窗口一闪而过
        try:
            input("按回车退出...")
        except Exception:
            pass
