"""offline/edits.py —— 正文**人工修订层**（唯一读写路径）。

## 为什么有这一层

用户要「侧边栏看正文并渲染图表 → 对照原文 PDF 发现 MinerU 解析错的地方就地改 →
切片与向量基于用户修正过的全文，但**不能覆盖 MinerU 原版**」（2026-10-11 口径，
设计方案附录 J.1）。

    <kb>/mineru/<key>/structured_content.json   原版解析产物（只读，永不覆盖）
            | mineru.render_pages()
            v
       pages.json（机器稿，逐页 markdown）
            |  + <kb>/edits/<key>.md（人工修订层：**只存被改过的页**）
            v
       有效正文 = edits.apply(key, pages)

修订层只要接在 convert.py 的「正文来源」之后，下游三处（fulltext 落盘 /
collect_chunks / item.fulltext_chars）一起生效 —— 检索、溯源、视图都不用知道
有修订层这回事。

## 磁盘格式

    <kb>/edits/<key>.md     与 fulltext 同形的 ## p.N 分节，**只含被改过的页**
    <kb>/edits/<key>.json   边车 {version, key, pages: {3: {base, at}}}

base 是该页**机器稿**文本的 sha1 前 16 位（不是上一次修订稿的 hash）。合并时
机器文本 hash != base => **仍然用用户的版本**，但 meta() 会把它标成「基于旧解析」
—— 静默丢掉用户的修正比提示冲突糟得多。

## 纪律

· 所有写盘都走 _atomic_write（先写 .tmp 再 os.replace），窗格不会读到半截文件；
· **只存改过的页**：没出现的页 = 用机器稿，MinerU 重解析后照吃新解析；
· 本模块是修订层**唯一**的读写实现，别处不许自己读写 edits/ 下的文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import schemas as S  # noqa: E402

VERSION = 1
RE_PAGE = re.compile(r"^##\s*p\.(\d+)\s*$")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def edits_dir(kb_dir: str = "") -> str:
    """修订层目录。默认 <知识库>/edits/。"""
    if kb_dir:
        return os.path.join(str(kb_dir), "edits")
    return os.path.join(S.KB_DIR, "edits")


def md_path(key: str, kb_dir: str = "") -> str:
    return os.path.join(edits_dir(kb_dir), f"{key}.md")


def json_path(key: str, kb_dir: str = "") -> str:
    return os.path.join(edits_dir(kb_dir), f"{key}.json")


def draft_path(key: str, kb_dir: str = "") -> str:
    return os.path.join(edits_dir(kb_dir), f"{key}.draft.md")


def page_hash(text: str) -> str:
    """一页文本的短 hash（用于判「这一页的机器稿还是不是当初那一版」）。"""
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:16]


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def parse_md(text: str) -> dict:
    """把 ## p.N 分节解析成 {页码(int): 正文}。没有分节就返回空。"""
    out = {}
    cur = None
    buf = []
    for line in str(text or "").splitlines():
        m = RE_PAGE.match(line)
        if m:
            if cur is not None:
                out[cur] = "\n".join(buf).strip("\n")
            cur = int(m.group(1))
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip("\n")
    return out


def render_md(pages: dict) -> str:
    """{页码: 正文} → 与 fulltext 同形的分节 markdown。"""
    parts = []
    for n in sorted(pages):
        parts.append(("## p.%d\n%s" % (n, pages[n])).rstrip() + "\n")
    return "\n".join(parts)


def load(key: str, kb_dir: str = "") -> dict:
    """读修订的页（没有文件 / 读坏了都返回 {}）。"""
    try:
        with open(md_path(key, kb_dir), encoding="utf-8") as fh:
            return parse_md(fh.read())
    except OSError:
        return {}


def load_meta(key: str, kb_dir: str = "") -> dict:
    """读边车 json（没有/坏了返回 {}）。"""
    try:
        with open(json_path(key, kb_dir), encoding="utf-8") as fh:
            doc = json.load(fh) or {}
        return doc if isinstance(doc, dict) else {}
    except Exception:      # noqa: BLE001
        return {}


def _save(key: str, pages: dict, pages_meta: dict, kb_dir: str = "") -> None:
    """把修订页与边车一起原子写盘；页全空就删文件（回到机器稿）。"""
    if not pages:
        remove(key, kb_dir)
        return
    _atomic_write(md_path(key, kb_dir), render_md(pages))
    doc = {"version": VERSION, "key": str(key),
           "pages": {str(k): pages_meta.get(str(k), pages_meta.get(k, {}))
                     for k in sorted(pages)}}
    _atomic_write(json_path(key, kb_dir),
                  json.dumps(doc, ensure_ascii=False, indent=1) + "\n")


def remove(key: str, kb_dir: str = "") -> bool:
    """删掉这一篇的修订文件（回到机器稿）。返回是否删到了东西。"""
    hit = False
    for p in (md_path(key, kb_dir), json_path(key, kb_dir)):
        try:
            os.remove(p)
            hit = True
        except OSError:
            pass
    return hit


def has_edits(key: str, kb_dir: str = "") -> bool:
    return bool(load(key, kb_dir))


