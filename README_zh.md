# BSC IDO Telegram 监控机器人（Railway / GitHub 版）

这是一个适用于 **BSC 链** 的 Telegram 中文机器人，专门监控多个工厂/部署器合约地址发出的：

```solidity
event NewIDOContract(address indexed idoAddress);
```

一旦发现新 IDO 合约，就会把：
- 工厂地址
- 新 IDO 合约地址
- 交易哈希
- 区块号
- 同交易里的 `OwnershipTransferred`

一起推送到 Telegram。

---

## 功能

- 中文命令
- 支持导入多个地址监控
- 支持备注
- 支持暂停 / 恢复
- 使用 SQLite 保存监控列表
- 适合部署到 Railway

---

## 目录结构

```text
.
├── bsc_ido_tg_bot.py
├── requirements.txt
├── railway.json
├── .env.example
├── .gitignore
└── README_zh.md
```

---

## Railway 部署步骤

### 1）上传到 GitHub
新建一个 GitHub 仓库，把这些文件全部推上去。

### 2）在 Railway 创建项目
- 进入 Railway
- New Project
- Deploy from GitHub repo
- 选择你的仓库

### 3）添加环境变量
在 Railway 的 Variables 里添加：

```env
BOT_TOKEN=你的TG机器人Token
# 主用 BSC RPC（必填）。也可以在这里用逗号填多个，程序会把第一个当主用，其余当备用。
BSC_RPC_URL=你的BSC RPC
# 额外备用 RPC，逗号分隔（可选）。任一 RPC 失败时自动切换到下一个。
# 例：BSC_RPC_URLS=https://bsc-dataseed1.binance.org,https://bsc-dataseed2.binance.org,https://bsc.publicnode.com
BSC_RPC_URLS=
DB_PATH=/data/watchers.db
SCAN_INTERVAL=5
CONFIRMATIONS=2
BLOCK_CHUNK=200
DEFAULT_LOOKBACK=0
LOG_LEVEL=INFO
```

### 4）挂载 Volume
强烈建议给这个服务挂一个 Volume，并把挂载路径设为：

```text
/data
```

这样 SQLite 数据库会持久化保存，不会因为重新部署丢失监控列表。

### 5）部署
Railway 会自动构建并启动，启动命令已经写在 `railway.json`：

```bash
python bsc_ido_tg_bot.py
```

---

## 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bsc_ido_tg_bot.py
```

---

## 机器人命令

- `/start`
- `/help`
- `/chatid`
- `/add 0x地址 备注`
- `/import`
- `/list`
- `/del 0x地址`
- `/pause 0x地址`
- `/resume 0x地址`
- `/status`

---

## 批量导入示例

### 方式 1：多行

```text
/import
0x1111111111111111111111111111111111111111|项目A
0x2222222222222222222222222222222222222222|项目B
0x3333333333333333333333333333333333333333|项目C
```

### 方式 2：单行逗号分隔

```text
/import 0x1111111111111111111111111111111111111111,0x2222222222222222222222222222222222222222
```

---

## 适用前提

这个脚本默认你监控的是：

- 工厂合约
- 管理合约
- 部署器合约

并且这些地址会触发：

```solidity
event NewIDOContract(address indexed idoAddress);
```

如果你想监控的是 **EOA 钱包直接部署合约**，那就不是这套逻辑了，要改成监控合约创建交易或 trace。

---

## 推荐

BSC 上做日志监听时，尽量使用：
- 支持 `eth_getLogs` 的 RPC
- 或者 WebSocket RPC

因为部分官方 Mainnet endpoint 默认关闭了 `eth_getLogs`。
