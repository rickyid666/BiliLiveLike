# -*- coding: utf-8 -*-
"""
命令行版测试: 以子进程方式喂命令, 断言输出与退出码 (临时目录, 不碰真实配置)

断言只看"结构化/语言无关"的标记, 不看中文措辞 ——
之前断言 '已加入任务列表' / '任务 2 个' 这类中文子串, 在非 UTF-8 输出环境下
(中文被降级为转义) 会整片失败, 而功能其实完全正常。措辞是会变的, 行为才是契约。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    import requests as _requests_probe  # noqa: F401
    HAS_DEPS = True
except ImportError:                     # 裸 python: 缺依赖时优雅跳过, CI 里一定会装
    HAS_DEPS = False
NEED_DEPS = unittest.skipUnless(HAS_DEPS, "需要 requests（pip install requests）")


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(tmp, commands, timeout=90):
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "bili_like_api.py"],
        cwd=tmp, input=commands, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout)
    return proc


def task_rows(out):
    """从任务列表里抽出房间号。

    列表每行形如:  <房间号>  <状态>  <点赞数>  <请求数>
    只看"首列是数字且列数足够"的行 —— 与界面文案无关, 换语言也不影响。
    """
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].isdigit():
            rows.append(parts[0])
    return rows


@NEED_DEPS
class CliTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bll_cli_")
        shutil.copy(os.path.join(ROOT, "bili_like_api.py"),
                    os.path.join(cls.tmp, "bili_like_api.py"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_help_lists_multi_room_commands(self):
        """帮助里必须能看到多房间命令(命令名是 ASCII, 与语言环境无关)"""
        proc = run_cli(self.tmp, "quit\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for cmd in ("add", "list", "go", "stop", "rm", "status", "quit"):
            self.assertIn(cmd, proc.stdout)

    def test_add_list_rm_flow(self):
        """增 / 查 / 删 的行为: 用列表里的房间号验证, 不依赖中文"""
        p1 = run_cli(self.tmp, "add 111 222\nquit\n")
        self.assertEqual(p1.returncode, 0, p1.stdout + p1.stderr)

        p2 = run_cli(self.tmp, "list\nquit\n")
        self.assertEqual(task_rows(p2.stdout), ["111", "222"],
                         "两个房间都该在任务列表里\n" + p2.stdout)

        p3 = run_cli(self.tmp, "rm 222\nlist\nquit\n")
        self.assertEqual(task_rows(p3.stdout), ["111"],
                         "删掉 222 后列表里只该剩 111\n" + p3.stdout)

        # 清理: 把 111 也删掉, 免得影响后续用例
        run_cli(self.tmp, "rm 111\nquit\n")

    def test_invalid_command_is_handled(self):
        """未知命令不能中断会话(后面还能继续加任务)"""
        proc = run_cli(self.tmp, "what\nadd 333\nlist\nquit\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(task_rows(proc.stdout), ["333"],
                         "未知命令之后会话应当仍然可用\n" + proc.stdout)
        run_cli(self.tmp, "rm 333\nquit\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
