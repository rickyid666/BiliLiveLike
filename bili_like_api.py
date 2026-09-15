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
    "wait_live": True,      # 未开播时每分钟自动检查, 开播后自动开始
    "rooms": [],            # 多房间任务列表(自动保存)
    "global_gap": 4.0,      # 全局请求闸门: 同一账号任意两次请求至少间隔(秒), 风控关键
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


class RequestGate:
    """全局请求闸门: 同一账号的请求串行放行, 且彼此至少间隔 min_gap 秒

    多房间并发时这是最关键的一环 —— 各任务独立计时互不等待, 但真正出网的请求
    仍然是一条线, 不会出现"同一账号几秒内并发打多个接口"这种明显的自动化特征。
    """

    def __init__(self, min_gap=4.0):
        self.min_gap = float(min_gap)
        self._lock = threading.Lock()
        self._last = 0.0
        self.waited = 0            # 被闸门拦下等待的次数(用于状态展示)

    def call(self, fn, stop_flag=None):
        """串行执行 fn; 若还未到最小间隔则等待。等待中被停止则返回 None"""
        with self._lock:
            end = self._last + self.min_gap
            now = time.time()
            if now < end:
                self.waited += 1
                while time.time() < end:
                    if stop_flag is not None and stop_flag.is_set():
                        return None
                    time.sleep(min(0.1, max(0.0, end - time.time())))
            try:
                return fn()
            finally:
                self._last = time.time()


class RoomTask:
    """单个直播间的点赞任务: 独立房间 / 独立状态 / 独立计时器 / 独立计数"""

    # 各种等待时长 (抽成类常量, 方便测试里缩短, 也便于以后做界面可调)
    WAIT_LIVE_INTERVAL = 60      # 未开播时每隔多久查一次
    RETRY_ON_ERROR = 3           # 普通异常后的重试等待
    SLOW_DOWN_WAIT = 10          # 触发风控后的额外等待

    def __init__(self, mgr, raw):
        self.mgr = mgr
        self.raw = normalize_room(raw) or raw
        self.short = self.raw
        self.room_id = None
        self.state = "idle"        # idle / waiting / running / done / error / stopped
        self.msg = "待启动"
        self.likes = 0
        self.clicks = 0
        self.started_at = None
        self.stop_flag = threading.Event()
        self.thread = None

    def snapshot(self):
        return {
            "room": self.raw,
            "room_id": self.room_id,
            "state": self.state,
            "msg": self.msg,
            "likes": self.likes,
            "clicks": self.clicks,
            "alive": bool(self.thread and self.thread.is_alive()),
            "elapsed": (int(time.time() - self.started_at)
                        if self.started_at and self.thread and self.thread.is_alive() else 0),
        }

    def log(self, text):
        print(f"[task {self.short}] {text}")

    def start(self):
        if self.thread and self.thread.is_alive():
            return False
        self.stop_flag.clear()
        self.state, self.msg = "waiting", "启动中"
        self.thread = threading.Thread(target=self.run, daemon=True,
                                       name=f"room-{self.raw}")
        self.thread.start()
        return True

    def stop(self):
        self.stop_flag.set()

    def _sleep(self, seconds):
        """可被打断的等待; 返回 True 表示应中断"""
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_flag.is_set():
                return True
            time.sleep(min(0.1, max(0.0, end - time.time())))
        return self.stop_flag.is_set()

    def run(self):
        app, gate, cfg = self.mgr.app, self.mgr.gate, self.mgr.app.cfg
        try:
            res = gate.call(lambda: app.resolve_room(self.raw), self.stop_flag)
            if res is None:
                return
            info, err = res
            if not info:
                self.state, self.msg = "error", err or "房间解析失败"
                self.log(self.msg)
                return
            self.room_id = info["room_id"]
            self.short = info.get("short") or self.raw

            # (1) 未开播: 按配置决定等待还是收工
            if not info["live_status"]:
                if not cfg.get("wait_live", True):
                    self.state, self.msg = "done", "未开播，已结束"
                    self.log("该直播间当前未开播，点赞无效，任务结束")
                    return
                self.state, self.msg = "waiting", "等待开播"
                self.log("当前未开播，每分钟自动检查一次…")
                waited = 0
                while not self.stop_flag.is_set():
                    if self._sleep(self.WAIT_LIVE_INTERVAL):
                        return
                    waited += 1
                    res2 = gate.call(lambda: app.resolve_room(self.raw), self.stop_flag)
                    if res2 is None:
                        return
                    again, _e2 = res2
                    if again and again.get("live_status"):
                        info = again
                        self.log(f"检测到已开播（已等待 {waited} 次检查），开始点赞")
                        break
                    self.log(f"还没开播，已检查 {waited} 次…")
                if self.stop_flag.is_set():
                    return

            # (2) 开播: 进入点赞循环
            self.state, self.msg = "running", "运行中"
            self.started_at = time.time()
            self.log(f"开始点赞（间隔 {cfg['interval_min']}~{cfg['interval_max']}s，"
                     f"每次 {cfg['click_min']}~{cfg['click_max']} 赞，上限 {cfg['max_likes']}）")

            while not self.stop_flag.is_set():
                if self._sleep(random.uniform(cfg["interval_min"], cfg["interval_max"])):
                    return
                res3 = gate.call(lambda: app._like_once(info), self.stop_flag)
                if res3 is None:
                    return
                r, click_time = res3
                self.clicks += 1
                code = r.get("code")
                if code == 0:
                    self.likes += click_time
                    self.log(f"第{self.clicks}次请求 OK (+{click_time} 赞，累计 {self.likes})")
                elif code == -101:
                    self.state, self.msg = "error", "登录已失效"
                    self.log("登录已失效，请重新登录，任务停止")
                    return
                elif code == -352:
                    self.msg = "风控降速中"
                    self.log("触发风控(-352)，自动放慢速度…")
                    cfg["interval_min"] = min(cfg["interval_min"] + 3, 30)
                    cfg["interval_max"] = min(cfg["interval_max"] + 5, 60)
                    save_config(cfg)
                    if self._sleep(self.SLOW_DOWN_WAIT):
                        return
                else:
                    self.log(f"返回异常 code={code} message={r.get('message')}")
                    if self._sleep(self.RETRY_ON_ERROR):
                        return
                limit = cfg["max_likes"]
                if limit and self.likes >= limit:
                    self.state, self.msg = "done", f"已达上限 {limit}"
                    self.log(f"已达单场点赞上限({limit})，任务完成")
                    return
        except Exception as e:
            self.state, self.msg = "error", f"异常: {e}"
            self.log(f"任务异常: {e}")
        finally:
            if self.state not in ("done", "error"):
                self.state, self.msg = "stopped", "已停止"
            self.log("任务结束")


