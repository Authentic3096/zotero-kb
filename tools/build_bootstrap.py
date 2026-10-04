"""把 zotero-plugin/src/*.js 拼成 zotero-plugin/bootstrap.js（xpi 里装的是它）。

    python tools/build_bootstrap.py            # 生成
    python tools/build_bootstrap.py --check    # 只校验是否同步（CI 用）

## 为什么要有这一步

插件是 bootstrapped extension，**Zotero 只加载 xpi 根目录下的 `bootstrap.js`
一个文件**（见 skills/zotero-plugin-dev/SKILL.md 的打包那节）。所以源码分成
`src/` 多个文件、运行时仍是一个文件，中间必须有这一步拼接。

为什么不改成运行时 `loadSubScript` 多文件加载（真正的模块化）：
  ① xpi 里 `manifest.json` / `bootstrap.js` 必须在**根目录**，往包里加子目录
     是新的未知（本项目在"装不上且报错没有线索"上吃过很多次亏）；
  ② 沙箱里 `loadSubScript` 的目标 scope 没有实测过，属于在最容易黑箱失败的
     地方引入新失败模式。
拼接法让**运行时与拆分前完全一样**，代价只是多一条"改源码要重新生成"的规矩
—— 而这条规矩由 `--check`（进 CI）和 pack_plugin 的打包前校验强制。

## 规矩

· 源文件顺序只认下面 `SRC_ORDER`，**不依赖文件系统排序**；`src/` 里出现清单
  外的 `.js`、或清单里的文件不存在，都直接报错退出（防止"新文件忘了进清单"）。
· 生成物写成 UTF-8 **无 BOM** + LF（仓库 .gitattributes 要求 LF；BOM 会
  让 Zotero 那边的报错完全没有线索，见 AGENTS.md 的编码那条）。
· 每个源文件前加一行 `// ===== src/<名> =====` 横幅：出栈、grep 时能一眼看出
  这段来自哪个源文件。
"""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "zotero-plugin")
SRC = os.path.join(PLUGIN, "src")
OUT = os.path.join(PLUGIN, "bootstrap.js")

# 拼接顺序 = 阅读顺序。改这个列表就能改顺序，但别改文件名（它们要能 grep 到）。
SRC_ORDER = [
    "00-core",
    "01-lifecycle",
    "02-paths",
    "03-processes",
    "04-prefs",
    "05-server",
    "06-watch",
    "07-classify",
    "08-metafill",
    "09-metabatch",
    "10-acquire",
    "11-notify",
    "12-dsh",
    "13-kbopen",
    "14-menus",
    "15-prefpane",
    "16-weightcol",
    "17-windowhook",
    "18-taskpoll",
    "99-bootstrap",
]

HEADER = [
    "// ⚠ 本文件由 tools/build_bootstrap.py 从 zotero-plugin/src/*.js 生成，",
    "//    不要直接改这里 —— 改 src/ 下的源文件，再跑：",
    "//        python tools/build_bootstrap.py",
    "//    改完没重新生成的话，打包前会被拒绝（tools/pack_plugin.py 会校验）。",
    "//    源码共 " + str(len(SRC_ORDER)) + " 个文件，清单在 build_bootstrap.py 的 SRC_ORDER。",
    "",
]

ASSIGN_OPEN = "Object.assign(ZoteroKB, {"


class SyncError(RuntimeError):
    pass


def read_src(name: str) -> str:
    path = os.path.join(SRC, name + ".js")
    if not os.path.exists(path):
        raise SyncError(f"src/{name}.js 不存在（清单里有它）")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.startswith("\ufeff"):
        raise SyncError(f"src/{name}.js 带 UTF-8 BOM —— 去掉再跑")
    if "\r" in text:
        raise SyncError(f"src/{name}.js 里有 CR（行尾要是 LF）")
    return text


def check_src_list() -> list[str]:
    """src/ 里不能有清单外的 .js，清单里也不能有缺失的。"""
    if not os.path.isdir(SRC):
        raise SyncError(f"没有 {SRC} 目录")
    have = sorted(n[:-3] for n in os.listdir(SRC) if n.endswith(".js"))
    extra = [n for n in have if n not in SRC_ORDER]
    missing = [n for n in SRC_ORDER if n not in have]
    if extra:
        raise SyncError(f"src/ 里有清单外的文件：{extra}（要加进 SRC_ORDER）")
    if missing:
        raise SyncError(f"SRC_ORDER 里的这些文件不存在：{missing}")
    return have


