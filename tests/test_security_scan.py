# -*- coding: utf-8 -*-
"""安全扫描器自身的测试: 埋假凭据必须被拦下, 干净仓库必须通过"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCANNER = os.path.join(ROOT, "tools", "security_scan.py")


class SecurityScanTest(unittest.TestCase):

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

    def run_scan(self, tmp, *args):
        p = subprocess.run([sys.executable, os.path.join("tools", "security_scan.py"), *args],
                           cwd=tmp, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def test_clean_repo_passes(self):
        tmp = self.make_repo()
        try:
            with open(os.path.join(tmp, "app.py"), "w", encoding="utf-8") as f:
                f.write("print('hello')\nROOM = 123456\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            code, out = self.run_scan(tmp, "--all")
            self.assertEqual(code, 0, out)
            self.assertIn("通过", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_planted_cookie_is_blocked(self):
        tmp = self.make_repo()
        try:
            with open(os.path.join(tmp, "leak.py"), "w", encoding="utf-8") as f:
                f.write('COOKIE = "SESSDATA=' + "a" * 40 + '; bili_jct=' + "b" * 32 + '"\n')
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            code, out = self.run_scan(tmp, "--all")
            self.assertEqual(code, 1, "埋了 Cookie 必须被拦下")
            self.assertIn("SESSDATA", out)
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
            code, out = self.run_scan(tmp, "--all")
            self.assertEqual(code, 1, out)
            self.assertIn("密钥", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_private_ip_and_user_path_blocked(self):
        tmp = self.make_repo()
        try:
            with open(os.path.join(tmp, "conf.py"), "w", encoding="utf-8") as f:
                f.write("HOST = '192.168.31.44'\nPATH = r'C:\\Users\\Someone\\secret'\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            code, out = self.run_scan(tmp, "--all")
            self.assertEqual(code, 1, out)
            self.assertIn("私人 IPv4", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gitignore_check(self):
        tmp = tempfile.mkdtemp(prefix="bll_scan_")
        try:
            os.makedirs(os.path.join(tmp, "tools"))
            shutil.copy(SCANNER, os.path.join(tmp, "tools", "security_scan.py"))
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            code, out = self.run_scan(tmp, "--all")
            self.assertEqual(code, 1, ".gitignore 缺规则时也要拦下")
            self.assertIn(".gitignore", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
