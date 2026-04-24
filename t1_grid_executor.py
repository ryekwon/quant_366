#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ⚠️  【策略退役公告】2026-04-16 正式停止执行                        ║
# ║                                                                      ║
# ║  本文件 (t1_grid_executor.py) 已由                                   ║
# ║    etf_ou_grid_executor.py  (非对称OU均值回归网格)                   ║
# ║  正式接替，不再由 autopilot_master.py 调度。                         ║
# ║                                                                      ║
# ║  持仓移交情况：2026-04-16 开盘后由 t1_emergency_reset.py             ║
# ║    以 --live 模式一次性清仓所有 T1 存量持仓，账本归零。              ║
# ║                                                                      ║
# ║  本文件保留源码以便历史追溯，禁止重新启动。                          ║
# ╚══════════════════════════════════════════════════════════════════════╝
"""
t1_grid_executor.py — T+1 纯机械化网格交易【盘中极速执行与状态机管理】
====================================================================
调度时间：每日 09:30 由 autopilot_master.py 以守护进程（watchdog）方式拉起，
          在盘中 09:30 ~ 15:00 持续轮询，15:00 后自动退出。

物理账本隔离原则（不可违背）：
  - 所有状态读写只基于 .state/t1_grid_ledger.yaml
  - 绝对禁止用 query_stock_positions 作逻辑判断（防止打架）
  - 实盘下单基于账本中的 available_shares / locked_shares

策略领地隔离（Firewall）：
  - 执行前读取其他策略账本，绑定所有已占用标的，禁止 T1 重叠操作

三大执行逻辑（优先级：Executioner > 卖出 > 买入）：
  1. Executioner（空间熔断 + 时间衰减）→ 跌停价全清
  2. 网格收割（卖出）→ 买一价挂卖单
  3. 防跳空建仓（买入）→ 卖一价挂买单，格数最多 +1

安全特性：
  - TTL 文件锁防多开
  - QMT 5 重连接重试
  - 盘口保护窗口（09:25~09:33，13:00~13:03）
  - 每标的 15s 下单冷却，防重复挂单
  - 支持 --dry-run 参数，只打印不下单
"""


import os
import sys
import json
import time
import random
import logging
import argparse
import traceback
import threading
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

# ⚠️ quant-safe-patterns: xtconstant 必须与 xtdata 同行导入，否则下单时 NameError
from xtquant import xtdata, xtconstant
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from dotenv import load_dotenv

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

# =====================================================
# 强制 UTF-8 输出（防止 Emoji 在 Windows 控制台崩溃）
# =====================================================
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

load_dotenv()

# =====================================================
# 路径与环境配置
# =====================================================
_DIR            = Path(__file__).parent.resolve()
LEDGER_FILE     = _DIR / ".state" / "t1_grid_ledger.yaml"
WHITELIST_FILE  = Path(r"Z:\QuantpC_Workspace\Data\t1_grid_whitelist.yaml")
STATUS_FILE     = _DIR / ".state" / "autopilot_status.json"
ACTION_LOG_DIR  = _DIR / ".state" / "action_logs"
LOG_DIR         = _DIR / "logs"
LOCK_FILE       = _DIR / ".state" / "t1_grid_executor.lock"

QMT_PATH        = os.getenv("QMT_PATH", "")
ACCOUNT_ID      = os.getenv("ACCOUNT_ID", "")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")

# =====================================================
# 策略标识（订单标识规范 quant-v4-patterns §3）
# =====================================================
STRATEGY_NAME   = "T1_Grid"
ORDER_BUY       = "T1_Buy"
ORDER_SELL      = "T1_Sell"
ORDER_EXECUTIONER = "T1_Exec"
ORDER_PHASEOUT  = "T1_PhaseOut"   # Phase-out 僵尸清道夫强平单标识
PROBE_NAME      = "t1_grid_executor"

# =====================================================
# T1 全局资金总阀（手动可调）
# =====================================================
T1_TOTAL_CAPITAL = 200_000   # ← 20w 总流动性池，每月换仓时手动更新

# =====================================================
# Phase-out 淘汰标的双重熔断参数
# =====================================================
PHASEOUT_MAX_HOLD_DAYS = 20  # 最大容忍持仓自然日（约4个交易周）
PHASEOUT_MAX_DRAWDOWN  = 0.15  # 最大单边下行容忍度（-15%）

# =====================================================
# 运行参数
# =====================================================
POLL_INTERVAL_SEC   = 3      # 主循环轮询间隔（秒）
HEARTBEAT_SEC       = 600    # 心跳日志间隔（10分钟）
ORDER_COOLDOWN_SEC  = 15     # 单标的下单冷却时间（防重复）
MAX_GRID_DEPTH      = 4      # 最大向下格数
MAX_CONN_RETRY      = 5      # QMT 连接最大重试次数
PENDING_TIMEOUT_SEC = 60     # pending 订单超时巡检阈值（秒）
PENDING_SWEEP_SEC   = 30     # pending 巡检间隔（秒）

# =====================================================
# 全局线程安全注册表（CTO 补丁 A + B + C）
# =====================================================
# 补丁 A：账本文件 IO 全局锁（回调线程 & 主线程共享）
_ledger_lock = threading.Lock()

# 补丁 B/C：在途委托注册表
#   {seq: {code, grid_level, direction, target_qty, sent_at}}
_pending_orders_lock = threading.Lock()
_pending_orders: dict[int, dict] = {}

# 盘口保护窗口（集合竞价 + 午盘回启期价格不稳定）
GUARD_WINDOWS = [("0925", "0930"), ("0930", "0933"), ("1300", "1303")]

# =====================================================
# 文件日志（同时写文件 + 终端）
# =====================================================
LOG_DIR.mkdir(exist_ok=True)
_today_str = datetime.now().strftime("%Y%m%d")
_log_path  = LOG_DIR / f"{_today_str}_t1_grid_executor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
def log(msg: str):
    logging.info(msg)
    sys.stdout.flush()


