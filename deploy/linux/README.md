# Linux 服务部署（systemd）：智能机器人长连接

把 `scripts/wecom_aibot.py` 注册成 systemd 服务，开机自启、崩溃自动重启，
通过 `journalctl` 查看日志。长连接由本机主动发起，服务器不需要公网 IP、
不需要开放端口。

> 下文路径 `/opt/rag-agent-secretary` 和运行用户 `root` 都是**占位符**。
> 请按实际部署位置和低权限账号修改 `wecom-aibot.service`；不建议长期用
> root 运行。

## 1. 准备代码与虚拟环境

以占位路径为例：

```bash
sudo mkdir -p /opt/rag-agent-secretary
sudo chown -R "$USER" /opt/rag-agent-secretary
git clone <仓库地址> /opt/rag-agent-secretary
cd /opt/rag-agent-secretary

python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt
```

## 2. 配置 `.env`

```bash
cp .env.example .env
# 编辑 .env，至少填写：
#   WECOM_AIBOT_BOT_ID=...
#   WECOM_AIBOT_SECRET=...
#   以及模型 API Key（DEEPSEEK_API_KEY 或 ZHIPU_API_KEY）
```

程序通过 `python-dotenv` 自己读取仓库根目录的 `.env`，所以 unit 里不需要
`EnvironmentFile`。`.env` 权限建议设为 `600`，不要提交到 Git。

先手工校验配置：

```bash
cd /opt/rag-agent-secretary
.venv/bin/python scripts/wecom_aibot.py --check
```

输出“配置检查通过”说明 Bot ID / Secret 已读取；缺配置会列出变量名并
返回退出码 1。

## 3. 安装并启动服务

```bash
sudo cp deploy/linux/wecom-aibot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wecom-aibot.service
```

确认状态：

```bash
systemctl status wecom-aibot.service
```

## 4. 查看日志

```bash
# 实时跟踪
journalctl -u wecom-aibot.service -f

# 最近 200 行
journalctl -u wecom-aibot.service -n 200 --no-pager
```

启动后先看到：

```text
智能机器人长连接启动：bot_id=...
WebSocket 已连接
认证成功，机器人已上线
```

**只看到“智能机器人长连接启动：bot_id=...”不算启动成功**，那只表示进程
读到了配置并开始连接；必须看到 **“认证成功，机器人已上线”** 才算真正上线，
此时给机器人发消息才会有回复。若一直停在“WebSocket 已连接”，检查 Bot ID /
Secret 是否正确、该机器人是否已被其他长连接占用。

日志只包含 Bot ID，不包含 Secret。断线时会出现 `连接断开：...` 和
`第 N 次重连`，认证失败等异常会出现 `长连接错误：...`。

## 5. 停止 / 重启 / 卸载

```bash
sudo systemctl stop wecom-aibot.service
sudo systemctl restart wecom-aibot.service
sudo systemctl disable --now wecom-aibot.service
```

## 注意事项

- **同一机器人同一时间只能有一个长连接**：ECS 和家里不能同时运行，否则
  后连上的会顶掉前一个；
- 修改代码或 `.env` 后需要 `sudo systemctl restart wecom-aibot.service`；
- 服务使用 `Restart=always`，网络中断由 SDK 自动重连；若进程被 systemd
  重启，也会在 5 秒后重新连接；
- 长连接暂不支持 `/code`，该命令只会收到“长连接模式暂不支持 /code”的回复。
