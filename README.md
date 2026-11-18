# 个人秘书：带工作台的 RAG + Agent

一个面向个人知识管理与日常事务的可扩展 Agent 骨架：

- **Supervisor** 把问题路由给 `study_expert` / `life_expert` / `desk_expert`；
- **Worker** 通过工具检索本地 ChromaDB；
- **desk_expert** 负责工作台：待办、日程、提醒、每日简报，支持“明天下午3点”这类自然语言时间；
- **MemoryCurator** 在每轮回答结束后自动判断是否值得写入长期记忆；
- **Web UI 预览**：基于 Streamlit 构建的极简交互界面，支持流式回答、Agent 路由追踪、长期记忆面板与侧栏工作台。
- 你也可以直接说“记住……”，强制保存；
- 知识库支持新增、覆盖、查看、检索、删除，不是只读模式。

所有 Embedding 推理都在本机 CPU 上运行，不需要在线 Embedding 服务或 NVIDIA GPU。

## 技术栈

| 模块 | 选型 |
| --- | --- |
| 生成模型 | DeepSeek API（`https://api.deepseek.com`，模型 `deepseek-flash`） |
| 编排 | LangChain + LangGraph（Supervisor 模式） |
| Web UI | Streamlit（流式输出、路由状态、检索来源） |
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
  ├──(desk_expert)──────────> desk_expert ────┤
  │                                           │
  └──(FINISH)──> memory_curator ──> END       │
                       ▲                      │
                       └──────────────────────┘
                        Worker 完成后回到 supervisor
```

`memory_curator` 只在 Supervisor 判断本轮任务结束时运行一次，不参与回答生成。

## 项目结构

```text
rag-agent-secretary/
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
│  ├─ desk/
│  │  ├─ models.py       # 任务/通知数据模型
│  │  ├─ store.py        # SQLite 工作台存储与查询
│  │  └─ timeparse.py    # “明天下午3点”等自然语言时间解析
│  ├─ workers/
│  │  ├─ base.py         # 通用 Worker：ReAct 工具循环
│  │  ├─ study_expert.py # 高数、C 语言、PTE
│  │  ├─ life_expert.py  # Kali、会计、健身
│  │  └─ desk_expert.py  # 待办、日程、提醒
│  ├─ tools/
│  │  ├─ retrieval.py    # 真实 Chroma 检索工具
│  │  └─ desk_tools.py   # 工作台读写工具（add/list/done 等）
│  └─ rag/
│     ├─ embeddings.py   # 本地 CPU Embedding
│     ├─ chunking.py     # Markdown 按标题切块
│     ├─ ingest.py       # 文件/目录/zip 导入
│     └─ chroma_store.py # Chroma 增删改查封装
├─ scripts/
│  ├─ kb.py              # 知识库管理 CLI（查看/检索/删除）
│  ├─ ingest.py          # 文档导入 CLI
│  ├─ pack_kb.py         # 知识包打包/查看 CLI
│  ├─ desk.py            # 工作台 CLI（增删改查/完成/取消）
│  ├─ remind_daemon.py   # 本地提醒与每日简报 daemon
│  ├─ download_model.py  # 预下载 Embedding 模型
│  └─ rag_smoke.py       # 单独验证 Chroma + CPU 向量检索
├─ data/
│  ├─ docs/              # 你的个人文档（被 .gitignore 忽略）
│  ├─ example/           # 公开示例文档（可提交）
│  ├─ runtime/chroma/    # 运行时向量库（被忽略，可重建）
│  └─ runtime/desk.db    # 工作台待办/日程/通知（被忽略）
├─ pack/
│  ├─ example/           # 示例知识包
│  └─ README.md          # 知识包格式说明
├─ tests/
│  ├─ test_chunking.py   # 切块长度与稳定 ID 测试
│  ├─ test_ingest.py     # 导入/更新/清理一致性测试
│  ├─ test_desk_store.py # 工作台 SQLite 存储测试
│  ├─ test_desk_timeparse.py # 自然语言时间解析测试
│  ├─ test_desk_reminders.py # 提醒与简报逻辑测试
│  ├─ test_desk_expert.py    # desk_expert 工具调用测试
│  └─ test_chroma_integration.py # 真实 Chroma 导入集成测试
├─ test_run.py           # 路由 + 检索端到端演示
├─ test_memory.py        # 自动记忆写入/召回验证
├─ chat.py               # 命令行连续对话入口
├─ app.py                # Streamlit Web UI
├─ requirements.txt
├─ requirements-dev.txt  # pytest 等开发依赖
├─ pytest.ini
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
- 同一文件重复导入会更新内容变化的片段，并清理已经从文档中删除的小节；
- 重命名文件会被视为新文件，旧路径的片段不会自动删除；需要时用
  `scripts/kb.py list --source doc` 找到旧 ID，再用 `delete` 清理；
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

