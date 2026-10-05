"""offline/mineru.py 的探测解析单测（不需要真装 MinerU）。

    python tests/test_mineru_probe.py

为什么值得单独一个测试文件：`probe()` 要解析 `mineru-kit models show` 的
**人读输出**（不是 JSON），措辞随版本变，而这正是最容易错的地方 ——
本机实测踩过两次：
  · 状态词有两套：**缺模型时是「缺失」，齐了是「就绪」**（第一版只认
    「正常/缺失」，于是模型明明齐了却报"没下全"，把用户往重装引）；
  · `model.base_dir: D:/…` 这类行会被仓库正则误吞（必须限定在「仓库:」段里）。
所以这里用**真机采下来的原文**当夹具，另加四种失败形态（超时/崩了/未装/
模型缺失），并用 `runner` 注入假子进程 —— 不依赖真的 MinerU 在不在。

⚠ 最后一段是"真装了就跑、没装就 SKIP"的集成检查：本机装了 MinerU 时，
  它顺手证明"从源码到真实二进制"这条路是通的。
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "offline"))

import schemas as S      # noqa: E402
import mineru as M       # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------- 真机原文夹具
# 2026-10-05 在本机 `mineru-kit models show` 的真实输出（GPU + 模型齐）。
SHOW_READY = """配置文件: D:\\repo\\.mineru\\home\\config.yaml
配置文件存在: true
MINERU_MODEL_SOURCE=(unset)
model.base_dir: D:/repo/.mineru/models
model.base_dir.source: file
model.source: modelscope
model.source.source: file
model.small_backend: auto
model.small_backend.source: file
model.vlm.engine: llama-cpp
model.vlm.engine.source: file
生效的小模型后端: torch
生效的 VLM 引擎: llama-cpp
仓库:
  MinerU-4_models_torch: 就绪
(D:/repo/.mineru/models/MinerU-4_models_torch)
  MinerU-4_models_onnx: 就绪
(D:/repo/.mineru/models/MinerU-4_models_onnx)
  MinerU2.5-Pro-2605-1.2B: 缺失
(D:/repo/.mineru/models/MinerU2.5-Pro-2605-1.2B)
  MinerU2.5-Pro-2605-1.2B-GGUF: 就绪
(D:/repo/.mineru/models/MinerU2.5-Pro-2605-1.2B-GGUF)
模型档位:
  basic: MinerU-4_models_torch
  standard: MinerU-4_models_torch, MinerU2.5-Pro-2605-1.2B-GGUF
"""

# 没装模型时（首次装完 mineru-kit、还没跑 models download）。
SHOW_MISSING = """配置文件: C:\\Users\\x\\.mineru\\config.yaml
配置文件存在: false
model.base_dir: C:\\Users\\x\\.mineru\\models
生效的小模型后端: onnx
生效的 VLM 引擎: llama-cpp
仓库:
  MinerU-4_models_torch: 缺失
(C:\\Users\\x\\.mineru\\models\\MinerU-4_models_torch)
  MinerU-4_models_onnx: 缺失
(C:\\Users\\x\\.mineru\\models\\MinerU-4_models_onnx)
模型档位:
  basic: MinerU-4_models_onnx
  standard: MinerU-4_models_onnx, MinerU2.5-Pro-2605-1.2B-GGUF
