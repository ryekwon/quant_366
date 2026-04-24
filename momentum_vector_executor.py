# -*- coding: utf-8 -*-
# ==============================================================================
# 🎯 [部署节点] : Quant-PC
# 📦 [核心职责] : 右侧动量向量执行器 (Momentum Vector Executor)
# ⚙️ [触发时机] : autopilot 09:30 启动，1分钟轮询，15:00 前关闭
# 🔗 [上游]     : .state/momentum_slots.json  (momentum_master 输出的 TOP-N)
#                 .state/t0_absolute_pool.csv  (T+0 白名单，用于 T+0/T+1 分类)
# 🔗 [下游]     : .state/momentum_holdings.json   (本策略独立账本)
#                 .state/momentum_hwm.json         (移动止盈水位线)
#                 .state/momentum_t1_signals.json  (T+1 次日集合竞价挂单信号)
#                 logs/YYYYMMDD_momentum_vector.log
# ⚙️ [算法核心] : 入场 = VWAP 上方 + 涨幅>1% + 开火窗口 09:30-10:30
#                 移动止盈 = 从最高水位线回撤 4% → 立即清仓
#                 硬止损   = 亏损 5% → 立即清仓
#                 T+0标的  = 白名单查表直接卖出
#                 T+1标的  = 当日触发 → 记信号 → 次日 09:25 集合竞价执行
# ==============================================================================
import os
import sys
import json
import csv
import math
import time
import threading
import logging
import random
from datetime import datetime, date, timedelta
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# 🛡️ Windows 控制台 UTF-8 补丁
# ──────────────────────────────────────────────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ──────────────────────────────────────────────────────────────
# xtquant 导入（xtconstant 必须同行，见 quant-safe-patterns §1）
# ──────────────────────────────────────────────────────────────
from xtquant import xtdata, xtconstant
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

# ──────────────────────────────────────────────────────────────
# N8N 推送（失败静默，不阻断主逻辑）
# ──────────────────────────────────────────────────────────────
from dotenv import load_dotenv
try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

load_dotenv()
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")

# ==============================================================================
# 📌 配置区（所有可调参数集中在此，禁止散落在代码中）
# ==============================================================================

_DIR = os.path.dirname(os.path.abspath(__file__))
_STATE_DIR = os.path.join(_DIR, ".state")
_LOG_DIR   = os.path.join(_DIR, "logs")
os.makedirs(_STATE_DIR, exist_ok=True)
os.makedirs(_LOG_DIR, exist_ok=True)

# ── 资金配置 ─────────────────────────────────────────────────
SLOT_COUNT       = 3          # 最大并发仓位槽位数
CAPITAL_PER_SLOT = 30_000     # 每个槽位固定投入（元）

# ── 入场窗口 ─────────────────────────────────────────────────
ENTRY_OPEN_HHMM  = "0930"    # 最早开火时间（集合竞价结束）
ENTRY_CLOSE_HHMM = "1030"    # 最晚入场时间（鱼身捕捉窗口关闭）
NO_NEW_ENTRY_HHMM= "1430"    # 绝对禁止新开仓（14:30之后不开仓）

# ── 入场过滤 ─────────────────────────────────────────────────
MIN_GAIN_PCT     = 0.01       # 日内涨幅至少 1% 才允许入场

# ── 退出条件 ─────────────────────────────────────────────────
TRAILING_STOP_DROP = 0.04     # 移动止盈：从最高水位回撤 4% 触发
HARD_STOP_LOSS     = 0.05     # 硬止损：亏损 5% 无条件清仓

# ── T+1 次日挂单 ─────────────────────────────────────────────
T1_AUCTION_HHMM  = "0925"    # T+1 信号执行：集合竞价时间

# ── 文件路径 ─────────────────────────────────────────────────
SLOTS_JSON     = os.path.join(_STATE_DIR, "momentum_slots.json")
HOLDINGS_JSON  = os.path.join(_STATE_DIR, "momentum_holdings.json")
HWM_JSON       = os.path.join(_STATE_DIR, "momentum_hwm.json")
T1_SIGNALS_JSON= os.path.join(_STATE_DIR, "momentum_t1_signals.json")
# T+0 白名单由 momentum_master.py 在写入 momentum_slots.json 时注入 trade_rule 字段
# Executor 直接读取，无需再查表（quant-v4-patterns §12.5）
T0_POOL_CSV    = os.path.join(_STATE_DIR, "t0_absolute_pool.csv")

# ── 遥测 CSV ─────────────────────────────────────────────────
_TODAY = date.today().strftime("%Y%m%d")
TELEMETRY_CSV = os.path.join(_LOG_DIR, f"{_TODAY}_momentum_telemetry.csv")
_TELEM_FIELDS = [
    "event_type",     # SIGNAL_DETECTED / POSITION_OPENED / HOLDING_LOG / TRAILING_STOP / HARD_STOP / T1_SIGNAL / T1_EXECUTED
    "ts",             # 时间戳 HH:MM:SS
    "code", "name",
    "buy_price",      # 入场成本价（VWAP）
    "current_price",  # 当前现价
    "hwm",            # 历史最高价水位
    "pnl_pct",        # 浮动盈亏 %
    "exit_reason",    # 退出原因
    "qty",            # 持仓手数
    "t0_eligible",    # T+0 可交易（True/False）
]
_TELEM_LOCK = threading.Lock()

