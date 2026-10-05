"""offline/mineru.py —— MinerU（**可选组件**）的探测。

MinerU 是"能装就更好、不装也能用"的东西：装了它，PDF 解析出来的正文更干净、
公式是 LaTeX、表格和扫描件更稳；没装就继续用 `zreader.fulltext_for()`
（Zotero 自己的 `.zotero-ft-cache` + PyMuPDF + 模型仲裁）。

**本轮（2026-10-05）这个模块只做探测**：
    probe()  →  装没装、在哪、什么版本、能跑哪些档位、GPU 可用不可用
下一轮会在这里加 `fulltext_for(item, tier) -> (pages, source)`（与
`zreader.fulltext_for` **同形**），接进 `convert.py` 的正文抽取那一步 ——
接缝已经留好，下游（md/切片/向量/MANIFEST）不用改。

三条设计约束（写在这里免得后来人踩）：
  · **只用标准库**：面板、本地服务、CLI 三处都会 import 它，不能拖重依赖。
  · **快 + 可缓存**：`/mineru-check` 每次都会调它，默认缓存 60 秒。
  · **找不到不是错误**：`ok=False` 是**正常状态**，调用方据此降级；
    不要在这里抛异常（面板/插件会因为一个可选组件而变红）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schemas as S  # noqa: E402

# 探测结果缓存：键是 exe 路径（"" = 自动探测），值 {"at": 时间戳, "data": {...}}
_CACHE: dict[str, dict] = {}
PROBE_TTL = 60.0

# Windows 上别让子进程弹黑框（插件/服务是 pythonw 起的，弹一个 console 很丑）
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _cache_get(key: str) -> dict | None:
    item = _CACHE.get(key)
    if not item:
        return None
    if time.time() - item["at"] > PROBE_TTL:
        return None
    return item["data"]


def clear_cache() -> None:
    _CACHE.clear()


def _run(args: list[str], timeout: float = 90.0,
         env_extra: dict | None = None) -> tuple[int, str]:
    """跑一个子进程，返回 (退出码, 合并输出)。**不抛**：超时/找不到都算失败。

    ⚠ 一律 `CREATE_NO_WINDOW` + `encoding="utf-8"`：MinerU 的输出是中文，
      而 Windows 默认代码页是 GBK，不指定编码会得到一堆乱码
      （"生效的小模型后端" 会变成问号，正则就匹配不到了）。
    """
    env = dict(os.environ)
    for k, v in (env_extra or {}).items():
        if v:
            env[k] = v
    try:
        p = subprocess.run(
            args, capture_output=True, timeout=timeout, env=env,
            encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout:.0f} 秒）"
    except FileNotFoundError:
        return 127, "找不到可执行文件"
    except OSError as exc:
        return 126, f"{type(exc).__name__}: {exc}"
    out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
    return p.returncode, out


def kit_python(exe: str) -> str:
    """从 `mineru-kit.exe` 推出同环境的 `python.exe`（拿版本/GPU 信息用）。"""
    if not exe:
        return ""
    d = os.path.dirname(exe)
    for name in ("python.exe", "python"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return ""


def _parse_show(text: str) -> dict:
    """解析 `mineru-kit models show` 的输出。

    不同版本的措辞会变，所以**每一条都当"能捡到就捡"**：捡不到不影响
    `probe()` 的结论（exe 存在就是 ok=True），只是相应字段留空。
    """
    info: dict = {"small_backend": "", "vlm_engine": "", "tiers": {}, "repos": {}}

    m = re.search(r"生效的小模型后端\s*[:：]\s*(\S+)", text)
    if m:
        info["small_backend"] = m.group(1).strip()
    m = re.search(r"生效的\s*VLM\s*引擎\s*[:：]\s*(\S+)", text)
    if m:
        info["vlm_engine"] = m.group(1).strip()

    # 仓库段**逐行**扫：`  <名字>: 就绪/正常/缺失`，后面可能跟一行括号里的路径。
    #
    # ⚠ 状态词有两套：**缺模型时是「缺失」，模型齐了是「就绪」**（本机实测；
    #   我第一版只认「正常/缺失」，于是模型明明齐了却判成"没下全"）。
    #   所以这里**只否掉"缺失"**，其余一律算就绪 —— 版本换个词也不会误判。
    # ⚠ 不能拿整篇文本正则扫（`model.base_dir: D:/…` 也会中招），必须限定在
    #   「仓库:」到下一个段头之间，且跳过括号续行。
    in_repo = False
    for ln in text.splitlines():
        if re.match(r"^\s*仓库\s*[:：]?\s*$", ln):
            in_repo = True
            continue
        if re.match(r"^\s*(模型档位|配置文件|生效的|MINERU_)\s*[:：]", ln):
            in_repo = False
            continue
        if not in_repo:
            continue
        m = re.match(r"^\s+([A-Za-z0-9][\w.\-]*)\s*[:：]\s*(\S+)", ln)
        if m:
            info["repos"][m.group(1)] = not m.group(2).startswith("缺失")

    # 档位段：`  basic: A, B` / `  standard: A, B`
    block = ""
    m = re.search(r"模型档位\s*[:：]\s*(.*?)(?:\n\s*\n|\Z)", text, re.S)
    if m:
        block = m.group(1)
    if not block:
        block = text
    for m in re.finditer(r"^\s*(basic|standard|flash|advanced)\s*[:：]\s*(.+)$",
                         block, re.M):
        repos = [x.strip() for x in m.group(2).split(",") if x.strip()]
        info["tiers"][m.group(1)] = repos

    # 连带算一下"这一档的模型齐不齐"（齐 = 所有仓库都 正常）
    info["tier_ready"] = {
        tier: bool(repos) and all(info["repos"].get(r) for r in repos)
        for tier, repos in info["tiers"].items()
    }
    return info


def probe(exe: str = "", *, force: bool = False, with_gpu: bool = True,
          timeout: float = 90.0, runner=None) -> dict:
    """探测 MinerU。**任何情况都返回 dict**（不抛）。

    `runner` 只为测试留的口子（默认 `_run`）：单测里注入一个假 runner 就能
    覆盖"就绪/缺失/超时/崩了"四种输出，不用真装一个 MinerU
    —— 解析那两个正则的地方正是最容易出错的地方（本机就踩过：只认「正常」
    不认「就绪」，于是模型明明齐了却报"没下全"）。

    返回字段：
        ok           装没装（能不能用）
        exe          mineru-kit 的完整路径（"" = 没找到）
        version      mineru 的版本号（拿不到就空）
        small_backend 生效的小模型后端（torch / onnx）
        vlm_engine    生效的 VLM 引擎（llama-cpp / lmdeploy / vllm）
        tiers / tier_ready   档位 → 需要哪些模型仓库 / 这一档齐没齐
        repos        仓库名 → 是否完整
        model_dir     模型目录
        home          MINERU_HOME
        torch / cuda / cuda_ok / gpu   推理环境
        why          没装或没配好时的**人话原因**（给面板/对话框直接用）
        elapsed      这次探测花了多少秒
    """
    run = runner or _run
    t0 = time.time()
    key = exe or "auto"
    if not force:
        hit = _cache_get(key)
        if hit is not None:
            data = dict(hit)
            data["cached"] = True
            return data

    kit = S.resolve_mineru(exe) if not exe else exe
    data: dict = {
        "ok": False, "exe": kit or "", "version": "",
        "small_backend": "", "vlm_engine": "", "tiers": {}, "tier_ready": {},
        "repos": {}, "model_dir": "", "home": S.mineru_home(),
        "torch": "", "cuda": "", "cuda_ok": None, "gpu": "",
        "why": "", "checked_at": time.time(), "elapsed": 0.0, "cached": False,
    }
    if not kit:
        data["why"] = ("未检测到 MinerU。它是可选组件 —— 不装也不影响现有功能；"
                       "想装：面板「知识库结构 → MinerU 安装引导」，"
                       "或双击 scripts\\install-mineru.cmd")
        data["elapsed"] = round(time.time() - t0, 2)
        _CACHE[key] = {"at": time.time(), "data": data}
        return data

    env_extra = {"MINERU_HOME": data["home"]} if data["home"] else {}

    # ---- ① models show：后端/引擎/档位/模型是否齐
    code, out = run([kit, "models", "show"], timeout=timeout, env_extra=env_extra)
    parsed = _parse_show(out)
    # ⚠ 退出码非 0 **且什么都没解析出来** = 真没跑成（超时/崩了/DLL 缺）。
    #   不能只看"输出非空"：超时的提示语是进程写的，非空但不含任何有用字段，
    #   放过去就会报成"装了，但模型还没下全"—— 把用户往"重下模型"引
    #   （本机测试逮到：超时被报成模型缺失）。
    if code != 0 and not (parsed.get("small_backend") or parsed.get("vlm_engine")
                          or parsed.get("tiers")):
        data["why"] = (f"`mineru-kit models show` 没跑成（退出码 {code}）："
                       f"{(out or '').strip()[:200] or '没有任何输出'}")
        data["elapsed"] = round(time.time() - t0, 2)
        _CACHE[key] = {"at": time.time(), "data": data}
        return data
    data.update(parsed)
    m = re.search(r"model\.base_dir\s*[:：]\s*(\S+)", out)
    if m:
        data["model_dir"] = m.group(1).strip()

    # ---- ② 同环境的 python：版本 + torch/CUDA
    if with_gpu:
        py = kit_python(kit)
        if py:
            js = (
                "import json\n"
                "o={}\n"
                "try:\n"
                " from importlib.metadata import version as _v\n"
                " o['version']=_v('mineru')\n"
                "except Exception as e:o['version_err']=str(e)[:80]\n"
                "try:\n"
                " import torch\n"
                " o['torch']=torch.__version__;o['cuda']=torch.version.cuda or ''\n"
                " o['cuda_ok']=bool(torch.cuda.is_available())\n"
                " o['gpu']=torch.cuda.get_device_name(0) if o['cuda_ok'] else ''\n"
                "except Exception as e:o['torch_err']=str(e)[:80]\n"
                "print('KBJSON'+json.dumps(o))\n"
            )
            c2, out2 = run([py, "-c", js], timeout=max(timeout, 120.0))
            m = re.search(r"KBJSON(\{.*\})", out2)
            if m:
                try:
                    j = json.loads(m.group(1))
                except json.JSONDecodeError:
                    j = {}
                data["version"] = str(j.get("version") or "")
                data["torch"] = str(j.get("torch") or "")
                data["cuda"] = str(j.get("cuda") or "")
                data["cuda_ok"] = j.get("cuda_ok")
                data["gpu"] = str(j.get("gpu") or "")
                if j.get("torch_err"):
                    data["why"] = f"torch 不可用：{j['torch_err']}"

    data["ok"] = True
    if not data["why"]:
        ready = [t for t, v in (data.get("tier_ready") or {}).items() if v]
        if ready:
            data["why"] = "可用（已就绪档位：" + "、".join(sorted(ready)) + "）"
        else:
            data["why"] = "装了，但**模型还没下全**（跑一次 scripts\\install-mineru.cmd，或面板安装引导）"
    data["elapsed"] = round(time.time() - t0, 2)
    _CACHE[key] = {"at": time.time(), "data": data}
    return data


def summary_line(info: dict) -> str:
    """给面板/日志用的一行人话。"""
    if not info.get("ok"):
        return f"未检测到（{info.get('why', '')[:60]}）"
    bits = []
    if info.get("version"):
        bits.append(f"mineru {info['version']}")
    if info.get("torch"):
        cuda = info.get("cuda") or "cpu"
        bits.append(f"torch {info['torch']}（{cuda}）")
    if info.get("small_backend"):
        bits.append(f"小模型 {info['small_backend']}")
    if info.get("vlm_engine"):
        bits.append(f"VLM {info['vlm_engine']}")
    if info.get("tier_ready"):
        ready = [t for t, v in info["tier_ready"].items() if v]
        bits.append("档位 " + ("/".join(sorted(ready)) if ready else "模型未下全"))
    return " · ".join(bits) if bits else "已检测到"


def main(argv: list[str] | None = None) -> int:
    """`python offline/mineru.py` —— 手动看一眼探测结果（排查用）。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in argv or "-f" in argv
    exe = ""
    for a in argv:
        if a.startswith("--exe="):
            exe = a.split("=", 1)[1]
    info = probe(exe, force=force)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if info.get("ok"):
        print("\n" + summary_line(info))
    return 0 if info.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
