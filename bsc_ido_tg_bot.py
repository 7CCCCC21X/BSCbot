#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BSC IDO 监控 Telegram 机器人

功能：
1. 监控多个工厂/部署器合约地址发出的 NewIDOContract(address idoAddress) 事件
2. 抓到新 IDO 合约后，推送到 Telegram
3. 支持中文命令：/add /import /list /del /pause /resume /status /chatid /help
4. 可选：从同一笔交易的 receipt 中解析新合约上的 OwnershipTransferred 事件

说明：
- 这个脚本默认假设“被监控地址”会发出如下事件：
    event NewIDOContract(address indexed idoAddress)
- 你的样例日志正是这个模式，最适合用事件日志来做监控。
- 需要一个支持 eth_getLogs 的 BSC RPC；部分官方 Mainnet RPC 默认禁用了 eth_getLogs。
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes
from web3 import Web3
from web3.exceptions import Web3RPCError

# 允许本地通过 .env 启动；Railway 会直接注入环境变量
load_dotenv()


# =========================
# 配置
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BSC_RPC_URL = os.getenv("BSC_RPC_URL", "")
DB_PATH = os.getenv("DB_PATH", "watchers.db")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "5"))
CONFIRMATIONS = int(os.getenv("CONFIRMATIONS", "2"))
BLOCK_CHUNK = int(os.getenv("BLOCK_CHUNK", "200"))
DEFAULT_LOOKBACK = int(os.getenv("DEFAULT_LOOKBACK", "0"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

BSCSCAN_TX = os.getenv("BSCSCAN_TX", "https://bscscan.com/tx/")
BSCSCAN_ADDRESS = os.getenv("BSCSCAN_ADDRESS", "https://bscscan.com/address/")

# 事件签名
NEW_IDO_EVENT = "NewIDOContract(address)"
OWNERSHIP_TRANSFERRED_EVENT = "OwnershipTransferred(address,address)"

NEW_IDO_TOPIC = Web3.keccak(text=NEW_IDO_EVENT).hex()
OWNERSHIP_TRANSFERRED_TOPIC = Web3.keccak(text=OWNERSHIP_TRANSFERRED_EVENT).hex()

ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("bsc-ido-bot")


# =========================
# 基础工具
# =========================
def ensure_parent_dir(file_path: str) -> None:
    parent = Path(file_path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


if not BOT_TOKEN:
    raise RuntimeError("缺少 BOT_TOKEN 环境变量")
if not BSC_RPC_URL:
    raise RuntimeError("缺少 BSC_RPC_URL 环境变量")

ensure_parent_dir(DB_PATH)

w3 = Web3(Web3.HTTPProvider(BSC_RPC_URL, request_kwargs={"timeout": 30}))
if not w3.is_connected():
    raise RuntimeError("BSC RPC 连接失败，请检查 BSC_RPC_URL")


# =========================
# 数据结构
# =========================
@dataclass
class Watcher:
    id: int
    chat_id: int
    address: str
    address_lc: str
    label: str
    last_scanned_block: int
    enabled: int
    created_at: int


# =========================
# DB 层
# =========================
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watchers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                address_lc TEXT NOT NULL,
                label TEXT DEFAULT '',
                last_scanned_block INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                UNIQUE(chat_id, address_lc)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_events (
                watcher_id INTEGER NOT NULL,
                tx_hash TEXT NOT NULL,
                log_index INTEGER NOT NULL,
                PRIMARY KEY (watcher_id, tx_hash, log_index)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_watchers_enabled ON watchers(enabled, last_scanned_block)"
        )
        conn.commit()


def checksum_address(addr: str) -> str:
    return Web3.to_checksum_address(addr)


def topic_to_address(topic: bytes | str) -> str:
    if isinstance(topic, str):
        hex_str = topic[2:] if topic.startswith("0x") else topic
    else:
        hex_str = topic.hex()
    return Web3.to_checksum_address("0x" + hex_str[-40:])


def extract_address(text: str) -> Optional[str]:
    m = ADDRESS_RE.search(text or "")
    if not m:
        return None
    return checksum_address(m.group(0))


def current_unix() -> int:
    return int(time.time())


def row_to_watcher(row: sqlite3.Row) -> Watcher:
    return Watcher(
        id=row["id"],
        chat_id=row["chat_id"],
        address=row["address"],
        address_lc=row["address_lc"],
        label=row["label"],
        last_scanned_block=row["last_scanned_block"],
        enabled=row["enabled"],
        created_at=row["created_at"],
    )


def add_watcher(chat_id: int, address: str, label: str, last_scanned_block: int) -> Tuple[bool, str]:
    address = checksum_address(address)
    address_lc = address.lower()
    label = (label or "").strip()
    with db_conn() as conn:
        cur = conn.execute(
            "SELECT 1 FROM watchers WHERE chat_id = ? AND address_lc = ?",
            (chat_id, address_lc),
        )
        if cur.fetchone():
            return False, f"地址已存在：{address}"

        conn.execute(
            """
            INSERT INTO watchers(chat_id, address, address_lc, label, last_scanned_block, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (chat_id, address, address_lc, label, max(last_scanned_block, 0), current_unix()),
        )
        conn.commit()
    return True, address


def delete_watcher(chat_id: int, address: str) -> bool:
    address = checksum_address(address)
    with db_conn() as conn:
        cur = conn.execute(
            "DELETE FROM watchers WHERE chat_id = ? AND address_lc = ?",
            (chat_id, address.lower()),
        )
        conn.commit()
        return cur.rowcount > 0


def set_enabled(chat_id: int, address: str, enabled: bool) -> bool:
    address = checksum_address(address)
    with db_conn() as conn:
        cur = conn.execute(
            "UPDATE watchers SET enabled = ? WHERE chat_id = ? AND address_lc = ?",
            (1 if enabled else 0, chat_id, address.lower()),
        )
        conn.commit()
        return cur.rowcount > 0


def get_watchers_by_chat(chat_id: int) -> List[Watcher]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM watchers WHERE chat_id = ? ORDER BY id DESC",
            (chat_id,),
        ).fetchall()
    return [row_to_watcher(r) for r in rows]


def get_enabled_watchers() -> List[Watcher]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM watchers WHERE enabled = 1 ORDER BY id ASC"
        ).fetchall()
    return [row_to_watcher(r) for r in rows]


def update_last_scanned_block(watcher_id: int, block_number: int) -> None:
    with db_conn() as conn:
        conn.execute(
            "UPDATE watchers SET last_scanned_block = ? WHERE id = ?",
            (block_number, watcher_id),
        )
        conn.commit()


def is_processed(watcher_id: int, tx_hash: str, log_index: int) -> bool:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_events WHERE watcher_id = ? AND tx_hash = ? AND log_index = ?",
            (watcher_id, tx_hash, log_index),
        ).fetchone()
    return row is not None


def mark_processed(watcher_id: int, tx_hash: str, log_index: int) -> None:
    with db_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_events(watcher_id, tx_hash, log_index) VALUES (?, ?, ?)",
            (watcher_id, tx_hash, log_index),
        )
        conn.commit()


def enabled_count() -> int:
    with db_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM watchers WHERE enabled = 1").fetchone()
    return int(row["c"])


# =========================
# 链上逻辑
# =========================
def get_latest_block() -> int:
    return int(w3.eth.block_number)


def get_logs_for_address(address: str, from_block: int, to_block: int) -> list:
    return w3.eth.get_logs(
        {
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": checksum_address(address),
            "topics": [NEW_IDO_TOPIC],
        }
    )


def get_receipt(tx_hash_hex: str):
    return w3.eth.get_transaction_receipt(tx_hash_hex)


def parse_ownership_transfers_from_receipt(receipt, ido_address: str) -> List[Tuple[str, str]]:
    ido_lc = ido_address.lower()
    items: List[Tuple[str, str]] = []
    for log in receipt.get("logs", []):
        if str(log["address"]).lower() != ido_lc:
            continue
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue
        if topics[0].hex().lower() != OWNERSHIP_TRANSFERRED_TOPIC.lower():
            continue
        prev_owner = topic_to_address(topics[1])
        new_owner = topic_to_address(topics[2])
        items.append((prev_owner, new_owner))
    return items


def parse_new_ido_log(log) -> Tuple[str, str, int, int]:
    topics = log.get("topics", [])
    if len(topics) < 2:
        raise ValueError("NewIDOContract 日志 topics 不完整")
    ido_address = topic_to_address(topics[1])
    tx_hash = log["transactionHash"].hex()
    block_number = int(log["blockNumber"])
    log_index = int(log["logIndex"])
    return ido_address, tx_hash, block_number, log_index


# =========================
# TG 输出
# =========================
def help_text() -> str:
    return (
        "<b>BSC IDO 监控机器人</b>\n\n"
        "适用场景：监控多个工厂/部署器合约地址发出的 <code>NewIDOContract(address)</code> 事件。\n\n"
        "<b>命令列表</b>\n"
        "/start - 显示帮助\n"
        "/help - 显示帮助\n"
        "/chatid - 查看当前聊天 ID\n"
        "/add 地址 备注 - 添加一个监控地址\n"
        "/import - 批量导入多个地址\n"
        "/list - 查看当前聊天已添加的地址\n"
        "/del 地址 - 删除一个监控地址\n"
        "/pause 地址 - 暂停一个地址\n"
        "/resume 地址 - 恢复一个地址\n"
        "/status - 查看机器人运行状态\n\n"
        "<b>批量导入格式</b>\n"
        "直接发送：\n"
        "<code>/import\n"
        "0x1111111111111111111111111111111111111111|项目A\n"
        "0x2222222222222222222222222222222222222222|项目B\n"
        "0x3333333333333333333333333333333333333333</code>\n\n"
        "也支持单行逗号分隔：\n"
        "<code>/import 0x111...,0x222...,0x333...</code>"
    )


def render_notify_message(
    watcher: Watcher,
    ido_address: str,
    tx_hash: str,
    block_number: int,
    ownership_changes: Sequence[Tuple[str, str]],
) -> str:
    watcher_name = watcher.label or watcher.address

    lines = [
        "<b>🔔 发现新的 IDO 合约</b>",
        f"监控对象：<code>{html.escape(watcher_name)}</code>",
        f"工厂地址：<code>{html.escape(watcher.address)}</code>",
        f"新 IDO 合约：<code>{html.escape(ido_address)}</code>",
        f"区块：<code>{block_number}</code>",
        f"交易哈希：<code>{html.escape(tx_hash)}</code>",
    ]

    if ownership_changes:
        lines.append("")
        lines.append("<b>OwnershipTransferred</b>")
        for i, (prev_owner, new_owner) in enumerate(ownership_changes, start=1):
            lines.append(
                f"{i}. <code>{html.escape(prev_owner)}</code> → <code>{html.escape(new_owner)}</code>"
            )

    lines.append("")
    lines.append(f"<a href=\"{html.escape(BSCSCAN_ADDRESS + ido_address)}\">查看合约</a>")
    lines.append(f"<a href=\"{html.escape(BSCSCAN_TX + tx_hash)}\">查看交易</a>")
    return "\n".join(lines)


# =========================
# 命令处理
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(help_text(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(help_text(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.effective_chat:
        await update.message.reply_text(
            f"当前 chat_id：<code>{update.effective_chat.id}</code>",
            parse_mode=ParseMode.HTML,
        )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    if not context.args:
        await update.message.reply_text("用法：/add 地址 备注\n例如：/add 0x1234... Pancake IDO Factory")
        return

    raw_address = context.args[0]
    try:
        address = checksum_address(raw_address)
    except Exception:
        await update.message.reply_text("地址格式不正确，请检查后重试。")
        return

    label = " ".join(context.args[1:]).strip()
    latest = await asyncio.to_thread(get_latest_block)
    start_from = max(latest - CONFIRMATIONS - DEFAULT_LOOKBACK, 0)
    ok, msg = add_watcher(update.effective_chat.id, address, label, start_from)
    if ok:
        await update.message.reply_text(
            f"✅ 已添加监控\n地址：<code>{address}</code>\n备注：<code>{html.escape(label or '-')}</code>\n起扫区块：<code>{start_from}</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(msg)


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    if not context.args:
        await update.message.reply_text("用法：/del 地址")
        return
    try:
        address = checksum_address(context.args[0])
    except Exception:
        await update.message.reply_text("地址格式不正确。")
        return

    if delete_watcher(update.effective_chat.id, address):
        await update.message.reply_text(f"🗑️ 已删除：<code>{address}</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("没有找到这个地址。")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    if not context.args:
        await update.message.reply_text("用法：/pause 地址")
        return
    try:
        address = checksum_address(context.args[0])
    except Exception:
        await update.message.reply_text("地址格式不正确。")
        return
    if set_enabled(update.effective_chat.id, address, False):
        await update.message.reply_text(f"⏸️ 已暂停：<code>{address}</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("没有找到这个地址。")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    if not context.args:
        await update.message.reply_text("用法：/resume 地址")
        return
    try:
        address = checksum_address(context.args[0])
    except Exception:
        await update.message.reply_text("地址格式不正确。")
        return
    if set_enabled(update.effective_chat.id, address, True):
        await update.message.reply_text(f"▶️ 已恢复：<code>{address}</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("没有找到这个地址。")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    watchers = get_watchers_by_chat(update.effective_chat.id)
    if not watchers:
        await update.message.reply_text("当前聊天还没有添加任何监控地址。")
        return

    lines = ["<b>当前监控列表</b>"]
    for i, w in enumerate(watchers, start=1):
        status = "启用" if w.enabled else "暂停"
        label = w.label or "-"
        lines.append(
            f"{i}. <code>{html.escape(w.address)}</code>\n"
            f"   备注：<code>{html.escape(label)}</code>\n"
            f"   状态：<b>{status}</b>\n"
            f"   已扫到：<code>{w.last_scanned_block}</code>"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)


def parse_import_payload(text: str) -> List[Tuple[str, str]]:
    text = (text or "").strip()
    if not text:
        return []

    # 去掉命令头
    if text.startswith("/import"):
        parts = text.split(maxsplit=1)
        text = parts[1] if len(parts) > 1 else ""

    text = text.strip()
    if not text:
        return []

    items: List[Tuple[str, str]] = []

    # 单行逗号分隔的简写模式
    if "\n" not in text and "," in text and "|" not in text:
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            addr = extract_address(part)
            if addr:
                items.append((addr, ""))
        return items

    # 多行模式：地址|备注  或  地址 备注
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        label = ""
        if "|" in line:
            left, right = line.split("|", 1)
            addr = extract_address(left)
            label = right.strip()
        else:
            addr = extract_address(line)
            if addr:
                label = line.replace(addr, "", 1).strip()
        if addr:
            items.append((addr, label))
    return items


async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    raw_text = update.message.text or ""
    items = parse_import_payload(raw_text)
    if not items:
        await update.message.reply_text(
            "批量导入格式不正确。\n\n"
            "示例：\n"
            "/import\n"
            "0x1111111111111111111111111111111111111111|项目A\n"
            "0x2222222222222222222222222222222222222222|项目B"
        )
        return

    latest = await asyncio.to_thread(get_latest_block)
    start_from = max(latest - CONFIRMATIONS - DEFAULT_LOOKBACK, 0)

    added = []
    existed = []
    for addr, label in items:
        ok, msg = add_watcher(update.effective_chat.id, addr, label, start_from)
        if ok:
            added.append((msg, label))
        else:
            existed.append((addr, label))

    lines = [f"导入完成：新增 {len(added)} 个，跳过 {len(existed)} 个"]
    if added:
        lines.append("\n<b>新增：</b>")
        for addr, label in added[:30]:
            lines.append(f"- <code>{html.escape(addr)}</code>  <code>{html.escape(label or '-')}</code>")
    if existed:
        lines.append("\n<b>已存在/跳过：</b>")
        for addr, label in existed[:30]:
            lines.append(f"- <code>{html.escape(addr)}</code>  <code>{html.escape(label or '-')}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    latest = await asyncio.to_thread(get_latest_block)
    count = enabled_count()
    await update.message.reply_text(
        "<b>机器人状态</b>\n"
        f"RPC：<code>{html.escape(BSC_RPC_URL)}</code>\n"
        f"最新区块：<code>{latest}</code>\n"
        f"启用监控数量：<code>{count}</code>\n"
        f"扫描间隔：<code>{SCAN_INTERVAL}s</code>\n"
        f"确认块：<code>{CONFIRMATIONS}</code>\n"
        f"区块分片：<code>{BLOCK_CHUNK}</code>\n"
        f"数据库：<code>{html.escape(DB_PATH)}</code>",
        parse_mode=ParseMode.HTML,
    )


# =========================
# 后台扫描任务
# =========================
async def scan_once(application: Application) -> None:
    watchers = get_enabled_watchers()
    if not watchers:
        return

    latest = await asyncio.to_thread(get_latest_block)
    safe_latest = latest - CONFIRMATIONS
    if safe_latest < 0:
        return

    for watcher in watchers:
        if watcher.last_scanned_block >= safe_latest:
            continue

        start_block = watcher.last_scanned_block + 1
        while start_block <= safe_latest:
            end_block = min(start_block + BLOCK_CHUNK - 1, safe_latest)
            try:
                logs = await asyncio.to_thread(get_logs_for_address, watcher.address, start_block, end_block)
            except Web3RPCError as e:
                logger.error(
                    "扫描失败 watcher=%s blocks=%s-%s error=%s",
                    watcher.address,
                    start_block,
                    end_block,
                    e,
                )
                # 遇到 RPC 错误先退出本轮，避免错误推进游标
                break
            except Exception as e:
                logger.exception(
                    "未知错误 watcher=%s blocks=%s-%s error=%s",
                    watcher.address,
                    start_block,
                    end_block,
                    e,
                )
                break

            for log in logs:
                try:
                    ido_address, tx_hash, block_number, log_index = parse_new_ido_log(log)
                    if is_processed(watcher.id, tx_hash, log_index):
                        continue

                    ownership_changes: List[Tuple[str, str]] = []
                    try:
                        receipt = await asyncio.to_thread(get_receipt, tx_hash)
                        ownership_changes = parse_ownership_transfers_from_receipt(receipt, ido_address)
                    except Exception as e:
                        logger.warning("receipt 解析失败 tx=%s err=%s", tx_hash, e)

                    text = render_notify_message(
                        watcher=watcher,
                        ido_address=ido_address,
                        tx_hash=tx_hash,
                        block_number=block_number,
                        ownership_changes=ownership_changes,
                    )
                    await application.bot.send_message(
                        chat_id=watcher.chat_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    mark_processed(watcher.id, tx_hash, log_index)
                    logger.info("发现新 IDO: factory=%s ido=%s tx=%s", watcher.address, ido_address, tx_hash)
                except Exception as e:
                    logger.exception("处理日志失败 watcher=%s err=%s", watcher.address, e)

            update_last_scanned_block(watcher.id, end_block)
            start_block = end_block + 1


async def scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await scan_once(context.application)


# =========================
# 启动
# =========================
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("import", cmd_import))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("status", cmd_status))

    # 周期扫描
    app.job_queue.run_repeating(scan_job, interval=SCAN_INTERVAL, first=3)
    return app


def main() -> None:
    logger.info("初始化数据库... path=%s", DB_PATH)
    init_db()
    logger.info("机器人启动中... RPC=%s", BSC_RPC_URL)
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
