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
            data["why"] = ("装了，但「模型还没下全」"
                           "（跑一次 scripts\\install-mineru.cmd，或面板的安装引导）")
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


# ================================================================ 解析（接进转换管道）
#
# 这一节是"第二轮"：把 MinerU 真正接进 `convert.py` 的正文抽取那一步
# （探测/安装那部分在上面，两轮之间没有别的依赖）。
#
# ## 为什么用 `--format zip` 而不是默认的 markdown
#
# 实测两种输出（同一篇 5 页论文，basic 档）：
#   · `--format markdown`：一个 .md，**图片以 base64 内联**（14 张、最长 88228 字符），
#     整份 621 KB；而且**没有任何页码信息**（没有 \f、没有注释、没有 `## p.N`）。
#     base64 进切片会把向量彻底带偏，页码丢了则 `kb_fulltext(page_from=…)`
#     与"检索结果带页码"这两个核心能力全废。
#   · `--format zip`：markdown.md（图片是相对路径引用，同样 2 页只 9.8 KB）+
#     `structured_content.json`（**按页**：`pages[].page_idx` + `blocks[]`，
#     块类型有 doc_title/paragraph_title/text/equation/table/header/page_footnote…）
#     + middle_json + images/。
# 所以选 zip：页码从 `structured_content.json` 来，图片落成文件，
# 页眉页脚由 MinerU **自己标出来**（比我们那套正则可靠）。
#
# ## 产物的落点与"要不要重跑"
#
#   <kb>/mineru/<KEY>/markdown.md            全文（图片相对路径）
#                     structured_content.json 按页结构（渲染 pages 的输入）
#                     middle_json.json        原始版面（排查用）
#                     images/*.jpg            公式/图表切图
#                     pages.json              **已渲染好的逐页 markdown**（KB 直接吃）
#                     meta.json               指纹/档位/耗时/页数/图片数/工具版本
# 指纹 = 绝对路径 + 大小 + mtime_ns + 档位 + 可执行文件 + 工具版本。
# 命中就跳过重跑（"全库重解析"时只会处理真变过的那些），`--force` 覆盖。
# 产物**不进 index.db**：状态从 `items.fulltext_src` + meta.json + 目录是否存在读，
# 不值得为它加一张表（面板「PDF 解析」页就是这么看的）。

ART_ROOT = "mineru"
PARSE_TIMEOUT = 3600.0            # 单篇上限：basic 档 5 页约 16~33 秒，慢机器留足
PAGE_JSON = "pages.json"
META_JSON = "meta.json"
# 渲染 pages 时要**丢掉**的块类型：MinerU 分出来的页眉/页脚/页码
# （这正是我们原来用正则 + 灰区模型仲裁想干掉的东西，它直接标出来了）
DROP_BLOCK_TYPES = ("header", "footer", "page_footnote", "page_number",
                    "page_header", "page_footer")


def _kb_dir(kb_dir: str = "") -> str:
    """知识库目录（产物落在它下面）。留空按 schemas 解析。"""
    if kb_dir:
        return kb_dir
    try:
        return S.kb_dir()          # 与 convert / 面板同一个事实定义
    except Exception:      # noqa: BLE001
        try:
            return os.path.dirname(S.INDEX_DB)
        except Exception:      # noqa: BLE001
            return ""


def artifact_dir(key: str, kb_dir: str = "") -> str:
    return os.path.join(_kb_dir(kb_dir), ART_ROOT, key)


def read_meta(key: str, kb_dir: str = "") -> dict:
    """读 meta.json（没有/坏了都返回 {}）。"""
    p = os.path.join(artifact_dir(key, kb_dir), META_JSON)
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:      # noqa: BLE001
        return {}


def load_pages(key: str, kb_dir: str = "") -> list[str]:
    """读已渲染好的逐页文本（没有就返回空列表）。"""
    p = os.path.join(artifact_dir(key, kb_dir), PAGE_JSON)
    try:
        with open(p, encoding="utf-8") as fh:
            pages = json.load(fh)
        return [str(x) for x in pages] if isinstance(pages, list) else []
    except Exception:      # noqa: BLE001
        return []


