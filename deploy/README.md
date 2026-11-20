# 部署说明（Windows 旧笔记本 + 家庭宽带 + Cloudflare Tunnel）

## 适用场景

一台放在家里的 Windows 笔记本，没有公网 IP、不保证稳定供电，通过
Cloudflare Tunnel 把本机企业微信回调服务暴露到公网。目标是：

* 开机后服务自动启动，进程崩溃自动重启；
* 会话历史落 SQLite，重启进程后上下文不丢；
* 远程可以通过 SSH 排障，但 SSH 必须经过 Cloudflare Access；
* 所有密钥只放在本机 `.env` 和 `%USERPROFILE%\.cloudflared\`，不进仓库。

不适用于多机集群、容器编排或需要水平扩展的场景；本项目明确保持单机单进程。

## 目录结构

```text
deploy/
├─ README.md                     # 本文件：总览、执行顺序、回滚
├─ windows/
│  ├─ README.md                  # NSSM、自启动、电源设置、日常运维
│  └─ install-service.ps1        # 注册 wecom-bot Windows 服务（需管理员）
├─ cloudflared/
│  ├─ README.md                  # 登录、建隧道、DNS、Access、验证
│  └─ config.example.yml         # 隧道 ingress 示例（占位符）
└─ tools/
   └─ healthcheck.ps1            # 请求 /healthz，非 200 退出码为 1
```

`deploy/` 下没有任何明文密钥，脚本也不会写入 `.env`。所有真实值都在本机
手工填写。

## 执行顺序

### 1. 装 Python 与依赖

在仓库根目录（示例 `F:\rag-agent-secretary`）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

要求 Python 3.11+。首次启动会下载本地 embedding 模型，建议先手动跑一次
`scripts\wecom_bot.py` 确认模型可用，再注册服务。

### 2. 配置 `.env`

复制 `.env.example` 为 `.env`，按注释填写模型和企业微信配置。与会话持久化
相关的配置项：

```dotenv
# 会话历史 SQLite 路径；留空使用 data/runtime/conversations.db
CONVERSATION_DB=data/runtime/conversations.db
```

`data/runtime/` 已被 `.gitignore` 忽略，不会被提交。改完 `.env` 后需要
重启服务才会生效。

### 3. 安装 Windows 服务

管理员 PowerShell：

```powershell
cd F:\rag-agent-secretary
.\deploy\windows\install-service.ps1 -NssmPath C:\Tools\nssm\nssm.exe
```

服务名默认 `wecom-bot`，日志在 `data\runtime\logs\wecom-bot.*.log`。
详细参数、自动登录、关闭睡眠和 BIOS 来电自启见
[`windows/README.md`](windows/README.md)。

### 4. 配置 Cloudflare Tunnel

按 [`cloudflared/README.md`](cloudflared/README.md) 依次执行：

```powershell
cloudflared tunnel login
cloudflared tunnel create rag-secretary
cloudflared tunnel route dns rag-secretary wecom.example.com
cloudflared service install
```

其中 `wecom.example.com`、`ssh.example.com` 是占位符，需要换成自己的域名。
SSH 主机名必须先加 Cloudflare Access 策略。

### 5. 验证

```powershell
.\deploy\tools\healthcheck.ps1
curl.exe https://wecom.example.com/healthz
```

两条都应返回 `ok`。最后到企业微信后台把接收消息 URL 设置为
`https://wecom.example.com/wecom`，再发一条消息确认能收到回复。

重启验证（验收「重启后会话历史不丢」）：

```powershell
Restart-Service -Name wecom-bot
```

再次对话，服务应能续接重启前的上下文；SQLite 文件默认在
`data\runtime\conversations.db`。

## 回滚

按影响范围从小到大：

1. **只回滚代码**：

   ```powershell
   cd F:\rag-agent-secretary
   git switch main
   git pull --ff-only
   Restart-Service -Name wecom-bot
   ```

   如果之前用别的分支跑过，先记下当前分支名，回滚后可以 `git switch` 回去。

2. **停止并移除 Windows 服务**：

   ```powershell
   C:\Tools\nssm\nssm.exe stop wecom-bot
   C:\Tools\nssm\nssm.exe remove wecom-bot confirm
   ```

3. **移除 Cloudflare 服务**：

   ```powershell
   Stop-Service cloudflared
   cloudflared service uninstall
   ```

   隧道和 DNS 记录需要到 Cloudflare 控制台删除，避免留下悬空入口。

4. **数据备份 / 回退**：先 `Stop-Service wecom-bot`，再复制
   `data\runtime\conversations.db`（以及同目录的 `-wal`、`-shm` 文件）。
   会话历史可以丢弃，但不要误删 `data/docs/` 里的个人文档。

回滚不需要改 `.gitignore`，也不要提交 `.env`、`data/runtime/` 或
`pack/*.zip`。
