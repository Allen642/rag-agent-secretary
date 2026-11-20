# Windows 服务部署（NSSM）

把 `scripts/wecom_bot.py` 注册成开机自启的 Windows 服务，进程崩溃后自动
重启，日志写到 `data\runtime\logs\`。

## 1. 准备 Python 环境

在仓库根目录（示例为 `F:\rag-agent-secretary`）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

确认虚拟环境里的解释器存在：

```powershell
Test-Path .\.venv\Scripts\python.exe
```

再按仓库根目录的 `.env.example` 创建 `.env`，至少填好企业微信和模型配置。
`.env` 不要提交，也不要写进本目录的任何脚本。

## 2. 下载并放置 NSSM

1. 打开 <https://nssm.cc/download>，下载 NSSM 2.24；
2. 解压后取 `win64\nssm.exe`；
3. 放到固定目录，例如 `C:\Tools\nssm\nssm.exe`；
4. 确认：

```powershell
C:\Tools\nssm\nssm.exe version
```

> `nssm.exe` 不进仓库。也可以把所在目录加入 `PATH`，这样脚本可以不传
> `-NssmPath`；否则请始终显式传路径。

## 3. 以管理员身份安装服务

右键「Windows PowerShell」→「以管理员身份运行」，然后：

```powershell
cd F:\rag-agent-secretary
.\deploy\windows\install-service.ps1 -NssmPath C:\Tools\nssm\nssm.exe
```

默认值（与脚本参数一致）：

| 参数 | 默认值 |
| --- | --- |
| `-ServiceName` | `wecom-bot` |
| `-AppDirectory` | 脚本上两级，即仓库根目录 |
| `-PythonPath` | `<AppDirectory>\.venv\Scripts\python.exe` |
| `-ScriptPath` | `<AppDirectory>\scripts\wecom_bot.py` |
| `-NssmPath` | 先在 `PATH` 里找 `nssm.exe`，找不到则报错 |

需要自定义时示例：

```powershell
.\deploy\windows\install-service.ps1 `
  -ServiceName wecom-bot `
  -AppDirectory F:\rag-agent-secretary `
  -PythonPath F:\rag-agent-secretary\.venv\Scripts\python.exe `
  -ScriptPath F:\rag-agent-secretary\scripts\wecom_bot.py `
  -NssmPath C:\Tools\nssm\nssm.exe
```

脚本做的事：