在项目根目录下执行（下面以 `D:\rag-agent-secretary` 为例，按你的实际路径调整）：

```powershell
cd D:\rag-agent-secretary

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

# 8. 启动 Web UI
.\.venv\Scripts\streamlit.exe run app.py

# 9. 运行自动化测试（可选，先安装开发依赖）
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

如果虚拟环境已存在，可以跳过第 1、2 步。

测试使用临时文件和内存假知识库，不会访问网络，也不会写入正式
`data/runtime/chroma`。

## 日常使用：命令行聊天

```powershell
cd D:\rag-agent-secretary
.\.venv\Scripts\python.exe chat.py
```

聊天界面支持：

| 输入 | 作用 |
| --- | --- |
| 直接输入问题 | 路由 → 检索知识库 → 回答 → 自动记忆 |
| `记住：……` | 强调本轮信息必须写入长期记忆 |
| `/mem` | 查看最近 20 条自动记忆 |
| `/tasks` | 查看当前待办 |
| `/agenda` | 查看今日安排与已过期未完成 |
| `/done <id>` | 把指定任务标记为完成（id 可用 `/tasks` 查看） |
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

## 工作台（待办/日程/提醒）

工作台数据保存在本地 SQLite `data/runtime/desk.db`，由 `desk_expert` 和
`scripts/desk.py` 共同读写。时间既支持 `2026-11-20 09:00` 这类绝对格式，
也支持“明天下午3点”“下周一上午”这类自然语言。

### 对话式

直接对秘书说：

```text
你 > 提醒我明天下午3点交房租
  [路由] desk_expert：涉及待办与提醒
  [工具调用] add_task({'title': '交房租', 'due_at': '明天下午3点', ...})
（desk_expert 已把任务写入 tasks 表，并返回任务 id）
```

“提醒我……”会自动落库；“今天还有什么安排”会走 `list_schedule`；
“把刚那个任务标记完成”会走 `complete_task` 或 `update_task`。

### CLI

```powershell
# 新增（--due 用 "YYYY-MM-DD HH:MM" 绝对格式；--remind 是提前分钟数，必须配合 --due）
.\.venv\Scripts\python.exe scripts\desk.py add "交房租" --due "2026-11-20 09:00" --priority high --remind 60

# 查看（默认只看未完成；--all 包含已完成，--query 模糊搜索）
.\.venv\Scripts\python.exe scripts\desk.py list
.\.venv\Scripts\python.exe scripts\desk.py list --all

# 状态流转
.\.venv\Scripts\python.exe scripts\desk.py done <id>
.\.venv\Scripts\python.exe scripts\desk.py reopen <id>
.\.venv\Scripts\python.exe scripts\desk.py cancel <id>

# 删除：默认软删除（改为已取消），--hard 才真正删行
.\.venv\Scripts\python.exe scripts\desk.py rm <id>
.\.venv\Scripts\python.exe scripts\desk.py rm <id> --hard
```

`--db PATH` 可以写在子命令前后，默认使用 `data/runtime/desk.db`。

### 提醒 daemon

```powershell
# 默认每 30 秒轮询一次，每天 08:00 发送简报
.\.venv\Scripts\python.exe scripts\remind_daemon.py

