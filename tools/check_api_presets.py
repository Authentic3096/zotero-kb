"""检查插件设置面板里的「服务商预设」和服务端认的地址是否一致。

    python tools/check_api_presets.py

为什么需要：外接 API 这件事有**两处**在记服务商地址 ——
    · `zotero-plugin/settings.js` 的 `KB_API_PRESETS`（选服务商时自动填的地址）
    · `offline/judge.py` 的 `KNOWN_BASE_URLS`（按模型名推断时用的地址）
两处一旦不一致，就会出现最难查的一类问题：**设置界面显示一个地址、
实际请求打到另一个地址** —— 用户看到的和实际发生的不符，报错也看不懂。

本机就吃过这个亏的同类问题：知识库位置在插件和服务端各算一遍，
搬目录后两边不一致，面板报"找不到 Python 环境"。
所以这里做成自动检查，并在 check_plugin.py 里跑。
"""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SETTINGS_JS = os.path.join(ROOT, "zotero-plugin", "settings.js")

sys.path.insert(0, os.path.join(ROOT, "offline"))


def presets_from_js() -> dict[str, str]:
    """从 settings.js 里抠出 KB_API_PRESETS 的 {key: base}。

    不引 JS 引擎：那段是规规矩矩的 `key: { base: "url", ... }`，
    正则够用，而且这是给人看的检查脚本，不追求通用解析。

    ⚠ 返回空字典**不算错** —— 2026-10-03 按用户要求把外接 API 的 UI
      从插件里去掉了，"选服务商"那个下拉也随之消失，所以
      `KB_API_PRESETS` 不再存在。此时本脚本退化成"只检查服务端一侧"。
      将来要把 UI 加回来，把常量恢复即可，这个检查会自动重新对比两处。
    """
    try:
        src = open(SETTINGS_JS, encoding="utf-8").read()
    except OSError as exc:
        print(f"  [XX] 读不到 settings.js：{exc}")
        return {}
    m = re.search(r"const KB_API_PRESETS\s*=\s*\{(.*?)\n\};", src, re.S)
    if not m:
        return {}
    body = m.group(1)
    out: dict[str, str] = {}
    # 逐个块：  key: { ... base: "..." ... },
    for blk in re.finditer(
            r'(\w+)\s*:\s*\{(.*?)\n\s*\}', body, re.S):
        name, inner = blk.group(1), blk.group(2)
        bm = re.search(r'base:\s*"([^"]*)"', inner)
        if bm:
            out[name] = bm.group(1)
    return out


def main() -> int:
    print("=" * 70)
    print("外接 API 服务商地址一致性检查")
    print("=" * 70)

    import judge as J

    js = presets_from_js()
    py = dict(J.KNOWN_BASE_URLS or {})

    if not js:
        # 插件侧没有预设（UI 已移除）—— 只核对服务端一侧的完整性
        print("  插件设置面板**没有**外接 API 预设"
              "（按用户要求移除了这块 UI，属正常）")
        print(f"  服务端仍保留 {len(py)} 个服务商地址（能力没删，随时可开回）：")
        for k in sorted(py):
            print(f"    {k:14} {py[k]}")
        missing = [k for k in ("deepseek", "dashscope", "openai")
                   if k not in py]
        print()
        if missing:
            print(f"  [XX] 常用服务商地址缺失：{missing}")
            return 1
        print("  [OK] 常用服务商（DeepSeek / 通义千问 / OpenAI）地址都在")
        print("  [OK] 这种情况下插件不会用到它们，不构成不一致风险")
        return 0

    print(f"  settings.js 预设 {len(js)} 个，judge.py 已知 {len(py)} 个\n")
    bad = 0

    # 1) 两边都有的，地址必须完全相同
    for key in sorted(set(js) & set(py)):
        if key == "custom":
            continue
        a, b = js[key].rstrip("/"), py[key].rstrip("/")
        if a != b:
            print(f"  [XX] {key} 地址不一致：")
            print(f"         settings.js : {a}")
            print(f"         judge.py    : {b}")
            bad += 1
        else:
            print(f"  [OK] {key:14} {a}")

    # 2) settings.js 有、judge.py 没有的 —— 不算错，但要说一声
    only_js = sorted(set(js) - set(py) - {"custom"})
    if only_js:
        print(f"\n  [!!] 只在插件预设里（judge 按模型名推断时认不出这几家）："
              f"{only_js}")
        print(f"       影响：用户选了这家但不填地址时，服务端猜不到 → 会报"
              f"「没有指定模型」或连不上。")

    # 3) 预设里必须有 custom（给自建服务留口子）
    if "custom" not in js:
        print("  [XX] 预设里没有 custom（自建服务 vLLM / LM Studio 就没入口了）")
        bad += 1
    else:
        print("\n  [OK] 有 custom 预设（自建服务可手填地址）")

    # 4) 每个预设都得有 base 和 model（custom 的可以为空）
    empty = [k for k, v in js.items() if k != "custom" and not v]
    if empty:
        print(f"  [XX] 这些预设的 base_url 是空的：{empty}")
        bad += 1

    print()
    print("=" * 70)
    if bad:
        print(f"发现 {bad} 个问题")
        return 1
    print("通过：插件预设与服务端认的地址一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
