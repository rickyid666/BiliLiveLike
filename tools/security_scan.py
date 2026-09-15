# -*- coding: utf-8 -*-
"""
发布安全扫描: 阻止凭据/密钥/私人信息进入仓库

用法:
  python tools/security_scan.py            # 扫"会被提交/发布的东西"(已跟踪 + 未忽略的新文件)
  python tools/security_scan.py --all      # 扫整棵工作树(排除构建产物)
  python tools/security_scan.py --staged   # 只扫本次暂存的改动
  python tools/security_scan.py --json     # JSON 输出(给 CI 用)

退出码: 0 = 通过, 1 = 发现疑似敏感内容(应阻止发布)
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------- 禁止出现在仓库里的文件 ----------
BLOCKED_FILES = {
    "cookies.json": "登录凭据(含 SESSDATA / bili_jct)",
    "config_api.json": "本机运行配置(房间号/账号名)",
    "local.properties": "本机路径(Android SDK)",
}
BLOCKED_EXTS = {
    ".jks": "Java 签名密钥", ".keystore": "签名密钥", ".p12": "签名密钥",
    ".pfx": "签名密钥", ".b64": "密钥的 base64", ".pem": "私钥/证书",
    ".key": "私钥", ".p8": "私钥", ".kdbx": "密码库",
}
BLOCKED_NAMES = {"id_rsa", "id_ed25519", "credentials", "token.json", ".env"}
BLOCKED_PREFIXES = ("browser_profile/",)
SKIP_DIRS = {".git", "__pycache__", "node_modules", "build", "dist",
             ".gradle", "browser_profile", ".workbuddy"}
SKIP_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".apk", ".exe",
             ".so", ".dex", ".jar", ".woff", ".woff2", ".ttf"}

# ---------- 内容规则 (写成"很难匹配到自己"的形式, 避免自我误报) ----------
CONTENT_RULES = [
    ("B站 SESSDATA", re.compile(r"SESSDATA\s*=\s*[^;\s'\"]{30,}")),
    ("B站 bili_jct", re.compile(r"bili_jct\s*[=:]\s*[0-9a-fA-F]{32}")),
    ("B站 access_key", re.compile(r"access_key\s*[=:]\s*[0-9a-fA-F]{24,}")),
    ("B站 Cookie 头", re.compile(r"Cookie:\s*[^=\s;]{2,}=[^;\s]{16,};")),
    ("私钥文件内容", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("OpenAI Key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub Token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("疑似硬编码口令",
     re.compile(r"(password|passwd|secret|api_?key|access_?token)\s*[:=]\s*['\"][^'\"\s]{10,}['\"]",
                re.IGNORECASE)),
    ("私人 IPv4", re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
    ("本机用户路径", re.compile(r"(?:[A-Za-z]:\\+Users\\+|/Users/|/home/)(?!(?:runner|user|yourname|xxx|你)\b)[A-Za-z0-9_.\-]{2,}")),
]

# 允许出现这些内容的白名单(文件名 -> 允许的规则)
ALLOW = {
    "tools/security_scan.py": "扫描器自身必然包含规则文本",
    "tests/test_security_scan.py": "测试用例里会故意埋假凭据",
}


def git(*args):
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
        return out.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def is_git_repo():
    return os.path.isdir(os.path.join(ROOT, ".git"))


def collect_files(mode):
    """返回 [(相对路径, 是否来自 git)]"""
    if mode == "all" or not is_git_repo():
        files = []
        for base, dirs, names in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for n in names:
                p = os.path.join(base, n)
                files.append(os.path.relpath(p, ROOT).replace("\\", "/"))
        return files

    if mode == "staged":
        raw = git("diff", "--cached", "--name-only", "-z")
    else:
        raw = git("ls-files", "-z") + git("ls-files", "-o", "--exclude-standard", "-z")
    return sorted({p for p in raw.split("\0") if p.strip()})


def scan_file(path):
    findings = []
    rel = path.replace("\\", "/")
    # 文件名/后缀规则
    name = os.path.basename(rel)
    if name in BLOCKED_FILES:
        findings.append((rel, 0, "敏感文件", f"{BLOCKED_FILES[name]} 不应进仓库"))
    if name in BLOCKED_NAMES or name.startswith(".env"):
        findings.append((rel, 0, "敏感文件", "凭据/密钥类文件不应进仓库"))
    ext = os.path.splitext(name)[1].lower()
    if ext in BLOCKED_EXTS:
        findings.append((rel, 0, "密钥文件", BLOCKED_EXTS[ext]))
    for pref in BLOCKED_PREFIXES:
        if rel.startswith(pref) or ("/" + pref) in rel:
            findings.append((rel, 0, "浏览器数据", "可能含登录 Cookie 的浏览器配置目录"))

    if ext in SKIP_EXTS:
        return findings
    if rel in ALLOW:
        return findings

    full = os.path.join(ROOT, path)
    try:
        if os.path.getsize(full) > 2 * 1024 * 1024:
            return findings
        with open(full, encoding="utf-8", errors="ignore") as f:
            for lineno, line in enumerate(f, 1):
                if len(line) > 4000:
                    continue
                for label, pat in CONTENT_RULES:
                    if pat.search(line):
                        findings.append((rel, lineno, label, line.strip()[:120]))
    except (OSError, UnicodeError):
        pass
    return findings


def check_gitignore():
    """敏感文件必须被 .gitignore 覆盖, 否则迟早被 add -A 卷进去"""
    problems = []
    gi = os.path.join(ROOT, ".gitignore")
    text = ""
    if os.path.exists(gi):
        with open(gi, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    must_ignore = ["cookies.json", "config_api.json", "*.jks", "*.p12", "*.keystore"]
    for pat in must_ignore:
        if pat not in text:
            problems.append(f".gitignore 缺少规则: {pat}")
    return problems


def main():
    ap = argparse.ArgumentParser(description="发布前安全扫描")
    ap.add_argument("--all", action="store_true", help="扫整棵工作树")
    ap.add_argument("--staged", action="store_true", help="只扫暂存区")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    mode = "all" if args.all else ("staged" if args.staged else "tracked")
    files = collect_files(mode)
    findings = []
    for f in files:
        findings.extend(scan_file(f))
    problems = check_gitignore()

    ok = not findings and not problems
    if args.json:
        print(json.dumps({
            "ok": ok, "mode": mode, "scanned": len(files),
            "findings": [{"file": f, "line": l, "rule": r, "detail": d}
                         for f, l, r, d in findings],
            "gitignore": problems,
        }, ensure_ascii=False, indent=2))
    elif os.environ.get("GITHUB_ACTIONS") == "true":
        # 用 GitHub 注解输出: 这些会出现在 check-run annotations 里, 无需任何凭据就能读到
        for f, l, r, d in findings:
            print("::error file=%s,line=%s,title=%s::%s" % (f, l or 1, r, d))
        for p2 in problems:
            print("::error title=.gitignore::%s" % p2)
        if ok:
            print("::notice::security scan passed (%d files)" % len(files))
    else:
        if findings:
            print("=" * 60)
            print(" 发现疑似敏感内容, 已阻止发布")
            print("=" * 60)
            for f, l, r, d in findings:
                loc = f"{f}:{l}" if l else f
                print(f"  [{r}] {loc}")
                print(f"        {d}")
        if problems:
            print("=" * 60)
            print(" .gitignore 检查未通过")
            print("=" * 60)
            for p in problems:
                print("  -", p)
        if ok:
            print(f"[security] 通过 · 已扫描 {len(files)} 个文件 · 无敏感内容")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