# 自定义间隔、简报时刻，或只跑一轮做验证
.\.venv\Scripts\python.exe scripts\remind_daemon.py --interval 5
.\.venv\Scripts\python.exe scripts\remind_daemon.py --briefing 08:30
.\.venv\Scripts\python.exe scripts\remind_daemon.py --once
```

对应的环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `REMIND_INTERVAL_SECONDS` | `30` | 轮询间隔（秒），`--interval` 优先 |
| `BRIEFING_TIME` | `08:00` | 每日简报时刻（HH:MM），`--briefing` 优先 |

daemon 启动后会先补发已错过截止时间但尚未提醒的任务，之后到点打印提醒并写入
`notifications` 表；每天只发一次简报，重启不会重复。

想把提醒和简报推到企业微信，在 `.env` 中设置：

```dotenv
NOTIFY_CHANNEL=wecom
WECOM_CORP_ID=你的企业ID
WECOM_AGENT_ID=1000002
WECOM_SECRET=自建应用的Secret
WECOM_USER_ID=@all
```

推送只使用标准库 `urllib`，不需要额外依赖。企业微信的 `access_token`
会在进程内缓存并提前刷新；推送失败只记警告，不会中断 daemon。`NOTIFY_CHANNEL`
保持默认 `none` 时只打印到终端。

### 三端查看

- **命令行**：`chat.py` 内输入 `/tasks`（当前待办）、`/agenda`（今日安排 +
  已过期未完成）、`/done <id>`（标记完成）。
- **Web**：侧栏展开「工作台」，查看今日待办、未读通知，并可直接点“完成”。
- **CLI**：`scripts/desk.py list` / `list --all`，适合脚本或批量处理。

## Web UI（推荐）

```powershell
cd D:\rag-agent-secretary
.\.venv\Scripts\streamlit.exe run app.py
```

浏览器打开：

```text
http://localhost:8501
```

如果端口被占用：

```powershell
.\.venv\Scripts\streamlit.exe run app.py --server.port 8502
```

Web UI 功能：

- **流式回答**：Worker 的最终回答逐 token 显示；
- **Supervisor 路由状态**：实时显示 `主管正在思考…… 路由至 study_expert`；
- **检索来源**：折叠面板展示 Worker 调用的 query 和 Chroma 返回原文，
  内容带 `[长期记忆]` / `[知识库/主题]` 标记；
- **清空对话**：只清空当前会话上下文，不删除长期记忆；
- **查看长期记忆**：侧边栏展示最近写入的自动记忆和 ID；
- 连续对话最多带最近 20 条消息。

侧边栏的“清空对话”等价于 CLI 的 `/reset`；“查看长期记忆”直接读取
`ChromaKnowledgeBase.list_texts(where={"source": "auto_memory"})`。

> `app.py` 使用正式知识库 `data/runtime/chroma` 和长期记忆。
> 如果想用临时库测试，可复制一份 `app.py` 或改用 CLI 的 `--temp-kb`。

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
.\.venv\Scripts\python.exe scripts\kb.py list --source doc
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
| `BRIEFING_TIME` | `08:00` | 提醒 daemon 每日简报时刻（HH:MM） |
| `REMIND_INTERVAL_SECONDS` | `30` | 提醒 daemon 轮询间隔（秒） |
| `NOTIFY_CHANNEL` | `none` | 通知渠道：`none` / `wecom` |
| `WECOM_CORP_ID` | 空 | 企业微信企业 ID |
| `WECOM_AGENT_ID` | 空 | 企业微信应用 AgentId |
| `WECOM_SECRET` | 空 | 企业微信应用 Secret |
| `WECOM_USER_ID` | `@all` | 接收人，多个 UserID 用 `\|` 分隔 |

系统环境变量优先于 `.env`。代码在未设置 `HF_ENDPOINT` 时会自动回退到
`https://hf-mirror.com`；如需官方源，显式设置：

```powershell
$env:HF_ENDPOINT="https://huggingface.co"
```

## 预下载 Embedding 模型（推荐）

这样即使之后 Hugging Face 不可达，也能离线使用。模型约 100MB。

### 方式一：用项目脚本

```powershell
cd D:\rag-agent-secretary
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
Copy-Item -Recurse "C:\Users\<你>\.cache\huggingface\hub\models--BAAI--bge-small-zh-v1.5\snapshots\<hash>" "D:\rag-agent-secretary\models\bge-small-zh-v1.5"
```

然后：

```env
EMBEDDING_MODEL=D:\rag-agent-secretary\models\bge-small-zh-v1.5
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

`test_run.py` 会依次问三个问题：

1. 高数极限 → `study_expert`
2. 新手腹肌计划 → `life_expert`
3. 提醒我明天下午3点交房租 → `desk_expert`

默认使用临时知识库和工作台数据库，所以可以放心反复运行；需要验证正式库时使用
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

工作台问题会多出 `desk_expert` 的工具调用与落库结果：

```text
[supervisor] -> desk_expert
[desk_expert]
  [工具调用] add_task({'title': '交房租', 'due_at': '明天下午3点', ...})
  [工具结果] 已添加任务：a1b2c3d4... | 交房租 | 待办 | 截止 2026-11-19 15:00
  [desk_expert] 已帮你记下：明天 15:00 交房租……
