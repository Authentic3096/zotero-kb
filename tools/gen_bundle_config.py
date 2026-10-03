"""按当前机器的实际路径生成 DSH 接入配置（bundle/cordis.patch.yml）。

    python tools/gen_bundle_config.py           # 生成/更新
    python tools/gen_bundle_config.py --check   # 只检查是否已存在且路径有效

为什么做成"生成"而不是直接提交一份：

    DSH 从 profile 解析这个文件，里面的 `command` / `args` **必须是绝对路径**
    —— 用相对路径它找不到解释器。但绝对路径一旦提交进仓库，就是
    **开发机的路径**，别人克隆下来直接不能用（而且泄露了目录结构）。

    所以：仓库里放 `cordis.patch.yml.tmpl`（带占位符），
    安装时按本机实际情况生成 `cordis.patch.yml`（该文件在 .gitignore 里）。

`install-env.ps1` 会在装完依赖后自动调它，所以普通用户不用管这一步。
"""

from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUNDLE = os.path.join(ROOT, "bundle")
TMPL = os.path.join(BUNDLE, "cordis.patch.yml.tmpl")
OUT = os.path.join(BUNDLE, "cordis.patch.yml")

# 关掉这里就与仓库里的模板一致
PLACEHOLDER_PY = "__PROJECT_PYTHON__"
PLACEHOLDER_SERVER = "__PROJECT_SERVER__"


def find_python() -> str:
    """项目 venv 的解释器（优先 python.exe，不用 pythonw —— DSH 要收 stdio）。"""
    for rel in (r".venv\Scripts\python.exe", r".venv/bin/python"):
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            return p
    return sys.executable or "python"


def render() -> str:
    if not os.path.exists(TMPL):
        raise SystemExit(f"找不到模板：{TMPL}")
    tmpl = open(TMPL, encoding="utf-8").read()
    py = find_python()
    server = os.path.join(ROOT, "online", "server.py")
    out = tmpl.replace(PLACEHOLDER_PY, py.replace("\\", "\\\\"))
    out = out.replace(PLACEHOLDER_SERVER, server.replace("\\", "\\\\"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="只检查已存在的配置是否指向本机真实路径")
    args = ap.parse_args()

    py = find_python()
    server = os.path.join(ROOT, "online", "server.py")

    if args.check:
        if not os.path.exists(OUT):
            print(f"  [XX] {os.path.relpath(OUT, ROOT)} 不存在"
                  f"（跑 python tools\\gen_bundle_config.py 生成）")
            return 1
        txt = open(OUT, encoding="utf-8").read()
        ok_py = py.replace("\\", "\\\\") in txt
        ok_srv = server.replace("\\", "\\\\") in txt
        print(f"  解释器路径正确：{ok_py}  ({py})")
        print(f"  服务端路径正确：{ok_srv}  ({server})")
        if not (ok_py and ok_srv):
            print("  [XX] 配置指向别的路径 —— 重新生成："
                  "python tools\\gen_bundle_config.py")
            return 1
        print("  [OK] 配置有效")
        return 0

    print(f"  项目根：{ROOT}")
    print(f"  解释器：{py}")
    print(f"  服务端：{server}")
    if not os.path.exists(py):
        print(f"  [!!] 解释器不存在 —— 先跑 scripts\\install-env.cmd 建环境")
    if not os.path.exists(server):
        print(f"  [XX] 找不到 {server}")
        return 1
    txt = render()
    if PLACEHOLDER_PY in txt or PLACEHOLDER_SERVER in txt:
        print("  [XX] 替换没生效，模板里的占位符对不上")
        return 1
    old = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
    if old == txt:
        print(f"  [OK] 已是最新：{os.path.relpath(OUT, ROOT)}")
        return 0
    open(OUT, "w", encoding="utf-8", newline="\n").write(txt)
    print(f"  [OK] 已写入 {os.path.relpath(OUT, ROOT)}"
          + ("（内容有更新）" if old else "（首次生成）"))
    print("\n  接入 DSH：")
    print('    dsh plugin --profile desktop add "link:'
          + ROOT.replace("\\", "/") + '/bundle"')
    print("    然后重启 DSH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
