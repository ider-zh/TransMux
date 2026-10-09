# 当前机器部署（v2）

当前实例已切换为局域网访问：<http://192.168.1.229:8765>。
服务通过 `~/.config/systemd/user/transmux-local.service.d/lan.conf` 设置监听地址与允许的 Host，继续使用同一 `data-v2/` 数据目录。IP 变化后需更新该文件并执行 `systemctl --user daemon-reload` 和重启服务。当前仅监听该 LAN 地址，本机也使用此地址访问。

以下为默认本机部署步骤；重新运行脚本会保留上述 LAN 配置。

在项目根目录的宿主机终端运行：

```bash
bash scripts/deploy-local.sh
```

要求 Linux 用户级 systemd、Python 3.11+、可访问 Python 包源，且已安装并登录 Codex 或 CodeBuddy。脚本安装基础依赖到 `.venv`，创建并启动 `transmux-local.service`，检查 `/api/health`。v2 不需要本地向量模型。8765 端口须空闲。

访问 <http://127.0.0.1:8765>。默认仅本机访问，数据持久化到项目的 `data-v2/`。脚本保留安装时的 PATH 供服务查找 CLI；更换 Node 或 CLI 安装路径后重新运行脚本。

```bash
systemctl --user status transmux-local.service
journalctl --user -u transmux-local.service -n 100 --no-pager
systemctl --user restart transmux-local.service
systemctl --user disable --now transmux-local.service
```

服务随用户级 systemd 启动；若需未登录时开机启动，需在宿主机配置该用户的 lingering。

无 systemd 时，安装好依赖后可前台启动：

```bash
bash scripts/run-local.sh
```

Agent 代理配置按需写入项目 `.env`；不要直接复制示例中的其他机器 IP。DOC 转换与 PDF 预览需要 LibreOffice。

用户级 systemd 服务不会自动继承终端中的 `http_proxy` / `https_proxy`。本机已将终端使用的代理保存为 `.env` 中的 `TRANSMUX_CODEX_HTTP_PROXY` 和 `TRANSMUX_CODEX_HTTPS_PROXY`，仅供 Codex 子进程使用。该文件不进入 Git，后续 CLI 调用自动读取，修改后无需重启。遇到“终端可调用、网页任务超时”时，应首先核对这些设置。
