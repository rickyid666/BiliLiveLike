# -*- coding: utf-8 -*-
"""
安全扫描器自身的测试

两条设计原则在这里落地:
  1. 断言只看结构化 JSON 的字段(规则 ID), 不看中文措辞 ——
     之前断言 '通过'/'密钥' 这类中文子串, 结果 CI 换成注解输出就全挂。
     下游依赖自然语言的措辞是脆弱设计。
  2. 扫描器在非 UTF-8 环境下(Windows 英文区域/管道重定向)不能崩,
     因为"检查没跑成"和"检查通过"在崩溃时长得一模一样。
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCANNER_REL = os.path.join("tools", "security_scan.py")
SCANNER = os.path.join(ROOT, SCANNER_REL)


class SecurityScanTest(unittest.TestCase):

    # ---------- 脚手架 ----------
    def make_repo(self):
        tmp = tempfile.mkdtemp(prefix="bll_scan_")
        os.makedirs(os.path.join(tmp, "tools"))
        shutil.copy(SCANNER, os.path.join(tmp, "tools", "security_scan.py"))
        with open(os.path.join(tmp, ".gitignore"), "w", encoding="utf-8") as f:
            f.write("cookies.json\nconfig_api.json\n*.jks\n*.p12\n*.keystore\n")
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp, check=True)
        return tmp

    def run_scan(self, tmp, *args, env_extra=None, json_mode=True):
        """跑扫描器: 返回 (退出码, 解析后的 JSON, 原始输出)

        json_mode=False 时走人类可读/CI 注解通道 (用于验证双通道都还在)
        """
        env = dict(os.environ)
        env.pop("GITHUB_ACTIONS", None)        # 默认走本地人类输出模式
        if env_extra:
            env.update(env_extra)
        argv = [sys.executable, SCANNER_REL] + (["--json"] if json_mode else []) + list(args)
        p = subprocess.run(argv, cwd=tmp, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        out = (p.stdout or "") + (p.stderr or "")
        try:
            data = json.loads(p.stdout or "{}")
        except ValueError:
            data = {}
        return p.returncode, data, out

    def rules(self, data):
        return {f.get("rule") for f in data.get("findings", [])}

    # ---------- 应该被拦下 ----------
    def test_planted_cookie_is_blocked(self):
        tmp = self.make_repo()
        try:
            # 假凭据用拼接构造, 避免扫描器把本测试文件自己当成泄露
            with open(os.path.join(tmp, "leak.py"), "w", encoding="utf-8") as f:
                f.write('COOKIE = "SESSDATA=' + "a" * 40 + '; bili_jct=' + "b" * 32 + '"\n')
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            code, data, out = self.run_scan(tmp, "--all")
            self.assertEqual(code, 1, "埋了 Cookie 必须被拦下\n" + out)
            self.assertEqual(data.get("status"), "FAIL")
            self.assertIn("BILI_SESSDATA", self.rules(data))
            self.assertIn("BILI_JCT", self.rules(data))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_keystore_and_keyfile_blocked(self):
        tmp = self.make_repo()
        try:
            with open(os.path.join(tmp, "release.p12"), "wb") as f:
                f.write(b"\x00\x01binary-keystore")
            with open(os.path.join(tmp, "id_rsa"), "w", encoding="utf-8") as f:
                f.write("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n")
            subprocess.run(["git", "add", "-A", "-f"], cwd=tmp, check=True)
            code, data, out = self.run_scan(tmp, "--all")
            self.assertEqual(code, 1, out)
            self.assertIn("FILE_KEYSTORE", self.rules(data))
            self.assertIn("FILE_CREDENTIAL", self.rules(data))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_private_ip_and_user_path_blocked(self):
        tmp = self.make_repo()
        try:
            with open(os.path.join(tmp, "conf.py"), "w", encoding="utf-8") as f:
                f.write("HOST = '192.168.31.44'\nPATH = r'C:\\Users\\Someone\\secret'\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            code, data, out = self.run_scan(tmp, "--all")
            self.assertEqual(code, 1, out)
            got = self.rules(data)
            self.assertIn("PRIVATE_IPV4", got)
            self.assertIn("LOCAL_USER_PATH", got)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------- 不应该误报 ----------
    def test_clean_repo_passes(self):
        tmp = self.make_repo()
        try:
            with open(os.path.join(tmp, "app.py"), "w", encoding="utf-8") as f:
                f.write("print('hello')\nROOM = 123456\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            code, data, out = self.run_scan(tmp, "--all")
            self.assertEqual(code, 0, out)
            self.assertEqual(data.get("status"), "PASS")
            self.assertEqual(data.get("findings"), [])

            # 双通道: 走人类/CI 注解通道时, 中文摘要必须还在(否则日志不可排查)
            code2, _, out2 = self.run_scan(tmp, "--all", json_mode=False,
                                           env_extra={"GITHUB_ACTIONS": "true"})
            self.assertEqual(code2, 0)
            self.assertIn("::notice::", out2, "CI 里应有注解通道")
            self.assertIn("已扫描", out2, "CI 里也要保留人类可读摘要")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_placeholders_do_not_trigger(self):
        """占位符不能误报 —— 误报多了使用者就会无视告警"""
        tmp = self.make_repo()
        try:
            with open(os.path.join(tmp, "example.py"), "w", encoding="utf-8") as f:
                f.write("API_KEY = 'YOUR_API_KEY_HERE'\n"
                        "TOKEN = 'YOUR_TOKEN_HERE'\n"
                        "PASSWORD = 'changeme'\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            code, data, out = self.run_scan(tmp, "--all")
            self.assertEqual(code, 0, "占位符不应被当成真凭据\n" + out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------- .gitignore 覆盖 ----------
    def test_gitignore_check(self):
        tmp = tempfile.mkdtemp(prefix="bll_scan_")
        try:
            os.makedirs(os.path.join(tmp, "tools"))
            shutil.copy(SCANNER, os.path.join(tmp, "tools", "security_scan.py"))
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            code, data, out = self.run_scan(tmp, "--all")
            self.assertEqual(code, 1, ".gitignore 缺规则时也要拦下\n" + out)
            self.assertEqual(data.get("code"), "GITIGNORE_GAP")
            self.assertTrue(data.get("gitignore"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------- 编码安全 ----------
    def test_no_crash_under_non_utf8_stdout(self):
        """cp1252 环境下扫描器不能崩 —— 崩了会被误读成"通过了"或"没通过" """
        tmp = self.make_repo()
        try:
            with open(os.path.join(tmp, "leak.py"), "w", encoding="utf-8") as f:
                f.write('COOKIE = "SESSDATA=' + "a" * 40 + '"\n')
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            code, _, out = self.run_scan(
                tmp, "--all", env_extra={"PYTHONIOENCODING": "cp1252"})
            self.assertEqual(code, 1, out)
            self.assertNotIn("Traceback", out, "cp1252 下不该崩")
            self.assertNotIn("UnicodeEncodeError", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_safe_print_falls_back_when_reconfigure_impossible(self):
        """即便标准输出改不成 UTF-8, 也必须降级写出而不是抛异常"""
        spec = importlib.util.spec_from_file_location("bll_scanner_probe", SCANNER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)          # 导入不改 stdout, 只定义函数

        orig, buf = sys.stdout, io.BytesIO()
        sys.stdout = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
        try:
            mod._safe_print("中文降级输出 ✓")
            sys.stdout.flush()
            self.assertTrue(buf.getvalue(), "降级后仍应有输出")
        except UnicodeEncodeError as e:
            self.fail("_safe_print 不应抛异常: %r" % (e,))
        finally:
            sys.stdout = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
