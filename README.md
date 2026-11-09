# 个人秘书：可写长期记忆的 RAG + Agent

一个面向个人知识管理的可扩展 Agent 骨架：

- **Supervisor** 把问题路由给 `study_expert` / `life_expert`；
- **Worker** 通过工具检索本地 ChromaDB；
- **MemoryCurator** 在每轮回答结束后自动判断是否值得写入长期记忆；
- 你也可以直接说“记住……”，强制保存；
- 知识库支持新增、覆盖、查看、检索、删除，不是只读模式。

所有 Embedding 推理都在本机 CPU 上运行，不需要在线 Embedding 服务或 NVIDIA GPU。

## 技术栈

| 模块 | 选型 |
| --- | --- |
| 生成模型 | DeepSeek API（`https://api.deepseek.com`，模型 `deepseek-flash`） |
| 编排 | LangChain + LangGraph（Supervisor 模式） |
| 向量库 | ChromaDB `PersistentClient`，数据落盘到 `data/runtime/chroma/` |
| Embedding | `sentence-transformers` + `BAAI/bge-small-zh-v1.5`，**纯 CPU** |
| 运行环境 | Windows 11 / Python 3.11+ / 无需 NVIDIA GPU |

## 图结构

```text
START
  │
  ▼
supervisor ──(study_expert)──> study_expert ──┐
  │                                           │
  ├──(life_expert)──────────> life_expert ────┤
  │                                           │
  └──(FINISH)──> memory_curator ──> END       │
                       ▲                      │
                       └──────────────────────┘
                        Worker 完成后回到 supervisor
```

`memory_curator` 只在 Supervisor 判断本轮任务结束时运行一次，不参与回答生成。

## 项目结构

```text
rag/
├─ secretary/
│  ├─ config.py          # .env、HF 镜像、路径、模型/Embedding 配置
│  ├─ llm.py             # DeepSeek ChatOpenAI 初始化
│  ├─ state.py           # LangGraph 共享状态
│  ├─ prompts.py         # Supervisor / Worker / Memory 提示词
│  ├─ supervisor.py      # JSON 结构化路由决策
│  ├─ memory.py          # 自动长期记忆提取与写入
│  ├─ graph.py           # Supervisor + Worker + Memory 的图
│  ├─ json_utils.py      # 容错 JSON 提取
│  ├─ demo_model.py      # 仅用于 --offline 的确定性假模型
│  ├─ message_utils.py   # 消息转文本/截断
│  ├─ workers/
│  │  ├─ base.py         # 通用 Worker：ReAct 工具循环
│  │  ├─ study_expert.py # 高数、C 语言、PTE
│  │  └─ life_expert.py  # Kali、会计、健身
│  ├─ tools/
│  │  └─ retrieval.py    # 真实 Chroma 检索工具
│  └─ rag/
│     ├─ embeddings.py   # 本地 CPU Embedding
│     ├─ chunking.py     # Markdown 按标题切块
│     ├─ ingest.py       # 文件/目录/zip 导入
│     └─ chroma_store.py # Chroma 增删改查封装
├─ scripts/
│  ├─ kb.py              # 知识库管理 CLI（查看/检索/删除）
│  ├─ ingest.py          # 文档导入 CLI
│  ├─ pack_kb.py         # 知识包打包/查看 CLI
│  ├─ download_model.py  # 预下载 Embedding 模型
│  └─ rag_smoke.py       # 单独验证 Chroma + CPU 向量检索
├─ data/
│  ├─ docs/              # 你的个人文档（被 .gitignore 忽略）
│  ├─ example/           # 公开示例文档（可提交）
│  └─ runtime/chroma/    # 运行时向量库（被忽略，可重建）
├─ pack/
│  ├─ example/           # 示例知识包
│  └─ README.md          # 知识包格式说明
├─ test_run.py           # 路由 + 检索端到端演示
├─ test_memory.py        # 自动记忆写入/召回验证
├─ chat.py               # 命令行连续对话入口
├─ requirements.txt
├─ .env.example
└─ README.md
```

## 放入你的资料

把你的 Markdown / TXT 文件放进下面任一位置：

```text
data/docs/            # 个人资料（不提交 GitHub）
pack/你的知识包/       # 可打包、可迁移
```

然后导入：

```powershell
# 导入 data/docs 下全部文件
.\.venv\Scripts\python.exe scripts\ingest.py ingest

# 导入单个文件或目录
.\.venv\Scripts\python.exe scripts\ingest.py ingest --path data\docs\高数.md
.\.venv\Scripts\python.exe scripts\ingest.py ingest --path data\docs --topic 高数
```