def all_keys(kb_dir: str = "") -> list:
    """有修订的 key（面板/维护用）。"""
    d = edits_dir(kb_dir)
    if not os.path.isdir(d):
        return []
    return sorted(f[:-3] for f in os.listdir(d)
                  if f.endswith(".md") and not f.endswith(".draft.md"))


def apply(key: str, pages: list, kb_dir: str = "") -> list:
    """**有效正文** = 修订层按页覆盖机器稿。没有修订就原样返回。

    这是下游唯一该调用的入口：convert.py 在「正文来源」之后插一行，
    fulltext / 切片 / 向量 / fulltext_chars 就一起吃到修订。
    """
    ov = load(key, kb_dir)
    machine = list(pages or [])
    if not ov:
        return machine
    out = []
    for i, text in enumerate(machine, start=1):
        if i in ov:
            out.append(ov[i])
        else:
            out.append(text)
    return out


def save_page(key: str, page: int, text: str, machine_pages: list,
              kb_dir: str = "") -> dict:
    """写**一页**修订。base 取**当前机器稿**该页的 hash（不是上一次修订稿）。"""
    page = int(page)
    pages = load(key, kb_dir)
    meta = load_meta(key, kb_dir)
    pmeta = dict(meta.get("pages") or {})
    pages[page] = str(text or "")
    machine = list(machine_pages or [])
    base = page_hash(machine[page - 1]) if 1 <= page <= len(machine) else ""
    pmeta[str(page)] = {"base": base, "at": now_iso()}
    _save(key, pages, pmeta, kb_dir)
    return {"key": str(key), "page": page, "pages": len(pages),
            "base": base, "base_ok": bool(base)}


def restore_page(key: str, page: int, kb_dir: str = "") -> dict:
    """删掉**一页**的修订；一页不剩就把文件删掉。"""
    page = int(page)
    pages = load(key, kb_dir)
    meta = load_meta(key, kb_dir)
    pmeta = dict(meta.get("pages") or {})
    removed = pages.pop(page, None) is not None
    pmeta.pop(str(page), None)
    _save(key, pages, pmeta, kb_dir)
    return {"key": str(key), "page": page, "removed": removed,
            "pages": len(pages), "cleared": not pages}


def meta(key: str, machine_pages: list | None = None, kb_dir: str = "") -> dict:
    """给界面用：哪几页被改过、哪几页的 base 与**当前机器稿**不符。

    不符 = 「基于旧解析」：MinerU 重解析后那一页的机器文本已经变过，用户改的是
    旧版上的文本。**仍然采用用户版本**，只是要提示他去核对。
    """
    pages = load(key, kb_dir)
    doc = load_meta(key, kb_dir)
    pmeta = doc.get("pages") or {}
    stale = []
    if machine_pages:
        for n in sorted(pages):
            entry = pmeta.get(str(n)) or {}
            base = str(entry.get("base") or "")
            if not base:
                continue
            cur = page_hash(machine_pages[n - 1]) if 1 <= n <= len(machine_pages) else ""
            if cur and cur != base:
                stale.append(n)
    return {"key": str(key), "edited": sorted(pages), "count": len(pages),
            "stale": stale, "stale_count": len(stale),
            "version": int(doc.get("version") or 0),
            "at": (pmeta.get(str(sorted(pages)[-1])) or {}).get("at", "")
                  if pages else ""}


def save_draft(key: str, text: str, machine_pages: list, kb_dir: str = "") -> dict:
    """外部编辑器回灌：把整份草稿**逐页 diff**，只有变了的页写进覆盖。

    为什么不做整份覆盖：MinerU 重解析后整篇会被冻在旧解析上（附录 J.1）。
    """
    draft = parse_md(text)
    machine = list(machine_pages or [])
    pages = load(key, kb_dir)
    pmeta = dict(load_meta(key, kb_dir).get("pages") or {})
    changed = []
    for n, body in draft.items():
        if 1 <= n <= len(machine) and body.strip("\n") == str(machine[n - 1]).strip("\n"):
            # 与机器稿一致 → 撤掉覆盖（别把没改的页也冻住）
            if pages.pop(n, None) is not None:
                changed.append(n)
            pmeta.pop(str(n), None)
            continue
        if pages.get(n, "") != body:
            changed.append(n)
        pages[n] = body
        base = page_hash(machine[n - 1]) if 1 <= n <= len(machine) else ""
        pmeta[str(n)] = {"base": base, "at": now_iso()}
    _save(key, pages, pmeta, kb_dir)
    return {"key": str(key), "changed": sorted(changed), "pages": len(pages)}


def write_draft(key: str, pages: list, kb_dir: str = "") -> str:
    """把**整份有效正文**写成 <kb>/edits/<key>.draft.md（给外部编辑器）。"""
    body = render_md({i + 1: t for i, t in enumerate(pages or [])})
    p = draft_path(key, kb_dir)
    _atomic_write(p, body)
    return p


def summary(kb_dir: str = "") -> dict:
    """总览：有几篇被改过、共几页（面板/维护用）。"""
    keys = all_keys(kb_dir)
    pages = 0
    for k in keys:
        pages += len(load(k, kb_dir))
    return {"items": len(keys), "pages": pages, "keys": keys}
