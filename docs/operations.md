# 运维手册：启动 / 停止后端（测试环境）

当前测试后端跑在 NUC 上，**手动启动，没有开机自启**。电脑重启后需要按下面的步骤重新启动。

## 访问地址

| 项目 | 地址 |
|---|---|
| API | `http://nuc.tailcfa4ec.ts.net:8000/api/v1` |
| Swagger 文档 | `http://nuc.tailcfa4ec.ts.net:8000/docs` |
| 健康检查 | `http://nuc.tailcfa4ec.ts.net:8000/api/v1/health` |
| 测试账号 | `admin` / `password` |

只有登录了同一个 Tailscale 账号的设备才能访问（手机需要装 Tailscale App）。

---

## 启动

```bash
cd ~/dev/BillSplit
HOST=$(tailscale ip -4) scripts/start.sh
```

成功会输出：

```
started (pid 12345), log: data/server.log
```

- `HOST=$(tailscale ip -4)`：只监听本机的 Tailscale IP（目前是 `100.80.80.2`），tailnet 里的设备能访问，公网和局域网访问不到。
- **不加 `HOST`** 直接运行 `scripts/start.sh` 时，只监听 `127.0.0.1`，只有本机能访问，其他设备连不上。
- 换端口：`PORT=9000 HOST=$(tailscale ip -4) scripts/start.sh`。改了端口，访问地址里的 `8000` 也要跟着改。
- 已经在运行时再执行一次，会提示 `already running (pid ...)`，不会重复启动。
- 脚本会等后端健康检查通过再返回（最多约 15 秒）。如果输出 `did not become healthy`，请看下面的"查看日志"。

## 停止

```bash
cd ~/dev/BillSplit
scripts/stop.sh
```

输出 `stopped` 表示已停止，`not running` 表示本来就没在运行。

## 重启（例如更新代码之后）

```bash
cd ~/dev/BillSplit
git pull                                   # 如有代码更新
.venv/bin/pip install -e ".[dev]"          # 如有依赖更新
scripts/stop.sh && HOST=$(tailscale ip -4) scripts/start.sh
```

数据库结构变更（迁移）会在启动时自动执行。

## 查看状态

```bash
curl http://nuc.tailcfa4ec.ts.net:8000/api/v1/health
# {"status":"ok","version":"0.1.0","ocr_enabled":true,"storage":"local"}
```

能返回 JSON 就说明在正常运行。

## 查看日志

```bash
tail -f ~/dev/BillSplit/data/server.log      # 实时查看，Ctrl+C 退出
tail -n 100 ~/dev/BillSplit/data/server.log  # 看最后 100 行
```

---

## 文件说明

| 路径 | 内容 |
|---|---|
| `.env` | 配置：JWT 密钥、管理员账号、币种等。**不会提交到 git** |
| `data/billsplit.db` | SQLite 数据库（所有数据） |
| `data/files/` | 上传的发票和收据（没有配置 R2 时存在这里） |
| `data/server.log` | 运行日志 |
| `data/server.pid` | 当前进程号，`stop.sh` 用它来停止进程 |

## 常见问题

**启动时提示 `did not become healthy`**
看 `data/server.log` 最后几行。常见原因：
- 端口被占用：用 `ss -ltn | grep 8000` 检查，换一个 `PORT` 即可。
- Tailscale 没连上：运行 `tailscale status` 检查，必要时 `sudo tailscale up`。

**其他设备打不开地址**
1. 那台设备有没有登录 Tailscale？
2. 启动时有没有加 `HOST=$(tailscale ip -4)`？不加的话只有本机能访问。
3. 在 NUC 上运行 `curl http://nuc.tailcfa4ec.ts.net:8000/api/v1/health` 看本机是否正常。

**`stop.sh` 显示 `not running`，但端口还被占用**
可能是 pid 文件丢了。用 `ss -ltnp | grep 8000` 找到进程号，再 `kill <进程号>`。

**想清空测试数据重新开始**
```bash
scripts/stop.sh
rm -rf data/billsplit.db* data/files
HOST=$(tailscale ip -4) scripts/start.sh   # 会重建数据库，并按 .env 重新创建管理员
```
⚠️ 这会删除所有账单、用户和上传的文件，无法恢复。

**修改管理员密码**
`.env` 里的 `ADMIN_PASSWORD` 只在第一次建库时生效。之后要改密码，请登录后调用修改密码接口（`POST /api/v1/auth/change-password`），在 Swagger 文档页面里就能操作。

---

## 以后改成 HTTPS（可选）

1. 在 Tailscale 后台开启 Serve：https://login.tailscale.com/f/serve?node=n6gkUDDhk611CNTRL
2. 在 Tailscale 后台的 DNS 页面开启 HTTPS Certificates。
3. 改为只监听本机：`scripts/stop.sh && scripts/start.sh`（不加 `HOST`）。
4. 运行 `sudo tailscale serve --bg 8000`。

之后地址变为 `https://nuc.tailcfa4ec.ts.net/api/v1`（没有端口号），需要通知前端更新。