class TaskManager:
    """多直播间任务调度: 统一添加 / 启动 / 停止 / 查询, 并保证全局限速"""

    def __init__(self, app):
        self.app = app
        self.tasks = {}                       # short_room -> RoomTask (保持插入顺序)
        self._lock = threading.RLock()
        self.gate = RequestGate(min_gap=app.cfg.get("global_gap", 4.0))

    # ---------- 增删 ----------
    def add(self, raw):
        room = normalize_room(raw)
        if not room:
            return None, "房间号格式不对（支持纯数字或直播间链接）"
        with self._lock:
            if room in self.tasks:
                return self.tasks[room], ""
            task = RoomTask(self, room)
            self.tasks[room] = task
            self._save()
            return task, ""

    def add_many(self, raws):
        ok, bad = 0, []
        for r in raws:
            t, err = self.add(r)
            if t:
                ok += 1
            else:
                bad.append((r, err))
        return ok, bad

    def remove(self, raw):
        room = normalize_room(raw) or raw
        with self._lock:
            task = self.tasks.pop(room, None)
            if task:
                task.stop()
                self._save()
            return task is not None

    def get(self, raw):
        return self.tasks.get(normalize_room(raw) or raw)

    def list(self):
        with self._lock:
            return list(self.tasks.values())

    # ---------- 启动 / 停止 ----------
    def start(self, raw):
        task, err = self.add(raw)
        if not task:
            return None, err
        task.start()
        return task, ""

    def start_all(self):
        n = 0
        for t in self.list():
            if t.start():
                n += 1
        return n

    def stop(self, raw=None):
        if raw:
            t = self.get(raw)
            if t:
                t.stop()
                return 1
            return 0
        n = 0
        for t in self.list():
            if t.thread and t.thread.is_alive():
                t.stop()
                n += 1
        return n

    # ---------- 统计 ----------
    def active_count(self):
        return sum(1 for t in self.list() if t.thread and t.thread.is_alive())

    def totals(self):
        likes = sum(t.likes for t in self.list())
        clicks = sum(t.clicks for t in self.list())
        return likes, clicks

    def snapshot(self):
        return [t.snapshot() for t in self.list()]

    # ---------- 持久化 ----------
    def _save(self):
        try:
            self.app.cfg["rooms"] = [t.raw for t in self.list()]
            save_config(self.app.cfg)
        except Exception:
            pass

    def load_saved(self):
        for raw in list(self.app.cfg.get("rooms", [])):
            self.add(raw)