* 服务已存在则先停止并删除，再重新注册（可重复执行）；
* `AppDirectory` 指向仓库根，服务能读到 `.env` 和 `data\`；
* 标准输出 / 错误分别写入
  `data\runtime\logs\wecom-bot.out.log` 与 `wecom-bot.err.log`；
* `AppExit Default Restart`，崩溃后 5 秒重启；
* 启动类型 `SERVICE_AUTO_START`，开机自动运行；
* 最后立即启动服务。

服务以 LocalSystem 运行，仓库目录只需可读、`data\runtime\` 可写。不要把
仓库放在需要交互式登录才能访问的网络盘上。

## 4. 服务账户与模型缓存

NSSM 默认以 LocalSystem 账户运行服务，它的 `%USERPROFILE%` 是：

```text
C:\Windows\System32\config\systemprofile
```

因此服务看不到你登录用户目录下的 Hugging Face 缓存
（`C:\Users\<USER>\.cache\huggingface\hub`）。首次启动时 LocalSystem
会重新下载约 192MB 的 embedding 模型；如果家里网络当时不可用，服务会
启动失败并不断重启。

任选一种解法。

### a) 推荐：把缓存放到 ProgramData（在 `.env` 中指定 HF_HOME）

在仓库根目录的 `.env` 增加：

```dotenv
HF_HOME=C:\ProgramData\huggingface
```

创建目录，并把已有缓存复制过去（目录不存在就先创建）：

```powershell
New-Item -ItemType Directory -Force C:\ProgramData\huggingface\hub
Copy-Item -Recurse -Force "$env:USERPROFILE\.cache\huggingface\hub\*" C:\ProgramData\huggingface\hub
```

`huggingface_hub` 会把模型放在 `%HF_HOME%\hub`，所以目标路径是
`C:\ProgramData\huggingface\hub`。LocalSystem 对该目录默认可读。

### b) 或者复制到 LocalSystem 的用户目录

不设置 `HF_HOME` 时，LocalSystem 会使用：

```text
C:\Windows\System32\config\systemprofile\.cache\huggingface
```

可以从当前用户复制：

```powershell
New-Item -ItemType Directory -Force C:\Windows\System32\config\systemprofile\.cache\huggingface
Copy-Item -Recurse -Force "$env:USERPROFILE\.cache\huggingface\hub" C:\Windows\System32\config\systemprofile\.cache\huggingface\
```

改完 `.env` 或复制完缓存后重启服务：

```powershell
Restart-Service -Name wecom-bot
```

### 验证不再联网下载

模型就绪后，在 `.env` 增加：

```dotenv
TRANSFORMERS_OFFLINE=1
```

再重启服务并观察日志：

```powershell
Restart-Service -Name wecom-bot
Get-Content .\data\runtime\logs\wecom-bot.err.log -Tail 50 -Wait
```

服务能正常启动、日志里没有下载进度，就说明缓存路径正确、后续不会再联网
拉模型。如果启动时报找不到模型，先确认 `HF_HOME` 指向的目录下确实有
`hub`，且 LocalSystem 可读。

## 5. 关闭自动睡眠 / 休眠

旧笔记本默认会睡眠，服务会跟着断线。

图形界面：设置 → 系统 → 电源和电池 → 屏幕和睡眠 → 「接通电源时」把
「使设备进入睡眠状态」设为「从不」。

命令行（管理员）：

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

如果笔记本有厂商电源管理软件，也要把「合盖动作」设为「不采取任何操作」，
否则合盖后仍会断网。

## 6. 设置自动登录（可选但推荐）

Windows 服务本身不依赖用户登录，但自动登录方便远程排障和手动启动 GUI
工具。

1. `Win + R` 输入 `netplwiz` 回车；
2. 选中要自动登录的账户，取消勾选「要使用本计算机，用户必须输入用户名和
   密码」；
3. 点「应用」，输入该账户密码确认；
4. 重启验证是否直接进入桌面。

也可以使用微软 Sysinternals 的 Autologon 工具。自动登录会把密码保存在本机
凭据中，请确保这台机器只放在可信的家庭网络里。

## 7. BIOS 来电自启

断电恢复后需要机器自己开机：

1. 重启进入 BIOS/UEFI（常见按键 `F2` / `Del` / `F10`）；
2. 找到 `Restore on AC Power Loss` / `AC Power Recovery` / `After Power Loss`；
3. 设为 `Power On`（或 `Last State`）；
4. 保存退出。

不同品牌名称不同，找不到时优先查主板/笔记本型号的手册。

## 8. 日常运维

```powershell
Get-Service -Name wecom-bot
Restart-Service -Name wecom-bot
Stop-Service -Name wecom-bot
```

查看日志（`-Wait` 会持续跟踪）：

```powershell
Get-Content .\data\runtime\logs\wecom-bot.out.log -Tail 50 -Wait
Get-Content .\data\runtime\logs\wecom-bot.err.log -Tail 50
```

健康检查：

```powershell
.\deploy\tools\healthcheck.ps1
```

返回 `ok` 且退出码为 0 表示 `http://127.0.0.1:8080/healthz` 正常。可以让
任务计划程序每 5 分钟运行一次：

* 程序：`powershell.exe`
* 参数：
  `-NoProfile -ExecutionPolicy Bypass -File "F:\rag-agent-secretary\deploy\tools\healthcheck.ps1"`
* 「条件」里勾选「不管用户是否登录都要运行」。

## 9. 卸载

管理员 PowerShell：

```powershell
C:\Tools\nssm\nssm.exe stop wecom-bot
C:\Tools\nssm\nssm.exe remove wecom-bot confirm
```

服务删除不会删除代码、`.env` 或 `data\runtime\conversations.db`，需要彻底
清理时再手动处理。
