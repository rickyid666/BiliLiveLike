# -*- coding: utf-8 -*-
"""网页版端到端测试: 在临时目录起一个真实服务进程, 打真实 HTTP 请求

不碰真实配置/凭据 (全部复制到临时目录), 也不发任何点赞请求。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

try:
    import requests as _requests_probe  # noqa: F401
    HAS_DEPS = True
except ImportError:                     # 裸 python: 缺依赖时优雅跳过, CI 里一定会装
    HAS_DEPS = False
NEED_DEPS = unittest.skipUnless(HAS_DEPS, "需要 requests（pip install requests）")

import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["bili_like_api.py", "web_server.py"]
DIRS = [os.path.join("web", "index.html")]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(url, payload=None, timeout=15):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
        try:
            return r.status, json.loads(body)
        except json.JSONDecodeError:
            return r.status, body


@NEED_DEPS
class WebApiTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bll_web_")
        for f in FILES:
            shutil.copy(os.path.join(ROOT, f), os.path.join(cls.tmp, f))
        for d in DIRS:
            dst = os.path.join(cls.tmp, d)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(os.path.join(ROOT, d), dst)
        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "web_server.py", "--port", str(cls.port)],
            cwd=cls.tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{cls.port}"
        for _ in range(60):
            try:
                http(base + "/api/status", timeout=3)
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("web server 没起来")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.base = f"http://127.0.0.1:{self.port}"

    def test_01_page_served(self):
        status, body = http(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("任务列表", body)
        self.assertIn("viewport", body)

    def test_02_rooms_crud(self):
        _, r = http(self.base + "/api/rooms")
        self.assertTrue(r["ok"])
        self.assertEqual(r["rooms"], [])

        _, r = http(self.base + "/api/rooms/add", {"room": "123456"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["rooms"]), 1)

        _, r = http(self.base + "/api/rooms/add", {"room": "123456"})
        self.assertEqual(len(r["rooms"]), 1, "重复添加同一房间应去重")

        _, r = http(self.base + "/api/rooms/add", {"room": "https://live.bilibili.com/123456"})
        self.assertEqual(len(r["rooms"]), 1, "同一房间的链接形式也应去重")

        _, r = http(self.base + "/api/rooms/add", {"room": "abc"})
        self.assertFalse(r["ok"])
        self.assertIn("格式", r["error"])

        _, r = http(self.base + "/api/rooms/remove", {"room": "123456"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["rooms"], [])

    def test_03_status_shape(self):
        http(self.base + "/api/rooms/add", {"room": "654321"})
        _, st = http(self.base + "/api/status")
        self.assertTrue(st["ok"])
        for key in ("logged_in", "running", "likes", "clicks", "rooms",
                    "wait_live", "global_gap"):
            self.assertIn(key, st)
        self.assertIsInstance(st["rooms"], list)
        row = st["rooms"][0]
        for key in ("room", "state", "msg", "likes", "clicks", "alive"):
            self.assertIn(key, row)
        self.assertEqual(row["state"], "idle")

    def test_04_start_requires_login(self):
        _, r = http(self.base + "/api/rooms/start", {"rooms": ["654321"]})
        self.assertFalse(r["ok"])
        self.assertIn("登录", r["error"])

    def test_05_stop_and_logs(self):
        _, r = http(self.base + "/api/rooms/stop", {})
        self.assertTrue(r["ok"])
        _, r = http(self.base + "/api/log?since=0")
        self.assertTrue(r["ok"])
        self.assertIsInstance(r["lines"], list)
        self.assertIn("next", r)


class AppSingletonTest(unittest.TestCase):
    """ensure_app() 的并发回归测试

    真实故障: ensure_app() 原本是无锁的 `if ST.app is None: ST.app = new()`,
    而 BiliLikeApi 构造函数里有数秒的网络预取, 竞态窗口很大。
    ThreadingHTTPServer 每请求一线程 + 启动时还有个 refresh_account 线程,
    于是可能造出两个实例: 一个请求加到 A 实例、下一个请求操作 B 实例,
    表现为"刚加进去的房间消失"(网页端 e2e 曾以约 1/3 概率复现)。
    """

    def test_concurrent_ensure_app_creates_one_instance(self):
        sys.path.insert(0, ROOT)
        import threading as _th

        import bili_like_api as core_mod
        import web_server as ws

        created = []
        real_cls = core_mod.BiliLikeApi

        class SlowFake:
            """带延迟的假实例: 放大竞态窗口, 让无锁实现必然暴露"""

            def __init__(self):
                time.sleep(0.05)
                created.append(self)

        ws.core.BiliLikeApi = SlowFake          # web_server 用的是 core.BiliLikeApi
        saved_app = ws.ST.app
        ws.ST.app = None
        try:
            results = []
            lock = _th.Lock()

            def worker():
                inst = ws.ensure_app()
                with lock:
                    results.append(inst)

            threads = [_th.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)

            self.assertEqual(len(created), 1,
                             "并发调用只应构造一个实例, 实际构造了 %d 个" % len(created))
            self.assertEqual(len({id(r) for r in results}), 1,
                             "所有调用者都应拿到同一个实例")
        finally:
            ws.core.BiliLikeApi = real_cls
            ws.ST.app = saved_app


if __name__ == "__main__":
    unittest.main(verbosity=2)