导入规则：

- 支持 `.md`、`.markdown`、`.txt`；
- 按 Markdown 标题层级切块，每条片段带「主题｜标题路径」前缀，方便检索；
- 子目录名会作为 `topic`，例如 `data/docs/高数/极限.md` 的主题是 `高数`；
- 同一文件重复导入会覆盖旧片段（稳定 ID），不会重复堆积；
- 导入完成后用 `scripts/kb.py search "关键词"` 验证。

### 打包与迁移

```powershell
# 打包成 zip
.\.venv\Scripts\python.exe scripts\pack_kb.py pack --src pack\example --out pack\example.zip

# 查看包内容
.\.venv\Scripts\python.exe scripts\pack_kb.py inspect --path pack\example.zip

# 从 zip 导入
.\.venv\Scripts\python.exe scripts\ingest.py pack --path pack\example.zip
```

知识包格式见 [pack/README.md](pack/README.md)。

## 快速开始（Windows PowerShell）

在项目目录 `F:\rag` 下执行：

```powershell
# 1. 创建虚拟环境
python -m venv .venv

# 2. 安装依赖
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. 准备配置
Copy-Item .env.example .env
notepad .env
# 把 DEEPSEEK_API_KEY 改成你自己的 Key

# 4. 国内网络建议显式设置镜像（代码默认也会使用 hf-mirror）
$env:HF_ENDPOINT="https://hf-mirror.com"

# 5. 先跑离线演示：不需要 Key
.\.venv\Scripts\python.exe test_run.py --offline

# 6. 跑真实 DeepSeek 路由 + 检索（默认使用临时库，不污染正式记忆）
.\.venv\Scripts\python.exe test_run.py

# 如需写入正式知识库，再显式加 --live-kb
.\.venv\Scripts\python.exe test_run.py --live-kb

# 7. 验证自动记忆（使用临时知识库，不污染正式数据）
.\.venv\Scripts\python.exe test_memory.py
```

如果虚拟环境已存在，可以跳过第 1、2 步。

## 日常使用：命令行聊天

```powershell
cd F:\rag
.\.venv\Scripts\python.exe chat.py
```

聊天界面支持：

| 输入 | 作用 |
| --- | --- |
| 直接输入问题 | 路由 → 检索知识库 → 回答 → 自动记忆 |
| `记住：……` | 强调本轮信息必须写入长期记忆 |
| `/mem` | 查看最近 20 条自动记忆 |
| `/reset` | 清空当前对话上下文（长期记忆保留） |
| `/help` | 显示帮助 |
| `/exit` | 退出 |

命令行参数：

```powershell
.\.venv\Scripts\python.exe chat.py               # 正式知识库 data/runtime/chroma
.\.venv\Scripts\python.exe chat.py --temp-kb     # 临时知识库，退出即丢弃
.\.venv\Scripts\python.exe chat.py --quiet       # 只显示回答，不显示路由/检索细节
.\.venv\Scripts\python.exe chat.py --no-color    # 关闭颜色
```

实际会话示例：

```text
你 > 记住：我在学 PTE，目标是 65 分。
  [路由] study_expert：PTE 属于学业领域
  [工具调用] search_knowledge_base({'query': 'PTE 65 分 学习目标'})
  [检索] 知识库中暂未检索到相关内容。
  [记忆] 已写入/更新 2 条长期记忆
（回答内容……）

你 > 根据我的 PTE 目标推荐练习计划
  [记忆] 已写入/更新 1 条长期记忆
（此时工具结果里会出现 [长期记忆] 你的 PTE 考试目标是 65 分）
```

聊天会把最近 20 条消息作为上下文，所以可以追问“再说简单点”，不用重复背景。
对话上下文只存在进程内，退出后不会保留聊天记录；但**长期记忆会持久化到
`data/runtime/chroma`**，下次启动仍然生效。

## 自动记忆如何工作

每轮对话结束后，`MemoryCurator` 会读取**用户最近的发言**（不读取助手回答），让 DeepSeek 返回：

```json
{
  "should_store": true,
  "memories": [
    {"content": "用户每周三和周五晚上健身", "topic": "健身", "kind": "plan"}
  ]
}
```

会保存的内容：

- 稳定的个人事实：所在地、职业、学习阶段、设备等；
- 偏好和习惯：作息、饮食、训练、学习方式；
- 目标和计划：考试、健身、项目、截止日期；
- 约束：伤病、过敏、时间/预算限制；
- 用户明确说“记住 / 保存 / 以后按这个来”的内容。

