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
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from dotenv import load_dotenv
from telegram import BotCommand, Update
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
GROUP_LINK = os.getenv("GROUP_LINK", "https://t.me/xiaoccaac")
GROUP_NAME = os.getenv("GROUP_NAME", "小C聊天群")

# 事件签名
NEW_IDO_EVENT = "NewIDOContract(address)"
OWNERSHIP_TRANSFERRED_EVENT = "OwnershipTransferred(address,address)"

NEW_IDO_TOPIC = Web3.keccak(text=NEW_IDO_EVENT).hex()
OWNERSHIP_TRANSFERRED_TOPIC = Web3.keccak(text=OWNERSHIP_TRANSFERRED_EVENT).hex()

ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
TX_HASH_RE = re.compile(r"0x[a-fA-F0-9]{64}")

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


def extract_tx_hash(text: str) -> Optional[str]:
    m = TX_HASH_RE.search(text or "")
    if not m:
        return None
    return m.group(0)


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


def _get_raw_transaction(tx_hash: str) -> Optional[dict]:
    """
    当 w3.eth.get_transaction 因格式化错误（如 chainId 为空）失败时，
    使用原始 RPC 调用绕过 web3.py 的结果格式化器。
    """
    try:
        return w3.eth.get_transaction(tx_hash)
    except Exception:
        pass
    # 兜底：直接发原始 JSON-RPC 请求
    try:
        resp = w3.provider.make_request("eth_getTransactionByHash", [tx_hash])
        result = resp.get("result")
        if result and isinstance(result, dict):
            return result
    except Exception:
        pass
    return None


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


def _decode_abi_string(data_hex: str) -> Optional[str]:
    if not data_hex or data_hex == "0x":
        return None
    raw = data_hex[2:] if data_hex.startswith("0x") else data_hex
    if len(raw) == 64:
        # 某些合约返回 bytes32
        b = bytes.fromhex(raw)
        return b.split(b"\x00", 1)[0].decode("utf-8", errors="ignore").strip() or None
    if len(raw) < 192:
        return None
    try:
        offset = int(raw[0:64], 16) * 2
        if offset + 64 > len(raw):
            return None
        strlen = int(raw[offset: offset + 64], 16)
        start = offset + 64
        end = start + strlen * 2
        if end > len(raw):
            return None
        b = bytes.fromhex(raw[start:end])
        s = b.decode("utf-8", errors="ignore").strip()
        return s or None
    except Exception:
        return None


def _decode_abi_address(data_hex: str) -> Optional[str]:
    if not data_hex or data_hex == "0x":
        return None
    raw = data_hex[2:] if data_hex.startswith("0x") else data_hex
    if len(raw) < 64:
        return None
    word = raw[:64]
    addr_hex = "0x" + word[-40:]
    if addr_hex.lower() == "0x" + "0" * 40:
        return None
    try:
        return checksum_address(addr_hex)
    except Exception:
        return None


def _decode_abi_uint(data_hex: str) -> Optional[int]:
    if not data_hex or data_hex == "0x":
        return None
    raw = data_hex[2:] if data_hex.startswith("0x") else data_hex
    if len(raw) < 64:
        return None
    try:
        return int(raw[:64], 16)
    except Exception:
        return None


def _call_string_method(address: str, selector: str) -> Optional[str]:
    try:
        out = w3.eth.call({"to": checksum_address(address), "data": selector})
        if isinstance(out, (bytes, bytearray)):
            data_hex = "0x" + bytes(out).hex()
        else:
            data_hex = str(out)
        return _decode_abi_string(data_hex)
    except Exception:
        return None


def _call_address_method(address: str, selector: str) -> Optional[str]:
    try:
        out = w3.eth.call({"to": checksum_address(address), "data": selector})
        if isinstance(out, (bytes, bytearray)):
            data_hex = "0x" + bytes(out).hex()
        else:
            data_hex = str(out)
        return _decode_abi_address(data_hex)
    except Exception:
        return None


def _call_uint_method(address: str, selector: str) -> Optional[int]:
    try:
        out = w3.eth.call({"to": checksum_address(address), "data": selector})
        if isinstance(out, (bytes, bytearray)):
            data_hex = "0x" + bytes(out).hex()
        else:
            data_hex = str(out)
        return _decode_abi_uint(data_hex)
    except Exception:
        return None


