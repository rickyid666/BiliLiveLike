# -*- coding: utf-8 -*-
"""
标准输出编码回归测试

真实故障(CI 上的实际表现):
  Windows runner 是英文区域, 测试进程的 stdout 是管道且编码为 cp1252。
  任务线程里 log() 打印中文 -> UnicodeEncodeError -> 被 run() 的 except 捕获
  -> 在异常处理里又调 log() 再次抛出 -> 线程死亡 -> 任务被标记为 error。
  本地没暴露是因为本地 shell 是 UTF-8/cp936, 中文能写出去。

两层防护, 这里都要测:
  第一层 _fix_stdio_encoding(): 把非 UTF-8 的标准输出改成 UTF-8
  第二层 _safe_print()/_safe_write(): 即便改不了, 也绝不让写日志的失败冒泡
"""
import importlib.util
import io
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_PATH = os.path.join(ROOT, "bili_like_api.py")

try:
    import requests as _probe          # noqa: F401
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False
NEED_DEPS = unittest.skipUnless(HAS_DEPS, "需要 requests（pip install requests）")


def load_core():
    spec = importlib.util.spec_from_file_location(
        "bll_core_stdio_%d" % time.time_ns(), CORE_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class StdioEncodingTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 关掉第一层, 这样下面的用例能单独验证第二层(兜底)
        os.environ["BLL_NO_STDIO_SETUP"] = "1"
        cls.core = load_core()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("BLL_NO_STDIO_SETUP", None)

    def setUp(self):
        self._orig = sys.stdout
        self.buf = io.BytesIO()
        # 模拟 Windows 英文区域的管道: cp1252 + 严格模式
        sys.stdout = io.TextIOWrapper(self.buf, encoding="cp1252", errors="strict")

    def tearDown(self):
        sys.stdout = self._orig

    # ---------- 第二层: 兜底 ----------
    def test_safe_print_survives_non_utf8_stdout(self):
        """cp1252 下打印中文不能抛异常, 且必须仍然写出内容(降级为转义形式)"""
        try:
            self.core._safe_print("中文日志：点赞成功 +15 赞")
        except UnicodeEncodeError as e:
            self.fail("_safe_print 不应该抛异常: %r" % (e,))
        sys.stdout.flush()
        self.assertTrue(self.buf.getvalue(), "降级后仍应写出内容")

    def test_safe_write_handles_closed_stream(self):
        """流已关闭也不能抛异常, 只能返回 False"""
        class Dead:
            encoding = "utf-8"

            def write(self, _):
                raise ValueError("I/O operation on closed file")

            def flush(self):
                raise ValueError("I/O operation on closed file")

        self.assertFalse(self.core._safe_write(Dead(), "x"))

    @NEED_DEPS
    def test_task_log_does_not_kill_task_thread(self):
        """关键回归: 任务线程里打印中文不能让任务变成 error"""
        import tempfile
        core = self.core
        tmp = tempfile.mkdtemp(prefix="bll_stdio_")
        core.CONFIG_PATH = os.path.join(tmp, "config_test.json")
        core.COOKIE_PATH = os.path.join(tmp, "cookies_test.json")

        app = core.BiliLikeApi(prefetch=False)
        app.resolve_room = lambda raw: (
            {"room_id": 1, "anchor_id": 2, "live_status": 0, "short": "1"}, "")
        app.cfg.update({"wait_live": False})

        task, _ = app.manager.start("1")
        self.assertIsNotNone(task)
        task.thread.join(timeout=5)

        self.assertNotEqual(task.state, "error",
                            "任务不该被日志编码问题打成 error（实为: %s）" % task.msg)
        self.assertEqual(task.state, "done")
        self.assertIn("未开播", task.msg)
        sys.stdout.flush()

    # ---------- 第一层: 主动修正 ----------
    def test_fix_stdio_encoding_converts_to_utf8(self):
        """第一层: 非 UTF-8 的管道应被改成 UTF-8, 让中文可读(而不是转义)"""
        os.environ.pop("BLL_NO_STDIO_SETUP", None)
        try:
            self.core._fix_stdio_encoding()
            sys.stdout.write("中文直出\n")
            sys.stdout.flush()
            raw = self.buf.getvalue().decode("utf-8")
            # 换行符受 newline 转换影响(Windows 上会是 \r\n), 只比内容
            self.assertEqual(raw.replace("\r\n", "\n"), "中文直出\n",
                             "改成 UTF-8 后应当能原样写出中文")
        finally:
            os.environ["BLL_NO_STDIO_SETUP"] = "1"

    def test_utf8_stdout_is_left_alone(self):
        """已经是 UTF-8 就不该再动它(不替换流对象、不重复重配置)"""
        os.environ.pop("BLL_NO_STDIO_SETUP", None)
        buf = io.BytesIO()
        wrapped = io.TextIOWrapper(buf, encoding="utf-8", errors="strict")
        sys.stdout = wrapped
        try:
            self.core._fix_stdio_encoding()
            self.assertIs(sys.stdout, wrapped, "不该替换流对象")
            enc = (sys.stdout.encoding or "").lower().replace("-", "")
            self.assertIn(enc, ("utf8", "utf8mb4"))
        finally:
            sys.stdout = self._orig
            os.environ["BLL_NO_STDIO_SETUP"] = "1"


if __name__ == "__main__":
    unittest.main(verbosity=2)
