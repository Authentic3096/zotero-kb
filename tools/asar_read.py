"""按需读取 Electron app.asar 里的文件（不整体解包）。

    python tools/asar_read.py <app.asar> list [子串]      # 列文件名
    python tools/asar_read.py <app.asar> grep <正则> [--max N]   # 在文件内容里搜
    python tools/asar_read.py <app.asar> cat <内部路径> [--max N]  # 打印文件内容

为什么用它：DSH 的包都打包在 `resources\\app.asar`（本机 121MB）里，
拿不到 `.d.ts`，而 `npm pack` 只能拿到公开的客户端包、拿不到宿主服务定义。
本机为了确定 `sessionController.prompt()` 的入参字段名，绕了好几轮，
最后靠直接读 asar 解决 —— 所以把这个能力固化下来。

asar 格式（很简单，不需要额外依赖）：
    [8 字节 Pickle 头][JSON 目录][文件内容区]
Pickle 头里：uint32 size(=(headerSize+4) 对齐)、uint32 headerSize、
             uint32 jsonSize、uint32 jsonSize2，然后是 JSON。
JSON 目录是个树：{ files: { name: { size, offset, files? } } }
offset 是相对"文件内容区"起点的偏移。
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys

TEXT_EXT = (".js", ".mjs", ".cjs", ".ts", ".d.ts", ".json", ".md", ".yml", ".yaml",
            ".txt", ".mts", ".cts", ".map")


def read_header(asar: str):
    with open(asar, "rb") as fh:
        head = fh.read(16)
        if len(head) < 16:
            raise ValueError("文件太小，不是 asar")
        # Pickle: 第 1 个 uint32 是 payload 大小，第 2 个是 header 字符串长度
        _, header_size, json_size, _ = struct.unpack("<IIII", head)
        payload = fh.read(header_size)
        raw = payload[:json_size]
        text = raw.decode("utf-8", errors="replace")
        # header_size 是按 4 字节对齐的，JSON 后面可能有填充
        try:
            tree = json.loads(text)
        except json.JSONDecodeError:
            tree = json.loads(text[: text.rstrip("\x00").rfind("}") + 1])
        data_start = 8 + header_size
    return tree, data_start


def walk(tree, prefix=""):
    """展开成 {内部路径: (size, offset)}。"""
    out = {}
    files = tree.get("files") or {}
    for name, node in files.items():
        p = f"{prefix}/{name}" if prefix else name
        if isinstance(node, dict) and "files" in node:
            out.update(walk(node, p))
        elif isinstance(node, dict) and "size" in node:
            out[p] = (node.get("size", 0), node.get("offset", 0),
                      bool(node.get("unpacked")))
    return out


def read_file(asar: str, data_start: int, entry) -> bytes:
    size, offset, unpacked = entry
    if unpacked:
        # 解包在外面的目录里（app.asar.unpacked）
        raise ValueError("该文件是 unpacked，需去 app.asar.unpacked 读")
    with open(asar, "rb") as fh:
        fh.seek(data_start + int(offset))
        return fh.read(int(size))


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    asar, cmd = sys.argv[1], sys.argv[2]
    tree, data_start = read_header(asar)
    index = walk(tree)
    print(f"asar 内共 {len(index)} 个文件", file=sys.stderr)

    if cmd == "list":
        pat = sys.argv[3] if len(sys.argv) > 3 else ""
        hits = [p for p in sorted(index) if pat.lower() in p.lower()]
        for p in hits[:300]:
            print(f"  {index[p][0]:>10}  {p}")
        print(f"（匹配 {len(hits)} 个）", file=sys.stderr)
        return 0

    if cmd == "cat":
        p = sys.argv[3].lstrip("/")
        if p not in index:
            cands = [k for k in index if k.endswith(p)]
            if not cands:
                print(f"找不到 {p}", file=sys.stderr)
                return 1
            p = cands[0]
        limit = 20000
        if "--max" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--max") + 1])
        body = read_file(asar, data_start, index[p]).decode("utf-8", errors="replace")
        print(body[:limit])
        if len(body) > limit:
            print(f"\n…（截断，共 {len(body)} 字符）", file=sys.stderr)
        return 0

    if cmd == "grep":
        pat = re.compile(sys.argv[3])
        limit = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 40
        only_text = "--all" not in sys.argv
        shown = 0
        scanned = 0
        for p in sorted(index):
            if only_text and not p.endswith(TEXT_EXT):
                continue
            size = index[p][0]
            if size > 6_000_000:          # 跳过超大文件（压缩包/源码映射）
                continue
            try:
                body = read_file(asar, data_start, index[p]).decode("utf-8", "replace")
            except Exception:              # noqa: BLE001
                continue
            scanned += 1
            for i, line in enumerate(body.splitlines(), 1):
                if pat.search(line):
                    print(f"{p}:{i}: {line.strip()[:200]}")
                    shown += 1
                    if shown >= limit:
                        print(f"\n（已显示 {shown} 条，扫描 {scanned} 个文件）", file=sys.stderr)
                        return 0
        print(f"\n（共 {shown} 条，扫描 {scanned} 个文件）", file=sys.stderr)
        return 0

    print(f"未知命令 {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
