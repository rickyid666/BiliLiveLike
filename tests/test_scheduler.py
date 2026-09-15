# -*- coding: utf-8 -*-
"""
BiliLiveLike 自动化测试 (只用标准库 unittest, 不引入 pytest 依赖)

覆盖:
  - 全局请求闸门: 串行 + 最小间隔 + 可被停止打断
  - 单房间任务: 未开播/等开播/风控降速/登录失效/达标结束 状态机
  - 任务管理器: 添加去重/删除/单独启动停止/汇总统计
  - 对外兼容: app.running / app.stats 汇总口径

跑法:
  python -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

try:
    import requests as _requests_probe  # noqa: F401
    HAS_DEPS = True
except ImportError:                     # 裸 python: 缺依赖时优雅跳过, CI 里一定会装
    HAS_DEPS = False
NEED_DEPS = unittest.skipUnless(HAS_DEPS, "需要 requests（pip install requests）")


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load_core(config_dir):
    spec = importlib.util.spec_from_file_location(
        "bll_core_%d" % time.time_ns(), os.path.join(ROOT, "bili_like_api.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    # 把配置/凭据落到临时目录, 绝不碰真实配置
    m.CONFIG_PATH = os.path.join(config_dir, "config_test.json")
    m.COOKIE_PATH = os.path.join(config_dir, "cookies_test.json")
    return m


class FakeEngine:
    """可编程的假引擎: 决定每个房间的直播状态, 并按脚本返回点赞接口的 code"""

    def __init__(self, live=None, codes=None):
        self.live = dict(live or {})
        self.codes = list(codes or [])
        self.calls = []
        self.lock = threading.Lock()

    def install(self, app):
        app.resolve_room = self.resolve_room
        app._like_once = self.like_once
        return app

    def resolve_room(self, raw):
        r = core.normalize_room(raw)
        st = self.live.get(r, 1)
        if st == -1:
            return None, "房间不存在"
        return {"room_id": int(r), "anchor_id": 1, "live_status": st, "short": r}, ""

    def like_once(self, info):
        with self.lock:
            self.calls.append((time.time(), info["short"]))
            code = self.codes.pop(0) if self.codes else 0
        time.sleep(0.01)
        return {"code": code, "message": str(code)}, 10

    def gaps(self):
        with self.lock:
            c = list(self.calls)
        return [round(c[i + 1][0] - c[i][0], 3) for i in range(len(c) - 1)]


TMP = tempfile.mkdtemp(prefix="bll_test_")
core = load_core(TMP)
core.RoomTask.WAIT_LIVE_INTERVAL = 0.05     # 测试里把"每分钟"压到 50ms
core.RoomTask.RETRY_ON_ERROR = 0.02
core.RoomTask.SLOW_DOWN_WAIT = 0.02


def wait_until(pred, timeout=3.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


@NEED_DEPS
class SchedulerTest(unittest.TestCase):

    def make_app(self, **cfg):
        app = core.BiliLikeApi(prefetch=False)
        app.cfg.update({"interval_min": 0.01, "interval_max": 0.02,
                        "click_min": 10, "click_max": 10,
                        "max_likes": 100, "wait_live": True, "global_gap": 0.05})
        app.cfg.update(cfg)
        app.manager.gate.min_gap = app.cfg["global_gap"]
        return app

    def tearDown(self):
        for t in self.app.manager.list() if hasattr(self, "app") else []:
            t.stop()
        time.sleep(0.05)
        if hasattr(self, "app"):
            state = os.path.join(TMP, "config_test.json")
            if os.path.exists(state):
                os.remove(state)

    # ---------- 闸门 ----------
    def test_gate_serializes_and_paces(self):
        self.app = self.make_app()
        eng = FakeEngine()
        eng.install(self.app)
        self.app.manager.add_many(["111", "222", "333"])
        self.app.manager.start_all()
        self.assertTrue(wait_until(lambda: len(eng.calls) >= 6, 3.0))
        self.app.manager.stop()
        gaps = eng.gaps()
        self.assertTrue(gaps, "应当有多次请求")
        self.assertTrue(all(g >= self.app.cfg["global_gap"] - 0.01 for g in gaps),
                        f"请求应当被闸门拉开: {gaps}")

    def test_gate_aborts_when_stopped(self):
        gate = core.RequestGate(min_gap=5.0)
        flag = threading.Event()
        out = {}

        def worker():
            gate.call(lambda: "done", flag)      # 先占一次, 让闸门进入等待
        th = threading.Thread(target=worker)
        th.start()
        time.sleep(0.05)

        def worker2():
            out["r"] = gate.call(lambda: "never", flag)
        th2 = threading.Thread(target=worker2)
        th2.start()
        time.sleep(0.05)
        flag.set()
        th2.join(2.0)
        th.join(2.0)
        self.assertIsNone(out.get("r"), "等待中被停止应返回 None")

    # ---------- 单房间状态机 ----------
    def test_not_live_and_wait_disabled_ends(self):
        self.app = self.make_app(wait_live=False)
        FakeEngine(live={"111": 0}).install(self.app)
        task, _ = self.app.manager.start("111")
        self.assertTrue(wait_until(lambda: not task.thread.is_alive(), 2.0))
        self.assertEqual(task.state, "done")
        self.assertIn("未开播", task.msg)

    def test_waits_for_live_then_starts(self):
        self.app = self.make_app()
        eng = FakeEngine(live={"111": 0})
        eng.install(self.app)

        def flip():
            time.sleep(0.15)
            eng.live["111"] = 1
        threading.Thread(target=flip, daemon=True).start()

        task, _ = self.app.manager.start("111")
        self.assertTrue(wait_until(lambda: task.state == "running", 2.0),
                        f"应在开播后自动进入运行中, 当前 {task.state}/{task.msg}")
        self.assertTrue(wait_until(lambda: task.likes > 0, 2.0))
        task.stop()

    def test_login_invalid_marks_error(self):
        self.app = self.make_app()
        FakeEngine(codes=[-101]).install(self.app)
        task, _ = self.app.manager.start("111")
        self.assertTrue(wait_until(lambda: not task.thread.is_alive(), 2.0))
        self.assertEqual(task.state, "error")
        self.assertIn("登录", task.msg)

    def test_rate_limit_slows_down_then_keeps_going(self):
        self.app = self.make_app()
        eng = FakeEngine(codes=[-352])          # 第一次风控, 之后正常
        eng.install(self.app)
        task, _ = self.app.manager.start("111")
        self.assertTrue(wait_until(lambda: self.app.cfg["interval_min"] > 0.01, 2.0),
                        "风控后应放慢间隔")
        self.assertTrue(task.thread.is_alive(), "放慢不等于退出, 任务应继续")
        task.stop()

    def test_max_likes_finishes_task(self):
        self.app = self.make_app(max_likes=20)
        FakeEngine().install(self.app)
        task, _ = self.app.manager.start("111")
        self.assertTrue(wait_until(lambda: task.state == "done", 3.0))
        self.assertEqual(task.likes, 20)
        self.assertIn("上限", task.msg)

    def test_resolve_failure_marks_error(self):
        self.app = self.make_app()
        FakeEngine(live={"111": -1}).install(self.app)
        task, _ = self.app.manager.start("111")
        self.assertTrue(wait_until(lambda: not task.thread.is_alive(), 2.0))
        self.assertEqual(task.state, "error")

    # ---------- 管理器 ----------
    def test_add_dedupe_and_remove(self):
        self.app = self.make_app()
        t1, _ = self.app.manager.add("111")
        t2, _ = self.app.manager.add("https://live.bilibili.com/111")
        self.assertIs(t1, t2, "同一房间应复用任务")
        self.assertEqual(len(self.app.manager.list()), 1)
        self.assertTrue(self.app.manager.remove("111"))
        self.assertEqual(len(self.app.manager.list()), 0)

    def test_invalid_room_rejected(self):
        self.app = self.make_app()
        task, err = self.app.manager.add("abc")
        self.assertIsNone(task)
        self.assertIn("格式", err)

    def test_stop_one_keeps_others_running(self):
        self.app = self.make_app()
        FakeEngine().install(self.app)
        a, _ = self.app.manager.start("111")
        b, _ = self.app.manager.start("222")
        self.assertTrue(wait_until(lambda: a.likes > 0 and b.likes > 0, 3.0))
        self.app.manager.stop("111")
        self.assertTrue(wait_until(lambda: not a.thread.is_alive(), 2.0))
        self.assertTrue(b.thread.is_alive(), "另一个房间不应受影响")
        self.app.manager.stop()

    def test_stats_and_running_aggregate(self):
        self.app = self.make_app()
        FakeEngine().install(self.app)
        self.assertFalse(self.app.running)
        self.app.manager.start_all()
        self.assertFalse(self.app.running, "没有任务时不应为运行中")
        a, _ = self.app.manager.start("111")
        b, _ = self.app.manager.start("222")
        self.assertTrue(self.app.running)
        self.assertTrue(wait_until(lambda: self.app.stats["likes"] >= 20, 3.0))
        st = self.app.stats
        self.assertEqual(st["likes"], a.likes + b.likes)
        self.assertEqual(st["rooms"], 2)
        self.app.manager.stop()
        self.assertTrue(wait_until(lambda: not self.app.running, 2.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
