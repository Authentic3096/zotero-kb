"""procrun.py —— 面板/服务里跑子进程的**统一入口**（安全包装）。

为什么要它：管理面板与本地服务都是用 pythonw.exe 拉起来的
(scripts\0-panel.vbs / 4-service.vbs，或 Zotero 插件直接起)。pythonw 是
GUI 子系统、**没有控制台**，它的标准句柄常常是无效的（启动器不传、或传进来的是
一个坏值）。而 subprocess 在 stdin 没被显式指定时会**让子进程继承父进程的
标准句柄** —— 子进程一启动就拿到坏句柄，CreateProcess 直接报:

    OSError: [WinError 61] 句柄无效      （有的机器/入口报 WinError 6）

现象极具迷惑性：**同一个 exe，在控制台里跑完全正常，从面板里跑就"坏"了**
（2026-10-10 用户报的：运行环境页显示 mineru-kit.exe ✓ 存在，MinerU 引导却说
"装是装了，但没能跑起来：… WinError 61"，显卡探测也跟着报没取到信息）。

修法（三档，逐级兜底）:

    第 1 档  无窗口 + stdin=DEVNULL
            正常路径。DEVNULL 把坏句柄换成一个确定有效的空设备，子进程再也
            不继承父进程的句柄。
    第 2 档  无窗口 + DEVNULL + DETACHED_PROCESS
            第 1 档仍报句柄错误时再用。
    第 3 档  cmd /c <命令> > <临时文件> 2>&1，读临时文件
            句柄由 cmd 自己开；连 CreateProcess 都不把自己的句柄给子进程。
            只有 run() / stream() 有这一档。

返回里带 method（"用了哪一档"），调用方可以打进日志 —— 排"为什么这次走了
兜底"时是唯一线索。**第 1 档是正常情况，不要每次都刷日志**。

用法:

    from procrun import run, popen, spawn, stream
    r = run([exe, "models", "show"], timeout=90, merge_stderr=True)
    r.returncode, r.stdout, r.method

    p = popen([exe, "--parse"], merge_stderr=True)      # 流式读 p.stdout
    p.kb_method                                          # 用了哪一档

    spawn([ollama, "serve"], cwd=...)                    # 起了不管（全部 DEVNULL）

本模块**不 import** schemas / panels，避免任何循环依赖：它只依赖标准库。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

IS_WIN = os.name == "nt"

# 别弹黑窗口（Windows）
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0
# 子进程不再附着到父进程的控制台
DETACHED_PROCESS = 0x00000008 if IS_WIN else 0

# "继承来的句柄是坏的" —— 这两条是 Windows 上 CreateProcess 会抛的 OSError。
# 6  = ERROR_INVALID_HANDLE
# 61 = 用户 2026-10-10 实测从面板里报出来的那个（Windows 在坏句柄场景下确实会
#       给 61，别只认 6）
_HANDLE_WINERRORS = (6, 61)

# 最近一次实际使用的档位（排错用；每个进程一份）
LAST_METHOD = ""

_METHOD_1 = "无窗口+DEVNULL"
_METHOD_2 = "分离进程+DEVNULL"
_METHOD_3 = "cmd 重定向"


class Result:
    """run() 的返回值，形状对齐 subprocess.CompletedProcess 的常用字段。"""

    __slots__ = ("args", "returncode", "stdout", "stderr", "method")

    def __init__(self, args, returncode, stdout, stderr, method):
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.method = method

    def __repr__(self) -> str:      # pragma: no cover - 只为排错可读
        return ("Result(returncode=%r, method=%r, stdout=%r, stderr=%r)"
                % (self.returncode, self.method, (self.stdout or "")[:60],
                   (self.stderr or "")[:60]))


def _winerror(exc: BaseException):
    return getattr(exc, "winerror", None)


def _is_handle_error(exc: BaseException) -> bool:
    """这个 OSError 是不是"句柄无效"（只在这种情况才降档重试）。"""
    if not isinstance(exc, OSError):
        return False
    # FileNotFoundError / PermissionError 等是别的病，别拿兜底盖过去
    if isinstance(exc, (FileNotFoundError, PermissionError, NotADirectoryError,
                        IsADirectoryError)):
        return False
    win = _winerror(exc)
    if win is not None:
        return win in _HANDLE_WINERRORS
    # 非 Windows（或拿不到 winerror）时，按 errno 兜一下
    return getattr(exc, "errno", None) in (6, 61)


def _announce(method: str, on_tier=None) -> None:
    global LAST_METHOD
    LAST_METHOD = method
    if method != _METHOD_1 and on_tier:
        try:
            on_tier(method)
        except Exception:      # noqa: BLE001 - 日志回调不该拖垮调用
            pass


def _as_argv(args):
    if isinstance(args, (list, tuple)):
        return list(args)
    return args


def run(args, *, timeout=None, env=None, cwd=None, merge_stderr=False,
        encoding="utf-8", errors="replace", shell=False, on_tier=None):
    """跑一个子进程并收回输出。返回 Result（不抛句柄类错误）。

    参数与 subprocess.run 基本一致，另外：
      merge_stderr=True 时把 stderr 并进 stdout（Result.stderr 为空）；
      on_tier 是"走了兜底档"时可选的日志回调（第 1 档不回调）。
    超时仍抛 subprocess.TimeoutExpired；文件不存在仍抛 FileNotFoundError。
    """
    argv = _as_argv(args)
    last_exc = None
    for method, flags in ((_METHOD_1, CREATE_NO_WINDOW),
                          (_METHOD_2, CREATE_NO_WINDOW | DETACHED_PROCESS)):
        try:
            p = subprocess.run(
                argv, shell=shell, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=(subprocess.STDOUT if merge_stderr else subprocess.PIPE),
                timeout=timeout, env=env, cwd=cwd,
                text=True, encoding=encoding, errors=errors,
                creationflags=flags)
            _announce(method, on_tier)
            return Result(argv, p.returncode, p.stdout,
                          "" if merge_stderr else p.stderr, method)
        except subprocess.TimeoutExpired:
            raise
        except OSError as exc:
            if not _is_handle_error(exc):
                raise
            last_exc = exc
    return _run_via_cmd(argv, timeout=timeout, env=env, cwd=cwd,
                        encoding=encoding, errors=errors, shell=shell,
                        merge_stderr=merge_stderr, on_tier=on_tier,
                        first_exc=last_exc)


def _run_via_cmd(argv, *, timeout, env, cwd, encoding, errors, shell,
                 merge_stderr, on_tier, first_exc):
    """第 3 档兜底：交给 cmd /c 重定向到临时文件，我们再读文件。

    句柄是 cmd 自己开的；我们这边只用 DEVNULL。shell=True 的字符串命令
    也走这里（不再给 subprocess 传 shell）。
    """
    fd, tmp = tempfile.mkstemp(prefix="kbproc-", suffix=".txt")
    os.close(fd)
    try:
        if shell or isinstance(argv, str):
            base = argv if isinstance(argv, str) else subprocess.list2cmdline(argv)
            line = base
        else:
            line = subprocess.list2cmdline(argv)
        redir = '> "%s" 2>&1' % tmp
        # ⚠ 整条命令行用**字符串**交给 CreateProcess（cmd.exe 自己解析）。
        #   不能传 ["cmd.exe","/c", 整条]：Python 会给整条再加一层引号，
        #   cmd 收到的引号配对就乱了（实测退出码 1、输出为空）。
        try:
            p = subprocess.run("cmd.exe /d /c " + line + " " + redir,
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               timeout=timeout, env=env, cwd=cwd,
                               creationflags=CREATE_NO_WINDOW)
            code = p.returncode
        except subprocess.TimeoutExpired:
            raise
        except OSError as exc:
            # 连 cmd 都起不来：把最初那个句柄错误原样抛出去（信息更接近根因）
            raise (first_exc or exc)
        with open(tmp, "rb") as fh:
            data = fh.read()
        text = data.decode(encoding or "utf-8", errors or "replace")
        _announce(_METHOD_3, on_tier)
        return Result(argv, code, text, "", _METHOD_3)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def popen(args, *, env=None, cwd=None, merge_stderr=True,
          encoding="utf-8", errors="replace", shell=False, on_tier=None):
    """起一个**要读输出**的子进程；返回 subprocess.Popen，带 .kb_method。

    两档都失败时把最初那个句柄错误抛出去（流式场景没法用临时文件兜底 ——
    要实时输出的调用方请用 stream()，它会在失败时退回 run）。
    """
    argv = _as_argv(args)
    last_exc = None
    for method, flags in ((_METHOD_1, CREATE_NO_WINDOW),
                          (_METHOD_2, CREATE_NO_WINDOW | DETACHED_PROCESS)):
        try:
            p = subprocess.Popen(
                argv, shell=shell, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=(subprocess.STDOUT if merge_stderr else subprocess.PIPE),
                env=env, cwd=cwd, text=True, encoding=encoding, errors=errors,
                creationflags=flags, bufsize=1)
            try:
                p.kb_method = method
            except Exception:      # noqa: BLE001
                pass
            _announce(method, on_tier)
            return p
        except OSError as exc:
            if not _is_handle_error(exc):
                raise
            last_exc = exc
    raise last_exc if last_exc else OSError("子进程启动失败")


def spawn(args, *, env=None, cwd=None, shell=False, on_tier=None):
    """起一个**不读输出**的后台进程（三个标准流都接空设备）。

    "启动 Ollama / 拉起面板 / 打开资源管理器"这类地方用它 —— 这些地方原来
    只设了 creationflags，stdin/stdout 仍继承坏句柄，一样会 WinError 61。
    返回 Popen（失败时抛最后一次 OSError）。
    """
    argv = _as_argv(args)
    last_exc = None
    for method, flags in ((_METHOD_1, CREATE_NO_WINDOW),
                          (_METHOD_2, CREATE_NO_WINDOW | DETACHED_PROCESS)):
        try:
            p = subprocess.Popen(
                argv, shell=shell, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, cwd=cwd, creationflags=flags)
            try:
                p.kb_method = method
            except Exception:      # noqa: BLE001
                pass
            _announce(method, on_tier)
            return p
        except OSError as exc:
            if not _is_handle_error(exc):
                raise
            last_exc = exc
    raise last_exc if last_exc else OSError("子进程启动失败")


def kill_tree(pid, on_tier=None) -> bool:
    """连同子进程树一起杀（taskkill /T /F）。返回杀成没成。"""
    if not pid:
        return False
    try:
        r = run(["taskkill", "/PID", str(pid), "/T", "/F"],
                merge_stderr=True, timeout=20, on_tier=on_tier)
        return r.returncode == 0
    except Exception:      # noqa: BLE001
        return False


def stream(args, *, log=None, timeout=None, env=None, cwd=None,
           encoding="utf-8", errors="replace", shell=False, on_tier=None,
           kill_on_timeout=True):
    """跑子进程并**实时**喂给 log(line)；返回 (退出码, 输出尾部, 档位)。

    与 popen() 的区别就是它保证有输出：
      · 正常：逐行回调；
      · 两条 Popen 路径都因句柄失败 → 退回 run()（第 3 档），完成后把输出
        **逐行回放**给 log（此时不再实时，但至少能看到结果）。
    timeout 到点会杀进程树并返回 124。
    """
    argv = _as_argv(args)

    def _emit(line: str):
        if log:
            try:
                log(line)
            except Exception:      # noqa: BLE001
                pass

    try:
        p = popen(argv, env=env, cwd=cwd, merge_stderr=True, shell=shell,
                  encoding=encoding, errors=errors, on_tier=on_tier)
    except OSError as exc:
        try:
            res = run(argv, env=env, cwd=cwd, merge_stderr=True, shell=shell,
                      encoding=encoding, errors=errors, on_tier=on_tier)
        except Exception as exc2:      # noqa: BLE001
            return 126, "启动失败：%s；兜底也失败：%s" % (exc, exc2), _METHOD_3
        lines = (res.stdout or "").splitlines()
        for ln in lines:
            _emit("    " + ln[:200])
        return res.returncode, "\n".join(lines[-40:]), res.method

    tail = []
    deadline = (time.time() + timeout) if timeout else None
    try:
        for line in p.stdout:                      # type: ignore[union-attr]
            s = line.rstrip("\r\n")
            tail.append(s)
            if len(tail) > 40:
                tail.pop(0)
            if s.strip():
                _emit("    " + s[:200])
            if deadline and time.time() > deadline:
                if kill_on_timeout:
                    kill_tree(p.pid, on_tier=on_tier)
                else:
                    p.kill()
                return 124, "超时（>%d 秒）：%s" % (
                    int(timeout), "\n".join(tail[-6:])), getattr(p, "kb_method", "")
        return p.wait(), "\n".join(tail), getattr(p, "kb_method", "")
    except Exception as exc:      # noqa: BLE001
        try:
            if kill_on_timeout:
                kill_tree(p.pid, on_tier=on_tier)
        except Exception:      # noqa: BLE001
            pass
        return 126, "%s: %s" % (type(exc).__name__, exc), getattr(p, "kb_method", "")


def selfcheck() -> int:
    """跑一次最小自检（控制台或 pythonw 下都能用），把档位打到 stderr。"""
    r = run([sys.executable, "-c", "print('procrun-ok')"], merge_stderr=True,
            timeout=30)
    print("档位=%s 退出码=%s 输出=%r" % (r.method, r.returncode,
                                        (r.stdout or "").strip()), file=sys.stderr)
    return 0 if r.returncode == 0 and "procrun-ok" in (r.stdout or "") else 1


if __name__ == "__main__":
    sys.exit(selfcheck())
