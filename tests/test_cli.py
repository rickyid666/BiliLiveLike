# -*- coding: utf-8 -*-
"""命令行版测试: 以子进程方式喂命令, 断言输出与退出码 (临时目录, 不碰真实配置)"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(tmp, commands, timeout=90):
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "bili_like_api.py"],
        cwd=tmp, input=commands, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout)
    return proc


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
        proc = run_cli(self.tmp, "quit\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for cmd in ("add ", "list", "go ", "stop ", "rm "):
            self.assertIn(cmd.strip(), proc.stdout)

    def test_add_list_rm_flow(self):
        proc = run_cli(self.tmp, "add 111 222\nlist\nrm 222\nlist\nquit\n")
        out = proc.stdout
        self.assertEqual(proc.returncode, 0, out + proc.stderr)
        self.assertEqual(out.count("已加入任务列表"), 2)
        self.assertIn("已移除 222", out)
        self.assertIn("任务 2 个", out)     # 第一次 list
        self.assertIn("任务 1 个", out)     # rm 之后

    def test_invalid_command_is_handled(self):
        proc = run_cli(self.tmp, "what\nquit\n")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("未知命令", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