不会保存的内容：

- 通用知识问题、作业题、一次性查询、寒暄；
- 助手自己生成的回答；
- 密码、API Key、身份证号、银行卡号等敏感凭证；
- 没有跨会话价值的临时信息。

实现细节：

- 每条记忆使用**内容哈希**作为稳定 ID，重复内容执行 `upsert`，不会堆积；
- 对近义改写会做一次向量相似度检查，距离足够近时复用旧 ID，避免长期使用后
  记忆库被“同一件事的多种说法”堆满；
- 自动记忆 `metadata["source"] = "auto_memory"`，可在 CLI 中单独查看；
- 最多每轮保存 3 条；
- 记忆写入失败不会影响本轮回答，只在输出中报告 `memory_error`；
- 离线模式 `test_run.py --offline` 不写真实 Chroma；
- 真实模式 `test_run.py` 默认也使用临时 KnowledgeBase，只有加 `--live-kb`
  才会读写正式 `data/runtime/chroma`；
- `test_memory.py` 默认使用临时 Chroma 目录，不会写入正式 `data/runtime/chroma`；
- 不需要自动记忆时，可创建 `SecretaryAssistant(..., enable_auto_memory=False)`；
  这会省掉每轮一次额外的记忆判断调用。

自动记忆可能判断错误，建议偶尔检查：

```powershell
.\.venv\Scripts\python.exe scripts\kb.py stats
.\.venv\Scripts\python.exe scripts\kb.py list --source auto_memory
.\.venv\Scripts\python.exe scripts\kb.py search "我的健身习惯"
.\.venv\Scripts\python.exe scripts\kb.py delete memory-xxxxxxxx
```

## 手动写入资料

```powershell
# 新增一条资料
.\.venv\Scripts\python.exe scripts\kb.py add "极限的 ε-N 定义是……" --topic 高数

# 用指定 ID 覆盖/更新一条资料
.\.venv\Scripts\python.exe scripts\kb.py add "更新后的内容" --topic 高数 --id doc-001

# 检索
.\.venv\Scripts\python.exe scripts\kb.py search "极限定义" -k 5
```

Python API：

```python
from secretary.rag import ChromaKnowledgeBase

kb = ChromaKnowledgeBase()
kb.add_texts(
    ["极限的定义是……", "指针保存的是内存地址……"],
    metadatas=[
        {"source": "manual", "topic": "高数"},
        {"source": "manual", "topic": "C语言"},
    ],
)
kb.upsert_texts(["更新后的极限定义"], ids=["doc-001"])
kb.delete_texts(ids=["doc-001"])
```

## 配置说明

`.env` 中的变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 无 | 必填 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 模型名 |
| `CHROMA_DIR` | `data/runtime/chroma` | Chroma 持久化目录 |
| `DOCS_DIR` | `data/docs` | 个人文档目录（`ingest.py` 默认导入这里） |
| `EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 本地 Embedding 模型，可填本地目录 |
| `EMBEDDING_DEVICE` | `cpu` | 只允许 `cpu` |
| `HF_ENDPOINT` | `https://hf-mirror.com` | Hugging Face 镜像 |
| `HF_HUB_OFFLINE` | 未设置 | 设为 `1` 后强制离线加载缓存 |
| `TRANSFORMERS_OFFLINE` | 未设置 | 设为 `1` 后强制离线加载缓存 |

系统环境变量优先于 `.env`。代码在未设置 `HF_ENDPOINT` 时会自动回退到
`https://hf-mirror.com`；如需官方源，显式设置：

```powershell
$env:HF_ENDPOINT="https://huggingface.co"
```

## 预下载 Embedding 模型（推荐）

这样即使之后 Hugging Face 不可达，也能离线使用。模型约 100MB。

### 方式一：用项目脚本

```powershell
cd F:\rag
$env:HF_ENDPOINT="https://hf-mirror.com"
.\.venv\Scripts\python.exe scripts\download_model.py
```

脚本会打印模型缓存路径，例如：

```text
C:\Users\<你>\.cache\huggingface\hub\models--BAAI--bge-small-zh-v1.5\snapshots\<hash>
```

把 `.env` 改成该目录，并打开离线开关：

```env
EMBEDDING_MODEL=C:\Users\<你>\.cache\huggingface\hub\models--BAAI--bge-small-zh-v1.5\snapshots\<hash>
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
```

也可以把整个 `snapshots\<hash>` 目录复制到项目内，例如：