# =====================================================
# N8N Webhook 通知
# =====================================================
def send_webhook(title: str, message: str):
    """向 N8N 发送告警（失败静默，不阻断主逻辑）"""
    if not N8N_WEBHOOK_URL or not _HAS_REQUESTS:
        return
    try:
        payload = {
            "title": title,
            "message": message,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        requests.post(N8N_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        log(f"⚠️ Webhook 发送失败: {e}")


# =====================================================
# 探针：写入 autopilot_status.json
# =====================================================
def write_probe(status: str, buy_cnt: int = 0, sell_cnt: int = 0, note: str = ""):
    """实时状态探针，供 Dashboard / autopilot_master 监控消费"""
    try:
        all_status: dict = {}
        if STATUS_FILE.exists():
            try:
                with open(STATUS_FILE, "r", encoding="utf-8") as f:
                    all_status = json.load(f)
            except Exception:
                all_status = {}
        all_status[PROBE_NAME] = {
            "strategy_name": "T1 纯网格执行器",
            "script":        "t1_grid_executor.py",
            "pid":           os.getpid(),
            "status":        status,
            "updated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "buys_today":    buy_cnt,
            "sells_today":   sell_cnt,
            "description":   note or f"状态={status} 买{buy_cnt}卖{sell_cnt}",
        }
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(all_status, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 探针失败绝不中断主逻辑


# =====================================================
# Action Log（与 quant_logger.record_action 格式对齐）
# =====================================================
def append_action_log(action: str, target: str, price: float, reason: str, extra: dict | None = None):
    try:
        ACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = ACTION_LOG_DIR / f"action_{datetime.now().strftime('%Y%m%d')}.jsonl"
        record = {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "strategy":  STRATEGY_NAME,
            "action":    action,
            "target":    target,
            "price":     round(float(price), 4),
            "reason":    reason,
            "extra":     extra or {},
        }
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# =====================================================
# 进程唯一锁（带 TTL 自愈，与 t0/sniper 完全相同模式）
# =====================================================
def acquire_lock(max_age_seconds: int = 7200) -> bool:
    """带 TTL 的自愈型文件锁。默认 2 小时超时（盘中守护进程全天运行）"""
    lock = str(LOCK_FILE)
    if os.path.exists(lock):
        age = time.time() - os.path.getmtime(lock)
        if age > max_age_seconds:
            log(f"⚠️ [自愈] 发现残留孤儿锁 ({age:.0f}秒前)，强行粉碎并接管控制权...")
            try:
                os.remove(lock)
            except OSError:
                pass
        else:
            log(f"🚫 [并发拦截] 进程锁生效中 (存活 {age:.0f}秒)，本次调度静默退让。")
            return False
    try:
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return False


def release_lock():
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except OSError:
        pass


# =====================================================
# 账本读写（原子操作 + 全局 IO 锁，补丁 A）
# =====================================================
def load_ledger() -> dict:
    """线程安全账本读取。回调线程与主线程共享 _ledger_lock。"""
    with _ledger_lock:
        if not LEDGER_FILE.exists():
            return {}
        try:
            with open(LEDGER_FILE, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            log(f"❌ 账本读取失败: {e}")
            return {}


def save_ledger(ledger: dict):
    """线程安全原子写（tmp → replace）。回调线程与主线程共享 _ledger_lock。"""
    with _ledger_lock:
        tmp = LEDGER_FILE.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(ledger, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            tmp.replace(LEDGER_FILE)
        except Exception as e:
            log(f"❌ 账本写入失败: {e}")
            if tmp.exists():
                tmp.unlink()


# =====================================================
# Grid Inventory 辅助函数
# =====================================================
def _sum_available(inventory: dict) -> int:
    """从 grid_inventory 计算 available_shares（所有 holding 格的 filled_qty 之和）"""
    return sum(v["filled_qty"] for v in inventory.values() if v.get("status") == "holding")


def _get_max_holding_grid(inventory: dict) -> int:
    """返回当前最深 holding 格编号，无则返回 0"""
    holding = [int(k) for k, v in inventory.items() if v.get("status") == "holding" and v.get("filled_qty", 0) > 0]
    return max(holding) if holding else 0


def _get_min_holding_price(inventory: dict) -> float:
    """[物理价差锁] 锚点提取：遍历 grid_inventory 中所有 holding 槽，
    返回最低真实成交买入价（buy_price）。
    零持仓时返回 float('inf')，使价差锁对首格建仓完全透明。
    """
    prices = [
        float(slot.get("buy_price", 0.0))
        for slot in inventory.values()
        if slot.get("status") == "holding" and float(slot.get("buy_price", 0.0)) > 0
    ]
    return min(prices) if prices else float("inf")


def _check_and_heal_ledger(ledger: dict) -> list[str]:
    """[一致性铁律] 启动时校验账本三态映射。自动修复可愈违规，记录不可愈违规。

    三规则：
    1. grid==0 → available_shares 必须为 0
    2. grid==N → grid_inventory 必须有完整的 '1'..'N' holding 槽
    3. available_shares == sum(holding filled_qty)
    """
    warnings = []
    for code, rec in ledger.items():
        if not isinstance(rec, dict):
            continue
        grid  = int(rec.get("current_grid", 0))
        avail = int(rec.get("available_shares", 0))
        inv   = rec.get("grid_inventory") or {}

        # 铁律 1：grid==0 时 available_shares 必须为 0
        if grid == 0 and avail != 0:
            warnings.append(f"[{code}] AUTO-FIX grid=0 but avail={avail} → 归零")
            rec["available_shares"] = 0

        # 铁律 2+3：grid==N 时 inventory 校验
        if grid > 0:
            inv_total = _sum_available(inv)
            computed_grid = _get_max_holding_grid(inv)
            if computed_grid != grid:
                warnings.append(f"[{code}] AUTO-FIX current_grid={grid}→{computed_grid} (from inventory)")
                rec["current_grid"] = computed_grid
            if inv_total != avail:
                warnings.append(f"[{code}] AUTO-FIX available_shares={avail}→{inv_total} (from inventory)")
                rec["available_shares"] = inv_total
            for i in range(1, grid + 1):
                if str(i) not in inv:
                    warnings.append(f"[{code}] ⚠️ UNRECOVERABLE: grid={grid} slot {i} missing from inventory!")

    return warnings


# =====================================================
# Pending 辅助函数（补丁 B + C）
# =====================================================
def _is_order_pending(code: str, grid_level: int) -> bool:
    """检查 (code, grid_level) 是否已有在途委托（补丁 B：防重复发单）"""
    with _pending_orders_lock:
        return any(
            m["code"] == code and m["grid_level"] == grid_level
            for m in _pending_orders.values()
        )


def _write_fill_to_ledger(code: str, grid_level: int, direction: str,
                          filled_qty: int, filled_px: float):
    """回调线程 & 巡检线程共用：将一笔实际成交写入 grid_inventory

    [一致性铁律] 每次写入后强制同步三字段：
      available_shares = _sum_available(inventory)  (holding 格总股数)
      current_grid     = _get_max_holding_grid(inventory)  (最深 holding 格号)
    确保 current_grid 与 grid_inventory 永远强同构，无需人工对账。
    """
    ledger = load_ledger()
    rec = ledger.get(code)
    if not isinstance(rec, dict):
        log(f"⚠️ [Fill写账] {code} 账本无记录，跳过")
        return

    inventory = rec.setdefault("grid_inventory", {})
    key = str(grid_level)

    if direction == "buy":
        slot = inventory.setdefault(key, {"buy_price": 0.0, "filled_qty": 0, "status": "holding"})
        prev_qty = slot["filled_qty"]
        prev_px  = slot["buy_price"]
        total_qty = prev_qty + filled_qty
        if total_qty > 0:
            slot["buy_price"] = round((prev_px * prev_qty + filled_px * filled_qty) / total_qty, 4)
        slot["filled_qty"] = total_qty
        slot["status"]     = "holding"

    elif direction == "sell":
        if key in inventory:
            slot = inventory[key]
            slot["filled_qty"] = max(0, slot["filled_qty"] - filled_qty)
            if slot["filled_qty"] == 0:
                slot["status"] = "closed"
        # 如 key 不存在（已 closed），忽略

    # ── [铁律] 强制同步 available_shares + current_grid ──
    rec["available_shares"] = _sum_available(inventory)
    rec["current_grid"]     = _get_max_holding_grid(inventory)   # ← 关键：不靠乐观指针
    rec["grid_inventory"]   = inventory
    ledger[code] = rec
    save_ledger(ledger)


def _reconcile_stale_order(seq: int, meta: dict, xt_trader, acc):
    """补丁 C：对账超时 pending 订单 — 主动查 QMT 成交，补写或回滚账本"""
    code       = meta["code"]
    grid_level = meta["grid_level"]
    direction  = meta["direction"]
    target_qty = meta["target_qty"]

    try:
        # 查当日成交列表
        trades = xt_trader.query_stock_trades(acc, code)
        matched = [t for t in (trades or []) if t.order_id == seq]
    except Exception as e:
        log(f"  ❌ [Pending对账] seq={seq} 查询失败: {e}")
        matched = []

    if matched:
        # 有成交记录 → 补写账本
        total_filled = sum(t.traded_volume for t in matched)
        avg_px = sum(t.traded_price * t.traded_volume for t in matched) / total_filled
        log(f"  ✅ [Pending对账] seq={seq} 补记成交 {code} {direction} qty={total_filled} px={avg_px:.4f}")
        _write_fill_to_ledger(code, grid_level, direction, total_filled, avg_px)
        append_action_log("对账补记", code, avg_px, "Pending超时主动对账",
                          {"seq": seq, "direction": direction, "qty": total_filled})
    else:
        # 无成交 → 视为废单，回滚 current_grid
        log(f"  🗑️ [Pending对账] seq={seq} 无成交，视为废单，回滚 current_grid")
        ledger = load_ledger()
        rec = ledger.get(code)
        if isinstance(rec, dict):
            if direction == "buy":
                rec["current_grid"] = max(0, int(rec.get("current_grid", 0)) - 1)
            elif direction == "sell":
                rec["current_grid"] = min(MAX_GRID_DEPTH, int(rec.get("current_grid", 0)) + 1)
            ledger[code] = rec
            save_ledger(ledger)
        send_webhook("⚠️ T1 废单回滚", f"[{code}] seq={seq} grid={grid_level} {direction} 废单，已回滚")

    with _pending_orders_lock:
        _pending_orders.pop(seq, None)


def _sweep_stale_pending(xt_trader, acc):
    """补丁 C：主循环每 PENDING_SWEEP_SEC 秒调用一次，清理超时在途委托"""
    now = time.time()
    with _pending_orders_lock:
        stale = {seq: m for seq, m in _pending_orders.items()
                 if now - m.get("sent_at", now) > PENDING_TIMEOUT_SEC}
    for seq, meta in stale.items():
        log(f"  ⚠️ [Pending超时] seq={seq} code={meta['code']} grid={meta['grid_level']}，主动对账")
        _reconcile_stale_order(seq, meta, xt_trader, acc)


# =====================================================
# 白名单 + slot_capital 读取
# =====================================================
def _infer_exchange_suffix(code: str) -> str:
    """
    根据代码前缀规则自动判断交易所后缀。
    沪市（SH）：5、6 开头
    深市（SZ）：0、1、2、3 开头
    """
    prefix = code[:1] if code else "0"
    if prefix in ("5", "6"):
        return f"{code}.SH"
    return f"{code}.SZ"


def load_whitelist_map() -> dict:
    """
    返回 {code: {name}} 字典。
    ⚠️ 不再从白名单读取资金参数，symbol_max_limit / per_grid_capital 均从账本读取，
    账本值由 t1_master.py 每日盘前强制写入。

    格式兼容：
      - 顶级键 'etf_list' 或 'whitelist' 均可
      - code 支持带后缀（'512890.SH'）或纯数字（'517900'），纯数字自动补全后缀
    """
    if not WHITELIST_FILE.exists():
        log(f"❌ 白名单不存在: {WHITELIST_FILE}")
        return {}
    try:
        with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # 兼容 etf_list / whitelist 两种顶级键名
        items = data.get("etf_list") or data.get("whitelist") or []
        wl = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", "")).strip()
            if not code:
                continue
            # 自动补全交易所后缀
            if "." not in code:
                code = _infer_exchange_suffix(code)
            wl[code] = {"name": item.get("name", code)}
        log(f"✅ 白名单加载完毕，共 {len(wl)} 只标的")
        return wl
    except Exception as e:
        log(f"❌ 白名单读取失败: {e}")
        return {}



# =====================================================
# 策略隔离防火墙（quant-v4-patterns §1）
# =====================================================
def load_other_strategy_codes() -> set:
    """
    读取其他策略的账本/持仓文件，获取所有已被占用的标的。
    T1 执行器对这些标的完全失明，绝不干预。
    """
    protected: set = set()

    # T0 网格
    t0_target = _DIR / ".state" / "grid_targets.yaml"
    if t0_target.exists():
        try:
            with open(t0_target, "r", encoding="utf-8") as f:
                d = yaml.safe_load(f) or {}
            protected.update(d.keys())
        except Exception:
            pass

    # ETF 轮动（双源：targets + holdings）
    rot_targets = _DIR / ".state" / "rotation_targets.yaml"
    if rot_targets.exists():
        try:
            with open(rot_targets, "r", encoding="utf-8") as f:
                d = yaml.safe_load(f) or {}
            protected.update(d.get("targets", []))
        except Exception:
            pass

    rot_holdings = _DIR / ".state" / "rotation_holdings.json"
    if rot_holdings.exists():
        try:
            with open(rot_holdings, "r", encoding="utf-8") as f:
                protected.update((json.load(f) or {}).keys())
        except Exception:
            pass

    # Sniper（小虎）
    sniper_file = _DIR / ".state" / "sniper_holdings.json"
    if sniper_file.exists():
        try:
            with open(sniper_file, "r", encoding="utf-8") as f:
                protected.update((json.load(f) or {}).keys())
        except Exception:
            pass

    return protected


# =====================================================
# QMT 连接（与其他策略对齐，5 重重试）
# =====================================================
class T1Callback(XtQuantTraderCallback):
    """T1 网格专属 QMT 回调（Fill-Based Accounting 版）"""

    def on_disconnected(self):
        msg = "🚨 T1 网格：QMT 交易网关已断开！"
        log(msg)
        send_webhook("🚨 T1_Grid QMT 断开", msg)

    def on_stock_order(self, order):
        log(f"📝 [报单] {order.stock_code} | {order.order_remark} | 价格={order.price:.4f} | 状态={order.order_status}")

    def on_stock_trade(self, trade):
        """成交回调 — fill-based 账本写入（支持 QMT 拆单/部分成交）

        [铁律修复] QMT 可能将一笔委托拆成多笔成交推送（partial fills）。
        原来 pop() 在第一笔成交就删掉 pending，导致后续分笔成交被误认为外部单。
        修复：用 get() + 累加已成交量，等累计 == target_qty 时才 pop。
        """
        filled_qty = trade.traded_volume
        filled_px  = trade.traded_price

        # 1. 从 pending 取出元数据（不 pop — 支持分笔成交）
        with _pending_orders_lock:
            meta = _pending_orders.get(trade.order_id, None)

        if meta is None:
            # 非本引擎委托（手动下单等），仅记日志
            action_verb = "买入" if trade.order_remark == ORDER_BUY else "卖出"
            log(f"✅ [T1成交·外部] {trade.stock_code} | {action_verb} | 价={filled_px:.4f} qty={filled_qty}")
            return

        code       = meta["code"]
        direction  = meta["direction"]
        grid_level = meta["grid_level"]
        target_qty = meta.get("target_qty", 0)

        # 2. 累加已成交量到 pending（线程安全）
        with _pending_orders_lock:
            meta = _pending_orders.get(trade.order_id)
            if meta:
                meta["filled_so_far"] = meta.get("filled_so_far", 0) + filled_qty
                filled_so_far = meta["filled_so_far"]
                # 完全成交（或超额）才从 pending 移除
                if target_qty > 0 and filled_so_far >= target_qty:
                    _pending_orders.pop(trade.order_id, None)
            else:
                filled_so_far = filled_qty

        # 3. Fill-based 写账本（每笔分成交都写，_write_fill_to_ledger 内部做加权累计）
        _write_fill_to_ledger(code, grid_level, direction, filled_qty, filled_px)

        action_verb = "买入" if direction == "buy" else "卖出"
        msg = (
            f"✅ [T1成交·Fill] {code} | {action_verb} grid={grid_level}"
            f" | 成交价={filled_px:.4f} | 数量={filled_qty}"
            f" | 累计={filled_so_far}/{target_qty}"
        )
        log(msg)
        send_webhook("🤖 T1_Grid 交易战报", msg)
        append_action_log(
            action=action_verb,
            target=code,
            price=filled_px,
            reason="QMT 成交回报",
            extra={"qty": filled_qty, "grid_level": grid_level, "remark": trade.order_remark},
        )

    def on_order_error(self, order_error):
        """委托失败 — 清除 pending 并记录日志"""
        # 清除该 order 的 pending（避免永久占据注册表）
        with _pending_orders_lock:
            stale_seqs = [seq for seq, m in _pending_orders.items()
                          if m["code"] == order_error.stock_code]
            for seq in stale_seqs:
                _pending_orders.pop(seq, None)
        msg = f"❌ [T1下单失败] {order_error.stock_code} | {order_error.error_msg}"
        log(msg)
        send_webhook("🚨 T1_Grid 下单失败", msg)
        append_action_log(
            action="挂单失败",
            target=order_error.stock_code,
            price=0.0,
            reason=order_error.error_msg,
        )


def init_qmt_trader():
    """
    初始化 QMT 实盘接口，5 重重试。
    quant-safe-patterns: start() 后必须 sleep(5) 再 connect()。
    """
    if not ACCOUNT_ID or not QMT_PATH:
        log("❌ [配置缺失] ACCOUNT_ID 或 QMT_PATH 未设置，请检查 .env 文件。")
        return None, None

    session_id = random.randint(100000, 999999)
    trader = XtQuantTrader(QMT_PATH, session_id)
    trader.register_callback(T1Callback())
    trader.start()
    time.sleep(5)   # ← 规范等待，不可省略（quant-safe-patterns）

    for attempt in range(1, MAX_CONN_RETRY + 1):
        result = trader.connect()
        if result == 0:
            break
        log(f"   ⏳ QMT 连接未就绪 (rc={result})，10s 后重试 ({attempt}/{MAX_CONN_RETRY})...")
        time.sleep(10)
    else:
        msg = f"QMT 连接失败，已重试 {MAX_CONN_RETRY} 次"
        log(f"❌ {msg}")
        send_webhook("🚨 T1_Grid QMT 连接失败", msg)
        trader.stop()
        return None, None

    acc = StockAccount(ACCOUNT_ID)
    trader.subscribe(acc)
    log(f"✅ [系统就绪] QMT 实盘接口连接成功！PID={os.getpid()} Session={session_id}")
    return trader, acc


# =====================================================
# 盘口保护窗口检查
# =====================================================
def _now_hhmm() -> str:
    return datetime.now().strftime("%H%M")


def _in_guard_window() -> bool:
    now = _now_hhmm()
    return any(s <= now <= e for s, e in GUARD_WINDOWS)


# =====================================================
# Tick 行情安全访问（quant-safe-patterns §2.3）
# =====================================================
def get_tick_safe(code: str) -> dict:
    """
    获取标的 Tick，防御性返回。
    quant-safe-patterns: get_full_tick 字段访问必须 .get() 防御。
    """
    try:
        tick = xtdata.get_full_tick([code]).get(code, {})
        return tick
    except Exception:
        return {}


def get_down_limit(code: str) -> float:
    """获取跌停价，用于 Executioner 清仓"""
    try:
        detail = xtdata.get_instrument_detail(code) or {}
        return float(detail.get("DownLimit", 0.0))
    except Exception:
        return 0.0


# =====================================================
# 价格计算工具
# =====================================================
def calc_buy_price(base_price: float, current_grid: int, dynamic_step: float) -> float:
    """下一格买入触发价 = base * (1 - (current_grid+1) * step)"""
    return base_price * (1.0 - (current_grid + 1) * dynamic_step)


def calc_sell_price(base_price: float, current_grid: int, dynamic_step: float) -> float:
    """上一格卖出触发价 = base * (1 - (current_grid-1) * step)"""
    return base_price * (1.0 - (current_grid - 1) * dynamic_step)


def calc_hard_stop(base_price: float, dynamic_step: float, atr_value: float) -> float:
    """空间熔断强制清仓线 = base * (1 - 4*step) - 1.5*ATR"""
    return base_price * (1.0 - 4.0 * dynamic_step) - 1.5 * atr_value


def _calc_qty(capital: float, price: float) -> int:
    """按指定金额和当前价计算股数（向下取整至 100 倍数）"""
    if price <= 0:
        return 0
    return int(capital / price / 100) * 100


# =====================================================
# Phase-out 僵尸清道夫（淘汰标的双重熔断强平）
# =====================================================
def _run_phaseout_scan(
    ledger: dict,
    active_codes: set,
    xt_trader,
    acc,
    dry_run: bool,
    order_cooldown: dict,
) -> int:
    """
    清道夫巡检：对不在当月白名单但账本仍有 holding 的老标的，
    执行时间×空间双重熔断，强行拔管释放资金。

    触发条件（任一满足即执行）：
      1. 时间衰减：(today - phaseout_watch_since).days >= PHASEOUT_MAX_HOLD_DAYS
      2. 空间破位：当前市价 <= 最低持仓成本 * (1 - PHASEOUT_MAX_DRAWDOWN)

    返回：本次触发强平的标的数量
    """
    today = date.today()
    today_str = today.strftime("%Y-%m-%d")
    phaseout_cnt = 0
    ledger_dirty = False

    for code, rec in ledger.items():
        if not isinstance(rec, dict):
            continue

        # ── 只处理不在白名单的老标的 ─────────────────────────────
        if code in active_codes:
            continue

        # ── 检查是否有 holding 仓位 ──────────────────────────────
        inventory = dict(rec.get("grid_inventory") or {})
        holding_slots = {
            k: v for k, v in inventory.items()
            if v.get("status") == "holding" and int(v.get("filled_qty", 0)) > 0
        }
        if not holding_slots:
            continue  # 老标的已空仓，无需清道夫

        # ── 首次发现：打印监控激活日志，写入 watch_since ─────────
        if not rec.get("phaseout_watch_since"):
            rec["phaseout_watch_since"] = today_str
            ledger[code] = rec
            ledger_dirty = True
            log(f"  [{code}] 👁️  [清道夫激活] 老标的进入留党察看期，监控起始日={today_str}")

        # ── 计算持仓天数 ──────────────────────────────────────────
        try:
            watch_since = date.fromisoformat(rec["phaseout_watch_since"])
            hold_days = (today - watch_since).days
        except Exception:
            hold_days = 0

        # ── 获取当前价格 ──────────────────────────────────────────
        tick = get_tick_safe(code)
        last_price = tick.get("lastPrice", 0.0)
        bid1 = (tick.get("bidPrice") or [0])[0]

        if last_price <= 0:
            log(f"  [{code}] ⚠️  [清道夫] Tick 无效，暂跳过熔断判定。")
            continue

        # ── 提取最低持仓成本（空间破位锚点） ─────────────────────
        cost_prices = [
            float(v.get("buy_price", 0.0))
            for v in holding_slots.values()
            if float(v.get("buy_price", 0.0)) > 0
        ]
        min_cost = min(cost_prices) if cost_prices else 0.0

        # ── 双重熔断判定 ──────────────────────────────────────────
        trigger_time  = (hold_days >= PHASEOUT_MAX_HOLD_DAYS)
        trigger_space = (min_cost > 0 and last_price <= min_cost * (1.0 - PHASEOUT_MAX_DRAWDOWN))

        # 打印监控状态（仅在非触发时也记录观察状态）
        space_pct = (last_price / min_cost - 1.0) * 100 if min_cost > 0 else 0
        log(
            f"  [{code}] 🔍 [清道夫监控] "
            f"持仓{hold_days}天/{PHASEOUT_MAX_HOLD_DAYS}天 | "
            f"现价={last_price:.4f} 最低成本={min_cost:.4f} "
            f"浮动={space_pct:+.1f}%/{-PHASEOUT_MAX_DRAWDOWN*100:.0f}% | "
            f"触发={'时间' if trigger_time else ''}{'空间' if trigger_space else ''}{'无' if not trigger_time and not trigger_space else ''}"
        )

        if not (trigger_time or trigger_space):
            continue  # 双重熔断均未触发，继续等待

        # ── 无情处决：物理清仓协议 ────────────────────────────────
        reason_tag = "时间" if trigger_time else "空间"
        if trigger_time and trigger_space:
            reason_tag = "时间+空间"

        # 查物理真实持仓（V4.0 物理清仓保障铁律 §2）
        physical_qty = 0
        if not dry_run and xt_trader:
            try:
                pos_list = xt_trader.query_stock_positions(acc) or []
                target_pos = next((p for p in pos_list if p.stock_code == code), None)
                if target_pos:
                    physical_qty = int(target_pos.volume)
            except Exception as e:
                log(f"  [{code}] ❌ [清道夫] 查询物理持仓失败: {e}，回退用账本数量")
                physical_qty = int(rec.get("available_shares", 0))
        else:
            # dry-run 或无 trader：用账本数量
            physical_qty = int(rec.get("available_shares", 0))

        if physical_qty <= 0:
            log(f"  [{code}] ⚠️  [清道夫] 物理持仓=0，直接清理账本。")
        else:
            # 使用买一价挂单（尽量成交），无买一价则用 lastPrice
            exec_price = bid1 if bid1 > 0 else last_price
            estimated_value = round(physical_qty * exec_price, 2)

            if not dry_run and xt_trader:
                seq = xt_trader.order_stock(
                    acc, code,
                    xtconstant.STOCK_SELL,
                    physical_qty,
                    xtconstant.FIX_PRICE,
                    exec_price,
                    STRATEGY_NAME,
                    ORDER_PHASEOUT,
                )
            else:
                seq = 66666
                log(
                    f"  [{code}] [DRY-RUN] PHASEOUT SELL {physical_qty}@{exec_price:.4f} "
                    f"≈ {estimated_value:.0f}元"
                )

            if seq > 0 or dry_run:
                log(
                    f"\033[31m  🗑️  [僵尸清道夫] 老标的 {code} 触发({reason_tag})熔断，"
                    f"已强平释放资金 {estimated_value:.0f} 元，seq={seq}\033[0m"
                )
                send_webhook(
                    f"🗑️ T1 Phase-out 强平",
                    f"[{code}] {reason_tag}熔断\n"
                    f"持仓={hold_days}天 | 最低成本={min_cost:.4f} | 现价={last_price:.4f}\n"
                    f"强平 {physical_qty} 股 ≈ {estimated_value:.0f} 元",
                )
                append_action_log(
                    action="Phase-out强平",
                    target=code,
                    price=exec_price,
                    reason=f"Phase-out/{reason_tag}熔断",
                    extra={
                        "qty": physical_qty,
                        "seq": seq,
                        "hold_days": hold_days,
                        "min_cost": min_cost,
                        "estimated_value": estimated_value,
                        "trigger": reason_tag,
                    },
                )
                order_cooldown[code] = time.time()
                phaseout_cnt += 1
            else:
                log(f"  [{code}] ❌ [清道夫] 强平下单失败 seq={seq}")
                continue  # 下单失败不清账本

        # ── 账本销毁：将所有 slot 标为 force_closed ───────────────
        for slot_key, slot_val in inventory.items():
            if slot_val.get("status") == "holding":
                slot_val["status"] = "force_closed"
                slot_val["force_closed_date"] = today_str
                slot_val["force_close_reason"] = reason_tag
        rec["grid_inventory"]   = inventory
        rec["available_shares"] = 0
        rec["current_grid"]     = 0
        rec["phaseout_closed"]  = True
        rec["phaseout_closed_date"] = today_str
        ledger[code] = rec
        ledger_dirty = True

    if ledger_dirty:
        save_ledger(ledger)

    return phaseout_cnt


# =====================================================
# 主执行引擎
# =====================================================
def run_grid(dry_run: bool = False):

    # ── 0. 文件锁保护（防多开） ──────────────────────────────────
    _DIR / ".state" and (_DIR / ".state").mkdir(exist_ok=True)
    if not acquire_lock():
        return

    try:
        log("=" * 68)
        log("🚀 T+1 纯机械化网格执行器 启动")
        log(f"   PID={os.getpid()} | 账本={LEDGER_FILE}")
        if dry_run:
            log("🔍 [DRY-RUN 模式] 所有下单指令将只打印，不实际提交")
        log("=" * 68)

        write_probe("starting", note="正在初始化 QMT 连接...")
        send_webhook("🚀 T1_Grid 启动", f"T+1 网格执行器已启动 PID={os.getpid()}")

        # ── 1. 加载白名单 ────────────────────────────────────────
        whitelist_map = load_whitelist_map()
        if not whitelist_map:
            log("❌ 白名单为空，执行器退出。")
            write_probe("error", note="白名单为空")
            return
        log(f"📋 白名单标的数: {len(whitelist_map)}")

        # ── 1b. 账本一致性自检（铁律：启动即校验，自愈后继续）────────
        _startup_ledger = load_ledger()
        _heal_warnings  = _check_and_heal_ledger(_startup_ledger)
        if _heal_warnings:
            save_ledger(_startup_ledger)   # 将自愈结果持久化
            for _w in _heal_warnings:
                log(f"  🔧 [启动自检] {_w}")
            _unrecoverable = [w for w in _heal_warnings if "UNRECOVERABLE" in w]
            if _unrecoverable:
                send_webhook("🚨 T1账本一致性告警",
                             "\n".join(_unrecoverable) + "\n⚠️ 请人工核查 t1_grid_ledger.yaml")
        else:
            log("✅ [启动自检] 账本一致性校验通过，无需修复。")

        # ── 2. QMT 连接 ──────────────────────────────────────────
        if not dry_run:
            xt_trader, acc = init_qmt_trader()
            if not xt_trader:
                write_probe("error", note="QMT 连接失败")
                return
        else:
            xt_trader, acc = None, None

        # ── 3. 订阅行情（白名单 + 账本所有 holding 老标的） ─────────
        all_codes = list(whitelist_map.keys())
        # 将账本中所有 holding 标的也纳入订阅，确保清道夫扫描有实时价格
        _ledger_for_sub = load_ledger()
        for _code, _rec in _ledger_for_sub.items():
            if not isinstance(_rec, dict):
                continue
            _inv = _rec.get("grid_inventory") or {}
            _has_holding = any(v.get("status") == "holding" for v in _inv.values())
            if _has_holding and _code not in all_codes:
                all_codes.append(_code)
                log(f"   📡 [行情扩订] 老标的 {_code} 纳入 Tick 订阅（清道夫监控）")
        for code in all_codes:
            xtdata.subscribe_quote(code, period="tick")
        time.sleep(2)

        # ── 4. 初始化运行统计 ────────────────────────────────────
        buy_cnt         = 0
        sell_cnt        = 0
        exec_cnt        = 0
        _heartbeat_t    = 0.0
        _probe_update_t = 0.0
        _sweep_t        = 0.0   # pending 超时巡检计时器（补丁 C）
        # 每标的下单冷却计时器 {code: last_order_timestamp}
        _order_cooldown: dict[str, float] = {}
        # 日志节流阀：{throttle_key: last_print_timestamp}，同 key 60s 内只打一次
        _log_throttle:   dict[str, float] = {}
        _LOG_THROTTLE_SEC = 60.0

        def _throttled_log(key: str, msg: str) -> None:
            """节流打印：同 key 每 60s 最多落盘一次，防止高频重复日志炸日志文件。"""
            if time.time() - _log_throttle.get(key, 0) >= _LOG_THROTTLE_SEC:
                log(msg)
                _log_throttle[key] = time.time()

        write_probe("running", note="进入盘中轮询...")
        log("✅ 系统就绪，进入盘中 3 秒轮询主循环...")

        # ── 5. 主循环（09:30 ~ 15:00） ───────────────────────────
        while True:
            now_hhmm = _now_hhmm()

            # 到点退出
            if now_hhmm >= "1500":
                log("🔔 15:00 收盘，T1 执行器正常退出。")
                write_probe("stopped", buy_cnt, sell_cnt, "收盘正常退出")
                send_webhook("🏁 T1_Grid 收盘退出", f"今日 买{buy_cnt} 卖{sell_cnt} 熔断{exec_cnt}")
                break

            # 未到开盘时间
            if now_hhmm < "0930":
                time.sleep(5)
                continue

            # 心跳日志
            if time.time() - _heartbeat_t > HEARTBEAT_SEC:
                log(f"💓 [Heartbeat] {datetime.now().strftime('%H:%M:%S')} | 今日 买{buy_cnt} 卖{sell_cnt} 熔断{exec_cnt}")
                _heartbeat_t = time.time()

            # 补丁 C：pending 超时巡检（每 PENDING_SWEEP_SEC 秒一次）
            if not dry_run and xt_trader and time.time() - _sweep_t > PENDING_SWEEP_SEC:
                _sweep_stale_pending(xt_trader, acc)
                _sweep_t = time.time()

            # 定时更新探针
            if time.time() - _probe_update_t > 60:
                write_probe("running", buy_cnt, sell_cnt, f"{now_hhmm} 轮询中")
                _probe_update_t = time.time()

            # 盘口保护窗口（节流：60s 内只打一次）
            if _in_guard_window():
                _throttled_log("guard_window", f"[{now_hhmm}] 🛡️ 盘口保护窗口，跳过本轮委托。")
                time.sleep(5)
                continue

            # ── 5a. 每轮重新加载账本（允许 master 修改立即生效）──────
            ledger = load_ledger()
            # ── 5b. 加载策略隔离防火墙 ──────────────────────────────
            protected_codes = load_other_strategy_codes()

            # ── 5b2. 僵尸清道夫扫描（每轮对老标的做双重熔断判定） ────
            _active_candidates = set(whitelist_map.keys())
            _phaseout_fired = _run_phaseout_scan(
                ledger=ledger,
                active_codes=_active_candidates,
                xt_trader=xt_trader,
                acc=acc,
                dry_run=dry_run,
                order_cooldown=_order_cooldown,
            )
            if _phaseout_fired:
                exec_cnt += _phaseout_fired
                # 清道夫已写账本，重新加载确保后续逻辑读到最新状态
                ledger = load_ledger()

            # ── 5c. 逐标的处理 ──────────────────────────────────────
            for code, wl_cfg in whitelist_map.items():

                # ── 策略隔离防火墙（节流：60s 内只打一次）────────────
                if code in protected_codes:
                    _throttled_log(f"fw_{code}", f"  [{code}] 🧱 [Firewall] 被其他策略占用，T1 失明跳过。")
                    continue

                rec = ledger.get(code)
                if not isinstance(rec, dict):
                    # 账本中还未初始化该标的（master 首次运行前），跳过
                    _throttled_log(f"uninit_{code}", f"  [{code}] ⏭  账本未初始化，等待 t1_master 首次运行。")
                    continue

                # ── 读取账本字段（全部防御性 .get()） ────────────────
                base_price       = float(rec.get("base_price", 0.0))
                dynamic_step     = float(rec.get("dynamic_step", 0.012))
                atr_value        = float(rec.get("atr_value", 0.0))
                current_grid     = int(rec.get("current_grid", 0))
                available_shares = int(rec.get("available_shares", 0))
                locked_shares    = int(rec.get("locked_shares", 0))
                idle_days        = int(rec.get("idle_days", 0))
                cooldown_until   = str(rec.get("cooldown_until", "2000-01-01"))

                # grid_inventory（fill-based 物理账本）
                inventory = dict(rec.get("grid_inventory") or {})
                # 向后展容：还没有 grid_inventory 时用旧字段建临时 inventory（只读）
                if not inventory and available_shares > 0 and current_grid > 0:
                    avg_qty = available_shares // current_grid
                    for gi in range(1, current_grid + 1):
                        inventory[str(gi)] = {"buy_price": base_price, "filled_qty": avg_qty, "status": "holding"}

                # 资金参数：由 t1_master 每日盘前写入账本，此处直接读取
                symbol_max_limit = float(rec.get("symbol_max_limit", 20000))
                per_grid_capital = float(rec.get("per_grid_capital", 5000))

                # ── Cooldown 冷却期检查（节流：60s 内只打一次）────────
                today_str = date.today().strftime("%Y-%m-%d")
                if cooldown_until >= today_str:
                    _throttled_log(f"cd_{code}", f"  [{code}] ❄️  冷却期至 {cooldown_until}，静默跳过。")
                    continue

                # ── 跳过未建网标的（base_price 为 0，节流）──────────
                if base_price <= 0:
                    _throttled_log(f"bp_{code}", f"  [{code}] ⏭  base_price=0，等待 t1_master 建网。")
                    continue

                # ── 获取 Tick ──────────────────────────────────────
                tick        = get_tick_safe(code)
                last_price  = tick.get("lastPrice", 0.0)
                ask1        = (tick.get("askPrice") or [0])[0]   # 卖一（买入挂单用）
                bid1        = (tick.get("bidPrice") or [0])[0]   # 买一（卖出挂单用）
                volume      = tick.get("volume", 0)

                if last_price <= 0 or volume == 0:
                    _throttled_log(f"tick_{code}", f"  [{code}] ⚠️  Tick 无效 (price={last_price} vol={volume})，跳过。")
                    continue

                # ── 计算动态网格线 ────────────────────────────────
                buy_trigger   = calc_buy_price(base_price, current_grid, dynamic_step)
                sell_trigger  = calc_sell_price(base_price, current_grid, dynamic_step)
                hard_stop     = calc_hard_stop(base_price, dynamic_step, atr_value)

                # ── 下单冷却检查 ──────────────────────────────────
                last_order_t = _order_cooldown.get(code, 0.0)
                in_cooldown  = (time.time() - last_order_t) < ORDER_COOLDOWN_SEC

                # ┌─────────────────────────────────────────────────
                # │ 优先级 1：Executioner（无条件机械清仓）
                # └─────────────────────────────────────────────────
                trigger_space = False  # CEO指令：彻底关闭空间熔断，打满 4 格后无条件装死扛单，拒绝割肉
                trigger_time  = (idle_days >= 15 and available_shares > 0)

                if (trigger_space or trigger_time) and available_shares > 0:
                    reason_tag = "空间熔断" if trigger_space else "时间衰减"
                    log(
                        f"  [{code}] ☠️  [Executioner/{reason_tag}] "
                        f"available={available_shares} grid={current_grid} "
                        f"idle={idle_days} price={last_price:.4f} hard_stop={hard_stop:.4f}"
                    )
                    send_webhook(
                        f"☠️ T1_Grid Executioner 触发",
                        f"[{code}] {reason_tag} | 清仓 {available_shares} 股 | 现价={last_price:.4f}",
                    )

                    # 取跌停价（极端行情确保成交），无法获取则用 lastPrice
                    exec_price = get_down_limit(code)
                    if exec_price <= 0:
                        exec_price = last_price

                    if not dry_run and xt_trader:
                        seq = xt_trader.order_stock(
                            acc, code,
                            xtconstant.STOCK_SELL,
                            available_shares,
                            xtconstant.FIX_PRICE,
                            exec_price,
                            STRATEGY_NAME,
                            ORDER_EXECUTIONER,
                        )
                    else:
                        seq = 99999
                        log(f"  [{code}] [DRY-RUN] SELL {available_shares}@{exec_price:.4f} (Executioner)")

                    if seq > 0 or dry_run:
                        # 账本清零 + 设置冷却期
                        cooldown_date = (date.today() + timedelta(days=10)).strftime("%Y-%m-%d")
                        rec["available_shares"] = 0
                        rec["locked_shares"]    = 0
                        rec["current_grid"]     = 0
                        rec["base_price"]       = 0.0
                        rec["idle_days"]        = 0
                        rec["cooldown_until"]   = cooldown_date
                        ledger[code]            = rec
                        save_ledger(ledger)

                        exec_cnt += 1
                        _order_cooldown[code] = time.time()
                        append_action_log(
                            action="机械清仓",
                            target=code,
                            price=exec_price,
                            reason=f"Executioner/{reason_tag}",
                            extra={
                                "qty":            available_shares,
                                "seq":            seq,
                                "cooldown_until": cooldown_date,
                                "trigger":        reason_tag,
                            },
                        )
                        log(f"  [{code}] ✅ Executioner 清仓委托已发 seq={seq}，冷却至 {cooldown_date}")
                    else:
                        log(f"  [{code}] ❌ Executioner 下单失败 seq={seq}")
                    continue  # Executioner 触发后跳过后续逻辑

                # ┌─────────────────────────────────────────────────
                # │ 优先级 2：网格收割（卖出）——第三定律：对称清零
                # └─────────────────────────────────────────────────
                sell_grid = _get_max_holding_grid(inventory)

                if sell_grid > 0:
                    sell_trigger_for_grid = calc_sell_price(base_price, sell_grid, dynamic_step)
                    if (
                        last_price >= sell_trigger_for_grid
                        and not in_cooldown
                        and not _is_order_pending(code, sell_grid)
                    ):
                        slot = inventory.get(str(sell_grid), {})
                        sell_qty   = int(slot.get("filled_qty", 0))
                        sell_price = bid1 if bid1 > 0 else last_price
                        if sell_qty <= 0:
                            log(f"  [{code}] ⚠️  [Grid{sell_grid}] filled_qty=0，跳过卖出。")
                        else:
                            log(
                                f"  [{code}] 🟢 [卖出] 现价={last_price:.4f} >= 触发={sell_trigger_for_grid:.4f} "
                                f"挂买一={sell_price:.4f} qty={sell_qty} grid={sell_grid}"
                            )
                            if not dry_run and xt_trader:
                                seq = xt_trader.order_stock(
                                    acc, code, xtconstant.STOCK_SELL, sell_qty,
                                    xtconstant.FIX_PRICE, sell_price, STRATEGY_NAME, ORDER_SELL,
                                )
                            else:
                                seq = 88888
                                log(f"  [{code}] [DRY-RUN] SELL {sell_qty}@{sell_price:.4f} grid={sell_grid}")
                            if seq > 0 or dry_run:
                                with _pending_orders_lock:
                                    _pending_orders[seq] = {
                                        "code": code, "grid_level": sell_grid,
                                        "direction": "sell", "target_qty": sell_qty,
                                        "sent_at": time.time(),
                                    }
                                rec["current_grid"] = sell_grid - 1
                                rec["idle_days"]    = 0
                                ledger[code] = rec
                                save_ledger(ledger)
                                sell_cnt += 1
                                _order_cooldown[code] = time.time()
                                append_action_log(
                                    action="卖出", target=code, price=sell_price,
                                    reason=f"网格收割 grid {sell_grid}→{sell_grid-1}",
                                    extra={"qty": sell_qty, "seq": seq, "trigger": sell_trigger_for_grid},
                                )
                            else:
                                log(f"  [{code}] ❌ 卖出下单失败 seq={seq}")
                        continue
                    elif last_price >= sell_trigger_for_grid and _is_order_pending(code, sell_grid):
                        log(f"  [{code}] ⏳ Grid{sell_grid} 已有在途委托，跳过。")
                        continue

                # 安全初始化 one_grid_qty（旧版遗留，防止 UnboundLocalError）
                one_grid_qty = 0

                if (
                    current_grid > 0
                    and last_price >= sell_trigger
                    and available_shares >= one_grid_qty
                    and one_grid_qty > 0
                    and not in_cooldown
                ):
                    sell_qty   = one_grid_qty
                    sell_price = bid1 if bid1 > 0 else last_price   # 挂买一价（taker 委托）

                    log(
                        f"  [{code}] 🟢 [卖出] 现价={last_price:.4f} >= 触发={sell_trigger:.4f} "
                        f"挂买一={sell_price:.4f} qty={sell_qty} avail={available_shares}"
                    )

                    if not dry_run and xt_trader:
                        seq = xt_trader.order_stock(
                            acc, code,
                            xtconstant.STOCK_SELL,
                            sell_qty,
                            xtconstant.FIX_PRICE,
                            sell_price,
                            STRATEGY_NAME,
                            ORDER_SELL,
                        )
                    else:
                        seq = 88888
                        log(f"  [{code}] [DRY-RUN] SELL {sell_qty}@{sell_price:.4f}")

                    if seq > 0 or dry_run:
                        rec["available_shares"] = available_shares - sell_qty
                        rec["current_grid"]     = current_grid - 1
                        rec["idle_days"]        = 0
                        ledger[code]            = rec
                        save_ledger(ledger)

                        sell_cnt += 1
                        _order_cooldown[code] = time.time()
                        append_action_log(
                            action="卖出",
                            target=code,
                            price=sell_price,
                            reason=f"网格收割 grid {current_grid}→{current_grid-1}",
                            extra={"qty": sell_qty, "seq": seq, "trigger": sell_trigger},
                        )
                    else:
                        log(f"  [{code}] ❌ 卖出下单失败 seq={seq}")
                    continue  # 卖出触发后本标的本轮结束

                elif (
                    current_grid > 0
                    and last_price >= sell_trigger
                    and available_shares < one_grid_qty
                ):
                    _throttled_log(
                        f"sell_chk_{code}",
                        f"  [{code}] ⚠️  [卖出强校验] 可用股数不足 "
                        f"(avail={available_shares} < need={one_grid_qty})，跳过。"
                    )

                # ┌─────────────────────────────────────────────────
                # │ 优先级 3：防跳空建仓（买入）— 三重安全校验
                # └─────────────────────────────────────────────────
                # 14:30 后 buy_price 额外向下拓宽 0.5% 安全垫
                effective_buy_trigger = buy_trigger
                if now_hhmm >= "1430":
                    effective_buy_trigger = buy_trigger * (1.0 - 0.005)

                # ── 物理价差锁（Dynamic Price Spacing Lock）─────────
                # 核心法则：下一格触发线不得高于「当前最低持仓成交价 - 1个完整step」
                # 防止价格跌穿多格后在同一价位连续买入，消耗子弹却无法拉开成本空间。
                # 零持仓时 _get_min_holding_price 返回 inf，价差锁自动透明，首格正常建仓。
                _min_fill_px = _get_min_holding_price(inventory)
                _dynamic_ceiling = _min_fill_px * (1.0 - dynamic_step)
                if _dynamic_ceiling < effective_buy_trigger:
                    log(
                        f"  [{code}] 🔒 [价差锁] 最低持仓价={_min_fill_px:.4f} "
                        f"动态天花板={_dynamic_ceiling:.4f} "
                        f"(原触发={effective_buy_trigger:.4f} 现价={last_price:.4f}) "
                        f"→ {'✅通过' if last_price <= _dynamic_ceiling else '🚫拦截，价差不足一格'}"
                    )
                    effective_buy_trigger = _dynamic_ceiling
                # ────────────────────────────────────────────────────

                if (
                    current_grid < MAX_GRID_DEPTH
                    and last_price <= effective_buy_trigger
                    and not in_cooldown
                ):
                    # ── 校验①：计算单格股数（per_grid_capital，不是封顶额）──
                    buy_qty = _calc_qty(per_grid_capital, last_price)
                    if buy_qty < 100:
                        log(
                            f"  [{code}] ⚠️  资金不足一手 "
                            f"(per_grid={per_grid_capital:.0f} price={last_price:.4f})，跳过。"
                        )
                        continue

                    # ── 校验②：局部资金硬顶（Hard Cap Check）───────────────
                    current_holding    = available_shares + locked_shares
                    projected_exposure = (current_holding + buy_qty) * last_price
                    if projected_exposure > symbol_max_limit:
                        log(
                            f"  [{code}] 🛑 [HardCap] 触发资金硬顶，拒绝加仓 "
                            f"(持仓={current_holding}股 + 拟买={buy_qty}股) "
                            f"× {last_price:.4f} = {projected_exposure:.0f} "
                            f"> 上限 {symbol_max_limit:.0f}"
                        )
                        continue

                    # ── 校验③：防跳空瀑布 —— 单次仅 +1 格，绝不连跳 ────────
                    # 无论价格跌穿多少格，current_grid 仅 += 1，buy_qty 仅 per_grid_capital
                    buy_price = ask1 if ask1 > 0 else last_price   # 挂卖一价（taker 委托）

                    log(
                        f"  [{code}] 🔴 [买入] 现价={last_price:.4f} <= 触发={effective_buy_trigger:.4f} "
                        f"挂卖一={buy_price:.4f} qty={buy_qty}({per_grid_capital:.0f}元) "
                        f"grid {current_grid}→{current_grid+1} | "
                        f"预期暴露={projected_exposure:.0f}/{symbol_max_limit:.0f}元"
                    )

                    if not dry_run and xt_trader:
                        seq = xt_trader.order_stock(
                            acc, code,
                            xtconstant.STOCK_BUY,
                            buy_qty,
                            xtconstant.FIX_PRICE,
                            buy_price,
                            STRATEGY_NAME,
                            ORDER_BUY,
                        )
                    else:
                        seq = 77777
                        log(f"  [{code}] [DRY-RUN] BUY {buy_qty}@{buy_price:.4f} (per_grid={per_grid_capital:.0f})")

                    if seq > 0 or dry_run:
                        # 补丁 B：注册 pending，不再乐观写 available_shares
                        with _pending_orders_lock:
                            _pending_orders[seq] = {
                                "code": code, "grid_level": current_grid + 1,
                                "direction": "buy", "target_qty": buy_qty,
                                "sent_at": time.time(),
                            }
                        rec["current_grid"] = current_grid + 1   # 仅更新 grid（防同帧重复触发）
                        rec["idle_days"]    = 0
                        ledger[code]        = rec
                        save_ledger(ledger)

                        buy_cnt += 1
                        _order_cooldown[code] = time.time()
                        append_action_log(
                            action="买入",
                            target=code,
                            price=buy_price,
                            reason=f"防跳空建仓 grid {current_grid}→{current_grid+1}",
                            extra={
                                "qty":               buy_qty,
                                "seq":               seq,
                                "trigger":           effective_buy_trigger,
                                "per_grid_capital":  per_grid_capital,
                                "symbol_max_limit":  symbol_max_limit,
                                "projected_exposure": round(projected_exposure, 2),
                                "after_1430":        now_hhmm >= "1430",
                            },
                        )
                    else:
                        log(f"  [{code}] ❌ 买入下单失败 seq={seq}")

            # ── 5d. 等待下一轮 ──────────────────────────────────────
            time.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        log("⏹️ 手动中断，执行器退出。")
        write_probe("stopped", note="手动中断")
    except Exception as e:
        log(f"🔥 执行器崩溃: {e}")
        traceback.print_exc()
        write_probe("error", note=f"崩溃: {e}")
        send_webhook("🚨 T1_Grid 崩溃", f"执行器异常退出: {e}")
        raise
    finally:
        if xt_trader and not dry_run:
            try:
                xt_trader.stop()
            except Exception:
                pass
        release_lock()
        log("🔓 进程锁已释放，T1 执行器退出完毕。")


# =====================================================
# 主入口
# =====================================================
def main():
    parser = argparse.ArgumentParser(description="T1 Grid Executor — 盘中极速执行")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印交易信号，不实际下单（测试模式）",
    )
    args = parser.parse_args()
    run_grid(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
