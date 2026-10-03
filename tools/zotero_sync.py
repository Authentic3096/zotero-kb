"""通过 Zotero 10 的本地 API 写回分类与标签。

    python tools/zotero_sync.py check        # 只读：探测可达性与授权需求
    python tools/zotero_sync.py authorize    # 走一次授权（Zotero 里点「允许」），把 key 存下来
    python tools/zotero_sync.py plan         # 干跑：按 taxonomy-plan.json 算出要做什么，不写
    python tools/zotero_sync.py apply        # 真正执行（需要已授权）
    python tools/zotero_sync.py verify       # 执行后核对

为什么要单独做这个而不是让插件全包：
  · **批量**重整分类（100 篇、6 个分类）用脚本更方便，能干跑、能核对、能回退；
  · 插件负责**单篇**的即时建议（新文献保存时），两者互补。
  两条路都写 Zotero，但走的是不同通道：这里是 Zotero 自己的 Web API 兼容层
  （走它的事务与同步队列），比直接改 sqlite 安全得多。

Zotero 10 的写入要先授权拿 key：
  POST /api/local/authorize  {appName}  →  200 {"key": "..."}
  之后带上 `Authorization: Bearer <key>` 与 `Zotero-API-Version: 3` 写。
  这个端点只在 Zotero 10+ 存在；9.x 返回 404。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import schemas as S  # noqa: E402

BASE = "http://127.0.0.1:23119/api"
PLAN_PATH = os.path.join(S.KB_DIR, "taxonomy-plan.json")
KEY_PATH = os.path.join(S.KB_DIR, "zotero-api-key.txt")
LOG_PATH = os.path.join(S.KB_DIR, "logs", "zotero-sync.log")

OK, WARN, BAD = "  [OK]  ", "  [!!]  ", "  [XX]  "


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class ZoteroAPI:
    """Zotero 本地 API 客户端（只用到读 + 写分类/条目）。"""

    def __init__(self, base: str = BASE, timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.key = self._load_key()
        self.server_id = ""
        self.user = "0"

    # ---------------------------------------------------------- 底层

    def _load_key(self) -> str:
        if os.path.exists(KEY_PATH):
            return open(KEY_PATH, encoding="utf-8").read().strip()
        return ""

    def _save_key(self, key: str) -> None:
        os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
        with open(KEY_PATH, "w", encoding="utf-8") as fh:
            fh.write(key)
        os.chmod(KEY_PATH, 0o600)
        self.key = key

    def request(self, method: str, path: str, body=None, auth: bool = True,
                extra_headers: dict | None = None, expect_json: bool = True):
        url = f"{self.base}{path}"
        data = None
        # Zotero 10 收紧了本地 HTTP 服务的校验（官方 "Zotero 10 for Developers"）：
        #   · Host 必须是 localhost / 127.0.0.1 / [::1]，否则 400；
        #   · User-Agent 以 Mozilla/ 开头、或带 Origin 的请求会被**直接丢弃**
        #     （不返回响应），除非带 Zotero-Allowed-Request 头。
        #     这条以前只对 CORS-simple 类型生效，所以"以前能用的 JSON POST"
        #     在 10 上可能被拒 —— 就是这里踩的。
        #   · 写操作必须带 Zotero-Server-ID。
        headers = {
            "Zotero-API-Version": "3",
            "User-Agent": "zotero-kb/1.0 (local tool)",
            "Zotero-Allowed-Request": "1",
            "Host": "127.0.0.1:23119",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth and self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        if self.server_id:
            headers["Zotero-Server-ID"] = self.server_id
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                self.server_id = resp.headers.get("Zotero-Server-ID", self.server_id)
                if not expect_json:
                    return resp.status, raw, dict(resp.headers)
                try:
                    return resp.status, json.loads(raw) if raw else {}, dict(resp.headers)
                except json.JSONDecodeError:
                    return resp.status, raw, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            return exc.code, raw, dict(exc.headers or {})
        except Exception as exc:  # noqa: BLE001
            return 0, f"{type(exc).__name__}: {exc}", {}

    # ---------------------------------------------------------- 授权

    def probe(self) -> dict:
        """探测：可达性、是否需要授权、写入是否支持。"""
        out: dict = {}
        code, payload, headers = self.request("GET", "/", auth=False)
        out["reachable"] = code == 200
        out["status"] = code
        out["server_id"] = headers.get("Zotero-Server-ID", "")
        out["api_version"] = headers.get("Zotero-API-Version", "")
        if not out["reachable"]:
            out["detail"] = str(payload)[:200]
            return out
        # 写端点存在性：故意不带 key
        code2, payload2, _ = self.request(
            "POST", "/local/authorize", {"appName": "zotero-kb probe"},
            auth=False, expect_json=False)
        out["authorize_status"] = code2
        if code2 == 404:
            out["write_supported"] = False
            out["hint"] = "本地 API 没有 /local/authorize —— 需要 Zotero 10+"
        elif code2 == 428:
            out["write_supported"] = True
            out["hint"] = "需要带上 Zotero-Server-ID（脚本会自动带）"
            out["need_server_id"] = True
        elif code2 in (200, 201):
            out["write_supported"] = True
        elif code2 in (401, 403):
            out["write_supported"] = True
            out["hint"] = "端点存在但被拒 —— 可能没勾选「允许本机其他应用通信」"
        else:
            out["write_supported"] = None
            out["detail"] = str(payload2)[:200]
        return out

    def authorize(self, app_name: str = "Zotero 文献知识库") -> tuple[bool, str]:
        """走一次授权。Zotero 会弹窗让用户点「允许」。"""
        if not self.server_id:
            _, _, headers = self.request("GET", "/", auth=False)
            self.server_id = headers.get("Zotero-Server-ID", "")
        extra = {"Zotero-Server-ID": self.server_id} if self.server_id else {}
        code, payload, _ = self.request(
            "POST", "/local/authorize",
            {"appName": app_name}, auth=False, extra_headers=extra)
        if code in (200, 201) and isinstance(payload, dict) and payload.get("key"):
            self._save_key(payload["key"])
            return True, payload["key"]
        return False, f"HTTP {code}: {str(payload)[:300]}"

    # ---------------------------------------------------------- 读

    def collections(self, library: str = "users/0") -> list[dict]:
        code, payload, _ = self.request(
            "GET", f"/{library}/collections?limit=100")
        if code != 200 or not isinstance(payload, list):
            raise RuntimeError(f"读分类失败：HTTP {code} {str(payload)[:200]}")
        return payload

    def items_in_collection(self, coll_key: str, library: str = "users/0") -> list[dict]:
        out: list[dict] = []
        start = 0
        while True:
            code, payload, _ = self.request(
                "GET",
                f"/{library}/collections/{coll_key}/items/top?limit=100&start={start}",
            )
            if code != 200 or not isinstance(payload, list):
                break
            out.extend(payload)
            if len(payload) < 100:
                break
            start += 100
        return out

    def item(self, key: str, library: str = "users/0") -> dict:
        code, payload, _ = self.request("GET", f"/{library}/items/{key}")
        return payload if code == 200 and isinstance(payload, dict) else {}

    # ---------------------------------------------------------- 写

    def create_collection(self, name: str, parent: str | None = None,
                          library: str = "users/0") -> tuple[bool, str]:
        body = [{"name": name}]
        if parent:
            body[0]["parentCollection"] = parent
        code, payload, _ = self.request("POST", f"/{library}/collections", body)
        if code in (200, 201) and isinstance(payload, dict):
            ok = payload.get("successful") or {}
            if ok:
                return True, list(ok.values())[0].get("key", "")
            return False, json.dumps(payload, ensure_ascii=False)[:300]
        return False, f"HTTP {code}: {str(payload)[:300]}"

    def update_item(self, key: str, patch: dict,
                    library: str = "users/0") -> tuple[bool, str]:
        code, payload, _ = self.request(
            "PATCH", f"/{library}/items/{key}", patch)
        if code in (200, 204):
            return True, ""
        return False, f"HTTP {code}: {str(payload)[:300]}"

    def delete_collection(self, coll_key: str, library: str = "users/0") -> tuple[bool, str]:
        """删除分类。

        ⚠ 删除也必须带 **If-Unmodified-Since-Version** —— Zotero 的 Web API 对
        POST/PATCH/**DELETE** 一律要求版本前置条件。实测漏了这步报
        `428: If-Unmodified-Since-Version not provided`，于是"文献都搬走了、
        空分类却删不掉"。所以先读一次拿 version 再删。
        """
        code, payload, _ = self.request(
            "GET", f"/{library}/collections/{coll_key}")
        version = payload.get("version") if isinstance(payload, dict) else None
        extra = ({"If-Unmodified-Since-Version": str(version)}
                 if version is not None else {})
        code, payload, _ = self.request(
            "DELETE", f"/{library}/collections/{coll_key}", extra_headers=extra)
        if code in (200, 204):
            return True, ""
        return False, f"HTTP {code}: {str(payload)[:300]}"

    def set_item_collections(self, item_key: str, collection_keys: list[str],
                             library: str = "users/0") -> tuple[bool, str]:
        """整份替换某条目的所属分类。

        注意必须用 **If-Unmodified-Since-Version**：Zotero 的 Web API 写操作
        都要求带上版本号，否则会 428 Precondition Required。
        """
        it = self.item(item_key, library)
        if not it:
            return False, "读条目失败"
        version = it.get("version")
        patch = {"collections": collection_keys}
        code, payload, _ = self.request(
            "PATCH", f"/{library}/items/{item_key}", patch,
            extra_headers={"If-Unmodified-Since-Version": str(version)})
        if code in (200, 204):
            return True, ""
        return False, f"HTTP {code}: {str(payload)[:200]}"


# ---------------------------------------------------------------- 业务


def load_plan() -> dict:
    if not os.path.exists(PLAN_PATH):
        raise SystemExit(f"找不到方案文件：{PLAN_PATH}")
    return json.load(open(PLAN_PATH, encoding="utf-8"))


def cmd_check() -> int:
    print("=" * 68)
    print("Zotero 本地 API 探测（只读，不写任何东西）")
    print("=" * 68)
    api = ZoteroAPI()
    info = api.probe()
    print(f"  端点：{api.base}")
    if not info.get("reachable"):
        print(f"{BAD}连不上（HTTP {info.get('status')}）")
        print("       Zotero 要开着；若一直连不上，检查它是否卡在某个弹窗上。")
        print(f"       详情：{info.get('detail', '')}")
        return 1
    print(f"{OK}可达")
    if info.get("server_id"):
        print(f"{OK}Zotero-Server-ID: {info['server_id']}（10+ 才有）")
    else:
        print(f"{WARN}没有 Zotero-Server-ID —— 可能是 Zotero 9.x，写入不可用")
    print(f"      API 版本：{info.get('api_version')}")
    ws = info.get("write_supported")
    if ws is True:
        print(f"{OK}写入端点存在")
    elif ws is False:
        print(f"{BAD}写入不可用：{info.get('hint', '')}")
        return 1
    else:
        print(f"{WARN}写入状态未知：{info.get('hint') or info.get('detail', '')}")

    if info.get("hint"):
        print(f"      提示：{info['hint']}")

    # 读一次分类，确认读权限
    try:
        cols = api.collections()
        print(f"{OK}能读分类（{len(cols)} 个）")
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}读分类失败：{exc}")
        print("      多半是没勾选 设置 → 高级 → 允许本机上的其他应用程序与 Zotero 通信")
        return 1

    if api.key:
        print(f"{OK}已保存授权 key（{KEY_PATH}）")
    else:
        print(f"{WARN}还没有授权 key —— 要写回先跑：zotero_sync.py authorize")
        print("      （Zotero 会弹窗，点「允许」即可；只需一次）")
    print("=" * 68)
    return 0


def cmd_authorize() -> int:
    print("=" * 68)
    print("申请 Zotero 本地写入授权")
    print("=" * 68)
    api = ZoteroAPI()
    info = api.probe()
    if not info.get("reachable"):
        print(f"{BAD}Zotero 本地 API 连不上，先按 check 的提示处理。")
        return 1
    print("  正在请求授权…… **请留意 Zotero 窗口，会弹出确认框，点「允许」**")
    ok, detail = api.authorize()
    if ok:
        print(f"{OK}已获得 key 并保存到 {KEY_PATH}")
        print("      之后写回不用再点允许（除非你在 Zotero 里撤销了）")
        return 0
    print(f"{BAD}授权失败：{detail}")
    if "403" in detail:
        print("      检查 设置 → 高级 → 勾选「允许本机上的其他应用程序与 Zotero 通信」")
    return 1


def build_actions(api: ZoteroAPI) -> tuple[list[dict], dict]:
    """把方案翻译成具体动作，同时做一致性检查。"""
    plan = load_plan()
    cols = api.collections()
    by_name = {c["data"]["name"]: c for c in cols}
    actions: list[dict] = []
    report: dict = {"existing": [c["data"]["name"] for c in cols],
                    "problems": [], "counts": {}}

    # 现有各类的条目数（用于核对）
    for name, col in by_name.items():
        items = api.items_in_collection(col["key"])
        report["counts"][name] = len(items)

    for merge in plan.get("merges", []):
        keep = merge["keep"]
        if keep not in by_name:
            report["problems"].append(f"保留分类「{keep}」不存在，跳过这组合并")
            continue
        keep_key = by_name[keep]["key"]
        for absorb in merge.get("absorb", []):
            if absorb not in by_name:
                report["problems"].append(f"待并入分类「{absorb}」不存在，跳过")
                continue
            absorb_col = by_name[absorb]
            items = api.items_in_collection(absorb_col["key"])
            actions.append({
                "kind": "merge",
                "keep": keep, "keep_key": keep_key,
                "absorb": absorb, "absorb_key": absorb_col["key"],
                "items": [i["key"] for i in items],
                "n_items": len(items),
                "reason": merge.get("reason", ""),
            })
    return actions, report


def cmd_plan() -> int:
    print("=" * 68)
    print("分类重整 · 干跑（不写任何东西）")
    print("=" * 68)
    api = ZoteroAPI()
    try:
        actions, report = build_actions(api)
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}读 Zotero 失败：{exc}")
        print("      先跑 zotero_sync.py check 确认可达且已勾选允许通信。")
        return 1

    print(f"\n现有分类 {len(report['existing'])} 个：")
    for name, n in sorted(report["counts"].items(), key=lambda x: -x[1]):
        print(f"    {name:34} {n:>3} 篇")

    if not actions:
        print(f"\n{WARN}没有可执行的动作（合并目标都不在现有分类里？）")
    for act in actions:
        print(f"\n  合并：「{act['absorb']}」({act['n_items']} 篇) → 「{act['keep']}」")
        print(f"        之后删除空分类「{act['absorb']}」")
        if act["reason"]:
            print(f"        理由：{act['reason']}")
        if act["n_items"]:
            sample = act["items"][:5]
            print(f"        涉及条目（前 5）：{sample}")

    plan = load_plan()
    print(f"\n预期最终分类：")
    for want in plan.get("expected_final", []):
        print(f"    {want['name']:34} {want['count']:>3} 篇")

    if report["problems"]:
        print(f"\n{WARN}问题：")
        for p in report["problems"]:
            print(f"    - {p}")
    print("\n（这是干跑。确认无误后跑：zotero_sync.py apply）")
    print("=" * 68)
    return 0


def cmd_apply() -> int:
    print("=" * 68)
    print("分类重整 · 执行")
    print("=" * 68)
    api = ZoteroAPI()
    if not api.key:
        print(f"{BAD}还没有授权。先跑：zotero_sync.py authorize")
        return 1
    actions, report = build_actions(api)
    if not actions:
        print("没有要执行的动作。")
        return 0

    total_items = sum(a["n_items"] for a in actions)
    print(f"  将移动 {total_items} 篇文献，涉及 {len(actions)} 组合并。")
    print("  注意：会同步到你的 zotero.org / 坚果云（这是 Zotero 的正常行为）。")
    ok_all = True

    for act in actions:
        print(f"\n▶ 「{act['absorb']}」 → 「{act['keep']}」")
        moved = failed = 0
        for key in act["items"]:
            it = api.item(key)
            if not it:
                failed += 1
                continue
            # 目标分类列表 = 现有分类 - 被并入的那个 + 保留的那个
            current = it.get("data", {}).get("collections", []) or []
            new_cols = [c for c in current if c != act["absorb_key"]]
            if act["keep_key"] not in new_cols:
                new_cols.append(act["keep_key"])
            ok, err = api.set_item_collections(key, new_cols)
            if not ok and ("401" in err or "expired" in err.lower()
                           or "Invalid" in err):
                # key 中途失效（实测批量跑到第 2 条就可能 401，而单独测又正常）。
                # 自动重授权一次再重试，免得整批白跑。
                log(f"  key 失效，重新授权…（{err[:60]}）")
                ok_auth, detail = api.authorize()
                if ok_auth:
                    ok, err = api.set_item_collections(key, new_cols)
                else:
                    log(f"  重新授权失败：{detail[:120]}")
            if ok:
                moved += 1
            else:
                failed += 1
                log(f"  条目 {key} 移动失败：{err}")
        print(f"   移动 {moved} 篇" + (f"，失败 {failed} 篇" if failed else ""))
        if failed:
            ok_all = False
            print(f"   {WARN}有失败，暂不删除空分类「{act['absorb']}」，等你核对后重跑")
            continue
        # 全部移走后再删空分类
        left = api.items_in_collection(act["absorb_key"])
        if left:
            print(f"   {WARN}「{act['absorb']}」里还剩 {len(left)} 篇，不删分类")
            ok_all = False
        else:
            ok, err = api.delete_collection(act["absorb_key"])
            if ok:
                print(f"   已删除空分类「{act['absorb']}」")
            else:
                print(f"   {WARN}删除分类失败：{err}")
                ok_all = False

    print("\n" + "=" * 68)
    print("完成。" if ok_all else "部分完成，请看上面提示。")
    print("核对：zotero_sync.py verify")
    print("=" * 68)
    return 0 if ok_all else 1


def cmd_verify() -> int:
    print("=" * 68)
    print("分类重整 · 核对")
    print("=" * 68)
    api = ZoteroAPI()
    try:
        cols = api.collections()
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}读分类失败：{exc}")
        return 1
    print(f"\n现有分类 {len(cols)} 个：")
    for c in sorted(cols, key=lambda x: x["data"]["name"]):
        n = len(api.items_in_collection(c["key"]))
        print(f"    {c['data']['name']:34} {n:>3} 篇")

    plan = load_plan()
    want = {w["name"]: w["count"] for w in plan.get("expected_final", [])}
    merged_away = set()
    for m in plan.get("merges", []):
        merged_away.update(m.get("absorb", []))
    got = {c["data"]["name"]: len(api.items_in_collection(c["key"])) for c in cols}

    print("\n与方案对照：")
    bad = 0
    warn = 0
    for name, expect in sorted(want.items(), key=lambda x: -x[1]):
        actual = got.get(name)
        if actual is None:
            print(f"    [XX] {name:32} 期望 {expect:>3}，实际**不存在**")
            bad += 1
        elif actual != expect:
            print(f"    [!!] {name:32} 期望 {expect:>3}，实际 {actual:>3}"
                  f"（±{actual - expect:+d}，多为后来又加过文献，属正常）")
            warn += 1
        else:
            print(f"    [OK] {name:32} {actual:>3} 篇")

    # 被合并掉的分类不该还在
    left = sorted(merged_away & set(got))
    if left:
        print(f"\n    [XX] 这些分类本该已合并删除，但仍然存在：{left}")
        bad += 1
    else:
        print(f"\n    [OK] 被合并的分类已全部清掉（{sorted(merged_away)}）")

    # 新增分类是好事，不算错（本机实测：用户自己又建了「稀疏线性最小二乘法」）
    extra = sorted(set(got) - set(want) - merged_away)
    if extra:
        print(f"    [--] 另有新增分类（不算问题）：{extra}")

    print(f"\n现有分类共 {len(got)} 个：")
    for name, n in sorted(got.items(), key=lambda x: -x[1]):
        print(f"      {name:34} {n:>3} 篇")

    print("\n" + ("分类结构与方案一致。" if not bad
                  else f"有 {bad} 处不一致，见上面。"))
    print("=" * 68)
    return 0 if not bad else 1


def cmd_apply_one(key: str, category: str, tags: list[str]) -> int:
    """把一篇文献归到一个分类（并可选打标签）—— 面板「应用建议」走这里。

    为什么单独做而不是复用 cmd_apply：
      `cmd_apply` 读的是 `taxonomy-plan.json`（整库重整的方案），
      而这里是"用户看着某一篇的建议，点一下应用"，不该去动那份方案文件。

    ⚠ 与批量重整共用同一套 ZoteroAPI（同样的版本头、同样的重授权逻辑），
      所以授权状态是共享的：批量重整授权过，这里就不用再授权。
    """
    print("=" * 68)
    print("应用分类建议（单篇）")
    print("=" * 68)
    print(f"  文献：{key}")
    print(f"  分类：{category or '（不改）'}")
    if tags:
        print(f"  标签：{'、'.join(tags)}")

    api = ZoteroAPI()
    if not api.key:
        print(f"\n{BAD}还没有授权。先在管理面板的「高级 → 分类重整」里点"
              f"「2 申请授权」，或跑：zotero_sync.py authorize")
        print("      （还需要在 Zotero 设置里勾选「允许本机上的其他应用程序"
              "与 Zotero 通信」）")
        return 1

    try:
        it = api.item(key)
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}读条目失败：{exc}")
        return 1
    if not it:
        print(f"{BAD}Zotero 里找不到条目 {key}")
        return 1

    cur = it.get("data", {}).get("collections", []) or []
    print(f"  现在归属：{len(cur)} 个分类")

    # 分类：只做"加进去"，不删原有的 —— 用户点的是"这篇该归到 X"，
    # 不是"把它的其他分类都去掉"。多归属由用户自己在 Zotero 里整理。
    if category:
        try:
            cols = api.collections()
        except Exception as exc:  # noqa: BLE001
            print(f"{BAD}读分类列表失败：{exc}")
            return 1
        by_name = {c["data"]["name"]: c for c in cols}
        col = by_name.get(category)
        if not col:
            print(f"{WARN}分类「{category}」不存在，先创建它…")
            ok, res = api.create_collection(category)
            if not ok:
                print(f"{BAD}创建分类失败：{res}")
                return 1
            col = res
        new_cols = list(cur)
        if col["key"] not in new_cols:
            new_cols.append(col["key"])
            ok, err = api.set_item_collections(key, new_cols)
            if not ok and ("401" in err or "expired" in err.lower()
                           or "Invalid" in err):
                log("  key 失效，重新授权…")
                if api.authorize()[0]:
                    ok, err = api.set_item_collections(key, new_cols)
            if ok:
                print(f"{OK}已归入「{category}」")
            else:
                print(f"{BAD}归入失败：{err}")
                return 1
        else:
            print(f"{OK}已经在「{category}」里了")

    # 标签：走 Zotero 本地 API（Web API 的 tags 是整份替换，所以要合并）
    if tags:
        data = it.get("data", {})
        have = {t.get("tag") for t in (data.get("tags") or [])}
        merged = [{"tag": t} for t in sorted(have | set(tags))]
        version = it.get("version")
        code, payload, _ = api.request(
            "PATCH", f"/users/0/items/{key}", {"tags": merged},
            extra_headers={"If-Unmodified-Since-Version": str(version)})
        if code in (200, 204):
            add = [t for t in tags if t not in have]
            print(f"{OK}标签已更新（新增 {len(add)} 个）")
        else:
            print(f"{BAD}写标签失败：HTTP {code} {str(payload)[:160]}")
            return 1

    print("\n注意：改动会同步到你的 zotero.org / 坚果云（Zotero 的正常行为）。")
    print("=" * 68)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="通过 Zotero 本地 API 写回分类")
    ap.add_argument("action",
                    choices=["check", "authorize", "plan", "apply", "verify",
                             "apply-one"],
                    nargs="?", default="check")
    ap.add_argument("--key", default="", help="apply-one：文献 key")
    ap.add_argument("--category", default="", help="apply-one：目标分类名")
    ap.add_argument("--tags", default="", help="apply-one：逗号分隔的标签")
    args = ap.parse_args()
    if args.action == "apply-one":
        if not args.key:
            print(f"{BAD}apply-one 需要 --key")
            return 1
        tags = [t.strip() for t in args.tags.replace("，", ",").split(",")
                if t.strip()]
        return cmd_apply_one(args.key, args.category, tags)
    return {"check": cmd_check, "authorize": cmd_authorize,
            "plan": cmd_plan, "apply": cmd_apply, "verify": cmd_verify}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
