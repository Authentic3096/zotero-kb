"""把仓库里残留的"真实研究领域"词换成中性例子（发布前脱敏）。

    <捆绑 python> tools/scrub_domain_words.py [--check]

## 为什么必须有这个

项目要公开，但代码/文档里随手举的例子很容易带上用户真实的研究方向
（测试用的检索词、注释里拿来打比方的真实论文标题……）。
这类东西**单看每一处都不起眼**，合起来就是把人家在做什么讲清楚了。

所以：例子一律换成中性学科（机器学习 / 气候变化 / 目标检测这类），
真要举本机数据时改成占位符。

`--check` 只扫不改，返回非零表示还有残留 —— 可以挂进发布流程。
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 通用检查：与研究方向无关，可以公开。
GENERIC_PATTERNS = [
    r"C:\\Users\\[^<\\]",           # 真实用户目录（占位符 C:\Users\<你> 不算）
]

# ⚠ 领域词**不能写死在这里** —— 词表本身就是泄漏。
#   本文件原先把**具体的领域词**直接列在 PATTERNS 里，而它自己的
#   文档字符串还写着"词表本身成了泄漏"这条教训（那次只修了用户名）。
#   把"这个人在做什么方向"写进公开代码，等于替读者把研究方向讲了。
#   ⚠ 连注释里举例都不行 —— 本文件在扫描时是排除自身的，自检抓不到这里。
#   所以领域词改从下面两个**本地**来源读，任一个即可：
#     · 环境变量 KB_SCRUB_WORDS（分号分隔的正则）
#     · tools/domain-words.local.txt（一行一个正则，# 开头为注释；已 gitignore）
#   两个都没有时，只跑通用检查。
LOCAL_WORDS_FILE = os.path.join(ROOT, "tools", "domain-words.local.txt")


def _domain_patterns() -> list[str]:
    out = [p.strip() for p in
           os.environ.get("KB_SCRUB_WORDS", "").split(";") if p.strip()]
    if os.path.isfile(LOCAL_WORDS_FILE):
        with io.open(LOCAL_WORDS_FILE, encoding="utf-8",
                     errors="replace") as fh:
            out += [ln.strip() for ln in fh
                    if ln.strip() and not ln.strip().startswith("#")]
    return out


PATTERNS = GENERIC_PATTERNS + _domain_patterns()


def _user_patterns() -> list[str]:
    """**运行时**取当前用户名当脱敏词 —— 不写死在代码里。

    ⚠ 踩过的坑：一开始把用户名直接写进 PATTERNS，结果**词表本身成了泄漏**，
    release 审计直接判 FAIL（"全仓库没有本机用户名"）。
    用户名本来就能从环境里拿到，写死毫无必要。
    """
    import getpass                                           # noqa: PLC0415
    out = []
    for name in {os.environ.get("USERNAME", ""), os.environ.get("USER", ""),
                 getpass.getuser(), os.path.basename(os.path.expanduser("~"))}:
        if name and len(name) >= 3 and name.lower() not in ("user", "admin", "public"):
            out.append(re.escape(name))
    return sorted(set(out))


PATTERNS += _user_patterns()

SKIP_DIRS = {".venv", "node_modules", ".git", "__pycache__", "kb",
             "htmlcov", ".cache"}
# 迁移备份目录名带时间戳 —— 按前缀忽略，别把某一次的目录名写进公开代码
SKIP_PREFIXES = ("_kb-backup-",)
EXTS = {".py", ".js", ".md", ".json", ".txt", ".ps1", ".cmd", ".vbs",
        ".xhtml", ".svg", ".yml", ".ftl", ".toml"}

# 白名单：**本文件自己**（它自己就写着"真实用户目录"这类模式串，
# 不排除的话永远报自己），以及系统常量目录（不是某个用户的目录）。
SELF = os.path.abspath(__file__)
ALLOW = [
    r"C:\\Users\\Public",           # 系统常量
    r"C:\\Users\\<[^>]*>",          # 占位符
    r"C:\\Users\\\\<",              # 转义过的占位符
]


def _allowed(line: str) -> bool:
    return any(re.search(a, line) for a in ALLOW)


def scan():
    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                      if d not in SKIP_DIRS
                      and not d.startswith(SKIP_PREFIXES)]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in EXTS:
                continue
            p = os.path.join(dirpath, fn)
            if os.path.abspath(p) in (SELF, os.path.abspath(LOCAL_WORDS_FILE)):
                continue          # 词表文件自己当然含这些词，跳过
            try:
                lines = io.open(p, encoding="utf-8", errors="replace").read().splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                if _allowed(line):
                    continue
                for pat in PATTERNS:
                    if re.search(pat, line):
                        hits.append((os.path.relpath(p, ROOT), i, pat, line.strip()))
                        break
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只扫不改；有残留则退出码 1")
    args = ap.parse_args()

    if not _domain_patterns():
        print("[提示] 没加载到领域词表，本次只检查通用项（真实用户目录）。"
              "要连研究方向一起查，见本文件顶部注释里的两个本地来源。")
        print()
    hits = scan()
    if not hits:
        print("[OK] 没有发现真实研究领域 / 个人路径的残留")
        return 0

    print(f"发现 {len(hits)} 处可疑：")
    for rel, ln, pat, text in hits:
        t = text if len(text) <= 84 else text[:84] + "…"
        print(f"  {rel}:{ln}  [{pat}]")
        print(f"      {t}")
    if args.check:
        return 1
    print()
    print("（本脚本只负责**报告**；具体替换见 tools/scrub_domain_words.py 的历史 "
          "提交或手工改 —— 自动替换容易改坏语义，所以留人工判断。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
