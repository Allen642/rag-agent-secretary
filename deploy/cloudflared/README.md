# Cloudflare Tunnel（Windows）

面向「家庭宽带 + 没有公网 IP」的场景：把本机 `127.0.0.1:8080` 的企业微信
回调暴露到 `https://wecom.example.com`，同时可选暴露 SSH。

> 所有示例都用占位符：`<TUNNEL_UUID>`、`<USER>`、`example.com`。不要把
> `cert.pem`、`<TUNNEL_UUID>.json` 或真实域名提交到仓库。

## 1. 安装 cloudflared

任选一种：

```powershell
winget install --id Cloudflare.cloudflared
```

或从 <https://github.com/cloudflare/cloudflared/releases> 下载
`cloudflared-windows-amd64.exe`，改名为 `cloudflared.exe` 放到
`C:\Cloudflared\bin\`，并把该目录加入 `PATH`。

确认安装：

```powershell
cloudflared --version
```

## 2. 登录并创建隧道

```powershell
cloudflared tunnel login
cloudflared tunnel create rag-secretary
```

`login` 会在浏览器里让你选择域名；`create` 会输出 Tunnel UUID，并在
`C:\Users\<USER>\.cloudflared\<TUNNEL_UUID>.json` 生成凭据文件。记下 UUID。

## 3. 写配置

复制示例：

```powershell
Copy-Item .\deploy\cloudflared\config.example.yml "$env:USERPROFILE\.cloudflared\config.yml"
```

编辑 `C:\Users\<USER>\.cloudflared\config.yml`，替换：

* `tunnel`：上一步的 Tunnel UUID；
* `credentials-file`：凭据 JSON 的真实路径；
* `wecom.example.com` / `ssh.example.com`：你自己的域名。

校验配置：

```powershell
cloudflared tunnel ingress validate --config "$env:USERPROFILE\.cloudflared\config.yml"
```

## 4. 绑定域名

```powershell
cloudflared tunnel route dns rag-secretary wecom.example.com
cloudflared tunnel route dns rag-secretary ssh.example.com
```

## 5. 安装为 Windows 服务

Windows 服务以 LocalSystem 运行，默认读取
`C:\Windows\System32\config\systemprofile\.cloudflared\config.yml`，而不是
当前用户的目录。以管理员身份执行：

```powershell
New-Item -ItemType Directory -Force C:\Windows\System32\config\systemprofile\.cloudflared
Copy-Item "$env:USERPROFILE\.cloudflared\config.yml" C:\Windows\System32\config\systemprofile\.cloudflared\config.yml
Copy-Item "$env:USERPROFILE\.cloudflared\<TUNNEL_UUID>.json" C:\Windows\System32\config\systemprofile\.cloudflared\<TUNNEL_UUID>.json
```

同时把 systemprofile 下 `config.yml` 里的 `credentials-file` 改成：

```yaml
credentials-file: C:\Windows\System32\config\systemprofile\.cloudflared\<TUNNEL_UUID>.json
```

然后安装并启动服务：

```powershell
cloudflared service install
Start-Service cloudflared
```

部分 cloudflared 版本注册的 `ImagePath` 不带 `tunnel run`，表现为服务
Running 但隧道没连上。用管理员 PowerShell 检查：

```powershell
Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\cloudflared" ImagePath
```

如果不是下面这种形式，就修正它：

```powershell
Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\cloudflared" ImagePath `
  'C:\Cloudflared\bin\cloudflared.exe --config=C:\Windows\System32\config\systemprofile\.cloudflared\config.yml tunnel run'
Restart-Service cloudflared
```

查看隧道状态：

```powershell
cloudflared tunnel info rag-secretary
Get-Service cloudflared
```

## 6. SSH 必须加 Access 策略

`ssh.example.com` 一旦暴露到公网，任何拿到域名的人都能尝试登录。必须先到
Cloudflare Zero Trust → Access → Applications 为 `ssh.example.com` 建一个
Self-hosted 应用，配置至少一条策略（例如只允许自己的邮箱），并开启
One-time PIN 或 SSO。**不要跳过这一步，也不要只靠 SSH 密码。**

企业微信回调域名 `wecom.example.com` 不要加 Access 登录页，否则企业微信
服务器无法完成回调；它由企业微信签名校验保护。

## 7. 验证

本机服务先确认可用：

```powershell
.\deploy\tools\healthcheck.ps1
```

再通过隧道验证：

```powershell
curl.exe https://wecom.example.com/healthz
```

返回 `ok` 即说明 Tunnel → 本机 8080 链路正常。最后到企业微信后台把回调
URL 填成 `https://wecom.example.com/wecom`。

## 8. 卸载 / 回滚

```powershell
Stop-Service cloudflared
cloudflared service uninstall
```

需要保留的只有 `C:\Users\<USER>\.cloudflared\` 下的配置和凭据；删除前先
确认不再使用该隧道。
