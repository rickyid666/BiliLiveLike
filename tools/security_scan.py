# -*- coding: utf-8 -*-
"""
发布安全扫描: 阻止凭据/密钥/私人信息进入仓库

用法:
  python tools/security_scan.py            # 扫"会被提交/发布的东西"(已跟踪 + 未忽略的新文件)
  python tools/security_scan.py --all      # 扫整棵工作树(排除构建产物)
  python tools/security_scan.py --staged   # 只扫本次暂存的改动
  python tools/security_scan.py --json     # JSON 输出

退出码: 0 = 通过, 1 = 发现疑似敏感内容(应阻止发布)

设计约束:
  1. 零第三方依赖 —— 必须能在任何仓库、任何 Python 环境里直接跑(所以只用标准库)
  2. 双输出通道 —— 结构化 JSON 给机器判定, 中文摘要给人看; 两者都保留
  3. 规则用稳定的 ASCII ID 做标识, 中文只作为展示文案 —— 下游断言不该 depends on 措辞
  4. 编码安全 —— stdout 被重定向为 cp1252/cp936 时打印中文不能把扫描器打成崩溃
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------- 禁止出现在仓库里的文件 ----------
# 值: (规则ID, 中文说明)
BLOCKED_FILES = {
    "cookies.json": ("FILE_COOKIE_JAR", "登录凭据(含 SESSDATA / bili_jct)"),
    "cookies.js": ("FILE_COOKIE_DATA", "生成的 Cookie 数据文件(含真实凭据)"),
    "config_api.json": ("FILE_LOCAL_CONFIG", "本机运行配置(房间号/账号名)"),
    "local.properties": ("FILE_LOCAL_PATH", "本机路径(Android SDK)"),
}
BLOCKED_EXTS = {
    ".jks": ("FILE_KEYSTORE", "Java 签名密钥"),
    ".keystore": ("FILE_KEYSTORE", "签名密钥"),
    ".p12": ("FILE_KEYSTORE", "签名密钥"),
    ".pfx": ("FILE_KEYSTORE", "签名密钥"),
    ".b64": ("FILE_KEYSTORE", "密钥的 base64 文本"),
    ".pem": ("FILE_PRIVATE_KEY", "私钥/证书"),
    ".key": ("FILE_PRIVATE_KEY", "私钥"),
    ".p8": ("FILE_PRIVATE_KEY", "私钥"),
    ".kdbx": ("FILE_PASSWORD_DB", "密码库"),
}
BLOCKED_NAMES = {"id_rsa", "id_ed25519", "credentials", "token.json", ".env"}
BLOCKED_PREFIXES = ("browser_profile/",)
SKIP_DIRS = {".git", "__pycache__", "node_modules", "build", "dist",
             ".gradle", "browser_profile", ".workbuddy"}
SKIP_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".apk", ".exe",
             ".so", ".dex", ".jar", ".woff", ".woff2", ".ttf"}

# ---------- 内容规则 ----------
# 写成"很难匹配到自己"的形式, 避免扫描器自我误报
CONTENT_RULES = [
    ("BILI_SESSDATA", re.compile(r"SESSDATA\s*=\s*[^;\s'\"]{30,}")),
    ("BILI_JCT", re.compile(r"bili_jct\s*[=:]\s*[0-9a-fA-F]{32}")),
    ("BILI_ACCESS_KEY", re.compile(r"access_key\s*[=:]\s*[0-9a-fA-F]{24,}")),
    ("BILI_COOKIE_HEADER", re.compile(r"Cookie:\s*[^=\s;]{2,}=[^;\s]{16,};")),
    ("PRIVATE_KEY_BLOCK", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("OPENAI_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GITHUB_TOKEN", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("HARDCODED_SECRET",
     re.compile(r"(password|passwd|secret|api_?key|access_?token)\s*[:=]\s*['\"]([^'\"\s]{10,})['\"]",
                re.IGNORECASE)),
    ("PRIVATE_IPV4", re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
    ("LOCAL_USER_PATH",
     re.compile(r"(?:[A-Za-z]:\\+Users\\+|/Users/|/home/)"
                r"(?!(?:runner|user|yourname|xxx|你))[A-Za-z0-9_.\-]{2,}")),
]

# 只有"泛化规则"需要占位符白名单; 格式锚定的高精度规则(SESSDATA/AKIA/ghp_/私钥块)
# 一旦命中就是真的, 不允许被白名单放过。
GENERIC_RULES = {"HARDCODED_SECRET"}

# 占位符特征: 这些不该被当成真凭据, 否则误报多了使用者就会无视告警
PLACEHOLDER_RE = re.compile(
    r"(your|example|placeholder|sample|dummy|fake|demo|todo|fixme|changeme|"
    r"redacted|replace|insert|foobar|未填写|请填写)", re.IGNORECASE)


def is_placeholder(value):
    """判断捕获到的"疑似凭据"是不是占位符"""
    v = (value or "").strip().strip("'\"")
    if len(v) < 10:
        return True
    if len(set(v)) <= 4:                     # 全同一个字符, 或极少字符重复
        return True
    if re.fullmatch(r"[\*xX\-.]+", v):       # **** / xxxx / ....
        return True
    if any(t in v for t in ("<", ">", "${", "{{", "%s", "***")):
        return True
    low = v.lower()
    if low in ("changeme", "redacted", "none", "null", "undefined", "todo"):
        return True
    return bool(PLACEHOLDER_RE.search(v))

# 规则 ID -> 中文展示名 (只用于人看的输出)
LABELS = {
    "FILE_COOKIE_JAR": "凭据文件",
    "FILE_COOKIE_DATA": "凭据数据文件",
    "FILE_LOCAL_CONFIG": "本机配置",
    "FILE_LOCAL_PATH": "本机路径文件",
    "FILE_KEYSTORE": "签名密钥文件",
    "FILE_PRIVATE_KEY": "私钥文件",
    "FILE_PASSWORD_DB": "密码库",
    "FILE_CREDENTIAL": "凭据/密钥类文件",
    "DIR_BROWSER_PROFILE": "浏览器配置目录",
    "BILI_SESSDATA": "B站 SESSDATA",
    "BILI_JCT": "B站 bili_jct",
    "BILI_ACCESS_KEY": "B站 access_key",
    "BILI_COOKIE_HEADER": "B站 Cookie 头",
    "PRIVATE_KEY_BLOCK": "私钥文件内容",
    "AWS_ACCESS_KEY": "AWS Access Key",
    "OPENAI_KEY": "OpenAI Key",
    "GITHUB_TOKEN": "GitHub Token",
    "SLACK_TOKEN": "Slack Token",
    "HARDCODED_SECRET": "疑似硬编码口令",
    "PRIVATE_IPV4": "私人 IPv4",
    "LOCAL_USER_PATH": "本机用户路径",
}

# 允许出现这些内容的白名单(相对路径 -> 原因)
ALLOW = {
    "tools/security_scan.py": "扫描器自身必然包含规则文本",
    "tests/test_security_scan.py": "测试用例里会故意埋假凭据",
}


# ---------------------------------------------------------------------------
# 输出: 编码安全 + 双通道
# ---------------------------------------------------------------------------

def _safe_print(text=""):
    """编码安全的 print: 绝不因写日志失败而崩掉检查本身。

    故障背景: Windows 上 stdout 被重定向成管道/文件时编码可能是 cp1252/cp936,
    print(中文) 会抛 UnicodeEncodeError。扫描器崩掉最坏的结果不是"报错", 而是
    被误读成"检查没通过"甚至被误读成"没发现问题" —— 所以这里必须兜住。
    """
    text = str(text)
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        return
    except UnicodeEncodeError:
        pass
    except Exception:
        return
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    for candidate in (text.encode(enc, "backslashreplace").decode(enc, "replace"),
                      text.encode("ascii", "backslashreplace").decode("ascii")):
        try:
            sys.stdout.write(candidate + "\n")
            sys.stdout.flush()
            return
        except Exception:
            continue


def _fix_stdio_encoding():
    """把非 UTF-8 的 stdout/stderr 改成 UTF-8 (管道/重定向同样有效)"""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if enc in ("utf8", "utf8mb4"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------

def git(*args):
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
        return out.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def is_git_repo():
    return os.path.isdir(os.path.join(ROOT, ".git"))


def collect_files(mode):
    """返回会被提交/发布的相对路径列表"""
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
    """返回 [(规则ID, 文件, 行号, 命中说明)]"""
    findings = []
    rel = path.replace("\\", "/")
    name = os.path.basename(rel)

    # 文件名 / 后缀规则
    if name in BLOCKED_FILES:
        rid, why = BLOCKED_FILES[name]
        findings.append((rid, rel, 0, "%s 不应进仓库" % why))
    if name in BLOCKED_NAMES or name.startswith(".env"):
        findings.append(("FILE_CREDENTIAL", rel, 0, "凭据/密钥类文件不应进仓库"))
    ext = os.path.splitext(name)[1].lower()
    if ext in BLOCKED_EXTS:
        rid, why = BLOCKED_EXTS[ext]
        findings.append((rid, rel, 0, why))
    for pref in BLOCKED_PREFIXES:
        if rel.startswith(pref) or ("/" + pref) in rel:
            findings.append(("DIR_BROWSER_PROFILE", rel, 0,
                             "可能含登录 Cookie 的浏览器配置目录"))

    if ext in SKIP_EXTS or rel in ALLOW:
        return findings

    # 内容规则
    full = os.path.join(ROOT, path)
    try:
        if os.path.getsize(full) > 2 * 1024 * 1024:
            return findings
        with open(full, encoding="utf-8", errors="ignore") as f:
            for lineno, line in enumerate(f, 1):
                if len(line) > 4000:
                    continue
                for rid, pat in CONTENT_RULES:
                    m = pat.search(line)
                    if not m:
                        continue
                    if rid in GENERIC_RULES:
                        # 泛化规则必须过滤占位符: 'API_KEY = "YOUR_API_KEY_HERE"' 不该报警
                        # 注意 lastindex 指向最后匹配的捕获组(即凭据值本身)
                        value = m.group(m.lastindex) if m.lastindex else m.group(0)
                        if is_placeholder(value):
                            continue
                    findings.append((rid, rel, lineno, line.strip()[:120]))
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
            problems.append(".gitignore 缺少规则: %s" % pat)
    return problems


def human_report(findings, problems, files, ok):
    """人类可读摘要 —— CI 里也输出, 保证日志可排查"""
    if findings:
        _safe_print("=" * 60)
        _safe_print(" 发现疑似敏感内容, 已阻止发布")
        _safe_print("=" * 60)
        for rid, f, l, d in findings:
            loc = "%s:%s" % (f, l) if l else f
            _safe_print("  [%s] %s  %s" % (rid, loc, LABELS.get(rid, "")))
            _safe_print("        %s" % d)
    if problems:
        _safe_print("=" * 60)
        _safe_print(" .gitignore 检查未通过")
        _safe_print("=" * 60)
        for p in problems:
            _safe_print("  - " + p)
    if ok:
        _safe_print("[security] 通过 · 已扫描 %d 个文件 · 无敏感内容" % len(files))


def build_result(mode, files, findings, problems):
    """结构化 JSON 结果 (机器判定请用这个, 不要解析中文输出)"""
    ok = not findings and not problems
    return {
        "schema_version": "1.0",
        "status": "PASS" if ok else "FAIL",
        "severity": "NONE" if ok else "HIGH",
        "category": "SECRET",
        "code": "OK" if ok else ("SECRET_DETECTED" if findings else "GITIGNORE_GAP"),
        "message": ("未发现敏感内容" if ok else
                    "发现 %d 处疑似敏感内容" % len(findings) if findings else
                    ".gitignore 缺少覆盖规则"),
        "ok": ok,
        "mode": mode,
        "scanned": len(files),
        "findings": [{"rule": rid, "label": LABELS.get(rid, ""),
                      "file": f, "line": l, "detail": d}
                     for rid, f, l, d in findings],
        "gitignore": problems,
    }


def main():
    ap = argparse.ArgumentParser(description="发布前安全扫描")
    ap.add_argument("--all", action="store_true", help="扫整棵工作树")
    ap.add_argument("--staged", action="store_true", help="只扫暂存区")
    ap.add_argument("--json", action="store_true",
                    help="JSON 输出 (机器可读; 下游判定请用这个字段, 别解析中文摘要)")
    args = ap.parse_args()

    _fix_stdio_encoding()

    mode = "all" if args.all else ("staged" if args.staged else "tracked")
    files = collect_files(mode)
    findings = []
    for f in files:
        findings.extend(scan_file(f))
    problems = check_gitignore()
    result = build_result(mode, files, findings, problems)

    if args.json:
        _safe_print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # GitHub 注解: 出现在 check-run annotations 里, 无需任何凭据就能读到
            for rid, f, l, d in findings:
                _safe_print("::error file=%s,line=%s,title=%s::%s" % (f, l or 1, rid, d))
            for p in problems:
                _safe_print("::error title=.gitignore::%s" % p)
            if result["ok"]:
                _safe_print("::notice::security scan passed (%d files)" % len(files))
        # 人类摘要始终输出: 只留英文注解会让日志没法读
        human_report(findings, problems, files, result["ok"])

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:                      # noqa: BLE001
        # 扫描器自身出错必须是非 0, 绝不能"静默通过"
        _safe_print("[security] 扫描器内部错误: %r" % (exc,))
        sys.exit(3)
