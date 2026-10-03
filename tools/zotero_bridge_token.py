"""生成/查看/写入 Zotero↔DSH 桥的口令，并同步到两边配置。

    python tools/zotero_bridge_token.py show      # 看当前口令（默认）
    python tools/zotero_bridge_token.py new       # 生成新口令并写入 DSH bundle 配置

为什么需要口令：DSH 插件的 `/zotero-bridge/send` 能把任意文本送进你的对话，
等于"能在 DSH 里说话的入口"。虽然只监听 127.0.0.1，但本机上别的程序
（甚至带本地端口扫描的网页）也可能碰到它，所以加一道口令。
Zotero 插件在请求头 `x-dsh-bridge-token` 里带上它。

⚠ 口令**不能**放在 DSH 的 env 里：DSH 会清洗名称匹配
  `/KEY|PASSWORD|SECRET|TOKEN/i` 以及 `DSH_*` 前缀的环境变量
  （本机在配知识库 MCP 时从 bundle 注释里确认过这一点）。
  所以写在 plugin config 里，由插件自己读。
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                    # …\zotero-kb
PLUGINS_ROOT = os.path.dirname(ROOT)            # …\DSHplugins
# ⚠ bridge 是**独立**的 DSH 插件，放在 DSHplugins\zotero-bridge\，
#   不在 zotero-kb\ 下 —— 曾经按 ROOT 拼路径，结果 FileNotFoundError。
PATCH = os.path.join(PLUGINS_ROOT, "zotero-bridge", "bundle", "cordis.patch.yml")
STATUS_TOKEN = os.path.join(ROOT, "kb", "bridge-token.txt")


def read_token() -> str:
    if not os.path.exists(PATCH):
        return ""
    src = open(PATCH, encoding="utf-8").read()
    m = re.search(r"^\s*token:\s*'?([^'\n]*)'?\s*$", src, re.M)
    return (m.group(1) if m else "").strip()


def write_token(tok: str) -> None:
    src = open(PATCH, encoding="utf-8").read()
    if re.search(r"^\s*token:\s*.*$", src, re.M):
        src = re.sub(r"^(\s*token:\s*).*$", rf"\g<1>'{tok}'", src, count=1, flags=re.M)
    else:
        src = src.rstrip() + f"\n        token: '{tok}'\n"
    open(PATCH, "w", encoding="utf-8").write(src)
    os.makedirs(os.path.dirname(STATUS_TOKEN), exist_ok=True)
    open(STATUS_TOKEN, "w", encoding="utf-8").write(tok)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", nargs="?", default="show",
                    choices=["show", "new"])
    args = ap.parse_args()

    if args.action == "show":
        tok = read_token()
        if not tok:
            print("  [!!] bundle 配置里还没有口令。跑：zotero_bridge_token.py new")
            return 1
        print(f"  当前口令：{tok}")
        print(f"  配置位置：{PATCH}")
        print(f"  副本：    {STATUS_TOKEN}")
        return 0

    tok = secrets.token_urlsafe(24)
    write_token(tok)
    print(f"  [OK] 已生成并写入新口令（{len(tok)} 字符）")
    print(f"       {PATCH}")
    print(f"       副本：{STATUS_TOKEN}")
    print("\n  下一步：")
    print("   1) 让 DSH 重新加载配置（改 cordis.patch.yml 后 patchReload=live 会自动生效；")
    print("      不生效就重启 DSH）")
    print("   2) 验证端点：python tools\\zotero_bridge_check.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