def _is_probable_erc20(address: str) -> Tuple[bool, Optional[str]]:
    # 返回：是否像 ERC20，和可读名称（优先 name，其次 symbol）
    name = _call_string_method(address, _selector("name()"))
    if name:
        return True, name
    symbol = _call_string_method(address, _selector("symbol()"))
    if symbol:
        return True, f"({symbol})"
    decimals = _call_uint_method(address, _selector("decimals()"))
    if decimals is not None and 0 <= decimals <= 36:
        return True, None
    total_supply = _call_uint_method(address, _selector("totalSupply()"))
    if total_supply is not None:
        return True, None
    return False, None


def _selector(signature: str) -> str:
    return "0x" + Web3.keccak(text=signature).hex()[:8]


def _extract_addresses_from_input(input_data: str) -> List[str]:
    if not input_data or input_data == "0x":
        return []
    data = input_data[2:] if input_data.startswith("0x") else input_data
    if len(data) <= 8:
        return []
    payload = data[8:]  # 去掉 4-byte selector
    words = [payload[i: i + 64] for i in range(0, len(payload), 64) if len(payload[i: i + 64]) == 64]
    seen = set()
    out: List[str] = []
    for w in words:
        if w[:24] != "0" * 24:
            continue
        addr_hex = "0x" + w[24:]
        if addr_hex.lower() == "0x" + "0" * 40:
            continue
        try:
            addr = checksum_address(addr_hex)
        except Exception:
            continue
        low = addr.lower()
        if low not in seen:
            seen.add(low)
            out.append(addr)
    return out


def _extract_timestamps_from_input(input_data: str) -> List[int]:
    if not input_data or input_data == "0x":
        return []
    data = input_data[2:] if input_data.startswith("0x") else input_data
    if len(data) <= 8:
        return []
    payload = data[8:]
    words = [payload[i: i + 64] for i in range(0, len(payload), 64) if len(payload[i: i + 64]) == 64]
    out: List[int] = []
    for w in words:
        try:
            v = int(w, 16)
        except Exception:
            continue
        # 粗略过滤：2017-01-01 ~ 2040-01-01 的 unix 秒
        if 1483228800 <= v <= 2208988800:
            out.append(v)
    return out


def _decode_create_ido_input(input_data: str) -> Tuple[List[str], List[int]]:
    """
    尝试按 ABI 动态数组布局解析 createIDO 类参数：
      (address[] _addresses, uint256[] _startAndEndTimestamps, ...)
    不依赖具体函数签名，只要前两个参数是动态数组即可。
    """
    if not input_data or input_data == "0x":
        return [], []
    data = input_data[2:] if input_data.startswith("0x") else input_data
    if len(data) <= 8:
        return [], []
    payload = data[8:]  # 去掉 selector
    if len(payload) < 64 * 2:
        return [], []

    def read_word(offset_hex_chars: int) -> Optional[int]:
        if offset_hex_chars < 0 or offset_hex_chars + 64 > len(payload):
            return None
        try:
            return int(payload[offset_hex_chars: offset_hex_chars + 64], 16)
        except Exception:
            return None

    # 前两个参数 offset（按 ABI，offset 相对参数区起点）
    off_addr = read_word(0)
    off_ts = read_word(64)
    if off_addr is None or off_ts is None:
        return [], []

    def read_dynamic_array(offset_bytes: int) -> List[int]:
        base = offset_bytes * 2
        arr_len = read_word(base)
        if arr_len is None or arr_len < 0 or arr_len > 64:
            return []
        out: List[int] = []
        p = base + 64
        for _ in range(arr_len):
            v = read_word(p)
            if v is None:
                break
            out.append(v)
            p += 64
        return out

    raw_addrs = read_dynamic_array(off_addr)
    raw_ts = read_dynamic_array(off_ts)

    addrs: List[str] = []
    for v in raw_addrs:
        addr_hex = f"0x{v:064x}"[-40:]
        a = "0x" + addr_hex
        if a.lower() == "0x" + "0" * 40:
            addrs.append(a)
            continue
        try:
            addrs.append(checksum_address(a))
        except Exception:
            addrs.append(a)

    ts = [x for x in raw_ts if 1483228800 <= x <= 2208988800]
    return addrs, ts


def _fmt_ts_short(ts: int) -> str:
    """格式化时间戳为简洁格式：YYYY-MM-DD HH:MM:SS UTC+8"""
    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    dt_cn = dt_utc.astimezone(timezone(timedelta(hours=8)))
    return f"{dt_cn:%Y-%m-%d %H:%M:%S} UTC+8"