class BiliLikeApi:
    def __init__(self, prefetch=True):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.cfg = load_config()
        self.paused = False
        self._stop = threading.Event()
        self.manager = TaskManager(self)     # 多房间任务调度
        self.manager.load_saved()
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

    # ---------- 对外接口(兼容旧的单房间用法) ----------
    @property
    def running(self):
        """是否有任务在跑(多房间时 = 任一任务存活)"""
        return self.manager.active_count() > 0

    @property
    def stats(self):
        """汇总统计(GUI / 网页端直接用这个)"""
        likes, clicks = self.manager.totals()
        starts = [t.started_at for t in self.manager.list() if t.started_at]
        return {
            "likes": likes,
            "clicks": clicks,
            "room": self.cfg.get("room", ""),
            "start": min(starts) if starts else None,
            "rooms": len(self.manager.list()),
            "active": self.manager.active_count(),
        }

    def add_room(self, *rooms):
        """添加房间任务(不启动)"""
        ok, bad = self.manager.add_many(rooms)
        for t in self.manager.list():
            if t.raw in [normalize_room(r) for r in rooms]:
                print(f"[task {t.short}] 已加入任务列表({t.state})")
        for r, err in bad:
            print(f"[task] {r}: {err}")
        return ok

    def start(self, room: str = ""):
        """启动任务: 带房间号=添加并启动该房间; 不带=启动全部已添加的房间"""
        ok, uname = self.logged_in()
        if not ok:
            print("[task] 未登录! 请先输入 login 扫码 或 paste 粘贴Cookie")
            return
        self._uid = self.s.cookies.get("DedeUserID", "")
        self._csrf = self.s.cookies.get("bili_jct", "")

        target = normalize_room(room)
        if target:
            self.cfg["room"] = target
            save_config(self.cfg)
            task, err = self.manager.start(target)
            if not task:
                print(f"[task] {err}")
                return
            print(f"[task {task.short}] 已启动")
            return

        if not self.manager.list():
            print("[task] 任务列表是空的, 先 add 房间号 或 go 房间号")
            return
        n = self.manager.start_all()
        print(f"[task] 已启动 {n} 个房间任务"
              f"(全局请求间隔 {self.manager.gate.min_gap:g}s, 避免并发特征)")

    def stop(self, room: str = ""):
        if room:
            n = self.manager.stop(room)
            print(f"[task] 正在停止 {normalize_room(room) or room} ..." if n
                  else f"[task] 没找到房间 {room}")
            return
        n = self.manager.stop()
        print(f"[task] 正在停止 {n} 个任务 ..." if n else "[task] 当前没有运行中的任务")

    def remove_room(self, room):
        return self.manager.remove(room)

    def status(self):
        ok, uname = self.logged_in()
        likes, clicks = self.manager.totals()
        tasks = self.manager.list()
        active = self.manager.active_count()
        print(f"[status] 账号: {uname if ok else '未登录'} | "
              f"任务 {len(tasks)} 个(运行中 {active}) | "
              f"已点赞 {likes} ({clicks} 次请求) | "
              f"全局间隔 {self.manager.gate.min_gap:g}s | "
              f"状态: {'运行中' if active else '空闲'}")
        if tasks:
            print("        房间            状态      已点赞  请求")
            for t in tasks:
                print(f"        {t.short:<14} {t.msg:<8} {t.likes:>6}  {t.clicks:>4}")


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
    print("命令:")
    print("  add 房间号 [房间号...]   添加房间任务(可一次多个)")
    print("  list                     查看任务列表")
    print("  go [房间号...]           启动(带房间号则先添加再启动; 不带则启动全部)")
    print("  stop [房间号]            停止某个房间; 不带参数=全部停止")
    print("  rm 房间号                移除任务")
    print("  status / login / paste / quit")
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
            rooms = cmd[2:].split()
            if rooms:
                app.add_room(*rooms)          # 先加入任务列表
            app.start()                       # 不带参数 = 启动全部
        elif low == "add" or low.startswith("add "):
            rooms = cmd[3:].split()
            if not rooms:
                print("用法: add 房间号 [房间号...]")
            else:
                app.add_room(*rooms)
        elif low == "rm" or low.startswith("rm "):
            rooms = cmd[2:].split()
            if not rooms:
                print("用法: rm 房间号")
            for r in rooms:
                print(f"[task] 已移除 {r}" if app.remove_room(r) else f"[task] 没找到 {r}")
        elif low == "list":
            app.status()
        elif low == "stop" or low.startswith("stop "):
            app.stop(cmd[4:].strip())
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
