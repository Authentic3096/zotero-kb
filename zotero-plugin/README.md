# Zotero 文献知识库插件

把新加入的文献**自动送进本地知识库**（切片 + 索引 + 向量），并用**本地模型**
推荐分类与标签，弹窗让你**一键应用**。

> 本机已升级到 Zotero 10.0.5；插件声明 `7.0 ~ 11.*`，9 和 10 都能装。

---

## 它解决什么

以前新加一篇文献要：双击 `1-convert.cmd` → 等构建 → 再自己想归到哪个分类。
现在：在 Zotero 里保存文献 → 插件自动切片 → 本地模型给出分类建议 → 点「应用」。
全程不用离开 Zotero。

## 安装（三步）

### 1. 启动本地服务

双击 **`scripts\4-service.vbs`**（后台运行、无窗口）。
也可以用管理面板（`scripts\0-panel.vbs`）的「分类 / 标签」页 → 「启动服务」。

服务地址固定 `http://127.0.0.1:8765`，健康检查：
<http://127.0.0.1:8765/health>（这个不需要 token）。

### 2. 装插件

Zotero → **工具 → 插件** → 右上角**齿轮 → Install Plugin From File…**
→ 选 `zotero-plugin\zotero-kb-0.1.0.xpi`

> ⚠ 本机实测：**往 profile 的 `extensions\` 目录丢 xpi 是无效的**
> （Zotero 9/10 都不再扫描那里），必须走上面这个入口。
> 装完不用重启。

### 3. 填设置

Zotero → **编辑 → 设置 → 文献知识库**：

| 项 | 填什么 |
|---|---|
| 本地服务地址 | `http://127.0.0.1:8765`（默认已填） |
| 访问 token | 通常**不用填** —— 插件探活时会自动从服务端取回并保存 |
| **模型来源** | `Ollama（本机）` 或 `OpenAI 兼容 API`，见下一节 |
| 分类列表 | 留空 = 用知识库里现有的分类 |
| 新文献自动处理 | 勾上（默认开） |

点「测试连接」应显示 **✓ 连接成功**。

---

## 模型来源：两种后端

在设置面板的「模型来源」里选，**这是唯一的配置入口**。

### Ollama（本机，默认）

```
模型来源：Ollama（本机，不需要 key）
使用的模型：留空 = 自动选（优先 4B）
```

需要先装好 Ollama 并拉一个模型：

```powershell
ollama pull qwen3:4b-instruct
```

**优点**：完全离线、不花钱、文献内容不出本机。

### OpenAI 兼容 API（云端或自建）

```
模型来源：OpenAI 兼容 API
模型名：  deepseek-chat
API Key： sk-...
API 地址：留空即可（按模型名自动推断）
```

**地址能自动推断**，所以通常只填"模型名 + Key"两样：

| 模型名填 | 自动用的地址 |
|---|---|
| `deepseek-chat` / `deepseek-reasoner` | `https://api.deepseek.com/v1` |
| `qwen-max` / `qwen-plus` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `gpt-4o-mini` | `https://api.openai.com/v1` |
| `moonshot-v1-8k` / `kimi-*` | `https://api.moonshot.cn/v1` |
| `glm-4` | `https://open.bigmodel.cn/api/paas/v4` |
| `llama-3.3-70b` | `https://api.groq.com/openai/v1` |

自建的（vLLM / LM Studio / one-api / SiliconFlow…）**必须手填地址**，
注意有些服务要带 `/v1`。

> 填完点「**把上面这些同步给本机知识库服务**」。为什么需要这一步：
> 插件设置的配置会随分类请求发过去，但**管理面板、命令行**不经过插件 ——
> 同步一次它们就都用同一套，不会出现"插件用 A、面板用 B"。

### 支持范围

凡是提供 `/chat/completions` 的都能用：OpenAI、DeepSeek、通义千问
（dashscope 兼容模式）、Moonshot/Kimi、智谱 GLM、Groq、OpenRouter、
SiliconFlow，以及自建的 vLLM、LM Studio、Ollama 的 `/v1` 层。