def _fmt_unix(ts: int) -> str:
    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    dt_cn = dt_utc.astimezone(timezone(timedelta(hours=8)))
    return f"{ts} (UTC+8 {dt_cn:%Y-%m-%d %H:%M:%S}, UTC {dt_utc:%Y-%m-%d %H:%M:%S})"


def extract_tx_extra_info(
    tx_hash: str,
) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[int], List[Tuple[int, str, Optional[str]]]]:
    """
    返回：token_address, token_name, start_ts, end_ts, input_addresses
    说明：从创建交易 input 中启发式提取，适配大多数工厂 create 参数布局。
    input_addresses: 输入参数中解码出的所有非零地址列表，格式 [(索引, 地址, 名称或None), ...]。
    """
    tx = _get_raw_transaction(tx_hash)
    if tx is None:
        return None, None, None, None, []

    input_data = tx.get("input", "") if isinstance(tx, dict) else getattr(tx, "input", "")
    # web3.py 可能返回 HexBytes（bytes 子类），原始 RPC 返回 hex 字符串
    if isinstance(input_data, (bytes, bytearray)):
        input_data = "0x" + input_data.hex()
    elif not isinstance(input_data, str):
        input_data = str(input_data)
    tx_from = str(tx.get("from", "")).lower() if isinstance(tx, dict) else str(getattr(tx, "from", "")).lower()
    tx_to = str(tx.get("to", "")).lower() if isinstance(tx, dict) else str(getattr(tx, "to", "")).lower()

    token_address: Optional[str] = None
    token_name: Optional[str] = None
    input_addresses: List[Tuple[int, str, Optional[str]]] = []

    decoded_addrs, decoded_ts = _decode_create_ido_input(input_data)

    if decoded_addrs:
        # 遍历所有非零地址，收集详情并寻找代币
        for i, addr in enumerate(decoded_addrs):
            if addr.lower() == "0x" + "0" * 40:
                continue
            ok, readable_name = _is_probable_erc20(addr)
            if ok:
                input_addresses.append((i, addr, readable_name))
                if token_address is None:
                    token_address = addr
                    token_name = readable_name
            else:
                # 非 ERC20 合约也尝试获取 name() 用于显示
                name = _call_string_method(addr, _selector("name()"))
                input_addresses.append((i, addr, name))

        # 如果没找到 ERC20，使用第一个非零地址作为兜底
        if token_address is None and input_addresses:
            token_address = input_addresses[0][1]
            token_name = input_addresses[0][2]

    # 1) 从 input 候选地址中找最像 ERC20 的地址（兜底）
    if token_address is None:
        seen_addrs = {a[1].lower() for a in input_addresses}
        first_contract_candidate: Optional[str] = None
        fallback_idx = 0
        for addr in _extract_addresses_from_input(input_data):
            if addr.lower() in {tx_from, tx_to}:
                continue
            try:
                code = w3.eth.get_code(addr)
            except Exception:
                continue
            if not code:
                continue
            if first_contract_candidate is None:
                first_contract_candidate = addr
            is_erc20, readable_name = _is_probable_erc20(addr)
            # 同时收集到 input_addresses 用于展示（去重）
            if addr.lower() not in seen_addrs:
                input_addresses.append((fallback_idx, addr, readable_name if is_erc20 else None))
                seen_addrs.add(addr.lower())
                fallback_idx += 1
            if is_erc20:
                token_address = addr
                token_name = readable_name
                break

        if token_address is None and first_contract_candidate is not None:
            token_address = first_contract_candidate

    # 2) 解析可能的起止时间（优先动态数组解码结果）
    ts_list = decoded_ts if decoded_ts else _extract_timestamps_from_input(input_data)
    start_ts = ts_list[0] if len(ts_list) > 0 else None
    end_ts = ts_list[1] if len(ts_list) > 1 else None
    return token_address, token_name, start_ts, end_ts, input_addresses