def fingerprint(pdf: str, tier: str, exe: str, tool_version: str = "") -> str:
    """PDF + 档位 + 工具 的指纹（决定要不要重跑）。

    ⚠ `tool_version` 一定要带上：MinerU 升级后同一份 PDF 的产物也会变
      （本机从 4.0.10 起的实测），不带上就永远命中旧产物。
      用 `probe()` 报的版本（`importlib.metadata.version` 在**项目 venv** 里
      查不到 mineru —— 它装在 `.mineru\\.venv`，所以这里不能自己查）。
    """
    import hashlib
    try:
        st = os.stat(pdf)
        size, mtime = st.st_size, st.st_mtime_ns
    except OSError:
        size, mtime = 0, 0
    raw = "|".join([os.path.abspath(pdf), str(size), str(mtime), tier, exe,
                    tool_version])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _as_text(val) -> str:
    """把块字段可能的各种形状（str / list[str] / None）压成一行文本。

    ⚠ 实测（本机 MinerU 4.0.10）：`img_caption` / `img_footnote` 是**列表**
      （如 `["图 2 测点分布图"]`）。直接 str() 会渲染成 `['图 2 …']`，
      带方括号和引号 —— 正文里难看，进检索还是噪声。
    """
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        return " ".join(_as_text(x) for x in val).strip()
    return str(val).strip()


def render_pages(structured: dict) -> list[str]:
    """把 `structured_content.json` 渲染成**逐页 markdown**（索引 0 = 第 1 页）。

    规则（都是为了喂 KB，不是为了好看）：
      · 页眉/页脚/页码 **丢掉**（MinerU 已标注类型，比正则可靠）；
      · 标题按 `level` 加 `#`；`text` 原样（里面本来就带 `<sup>` 之类）；
      · 公式包成 `$$…$$`（LaTeX 原样，能被检索命中）；
      · 表格用 `content`（MinerU 给的是 HTML/markdown，原样保留）；
      · 图片：写一行 `![chart](images/xxx.jpg)` **并带上图注文字**
        （图注是能搜到的信息，别丢）；`--disable-image-analysis` 时没有图注也不报错；
      · 未知类型：有 `content` 就用 content，没有就跳过（宁可少写，不要写错）。
    """
    out: list[str] = []
    for pg in (structured.get("pages") or []):
        if not isinstance(pg, dict):
            continue
        lines: list[str] = [render_block(b) for b in (pg.get("blocks") or [])]
        # 空行分隔：KB 的切片按行/段来，粘连会把两段并成一块
        out.append("\n\n".join(x for x in lines if x))
    return out


def block_type(b) -> str:
    """块的类型（小写）；不是 dict 就给空串。给外层按类型切节用。"""
    if not isinstance(b, dict):
        return ""
    return str(b.get("type") or "").lower()


def render_block(b) -> str:
    """渲染**一个块**成 markdown 片段（空串 = 这一块不产出文本）。

    规则（与 render_pages 完全共用，避免两处漂移）：
      · 页眉/页脚/页码 → 丢掉；
      · 标题 → `#`/`##`（按 level）；
      · 公式 → `$$…$$`；
      · 图片 → 一行 `![图注](images/xxx.jpg)` +（图注另起一行）；
      · 其它有 content 的原样保留。
    """
    if not isinstance(b, dict):
        return ""
    typ = block_type(b)
    if typ in DROP_BLOCK_TYPES:
        return ""
    content = str(b.get("content") or "").strip()
    if typ in ("doc_title", "title"):
        return "# " + content
    if typ in ("paragraph_title", "section_title", "heading"):
        lvl = b.get("level")
        try:
            lvl = max(2, min(6, int(lvl) + 1))
        except Exception:      # noqa: BLE001
            lvl = 2
        return "#" * lvl + " " + content
    if typ in ("equation", "interline_equation", "inline_equation"):
        return "$$" + content + "$$" if content else ""
    if typ in ("image", "chart", "figure"):
        img = _as_text(b.get("img_path") or b.get("image_path") or "")
        cap = " ".join(
            _as_text(b.get(k)) for k in
            ("img_caption", "caption", "img_footnote")
            if _as_text(b.get(k))).strip()
        parts = []
        if img:
            parts.append(f"![{cap or typ}]({img})")
        if cap:
            parts.append(cap)
        elif content and content != img:
            parts.append(content)
        return "\n".join(parts)
    return content


