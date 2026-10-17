"""公式就地纠错：**区域/点击位置 → 定位到那一条 $$…$$ → 回写修订层**。

## 不做识别，只做搬运

识别由**人**完成：对话框里手写 LaTeX，或点「用 MathType 编辑」走剪贴板往返
（MathType 的 Cut and Copy Preferences 设成 TeX -- LaTeX 后，复制出来就是 LaTeX）。
本模块只负责三件事：
  1. 把用户选中的区域对应到某一条公式（**定位**）；
  2. 把新 LaTeX 写回**该页**的修订层（**回写**由引擎走既有 doc.save）；
  3. 裁一块预览图给对话框看。

## 坐标口径（2026-10-12 真机实测定死，别再猜）

  · structured_content.json 里 equation 块的 **bbox = 归一化、左上原点**（0~1）；
  · Zotero 的 params.position / 标注 position 的 **rects = PDF 用户空间点、左下原点**
    （pageIndex 0 基；实测 50 条高亮，按左下翻转 Y 后与 PyMuPDF 原文平均字符重合 0.942，
     不翻转只有 0.224）。

两套原点相反。换算：nx = x / W；ny0 = (2*my0 + mh - y1) / mh；ny1 = (2*my0 + mh - y0) / mh
（my0/mh = 页 mediabox 的原点与高；本机样本 my0=0）。
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile

import edits as ED          # noqa: E402 —— 修订层（唯一写路径的落点）
import mineru as M          # noqa: E402

# 与 render_block 保持一致：这些块类型会被渲染成 $$…$$。
EQ_TYPES = ("equation", "interline_equation", "inline_equation")

RE_DISPLAY = re.compile(r"\$\$(.*?)\$\$", re.S)
RE_TAG = re.compile(r"\\tag\s*\{[^}]*\}")

# MathType 的落点（安装器默认装到 x86 目录；64 位也有）。
# ⚠ 2026-10-16 起真正找它的是 `schemas.resolve_mathtype()`（注册表 App Paths →
#   卸载信息 InstallLocation → Program Files(x86)/Program Files → 这两条历史落点），
#   与其它运行环境项共用一套优先级，并且**可以在管理面板「运行环境」页里手填**。
#   这里保留这两个常量只为"解析器自己崩了"时兜底，别再往这里加路径。
MATHTYPE_CANDIDATES = (
    r"C:\Program Files (x86)\MathType\MathType.exe",
    r"C:\Program Files\MathType\MathType.exe",
)


# ---------------------------------------------------------------- 坐标换算（纯函数）


def norm_from_pdf_rect(rect, mh: float, my0: float = 0.0,
                       mw: float = 0.0, mx0: float = 0.0):
    """Zotero 的左下原点 rect → MinerU 的左上原点归一化 rect。

    rect = [x0, y0, x1, y1]（PDF 点）。mw/mx0 只影响 x 的归一化。
    """
    x0, y0, x1, y1 = (float(v) for v in rect)
    mh = float(mh) or 1.0
    nx0 = (x0 - mx0) / (float(mw) or 1.0)
    nx1 = (x1 - mx0) / (float(mw) or 1.0)
    ny0 = (2.0 * my0 + mh - y1) / mh
    ny1 = (2.0 * my0 + mh - y0) / mh
    return [min(nx0, nx1), min(ny0, ny1), max(nx0, nx1), max(ny0, ny1)]


def rect_center(rect):
    return ((rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0)


def rect_iou(a, b) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    return inter / (area_a + area_b - inter) if (area_a + area_b - inter) else 0.0


def point_in_rect(p, rect) -> bool:
    return rect[0] - 1e-9 <= p[0] <= rect[2] + 1e-9 and \
        rect[1] - 1e-9 <= p[1] <= rect[3] + 1e-9


def match_block(rect, blocks, max_dist: float = 0.06):
    """在候选块里挑最匹配的一个（点命中优先，否则取最近且不出阈值）。

    blocks 里每项要有 "bbox"（归一化、左上原点）。返回 (best, scored)；
    best 为 None 表示没匹配上（调用方应让用户从该页公式列表里挑）。
    """
    cx, cy = rect_center(rect)
    diag = ((rect[2] - rect[0]) ** 2 + (rect[3] - rect[1]) ** 2) ** 0.5 or 1e-9
    scored = []
    for b in blocks:
        bb = b.get("bbox") or []
        if len(bb) != 4:
            continue
        bb = [float(v) for v in bb]
        bc = rect_center(bb)
        inside = point_in_rect((cx, cy), bb)
        dist = ((cx - bc[0]) ** 2 + (cy - bc[1]) ** 2) ** 0.5
        if inside:
            score = 1.0 + rect_iou(rect, bb)
        elif dist <= max_dist:
            score = 1.0 / (1.0 + dist * 10.0)
        else:
            continue
        scored.append((score, b))
    scored.sort(key=lambda x: -x[0])
    return (scored[0][1] if scored else None), scored


# ---------------------------------------------------------------- 页/块读取


def page_info(key: str, page_index: int, kb_dir: str = "") -> dict:
    """取一页的：有效正文、公式块（按渲染顺序）、以及页面尺寸。"""
    pages = M.load_pages(key, kb_dir)
    if not pages:
        return {"ok": False, "why": "这一篇没有逐页正文（没建过索引？）"}
    if page_index < 0 or page_index >= len(pages):
        return {"ok": False, "why": "页码越界（0..%d）" % (len(pages) - 1)}
    machine = ED.apply(key, pages, kb_dir=kb_dir)
    text = machine[page_index]
    blocks = []
    struct = M.load_structured(key, kb_dir)
    for pg in (struct.get("pages") or []):
        if int(pg.get("page_idx") if pg.get("page_idx") is not None else -1) != page_index:
            continue
        for bi, b in enumerate(pg.get("blocks") or []):
            if M.block_type(b) not in EQ_TYPES:
                continue
            blocks.append({
                "block_index": bi,
                "eq_ordinal": len(blocks),          # 该页第几条公式（0 基）
                "type": M.block_type(b),
                "bbox": [float(v) for v in (b.get("bbox") or [])][:4],
                "latex": str(b.get("content") or ""),
                "image_source": str(b.get("image_source") or ""),
            })
        break
    return {"ok": True, "text": text, "blocks": blocks, "n_pages": len(pages)}


def page_size(key: str, page_index: int, kb_dir: str = ""):
    """从 PDF 拿该页 (W, H, my0)。拿不到返回 None。"""
    meta = M.read_meta(key, kb_dir)
    pdf = str(meta.get("pdf") or "")
    if not pdf or not os.path.exists(pdf):
        return None
    try:
        import pymupdf as fitz
        doc = fitz.open(pdf)
        if page_index >= doc.page_count:
            doc.close()
            return None
        pg = doc[page_index]
        mb = pg.mediabox
        out = (float(mb.width), float(mb.height), float(mb.y0), float(pg.rect.width))
        doc.close()
        return out
    except Exception:      # noqa: BLE001
        return None


def find_formula(key: str, page_index: int, rect=None, kb_dir: str = "",
                 max_dist: float = 0.06) -> dict:
    """定位：区域 rect（左下原点 PDF 点）→ 该页最匹配的公式块。"""
    info = page_info(key, page_index, kb_dir)
    if not info.get("ok"):
        return info
    blocks = info["blocks"]
    if not blocks:
        return {"ok": True, "matched": None, "why": "这一页没有公式块",
                "page_text": info["text"], "blocks": [], "candidates": [],
                "n_pages": info.get("n_pages")}
    matched = None
    scored = []
    if rect:
        size = page_size(key, page_index, kb_dir)
        if size:
            W, H, my0, _ = size
            nr = norm_from_pdf_rect(rect, H, my0, W, 0.0)
            matched, scored = match_block(nr, blocks, max_dist=max_dist)
    if matched is None and rect is None and len(blocks) == 1:
        matched = blocks[0]
    return {
        "ok": True,
        "matched": matched,
        "candidates": [b for _, b in scored] if scored else list(blocks),
        "blocks": blocks,
        "page_text": info["text"],
        "n_pages": info["n_pages"],
    }


# ---------------------------------------------------------------- 回写（纯函数）


def replace_nth_formula(page_text: str, ordinal: int, new_latex: str,
                        old_latex: str = "") -> str:
    """把该页第 ordinal 条 $$…$$ 换成新 LaTeX（保留原有 \\tag 编号）。

    · 优先按 old_latex 内容定位（用户改过别处时更稳），匹配不到再退回序号；
    · 新 LaTeX 自己没写 \\tag 而旧的有 → 沿用旧的编号，免得式号丢掉。
    """
    spans = list(RE_DISPLAY.finditer(page_text or ""))
    if not spans:
        raise ValueError("这一页没有 $$…$$ 公式")
    idx = None
    if old_latex:
        for i, m in enumerate(spans):
            if m.group(1).strip() == old_latex.strip():
                idx = i
                break
    if idx is None:
        if ordinal < 0 or ordinal >= len(spans):
            raise ValueError("公式序号越界（这一页有 %d 条）" % len(spans))
        idx = ordinal
    old = spans[idx].group(1)
    body = str(new_latex or "").strip()
    if not RE_TAG.search(body):
        m = RE_TAG.search(old)
        if m:
            body = body + " " + m.group(0)
    return page_text[:spans[idx].start()] + "$$" + body + "$$" + page_text[spans[idx].end():]


# ---------------------------------------------------------------- 预览裁剪


def preview_png_b64(key: str, page_index: int, block=None, rect=None,
                    kb_dir: str = "", zoom: float = 3.0) -> str:
    """给对话框看的小图（base64 PNG，带 data: 前缀）。

    优先用 MinerU 自己存的公式裁剪图（就是那一块，最准）；没有才按 rect 从 PDF 裁。
    """
    data = None
    if block and block.get("image_source"):
        p = os.path.join(M.artifact_dir(key, kb_dir), block["image_source"])
        if os.path.exists(p):
            data = open(p, "rb").read()
    if data is None:
        size = page_size(key, page_index, kb_dir)
        meta = M.read_meta(key, kb_dir)
        pdf = str(meta.get("pdf") or "")
        if size and pdf and os.path.exists(pdf):
            try:
                import pymupdf as fitz
                W, H, my0, _ = size
                doc = fitz.open(pdf)
                pg = doc[page_index]
                if rect:
                    nr = norm_from_pdf_rect(rect, H, my0, W, 0.0)
                    box = fitz.Rect(nr[0] * W, nr[1] * H, nr[2] * W, nr[3] * H)
                elif block and len(block.get("bbox") or []) == 4:
                    bb = block["bbox"]
                    box = fitz.Rect(bb[0] * W, bb[1] * H, bb[2] * W, bb[3] * H)
                else:
                    doc.close()
                    return ""
                pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=box)
                data = pix.tobytes("png")
                doc.close()
            except Exception:      # noqa: BLE001
                data = None
    if not data:
        return ""
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------- MathType


def mathtype_state() -> dict:
    """MathType 在不在（**没装也不要隐藏按钮，只置灰**）。

    路径与其它运行环境项走**同一套解析器**（`schemas.resolve_mathtype`）：
    调用方指定 > 环境变量 `MATHTYPE_EXE` > 面板设置（管理面板「运行环境」页）>
    自动探测（注册表 → 安装目录 → 常见落点）> 救急 `.env` > 空。
    返回形状不变：`{installed, exe}`；`exe` 是**解析出来的值**（用户手填了不存在的
    路径时它非空、`installed` 为 False —— 调用方据此区分"没装"与"填错了"）。
    """
    exe = ""
    try:
        import schemas as _S
        exe = str(_S.resolve_mathtype() or "")
    except Exception:      # noqa: BLE001 —— 解析器出问题不该让整个公式功能不可用
        exe = ""
    if not exe:
        # 解析器给不出（自己崩了/依赖缺失）：退回两个历史落点看一眼
        for cand in MATHTYPE_CANDIDATES:
            if os.path.exists(cand):
                exe = cand
                break
    installed = bool(exe) and os.path.exists(exe)
    # ⚠ exe 原样返回（哪怕它不存在）：用户手填了一个不存在的路径时，
    #   调用方要能说出"你填的那个找不到"，而不是笼统的"本机没装"。
    return {"installed": installed, "exe": exe}


def _to_clipboard(text: str) -> bool:
    """把一段文本放进 Windows 剪贴板。

    为什么不用 clip.exe：它按**控制台代码页**解 stdin，LaTeX 里出现非 ASCII
    （中文式号、Unicode 符号）就会被写坏。改成写临时文件 + PowerShell 的
    Set-Clipboard（PS 5.1 自带，按 UTF-8 读文件），中文也是对的。
    """
    path = os.path.join(tempfile.gettempdir(), "zotero-kb-formula-clip.txt")
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except OSError:
        return False
    try:
        cmd = ("Set-Clipboard -Value (Get-Content -Raw -Encoding UTF8 "
               "-LiteralPath '" + path.replace("'", "''") + "')")
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", cmd],
            capture_output=True, timeout=20)
        return ps.returncode == 0
    except Exception:      # noqa: BLE001
        return False


def start_mathtype(latex: str = "", dry_run: bool = False) -> dict:
    """剪贴板往返的第一步：把 LaTeX 放进剪贴板，并启动 MathType。

    dry_run=True：只做检测与写剪贴板，**不启动** MathType（自检用，免得弹窗）。

    为什么放在**服务端**：插件沙箱里没有稳定的「启动某个 exe / 写剪贴板」能力，
    而 Python 这边两句就完事，还能被自检打到。
    返回 {ok, installed, exe, copied, launched, why}。
    """
    st = mathtype_state()
    out = {"ok": True, "installed": st["installed"], "exe": st["exe"],
           "copied": False, "launched": False, "why": []}
    if latex:
        out["copied"] = _to_clipboard(latex)
        if not out["copied"]:
            out["why"].append("把 LaTeX 放进剪贴板失败（可以手动复制）")
    if not st["installed"]:
        out["ok"] = False
        # 区分"填错了"与"没装"：用户手填的路径（source 是面板/环境变量/.env）
        # 指到不存在的文件时，笼统说"本机没装"会让人去重装 MathType，
        # 而真正该做的是去「运行环境」页把那个路径改对。
        if st["exe"]:
            out["why"].append("MathType 路径不存在：" + st["exe"]
                              + "（到管理面板「运行环境」页改，或点「恢复自动」）")
        else:
            out["why"].append("本机没装 MathType（可选组件；不装也能手改 LaTeX）")
        out["why"] = "；".join(out["why"])
        return out
    if dry_run:
        out["why"] = "；".join(out["why"])
        return out
    startfile = getattr(os, "startfile", None)
    if startfile is None:      # 非 Windows（CI）
        out["ok"] = False
        out["why"].append("这个平台没有 os.startfile")
        out["why"] = "；".join(out["why"])
        return out
    try:
        startfile(st["exe"])
        out["launched"] = True
    except Exception as exc:      # noqa: BLE001
        out["ok"] = False
        out["why"].append("启动 MathType 失败：%s" % exc)
    out["why"] = "；".join(out["why"])
    return out
