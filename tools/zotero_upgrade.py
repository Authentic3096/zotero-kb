"""Zotero 升级前检查与备份（不改任何东西，只读+备份）。

    python tools/zotero_upgrade.py check      # 版本 / 插件兼容性 / 数据量 / 备份建议
    python tools/zotero_upgrade.py backup     # 备份数据目录（含 storage 与 sqlite）
    python tools/zotero_upgrade.py verify     # 升级后核对（版本、库可读、条目数、写入能力）

为什么单独做这个：升级 Zotero 是**不可逆**的操作（数据库 schema 会升级，
旧版打不开新库）。而本机历史上装过 6 个第三方插件，升级后有可能被禁用或报错。
所以升级前必须能一眼看到：哪些插件已声明支持 10.x、数据备份在哪、出问题怎么退。
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import urllib.request
import zipfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import schemas as S  # noqa: E402
import settings as _ST  # noqa: E402（.env 覆盖层，本工具自己的设置）

ZOTERO_DIR = os.path.dirname(S.ZOTERO_DB)          # …\<Zotero 数据目录>\Zotero
PROFILE_DIR = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "Zotero",
                           "Zotero", "Profiles")
BACKUP_ROOT = (_ST.get("ZOTERO_BACKUP_ROOT")
               or os.path.join(os.path.expanduser("~"), "ZoteroBackup"))
# 备份目录：`ZOTERO_BACKUP_ROOT`（环境变量或 .env）> 用户目录下的 ZoteroBackup。
# 原来写死 `D:\ZoteroBackup`（旧机器）—— 换机器后那个盘符可能根本不存在。

OK, WARN, BAD = "  [OK]  ", "  [!!]  ", "  [XX]  "


ZOTERO_EXE = S.resolve_zotero()   # 自动探测（ZOTERO_EXE 环境变量/.env → 常见位置 → 注册表）；找不到是 ""


def _ver_tuple(text: str) -> tuple:
    """把版本串转成可比较的元组。'10.*' → (10,)，'9.0.6' → (9,0,6)。"""
    nums = re.findall(r"\d+", text or "")
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


def local_version() -> str:
    """读本机 Zotero 版本。

    数据来源优先级（都实测过）：
      1. `zotero.exe` 的 ProductVersion —— 最可靠。安装器每次覆盖安装都会更新它。
      2. 注册表 Uninstall 项 —— 可能残留旧版本条目（本机就有 7.0.30 和 9.0.6 两条）。
      3. 应用目录里的 install.log —— 那是**首次安装**时的记录，不会随升级更新
         （本机写的是 7.0.30，早就过时了）。
      4. platform.ini —— 那是 Mozilla 平台的构建信息，不是 Zotero 版本，**不能用**。
    """
    if os.path.exists(ZOTERO_EXE):
        try:
            import subprocess
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Item '{ZOTERO_EXE}').VersionInfo.ProductVersion"],
                capture_output=True, text=True, timeout=20)
            v = (out.stdout or "").strip()
            if re.match(r"^\d+\.", v):
                return v
        except Exception:  # noqa: BLE001
            pass
    try:
        import subprocess
        out = subprocess.run(
            ["reg", "query",
             r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Zotero",
             "/v", "DisplayVersion"],
            capture_output=True, text=True, timeout=10)
        m = re.search(r"DisplayVersion\s+REG_SZ\s+([\d.]+)", out.stdout)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return "(未知)"


def latest_version() -> str:
    """查官网最新版本。

    试三条路：Zotero 自己的更新清单（Mozilla 格式）→ 下载页 HTML。
    都拿不到就返回空串，**不当作问题** —— 查不到版本不代表不能升级，
    用户直接去 https://www.zotero.org/download/ 看即可。
    """
    for url in ("https://www.zotero.org/download/client/update/6.0/update_win32.json",
                "https://www.zotero.org/download/client/update/5.0/update_win32.json"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("version"):
                return str(data["version"])
        except Exception:  # noqa: BLE001
            continue
    try:
        req = urllib.request.Request("https://www.zotero.org/download/",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        m = re.search(r"[Zz]otero\s+(\d+\.\d+(?:\.\d+)?)", html)
        if m:
            return m.group(1)
        m = re.search(r'data-version="(\d+\.\d+(?:\.\d+)?)"', html)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return ""


def plugin_report() -> list[dict]:
    """逐个检查已装插件声明的兼容版本范围。

    插件的 manifest 里 `strict_max_version` 决定它能装到哪些 Zotero 版本上。
    升级到 10 之后，声明 `10.*` 的还能用，声明 `9.*` 的会被禁用。
    """
    ext_dir = None
    if os.path.isdir(PROFILE_DIR):
        for prof in os.listdir(PROFILE_DIR):
            cand = os.path.join(PROFILE_DIR, prof, "extensions")
            if os.path.isdir(cand):
                ext_dir = cand
                break
    out: list[dict] = []
    if not ext_dir:
        return out
    for xpi in glob.glob(os.path.join(ext_dir, "*.xpi")):
        info = {"file": os.path.basename(xpi), "min": "?", "max": "?", "name": "?"}
        try:
            with zipfile.ZipFile(xpi) as z:
                raw = z.read("manifest.json").decode("utf-8", errors="replace")
            data = json.loads(raw)
            app = (data.get("applications") or data.get("browser_specific_settings")
                   or {}).get("zotero") or {}
            info["min"] = app.get("strict_min_version", "?")
            info["max"] = app.get("strict_max_version", "?")
            info["name"] = data.get("name", "?")
        except Exception as exc:  # noqa: BLE001
            info["error"] = f"{type(exc).__name__}: {exc}"
        out.append(info)
    return out


def data_stats() -> dict:
    """数据目录规模（用于估算备份时间/空间）。"""
    total = 0
    files = 0
    for base, _dirs, names in os.walk(os.path.dirname(ZOTERO_DIR)):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(base, n))
                files += 1
            except OSError:
                pass
    return {"bytes": total, "files": files}


def db_stats() -> dict:
    """直接读 Zotero 库（只读快照），拿到条目规模与顶层条目数。"""
    tmp = os.path.join(S.CACHE_DIR, "upgrade-check.sqlite")
    os.makedirs(S.CACHE_DIR, exist_ok=True)
    shutil.copy2(S.ZOTERO_DB, tmp)
    conn = sqlite3.connect(tmp)
    out = {}
    try:
        out["items"] = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        out["collections"] = conn.execute(
            "SELECT COUNT(*) FROM collections").fetchone()[0]
        out["attachments"] = conn.execute(
            "SELECT COUNT(*) FROM itemAttachments").fetchone()[0]
        out["annotations"] = conn.execute(
            "SELECT COUNT(*) FROM itemAnnotations").fetchone()[0]
        out["notes"] = conn.execute("SELECT COUNT(*) FROM itemNotes").fetchone()[0]
        # 顶层条目（= 知识库里的"文献"数）。**必须排除回收站** ——
        # 知识库建索引时也不收回收站里的条目（zreader 里显式过滤 deletedItems），
        # 这里不排的话两边天生差几篇，会误报"索引过期"。
        # （本机实测：不排回收站是 102，排除后 100，与索引一致。）
        out["top_level"] = conn.execute(
            """
            SELECT COUNT(*) FROM items i
            WHERE i.itemID NOT IN (SELECT itemID FROM itemAttachments)
              AND i.itemID NOT IN (SELECT itemID FROM itemNotes)
              AND i.itemID NOT IN (SELECT itemID FROM itemAnnotations)
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
            """
        ).fetchone()[0]
        out["trash"] = conn.execute(
            "SELECT COUNT(*) FROM deletedItems").fetchone()[0]
        # Zotero 的 version 表是 (schema, version) 两列 —— 不是单列 value，
        # 早先按 `SELECT value FROM version` 取会直接报 no such column。
        try:
            row = conn.execute(
                "SELECT version FROM version WHERE schema = 'userdata'").fetchone()
            out["schema"] = row[0] if row else "?"
        except sqlite3.Error:
            out["schema"] = "?"
    finally:
        conn.close()
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return out


def kb_stats() -> dict:
    """知识库索引侧的规模（用来和 Zotero 侧对照）。"""
    out: dict = {}
    if not os.path.exists(S.INDEX_DB):
        return out
    # searcher 在 online/ 下，而本脚本只把 offline/ 放进了 path
    online = os.path.join(ROOT, "online")
    if online not in sys.path:
        sys.path.insert(0, online)
    from searcher import Searcher

    s = Searcher()
    try:
        out["items"] = s.stats()["items"]
        out["built_at"] = S.get_meta(s.conn, "built_at", "")
    finally:
        s.close()
    return out


def cmd_check() -> int:
    print("=" * 68)
    print("Zotero 升级前检查")
    print("=" * 68)
    problems: list[str] = []

    print("\n[1] 版本")
    cur = local_version()
    new = latest_version()
    print(f"  本机：{cur}")
    if new:
        print(f"  官网：{new}")
        cur_t, new_t = _ver_tuple(cur), _ver_tuple(new)
        if cur_t and new_t and new_t > cur_t:
            print(f"{WARN}可以升级到 {new}")
        elif cur_t and new_t:
            print(f"{OK}已是最新")
    else:
        print("  官网：查不到（不影响；手动看 https://www.zotero.org/download/）")
        print("        已知：Zotero 10.0 于 2026-08-17 发布，10.0.5 于 2026-09-30；"
              "本地 API 写入支持从 10.0 起")
    target = new or "10"
    tgt_major = _ver_tuple(target)[:1]

    print(f"\n[2] 已装插件（看 strict_max_version 是否覆盖 {target}）")
    plugins = plugin_report()
    if not plugins:
        print(f"{WARN}没找到插件目录")
    for p in plugins:
        mx_raw = str(p.get("max", "?"))
        mx = _ver_tuple(mx_raw)
        # 兼容判断要**比较版本**，不能看前缀：
        # 本机有个插件声明的是 `11.*`（比目标还高），按"是否以 10 开头"判会误报成不兼容。
        supported = (not mx) or mx >= tgt_major or mx_raw.strip() in ("*", "*.*")
        flag = OK if supported else WARN
        note = "" if supported else "  ← 可能被禁用"
        print(f"{flag}{p['file'][:44]:44} {p.get('min','?'):>12} ~ {mx_raw:<9}"
              f"{p.get('name','')[:20]}{note}")
        if not supported:
            problems.append(f"插件 {p['file']} 只声明支持到 {mx_raw}，"
                            f"升级到 {target} 后可能被禁用（去插件主页看新版）")
    print("\n  说明：升级后 Zotero 会把超范围的插件自动禁用，**不会损坏数据**；"
          "\n        拿到新版 xpi 重新装即可。声明 11.* 这类更高的范围是兼容的。")

    print("\n[3] 数据规模")
    ds = data_stats()
    print(f"  数据目录：{os.path.dirname(ZOTERO_DIR)}")
    print(f"  共 {ds['files']} 个文件，{ds['bytes'] / 1024 / 1024:.0f} MB")
    try:
        dbs = db_stats()
        print(f"  条目 {dbs['items']}（顶层文献 {dbs['top_level']}）｜"
              f"分类 {dbs['collections']}｜附件 {dbs['attachments']}｜"
              f"标注 {dbs['annotations']}｜笔记 {dbs['notes']}｜"
              f"回收站 {dbs['trash']}")
        try:
            print(f"  schema userdata v{dbs['schema']}")
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}读库失败：{exc}")
        problems.append("Zotero 库读不了，升级前先确认库是好的")
        dbs = {}

    # 与知识库索引对照：两边顶层条目数该一致，不一致说明索引过期
    try:
        kbs = kb_stats()
        if kbs and dbs:
            print(f"\n  知识库索引：{kbs['items']} 篇（建于 {kbs['built_at'][:19]}）")
            delta = dbs["top_level"] - kbs["items"]
            if delta == 0:
                print(f"{OK}与 Zotero 顶层条目数一致")
            elif delta > 0:
                print(f"{WARN}Zotero 比索引多 {delta} 篇 —— 索引过期，"
                      f"升级后跑一次增量")
                problems.append(f"索引比 Zotero 少 {delta} 篇，跑一次 1-convert.cmd")
            else:
                print(f"{WARN}索引比 Zotero 多 {-delta} 篇 —— 可能有条目被删/合并，"
                      f"跑一次 --full 重建")
                problems.append(f"索引比 Zotero 多 {-delta} 篇，建议 --full 重建")
    except Exception as exc:  # noqa: BLE001
        print(f"{WARN}索引侧统计失败：{type(exc).__name__}: {exc}")

    print("\n[4] 备份现状")
    if os.path.isdir(BACKUP_ROOT):
        snaps = sorted(os.listdir(BACKUP_ROOT))
        print(f"{OK}备份目录 {BACKUP_ROOT}（{len(snaps)} 份）")
        for s in snaps[-3:]:
            print(f"      {s}")
    else:
        print(f"{WARN}还没有备份。升级前先跑：python tools/zotero_upgrade.py backup")
        problems.append(f"没有备份（建议备份到 {BACKUP_ROOT}）")

    print("\n" + "=" * 68)
    if problems:
        print(f"建议先处理 {len(problems)} 项：")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
    else:
        print("检查通过，可以升级。")
    print("\n升级方式（二选一）：")
    print("  A. Zotero 里：帮助 → 检查更新（最省事，能看到进度）")
    print("  B. 官网下载 10.x 安装包，覆盖安装到当前 Zotero 目录"
          + (f"（{os.path.dirname(ZOTERO_EXE)}）" if ZOTERO_EXE else ""))
    print("     官方说明：可以直接覆盖安装新版本，不会丢数据。")
    print("  升级完跑一次：python tools/zotero_upgrade.py verify")
    print("=" * 68)
    return 1 if problems else 0


def cmd_backup() -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_ROOT, f"Zotero-backup-{stamp}")
    src = os.path.dirname(ZOTERO_DIR)
    print(f"备份 {src}")
    print(f"  → {dst}")
    os.makedirs(dst, exist_ok=True)
    total = 0
    for base, dirs, names in os.walk(src):
        rel = os.path.relpath(base, src)
        target = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target, exist_ok=True)
        for n in names:
            s = os.path.join(base, n)
            d = os.path.join(target, n)
            try:
                shutil.copy2(s, d)
                total += 1
            except OSError as exc:
                print(f"  [!!] 跳过 {n}：{exc}")
    size = sum(os.path.getsize(os.path.join(b, f))
               for b, _d, fs in os.walk(dst) for f in fs
               if os.path.exists(os.path.join(b, f)))
    print(f"{OK}完成：{total} 个文件，{size / 1024 / 1024:.0f} MB")
    print(f"  回退方式：把 {dst} 里的内容拷回 {src}（升级后如要回退，"
          f"需先卸载 Zotero 10、装回 9.x —— 旧版打不开新版升级过的库）")
    # 备份记录
    rec = os.path.join(dst, "_backup-info.json")
    with open(rec, "w", encoding="utf-8") as fh:
        json.dump({
            "created": stamp,
            "zotero_version": local_version(),
            "db": db_stats(),
            "source": src,
        }, fh, ensure_ascii=False, indent=2)
    return 0


def probe_write_api() -> dict:
    """探测 Zotero 本地 API 的写入能力（需要 Zotero 正在运行）。

    读：`GET /api/users/0/items?limit=1` 无需授权。
    写：Zotero 10+ 才支持，且要先 `POST /api/local/authorize` 拿一次性 key。
    这里只做**无副作用的探测**：看根路径的响应头有没有 Zotero-Server-ID
    （10+ 才带），以及写端点是否返回 401/403（说明存在但需授权）。
    """
    base = "http://127.0.0.1:23119/api"
    out: dict = {"reachable": False}
    try:
        req = urllib.request.Request(f"{base}/", method="GET")
        with urllib.request.urlopen(req, timeout=6) as resp:
            out["reachable"] = True
            out["status"] = resp.status
            out["server_id"] = resp.headers.get("Zotero-Server-ID", "")
            out["api_version"] = resp.headers.get("Zotero-API-Version", "")
            out["schema_version"] = resp.headers.get("Zotero-Schema-Version", "")
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    # 写探测：故意不带 key，看响应码
    try:
        body = json.dumps({"appName": "zotero-kb probe"}).encode()
        req = urllib.request.Request(
            f"{base}/local/authorize", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            out["write_probe"] = f"HTTP {resp.status}（意外：居然直接通过了）"
    except urllib.error.HTTPError as exc:
        out["write_probe"] = f"HTTP {exc.code}"
        if exc.code == 428:
            out["write_supported"] = True     # 需要 Zotero-Server-ID 头
        elif exc.code in (401, 403):
            out["write_supported"] = True     # 存在但需授权
        elif exc.code == 404:
            out["write_supported"] = False    # 没有这个端点 = 9.x
    except Exception as exc:  # noqa: BLE001
        out["write_probe"] = f"{type(exc).__name__}: {exc}"
    return out


def cmd_verify() -> int:
    print("=" * 68)
    print("Zotero 升级后核对")
    print("=" * 68)
    print(f"\n[1] 版本：{local_version()}")
    print("\n[2] 库可读性")
    try:
        dbs = db_stats()
        print(f"{OK}条目 {dbs['items']}｜分类 {dbs['collections']}｜"
              f"附件 {dbs['attachments']}｜标注 {dbs['annotations']}｜"
              f"笔记 {dbs['notes']}｜schema v{dbs['schema']}")
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}读库失败：{exc}")
        return 1

    print("\n[3] 本地 API 与写入能力")
    probe = probe_write_api()
    if not probe.get("reachable"):
        print(f"{WARN}本地 API 没响应：{probe.get('error', '')}")
        print("       Zotero 没开，或没勾选「设置 → 高级 → 允许本机上的"
              "其他应用程序与 Zotero 通信」。")
    else:
        print(f"{OK}本地 API 可用（HTTP {probe.get('status')}）")
        if probe.get("server_id"):
            print(f"{OK}Zotero-Server-ID: {probe['server_id']}（10+ 才有）")
        if probe.get("schema_version"):
            print(f"     schema 版本：{probe['schema_version']}")
        wp = probe.get("write_probe", "?")
        if probe.get("write_supported"):
            print(f"{OK}写入端点存在（探测结果 {wp}）——可以回填标签/分类")
        else:
            print(f"{WARN}写入端点不可用（探测结果 {wp}）——仍是只读，"
                  f"需要 Zotero 10+")

    print("\n[4] 知识库是否还认得这个库")
    print("     跑一次：scripts\\1-convert.cmd（增量），再看 kb_stats 的条目数是否一致")
    print("=" * 68)
    return 0


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    if action == "check":
        return cmd_check()
    if action == "backup":
        return cmd_backup()
    if action == "verify":
        return cmd_verify()
    print("用法：zotero_upgrade.py [check|backup|verify]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