```powershell
Copy-Item -Recurse "C:\Users\<你>\.cache\huggingface\hub\models--BAAI--bge-small-zh-v1.5\snapshots\<hash>" "F:\rag\models\bge-small-zh-v1.5"
```

然后：

```env
EMBEDDING_MODEL=F:\rag\models\bge-small-zh-v1.5
```

### 方式二：用 Docker 预下载（需要 Docker Desktop）

把容器内的 Hugging Face 缓存映射到宿主机的默认缓存目录：

```powershell
docker run --rm -it `
  -v "${env:USERPROFILE}/.cache/huggingface:/root/.cache/huggingface" `
  -e HF_ENDPOINT=https://hf-mirror.com `
  python:3.12-slim `
  sh -c "pip install -U huggingface_hub && hf download BAAI/bge-small-zh-v1.5"
```

下载后，Windows 上的路径就是：

```text
C:\Users\<你>\.cache\huggingface\hub\models--BAAI--bge-small-zh-v1.5\snapshots\<hash>
```

把它填入 `.env` 的 `EMBEDDING_MODEL`，并设置 `HF_HUB_OFFLINE=1` 即可。

### 验证离线加载

```powershell
$env:HF_HUB_OFFLINE="1"
$env:TRANSFORMERS_OFFLINE="1"
.\.venv\Scripts\python.exe -c "from secretary.rag.embeddings import LocalCPUEmbedder; e=LocalCPUEmbedder(); print(len(e.embed_query('测试')))"
```

输出 `512` 就说明模型已完全从本地加载。

## 预期输出

`test_run.py` 会依次问两个问题：

1. 高数极限 → `study_expert`
2. 新手腹肌计划 → `life_expert`

默认使用临时知识库，所以可以放心反复运行；需要验证正式库时使用
`test_run.py --live-kb`。每个问题会打印：

```text
[supervisor] -> study_expert
[study_expert]
  [工具调用] search_knowledge_base({'query': '...'})
  [工具结果] 知识库中暂未检索到……
  [study_expert] ...
[supervisor] -> FINISH
[memory_curator] 写入/更新长期记忆：0 条
```

最后输出：

```text
路由验证通过：两个问题分别到达 study_expert 和 life_expert。
```

`test_memory.py` 会在临时知识库中写入几条带“记住”的个人信息，再验证它们能否被检索到：

```text
本次使用临时知识库，不会写入正式 data/runtime/chroma。

自动记忆写入：3 条
...
验证通过：自动记忆已写入 Chroma，并能在后续检索中召回。
```

## 架构说明

### Supervisor 路由

DeepSeek 返回 JSON，再由 Pydantic 校验：

```python
class RouteDecision(BaseModel):
    next_agent: Literal["study_expert", "life_expert", "FINISH"]
    reason: str
```

`deepseek-flash` 的思考模式不支持强制 `tool_choice`，因此没有使用
`with_structured_output(method="function_calling")`，而是普通对话 + JSON 解析。
Worker 的 tool calling 不强制 `tool_choice`，不受这个限制影响。

### Worker 检索

`WorkerAgent` 实现一个小型 ReAct 循环：

```text
模型 -> 请求工具 -> 执行 Chroma 检索 -> ToolMessage 回填 -> 模型 -> 最终回答
```

工具会过滤余弦距离过大的结果，并在返回文本中标注 `[长期记忆]` 或
`[知识库/主题]`，让模型知道内容来源。Worker 只读知识库；写入由
`MemoryCurator` 和管理 CLI 负责。

### 自动记忆

`MemoryCurator` 在 `FINISH` 后运行：

1. 只收集用户最近 3 条消息；
2. 让模型输出结构化 JSON；
3. 过滤敏感内容和空内容；
4. 按内容哈希生成 ID；
5. `upsert` 到 Chroma，`source=auto_memory`。

## 如何扩展

### 增加 Worker（例如 coding_expert）

1. 在 `secretary/prompts.py` 增加提示词。
2. 在 `secretary/workers/` 新建类，继承 `WorkerAgent`。
3. 在 `secretary/graph.py` 中注册节点、扩展 `RouteDecision` 的 `Literal`
   和条件边映射。
4. 在 Supervisor 提示词中补充职责。

### 导入 Markdown/PDF/网页资料

当前 CLI 支持直接写文本。要做文件导入，可以在 `secretary/rag/` 增加
`ingest.py`：读取文件 → 按标题/长度切块 → 计算稳定 ID → 调用 `upsert_texts`。
Worker 和记忆层不需要改动。