def parse_item(key: str, pdf: str, tier: str = "basic", kb_dir: str = "",
               force: bool = False, timeout: float = PARSE_TIMEOUT,
               runner=None, log=None) -> dict:
    """把一篇 PDF 交给 MinerU 解析，产物落到 `<kb>/mineru/<KEY>/`。

    返回 dict（**任何情况都返回，不抛**）：
      ok / cached / key / tier / pages(list[str]) / source("mineru-basic")
      seconds / why / artifacts{markdown, structured, pages, images, meta}
      n_pages / n_images / fingerprint
    """
    t0 = time.time()

    def note(msg: str):
        if log:
            try:
                log(msg)
            except Exception:      # noqa: BLE001
                pass

    info = probe(force=False)
    out = {"ok": False, "cached": False, "key": key, "tier": tier, "pages": [],
           "source": f"mineru-{tier}", "seconds": 0.0, "why": "",
           "n_pages": 0, "n_images": 0, "fingerprint": "", "artifacts": {}}
    if not info.get("ok") or not info.get("exe"):
        out["why"] = "MinerU 不可用：" + (info.get("why") or "没装/没配好")
        return out
    exe = info["exe"]
    if not pdf or not os.path.isfile(pdf):
        out["why"] = f"PDF 不存在：{pdf}"
        return out
    ready = (info.get("tier_ready") or {}).get(tier)
    if ready is False:
        out["why"] = (f"{tier} 档的模型还没下全（跑一次安装引导，"
                      f"或 mineru-kit models download --tier {tier}）")
        return out

    fp = fingerprint(pdf, tier, exe, str(info.get("version") or ""))
    out["fingerprint"] = fp
    adir = artifact_dir(key, kb_dir)
    meta = read_meta(key, kb_dir)
    pages_path = os.path.join(adir, PAGE_JSON)
    md_path = os.path.join(adir, "markdown.md")
    if (not force and meta.get("fingerprint") == fp and meta.get("ok")
            and os.path.isfile(md_path) and os.path.isfile(pages_path)):
        pages = load_pages(key, kb_dir)
        if pages:
            out.update({"ok": True, "cached": True, "pages": pages,
                        "n_pages": len(pages),
                        "n_images": int(meta.get("n_images") or 0),
                        "seconds": 0.0,
                        "why": "命中指纹，用已有产物",
                        "artifacts": meta.get("artifacts") or {}})
            note(f"  MinerU：命中指纹，跳过重解析（{len(pages)} 页）")
            return out

    tmp = os.path.join(adir, "_tmp")
    try:
        os.makedirs(adir, exist_ok=True)
        shutil_rm(tmp)
        os.makedirs(tmp, exist_ok=True)
        cmd = [exe, "parse", pdf, "-o", tmp, "--tier", tier, "--format", "zip"]
        note(f"  MinerU：{tier} 档解析中…（{os.path.basename(pdf)}）")
        run = runner or _run          # 与 probe 同一个口子：单测注入假 runner
        rc, text = run(cmd, timeout=timeout)
        zips = []
        for root, _dirs, files in os.walk(tmp):
            for f in files:
                if f.lower().endswith(".zip"):
                    zips.append(os.path.join(root, f))
        if not zips:
            out["why"] = (f"没拿到 zip 产物（退出码 {rc}）"
                          + ("；输出尾部：" + " / ".join(
                              (text or "").strip().splitlines()[-3:])
                             if text else ""))
            _write_meta(adir, {"key": key, "tier": tier, "ok": False,
                               "fingerprint": "", "why": out["why"],
                               "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "last_error_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                        keep_old=True)
            return out
        artifacts = _extract_zip(zips[0], adir)
        structured = {}
        try:
            with open(artifacts["structured"], encoding="utf-8") as fh:
                structured = json.load(fh)
        except Exception as exc:      # noqa: BLE001
            out["why"] = f"读 structured_content.json 失败：{exc}"
            return out
        pages = render_pages(structured)
        if not pages:
            out["why"] = "structured_content.json 里没有可用的页内容"
            return out
        with open(pages_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(pages, fh, ensure_ascii=False, indent=1)
        n_img = len(artifacts.get("images") or [])
        meta = {"key": key, "tier": tier, "ok": True, "cached_at": None,
                "fingerprint": fp,
                "pdf": os.path.abspath(pdf), "pdf_size": os.path.getsize(pdf),
                "exe": exe, "mineru": str(info.get("version") or ""),
                "n_pages": len(pages), "n_images": n_img,
                "seconds": round(time.time() - t0, 1),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "artifacts": artifacts}
        _write_meta(adir, meta)
        out.update({"ok": True, "pages": pages, "n_pages": len(pages),
                    "n_images": n_img,
                    "seconds": round(time.time() - t0, 1),
                    "artifacts": artifacts,
                    "why": f"{len(pages)} 页 / {n_img} 张图，"
                           f"耗时 {round(time.time() - t0, 1)} 秒"})
        note(f"  MinerU：解析完成，{len(pages)} 页、{n_img} 张图，"
             f"{round(time.time() - t0, 1)} 秒")
        return out
    except Exception as exc:      # noqa: BLE001
        out["why"] = f"{type(exc).__name__}: {exc}"
        _write_meta(adir, {"key": key, "tier": tier, "ok": False,
                           "fingerprint": "", "why": out["why"],
                           "at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                    keep_old=True)
        return out
    finally:
        try:
            shutil_rm(tmp)
        except Exception:      # noqa: BLE001
            pass


def _finish_from_zip(key: str, tier: str, pdf: str, fp: str, zip_path: str,
                     adir: str, seconds: float, exe: str, tool_version: str,
                     log=None) -> dict:
    """把一份 zip 落成一篇的产物（单篇与批量共用这一段出口）。

    为什么抽出来：单篇 `parse_item()` 与批量 `parse_many()` 的**产物形状必须一致**，
    否则"某些篇是批量解析的、某些是逐篇的"会冒出两套 meta/pages 格式。
    """
    out = {"ok": False, "key": key, "tier": tier, "pages": [], "why": "",
           "n_pages": 0, "n_images": 0, "fingerprint": fp,
           "source": f"mineru-{tier}", "seconds": round(seconds, 1),
           "artifacts": {}}
    try:
        artifacts = _extract_zip(zip_path, adir)
        structured = {}
        try:
            with open(artifacts["structured"], encoding="utf-8") as fh:
                structured = json.load(fh)
        except Exception as exc:      # noqa: BLE001
            out["why"] = f"读 structured_content.json 失败：{exc}"
            return out
        pages = render_pages(structured)
        if not pages:
            out["why"] = "structured_content.json 里没有可用的页内容"
            return out
        pages_path = os.path.join(adir, PAGE_JSON)
        with open(pages_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(pages, fh, ensure_ascii=False, indent=1)
        n_img = len(artifacts.get("images") or [])
        _write_meta(adir, {
            "key": key, "tier": tier, "ok": True, "fingerprint": fp,
            "pdf": os.path.abspath(pdf),
            "pdf_size": os.path.getsize(pdf) if os.path.isfile(pdf) else 0,
            "exe": exe, "mineru": tool_version,
            "n_pages": len(pages), "n_images": n_img,
            "seconds": round(seconds, 1),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "artifacts": artifacts,
        })
        out.update({"ok": True, "pages": pages, "n_pages": len(pages),
                    "n_images": n_img, "artifacts": artifacts,
                    "why": f"{len(pages)} 页 / {n_img} 张图"})
    except Exception as exc:      # noqa: BLE001
        out["why"] = f"{type(exc).__name__}: {exc}"
    if not out["ok"]:
        _write_meta(adir, {"key": key, "tier": tier, "ok": False,
                           "fingerprint": "", "why": out["why"],
                           "at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                    keep_old=True)
    return out


def parse_many(pairs, tier: str = "basic", kb_dir: str = "", force: bool = False,
               batch_size: int = 20, timeout_per_item: float = 600.0,
               runner=None, log=None, stream: bool = False) -> dict:
    """批量解析：**一次加载模型**，跑一批（目录输入），产物逐篇落盘。

    `pairs` = `[(key, pdf_path), …]`。返回：
      ok / n_ok / n_cached / n_failed / failed[(key, why)] / seconds / results{key: res}

    ⚠ 为什么值得单独一条路（本机实测）：
      每次 `mineru-kit parse` 都要重新加载模型（约 2 分钟）。逐篇跑 93 篇 ≈ 3 小时；
      目录批量 2 篇只要 14.5 秒。**面板「全库重解析」走的必须是这条**，
      否则用户会以为程序卡死了。
    """
    t0 = time.time()
    res = {"ok": True, "n_ok": 0, "n_cached": 0, "n_failed": 0,
           "failed": [], "seconds": 0.0, "results": {}, "why": ""}

    def note(msg: str):
        if log:
            try:
                log(msg)
            except Exception:      # noqa: BLE001
                pass

    info = probe(force=False)
    if not info.get("ok") or not info.get("exe"):
        res["ok"] = False
        res["why"] = "MinerU 不可用：" + (info.get("why") or "没装/没配好")
        return res
    exe = info["exe"]
    ver = str(info.get("version") or "")
    if (info.get("tier_ready") or {}).get(tier) is False:
        res["ok"] = False
        res["why"] = (f"{tier} 档的模型还没下全（跑一次安装向导，或 "
                      f"mineru-kit models download --tier {tier}）")
        return res

    todo: list[tuple[str, str, str]] = []      # (key, pdf, fingerprint)
    cached: list[str] = []
    for key, pdf in pairs:
        if not pdf or not os.path.isfile(pdf):
            res["n_failed"] += 1
            res["failed"].append((key, f"PDF 不存在：{pdf}"))
            continue
        fp = fingerprint(pdf, tier, exe, ver)
        meta = read_meta(key, kb_dir)
        adir = artifact_dir(key, kb_dir)
        if (not force and meta.get("fingerprint") == fp and meta.get("ok")
                and os.path.isfile(os.path.join(adir, PAGE_JSON))):
            cached.append(key)
            res["results"][key] = {"ok": True, "cached": True, "key": key,
                                   "tier": tier, "pages": load_pages(key, kb_dir),
                                   "source": f"mineru-{tier}", "why": "命中指纹"}
            continue
        todo.append((key, pdf, fp))
    res["n_cached"] = len(cached)
    if cached:
        note(f"批量解析：{len(cached)} 篇命中指纹、跳过；{len(todo)} 篇要解析")
    if not todo:
        res["seconds"] = round(time.time() - t0, 1)
        note(f"批量解析：没有需要解析的（全部命中指纹），{res['seconds']} 秒")
        return res

    for i in range(0, len(todo), max(1, batch_size)):
        chunk = todo[i:i + max(1, batch_size)]
        indir = os.path.join(_kb_dir(kb_dir), ART_ROOT, "_batch_in")
        outdir = os.path.join(_kb_dir(kb_dir), ART_ROOT, "_batch_out")
        shutil_rm(indir)
        shutil_rm(outdir)
        os.makedirs(indir, exist_ok=True)
        os.makedirs(outdir, exist_ok=True)
        links = 0
        for key, pdf, _fp in chunk:
            dst = os.path.join(indir, key + ".pdf")
            try:
                os.link(pdf, dst)          # 同盘硬链，零拷贝
                links += 1
            except Exception:      # noqa: BLE001
                try:
                    import shutil as _sh
                    _sh.copy2(pdf, dst)
                except Exception as exc:      # noqa: BLE001
                    res["n_failed"] += 1
                    res["failed"].append((key, f"复制 PDF 失败：{exc}"))
        note(f"批量解析：第 {i // max(1, batch_size) + 1} 批，{len(chunk)} 篇"
             f"（硬链 {links} 篇）—— 模型只加载这一次")
        b0 = time.time()
        cmd = [exe, "parse", indir, "-o", outdir, "--tier", tier,
               "--format", "zip"]
        timeout = max(600.0, timeout_per_item * len(chunk))
        if runner is not None:
            rc, text = runner(cmd, timeout=timeout)
        elif stream:
            rc, text = _run_stream(cmd, timeout=timeout, log=note)
        else:
            rc, text = _run(cmd, timeout=timeout)
        spent = time.time() - b0
        note(f"批量解析：这一批跑完，退出码 {rc}，耗时 {round(spent, 1)} 秒")

        got = set()
        for fn in (os.listdir(outdir) if os.path.isdir(outdir) else []):
            if not fn.lower().endswith(".zip"):
                continue
            key = fn[:-4]
            pdf = next((p for k, p, _f in chunk if k == key), "")
            fp = next((f for k, _p, f in chunk if k == key), "")
            adir = artifact_dir(key, kb_dir)
            one = _finish_from_zip(key, tier, pdf, fp, os.path.join(outdir, fn),
                                   adir, spent / max(1, len(chunk)), exe, ver,
                                   log=log)
            res["results"][key] = one
            if one["ok"]:
                res["n_ok"] += 1
                got.add(key)
                note(f"    ✓ {key}：{one['why']}")
            else:
                res["n_failed"] += 1
                res["failed"].append((key, one["why"]))
                note(f"    ✗ {key}：{one['why']}")
        for key, _pdf, _fp in chunk:
            if key in got or any(k == key for k, _w in res["failed"]):
                continue
            res["n_failed"] += 1
            msg = (f"这一批没有产出它的 zip（退出码 {rc}）"
                   if rc != 0 else "这一批没有产出它的 zip")
            res["failed"].append((key, msg))
            note(f"    ✗ {key}：{msg}")
        shutil_rm(indir)
        shutil_rm(outdir)
    res["seconds"] = round(time.time() - t0, 1)
    if res["n_failed"]:
        res["ok"] = res["n_ok"] > 0
    note(f"批量解析完成：成功 {res['n_ok']} 篇、命中指纹 {res['n_cached']} 篇、"
         f"失败 {res['n_failed']} 篇，共 {res['seconds']} 秒")
    return res


def _run_stream(args: list[str], timeout: float = 3600.0,
                log=None) -> tuple[int, str]:
    """跑子进程并**实时**把输出喂给 log（批量解析要几十分钟，不能憋着）。

    与 `_run` 的区别：`_run` 是 `subprocess.run(capture_output=True)` ——
    批量时用户会盯着日志看，几十秒没有任何输出就会以为卡死了。
    ⚠ 超时用 `communicate(timeout=)` 兜底 + 杀进程树（子进程还有孙进程）。
    """
    env = dict(os.environ)
    tail: list[str] = []
    p = None
    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace",
                             creationflags=_NO_WINDOW, env=env)
        deadline = time.time() + timeout
        for line in p.stdout:                      # type: ignore[union-attr]
            s = line.rstrip()
            tail.append(s)
            if len(tail) > 40:
                tail.pop(0)
            if log and s.strip():
                try:
                    log("    " + s[:200])
                except Exception:      # noqa: BLE001
                    pass
            if time.time() > deadline:
                raise subprocess.TimeoutExpired(args, timeout)
        return p.wait(), "\n".join(tail)
    except subprocess.TimeoutExpired:
        if p:
            try:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               capture_output=True, creationflags=_NO_WINDOW)
            except Exception:      # noqa: BLE001
                p.kill()
        return 124, "超时（>" + str(int(timeout)) + " 秒）：" + "\n".join(tail[-6:])
    except Exception as exc:      # noqa: BLE001
        return 126, f"{type(exc).__name__}: {exc}"


def shutil_rm(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _write_meta(adir: str, meta: dict, keep_old: bool = False) -> None:
    """写 meta.json。`keep_old=True` 时（失败路径）保留旧的指纹/页数信息。

    为什么要保留：一次失败**不该**让"已经解析好的产物"看起来没解析过 ——
    否则全库重跑时会反复重试同样的坏文件，也看不出"上次是好的、这次失败了"。
    """
    p = os.path.join(adir, META_JSON)
    try:
        os.makedirs(adir, exist_ok=True)
        old = {}
        if keep_old:
            try:
                with open(p, encoding="utf-8") as fh:
                    old = json.load(fh) or {}
            except Exception:      # noqa: BLE001
                old = {}
        merged = dict(old)
        merged.update(meta)
        if keep_old and old.get("ok"):
            merged["ok"] = True                 # 旧产物仍然可用
            merged["last_error"] = meta.get("why", "")
            merged["last_error_at"] = meta.get("at", "")
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(merged, fh, ensure_ascii=False, indent=1)
    except Exception:      # noqa: BLE001
        pass


def _extract_zip(zip_path: str, adir: str) -> dict:
    """把 zip 里的产物解到 `<kb>/mineru/<KEY>/`，返回各产物的绝对路径。

    ⚠ 手写解压而不是 `shutil.unpack_archive`：要**只挑我们认识的**、
      并防 zip-slip（条目名里带 `..` 就跳过）。产物名以 MinerU 4.0.10 为准，
      换个文件名时按后缀兜底（`*_content_list.json` 之类）。
    """
    import zipfile
    res = {"markdown": "", "structured": "", "middle": "", "images": [],
           "content_list": "", "dir": adir}
    # ⚠ 自己保证目录存在：`_extract_zip` 会被单测/别的入口直接调用，
    #   不能依赖调用方先 makedirs（本机实测：不建目录就 FileNotFoundError）。
    os.makedirs(adir, exist_ok=True)
    img_dir = os.path.join(adir, "images")
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            base = os.path.basename(name)
            if not base or ".." in name.replace("\\", "/").split("/"):
                continue
            low = base.lower()
            target = ""
            if low == "markdown.md":
                target = os.path.join(adir, "markdown.md")
            elif low == "structured_content.json":
                target = os.path.join(adir, "structured_content.json")
            elif low == "middle_json.json":
                target = os.path.join(adir, "middle_json.json")
            elif low.endswith("_content_list.json"):
                target = os.path.join(adir, "content_list.json")
            elif low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")):
                os.makedirs(img_dir, exist_ok=True)
                target = os.path.join(img_dir, base)
                res["images"].append(target)
            if not target:
                continue
            with zf.open(name) as src, open(target, "wb") as dst:
                dst.write(src.read())
            if low == "markdown.md":
                res["markdown"] = target
            elif low == "structured_content.json":
                res["structured"] = target
            elif low == "middle_json.json":
                res["middle"] = target
            elif low.endswith("_content_list.json"):
                res["content_list"] = target
    if not res["structured"]:
        # 没有 structured_content.json（老版本/别的格式）：退回 middle_json
        res["structured"] = res["middle"]
    return res


def fulltext_for(item, tier: str = "basic", kb_dir: str = "", force: bool = False,
                 timeout: float = PARSE_TIMEOUT, runner=None, log=None
                 ) -> tuple[list[str], str]:
    """**与 `zreader.fulltext_for` 同形**：返回 (pages, 来源标记)。

    `source` = `mineru-basic` / `mineru-standard`…（命中已有产物时加 `+cached`）。
    任何失败都返回 `([], "")` —— **回落是调用方的事**（convert 那边会退回
    Zotero 缓存 / PyMuPDF，并如实记录这一篇回落了）。
    """
    pdfs = list(getattr(item, "pdfs", None) or [])
    if not pdfs:
        return ([], "")
    pdf = ""
    for att in pdfs:
        p = getattr(att, "path", "") or ""
        if p and os.path.isfile(p):
            pdf = p
            break
    if not pdf:
        return ([], "")
    res = parse_item(item.key, pdf, tier=tier, kb_dir=kb_dir, force=force,
                     timeout=timeout, runner=runner, log=log)
    if not res.get("ok") or not res.get("pages"):
        if log:
            try:
                log(f"  [!!] MinerU 解析失败（{item.key}）：{res.get('why')}")
            except Exception:      # noqa: BLE001
                pass
        return ([], "")
    # ⚠ 不往来源里加 "+cached"：`items.fulltext_src` 记的是**文本从哪来**，
    #   "这次命中了指纹"是**本次运行**的统计（MANIFEST 的 `mineru_cached` 记它）。
    #   带后缀会让这一列永久留着 `mineru-basic+cached` 这种运行态噪声。
    return (list(res["pages"]), res["source"])


def status_for(key: str, kb_dir: str = "") -> dict:
    """一篇的解析状态（面板「PDF 解析」页用）：产物在不在 + 指纹有没有过期。"""
    meta = read_meta(key, kb_dir)
    adir = artifact_dir(key, kb_dir)
    fp_now = ""
    pdf = meta.get("pdf") or ""
    if pdf and os.path.isfile(pdf) and meta.get("tier"):
        fp_now = fingerprint(pdf, str(meta.get("tier")), meta.get("exe") or "",
                             str(meta.get("mineru") or ""))
    return {
        "key": key,
        "parsed": bool(meta.get("ok")) and os.path.isfile(
            os.path.join(adir, PAGE_JSON)),
        "tier": meta.get("tier") or "",
        "pages": int(meta.get("n_pages") or 0),
        "images": int(meta.get("n_images") or 0),
        "seconds": meta.get("seconds") or 0,
        "at": meta.get("at") or "",
        "pdf": pdf,
        "dir": adir,
        "stale": bool(fp_now and meta.get("fingerprint")
                      and fp_now != meta.get("fingerprint")),
        "last_error": meta.get("last_error") or (
            meta.get("why") if not meta.get("ok") else ""),
    }


def clear_artifacts(key: str = "", kb_dir: str = "") -> int:
    """删掉**解析产物**。`key` 留空 = 全删。返回删掉的篇数。

    ⚠ 名字**必须**与 `clear_cache()`（那个清探测结果内存缓存的）分清：
      本轮第一版就叫 `clear_cache()`，把探测那个覆盖了 —— 于是
      `test_mineru_probe.py` 里"清探测缓存"的调用把真实知识库的
      `<kb>/mineru/*` 整批删掉（全库重建跑到第 6 篇时产物全没了，本机实测）。
      Python 里后定义的赢、而且不会报任何错，所以这里连名字带护栏一起加固：
      **只删看起来像产物的目录**（里面有 meta.json 或 pages.json）。
    """
    root = os.path.join(_kb_dir(kb_dir), ART_ROOT)

    def is_artifact(d: str) -> bool:
        return (os.path.isfile(os.path.join(d, META_JSON))
                or os.path.isfile(os.path.join(d, PAGE_JSON)))

    n = 0
    if key:
        adir = artifact_dir(key, kb_dir)
        if os.path.isdir(adir) and is_artifact(adir):
            shutil_rm(adir)
            n = 1
        return n
    if os.path.isdir(root):
        for name in os.listdir(root):
            d = os.path.join(root, name)
            if os.path.isdir(d) and is_artifact(d):
                shutil_rm(d)
                n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    """`python offline/mineru.py …`

    子命令：
      （不带）            打印探测结果（排查用；原行为）
      parse --key K [--tier basic] [--force] [--pdf P]
                          解析一篇（产物落 <kb>/mineru/<K>/），并打印页数
      status [--key K]    看一篇/全部已解析的状态
      clear --key K|--all 删产物（函数名 clear_artifacts，见它的说明）
      pages --key K [--page N]   打印某页渲染后的文本（核对用）
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    def opt(name: str, default: str = "") -> str:
        for i, a in enumerate(argv):
            if a == name and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(name + "="):
                return a.split("=", 1)[1]
        return default

    cmd = argv[0] if argv and not argv[0].startswith("-") else ""
    force = "--force" in argv or "-f" in argv

    if cmd == "parse":
        key = opt("--key")
        pdf = opt("--pdf")
        tier = opt("--tier", "basic")
        kb_dir = opt("--kb", _kb_dir())
        if not pdf and key:
            # 从 Zotero 库里找这一篇的 PDF（够用：这是手动排查入口）
            try:
                import zreader
                r = zreader.ZoteroReader()        # ⚠ 类名是 ZoteroReader
                for it in r.load_items(r.top_level_items()):
                    if it.key == key:
                        pdf = next((p.path for p in it.pdfs
                                    if os.path.isfile(p.path)), "")
                        break
                r.close()
            except Exception as exc:      # noqa: BLE001
                print(f"[!!] 自己找 PDF 失败：{exc}（可用 --pdf 指定）")
        if not pdf:
            print("[XX] 需要 --pdf，或给一个库里有的 --key")
            return 2
        res = parse_item(key or os.path.splitext(os.path.basename(pdf))[0],
                         pdf, tier=tier, kb_dir=kb_dir, force=force,
                         log=lambda m: print(m, flush=True))
        print(json.dumps({k: v for k, v in res.items() if k != "pages"},
                         ensure_ascii=False, indent=2))
        if res.get("pages"):
            head = res["pages"][0][:200].replace("\n", " ")
            print(f"\n第 1 页开头：{head}")
        return 0 if res.get("ok") else 1

    if cmd == "status":
        key = opt("--key")
        if key:
            print(json.dumps(status_for(key, opt("--kb")), ensure_ascii=False,
                             indent=2))
            return 0
        root = os.path.join(_kb_dir(opt("--kb")), ART_ROOT)
        keys = sorted(os.listdir(root)) if os.path.isdir(root) else []
        done = 0
        for k in keys:
            st = status_for(k, opt("--kb"))
            done += 1 if st["parsed"] else 0
            print(f"  {k}  {'✓' if st['parsed'] else '✗'}  {st['tier'] or '-'}"
                  f"  {st['pages']} 页  {st['images']} 图  {st['seconds']}s"
                  f"{'  ⚠ 指纹过期' if st['stale'] else ''}"
                  f"{'  错误：' + st['last_error'][:60] if st['last_error'] else ''}")
        print(f"共 {len(keys)} 篇有产物，其中 {done} 篇可用")
        return 0

    if cmd == "clear":
        if "--all" in argv:
            print(f"删掉 {clear_artifacts('', opt('--kb'))} 篇的产物")
            return 0
        key = opt("--key")
        if not key:
            print("[XX] 需要 --key K 或 --all")
            return 2
        print(f"删掉 {clear_artifacts(key, opt('--kb'))} 篇的产物")
        return 0

    if cmd == "pages":
        key = opt("--key")
        pages = load_pages(key, opt("--kb"))
        if not pages:
            print("[XX] 没有已渲染的 pages.json（先 parse）")
            return 1
        want = opt("--page")
        if want.isdigit():
            i = max(0, min(len(pages) - 1, int(want) - 1))
            print(f"—— 第 {i + 1}/{len(pages)} 页 ——\n{pages[i]}")
        else:
            print(f"共 {len(pages)} 页；每页字符数："
                  f"{[len(p) for p in pages]}")
        return 0

    # 默认：探测（保留原行为）
    exe = opt("--exe")
    info = probe(exe, force=force)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if info.get("ok"):
        print("\n" + summary_line(info))
    return 0 if info.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