**不支持** Anthropic Claude、Google Gemini 的**原生**协议（不是 OpenAI 格式），
要用得先过一层转换网关（one-api / LiteLLM）。

### 换后端之后没生效？

依次看：**①** 有没有点「同步给本机知识库服务」；**②** 服务端日志
（`kb\logs\localserver.log`）；**③** 在管理面板点「检查模型服务」，
它会显示当前用的是哪个后端、试生成一次、失败时给出具体原因
（401 = key 不对、404 = 地址或模型名不对、429 = 限流或余额）。

---

## 用它

保存一篇新文献后，约 2-5 秒（取决于要不要新切片）会弹出提示：

```
文献知识库 · 分类建议
《×××××》

推荐分类：分类 A   置信 92%
理由：………
推荐标签：磁补偿、注意力机制、……

要按这个建议分类并打标签吗？
[应用] [只打标签] [跳过]
```

- **应用**：归入推荐分类 + 加上推荐标签（写回 Zotero）
- **只打标签**：不动分类，只加标签
- **跳过**：什么都不做

若推荐分类不存在，插件会**自动新建**（可在设置里关掉 `createMissing`）。
每篇文献**只归一个分类**（按你的要求），分类最多两层。

---

## 服务接口（想自己接别的工具时看这里）

所有接口都在 `http://127.0.0.1:8765`，除 `/health` 外都要
`X-KB-Token` 头（或 `?token=`）。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 探活（**不需要 token**） |
| GET | `/status` | 知识库规模 |
| POST | `/classify` | 给一篇文献推荐分类 + 标签（插件主要用这个） |
| POST | `/item-info` | 查这些文献切片过没有 |
| POST | `/reindex` | 触发增量构建（异步，返回 job id） |
| POST | `/tag-suggest` | 批量标签建议 |
| GET / POST | `/metafill` | 单篇的元数据补全建议（从 PDF 首页找 date / DOI / 卷 / 期 / 页码；**只读**，写回在插件侧由用户勾选后做） |
| POST | `/collection-suggest` | 整个库的分类整理建议 |
| POST | `/search` | 在知识库里检索 |
| GET | `/jobs/<id>` | 查异步任务状态 |

`/classify` 请求示例：

```json
{
  "item": {"title": "…", "abstract": "…", "tags": ["…"]},
  "categories": [{"name": "分类 A"}, {"name": "分类器"}],
  "model": ""
}
```

响应：`{"category": "分类 A", "confidence": 0.92, "reason": "…", "tags": [...]}`

---

## 排障

| 症状 | 原因 / 处理 |
|---|---|
| 设置里「测试连接」失败 | 服务没启动 → 双击 `scripts\4-service.vbs`；或 token 没填对 |
| 保存文献后没弹窗 | 设置里「新文献自动处理」没勾；或该条目是附件/笔记（插件只处理文献条目） |
| 显示"知识库服务没启动" | 同上，启动服务即可 |
| 想看细节 | Zotero → 帮助 → 调试输出日志；或看 `kb\logs\localserver.log` |
| 分类推荐不准 | 在设置里把「分类列表」填得具体些（模型按这个清单选）；或换 4B 模型 |
| 插件没生效 | 确认版本 `7.0 ~ 11.*` 覆盖你的 Zotero；10.0.5 实测可用 |

## 已知限制

- 插件**不下载 PDF**：它只处理 Zotero 里已经有的附件。
- 切片是**全库增量**跑的（`/reindex` 会扫一遍变化的条目），不是只切你刚加的那篇 ——
  知识库的增量判定基于 Zotero 的 `items.version`，所以很快（本机 100 篇约 0.3 秒）。
- 分类建议**一次只给一篇**；批量重整整个分类结构用管理面板的「生成分类建议」。
- 模型判断有成本：每篇约 2-5 秒（4B，本机 CPU/GPU 混跑）。嫌慢可换 1.7B。