"""


def test_parse_show():
    print("\n[1] 解析 `models show`（真机原文）")
    info = M._parse_show(SHOW_READY)
    check("小模型后端", info["small_backend"] == "torch", str(info))
    check("VLM 引擎", info["vlm_engine"] == "llama-cpp", str(info))
    check("仓库状态：就绪 与 缺失 都认（**「就绪」不是「正常」**）",
          info["repos"] == {"MinerU-4_models_torch": True,
                            "MinerU-4_models_onnx": True,
                            "MinerU2.5-Pro-2605-1.2B": False,
                            "MinerU2.5-Pro-2605-1.2B-GGUF": True},
          json.dumps(info["repos"], ensure_ascii=False))
    check("档位 → 仓库清单",
          info["tiers"].get("standard")
          == ["MinerU-4_models_torch", "MinerU2.5-Pro-2605-1.2B-GGUF"],
          str(info["tiers"]))
    check("basic 档就绪（它的仓库全就绪）", info["tier_ready"]["basic"] is True,
          str(info["tier_ready"]))
    check("standard 档就绪（1.2B 缺失但档位只要求 GGUF 那个）",
          info["tier_ready"]["standard"] is True, str(info["tier_ready"]))

    miss = M._parse_show(SHOW_MISSING)
    check("模型全缺时 basic 判为未就绪", miss["tier_ready"]["basic"] is False,
          str(miss["tier_ready"]))
    check("BASE_DIR 那几行没被当成仓库",
          "model.base_dir" not in miss["repos"], str(miss["repos"]))
    check("没模型时后端是 onnx", miss["small_backend"] == "onnx", str(miss))


def _fake_runner(show_out: str, show_code: int = 0, py_code: int = 0,
                 py_out: str = "", show_msg: str = ""):
    """造一个假 `_run`：第一条命令（models show）给 show_out，第二条给 py_out。"""
    calls = {"n": 0}

    def run(args, timeout=0, env_extra=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return show_code, (show_out or show_msg)
        return py_code, py_out

    return run, calls


def test_probe_states():
    print("\n[2] probe() 的四种状态（注入假 runner，不依赖真装）")
    # ⚠ **一定要把配置/状态文件重定向到临时目录**：否则
    #   `S.write_location_config(...)` 会去改用户真实的 kb-location.json
    #   —— 本机第一版测试就这么把 `env.project_root` 覆盖没了
    #   （那是本机路径记录，丢了会让"项目目录"退回自动探测）。
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="kbmineru_probe_")
    olds = (S.LOCATION_FILE, S.USER_LOCATION_FILE, S.USER_CONFIG_DIR,
            S._scan_for_mineru, os.environ.get("ZOTERO_KB_MINERU"))
    S.LOCATION_FILE = os.path.join(tmp, "kb-location.json")
    S.USER_LOCATION_FILE = os.path.join(tmp, "location.json")
    S.USER_CONFIG_DIR = tmp
    S._scan_for_mineru = lambda: []          # 关掉自动发现（本机真的装了）
    M.clear_cache()

    # 造一个"能被当 kit 用"的假 exe：**必须有个 python.exe 兄弟**
    #（probe 会拿同环境的 python 去问版本/GPU；runner 是假的，不会真执行）
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir, exist_ok=True)
    fake_exe = os.path.join(bindir, "mineru-kit.exe")
    open(fake_exe, "w", encoding="utf-8").close()
    shutil.copyfile(sys.executable, os.path.join(bindir, "python.exe"))
    os.environ["ZOTERO_KB_MINERU"] = fake_exe

    py_json = "KBJSON" + json.dumps({
        "version": "4.0.10", "torch": "2.14.1+cu130", "cuda": "13.0",
        "cuda_ok": True, "gpu": "NVIDIA GeForce RTX 4060 Laptop GPU"})
    try:
        M.clear_cache()
        run, _ = _fake_runner(SHOW_READY, py_out=py_json)
        info = M.probe(force=True, runner=run)
        check("就绪：ok=True", info["ok"] is True, str(info.get("why")))
        check("版本/GPU 从同环境的 python 里取到",
              info["version"] == "4.0.10" and info["cuda_ok"] is True
              and "4060" in info["gpu"], json.dumps(
                  {k: info[k] for k in ("version", "torch", "cuda", "gpu")},
                  ensure_ascii=False))
        check("why 是人话（列出就绪档位）",
              "basic" in info["why"], info["why"])
        check("缓存生效（第二次不再跑子进程，cached=True）",
              M.probe().get("cached") is True)

        M.clear_cache()
        run, _ = _fake_runner(SHOW_MISSING, py_out=py_json)
        info = M.probe(force=True, runner=run)
        check("模型没下全时 ok 仍为 True，但 why 明确说「没下全」",
              info["ok"] is True and "没下全" in info["why"], info["why"])

        M.clear_cache()
        run, _ = _fake_runner("", show_code=124, show_msg="超时（>90 秒）")
        info = M.probe(force=True, runner=run)
        check("超时：ok=False 且 why 带原因（不抛异常）",
              info["ok"] is False and "超时" in info["why"], info["why"])

        M.clear_cache()
        run, _ = _fake_runner("", show_code=127, show_msg="")
        info = M.probe(force=True, runner=run)
        check("跑不起来：ok=False 且 why 有话",
              info["ok"] is False and bool(info["why"]), info["why"])

        M.clear_cache()
        os.environ.pop("ZOTERO_KB_MINERU", None)
        S.write_location_config(env={"mineru": ""}, mineru_exe="")
        info = M.probe(force=True)
        check("没装：ok=False，why 指路（安装脚本 / 面板引导）",
              info["ok"] is False and "install-mineru" in info["why"],
              info["why"])
        check("没装时不跑子进程（exe 为空）", info["exe"] == "", info["exe"])
    finally:
        (S.LOCATION_FILE, S.USER_LOCATION_FILE, S.USER_CONFIG_DIR,
         S._scan_for_mineru, old_env) = olds
        if old_env is None:
            os.environ.pop("ZOTERO_KB_MINERU", None)
        else:
            os.environ["ZOTERO_KB_MINERU"] = old_env
        M.clear_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def test_summary_line():
    print("\n[3] summary_line（面板/日志那一行）")
    s = M.summary_line({"ok": True, "version": "4.0.10",
                        "torch": "2.14.1+cu130", "cuda": "13.0",
                        "small_backend": "torch", "vlm_engine": "llama-cpp",
                        "tier_ready": {"basic": True, "standard": False}})
    check("带版本/后端/引擎/档位", "4.0.10" in s and "torch" in s
          and "basic" in s, s)
    check("没装时给的是人话", "未检测到" in M.summary_line({"ok": False,
                                                     "why": "没找到"}))


def test_real_if_installed():
    print("\n[4] 真装了就跑一遍集成检查（没装则 SKIP）")
    exe = S.resolve_mineru()
    if not exe:
        print("  SKIP  本机没装 MinerU（这正是可选组件的正常状态）")
        return
    info = M.probe(force=True)
    check(f"本机 MinerU 可用：{exe}", info["ok"] is True, str(info.get("why")))
    check("能报出后端与引擎（说明 CLI 真跑起来了）",
          bool(info["small_backend"] and info["vlm_engine"]), str(info))
    if info.get("cuda_ok"):
        print(f"       GPU：{info.get('gpu')} / torch {info.get('torch')}")
    print("       " + M.summary_line(info))


def main() -> int:
    test_parse_show()
    test_probe_states()
    test_summary_line()
    test_real_if_installed()
    print(f"\n{'=' * 60}\n通过 {PASS}　失败 {FAIL}\n{'=' * 60}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