### 多轮对话入口

当前 `test_run.py` / `test_memory.py` 每次调用都是独立一轮。图状态已经用
`add_messages` 保存完整消息；后续增加 CLI/Web 时，把历史消息一起传入
`graph.invoke` 即可。自动记忆每轮都会运行。

## 常见问题

**`未找到有效的 DEEPSEEK_API_KEY`**

确认 `.env` 在项目根目录，且不是模板里的占位 Key。也可以临时设置：

```powershell
$env:DEEPSEEK_API_KEY="sk-..."
```

**Embedding 加载失败 / 连接 huggingface.co 超时 / 429**

先设置镜像再重试：

```powershell
$env:HF_ENDPOINT="https://hf-mirror.com"
.\.venv\Scripts\python.exe scripts\download_model.py
```

如果镜像返回 429，稍等重试；模型下载完成后设置
`HF_HUB_OFFLINE=1` 和 `TRANSFORMERS_OFFLINE=1` 即可离线运行。

**知识库一直检索不到内容**

知识库初始为空，这是正常的。先手动添加资料：

```powershell
.\.venv\Scripts\python.exe scripts\kb.py add "你的资料内容" --topic 高数
```

之后可以直接检索验证：

```powershell
.\.venv\Scripts\python.exe scripts\kb.py search "高数 极限"
```

`test_memory.py` 使用临时知识库，只用于验证自动记忆流程，不会给正式库添加数据。

**自动记忆为什么没保存？**

可能原因：

- 这句话没有长期价值（通用知识、一次性问题）；
- 模型判断 `should_store=false`；
- 内容触发了敏感信息过滤；
- `memory_error` 显示 Embedding/Chroma 写入失败。

先检查 `scripts/kb.py list --source auto_memory`。如果确实需要保存，直接说
“记住：……”，触发词会提高提取优先级。

**会不会用到显卡？**

不会。`LocalCPUEmbedder` 会拒绝 `device != "cpu"`，PyTorch 使用 CPU 推理。

**数据存在哪里？如何清空？**

```text
data/runtime/chroma/     # 向量库与自动记忆（可重建）
data/docs/               # 你的原始文档（导入时读取）
```

测试阶段可以整体删除运行时数据：

```powershell
Remove-Item -Recurse -Force F:\rag\data\runtime\chroma
```

删除后重新执行 `scripts\ingest.py ingest` 即可从 `data/docs` 重建。
正式使用后建议用 `scripts/kb.py delete <id>` 精确删除，避免误删资料。

**导入后检索不到？**

1. 确认导入成功：`scripts\kb.py stats` 的总条数应大于 0；
2. 直接检索测试：`scripts\kb.py search "文档里出现过的关键词"`；
3. 余弦距离过大（> 0.65）的片段会被工具过滤，换更贴近文档原文的说法再问。

## 发布到 GitHub

这个仓库默认只提交**代码和示例**，不会提交个人资料、API Key 和向量库。

| 路径 | 是否提交 | 说明 |
| --- | --- | --- |
| `data/docs/` | ❌ | 你的个人档案，已被 `.gitignore` 忽略 |
| `data/runtime/` | ❌ | 向量库与记忆，可随时从文档重建 |
| `.env` | ❌ | 含 API Key，`.env.example` 才是模板 |
| `data/example/` | ✅ | 公开示例文档 |
| `pack/example/` | ✅ | 公开示例知识包 |
| `pack/*.zip` | ❌ | 本地打包产物 |

首次上传：

```powershell
cd F:\rag
git init
git add .
git status          # 确认 data/docs、.env、data/runtime 没有出现在待提交列表
git commit -m "feat: personal RAG + agent skeleton"
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git branch -M main
git push -u origin main
```

上传前建议自查：

```powershell
# 应该看不到任何个人文档
git ls-files | Select-String "data/docs"
# 应该看不到 .env
git ls-files | Select-String "^\.env$"
```

如果以前误提交过 `.env` 或个人文档，需要从历史中移除并**立刻重置 API Key**。

## 安全与边界

- `.env`、`data/docs/`、`data/runtime/` 都不会提交到版本库。
- 记忆和资料以明文保存在本地 Chroma；当前没有加密、用户认证和权限隔离。
- 自动记忆可能误判，请定期查看并删除不准确的内容。
- 敏感凭证过滤只是兜底，不要主动把密码/API Key 发给模型。
- Kali 相关内容仅用于合法授权场景。
- 当前不包含 PDF/Word 解析、重排序、记忆冲突消解和评测体系。
