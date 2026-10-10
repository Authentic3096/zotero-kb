"""本地小模型（Ollama）辅助：打标签、批量摘要、从对话里抽经验。

    python offline/judge.py status                    # 看 Ollama 与模型是否就绪
    python offline/judge.py tag --limit 5             # 给还没标签的文献打标签（试跑）
    python offline/judge.py summarize --key <KEY>  # 给一篇生成 2-3 句要点
    python offline/judge.py extract --file <片段.txt>  # 从一段文字里抽经验（试跑）

为什么用 4B 小模型干这些：打标签、写摘要、把一段对话整理成 JSON 都是
**抽取与格式化**，不是推理题。交给 DSH 主模型做等于每次都付一遍
"系统提示 + 工具定义"的开场费。放本机跑，成本只有电费。

设计红线（重要）：
  · 小模型**不判断方法是否有效** —— 那要真实实验反馈，它会一本正经地编。
    它只做"把已经写出来的结论整理成结构化字段"。
  · 一切产出都带 `confidence` 与 `evidence`，并且**默认只落到待确认区**，
    人工过一眼才入库（见 learn.py）。
  · 模型不可用时全部功能优雅降级，绝不影响检索。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prompts as PR  # noqa: E402 —— 提示词注册表（用户可改，改完立即生效）
import schemas as S  # noqa: E402
import settings as SETT  # noqa: E402 —— .env 覆盖层（进程环境变量 > .env）

# ⚠ 这两个走 settings：**参数类**（地址 / 模型名），不是密钥 —— 可以写进 .env，
#   这样"面板还没起来、要先改地址"时有一层文件可改。密钥（api_key）**不进 .env**，
#   仍然只在 kb/llm-config.json 里（见 settings.py 的边界说明）。
OLLAMA_HOST = SETT.get("OLLAMA_HOST") or "http://127.0.0.1:11434"
DEFAULT_MODEL = SETT.get("KB_LOCAL_MODEL") or "qwen3:4b-instruct"

# ---------------------------------------------------------------- 模型配置
#
# 支持两类后端（覆盖了绝大多数情况）：
#   · ollama          —— 本机跑（默认）。走它自己的 /api/generate。
#   · openai          —— 任何 OpenAI 兼容的 /chat/completions：
#                        OpenAI、DeepSeek、通义千问(dashscope 兼容模式)、
#                        Moonshot、智谱、Groq、OpenRouter、vLLM、LM Studio…
#                        它们只是 base_url 和模型名不同。
#
# 配置来源优先级：**调用时传参 > 配置文件 > 环境变量 > 默认值**。
#   传参优先是为了让 Zotero 插件设置面板说了算（用户在哪儿配就以哪儿为准）。
#   配置文件 kb/llm-config.json 由管理面板/脚本写入，改了立即生效（每次读）。
#
# ⚠ 配置文件里有 api_key，所以它**不能**进版本库（已在 .gitignore 里）。
LLM_CONFIG_PATH = os.path.join(os.path.dirname(S.INDEX_DB), "llm-config.json")

# 常见服务的默认 base_url，省得用户自己查（留空就按 provider 默认）
KNOWN_BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "ollama": OLLAMA_HOST + "/v1",          # Ollama 也有 OpenAI 兼容层
}


def load_llm_config() -> dict:
    """读模型配置（每次调用都读，改了立即生效，不用重启服务）。"""
    cfg: dict = {}
    try:
        with open(LLM_CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg = data
    except (OSError, json.JSONDecodeError):
        cfg = {}
    # 环境变量兜底（方便 Docker/CI 里配，不用写文件）
    env_map = {
        "provider": "KB_LLM_PROVIDER",
        "base_url": "KB_LLM_BASE_URL",
        "api_key": "KB_LLM_API_KEY",
        "model": "KB_LLM_MODEL",
        "ollama_host": "OLLAMA_HOST",
    }
    for key, env in env_map.items():
        if os.environ.get(env) and not cfg.get(key):
            cfg[key] = os.environ[env]
    # 默认值
    cfg.setdefault("provider", "ollama")
    cfg.setdefault("ollama_host", OLLAMA_HOST)
    cfg.setdefault("model", "")
    cfg.setdefault("base_url", "")
    cfg.setdefault("api_key", "")
    return cfg


def save_llm_config(**kw) -> str:
    """写模型配置（只覆盖传入的键）。返回配置文件路径。"""
    cfg = load_llm_config()
    for key, val in kw.items():
        if val is not None:
            cfg[key] = val
    # 只留认识的键，避免把临时字段写进去
    keep = ("provider", "base_url", "api_key", "model", "ollama_host")
    out = {k: cfg.get(k, "") for k in keep}
    os.makedirs(os.path.dirname(LLM_CONFIG_PATH), exist_ok=True)
    with open(LLM_CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    try:                                    # 尽量收紧权限（Windows 上基本无效，Linux 上有效）
        os.chmod(LLM_CONFIG_PATH, 0o600)
    except OSError:
        pass
    return LLM_CONFIG_PATH


def guess_base_url_from_model(model: str) -> str:
    """从模型名猜服务商，返回它的 base_url（猜不到返回空）。

    为什么要这个：用户只知道自己要用 `deepseek-chat` 或 `gpt-4o-mini`，
    未必知道 base_url 该填什么。能从模型名认出来就自动补上，少一个填错的机会。
    """
    m = (model or "").strip().lower()
    if not m:
        return ""
    rules = [
        ("deepseek", "deepseek"),
        ("qwen", "dashscope"),          # qwen-max / qwen-plus / qwen-turbo
        ("qwq", "dashscope"),
        ("moonshot", "moonshot"),
        ("kimi", "moonshot"),
        ("glm", "zhipu"),
        ("gpt-", "openai"),
        ("llama", "groq"),              # llama-3.x 常见于 Groq
        ("mixtral", "groq"),
        # claude / gemini 不是 OpenAI 协议，故意不猜（免得给出错的服务地址）
    ]
    for token, provider in rules:
        if token in m:
            return KNOWN_BASE_URLS.get(provider, "")
    return ""


def resolve_base_url(provider: str, base_url: str = "", model: str = "") -> str:
    """决定实际的 base_url：显式给的 > 从模型名猜 > 常见服务默认 > 空。"""
    if base_url:
        return base_url.rstrip("/")
    guessed = guess_base_url_from_model(model)
    if guessed:
        return guessed.rstrip("/")
    key = (provider or "").strip().lower()
    if key in KNOWN_BASE_URLS:
        return KNOWN_BASE_URLS[key].rstrip("/")
    return ""


def llm_status() -> dict:
    """当前用哪个后端、是否可用（面板「检查模型服务」用）。"""
    cfg = load_llm_config()
    provider = (cfg.get("provider") or "ollama").strip().lower()
    if provider in ("openai", "openai-compatible", "api"):
        base = resolve_base_url(provider, cfg.get("base_url", ""))
        return {
            "provider": "openai",
            "base_url": base,
            "model": cfg.get("model") or "",
            "has_key": bool(cfg.get("api_key")),
            "available": bool(base and cfg.get("api_key")),
            "hint": "" if (base and cfg.get("api_key"))
                    else "需要填 base_url 与 api_key（在 Zotero 插件的设置面板里）",
        }
    ok, models = ollama_available()
    return {
        "provider": "ollama",
        "base_url": cfg.get("ollama_host") or OLLAMA_HOST,
        "model": cfg.get("model") or (pick_model() if ok else ""),
        "has_key": True,
        "available": ok and bool(models),
        "models": models[:20],
        "hint": "" if (ok and models)
                else "Ollama 连不上或没有模型：启动 Ollama 并 `ollama pull qwen3:4b-instruct`",
    }


# ---------------------------------------------------------------- 调用后端


def ollama_models(host: str = "", timeout: float = 3.0) -> tuple[bool, list[str]]:
    """探测某个 Ollama 地址，返回 (可用, 模型列表)。host 留空用配置里的。

    ⚠ 这个函数原来有**两份**：`ollama_available()`（写死用 OLLAMA_HOST，
    不读配置）和 `_ollama_tags()`（读配置）。后者把前者覆盖掉，
    所以"配置里改了 ollama_host 不生效"这种问题查起来很绕。
    已合并成一个，名字取更直白的 `ollama_models`。
    """
    if not host:
        try:
            host = load_llm_config().get("ollama_host") or OLLAMA_HOST
        except Exception:  # noqa: BLE001
            host = OLLAMA_HOST
    try:
        with urllib.request.urlopen(
                f"{host.rstrip('/')}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # 排序：把常用的（4B 那类）放前面，方便下拉列表里挑
        names = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        return True, names
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return False, []
    except Exception:  # noqa: BLE001
        return False, []


# 兼容旧名字（其它模块还在用 `ollama_available`）
def ollama_available(timeout: float = 3.0) -> tuple[bool, list[str]]:
    return ollama_models("", timeout)


def _ollama_tags(host: str = "", timeout: float = 3.0) -> tuple[bool, list[str]]:
    return ollama_models(host, timeout)


def pick_model(preferred: str = "", host: str = "") -> str:
    """挑一个在位的模型。

    优先级：显式指定（参数或 KB_LOCAL_MODEL）→ 4B（质量更好，用于抽经验/写摘要）
    → 1.7B（轻量，只适合打标签这类更简单的活）→ 任何其它模型。

    为什么要写死"优先 4B"：`ollama list` 的顺序不保证（实测装了 1.7B 之后
    它排到了前面），如果直接取第一个，抽经验质量会**悄悄下降**而没人察觉。

    host 留空则用配置里的 ollama_host。
    注意：这里**不要**去临时改全局 OLLAMA_HOST —— Python 会报
    "name is used prior to global declaration"，而且改全局本来就脏。
    """
    preferred = preferred or SETT.get("KB_LOCAL_MODEL", "")
    ok, models = _ollama_tags(host)
    if not ok or not models:
        return ""
    if preferred:
        for name in models:
            if name == preferred or name.startswith(preferred.split(":")[0]):
                return name
    for want in ("qwen3:4b-instruct", "qwen3:4b", DEFAULT_MODEL):
        for name in models:
            if name == want:
                return name
    for name in models:                      # 退而求其次：任何 4B
        if "4b" in name.lower():
            return name
    for name in models:                      # 再退：任何 qwen3
        if "qwen3" in name.lower():
            return name
    return models[0]


def _generate_ollama(prompt: str, system: str, model: str, json_mode: bool,
                     timeout: float, temperature: float, host: str,
                     num_ctx: int = 0) -> dict:
    """Ollama 原生协议（/api/generate）。"""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        # num_ctx 默认 8192：这些任务每次只吃几百到几千字。
        # 窗格聊天要注入整篇正文时才传更大的值（显存/内存换上下文）。
        "options": {"temperature": temperature,
                    "num_ctx": int(num_ctx) if num_ctx else 8192},
    }
    if system:
        payload["system"] = system
    if json_mode:
        payload["format"] = "json"
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "text": body.get("response", ""), "model": model,
                "eval_count": body.get("eval_count", 0), "backend": "ollama"}
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300]
        return {"ok": False, "error": f"HTTP {exc.code}: {detail!r}",
                "backend": "ollama"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "backend": "ollama"}


def _generate_openai(prompt: str, system: str, model: str, json_mode: bool,
                     timeout: float, temperature: float,
                     base_url: str, api_key: str) -> dict:
    """OpenAI 兼容协议（/chat/completions）。

    这一套能覆盖：OpenAI、DeepSeek、通义千问(dashscope 兼容模式)、Moonshot、
    智谱、Groq、OpenRouter、SiliconFlow、vLLM、LM Studio、Ollama 的 /v1 层…
    它们只是 base_url 和模型名不同，协议一致。
    """
    if not base_url:
        return {"ok": False, "backend": "openai",
                "error": "没有 base_url：请在 Zotero 插件设置里填，"
                         "或从常见服务里选一个（deepseek/openai/dashscope…）"}
    if not api_key:
        return {"ok": False, "backend": "openai",
                "error": "没有 api_key：请在 Zotero 插件设置里填"}
    # ⚠ 清洗 api_key：粘贴时最容易带上首尾空格、换行，甚至从别处复制进来
    #   的非 ASCII 字符（中文引号、全角空格）。这类脏字符会让 HTTP 头发不出去，
    #   报的却是一句 `UnicodeEncodeError: 'latin-1' codec can't encode...`
    #   —— 用户完全看不懂（本机实测）。
    #   清洗**成功也要说一声**：静默改掉用户的输入不是好事 ——
    #   他粘错了却看到"认证失败"，会以为是 key 本身有问题，
    #   而实际原因是自己多粘了个中文标点。
    raw_key = api_key
    api_key = "".join(ch for ch in str(api_key) if ord(ch) < 128).strip()
    key_note = ""
    if not api_key:
        return {"ok": False, "backend": "openai",
                "error": "api_key 里没有可用的 ASCII 字符"
                         "（是不是粘进了中文标点或全角空格？）",
                "hint": f"收到的前几个字符：{raw_key[:6]!r}"
                        "　正确格式是一串以 sk- 开头的英文数字"}
    if api_key != str(raw_key).strip():
        dropped = [c for c in str(raw_key) if ord(c) >= 128]
        key_note = (f"（注意：你的 key 里有 {len(dropped)} 个非英文字符"
                    f"（{''.join(dropped[:4])}…），已自动去掉；"
                    f"如果仍然认证失败，请回服务商后台重新复制一次）")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if json_mode:
        # 注意：不是所有兼容实现都支持 response_format，所以失败时下面会
        # 自动去掉它重试一次（有些老服务会直接 400）。
        payload["response_format"] = {"type": "json_object"}
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    def _post(pl: dict):
        req = urllib.request.Request(
            url, data=json.dumps(pl).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        body = _post(payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400]
        # 400 且带 response_format → 去掉它重试（兼容不支持该字段的服务）
        if exc.code == 400 and "response_format" in payload:
            payload.pop("response_format", None)
            try:
                body = _post(payload)
                text = ""
                choices = body.get("choices") or []
                if choices:
                    text = (choices[0].get("message") or {}).get("content", "")
                return {"ok": True, "text": text, "model": model,
                        "backend": "openai", "note": "该服务不支持 response_format，已自动去掉"}
            except Exception as exc2:  # noqa: BLE001
                return {"ok": False, "backend": "openai",
                        "error": f"HTTP {exc.code}: {detail!r} / 去掉 response_format 后仍失败：{exc2}"}
        # 把服务商返回的 JSON 错误体翻成人话 —— 用户看到
        # `b'{"error":{"message":"Insufficient Balance"...}}'` 是看不懂的
        hint = ""
        try:
            body_json = json.loads(detail.decode("utf-8", "replace"))
            msg = ((body_json.get("error") or {}).get("message")
                   or body_json.get("message") or "")
        except Exception:  # noqa: BLE001
            msg = detail.decode("utf-8", "replace")[:200]
        low = (msg or "").lower()
        if exc.code == 401:
            hint = "api_key 不对或已过期 —— 去服务商后台重新复制一个"
        elif exc.code == 402 or "insufficient" in low or "balance" in low:
            hint = "账户余额不足 —— 去服务商后台充值"
        elif exc.code == 404:
            hint = ("base_url 或模型名不对 —— 注意有些服务地址要带 /v1，"
                    "模型名要和服务商文档一致")
        elif exc.code == 429:
            hint = "请求太快或超出配额 —— 等一会儿再试"
        elif exc.code == 400:
            hint = "请求被拒 —— 多半是模型名写错了，或该模型不支持 JSON 输出"
        elif exc.code >= 500:
            hint = "服务商那边出错了 —— 过一会儿再试"
        full_hint = (key_note + "　" if key_note else "") + (
            hint or "检查插件设置里的「地址 / 模型名 / Key」三项")
        return {"ok": False, "backend": "openai",
                "error": f"HTTP {exc.code}" + (f"：{msg}" if msg else ""),
                "hint": full_hint}
    except UnicodeEncodeError as exc:
        # 上面已经清洗过 key，走到这里说明是别的地方混进了非 ASCII
        # （比如用户把 base_url 填成了中文，或模型名里有全角字符）
        return {"ok": False, "backend": "openai",
                "error": f"配置里有非法字符：{exc}",
                "hint": "检查「API 地址」和「模型名」是不是混进了中文或空格"}
    except urllib.error.URLError as exc:
        return {"ok": False, "backend": "openai",
                "error": f"连不上 {base_url}：{exc.reason}",
                "hint": "检查网络；如果用 OpenAI 官方地址，国内通常需要能访问外网"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "openai",
                "error": f"{type(exc).__name__}: {exc}"}

    choices = body.get("choices") or []
    text = ""
    if choices:
        text = (choices[0].get("message") or {}).get("content", "")
    usage = body.get("usage") or {}
    return {"ok": True, "text": text, "model": body.get("model", model),
            "backend": "openai", "eval_count": usage.get("completion_tokens", 0)}


def generate(prompt: str, system: str = "", model: str = "",
             json_mode: bool = True, timeout: float = 180.0,
             temperature: float = 0.1, provider: str = "",
             base_url: str = "", api_key: str = "",
             num_ctx: int = 0) -> dict:
    """调模型生成。返回 {ok, text|error, model, backend}。

    **后端与模型全都由配置决定**（kb/llm-config.json 或环境变量），
    调用方不用管是哪家 —— 只要传 prompt 就行。想临时覆盖才传 provider 等参数。

    provider:
      · "ollama"（默认）—— 本机跑，走 /api/generate
      · "openai"        —— OpenAI 兼容协议 /chat/completions
                           （DeepSeek、通义、OpenAI、Groq、OpenRouter、
                             vLLM、LM Studio… 都只是 base_url 和模型名不同）

    temperature 压到 0.1：这些任务要的是稳定复现，不是文采。
    json_mode 让模型输出结构化 JSON（两家协议都支持约束解码）。

    num_ctx 只对 Ollama 有意义（默认 8192）：窗格里的聊天要注入整篇上下文，
    8k 装不下；OpenAI 兼容后端由服务端自己决定窗口，这里忽略。
    """
    cfg = load_llm_config()
    prov = (provider or cfg.get("provider") or "ollama").strip().lower()
    if prov in ("openai-compatible", "api", "openai_compatible"):
        prov = "openai"

    if prov == "openai":
        return _generate_openai(
            prompt, system, model or cfg.get("model") or "",
            json_mode, timeout, temperature,
            base_url or resolve_base_url(
                "openai", cfg.get("base_url", ""),
                model or cfg.get("model") or ""),
            api_key or cfg.get("api_key", ""))

    # ---- Ollama
    host = cfg.get("ollama_host") or OLLAMA_HOST
    mdl = model or cfg.get("model") or ""
    if not mdl:
        mdl = pick_model(host=host)          # 把 host 传进去，不动全局
    if not mdl:
        return {"ok": False, "backend": "ollama",
                "error": "Ollama 不可用或没有模型：先启动 Ollama 并 "
                         "`ollama pull qwen3:4b-instruct`"}
    return _generate_ollama(prompt, system, mdl, json_mode, timeout,
                            temperature, host, num_ctx=num_ctx)


def parse_json(text: str) -> tuple[bool, object]:
    """容错地解出 JSON：模型有时会多包一层说明或代码块。"""
    if not text:
        return False, None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return True, json.loads(text)
    except json.JSONDecodeError:
        pass
    # 退一步：抠出最外层的大括号
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return True, json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return False, None
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            return True, json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return False, None


# ---------------------------------------------------------------- 三个任务
#
# ⚠ 话术已经搬到 `offline/prompts.py`（用户可看可改，改完立即生效）。
#   下面三个常量保留下来只为"别处引用它时还能拿到默认值"，
#   真正送进模型的文本一律走 `PR.get()` / `PR.render()`。

TAG_SYSTEM = PR.default("tag", "system")
SUMMARY_SYSTEM = PR.default("summary", "system")
EXTRACT_SYSTEM = PR.default("extract", "system")


def tag_item(title: str, abstract: str, model: str = "") -> dict:
    """给一篇文献打标签。"""
    prompt = PR.render("tag", title=title,
                       abstract=(abstract or "（无摘要）")[:1500])
    result = generate(prompt, system=PR.get("tag", "system"), model=model)
    if not result["ok"]:
        return {"ok": False, "error": result["error"]}
    good, data = parse_json(result["text"])
    if not good or not isinstance(data, dict):
        return {"ok": False, "error": f"模型输出不是 JSON：{result['text'][:200]}"}
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
    return {"ok": True, "tags": [str(t) for t in tags][:8],
            "topic": str(data.get("topic", ""))[:60], "model": result["model"]}


def summarize_item(title: str, text: str, model: str = "") -> dict:
    """给一篇文献写 2-3 句中文要点。"""
    prompt = PR.render("summary", title=title, text=text[:6000])
    result = generate(prompt, system=PR.get("summary", "system"), model=model)
    if not result["ok"]:
        return {"ok": False, "error": result["error"]}
    good, data = parse_json(result["text"])
    if not good or not isinstance(data, dict):
        return {"ok": False, "error": f"模型输出不是 JSON：{result['text'][:200]}"}
    points = data.get("points") or []
    if isinstance(points, str):
        points = [points]
    return {"ok": True, "points": [str(p) for p in points][:4],
            "method": str(data.get("method", ""))[:80],
            "keywords": [str(k) for k in (data.get("keywords") or [])][:8],
            "model": result["model"]}


def extract_experience(text: str, model: str = "") -> dict:
    """从一段对话文字里抽经验条目。

    这是"事后补经验"的核心。提示词里反复强调"没写就留空"，
    因为小模型在这类任务上最大的风险是**编造结论**。
    （话术在 `offline/prompts.py` 的 `extract` 一条里，用户可改。）
    """
    prompt = PR.render("extract", text=text[:8000])
    result = generate(prompt, system=PR.get("extract", "system"), model=model,
                      timeout=240.0)
    if not result["ok"]:
        return {"ok": False, "error": result["error"]}
    good, data = parse_json(result["text"])
    if not good or not isinstance(data, dict):
        return {"ok": False, "error": f"模型输出不是 JSON：{result['text'][:200]}"}
    items = data.get("items") or []
    if not isinstance(items, list):
        items = []
    cleaned = []
    valid_outcomes = {"effective", "ineffective", "partial", "unknown"}
    for item in items:
        if not isinstance(item, dict):
            continue
        outcome = str(item.get("outcome", "unknown")).strip().lower()
        if outcome not in valid_outcomes:
            outcome = "unknown"
        cleaned.append({
            "asked": str(item.get("asked", ""))[:400],
            "method": str(item.get("method", ""))[:300],
            "outcome": outcome,
            "reason": str(item.get("reason", ""))[:600],
            "context": str(item.get("context", ""))[:300],
            "item_refs": [str(x)[:120] for x in (item.get("item_refs") or [])][:5],
            "evidence": str(item.get("evidence", ""))[:200],
            "confidence": float(item.get("confidence") or 0.0),
        })
    return {"ok": True, "found": bool(data.get("found")) and bool(cleaned),
            "items": cleaned, "model": result["model"]}


# ---------------------------------------------------------------- CLI


def list_categories() -> list[dict]:
    """从知识库现有条目聚合分类（名字 + 篇数 + 标题样例）。

    样例是 few-shot 的关键：让模型看到"这个库里什么文献归到哪"的实际口径，
    比只给分类名准得多。
    """
    import json as _json

    import schemas as S

    conn = S.connect(S.INDEX_DB)
    try:
        mapping: dict[str, list[str]] = {}
        for row in conn.execute("SELECT title, collections FROM items"):
            for name in _json.loads(row["collections"] or "[]"):
                mapping.setdefault(name, []).append(row["title"])
        return [{"name": n, "count": len(t), "sample": t[:4]}
                for n, t in sorted(mapping.items())]
    finally:
        conn.close()


def classify_item(item_meta: dict, categories: list[dict] | None = None,
                  model: str = "", feedback: str = "",
                  previous: dict | None = None,
                  provider: str = "", base_url: str = "",
                  api_key: str = "") -> dict:
    """给一篇文献推荐分类与标签（**插件与服务端共用这一份**）。

    设计约束（来自用户）：
      · 每篇文献只归到**一个**分类（不做多归属）——提示词明确要求单选；
      · 分类最多两层，但只推荐**已有分类**，不擅自造新分类；
      · 分类列表明细可由调用方传入（插件可配置），没传就用知识库现有的。

    `feedback` / `previous` 用来支持"跟模型对话调整"：用户对上一次的建议
    不满意时，把原建议和一句人话（"这更像注意力机制那类"）一起发回来，
    模型基于**同一篇文献 + 上一轮结论 + 用户意见**重新判断。
    ⚠ 这不是"让模型自由发挥"：分类名照样必须落在已有分类里（下面的校验不变），
      用户的意见只影响它选哪个，不影响可选范围。

    `provider` / `base_url` / `api_key`：本次请求要用的模型配置。
    ⚠ 必须**直接传给 generate()**，不能只靠"服务端先把参数落盘、这里再读回配置" ——
      那条路有两个坑（本机实测踩到）：
        · `_persist_llm_overrides` 的语义是"空值不写"，所以用户**清空** key
          时旧 key 会留着，本次请求仍拿旧 key 去认证 → 报"key 无效"，
          而用户明明刚改过；
        · 落盘失败（权限、文件被占）时本次请求会静默用旧配置，
          报的错和用户填的完全对不上。
      显式传参之后，"本次用哪套配置"就是确定的。

    ⚠ 这份逻辑原本只写在 `online/localserver.py` 的 do_classify_one 里。
       抽出到这里是因为管理面板也要用 —— 两份实现必然漂移，提示词一改就不同步。
       改动时请只改这里，不要在两处各写一份。
    """
    # ⚠ 不要在这里无条件 pick_model() —— 那是 Ollama 专用的（它会去
    #   探测 /api/tags）。配了 OpenAI 兼容后端时应该用配置里的模型名。
    #   所以：只有 provider=ollama 且没指定模型时，才让 pick_model 挑一个。
    cfg = load_llm_config()
    prov = (provider or cfg.get("provider") or "ollama").strip().lower()
    if not model:
        if prov in ("openai-compatible", "api", "openai_compatible", "openai"):
            model = cfg.get("model") or ""
            if not model:
                return {"error": "没有指定模型", "hint":
                        "在 Zotero 插件设置里填模型名（如 deepseek-chat）"}
        else:
            ok, _models = ollama_available()
            if not ok:
                return {"error": "本地模型服务不可用",
                        "hint": "启动 Ollama 后重试"}
            model = pick_model()
            if not model:
                return {"error": "Ollama 里没有可用模型",
                        "hint": "ollama pull qwen3:4b-instruct"}

    if not categories:
        categories = list_categories()

    cat_lines = []
    for c in categories[:40]:
        samples = "；".join(str(x)[:44] for x in (c.get("sample") or [])[:4])
        cat_lines.append(f"- {c['name']}（{c.get('count', '?')} 篇）"
                         + (f"\n    样例：{samples}" if samples else ""))

    title = item_meta.get("title") or "(无标题)"
    abstract = (item_meta.get("abstract") or "")[:1200]
    tags = item_meta.get("tags") or []
    prompt = (
        "任务：给一篇新加入文献库的文献，从**已有分类里选一个**最合适的。\n\n"
        "【已有分类】\n" + "\n".join(cat_lines) + "\n\n"
        "【待分类文献】\n"
        f"标题：{title}\n"
        f"摘要：{abstract or '(无摘要)'}\n"
        + (f"已有标签：{', '.join(tags[:12])}\n" if tags else "")
    )
    # 用户对上一轮建议的调整意见 —— 放在"文献"之后、"要求"之前，
    # 让模型看到它是"在此基础上修正"而不是"重新随便猜一个"。
    if previous and (previous.get("category") or previous.get("reason")):
        prompt += (
            "\n【你上一轮的建议】\n"
            f"分类：{previous.get('category') or '（未给出）'}\n"
            f"理由：{previous.get('reason') or '（无）'}\n"
        )
    if feedback:
        prompt += (
            "\n【用户的调整意见】（请据此修正，但仍必须从已有分类里选）\n"
            + str(feedback)[:500] + "\n"
        )

    prompt += (
        "\n要求：\n"
        "1. category 必须是上面列出的分类名之一，**原样照抄**，不要改写、不要造新分类。\n"
        "2. 只选**一个**最合适的。若都不合适，category 用空字符串并说明。\n"
        + ("3. **必须尊重用户的调整意见** —— 用户比你更了解这篇文献的归属；\n"
           "   如果用户的意见对应的分类确实在列表里，就选它。\n"
           if feedback else "")
        + "4. 另外给 3-6 个中文标签（可复用「已有标签」里合适的）。\n"
        '输出 JSON：{"category": "分类名", "confidence": 0.0-1.0, '
        '"reason": "为什么归到这里（一句）", "tags": ["标签"]}'
    )
    res = generate(prompt, model=model, timeout=240,
                   provider=provider, base_url=base_url, api_key=api_key)
    if not res.get("ok"):
        # ⚠ hint 要一起带出去 —— 用户看到"HTTP 401"不知道要干嘛，
        #   而"api_key 不对或已过期 —— 去服务商后台重新复制一个"才是他能动的。
        out = {"error": f"模型调用失败：{res.get('error')}"}
        if res.get("hint"):
            out["hint"] = res["hint"]
        return out
    good, data = parse_json(res["text"])
    if not good or not isinstance(data, dict):
        return {"error": "模型输出无法解析", "raw": str(res["text"])[:400]}

    valid_names = {c["name"] for c in categories}
    pick = str(data.get("category") or "").strip()
    # 模型偶尔会改写分类名（加书名号、去空格）。做一次宽松匹配兜住，
    # 但只接受"唯一命中" —— 含糊就不认，宁可不给建议，也不塞进错的分类。
    resolved = pick if pick in valid_names else ""
    if pick and not resolved:
        loose = [n for n in valid_names
                 if pick.strip("《》「」\"' ") == n
                 or pick.strip("《》「」\"' ") in n
                 or n in pick]
        if len(loose) == 1:
            resolved = loose[0]

    tags_out = data.get("tags") or []
    if isinstance(tags_out, str):
        tags_out = [t.strip() for t in tags_out.replace("，", ",").split(",")
                    if t.strip()]
    return {
        "category": resolved,
        "raw_category": pick,
        "confidence": float(data.get("confidence") or 0.0),
        "reason": str(data.get("reason") or "")[:300],
        "tags": [str(t) for t in tags_out][:8],
        "model": model,
        "known_categories": sorted(valid_names),
        # 回带：让前端知道这是第几轮调整（弹窗标题/日志用得上）
        "round": int((previous or {}).get("round") or 0) + 1,
        "feedback": str(feedback)[:200],
    }


def cmd_classify(limit: int, key: str, use_model: bool, as_json: bool = False,
                 feedback: str = "") -> int:
    """命令行：给文献生成分类建议（管理面板「生成分类建议」走这里）。

    use_model=False 时只打印确定性的分类分布，不调模型（快、离线可用）。

    as_json=True：把结果写一份到 `<知识库>\\last-suggestion.json` 并打印路径。
      为什么要有这个：管理面板的「应用建议」需要拿到**上一次的结构化结果**
      （分类名 + 标签），而它是子进程，stdout 里只有给人看的文本。
      写文件比让面板去解析文本稳得多。

    feedback：用户对上一轮建议的调整意见（"这篇应该归到 XX"），
      会拼进提示词让模型重判 —— 与插件弹窗的「调整」是同一条路径。
    """
    import schemas as S

    conn = S.connect(S.INDEX_DB)
    try:
        if key:
            rows = conn.execute(
                "SELECT key, title, abstract, tags, collections FROM items "
                "WHERE key = ?", (key,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT key, title, abstract, tags, collections FROM items "
                "WHERE title <> '' ORDER BY key LIMIT ?", (limit,)).fetchall()
        if not rows:
            print("索引里没有可处理的条目")
            return 1

        cats = list_categories()
        print(f"知识库现有 {len(cats)} 个分类：")
        for c in cats:
            print(f"  {c['name']:26} {c['count']:>4} 篇")
        print()

        if not use_model:
            print("（--no-model：只列分类分布，未调用模型）")
            # 顺手报一下"还没归类"的条目有多少，便于决定要不要跑模型
            n_none = conn.execute(
                "SELECT COUNT(*) FROM items WHERE collections IS NULL "
                "OR collections IN ('', '[]')").fetchone()[0]
            print(f"未归类条目：{n_none} 篇")
            return 0

        ok, models = ollama_available()
        if not ok:
            print("  [XX] 本地模型服务不可用。先启动 Ollama。")
            return 1
        model = pick_model()
        print(f"用模型：{model}（可选：{', '.join(models[:5])}）")
        print(f"给 {len(rows)} 篇生成建议（每篇一次调用，可能要等一会儿）…\n")

        import json as _json

        if feedback:
            print(f"  调整意见：{feedback[:120]}")
            print()

        last = None
        for row in rows:
            cur = _json.loads(row["collections"] or "[]")
            meta = {
                "title": row["title"],
                "abstract": row["abstract"] or "",
                "tags": _json.loads(row["tags"] or "[]"),
            }
            res = classify_item(meta, cats, feedback=feedback,
                                previous=(last if feedback else None))
            head = f"  {row['key']}  {str(row['title'])[:40]}"
            if res.get("error"):
                print(f"{head}\n      [XX] {res['error']}")
                continue
            cur_s = "、".join(cur) or "（未归类）"
            cat = res["category"] or "（无合适分类）"
            mark = "=" if (res["category"] and res["category"] in cur) else "→"
            print(f"{head}")
            print(f"      现在：{cur_s}")
            print(f"      建议：{cat}   置信 {res['confidence']:.2f}   {mark}")
            if res.get("raw_category") and res["raw_category"] != res["category"]:
                print(f"      （模型原话「{res['raw_category']}」已按已知分类归一）")
            if res.get("reason"):
                print(f"      理由：{res['reason']}")
            if res.get("tags"):
                print(f"      标签：{'、'.join(res['tags'])}")
            # 记下结构化结果：单篇时给面板的「应用建议」用；
            # 多篇时也留着最后一条，便于 --json 的语义一致。
            last = dict(res)
            last["key"] = row["key"]
            last["title"] = row["title"]
            last["current_collections"] = cur

        if as_json and last:
            out = os.path.join(S.KB_DIR, "last-suggestion.json")
            try:
                with open(out, "w", encoding="utf-8") as fh:
                    _json.dump(last, fh, ensure_ascii=False, indent=2)
                print(f"\n结果已写入：{out}")
            except OSError as exc:
                print(f"\n[XX] 写结果文件失败：{exc}")
                return 1
        elif as_json:
            print("\n[XX] 没有可写入的建议结果（模型都失败了？）")
            return 1

        print("\n提示：这只是建议，不会自动改动 Zotero。"
              "要真正归类请用插件的「一键应用」，或跑 zotero_sync.py。")
        return 0
    finally:
        conn.close()


def cmd_status() -> int:
    """看模型状态（支持 Ollama 与 OpenAI 兼容两种后端）。"""
    st = llm_status()
    print("=" * 66)
    print("模型状态")
    print("=" * 66)
    print(f"  后端（provider）：{st['provider']}")
    print(f"  地址：{st['base_url'] or '（未配置）'}")
    print(f"  模型：{st.get('model') or '（未指定，Ollama 会自动挑）'}")
    if st["provider"] == "openai":
        print(f"  API Key：{'已配置' if st['has_key'] else '（未配置）'}")
    print()

    if not st["available"]:
        print(f"  [XX] 不可用：{st.get('hint') or '配置不完整'}")
        if st["provider"] == "ollama":
            print("       注：检索与读全文不依赖它，"
                  "只影响打标签/摘要/抽经验/分类建议。")
        return 1

    if st["provider"] == "ollama":
        print(f"  [OK] Ollama 在跑，已有模型：{', '.join(st.get('models') or [])}")

    # 真跑一次，确认能出结果（也顺带暖机 / 验证 key 有效）
    print("\n  试生成一次…")
    r = generate('只输出 JSON：{"ok": true}', timeout=120)
    if r.get("ok"):
        good, _data = parse_json(r["text"])
        print(f"  [OK] 成功（backend={r.get('backend')}，model={r.get('model')}，"
              f"{r.get('eval_count', 0)} tokens）"
              f"{'，JSON 可解析' if good else '，但 JSON 解析失败：' + str(r['text'])[:80]}")
        return 0
    print(f"  [XX] 失败：{r.get('error')}")
    if r.get("backend") == "openai":
        print("       检查：base_url 是否正确（有些服务要带 /v1）、"
              "api_key 是否有效、模型名是否是服务商支持的")
    return 1


def cmd_tag(limit: int, write: bool) -> int:
    conn = S.connect(S.INDEX_DB)
    rows = conn.execute(
        "SELECT key, title, abstract FROM items WHERE title <> '' ORDER BY RANDOM() LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        print("索引里没有条目")
        return 1
    print(f"给 {len(rows)} 篇打标签（write={write}）\n")
    for row in rows:
        result = tag_item(row["title"], row["abstract"] or "")
        if not result["ok"]:
            print(f"  [XX] {row['key']} {result['error']}")
            continue
        print(f"  [OK] {row['key']} {row['title'][:38]}")
        print(f"       tags={result['tags']}")
        print(f"       topic={result['topic']}")
        if write:
            conn.execute(
                "INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)",
                (f"ai_tags:{row['key']}",
                 json.dumps({"tags": result["tags"], "topic": result["topic"],
                             "model": result["model"]}, ensure_ascii=False)),
            )
    if write:
        conn.commit()
        print("\n已写入 meta 表（ai_tags:<key>）。注意：不覆盖 Zotero 里的标签。")
    conn.close()
    return 0


def cmd_summarize(key: str, write: bool) -> int:
    conn = S.connect(S.INDEX_DB)
    row = conn.execute("SELECT key, title FROM items WHERE key = ?", (key,)).fetchone()
    if not row:
        print(f"没有 key={key} 的条目")
        return 1
    path = os.path.join(S.FULLTEXT_DIR, f"{key}.md")
    if not os.path.exists(path):
        print(f"《{row['title']}》没有全文文件，无法摘要")
        return 1
    text = open(path, encoding="utf-8").read()
    result = summarize_item(row["title"], text)
    if not result["ok"]:
        print(f"失败：{result['error']}")
        return 1
    print(f"《{row['title']}》")
    print(f"  核心方法：{result['method']}")
    print(f"  关键词：{', '.join(result['keywords'])}")
    for i, point in enumerate(result["points"], 1):
        print(f"  {i}. {point}")
    if write:
        conn.execute(
            "INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)",
            (f"ai_summary:{key}", json.dumps(result, ensure_ascii=False)),
        )
        conn.commit()
        print("\n已写入 meta 表（ai_summary:<key>）")
    conn.close()
    return 0


def cmd_extract(path: str) -> int:
    if path == "-":
        text = sys.stdin.read()
    else:
        text = open(path, encoding="utf-8").read()
    result = extract_experience(text)
    if not result["ok"]:
        print(f"失败：{result['error']}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="本地小模型辅助")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p_tag = sub.add_parser("tag")
    p_tag.add_argument("--limit", type=int, default=5)
    p_tag.add_argument("--write", action="store_true")
    p_sum = sub.add_parser("summarize")
    p_sum.add_argument("--key", required=True)
    p_sum.add_argument("--write", action="store_true")
    p_ext = sub.add_parser("extract")
    p_ext.add_argument("--file", required=True)
    p_cls = sub.add_parser("classify")
    p_cls.add_argument("--limit", type=int, default=10)
    p_cls.add_argument("--key", default="")
    p_cls.add_argument("--no-model", action="store_true",
                       help="只列分类分布，不调模型")
    p_cls.add_argument("--json", action="store_true", dest="as_json",
                       help="把结构化结果写到 <知识库>\\last-suggestion.json，"
                            "供管理面板的「应用建议」使用")
    p_cls.add_argument("--feedback", default="",
                       help="对上一轮建议的调整意见（跟模型对话用），"
                            "例如：这篇应该归到分类 A")
    args = parser.parse_args()

    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "tag":
        return cmd_tag(args.limit, args.write)
    if args.cmd == "summarize":
        return cmd_summarize(args.key, args.write)
    if args.cmd == "classify":
        return cmd_classify(args.limit, args.key, not args.no_model,
                            as_json=args.as_json, feedback=args.feedback)
    return cmd_extract(args.file)


if __name__ == "__main__":
    sys.exit(main())