# ── 日志 ─────────────────────────────────────────────────────
_LOG_FILE = os.path.join(_LOG_DIR, f"{_TODAY}_momentum_vector.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
_log = logging.getLogger("mvec")

# ==============================================================================
# 📡 遥测 & N8N
# ==============================================================================

def _write_telem(row: dict):
    """线程安全追加遥测行。"""
    with _TELEM_LOCK:
        is_new = not os.path.exists(TELEMETRY_CSV)
        with open(TELEMETRY_CSV, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_TELEM_FIELDS, extrasaction="ignore")
            if is_new:
                w.writeheader()
            row.setdefault("ts", datetime.now().strftime("%H:%M:%S"))
            w.writerow(row)


def send_webhook(title: str, message: str):
    """N8N 推送，失败静默，timeout=5s。（见 quant-v4-patterns §13）"""
    if not _HAS_REQUESTS or not N8N_WEBHOOK_URL:
        return
    try:
        requests.post(
            N8N_WEBHOOK_URL,
            json={"title": title, "message": message,
                  "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            timeout=5,
        )
    except Exception:
        pass

# ==============================================================================
# 🗂️ 账本 IO（所有文件操作集中在此）
# ==============================================================================

_IO_LOCK = threading.Lock()   # 铁律四：账本读写必须加锁


def _load_json(path: str, default):
    """安全读取 JSON，读取失败返回 default。"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _log.warning(f"读取 {path} 失败: {e}，使用默认值")
        return default


def _save_json(path: str, data):
    """原子落盘（tmp → os.replace），防并发写损坏（铁律四）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, path)


def load_holdings() -> dict:
    with _IO_LOCK:
        return _load_json(HOLDINGS_JSON, {})


def save_holdings(holdings: dict):
    with _IO_LOCK:
        _save_json(HOLDINGS_JSON, holdings)


def load_hwm() -> dict:
    with _IO_LOCK:
        return _load_json(HWM_JSON, {})


def save_hwm(hwm: dict):
    with _IO_LOCK:
        _save_json(HWM_JSON, hwm)


def load_slots() -> list:
    return _load_json(SLOTS_JSON, [])


def load_t1_signals() -> dict:
    with _IO_LOCK:
        return _load_json(T1_SIGNALS_JSON, {})


def save_t1_signals(signals: dict):
    with _IO_LOCK:
        _save_json(T1_SIGNALS_JSON, signals)

# ==============================================================================
# 📈 行情工具
# ==============================================================================

def get_tick(code: str) -> dict:
    """获取单只标的最新 Tick（安全访问，见 quant-safe-patterns §2.3）。"""
    try:
        result = xtdata.get_full_tick([code])
        return result.get(code) or {}
    except Exception as e:
        _log.warning(f"get_full_tick({code}) 失败: {e}")
        return {}


def get_current_price(code: str) -> float:
    """获取最新价，失败返回 0.0。"""
    tick = get_tick(code)
    return float(tick.get("lastPrice", 0) or 0)


def get_vwap(code: str) -> float:
    """
    计算日内 VWAP（成交额 / 成交量）。
    使用 get_full_tick 的 amount（累计成交额元）/ volume（累计成交量手×100）。
    注意：xtdata volume 单位为手（100股），amount 单位为元。
    """
    tick = get_tick(code)
    amount = float(tick.get("amount", 0) or 0)   # 元
    volume = float(tick.get("volume", 0) or 0)    # 手
    if volume <= 0 or amount <= 0:
        return 0.0
    # volume（手）→ 股数：×100
    return round(amount / (volume * 100), 4)


def get_pre_close(code: str) -> float:
    """获取昨收价（多字段名兼容）。"""
    tick = get_tick(code)
    return float(
        tick.get("lastClose", 0)
        or tick.get("preClose", 0)
        or 0
    )


def get_name(code: str) -> str:
    """获取标的中文名称，失败返回 code。"""
    try:
        detail = xtdata.get_instrument_detail(code) or {}
        return detail.get("InstrumentName", code) or code
    except Exception:
        return code

# ==============================================================================
# 🔐 Fill-Based 订单追踪（铁律一：绝对禁止乐观更新）
# ==============================================================================

_PENDING: dict = {}          # {seq: {code, name, qty, price, fill_qty, fill_amount, ...}}
_PENDING_LOCK = threading.Lock()   # 铁律四

# 内存封条：已发出卖单但尚未收到回调的标的。
# 账本的撤销必须且只能由 on_stock_trade 回调执行，绝不在发单处乐观删除。
_pending_sells: set = set()   # {code}  铁律一内存封条


class MomentumCallback(XtQuantTraderCallback):
    """
    捕获 Momentum 策略的交易所真实回报。
    Fill-Based 架构：发单注册 _PENDING，仅在 on_stock_trade 回调确认后写账本。
    （铁律一：绝对禁止乐观更新）
    """

    def on_order_stock(self, response):
        oid = response.order_id
        with _PENDING_LOCK:
            meta = _PENDING.get(oid)
        if meta:
            _log.info(f"📝 [报单确认] {meta['name']}({meta['code']}) seq={oid} status={response.order_status}")

    def on_stock_trade(self, response):
        """分笔/全量成交，Fill-Based 累积（参照 sniper_entry_executor 实战模板）。"""
        oid = response.order_id
        with _PENDING_LOCK:
            meta = _PENDING.get(oid)
        if meta is None:
            return   # 不是 Momentum 策略发的单，忽略（铁律二：盲人摸象隔离）

        this_qty    = response.traded_volume
        this_price  = response.traded_price
        code        = meta["code"]
        name        = meta["name"]
        ordered_qty = meta["qty"]

        with _PENDING_LOCK:
            _PENDING[oid]["fill_qty"]    += this_qty
            _PENDING[oid]["fill_amount"] += this_price * this_qty
            total_qty    = _PENDING[oid]["fill_qty"]
            total_amount = _PENDING[oid]["fill_amount"]
            meta = dict(_PENDING[oid])   # 快照，避免持锁时 IO

        vwap = round(total_amount / total_qty, 4) if total_qty > 0 else this_price

        # ── 每次 fill 都覆写账本（VWAP + 合计量）────────────────────────
        _on_fill_write_holding(code, name, vwap, total_qty, meta)

        _log.info(
            f"📦 [分笔成交] {name}({code}) 本笔={this_qty}股@{this_price} "
            f"| 累计={total_qty}/{ordered_qty} | VWAP={vwap}"
        )

        if total_qty >= ordered_qty:
            if meta.get("direction") == "SELL":
                # 卖出全量成交：回调撤销账本（铁律一）+ 移除封条
                _pending_sells.discard(code)
                with _IO_LOCK:
                    _holdings = _load_json(HOLDINGS_JSON, {})
                    _holdings.pop(code, None)
                    _save_json(HOLDINGS_JSON, _holdings)
                with _PENDING_LOCK:
                    _PENDING[oid]["status"] = "filled"
                _log.info(f"✅ [卖出全量成交] {name}({code}) {total_qty}股 @ VWAP={vwap}，账本已撤销，封条已移除")
                send_webhook(
                    f"💰 动量向量 清仓完成",
                    f"{name}({code}) {total_qty}股 @ VWAP={vwap:.4f} 真实成交确认"
                )
            else:
                # 开仓全量成交
                _log.info(f"✅ [全量成交-开仓] {name}({code}) {total_qty}股 @ VWAP={vwap}")
                with _PENDING_LOCK:
                    _PENDING[oid]["status"] = "filled"
                send_webhook(
                    "🚀 动量向量 开仓成功",
                    f"{name}({code}) {total_qty}股 @ VWAP={vwap:.4f}\n"
                    f"槽位资金: {CAPITAL_PER_SLOT:,}元 | 移动止盈阈值: -{TRAILING_STOP_DROP:.0%}"
                )
                _write_telem({
                    "event_type": "POSITION_OPENED",
                    "code": code, "name": name,
                    "buy_price": vwap, "qty": total_qty,
                    "t0_eligible": meta.get("trade_rule") == "T+0",
                })
        else:
            with _PENDING_LOCK:
                _PENDING[oid]["status"] = "partial"

    def on_order_error(self, response):
        oid = response.order_id
        with _PENDING_LOCK:
            meta = _PENDING.pop(oid, None)
        if not meta:
            return
        err_msg = getattr(response, "error_msg", "未知")
        code    = meta.get("code", "")
        # 卖单被拒：移除封条，下一轮允许重试
        if meta.get("direction") == "SELL":
            _pending_sells.discard(code)
            _log.error(f"❌ [卖出委托被拒] {code} err={err_msg}，確条已移除，下轮重试")
        else:
            _log.error(f"❌ [委托被拒] {code} err={err_msg}")
        send_webhook("🚨 动量向量 委托被拒", f"{code} {err_msg}")


def _on_fill_write_holding(code: str, name: str, vwap: float, qty: int, meta: dict):
    """
    成交后写账本（线程安全）。
    铁律一：仅在此处写账，发单时不动账本。
    trade_rule 来自 momentum_slots.json，由 momentum_master.py 已写入。
    """
    holdings = load_holdings()
    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today_str = date.today().strftime("%Y-%m-%d")
    # 从 pending meta 读取 trade_rule（master 已写入 slots，入场前从 slot 注入）
    trade_rule = meta.get("trade_rule", "T+1")
    holdings[code] = {
        "name":       name,
        "buy_price":  vwap,
        "qty":        qty,
        "date":       today_str,
        "entry_ts":   now_str,
        "trade_rule": trade_rule,      # 从 slots 继承
    }
    save_holdings(holdings)

    # 初始化 HWM = 入场价
    hwm = load_hwm()
    if code not in hwm or hwm[code] < vwap:
        hwm[code] = vwap
        save_hwm(hwm)

# ==============================================================================
# 📥 入场协议 (Entry Protocol)
# ==============================================================================

def _can_open_entry(now_hhmm: str) -> bool:
    """判断当前时间是否在有效开火窗口内。"""
    return ENTRY_OPEN_HHMM <= now_hhmm <= ENTRY_CLOSE_HHMM


def _check_entry_signal(code: str) -> tuple[bool, str]:
    """
    检查入场条件：
    1. 最新价 > VWAP（价格在分时均线上方）
    2. 日内涨幅 > MIN_GAIN_PCT
    返回 (是否满足, 原因说明)
    """
    tick      = get_tick(code)
    last_price = float(tick.get("lastPrice", 0) or 0)
    pre_close  = float(tick.get("lastClose", 0) or tick.get("preClose", 0) or 0)
    amount     = float(tick.get("amount", 0) or 0)
    volume     = float(tick.get("volume", 0) or 0)

    if last_price <= 0 or pre_close <= 0:
        return False, f"价格无效 last={last_price} pre={pre_close}"

    # 日内涨幅
    gain_pct = (last_price / pre_close) - 1
    if gain_pct < MIN_GAIN_PCT:
        return False, f"涨幅不足 {gain_pct:.2%} < {MIN_GAIN_PCT:.0%}"

    # VWAP
    if volume > 0 and amount > 0:
        vwap = amount / (volume * 100)
        if last_price <= vwap:
            return False, f"价格在VWAP下方 {last_price:.3f} ≤ VWAP={vwap:.3f}"
    else:
        return False, "成交量为0，无法计算VWAP"

    return True, f"✅ 涨幅={gain_pct:.2%} 价格={last_price:.3f} VWAP={vwap:.3f}"


def try_open_positions(xt_trader, acc):
    """
    入场协议主函数：从 momentum_slots.json 读取 TOP-N 并逐一判断入场条件。
    铁律二：只读 momentum_holdings.json，不查 QMT 全局持仓。
    """
    now_hhmm = datetime.now().strftime("%H%M")
    if not _can_open_entry(now_hhmm):
        return   # 不在开火窗口，静默

    slots    = load_slots()
    holdings = load_holdings()

    # 检查槽位是否已满
    if len(holdings) >= SLOT_COUNT:
        return   # 静默，不刷日志（防刷屏）

    for slot in slots:
        code = slot["code"]

        # 已持仓则跳过
        if code in holdings:
            continue

        # 槽位满则停止
        if len(holdings) >= SLOT_COUNT:
            break

        # 入场条件确认
        ok, reason = _check_entry_signal(code)
        if not ok:
            _log.info(f"⏸️  [{code}] 入场条件未满足: {reason}")
            continue

        # 刷新盘口，确定买入价格（ask1 扫单）
        tick = get_tick(code)
        if not tick:
            continue
        ask_prices = tick.get("askPrice", [])
        ask1 = float(ask_prices[0]) if ask_prices else 0
        last_price = float(tick.get("lastPrice", 0) or 0)
        buy_price  = round(ask1 if ask1 > 0 else last_price, 3)

        if buy_price <= 0:
            _log.warning(f"⚠️ [{code}] 无法获取有效买入价，跳过")
            continue

        # 计算手数（向下取整到整百股）
        buy_qty = math.floor((CAPITAL_PER_SLOT / buy_price) / 100) * 100
        if buy_qty < 100:
            _log.warning(f"⚠️ [{code}] 仓位资金不足一手（{CAPITAL_PER_SLOT}元/{buy_price:.3f}），跳过")
            continue

        name = get_name(code)
        trade_rule = slot.get("trade_rule", "T+1")
        _log.info(
            f"🔫 [开仓点火] {name}({code}) "
            f"| {reason} "
            f"| {buy_qty}股 @ {buy_price}"
        )

        # 发单（铁律一：发单后只注册 pending，不更新账本）
        seq = xt_trader.order_stock(
            acc, code, xtconstant.STOCK_BUY, buy_qty,
            xtconstant.FIX_PRICE, buy_price,
            "MomVec", "MomVec_Buy"
        )

        if seq > 0:
            with _PENDING_LOCK:
                _PENDING[seq] = {
                    "code":         code,
                    "name":         name,
                    "qty":          buy_qty,
                    "price":        buy_price,
                    "trade_rule":   trade_rule,
                    "status":       "pending",
                    "fill_qty":     0,
                    "fill_amount":  0.0,
                    "sent_at":      time.time(),
                }
            _log.info(f"🚀 [{code}] 委托已发送 seq={seq}，等待成交回调...")
            _write_telem({
                "event_type":  "SIGNAL_DETECTED",
                "code": code,  "name": name,
                "buy_price":   buy_price,
                "qty":         buy_qty,
                "t0_eligible": trade_rule == "T+0",
            })
        else:
            _log.error(f"❌ [{code}] 下单失败 seq={seq}")

        time.sleep(0.3)   # 避免频繁下单

# ==============================================================================
# 🚪 退出协议 (Exit Protocol)
# ==============================================================================

def _is_t1_locked(holding_info: dict) -> bool:
    """
    判断当前持仓是否处于 T+1 锁定状态。
    - trade_rule == 'T+0'：永不锁定，随时可卖（账本由 master 写入）
    - trade_rule == 'T+1'：买入当日不可卖出
    """
    # 从账本欄位直接读取 trade_rule（master 层已写入）
    trade_rule = holding_info.get("trade_rule", "T+1")
    if trade_rule == "T+0":
        return False   # T+0 标的，直接放行

    buy_date  = holding_info.get("date", "")
    today_str = date.today().strftime("%Y-%m-%d")
    return buy_date == today_str   # 当日买入的 T+1 标的，处于锁定期


def _execute_sell(xt_trader, acc, code: str, name: str, qty: int,
                  exit_reason: str, current_price: float, buy_price: float,
                  info: dict = None):
    """
    物理清仓（铁律三：卖出量来自账本实际 qty，不重算）。
    查询实物持仓确认可卖数量（防止分红等导致的碎股问题）。
    """
    # 物理查仓（铁律三：卖出量用实物数量，非重新计算）
    try:
        pos_list = xt_trader.query_stock_positions(acc) or []
        real_pos = next((p for p in pos_list if p.stock_code == code), None)
        if real_pos and int(real_pos.volume) > 0:
            sell_qty = int(real_pos.volume)   # 物理真相
        else:
            sell_qty = qty                     # 账本兜底
    except Exception as e:
        _log.warning(f"⚠️ [{code}] 查询物理持仓失败: {e}，使用账本数量 {qty}")
        sell_qty = qty

    # 卖出价格：bid1 - 1 分钱（保证快速成交）
    tick = get_tick(code)
    bid_prices = tick.get("bidPrice", [])
    bid1       = float(bid_prices[0]) if bid_prices else 0
    sell_price = round(bid1 - 0.01, 3) if bid1 > 0 else current_price

    pnl_pct = round((current_price / buy_price - 1) * 100, 4) if buy_price > 0 else 0

    _log.info(
        f"💰 [清仓] {name}({code}) reason={exit_reason} "
        f"| 现价={current_price:.3f} 成本={buy_price:.3f} "
        f"| pnl={pnl_pct:+.2f}% | 数量={sell_qty}股"
    )

    seq = xt_trader.order_stock(
        acc, code, xtconstant.STOCK_SELL, sell_qty,
        xtconstant.FIX_PRICE, sell_price,
        "MomVec", "MomVec_Exit"
    )

    if seq > 0:
        # ✅ 正确做法：打上内存封条，防止下一届轮询重复发单
        _pending_sells.add(code)

        # ❗ 加入 _PENDING 注册表，供 on_stock_trade 回调识别卖出方向
        with _PENDING_LOCK:
            _PENDING[seq] = {
                "code":         code,
                "name":         name,
                "qty":          sell_qty,
                "price":        sell_price,
                "fill_qty":     0,
                "fill_amount":  0.0,
                "status":       "pending",
                "sent_at":      time.time(),
                "direction":    "SELL",      # 回调识别卖出方向用
                "trade_rule":   info.get("trade_rule", "T+1") if isinstance(info, dict) else "T+1",
            }

        # ⛔ 绝对禁止在这里撤销账本！账本必须等 on_stock_trade 真实成交后才撤销。
        # 不要： holdings.pop(code, None)

        # HWM 可以删除：趋势已终结，不再监控最高水位
        hwm = load_hwm()
        hwm.pop(code, None)
        save_hwm(hwm)

        send_webhook(
            f"💰 动量向量 清仓 [{exit_reason}]",
            f"{name}({code}) {sell_qty}股\n"
            f"现价={current_price:.3f} | 成本={buy_price:.3f} | pnl={pnl_pct:+.2f}%\n"
            f"seq={seq}"
        )
        _write_telem({
            "event_type":    exit_reason,
            "code": code,    "name": name,
            "buy_price":     buy_price,
            "current_price": current_price,
            "pnl_pct":       pnl_pct,
            "exit_reason":   exit_reason,
            "qty":           sell_qty,
        })
        _log.info(f"🚀 [{code}] 卖出委托已发送 seq={seq}，帖上封条，等候成交回调撤销账本")
    else:
        _log.error(f"❌ [{code}] 卖出失败 seq={seq}，将在下一轮继续重试")


def _record_t1_signal(code: str, name: str, exit_reason: str,
                       current_price: float, buy_price: float, qty: int):
    """
    T+1 标的触发退出信号但当日无法卖出时，记录信号到 momentum_t1_signals.json，
    次日 09:25 集合竞价执行。
    """
    signals = load_t1_signals()
    pnl_pct = round((current_price / buy_price - 1) * 100, 4) if buy_price > 0 else 0
    signals[code] = {
        "name":          name,
        "exit_reason":   exit_reason,
        "trigger_price": current_price,
        "buy_price":     buy_price,
        "qty":           qty,
        "pnl_pct":       pnl_pct,
        "signal_ts":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "execute_date":  (date.today() + timedelta(days=1)).strftime("%Y-%m-%d"),
    }
    save_t1_signals(signals)
    _log.info(
        f"📌 [T+1 信号记录] {name}({code}) reason={exit_reason} "
        f"pnl={pnl_pct:+.2f}% → 次日 09:25 执行"
    )
    send_webhook(
        f"📌 动量向量 T+1信号 [{exit_reason}]",
        f"{name}({code}) 触发退出信号，因T+1制度当日无法卖出\n"
        f"触发价={current_price:.3f} | pnl={pnl_pct:+.2f}% | 次日09:25执行"
    )
    _write_telem({
        "event_type":    "T1_SIGNAL",
        "code": code,    "name": name,
        "buy_price":     buy_price,
        "current_price": current_price,
        "pnl_pct":       pnl_pct,
        "exit_reason":   exit_reason,
        "qty":           qty,
    })


def execute_t1_pending_signals(xt_trader, acc):
    """
    次日 09:25 集合竞价：执行昨日记录的 T+1 卖出信号。
    在主循环中，每分钟检测到 09:25 时调用一次。
    """
    signals = load_t1_signals()
    if not signals:
        return

    today_str = date.today().strftime("%Y-%m-%d")
    to_execute = {
        code: info for code, info in signals.items()
        if info.get("execute_date", "") <= today_str
    }

    if not to_execute:
        return

    _log.info(f"⏰ [T+1 信号执行] 发现 {len(to_execute)} 个待执行卖出信号")
    for code, info in to_execute.items():
        name      = info.get("name", code)
        qty       = info.get("qty", 0)
        buy_price = info.get("buy_price", 0)
        reason    = info.get("exit_reason", "T1_DEFERRED")
        trig_price= info.get("trigger_price", 0)

        _log.info(f"  🔔 [{code}] {name} T+1 执行: qty={qty} reason={reason}")

        # 物理查仓确认可卖量
        try:
            pos_list = xt_trader.query_stock_positions(acc) or []
            real_pos = next((p for p in pos_list if p.stock_code == code), None)
            if real_pos is None or real_pos.volume <= 0:
                _log.warning(f"  ⚠️ [{code}] 物理持仓为0，信号已过期，清除")
                signals.pop(code, None)
                continue
            sell_qty = int(real_pos.volume)
        except Exception as e:
            _log.warning(f"  ⚠️ [{code}] 物理查仓失败: {e}，使用账本数量 {qty}")
            sell_qty = qty

        # 集合竞价使用市价（xtconstant.MARKET_PEER_PRICE 最优五档即时成交）
        seq = xt_trader.order_stock(
            acc, code, xtconstant.STOCK_SELL, sell_qty,
            xtconstant.MARKET_PEER_PRICE, 0,
            "MomVec", "MomVec_T1Exit"
        )

        if seq > 0:
            _log.info(f"  ✅ [{code}] T+1 卖出委托已发送 seq={seq}")
            # 清理账本
            holdings = load_holdings()
            holdings.pop(code, None)
            save_holdings(holdings)
            hwm = load_hwm()
            hwm.pop(code, None)
            save_hwm(hwm)
            signals.pop(code, None)
            send_webhook(
                "⏰ 动量向量 T+1隔夜平仓",
                f"{name}({code}) {sell_qty}股 | 昨日原因={reason}\n"
                f"昨日触发价={trig_price:.3f} | seq={seq}"
            )
            _write_telem({
                "event_type":    "T1_EXECUTED",
                "code": code,    "name": name,
                "buy_price":     buy_price,
                "current_price": trig_price,
                "exit_reason":   reason,
                "qty":           sell_qty,
            })
        else:
            _log.error(f"  ❌ [{code}] T+1 卖出失败 seq={seq}")

    save_t1_signals(signals)


def monitor_and_exit(xt_trader, acc):
    """
    退出协议主函数（每 1 分钟调用）：
    1. 更新所有持仓的 HWM（High Water Mark）
    2. 检查移动止盈（HWM 回撤 4%）
    3. 检查硬止损（亏损 5%）
    4. T+1 标的：触发信号但当日无法卖出，记录到 T1_SIGNALS
    """
    holdings = load_holdings()
    if not holdings:
        return

    hwm      = load_hwm()

    for code, info in list(holdings.items()):
        current_price = get_current_price(code)
        if current_price <= 0:
            _log.warning(f"⚠️ [{code}] 无法获取现价，跳过本轮检查")
            continue

        buy_price = float(info.get("buy_price", 0))
        qty       = int(info.get("qty", 0))
        name      = info.get("name", code)
        trade_rule = info.get("trade_rule", "T+1")

        # ── 更新 HWM ─────────────────────────────────────────────────
        current_hwm = float(hwm.get(code, buy_price))
        if current_price > current_hwm:
            hwm[code] = current_price
            current_hwm = current_price
            save_hwm(hwm)     # 实时落盘，防进程崩溃丢失水位

        pnl_pct = round((current_price / buy_price - 1) * 100, 4) if buy_price > 0 else 0

        # ── HOLDING_LOG（每轮都记录，供复盘分析）─────────────────────
        _write_telem({
            "event_type":    "HOLDING_LOG",
            "code": code,    "name": name,
            "buy_price":     round(buy_price, 4),
            "current_price": round(current_price, 4),
            "hwm":           round(current_hwm, 4),
            "pnl_pct":       pnl_pct,
            "qty":           qty,
            "t0_eligible":   trade_rule == "T+0",
        })
        _log.info(
            f"  📊 [{code}] {name} | "
            f"现价={current_price:.3f} HWM={current_hwm:.3f} "
            f"成本={buy_price:.3f} pnl={pnl_pct:+.2f}% "
            f"T+0={trade_rule == 'T+0'}"
        )

        # ── 退出信号判断 ─────────────────────────────────────────────
        triggered   = False
        exit_reason = ""

        # 1. 移动止盈：从最高水位回撤 TRAILING_STOP_DROP
        trailing_line = current_hwm * (1 - TRAILING_STOP_DROP)
        if current_price < trailing_line:
            triggered   = True
            exit_reason = "TRAILING_STOP"
            _log.info(
                f"  🎯 [{code}] 移动止盈触发 "
                f"现价={current_price:.3f} < HWM*{1-TRAILING_STOP_DROP:.2f}={trailing_line:.3f}"
            )

        # 2. 硬止损：亏损超过 HARD_STOP_LOSS
        if not triggered and buy_price > 0:
            stop_line = buy_price * (1 - HARD_STOP_LOSS)
            if current_price <= stop_line:
                triggered   = True
                exit_reason = "HARD_STOP"
                _log.info(
                    f"  🔴 [{code}] 硬止损触发 "
                    f"现价={current_price:.3f} ≤ 成本*{1-HARD_STOP_LOSS:.2f}={stop_line:.3f}"
                )

        # ── 执行退出 ──────────────────────────────────────────────────
        # -- 执行退出 -------------------------------------------------
        if not triggered:
            continue

        # 封条拦截：卖单已在路上，等候成交回调，绝不重复发单
        if code in _pending_sells:
            _log.info(f"  ⏳ [{code}] {name} 卖单已在途（封条已投），等候成交回调撤销账本")
            continue

        if _is_t1_locked(info):
            # T+1 锁定：记录信号，次日执行
            _record_t1_signal(code, name, exit_reason, current_price, buy_price, qty)
        else:
            # T+0 可交易 或 T+1已过锁定期
            _execute_sell(xt_trader, acc, code, name, qty, exit_reason, current_price, buy_price, info)

# ==============================================================================
# 🔬 Pending 超时巡检（防止网络闪断导致成交回报丢失）
# ==============================================================================

PENDING_TIMEOUT_SEC = 60   # 委托超时阈值（秒）


def sweep_stale_pending(xt_trader, acc):
    """
    每 30 秒扫描一次 _PENDING，超时委托物理查询成交情况。
    防止网络闪断导致成交回报丢失（铁律一补丁）。
    """
    now = time.time()
    with _PENDING_LOCK:
        stale = {
            seq: m for seq, m in _PENDING.items()
            if m.get("status") in ("pending", "partial")
            and now - m.get("sent_at", now) > PENDING_TIMEOUT_SEC
        }

    for seq, meta in stale.items():
        code = meta["code"]
        name = meta["name"]
        try:
            trades   = xt_trader.query_stock_trades(acc) or []
            filled   = sum(t.traded_volume for t in trades if getattr(t, "order_id", None) == seq)
            amount   = sum(t.traded_price * t.traded_volume for t in trades
                          if getattr(t, "order_id", None) == seq and t.traded_volume > 0)
            ordered  = meta["qty"]
            remaining = ordered - filled

            if filled > 0:
                vwap = round(amount / filled, 4)
                _on_fill_write_holding(code, name, vwap, filled, meta)
                _log.info(
                    f"🧹 [Sweeper] {name}({code}) 补录 {filled}股 @ VWAP={vwap}，"
                    f"剩余未成 {remaining}股"
                )

            if remaining > 0:
                cancel_res = xt_trader.cancel_order_stock(acc, seq)
                _log.info(f"🧹 [Sweeper] {code} 撤单 {remaining}股 res={cancel_res}")
                send_webhook("🧹 动量向量 超时撤单", f"{name}({code}) {remaining}股超时未成，已撤单")

            with _PENDING_LOCK:
                _PENDING[seq]["status"] = "swept"

        except Exception as e:
            _log.warning(f"⚠️ [Sweeper] {code} 处理失败: {e}")

# ==============================================================================
# 🔁 主循环
# ==============================================================================

def run_momentum_executor():
    """
    主执行循环（1分钟轮询）。
    """
    _log.info("=" * 60)
    _log.info(f"🚀 动量向量执行器 启动 | {datetime.now():%Y-%m-%d %H:%M:%S}")
    _log.info(f"   槽位数: {SLOT_COUNT} | 每槽资金: {CAPITAL_PER_SLOT:,}元")
    _log.info(f"   开火窗口: {ENTRY_OPEN_HHMM}-{ENTRY_CLOSE_HHMM}")
    _log.info(f"   移动止盈回撤: {TRAILING_STOP_DROP:.0%} | 硬止损: {HARD_STOP_LOSS:.0%}")
    _log.info("=" * 60)

    # ── 建立 QMT 连接 ─────────────────────────────────────────────
    acc_id   = os.getenv("ACCOUNT_ID")
    qmt_path = os.getenv("QMT_PATH")
    if not acc_id or not qmt_path:
        _log.error("❌ 环境变量缺失 ACCOUNT_ID 或 QMT_PATH，无法启动")
        return

    session_id = random.randint(100000, 999999)
    xt_trader  = XtQuantTrader(qmt_path, session_id)
    xt_trader.start()
    time.sleep(5)   # xtquant 规范：start() 后必须等待 ≥3秒再 connect()

    res = xt_trader.connect()
    if res != 0:
        _log.error(f"❌ QMT 链路连接失败 res={res}，请确认 MiniQMT 已启动")
        return

    acc = StockAccount(acc_id)
    xt_trader.register_callback(MomentumCallback())
    xt_trader.subscribe(acc)
    _log.info(f"✅ QMT 连接成功 | 账号: {acc_id}")

    # ── 订阅当前持仓标的的 Tick ────────────────────────────────────
    holdings = load_holdings()
    for code in holdings:
        try:
            xtdata.subscribe_quote(code, period="tick", count=-1)
            _log.info(f"  📡 订阅 Tick: {code}")
        except Exception as e:
            _log.warning(f"  ⚠️ 订阅失败 {code}: {e}")

    _last_sweep_ts   = 0
    _t1_executed_today = False   # 防止 T+1 信号同一天重复执行

    try:
        while True:
            now        = datetime.now()
            now_hhmm   = now.strftime("%H%M")
            today_str  = date.today().strftime("%Y-%m-%d")

            # ── 交易时段检查 ──────────────────────────────────────
            if now_hhmm < "0920" or now_hhmm > "1500":
                _log.info(f"🌙 [{now_hhmm}] 非交易时段，等待下一分钟...")
                time.sleep(60)
                continue

            # ── ① T+1 次日信号执行（09:25 集合竞价）─────────────
            if now_hhmm == T1_AUCTION_HHMM and not _t1_executed_today:
                execute_t1_pending_signals(xt_trader, acc)
                _t1_executed_today = True
            elif now_hhmm > T1_AUCTION_HHMM:
                # 当天已过 09:25（允许每天重置，次日再执行）
                pass

            # 新的一天重置 T+1 执行标志
            if now_hhmm == "0920":
                _t1_executed_today = False

            # ── ② 入场协议（09:30-10:30 开火窗口）───────────────
            if now_hhmm <= NO_NEW_ENTRY_HHMM:
                try_open_positions(xt_trader, acc)

            # ── ③ 退出协议监控（全交易时段）─────────────────────
            monitor_and_exit(xt_trader, acc)

            # ── ④ Pending 超时巡检（每 30 秒）────────────────────
            if time.time() - _last_sweep_ts >= 30:
                sweep_stale_pending(xt_trader, acc)
                _last_sweep_ts = time.time()

            # ── ⑤ 新增标的 Tick 订阅（持有标的可能因入场而增加）──
            holdings = load_holdings()
            for code in holdings:
                try:
                    xtdata.subscribe_quote(code, period="tick", count=-1)
                except Exception:
                    pass

            time.sleep(60)   # 1 分钟轮询

    except KeyboardInterrupt:
        _log.info("⛔ 收到中断信号，执行器正在关闭...")
    except Exception as e:
        _log.error(f"🔥 主循环未知异常: {e}", exc_info=True)
    finally:
        try:
            xt_trader.stop()
        except Exception:
            pass
        _log.info("🏁 动量向量执行器 已关闭")


# ==============================================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="动量向量执行器")
    parser.add_argument(
        "--t1-only", action="store_true",
        help="仅执行 T+1 集合竞价挂单信号，完成后立即退出（由 autopilot 09:20 阻塞调用）"
    )
    args = parser.parse_args()

    if args.t1_only:
        # ── T+1 独立模式：建连 → 执行待发信号 → 退出 ──────────────────────
        _log.info("=" * 60)
        _log.info(f"⏰ 动量向量执行器 T+1 独立模式 | {datetime.now():%Y-%m-%d %H:%M:%S}")
        _log.info("   模式：仅执行昨日 T+1 集合竞价信号，不启动主循环")
        _log.info("=" * 60)

        _acc_id   = os.getenv("ACCOUNT_ID")
        _qmt_path = os.getenv("QMT_PATH")
        if not _acc_id or not _qmt_path:
            _log.error("❌ 环境变量缺失 ACCOUNT_ID 或 QMT_PATH，T+1 独立模式无法启动")
            sys.exit(1)

        _session_id = random.randint(100000, 999999)
        _xt_trader  = XtQuantTrader(_qmt_path, _session_id)
        _xt_trader.start()
        time.sleep(5)

        _res = _xt_trader.connect()
        if _res != 0:
            _log.error(f"❌ QMT 链路连接失败 res={_res}，T+1 独立模式中止")
            sys.exit(1)

        _acc = StockAccount(_acc_id)
        _xt_trader.register_callback(MomentumCallback())
        _xt_trader.subscribe(_acc)
        _log.info(f"✅ QMT 连接成功 | 账号: {_acc_id}")

        _signals = load_t1_signals()
        if _signals:
            _log.info(f"📋 发现 {len(_signals)} 个待执行 T+1 信号，开始执行...")
            execute_t1_pending_signals(_xt_trader, _acc)
            time.sleep(3)   # 给委托推送一点缓冲
        else:
            _log.info("ℹ️ 无待执行的 T+1 信号，正常退出")

        try:
            _xt_trader.stop()
        except Exception:
            pass
        _log.info("🏁 T+1 独立模式执行完毕，进程退出")
        sys.exit(0)

    else:
        run_momentum_executor()
