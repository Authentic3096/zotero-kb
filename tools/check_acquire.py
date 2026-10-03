"""从 DSH 导入文献这条链路的自检 —— 一段一段验，不写用户的库。

    python tools/check_acquire.py                # 只查"链路通不通"（不发网络、不写库）
    python tools/check_acquire.py --live         # 加一段干跑：真抓一个 DOI 的元数据，**不落库**

为什么要这么一个工具：这条链路的每一环都在**别的进程**里 ——
本地服务（Python）→ 任务队列（HTTP）→ Zotero 插件（进程内 JS）→ Zotero 的
翻译器 / 附件 resolver（又会联网）。任何一环断掉，用户看到的都是同一句
"抓取失败"，根本分不清该修哪儿。所以这里按顺序把每一环单独探一遍，
**先失败的那一环就是问题所在**。

⚠ 本工具**永远不写库**：`--live` 走的是 `dry_run`，插件侧到"解析出元数据"
就返回（见 bootstrap.js 的 runAcquire 里 dryRun 分支）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))
sys.path.insert(0, os.path.join(ROOT, "online"))

import schemas as S          # noqa: E402
import acquire as AQ         # noqa: E402

OK, WARN, BAD, INFO = "  [OK]  ", "  [!!]  ", "  [XX]  ", "        "

# 公开的开放获取 DOI，只用来验"抓得到元数据"这件事本身。
# 它不会真的被写进库（--live 走 dry_run）。
PROBE_DOI = "10.21105/joss.02171"


def main() -> int:
    ap = argparse.ArgumentParser(description="从 DSH 导入文献链路自检")
    ap.add_argument("--live", action="store_true",
                    help="真抓一次元数据（dry_run，不写库）")
    ap.add_argument("--doi", default=PROBE_DOI, help="--live 用哪个 DOI")
    args = ap.parse_args()

    print("=" * 70)
    print("从 DSH 导入文献 · 链路自检")
    print("=" * 70)

    # ---- 1) 知识库位置
    print("\n[1/6] 知识库位置")
    print(INFO + "KB_DIR = " + S.KB_DIR)
    print(INFO + "ZOTERO_DATA_DIR = " + S.ZOTERO_DATA_DIR)
    if not os.path.isdir(S.KB_DIR):
        print(BAD + "知识库目录不存在 —— 先跑 scripts\\1-convert.cmd")
        return 1
    print(OK + "目录存在")

    # ---- 2) 本地服务
    print("\n[2/6] 本地服务（127.0.0.1:8765）")
    if not AQ.service_alive():
        print(BAD + "服务没在跑 —— 抓取任务派不出去")
        print(INFO + "启动：双击 scripts\\4-service.vbs，或在管理面板点「启动服务」")
        return 1
    print(OK + "服务在线")

    # ---- 3) 插件版本 + 轮询
    print("\n[3/6] Zotero 插件")
    st_path = AQ.PLUGIN_STATUS_PATH
    if not os.path.isfile(st_path):
        print(BAD + f"插件从没写过状态文件：{st_path}")
        print(INFO + "说明 Zotero 没开，或插件没启用")
        return 1
    import json
    try:
        st = json.load(open(st_path, encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(BAD + f"状态文件读不出来：{exc}")
        return 1
    age = time.time() - os.path.getmtime(st_path)
    ver = str(st.get("pluginVersion") or "?")
    print(INFO + f"插件版本 {ver}｜Zotero {st.get('zoteroVersion')}｜"
                 f"状态文件 {age:.0f} 秒前写的")
    print(INFO + f"alive={st.get('alive')} taskPolling={st.get('taskPolling')} "
                 f"tickCount={st.get('tickCount')} 权重缓存={st.get('weightsCached')}")

    need = ".".join(str(x) for x in AQ.MIN_PLUGIN_VERSION)
    if AQ._parse_version(ver) < AQ.MIN_PLUGIN_VERSION:
        print(BAD + f"插件版本太旧：要 {need} 以上才有「从 DSH 导入文献」")
        print(INFO + "插件是侧载的 —— **完全退出 Zotero 再重新打开**即可，不用重装")
        print(INFO + "重启后在 Zotero 里跑 工具 → 开发者 → 运行 JavaScript：")
        print(INFO + "    return Zotero.ZoteroKB.version")
        return 1
    if age > 120:
        print(WARN + f"状态文件 {age:.0f} 秒没更新了（插件每 ~30 秒写一次）"
                     "—— 任务轮询可能停了")
    ok, why = AQ._plugin_online()
    if not ok:
        print(BAD + "插件不可用：" + why)
        return 1
    print(OK + f"插件可用（≥ {need}），任务轮询活着")

    # ---- 4) DOI 归一化
    print("\n[4/6] DOI 归一化")
    samples = ["10.1234/abc", "https://doi.org/10.1234/abc",
               "doi:10.1234/abc.", "  10.1234/abc  ", "这不是 DOI"]
    allok = True
    for s in samples:
        got = AQ.normalize_doi(s)
        print(INFO + f"{s!r:38} -> {got!r}")
    if AQ.normalize_doi("10.1234/abc") != "10.1234/abc":
        allok = False
    if AQ.normalize_doi("这不是 DOI"):
        allok = False
    print((OK if allok else BAD) + ("归一化正常" if allok else "归一化有问题"))
    if not allok:
        return 1

    # ---- 5) 参数校验（不发网络）
    print("\n[5/6] 参数校验")
    try:
        AQ.acquire([])
        print(BAD + "空清单居然没报错")
        return 1
    except AQ.AcquireError as exc:
        print(OK + "空清单被挡住：" + str(exc)[:60])
    try:
        AQ.acquire([f"10.1234/x{i}" for i in range(AQ.MAX_ITEMS + 1)])
        print(BAD + "超量清单居然没报错")
        return 1
    except AQ.AcquireError as exc:
        print(OK + f"超过 {AQ.MAX_ITEMS} 篇被挡住：" + str(exc)[:48])

    # ---- 6) 干跑（唯一会联网的一步，仍然不写库）
    print("\n[6/6] 干跑：让 Zotero 真抓一个 DOI 的元数据（dry_run，不写库）")
    if not args.live:
        print(INFO + "跳过（加 --live 才跑）")
        print("\n" + "=" * 70)
        print("链路自检通过（未做联网验证）。")
        return 0

    print(INFO + f"DOI = {args.doi}")
    print(INFO + "这一步要 Zotero 去联网解析，通常 3~15 秒…")
    try:
        r = AQ.acquire([args.doi], dry_run=True,
                       progress=lambda t: print(INFO + t))
    except AQ.AcquireError as exc:
        print(BAD + str(exc))
        for h in exc.how_to_fix:
            print(INFO + "· " + h)
        return 1

    print("")
    print(AQ.format_report(r))
    if r.get("added"):
        print(OK + f"Zotero 能抓到元数据（{r['added']} 篇）。真实入库请用 "
                   "kb_acquire（不给 dry_run）。")
        return 0
    print(WARN + "这个 DOI 没解析出元数据 —— 换一个再试，"
                 "或者查回报里的 failed 原因")
    return 2


if __name__ == "__main__":
    sys.exit(main())
