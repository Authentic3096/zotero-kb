"""online/docpage.py —— 对照页（左 PDF 页 / 右修订后正文，可直接改）的后端。

为什么单独一个模块，而不是塞进 localserver.py：

  · localserver.py 已经三千多行，对照页是**自成一套**的东西（模板 + 页图渲染 +
    图片改写），摊在里面会把它淹掉；
  · 这里**不碰任何写路径** —— 唯一写盘是离线修订层 offline/edits.py，
    对照页只调既有的 POST /doc-edit（派发包 §一 / §六）。

三条纪律（派发包 §一 / §二）：

  ① 一个字都不改 md。右栏吃的是派生物：fulltext/<key>.md 的同源渲染
     （offline/mdhtml.render），编辑走既有端点 /doc-edit；
  ② 页级锚点现成：逐页粒度就是 <kb>/mineru/<key>/pages.json 的页码；
  ③ 不新增知识库目录：页图缓存放 <kb>/.cache/doc-pages/（.cache 已登记为可删）。

数据来源（"只认登记过的 PDF"）：

  · 入口 key 必须在 <kb>/index.db 的 items 表里（没登记 → 404），
    绝不做成"给个 key 就读盘"；
  · PDF 绝对路径取自 <kb>/mineru/<key>/meta.json 的 pdf 字段（构建时写下的
    登记簿）；现场去 Zotero 库查要拷整库快照，每翻一页拷一次库不可接受。

⚠ 这里所有的字符串都不带反引号：本模块的注释会被本项目的工具链反复读写，
  用「」与引号代替 markdown 反引号，省得再出转义问题。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "offline"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import schemas as S  # noqa: E402

DOC_PAGE_DEFAULT_W = 900          # 页图默认渲染宽度（CSS 像素）
DOC_PAGE_MIN_W = 300
DOC_PAGE_MAX_W = 2400
DOC_PAGE_EXT = ".png"
DOC_HTML_MAX_BYTES = 6 * 1024 * 1024   # 单页渲染上限，超了就只回错误、不回内容

# 模板按 mtime 缓存：改完 docpage.html 不用重启服务（本项目一贯口径）
_TPL_CACHE: dict = {"mtime": 0.0, "text": ""}
# 页图渲染用的 pymupdf 句柄缓存：同一篇连续翻页时别每页重开一次 PDF。
# ⚠ 不用 fitz（已 deprecated），用 pymupdf（本机 1.28.2 实测）。
_PDF_CACHE: dict = {"path": "", "mtime": 0.0, "doc": None}
# 服务是 ThreadingHTTPServer：两个页图请求（两个标签页 / 页面并发取图）撞在一起时，
# "换 PDF 就关掉上一个句柄"会和"另一个线程正在用那个句柄渲染"打架。整段渲染串行化，
# 单张页图 <200ms，代价可以忽略。
_PDF_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """日志走 localserver.log 的同一个 log()（延迟导入，避免循环依赖）。"""
    try:
        import localserver
        localserver.log("对照页：" + str(msg))
    except Exception:      # noqa: BLE001
        pass


# ---------------------------------------------------------------- 登记与取数


def item(key: str) -> dict | None:
    """登记过的条目（index.db 的 items 表）→ 元数据 dict；没有就是 None。"""
    key = str(key or "").strip()
    if not key:
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(S.INDEX_DB)
        try:
            row = conn.execute(
                "SELECT title, first_author, year FROM items WHERE key=?",
                (key,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return {"key": key, "title": str(row[0] or ""),
                "first_author": str(row[1] or ""), "year": str(row[2] or "")}
    except Exception:      # noqa: BLE001 —— 库不在 / 表不在都当作"没登记"
        return None


def pages(key: str) -> list:
    """逐页**机器稿**（mineru/pages.json）。没有就返回 []。"""
    import mineru as M
    return list(M.load_pages(key) or [])


def effective(key: str, machine: list) -> list:
    """有效正文 = 修订层按页覆盖机器稿（设计方案附录 J.1）。"""
    import edits as ED
    return list(ED.apply(key, machine))


def meta(key: str, machine: list) -> dict:
    """修订层状态：改过哪几页、哪几页「基于旧解析」。"""
    import edits as ED
    return ED.meta(key, machine)


def pdf_path(key: str) -> str:
    """这一篇登记在案的 PDF 绝对路径；对不上磁盘时按"没有"处理。"""
    import mineru as M
    meta_json = M.read_meta(key)
    pdf = str(meta_json.get("pdf") or "")
    if pdf and os.path.isfile(pdf):
        return pdf
    return ""


def pdf_sig(pdf: str) -> str:
    """PDF 的 mtime + 大小指纹（页图缓存键必须含它，换 PDF 要自动失效）。"""
    try:
        st = os.stat(pdf)
        raw = "%d|%d" % (st.st_size, st.st_mtime_ns)
    except OSError:
        raw = "0|0"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def clamp_w(raw) -> int:
    try:
        w = int(float(raw))
    except (TypeError, ValueError):
        return DOC_PAGE_DEFAULT_W
    return max(DOC_PAGE_MIN_W, min(DOC_PAGE_MAX_W, w))


# ---------------------------------------------------------------- 页图


def _open_pdf(path: str):
    """打开并缓存一个 PDF 句柄（同一篇连续翻页只开一次）。"""
    try:
        st = os.stat(path)
        mtime = st.st_mtime_ns
    except OSError:
        return None
    cached = _PDF_CACHE.get("doc")
    if cached is not None and _PDF_CACHE.get("path") == path \
            and _PDF_CACHE.get("mtime") == mtime:
        return cached
    try:
        import pymupdf
    except Exception as exc:      # noqa: BLE001
        _log("pymupdf 不可用（%s）" % exc)
        return None
    try:
        if cached is not None:
            try:
                cached.close()
            except Exception:      # noqa: BLE001
                pass
        doc = pymupdf.open(path)
    except Exception as exc:      # noqa: BLE001
        _log("打不开 PDF %s（%s）" % (path, exc))
        return None
    _PDF_CACHE.update({"path": path, "mtime": mtime, "doc": doc})
    return doc


def png(key: str, page: int, width: int) -> tuple:
    """PDF 第 page 页 → PNG 字节。返回 (data, error)。

    error 取值：""(成功) / "no-pdf" / "range" / "render"。
    """
    path = pdf_path(key)
    if not path:
        return b"", "no-pdf"
    width = clamp_w(width)
    page = int(page)
    sig = pdf_sig(path)
    cache_dir = os.path.join(S.CACHE_DIR, "doc-pages", str(key))
    dst = os.path.join(cache_dir, "%d@%d@%s%s" % (page, width, sig, DOC_PAGE_EXT))
    if os.path.isfile(dst):
        try:
            with open(dst, "rb") as fh:
                data = fh.read()
            if data:
                return data, ""
        except OSError:
            pass
    try:
        import pymupdf
    except Exception as exc:      # noqa: BLE001
        _log("pymupdf 不可用（%s）" % exc)
        return b"", "render"
    with _PDF_LOCK:
        doc = _open_pdf(path)
        if doc is None:
            return b"", "render"
        try:
            if page < 1 or page > doc.page_count:
                return b"", "range"
            pg = doc.load_page(page - 1)
            zoom = width / max(1.0, float(pg.rect.width))
            zoom = max(0.2, min(8.0, zoom))
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            data = pix.tobytes("png")
        except Exception as exc:      # noqa: BLE001
            _log("渲染第 %d 页失败（%s）" % (page, exc))
            return b"", "render"
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = dst + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dst)
    except OSError:
        pass                      # 缓存写不进去不影响这次渲染
    return data, ""


# ---------------------------------------------------------------- 图片


_DOC_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")


def image_path(key: str, name: str) -> str:
    """<kb>/mineru/<key>/ 下的一张图（**只允许图片、且不许跑出这个目录**）。

    为什么不让页面直接读 file://：浏览器里 http 页面加载 file:// 会被安全策略
    拦掉（插件侧栏那边换 resource:// 是同一类问题）。所以图片走这个端点。
    """
    import mineru as M
    base = os.path.normpath(M.artifact_dir(key))
    if not base or not os.path.isdir(base):
        return ""
    rel = str(name or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return ""
    full = os.path.normpath(os.path.join(base, *rel.split("/")))
    try:
        if os.path.commonpath([base, full]) != base:
            return ""
    except ValueError:
        return ""
    if not os.path.isfile(full):
        return ""
    if os.path.splitext(full)[1].lower() not in _DOC_IMG_EXT:
        return ""
    return full


def rewrite_imgs(frag: str, key: str, token: str) -> str:
    """把片段里**相对**的 img src 改写成 /kb-image?...&token=...

    知识库文件里图片一律是 images/xxx.jpg 这种相对路径（相对 mineru/<key>/），
    页面是 http 来的，直接加载不了 —— 这里按派发包 §一 的「渲染时解析路径」。
    """
    def rep(m):
        src = m.group(1)
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", src) or src.startswith("//"):
            return m.group(0)     # 已经是绝对 URL / data: → 不动
        url = "/kb-image?key=%s&name=%s&token=%s" % (
            _quote(key), _quote(src), _quote(token))
        return 'src="%s"' % url
    return re.sub(r'src="([^"]*)"', rep, frag)


def _quote(text) -> str:
    from urllib.parse import quote
    return quote(str(text), safe="")


# ---------------------------------------------------------------- 渲染


def render_page(key: str, page: int, machine: list, token: str) -> dict:
    """把**有效正文**第 page 页渲成 HTML 片段（图片改成 /kb-image 的绝对 URL）。

    这是「右栏」显示用的那一半；编辑框要的是 /doc-raw 的**原始 markdown**，
    两者绝不能混（装渲染后的 HTML 存回去会把 md 污染，见派发包 §二）。
    """
    import mdhtml
    eff = effective(key, machine)
    page = int(page)
    if page < 1 or page > len(eff):
        return {"html": "", "empty": True, "bytes": 0}
    text = str(eff[page - 1] or "")
    if not text.strip():
        return {"html": "", "empty": True, "bytes": 0}
    try:
        frag = mdhtml.render(text)
    except Exception as exc:      # noqa: BLE001
        _log("渲染第 %d 页失败（%s: %s）" % (page, type(exc).__name__, exc))
        return {"html": "", "empty": True, "bytes": 0,
                "error": "渲染失败：%s: %s" % (type(exc).__name__, exc)}
    frag = rewrite_imgs(frag, key, token)
    n = len(frag.encode("utf-8"))
    if n > DOC_HTML_MAX_BYTES:
        return {"html": "", "empty": True, "bytes": n,
                "error": "这一页渲染后超过 6MB，已跳过（请报给维护者）"}
    return {"html": frag, "empty": False, "bytes": n}


# ---------------------------------------------------------------- 模板


def template() -> str:
    """对照页的 HTML 模板（本目录的 docpage.html）。改完不用重启服务。"""
    path = os.path.join(_HERE, "docpage.html")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    if _TPL_CACHE.get("mtime") == mtime and _TPL_CACHE.get("text"):
        return str(_TPL_CACHE["text"])
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return ""
    _TPL_CACHE["mtime"] = mtime
    _TPL_CACHE["text"] = text
    return text


def html(key: str, page: int, token: str) -> str:
    """整页 HTML（自带 CSS/JS）——GET /doc 的响应体。"""
    tpl = template()
    if not tpl:
        return ""
    machine = pages(key)
    info = item(key) or {}
    cfg = {
        "key": key, "page": int(page), "pages": len(machine),
        "token": str(token), "title": str(info.get("title") or ""),
        "firstAuthor": str(info.get("first_author") or ""),
        "year": str(info.get("year") or ""),
        "hasPdf": bool(pdf_path(key)),
        "width": DOC_PAGE_DEFAULT_W,
    }
    # ⚠ 注入的 JSON 里可能出现 </script>（标题里带尖括号时）—— 转义掉斜杠，
    #   否则脚本标签会被提前闭合，页面直接白屏。
    payload = json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/")
    return tpl.replace("__KB_DOC_CONFIG__", payload)
