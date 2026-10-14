# 模型目录与选择：设计依据

> 2026-10-13 立档。目的是"下次改模型选择这一块，不用再去读 DSH 的包"。
> 只写**能指到出处**的事实；拿不准的一律进文末「未核实清单」。

## 1. 结论

成熟软件（含 DSH 官方）在"让用户挑模型"这件事上都是**三层兜底**，且**没有一家
在保存前做模型名存在性校验**：

1. **静态 / 内置目录**（自家维护一份 id + 上下文长度 + 价格，或从服务商接口拉）；
2. **动态探测**（打服务商的 `GET /models`，拿"这个 Key 现在真能用哪些"）；
3. **手填自定义**（vLLM / LM Studio / 内网网关这类没有目录的服务）。

校验一律**分阶段**：保存只做语法校验；"这个名字存不存在"交给保存后的一次试连，
失败就告诉用户该动哪一项（Key / 模型名 / 地址）。

## 2. DSH（官方桌面端 0.2.0-rc.2）的实做

| 位置（包内路径） | 它做什么 |
|---|---|
| `dsh/node_modules/@earendil-works/pi-ai/dist/providers/data/*.json` | **静态目录**：41 个 provider 各一份 JSON（id / name / baseUrl / contextWindow / maxTokens / cost…） |
| 同目录 `.manifest.json` | 目录指纹：`schemaVersion:3`、`generatedAt:2026-09-22T19:31:44.346Z`、`structureHash:c6acf0a1…`（逐文件 sha256） |
| `dsh/node_modules/@earendil-works/dsh-llm-pi-ai/lib/index.js` | 探测逻辑：`discoverModels` / `listingUrl` / `readListing` |
| `~/.dsh/profiles/<profile>/cordis.patch.yml` | 落盘：`agent-default-model` 的 `{provider, model, reasoningEffort}`；密钥**只写不读**（存成凭据引用，配置文件里不出现密钥值） |

`readListing` 里三条硬规则（我们照抄了）：**两种返回体都认**（`{data:[…]}` 与
`{models:{<id>:{…}}}`，后者拿对象键当 id）、**容量字段别名归一化**
（`contextWindow` / `context_window` / `context_length` / `max_input_tokens` /
`limit.context`）、**读上限 4 MiB 且单条坏数据跳过**。
"**命中已装目录就不联网**"是它的第一原则：有静态目录时不去打服务商接口。

已验证（本机复读）：上面两条路径存在；`deepseek.json` 里是
`deepseek-flash`（name `DeepSeek V4.1 Flash`）与 `deepseek-v4-pro`
（name `DeepSeek V4 Pro`），两者 `baseUrl=https://api.deepseek.com`、
`contextWindow=1000000`、`maxTokens=384000`；`openai.json` 里 41 条（含
`gpt-4o-mini` / `o4-mini` / `gpt-5.2` 等）。

## 3. 对照（简要）

| 软件 | 目录从哪来 | 探测 | 手填 |
|---|---|---|---|
| Ollama | 本机已拉取列表（`GET /api/tags`，带 `parameter_size` / `quantization_level`） | 每次查本机 | 用 `ollama pull` 加 |
| OpenRouter | 服务商侧目录（公开的模型列表接口） | 有 | 不支持自定义 id |
| LiteLLM | 静态成本/上下文表 + 各 provider 的 `/models` | 有（`/model/info`、`/v1/models`） | 支持自定义 |
| Open WebUI | 各已配置连接（Ollama / OpenAI 兼容）的 `/models` | 有，连完就能刷新 | 走"自定义连接" |
| Cherry Studio | 各服务商自己的模型清单（多为内置），社区还在要"自动获取可用模型" | 部分 | 支持 |
| Continue | `config.yaml` 里写 `models` 列表（手工维护） | 无 | 就是手填 |

出处：Ollama `/api/tags`（本项目 `offline/judge.py::ollama_models` 实际请求；
`/v1/models` 见同一文件的 OpenAI 兼容分支）；Open WebUI
<https://docs.openwebui.com/ecosystem/computer/ai/connect-a-model/>；
Cherry Studio <https://github.com/CherryHQ/cherry-studio/issues/3448>；
Continue <https://docs.continue.dev/reference/yaml-migration>；
LiteLLM <https://github.com/BerriAI/litellm/blob/main/litellm/litellm_core_utils/get_model_cost_map.py>。
OpenRouter 一行见「未核实清单」。

## 4. 我们面板采纳的方案

```
服务商（稳定 id）            模型
──────────────             ────
ollama   ──► ① GET /api/tags ──► 候选下拉
openai   ──► ① GET {base_url}/models（Bearer key）
                 │  失败
                 ├─► ② 内置候选（照抄 DSH 静态目录，页面写明"探测失败，以下为内置候选"）
                 └─► ③ 手填（那一格本来就是可输入的 combobox）
自定义(custom) ─► 同 openai，只是 base_url 自己填（页面据此分组，落盘仍是 provider=openai）
```

- **落盘只存稳定 id**：`{provider, model, base_url, api_key}`（`provider` 只有
  `ollama` / `openai` 两类 —— 引擎 `judge.generate()` 只认这两类，别写
  `deepseek` 这种服务商名，会被当成 ollama 走本机）。**绝不把 displayName 当模型名**
  （例如 `deepseek-flash` 是可以的，`DeepSeek V4.1 Flash` 不行）。
- **启动恢复**：先按存着的 id 恢复（目录探测失败也不把 id 弄丢），再拿这次探测的
  目录去解析它的名字/容量。
- **保存只做语法校验**：非空、URL 可解析（`http(s)://` 开头）、模型 id 是可打印
  ASCII（`[\x21-\x7E]`）。**不查存在性**。
- **保存后异步试连一次、不阻断**：401/403 → 去查 API Key；404 / "model not found"
  → 点名"模型名可能拼错"；超时/连不上 → 查 base_url 与网络；成功 → 报模型数与
  当前会用它。手填的模型名同样走这一趟。
- **本机优先 `/api/tags`**（比 `/v1/models` 多给参数量与量化档）。
- **缓存 + 显式刷新**：探测结果按 `(provider, base_url)` 在引擎进程里缓存 10 分钟，
  页面加「刷新模型列表」按钮；**打开下拉不打网络**。
- **上下文长度 / 最大输出：能拿到就显示，拿不到就留空**（宁可空也别猜）。

## 5. 未核实清单（照抄，不许美化）

- Python 在本机不可用（读 asar 是用 Node 内联脚本做的）。
- 打包态拿不到 TS 类型。
- OpenRouter 的 `/api/v1/models` 返回体、以及"不存在的 slug 是否 404"未核实。
- "Models 页写 provider/模型列表时落在 patch 文件哪一段"没有观察到真实写入。