def extract_ido_extra_info(ido_address: str) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[int]]:
    """
    从已创建的 ido 合约读取常见 getter，作为 input 解析失败时的兜底。
    返回：token_address, token_name, start_ts, end_ts
    """
    token_address: Optional[str] = None
    token_name: Optional[str] = None
    start_ts: Optional[int] = None
    end_ts: Optional[int] = None

    token_selectors = [
        _selector("token()"),
        _selector("saleToken()"),
        _selector("idoToken()"),
        _selector("projectToken()"),
        _selector("tokenAddress()"),
        _selector("saleTokenAddress()"),
        _selector("lpToken()"),
        _selector("raiseToken()"),
    ]
    start_selectors = [
        _selector("startTime()"),
        _selector("startTimestamp()"),
        _selector("startAt()"),
        _selector("startDate()"),
        _selector("start()"),
    ]
    end_selectors = [
        _selector("endTime()"),
        _selector("endTimestamp()"),
        _selector("endAt()"),
        _selector("endDate()"),
        _selector("end()"),
    ]

    for sel in token_selectors:
        token_address = _call_address_method(ido_address, sel)
        if token_address:
            break
    if token_address:
        token_name = _call_string_method(token_address, "0x06fdde03")
    if not token_name:
        # 有些 IDO 直接暴露名字字段
        for sig in ("name()", "tokenName()", "projectName()"):
            token_name = _call_string_method(ido_address, _selector(sig))
            if token_name:
                break

    for sel in start_selectors:
        start_ts = _call_uint_method(ido_address, sel)
        if start_ts and 1483228800 <= start_ts <= 2208988800:
            break
        start_ts = None
    for sel in end_selectors:
        end_ts = _call_uint_method(ido_address, sel)
        if end_ts and 1483228800 <= end_ts <= 2208988800:
            break
        end_ts = None

    return token_address, token_name, start_ts, end_ts


def merge_extra_info(
    primary: Tuple[Optional[str], Optional[str], Optional[int], Optional[int]],
    fallback: Tuple[Optional[str], Optional[str], Optional[int], Optional[int]],
) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[int]]:
    p_token_addr, p_token_name, p_start, p_end = primary
    f_token_addr, f_token_name, f_start, f_end = fallback
    return (
        p_token_addr or f_token_addr,
        p_token_name or f_token_name,
        p_start or f_start,
        p_end or f_end,
    )


def render_tx_extra_lines(
    token_address: Optional[str],
    token_name: Optional[str],
    start_ts: Optional[int],
    end_ts: Optional[int],
    show_when_empty: bool = False,
    input_addresses: Optional[List[Tuple[int, str, Optional[str]]]] = None,
) -> List[str]:
    has_data = token_address or token_name or start_ts or end_ts or input_addresses
    if not has_data:
        if not show_when_empty:
            return []
        return ["", "未解析到代币/时间字段（可能该合约未暴露标准 getter）。"]
    lines = [""]
    if token_name:
        lines.append(f"代币名字：{html.escape(token_name)}")
    if token_address:
        addr_link = BSCSCAN_ADDRESS + token_address
        lines.append(f"代币合约：<a href=\"{html.escape(addr_link)}\">{html.escape(token_address)}</a>")
    if not token_address and not token_name:
        lines.append("代币信息：未解析到")
    if start_ts:
        lines.append(f"开始时间：{html.escape(_fmt_ts_short(start_ts))}")
    if end_ts:
        lines.append(f"结束时间：{html.escape(_fmt_ts_short(end_ts))}")
    if input_addresses:
        lines.append("")
        lines.append("<b>创建参数地址列表</b>")
        for idx, addr, name in input_addresses:
            addr_link = BSCSCAN_ADDRESS + addr
            if name:
                lines.append(f"#{idx}: <a href=\"{html.escape(addr_link)}\">{html.escape(addr)}</a> — {html.escape(name)}")
            else:
                lines.append(f"#{idx}: <a href=\"{html.escape(addr_link)}\">{html.escape(addr)}</a>")
    return lines


