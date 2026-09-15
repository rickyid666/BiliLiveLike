# -*- coding: utf-8 -*-
"""
CI 失败时把关键错误贴成"提交评论" —— 这样无需任何凭据就能读到构建日志。

用法 (workflow, 在 if: failure() 的步骤里):
  env: { GH_TOKEN: ${{ secrets.GITHUB_TOKEN }} }
  run: python tools/report_ci_failure.py

日志来源: $RUNNER_TEMP 下的 check.log / build.log, 或者环境变量 CI_LOG_FILE。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

CANDIDATES = ["check.log", "build.log", "test.log"]


def find_log():
    explicit = os.environ.get("CI_LOG_FILE")
    if explicit and os.path.exists(explicit):
        return explicit
    tmp = os.environ.get("RUNNER_TEMP") or "/tmp"
    for name in CANDIDATES:
        p = os.path.join(tmp, name)
        if os.path.exists(p):
            return p
    return None


def is_frame(line):
    t = line.strip()
    return t.startswith("at ") or t.startswith("... ") or t.startswith('File "')


def extract(lines):
    sel = []
    # 1) 编译错误 / 明文报错行 (+ 后两行上下文)
    for i, ln in enumerate(lines):
        if re.search(r"\berror:\s", ln) or ln.strip().startswith("e: ") or "##[error]" in ln:
            sel.extend(lines[i:i + 3])
    # 2) Python/单元测试的断言与异常概览
    for i, ln in enumerate(lines):
        if re.search(r"^(FAILED|ERROR):|AssertionError|Traceback \(most recent", ln.strip()) \
                or "Test Failed" in ln or "不是 true" in ln:
            sel.extend(lines[i:i + 4])
    # 3) 安全扫描 / Gradle / PyInstaller 的失败概览
    for i, ln in enumerate(lines):
        if ("已阻止发布" in ln or "What went wrong" in ln
                or "Execution failed for task" in ln or "BUILD FAILED" in ln):
            sel.extend(lines[i:i + 30])
    if not sel:
        sel = lines[-60:]
    out, seen = [], set()
    for x in sel:
        if x.strip() and not is_frame(x) and x not in seen:
            seen.add(x)
            out.append(x)
    return "\n".join(out[:70])[:6500]


def main():
    log = find_log()
    if not log:
        print("找不到日志文件, 跳过上报 (可选文件: %s)" % CANDIDATES)
        return 0
    with open(log, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    text = extract(lines) or "(没解析到错误行)"

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    sha = os.environ.get("GITHUB_SHA")
    if not (token and repo and sha):
        print("缺少 token/repo/sha, 只在本地打印:\n" + text)
        return 0

    body = {"body": "CI 失败，关键日志（%s）：\n\n```\n%s\n```" % (os.path.basename(log), text)}
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/commits/%s/comments" % (repo, sha),
        data=json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("comment posted:", r.status)
    except urllib.error.HTTPError as e:
        print("post failed:", e.code, e.read()[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
