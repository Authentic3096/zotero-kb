"""把知识库迁移到新位置（默认跟着 Zotero 数据目录走）。

    python tools/migrate_kb.py                 # 干跑：只说会做什么
    python tools/migrate_kb.py --apply         # 真迁移（会先备份）
    python tools/migrate_kb.py --apply --to "E:\\my-kb"

迁移之后知识库位置由（优先级从高到低）决定：
    环境变量 KB_DIR  >  kb-location.json  >  Zotero 数据目录\\zotero-kb  >  项目\\kb
（逻辑在 offline/schemas.py 的 _resolve_kb_dir）

⚠ 三个必须注意的点：
  1. **服务必须先停**。index.db 是 WAL 模式，服务在跑时复制会漏掉 -wal 里的
     新数据（本机在读 Zotero 库时踩过同样的坑，见 README 的 WAL 一节）。
  2. **-wal / -shm 必须一起搬**。只搬 index.db 会丢最近写入的经验/权重。
  3. **迁移前先备份**。这里用"复制而不是移动"：迁完两边都有，
     确认没问题了再自己删旧的。多占点磁盘，换一份安心。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

# 这些是"数据"，必须搬
DATA_DIRS = ["papers", "fulltext", "inbox", "logs"]
# 这些可以重建，但搬过去省事（尤其 .cache 里的嵌入模型有 80+MB，重下要时间）
OPTIONAL_DIRS = [".cache", "profile-backup"]
# 必须一起搬的库文件（-wal/-shm 不能漏）
DB_FILES = ["index.db", "index.db-wal", "index.db-shm"]
# 配置/状态文件
CONF_FILES = ["MANIFEST.json", "taxonomy-plan.json", "pending-suggestions.json",
              "service-token.txt", "zotero-api-key.txt", "bridge-token.txt",
              "watch-baseline.json", "plugin-status.json", "llm-config.json"]


def dir_size(path: str) -> tuple[int, int]:
    total = n = 0
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
                n += 1
            except OSError:
                pass
    return total, n


def _configured_kb_dir(S) -> str:
    """读 kb-location.json 里显式写的 kb_dir（没写就返回空）。"""
    try:
        import json as _json
        with open(S.LOCATION_FILE, encoding="utf-8") as fh:
            raw = str((_json.load(fh) or {}).get("kb_dir") or "").strip()
        if not raw:
            return ""
        raw = os.path.expanduser(raw)
        if not os.path.isabs(raw):
            raw = os.path.join(S.KB_ROOT, raw)
        return os.path.abspath(raw)
    except Exception:  # noqa: BLE001
        return ""


def mb(x: int) -> str:
    return f"{x / 1048576:.1f} MB"


def running_server_pids() -> list[int]:
    """找出正在跑的知识库服务进程（迁移前必须停）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR "
             "Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match "
             "'localserver.py' } | Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=30)
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:  # noqa: BLE001
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真迁移（默认只干跑）")
    ap.add_argument("--to", default="", help="目标知识库目录（默认=解析出来的位置）")
    ap.add_argument("--skip-cache", action="store_true",
                    help="不搬 .cache（嵌入模型会在下次构建时重新下载）")
    args = ap.parse_args()

    import schemas as S

    # ⚠ 顺序很关键：S.KB_DIR 是**解析出来**的位置（可能已经指向新目标，
    #   哪怕那边还是空的）。所以不能拿它当"当前位置"，否则会出现
    #   "当前位置 == 目标位置，不需要迁移"这种自欺欺人的结论
    #   （本机第一版就这样，干跑时才发现）。
    #   要按"哪个目录真的有 index.db"来定当前位置。
    resolved = S.KB_DIR
    legacy = os.path.join(S.KB_ROOT, "kb")
    explicit = _configured_kb_dir(S)

    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append(legacy)
    candidates.append(resolved)

    def has_kb(p: str) -> bool:
        return bool(p) and os.path.exists(os.path.join(p, "index.db"))

    old = ""
    for c in candidates:
        if has_kb(c):
            old = os.path.abspath(c)
            break
    if not old:
        print(f"  [XX] 找不到现有的知识库（找过：{candidates}）")
        print("       如果这是全新安装，直接跑一次构建就会在目标位置建库：")
        print(f"         {resolved}")
        return 1

    new = args.to or resolved
    new = os.path.abspath(os.path.expanduser(new))

    print("=" * 70)
    print("知识库迁移")
    print("=" * 70)
    print(f"  当前位置：{old}")
    print(f"  目标位置：{new}")
    print(f"  Zotero 数据目录（探测）：{S.ZOTERO_DATA_DIR}")
    print(f"  kb-location.json：{'已存在' if explicit else '（没有，用自动规则）'}")
    print()

    if os.path.normcase(old) == os.path.normcase(new):
        print("  [--] 位置相同，不需要迁移。")
        print("       如果想让知识库跟随 Zotero：先确认 Zotero 数据目录探测正确，")
        print("       再删掉 kb-location.json（如果有）后重跑。")
        return 0

    if not os.path.isdir(old):
        print(f"  [XX] 当前知识库不存在：{old}")
        return 1
    if not os.path.exists(os.path.join(old, "index.db")):
        print(f"  [XX] {old} 里没有 index.db —— 这不像知识库目录，停下来。")
        return 1

    # ---- 检查服务是否在跑
    pids = running_server_pids()
    if pids:
        print(f"  [!!] 知识库服务正在运行（PID {pids}）")
        print("       迁移前必须停掉它 —— 否则 WAL 里的新数据会丢。")
        print("       停：任务管理器结束那些进程，或跑 scripts\\4-service.vbs 的相反操作")
        print("       （管理面板「分类/标签」页也有服务状态）")
        if not args.apply:
            print("\n  （干跑模式，继续说明会发生什么）")
        else:
            print("\n  [XX] 已中止。请先停服务再重跑。")
            return 1

    # ---- 目标检查
    if os.path.exists(new):
        existing = os.listdir(new)
        if existing:
            print(f"  [!!] 目标目录已存在且非空（{len(existing)} 项）：{new}")
            print("       会用当前知识库**覆盖同名文件**。若那里已有别的知识库，")
            print("       请先换目标或清空它。")
            if args.apply:
                print("       （继续：只覆盖同名文件，不删除目标里的其它文件）")
    total, nfiles = dir_size(old)
    print(f"  源大小：{mb(total)}，{nfiles} 个文件")
    print()

    # ---- 备份
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_root = os.path.join(os.path.dirname(old.rstrip("\\/")),
                               f"_kb-backup-{stamp}")
    print("  计划：")
    print(f"    1) 备份到 {backup_root}")
    print(f"    2) 复制数据到 {new}（含 index.db + -wal + -shm）")
    print("    3) 校验（条目数 / 经验数 / 切片数 一致）")
    print("    4) 提示你如何切回去（不会自动删旧目录）")
    print()

    if not args.apply:
        print("  （这是干跑。确认无误后加 --apply 真执行。）")
        return 0

    # ---- 1) 备份
    print("[1/3] 备份…")
    try:
        os.makedirs(backup_root, exist_ok=True)
        for name in os.listdir(old):
            src = os.path.join(old, name)
            dst = os.path.join(backup_root, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        bsz, bn = dir_size(backup_root)
        print(f"  [OK] 备份完成：{backup_root}（{mb(bsz)}，{bn} 文件）")
    except Exception as exc:  # noqa: BLE001
        print(f"  [XX] 备份失败，中止：{exc}")
        return 1

    # ---- 2) 复制
    print("\n[2/3] 复制到新位置…")
    os.makedirs(new, exist_ok=True)

    def copy_tree(name: str) -> None:
        src = os.path.join(old, name)
        if not os.path.isdir(src):
            return
        shutil.copytree(src, os.path.join(new, name), dirs_exist_ok=True)
        sz, n = dir_size(os.path.join(new, name))
        print(f"  [OK] {name:18} {mb(sz):>10}  {n:5} 文件")

    for d in DATA_DIRS:
        copy_tree(d)
    for d in OPTIONAL_DIRS:
        if d == ".cache" and args.skip_cache:
            print(f"  [--] .cache 跳过（--skip-cache：嵌入模型下次构建会重下）")
            continue
        copy_tree(d)

    for f in DB_FILES + CONF_FILES:
        src = os.path.join(old, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(new, f))
            print(f"  [OK] {f:26} {mb(os.path.getsize(src)):>10}")

    # ---- 3) 校验
    print("\n[3/3] 校验…")
    import sqlite3
    try:
        def counts(path: str) -> dict:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                out = {}
                for label, sql in (
                    ("条目", "SELECT COUNT(*) FROM items"),
                    ("切片", "SELECT COUNT(*) FROM chunks"),
                    ("向量", "SELECT COUNT(*) FROM embeddings"),
                    ("经验", "SELECT COUNT(*) FROM experience"),
                    ("权重", "SELECT COUNT(*) FROM item_weight"),
                ):
                    try:
                        out[label] = c.execute(sql).fetchone()[0]
                    except sqlite3.Error:
                        out[label] = -1
                return out
            finally:
                c.close()

        a = counts(os.path.join(old, "index.db"))
        b = counts(os.path.join(new, "index.db"))
        good = True
        for k in a:
            same = a[k] == b[k]
            good = good and same
            print(f"  {'[OK]' if same else '[XX]'} {k:5} 旧 {a[k]:>6} → 新 {b[k]:>6}"
                  f"{'' if same else '   ← 不一致！'}")
        if not good:
            print("\n  [XX] 数据不一致，**不要**删旧目录。请把上面的数字发给我。")
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  [!!] 校验没跑完：{exc}")
        print("       数据已复制，但请自己核对一下再删旧的。")

    print("\n" + "=" * 70)
    print("迁移完成")
    print("=" * 70)
    print(f"  新位置：{new}")
    print(f"  旧数据仍在：{old}")
    print(f"  备份：{backup_root}")
    print()
    print("  接下来：")
    print("   1) 重启知识库服务（scripts\\4-service.vbs）")
    print("   2) 跑 tools\\gui.py --check 看它认不认新位置")
    print("   3) 跑一次检索确认可用（面板里试搜一下）")
    print("   4) 都正常了，再自己决定要不要删旧目录和备份")
    print()
    print("  想切回旧位置：删掉 kb-location.json（如果有），或设环境变量 KB_DIR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