def analyze_tx_match_for_chat(chat_id: int, tx_hash: str) -> Tuple[bool, str]:
    watchers = get_watchers_by_chat(chat_id)
    if not watchers:
        return False, "当前聊天还没有添加任何监控地址，请先 /add 或 /import。"

    watcher_map = {w.address.lower(): w for w in watchers}
    receipt = get_receipt(tx_hash)
    logs = receipt.get("logs", [])

    matched_lines: List[str] = []
    new_ido_seen = 0

    token_address, token_name, start_ts, end_ts, input_addresses = extract_tx_extra_info(tx_hash)

    for lg in logs:
        topics = lg.get("topics", [])
        if not topics:
            continue
        if topics[0].hex().lower() != NEW_IDO_TOPIC.lower():
            continue
        new_ido_seen += 1
        emitter = str(lg["address"]).lower()
        watcher = watcher_map.get(emitter)
        if not watcher:
            continue
        ido_address, _, block_number, log_index = parse_new_ido_log(lg)
        token_address, token_name, start_ts, end_ts = merge_extra_info(
            (token_address, token_name, start_ts, end_ts),
            extract_ido_extra_info(ido_address),
        )
        status = "启用" if watcher.enabled else "暂停"
        matched_lines.append(
            f"- 命中监控：<code>{html.escape(watcher.address)}</code>（{status}）\n"
            f"  新 IDO：<code>{html.escape(ido_address)}</code>\n"
            f"  区块：<code>{block_number}</code>，logIndex：<code>{log_index}</code>"
        )

    tx_link = BSCSCAN_TX + tx_hash
    group_line = f"加入<a href=\"{html.escape(GROUP_LINK)}\">{html.escape(GROUP_NAME)}</a> 获取最新打新消息"

    if matched_lines:
        # 找到第一个命中的 watcher 名称
        first_watcher_name = None
        for lg in logs:
            topics = lg.get("topics", [])
            if not topics:
                continue
            if topics[0].hex().lower() != NEW_IDO_TOPIC.lower():
                continue
            emitter = str(lg["address"]).lower()
            w = watcher_map.get(emitter)
            if w:
                first_watcher_name = w.label or w.address
                break

        label = html.escape(first_watcher_name or "未知")
        token_name_safe = html.escape(token_name) if token_name else None
        token_addr_safe = html.escape(token_address) if token_address else None
        start_str = html.escape(_fmt_ts_short(start_ts)) if start_ts else None
        end_str = html.escape(_fmt_ts_short(end_ts)) if end_ts else None
        tx_link_safe = html.escape(tx_link)

        # 中文
        cn = [f"<b>{label}  部署新的IDO合约</b>"]
        if token_name_safe:
            cn.append(f"代币名称：{token_name_safe}")
        if token_addr_safe:
            cn.append(f"代币合约：{token_addr_safe}")
        if start_str:
            cn.append(f"开始时间：{start_str}")
        if end_str:
            cn.append(f"结束时间：{end_str}")
        if not token_addr_safe and not token_name_safe:
            cn.append("代币信息：未解析到")
        cn.append("")
        cn.append(f"TX：{tx_link_safe}")

        # English
        en = [f"<b>{label}  New IDO Contract Deployed</b>"]
        if token_name_safe:
            en.append(f"Token Name: {token_name_safe}")
        if token_addr_safe:
            en.append(f"Token Contract: {token_addr_safe}")
        if start_str:
            en.append(f"Start Time: {start_str}")
        if end_str:
            en.append(f"End Time: {end_str}")
        if not token_addr_safe and not token_name_safe:
            en.append("Token Info: Not detected")
        en.append("")
        en.append(f"TX: {tx_link_safe}")

        parts = cn + ["", "———————————", ""] + en
        parts.append("")
        parts.append(group_line)
        return True, "\n".join(parts)

    watcher_addr_list = "\n".join(
        f"- <code>{html.escape(w.address)}</code>（{'启用' if w.enabled else '暂停'}）" for w in watchers[:20]
    )

    if new_ido_seen == 0:
        extra_lines = render_tx_extra_lines(token_address, token_name, start_ts, end_ts, input_addresses=input_addresses)
        return (
            False,
            "\n".join(
                [
                    "<b>❌ 该交易不会被当前机器人命中</b>",
                    "原因：交易日志里未发现 <code>NewIDOContract(address)</code> 事件。",
                    *extra_lines,
                    f"\nTX：{html.escape(tx_link)}",
                    "",
                    "<b>当前聊天监控地址（前20个）</b>",
                    watcher_addr_list,
                    "",
                    group_line,
                ]
            ),
        )

    extra_lines = render_tx_extra_lines(token_address, token_name, start_ts, end_ts, input_addresses=input_addresses)
    return (
        False,
        "\n".join(
            [
                "<b>❌ 该交易不会被当前聊天命中</b>",
                "原因：虽然交易里有 <code>NewIDOContract(address)</code>，但发事件的地址不在当前聊天监控列表中。",
                *extra_lines,
                f"\nTX：{html.escape(tx_link)}",
                "",
                "<b>当前聊天监控地址（前20个）</b>",
                watcher_addr_list,
                "",
                group_line,
            ]
        ),
    )


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
        "/del 地址 - 删除一个地址\n"
        "/pause 地址 - 暂停一个地址\n"
        "/resume 地址 - 恢复一个地址\n"
        "/checktx 交易哈希 - 检查某笔交易能否被当前聊天命中\n"
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
    token_address: Optional[str] = None,
    token_name: Optional[str] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    input_addresses: Optional[List[Tuple[int, str, Optional[str]]]] = None,
) -> str:
    """返回中英双语通知消息。"""
    watcher_name = watcher.label or watcher.address
    tx_link = BSCSCAN_TX + tx_hash
    group_line = f"加入<a href=\"{html.escape(GROUP_LINK)}\">{html.escape(GROUP_NAME)}</a> 获取最新打新消息"

    token_name_safe = html.escape(token_name) if token_name else None
    token_addr_safe = html.escape(token_address) if token_address else None
    start_str = html.escape(_fmt_ts_short(start_ts)) if start_ts else None
    end_str = html.escape(_fmt_ts_short(end_ts)) if end_ts else None
    tx_link_safe = html.escape(tx_link)

    # --- 中文 ---
    cn = [f"<b>{html.escape(watcher_name)}  部署新的IDO合约</b>"]
    if token_name_safe:
        cn.append(f"代币名称：{token_name_safe}")
    if token_addr_safe:
        cn.append(f"代币合约：{token_addr_safe}")
    if start_str:
        cn.append(f"开始时间：{start_str}")
    if end_str:
        cn.append(f"结束时间：{end_str}")
    if not token_addr_safe and not token_name_safe:
        cn.append("代币信息：未解析到")
    cn.append("")
    cn.append(f"TX：{tx_link_safe}")

    # --- English ---
    en = [f"<b>{html.escape(watcher_name)}  New IDO Contract Deployed</b>"]
    if token_name_safe:
        en.append(f"Token Name: {token_name_safe}")
    if token_addr_safe:
        en.append(f"Token Contract: {token_addr_safe}")
    if start_str:
        en.append(f"Start Time: {start_str}")
    if end_str:
        en.append(f"End Time: {end_str}")
    if not token_addr_safe and not token_name_safe:
        en.append("Token Info: Not detected")
    en.append("")
    en.append(f"TX: {tx_link_safe}")

    # 合并
    parts = cn + ["", "———————————", ""] + en
    parts.append("")
    parts.append(group_line)
    return "\n".join(parts)


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


