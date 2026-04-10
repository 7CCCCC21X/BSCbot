# BSC IDO Telegram Monitor Bot

A Telegram bot for monitoring **BSC (Binance Smart Chain)** factory/deployer contracts that emit:

```solidity
event NewIDOContract(address indexed idoAddress);
```

When a new IDO contract is detected, the bot pushes a notification with:
- Token name (auto-detected from on-chain data)
- Token contract address
- IDO start/end time
- Transaction hash (BscScan link)

---

## Features

- Auto-detect token name, contract address, start/end time from transaction input
- Chinese commands + Telegram menu
- Batch import multiple monitoring addresses
- Labels, pause/resume support
- `/checktx` to check if a transaction would be detected
- `/debugtx` to debug transaction parsing step by step
- SQLite for persistent storage
- Ready for Railway deployment

---

## Project Structure

```text
.
├── bsc_ido_tg_bot.py
├── requirements.txt
├── railway.json
├── .env.example
├── .gitignore
├── README.md
└── README_zh.md
```

---

## Railway Deployment

### 1) Push to GitHub
Create a new GitHub repository and push all files.

### 2) Create Railway Project
- Go to Railway
- New Project → Deploy from GitHub repo
- Select your repository

### 3) Add Environment Variables
In Railway Variables:

```env
BOT_TOKEN=your_telegram_bot_token
BSC_RPC_URL=your_bsc_rpc_url
DB_PATH=/data/watchers.db
SCAN_INTERVAL=5
CONFIRMATIONS=2
BLOCK_CHUNK=200
DEFAULT_LOOKBACK=0
LOG_LEVEL=INFO
GROUP_LINK=https://t.me/xiaoccaac
GROUP_NAME=小C聊天群
```

### 4) Mount Volume
Mount a volume at `/data` so the SQLite database persists across deployments.

### 5) Deploy
Railway auto-builds and starts. The start command is in `railway.json`:

```bash
python bsc_ido_tg_bot.py
```

---

## Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your BOT_TOKEN and BSC_RPC_URL
python bsc_ido_tg_bot.py
```

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/help` | Show help |
| `/add 0xAddr Label` | Add monitoring address |
| `/import` | Batch import addresses |
| `/list` | View monitoring list |
| `/del 0xAddr` | Delete monitoring address |
| `/pause 0xAddr` | Pause monitoring |
| `/resume 0xAddr` | Resume monitoring |
| `/checktx 0xHash` | Check if a tx would be detected |
| `/debugtx 0xHash` | Debug transaction parsing |
| `/status` | Show bot status |
| `/chatid` | Show chat ID |

---

## Batch Import Examples

### Multi-line

```text
/import
0x1111111111111111111111111111111111111111|ProjectA
0x2222222222222222222222222222222222222222|ProjectB
0x3333333333333333333333333333333333333333|ProjectC
```

### Single line (comma separated)

```text
/import 0x111...,0x222...,0x333...
```

---

## Prerequisites

This bot monitors factory/deployer contracts that emit `NewIDOContract(address)` events.

If you need to monitor EOA wallet deployments, you would need a different approach (e.g., monitoring contract creation transactions or traces).

---

## Recommended RPC

Use an RPC endpoint that supports `eth_getLogs`. Some public BSC endpoints may have this disabled by default.