[supervisor] -> FINISH
[memory_curator] 写入/更新长期记忆：0 条
```

最后输出：

```text
路由验证通过：三个问题分别到达 study_expert、life_expert 和 desk_expert。
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
    next_agent: Literal["study_expert", "life_expert", "desk_expert", "FINISH"]
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

### 增加 Worker（已以 desk_expert 为例落地）

`desk_expert` 就是按这套流程加进来的：新增 Worker 不只是写一个类，还要同步
下面 5 处，否则路由、离线演示或端到端测试会缺一条链路。

1. `secretary/state.py`：在 `RouteName` 的 `Literal` 中加入新节点名。
2. `secretary/supervisor.py`：在 `RouteDecision.next_agent` 的 `Literal`
   中加入同一个节点名。
3. `secretary/prompts.py`：新增该 Worker 的系统提示词，并在
   `SUPERVISOR_SYSTEM_PROMPT` 中补充职责和路由规则。
4. `secretary/graph.py`：在 `WORKER_NODES`、`add_node`、条件边映射和
   “回到 supervisor”的边里注册新节点；需要新工具时在 `secretary/tools/`
   增加并在 `build_secretary_graph` 中注入。
5. `secretary/demo_model.py`：补离线模式下的路由关键词和回复；
   `test_run.py` 的 `QUESTIONS` 再加一个能命中新 Worker 的问题。

新增 Worker 类本身放在 `secretary/workers/`，继承 `WorkerAgent` 即可。
可对照 `desk_expert.py` / `desk_tools.py` / `DESK_EXPERT_PROMPT` /
`graph.py` 中的注册代码，以及 `tests/test_desk_expert.py`。

### 扩展文档格式（PDF/Word/网页）

当前内置导入支持 `.md`、`.markdown`、`.txt`，实现位于
`secretary/rag/ingest.py`。要支持 PDF/Word/网页，可以新增解析函数，先把内容
转成纯文本或 Markdown，再复用 `chunk_markdown` 和 `ingest_text` / `ingest_file`。
Worker 和记忆层不需要改动。

### 接入新的对话前端

`chat.py` 和 `app.py` 已经实现多轮对话：把最近 20 条历史消息传给
`SecretaryAssistant.stream()` 或 `SecretaryAssistant.invoke()` 即可。自动记忆
每轮都会运行。如果要接入其他前端，复用 `SecretaryAssistant`，不需要直接操作
LangGraph 图。

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
data/runtime/desk.db     # 工作台待办/日程/通知（sqlite，本地数据）
data/docs/               # 你的原始文档（导入时读取）
```

测试阶段可以整体删除运行时数据：

```powershell
Remove-Item -Recurse -Force D:\rag-agent-secretary\data\runtime\chroma
Remove-Item -Force D:\rag-agent-secretary\data\runtime\desk.db
```

删除后重新执行 `scripts\ingest.py ingest` 即可从 `data/docs` 重建向量库；
工作台数据没有对应的文档来源，删除后无法自动恢复。
正式使用后建议用 `scripts/kb.py delete <id>` 精确删除，避免误删资料。

**导入后检索不到？**

1. 确认导入成功：`scripts\kb.py stats` 的总条数应大于 0；
2. 直接检索测试：`scripts\kb.py search "文档里出现过的关键词"`；
3. 余弦距离过大（> 0.65）的片段会被工具过滤，换更贴近文档原文的说法再问。

**修改文档后重新导入会更新吗？**

会。同一个 `source_path` 下，内容变化的片段会更新，已经从文档中删除的小节
会被清理。重命名文件会被视为新文件，旧路径的片段需要手动删除：

```powershell
.\.venv\Scripts\python.exe scripts\kb.py list --source doc
.\.venv\Scripts\python.exe scripts\kb.py delete <旧片段 ID>
```

## 发布到 GitHub

这个仓库默认只提交**代码和示例**，不会提交个人资料、API Key 和向量库。

| 路径 | 是否提交 | 说明 |
| --- | --- | --- |
| `data/docs/` | ❌ | 你的个人档案，已被 `.gitignore` 忽略 |
| `data/runtime/` | ❌ | 向量库、长期记忆与工作台 `desk.db`，均为本地运行时数据 |
| `.env` | ❌ | 含 API Key，`.env.example` 才是模板 |
| `data/example/` | ✅ | 公开示例文档 |
| `pack/example/` | ✅ | 公开示例知识包 |
| `pack/*.zip` | ❌ | 本地打包产物 |

首次上传：

```powershell
cd D:\rag-agent-secretary
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