async def cmd_checktx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    if not context.args:
        await update.message.reply_text("用法：/checktx 0x交易哈希")
        return

    tx_hash = extract_tx_hash(context.args[0])
    if not tx_hash:
        await update.message.reply_text("交易哈希格式不正确，请输入 0x 开头的 66 位哈希。")
        return

    try:
        ok, text = await asyncio.to_thread(analyze_tx_match_for_chat, update.effective_chat.id, tx_hash)
    except Exception as e:
        logger.exception("checktx 失败 tx=%s err=%s", tx_hash, e)
        await update.message.reply_text(f"检查失败：{e}")
        return

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


def debug_tx_parsing(tx_hash: str) -> str:
    """逐步调试交易 input 解析，返回详细诊断信息。"""
    lines: List[str] = [f"<b>🔍 调试交易解析</b>\n交易：<code>{html.escape(tx_hash)}</code>"]

    # 1) 获取交易
    tx = None
    try:
        tx = w3.eth.get_transaction(tx_hash)
        lines.append("\n✅ get_transaction 成功")
    except Exception as e:
        lines.append(f"\n⚠️ get_transaction 失败：{html.escape(str(e)[:100])}")
        lines.append("尝试原始 RPC 兜底...")
        try:
            resp = w3.provider.make_request("eth_getTransactionByHash", [tx_hash])
            tx = resp.get("result")
            if tx and isinstance(tx, dict):
                lines.append("✅ 原始 RPC 获取成功")
            else:
                lines.append("❌ 原始 RPC 返回空")
        except Exception as e2:
            lines.append(f"❌ 原始 RPC 也失败：{html.escape(str(e2)[:100])}")
    if not tx:
        return "\n".join(lines)

    raw_input = tx.get("input", "") if isinstance(tx, dict) else getattr(tx, "input", "")
    lines.append(f"\n<b>input_data 类型</b>：<code>{html.escape(type(raw_input).__name__)}</code>")
    lines.append(f"<b>input_data 长度</b>：<code>{len(raw_input)}</code>")

    # 转换为字符串
    if isinstance(raw_input, (bytes, bytearray)):
        input_data = "0x" + raw_input.hex()
        lines.append("✅ 已从 bytes 转为 hex 字符串")
    elif isinstance(raw_input, str):
        input_data = raw_input
        lines.append("✅ input_data 已经是字符串")
    else:
        input_data = str(raw_input)
        lines.append(f"⚠️ 非预期类型，str() 转换：<code>{html.escape(input_data[:80])}</code>")

    lines.append(f"<b>hex 长度</b>：<code>{len(input_data)}</code>")
    if len(input_data) >= 10:
        lines.append(f"<b>selector</b>：<code>{html.escape(input_data[:10])}</code>")

    # 2) 解码动态数组
    lines.append("\n<b>--- _decode_create_ido_input ---</b>")
    try:
        decoded_addrs, decoded_ts = _decode_create_ido_input(input_data)
        lines.append(f"decoded_addrs 数量：<code>{len(decoded_addrs)}</code>")
        for i, addr in enumerate(decoded_addrs):
            lines.append(f"  [{i}] <code>{html.escape(addr)}</code>")
        lines.append(f"decoded_ts 数量：<code>{len(decoded_ts)}</code>")
        for i, ts in enumerate(decoded_ts):
            lines.append(f"  [{i}] <code>{ts}</code> → {html.escape(_fmt_unix(ts))}")
    except Exception as e:
        lines.append(f"❌ 解码失败：<code>{html.escape(str(e))}</code>")
        decoded_addrs, decoded_ts = [], []

    # 3) 逐个地址检测
    if decoded_addrs:
        lines.append("\n<b>--- 逐个地址检测 ---</b>")
        for i, addr in enumerate(decoded_addrs):
            if addr.lower() == "0x" + "0" * 40:
                lines.append(f"[{i}] 零地址，跳过")
                continue
            # get_code
            try:
                code = w3.eth.get_code(addr)
                has_code = bool(code and code != b"" and code != b"0x")
                lines.append(f"[{i}] <code>{html.escape(addr)}</code>")
                lines.append(f"    get_code: {'✅ 有代码' if has_code else '❌ 无代码'}（{len(code)} bytes）")
            except Exception as e:
                lines.append(f"[{i}] <code>{html.escape(addr)}</code>")
                lines.append(f"    get_code: ❌ 异常 {html.escape(str(e)[:80])}")
                continue
            # ERC20 检测
            try:
                ok, readable_name = _is_probable_erc20(addr)
                lines.append(f"    is_erc20: {'✅' if ok else '❌'}，name={html.escape(str(readable_name))}")
            except Exception as e:
                lines.append(f"    is_erc20: ❌ 异常 {html.escape(str(e)[:80])}")
            # 额外尝试 name()
            try:
                name = _call_string_method(addr, _selector("name()"))
                lines.append(f"    name(): <code>{html.escape(str(name))}</code>")
            except Exception as e:
                lines.append(f"    name(): ❌ {html.escape(str(e)[:60])}")

    # 4) 兜底地址扫描
    lines.append("\n<b>--- _extract_addresses_from_input ---</b>")
    try:
        fallback_addrs = _extract_addresses_from_input(input_data)
        lines.append(f"找到 {len(fallback_addrs)} 个地址")
        tx_from = str(tx.get("from", "")).lower() if isinstance(tx, dict) else str(getattr(tx, "from", "")).lower()
        tx_to = str(tx.get("to", "")).lower() if isinstance(tx, dict) else str(getattr(tx, "to", "")).lower()
        for addr in fallback_addrs[:10]:
            skip_reason = ""
            if addr.lower() == tx_from:
                skip_reason = " ← 是 tx.from，会跳过"
            elif addr.lower() == tx_to:
                skip_reason = " ← 是 tx.to，会跳过"
            lines.append(f"  <code>{html.escape(addr)}</code>{skip_reason}")
    except Exception as e:
        lines.append(f"❌ 失败：{html.escape(str(e)[:80])}")

    # 5) IDO 合约 getter 检测
    lines.append("\n<b>--- extract_ido_extra_info ---</b>")
    # 从 receipt 找 IDO 地址
    try:
        receipt = get_receipt(tx_hash)
        ido_address = None
        for lg in receipt.get("logs", []):
            topics = lg.get("topics", [])
            if topics and topics[0].hex().lower() == NEW_IDO_TOPIC.lower():
                ido_address, _, _, _ = parse_new_ido_log(lg)
                break
        if ido_address:
            lines.append(f"IDO 合约：<code>{html.escape(ido_address)}</code>")
            try:
                ido_token, ido_name, ido_start, ido_end = extract_ido_extra_info(ido_address)
                lines.append(f"  token_addr: <code>{html.escape(str(ido_token))}</code>")
                lines.append(f"  token_name: <code>{html.escape(str(ido_name))}</code>")
                lines.append(f"  start_ts: <code>{ido_start}</code>")
                lines.append(f"  end_ts: <code>{ido_end}</code>")
            except Exception as e:
                lines.append(f"  ❌ 查询失败：{html.escape(str(e)[:80])}")
        else:
            lines.append("未找到 NewIDOContract 事件")
    except Exception as e:
        lines.append(f"❌ receipt 获取失败：{html.escape(str(e)[:80])}")

    return "\n".join(lines)


