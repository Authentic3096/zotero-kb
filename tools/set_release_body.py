"""改 GitHub Release 的说明（正文）—— 以后不用再手工粘。

凭据来源：Windows 凭据管理器里 `gh:github.com:<用户名>`（本机已有，
是以前用 gh CLI 留下的通用凭据）。**只在运行时读，不落盘、不打印**。

用法：
    python tools/release/set_release_body.py --tag v1.7.7 --body <说明文件.md>
    python tools/release/set_release_body.py --tag v1.7.7 --show     # 只看当前正文开头

为什么要有它：发布说明随版本走，而 tag 附注只在"新建 Release"时被采用 ——
Release 已存在时工作流只覆盖附件、不动正文（见 .github/workflows/release.yml），
所以正文得单独改。以前是让人去网页上粘，现在这一步也自动化。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import io
import json
import sys
import urllib.error
import urllib.request

REPO_API = "https://api.github.com/repos/Authentic3096/zotero-kb"
TARGETS = ("gh:github.com:Authentic3096", "gh:github.com:")

CRED_TYPE_GENERIC = 1


class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wt.DWORD), ("Type", wt.DWORD),
        ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
        ("LastWritten", wt.FILETIME),
        ("CredentialBlobSize", wt.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
        ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR),
    ]


def read_token() -> str:
    """从凭据管理器取 gh 的 token（取不到返回空串）。"""
    advapi = ctypes.windll.advapi32
    for target in TARGETS:
        pcred = ctypes.POINTER(CREDENTIAL)()
        ok = advapi.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred))
        if not ok:
            continue
        try:
            blob = ctypes.string_at(pcred.contents.CredentialBlob,
                                    pcred.contents.CredentialBlobSize)
            tok = blob.decode("utf-8", "ignore").strip()
            if tok:
                return tok
        finally:
            advapi.CredFree(pcred)
    return ""


def api(path: str, token: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(REPO_API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "zotero-kb-release-script")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="改 GitHub Release 的说明")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--body", default="", help="说明文件（markdown）")
    ap.add_argument("--show", action="store_true", help="只打印当前正文开头")
    args = ap.parse_args(argv)

    token = read_token()
    if not token:
        print("  ✗ 没取到凭据（Windows 凭据管理器里找不到 gh:github.com:*）。")
        print("    临时办法：设 GITHUB_TOKEN 环境变量后重跑（脚本也认它）。")
        import os
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            return 2

    rel = api(f"/releases/tags/{args.tag}", token)
    print(f"  tag={rel['tag_name']}  id={rel['id']}  现有正文 {len(rel.get('body') or '')} 字符")
    if args.show:
        first = ((rel.get("body") or "").split("\n") or [""])[0]
        print("  当前首行：" + first[:80])
        return 0

    if not args.body:
        print("  要改就加 --body <文件>")
        return 2
    text = io.open(args.body, encoding="utf-8").read()
    try:
        out = api(f"/releases/{rel['id']}", token, "PATCH", {"body": text})
    except urllib.error.HTTPError as exc:
        print(f"  ✗ HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:200]}")
        return 1
    print(f"  ✓ 已更新：{out['html_url']}（正文 {len(out['body'])} 字符）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