def compose() -> str:
    check_src_list()
    parts = list(HEADER)
    for name in SRC_ORDER:
        text = read_src(name).rstrip("\n")
        parts.append(f"// ===== src/{name}.js =====")
        parts.extend(text.split("\n"))
        parts.append("")
    text = "\n".join(parts).rstrip("\n") + "\n"
    # 生成物自己再自检几条硬要求
    if "function startup(" not in text:
        raise SyncError("生成物里没有顶层 function startup( —— 99-bootstrap 丢了？")
    # 除了 00-core（自己开对象字面量）和 99-bootstrap（只有顶层函数），
    # 每个**有成员**的模块都必须用 Object.assign 包住，否则它的成员不会
    # 挂到 ZoteroKB 上 —— 那是最隐蔽的一种"改完没生效"。
    for name in SRC_ORDER:
        if name in ("00-core", "99-bootstrap"):
            continue
        src = read_src(name)
        has_member = any(re.match(r"^  [A-Za-z_$][\w$]*:\s*(?:async\s+)?function",
                                 ln) for ln in src.split("\n"))
        if has_member and ASSIGN_OPEN not in src:
            raise SyncError(f"src/{name}.js 里有成员，但没有 "
                            f"`{ASSIGN_OPEN}` 包住 —— 这些成员不会生效")
    if text.count("});") < 1:
        raise SyncError("生成物里一个 Object.assign 的收尾都没有")
    return text


def main() -> int:
    print("=" * 66)
    print("拼装 zotero-plugin/bootstrap.js")
    print("=" * 66)
    try:
        text = compose()
    except SyncError as exc:
        print(f"  [XX] {exc}")
        return 1

    old = None
    if os.path.exists(OUT):
        with open(OUT, "rb") as fh:
            raw = fh.read()
        if raw[:3] == b"\xef\xbb\xbf":
            print("  [XX] 现有 bootstrap.js 带 BOM，先去掉")
            return 1
        old = raw.decode("utf-8")

    check_only = "--check" in sys.argv
    if check_only:
        if old == text:
            print(f"  [OK] bootstrap.js 与 src/*.js 同步"
                  f"（{len(SRC_ORDER)} 个源文件 / {text.count(chr(10))} 行）")
            return 0
        print("  [XX] bootstrap.js 与 src/*.js **不同步** —— 跑 "
              "`python tools/build_bootstrap.py` 重新生成")
        old_lines = (old or "").split("\n")
        new_lines = text.split("\n")
        for i in range(max(len(old_lines), len(new_lines))):
            a = old_lines[i] if i < len(old_lines) else "<没有这一行>"
            b = new_lines[i] if i < len(new_lines) else "<没有这一行>"
            if a != b:
                name = current_source(new_lines, i)
                print(f"       第一个差异在第 {i + 1} 行"
                      f"（新生成的内容来自 src/{name}.js）：")
                print(f"         文件里： {a[:96]}")
                print(f"         应该是： {b[:96]}")
                break
        return 1

    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    raw = open(OUT, "rb").read()
    assert raw[:3] != b"\xef\xbb\xbf", "写出 BOM 了"
    assert b"\r" not in raw, "写出了 CR"
    print(f"  [OK] {OUT}")
    print(f"       {len(SRC_ORDER)} 个源文件 → {len(text.splitlines())} 行 / "
          f"{len(raw):,} 字节")
    for name in SRC_ORDER:
        n = read_src(name).count("\n")
        print(f"       src/{name}.js{'':<4} {n:>5} 行")
    return 0


def current_source(new_lines: list[str], idx: int) -> str:
    """第 idx 行（0 基）属于哪个源文件 —— 靠往上找最近的横幅。"""
    for i in range(idx, -1, -1):
        m = re.match(r"// ===== src/(\S+)\.js =====", new_lines[i])
        if m:
            return m.group(1)
    return "（文件头）"


if __name__ == "__main__":
    sys.exit(main())