async def cmd_debugtx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not context.args:
        await update.message.reply_text("用法：/debugtx 0x交易哈希")
        return
    tx_hash = extract_tx_hash(context.args[0])
    if not tx_hash:
        await update.message.reply_text("交易哈希格式不正确，请输入 0x 开头的 66 位哈希。")
        return
    try:
        text = await asyncio.to_thread(debug_tx_parsing, tx_hash)
    except Exception as e:
        logger.exception("debugtx 失败 tx=%s err=%s", tx_hash, e)
        await update.message.reply_text(f"调试失败：{e}")
        return
    # 分段发送，避免超长
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await update.message.reply_text(
                text[i:i + 4000],
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    else:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
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
                    token_address: Optional[str] = None
                    token_name: Optional[str] = None
                    start_ts: Optional[int] = None
                    end_ts: Optional[int] = None
                    input_addresses: List[Tuple[int, str, Optional[str]]] = []
                    try:
                        receipt = await asyncio.to_thread(get_receipt, tx_hash)
                        ownership_changes = parse_ownership_transfers_from_receipt(receipt, ido_address)
                    except Exception as e:
                        logger.warning("receipt 解析失败 tx=%s err=%s", tx_hash, e)
                    try:
                        token_address, token_name, start_ts, end_ts, input_addresses = await asyncio.to_thread(
                            extract_tx_extra_info, tx_hash
                        )
                    except Exception as e:
                        logger.warning("tx input 解析失败 tx=%s err=%s", tx_hash, e)
                    try:
                        from_ido = await asyncio.to_thread(extract_ido_extra_info, ido_address)
                        token_address, token_name, start_ts, end_ts = merge_extra_info(
                            (token_address, token_name, start_ts, end_ts),
                            from_ido,
                        )
                    except Exception as e:
                        logger.warning("ido getter 解析失败 ido=%s tx=%s err=%s", ido_address, tx_hash, e)

                    text = render_notify_message(
                        watcher=watcher,
                        ido_address=ido_address,
                        tx_hash=tx_hash,
                        block_number=block_number,
                        ownership_changes=ownership_changes,
                        token_address=token_address,
                        token_name=token_name,
                        start_ts=start_ts,
                        end_ts=end_ts,
                        input_addresses=input_addresses,
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
async def post_init(application: Application) -> None:
    """启动时设置 Telegram 菜单命令列表。"""
    await application.bot.set_my_commands([
        BotCommand("help", "显示帮助"),
        BotCommand("add", "添加监控地址"),
        BotCommand("import", "批量导入地址"),
        BotCommand("list", "查看监控列表"),
        BotCommand("del", "删除监控地址"),
        BotCommand("pause", "暂停监控地址"),
        BotCommand("resume", "恢复监控地址"),
        BotCommand("checktx", "检查交易是否命中"),
        BotCommand("debugtx", "调试交易解析"),
        BotCommand("status", "查看机器人状态"),
        BotCommand("chatid", "查看聊天 ID"),
    ])


def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("import", cmd_import))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("checktx", cmd_checktx))
    app.add_handler(CommandHandler("debugtx", cmd_debugtx))
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
