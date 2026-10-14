#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检查「单节上限 ↔ 模型窗口」这条耦合有没有被单方面改掉。

    python tools/check_llm_window.py        # 退出码 0 = 一致；1 = 耦合被破坏

为什么要有它：分节纲要每次只喂一节正文，正文上限在 `offline/digest.py`
（`SECTION_INPUT_CHARS`），模型窗口在 `offline/judge.py`（`DEFAULT_NUM_CTX`）。
**只改一个不会报任何错** —— 上限加大而窗口没跟，长节会被静默截断，纲要不完整；
窗口调小而正文照旧，模型会吃不下。所以把两者钉在一条检查里。

⚠ **用正则读源码，不 import**：`digest.py` / `judge.py` 一 import 就会连带
读配置、连 sqlite、拉 numpy 那一串依赖，检查脚本不该有这种副作用。

判据（与工作文件夹 `tests/test_digest.py::test_num_ctx_covers_section` 同口径）：
    · DEFAULT_NUM_CTX ≥ SECTION_INPUT_CHARS + 1000
      （1000 是给提示词骨架 ≈700 字 + 回答留的余量）
    · SECTION_INPUT_CHARS ≤ MAX_SECTION_CHARS + 200
      （单节上限比切节上限还大就没意义了）
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIGEST = os.path.join(ROOT, "offline", "digest.py")
JUDGE = os.path.join(ROOT, "offline", "judge.py")

MARGIN = 1000          # 窗口要比正文上限多出来的余量（提示词骨架 + 回答）
SLACK = 200            # 单节上限允许比切节上限大出的一点点


def _read(path: str) -> str:
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def _const(src: str, name: str, path: str) -> int:
    """读一个模块级整数常量（只认行首定义，忽略注释里的同名提及）。"""
    m = re.search(r"(?m)^%s\s*=\s*([0-9]+)" % re.escape(name), src)
    if not m:
        raise SystemExit("  [XX] %s 里找不到模块级常量 %s" % (path, name))
    return int(m.group(1))


def run(quiet: bool = False) -> int:
    d = _read(DIGEST)
    j = _read(JUDGE)
    section = _const(d, "SECTION_INPUT_CHARS", "offline/digest.py")
    max_section = _const(d, "MAX_SECTION_CHARS", "offline/digest.py")
    num_ctx = _const(j, "DEFAULT_NUM_CTX", "offline/judge.py")

    if not quiet:
        print("  单节上限 SECTION_INPUT_CHARS = %d（切节上限 MAX_SECTION_CHARS = %d）"
              % (section, max_section))
        print("  模型窗口 DEFAULT_NUM_CTX   = %d" % num_ctx)

    bad = []
    if num_ctx < section + MARGIN:
        bad.append("模型窗口太小：DEFAULT_NUM_CTX=%d < SECTION_INPUT_CHARS(%d) + %d"
                   % (num_ctx, section, MARGIN))
    if section > max_section + SLACK:
        bad.append("单节上限比切节上限还大：SECTION_INPUT_CHARS=%d > "
                   "MAX_SECTION_CHARS(%d) + %d" % (section, max_section, SLACK))

    if bad:
        print("  [XX] 单节上限与模型窗口的耦合被破坏：")
        for b in bad:
            print("       - %s" % b)
        print("       （改了 offline/digest.py 或 offline/judge.py？两个要一起改）")
        return 1
    if not quiet:
        print("  [OK] 单节上限与模型窗口一致（窗口 ≥ 上限 + %d）" % MARGIN)
    return 0


if __name__ == "__main__":
    sys.exit(run())
