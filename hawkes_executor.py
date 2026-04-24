# -*- coding: utf-8 -*-
# ==============================================================================
# 🦅 hawkes_executor.py  V3 Titan
# 📌 [策略名称] : Hawkes 刺客系统 V3 Titan — 实盘执行器
# 📦 [层级]     : Tick 桥接 + 三道防御锁 + 三位一体开火引擎 + 极速退壳协议
# 🔗 [上游]     : xtquant.xtdata 全量实时 Tick 推送
# 🔗 [下游]     : FastHawkesEngine (O(1) 脉冲解算器) → XtQuantTrader
#
# ┌─────────────────────────────────────────────────────────────────────────┐
# │  模块一：全局状态机 + 绝对防御装甲（三道物理锁）                        │
# │    锁A _pending_locks  —— 悲观在途锁（O(1) set）                       │
# │    锁B _last_fire_time —— 60 秒物理冷却器                              │
# │    锁C _current_exposure —— 20k 敞口封顶 → 触发 Micro-Exit             │
# │                                                                        │
# │  模块二：三位一体开火引擎 (The Trigger)                                 │
# │    嗅探1 Hawkes λ ≥ 25.0（O(1) 指数衰减）                              │
# │    嗅探2 OBI > 0.8（5档加权盘口失衡，O(1)）                            │
# │    嗅探3 check_toxicity()（VPIN 预留接口，当前 True）                   │
# │                                                                        │
# │  模块三：极速退壳协议 (Micro-Exit)                                      │
# │    止盈  +15 元 / 止损  -30 元 / 时间斩仓 45s / 锁C敞口强平            │
# │    物理清仓（query_stock_positions），Fill-Based 回调驱动敞口           │
# └─────────────────────────────────────────────────────────────────────────┘
#
# ⚠️  四大铁律合规：
#     ✓ 铁律一：Fill-Based 记账，on_order_trade 回调后才写账本
#     ✓ 铁律二：盲人摸象隔离，持仓决策只读 hawkes_holdings.json
#     ✓ 铁律三：退出时物理查仓 query_stock_positions，全额卖出
#     ✓ 铁律四：_pending_locks_mu / _hawk_pending_mu / _exposure_mu /
#               _holdings_mu 四锁全保护，零裸写共享状态
# ==============================================================================

import sys
import os
import time
import datetime
import threading
import json
import csv
import logging
import math

# ──────────────────────────────────────────────────────────────
# N8N Webhook（策略三件套之一，失败静默）
# ──────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

# ──────────────────────────────────────────────────────────────
# 🛡️  Windows 控制台 UTF-8 补丁
# ──────────────────────────────────────────────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ──────────────────────────────────────────────────────────────
# xtquant 导入（xtconstant 必须与 xtdata 同行，safe-patterns §1）
# ──────────────────────────────────────────────────────────────
from xtquant import xtdata, xtconstant
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

from hawkes_engine import FastHawkesEngine   # O(1) λ 指数衰减解算器


# ==============================================================================
# 📌  CONFIG（所有可调参数集中于此，禁止散落代码正文）
# ==============================================================================

# ── QMT 连接 ─────────────────────────────────────────────────
QMT_PATH   = os.getenv("QMT_PATH", r"C:\国金证券QMT交易端\userdata_mini")  # 从 .env 读取
ACCOUNT_ID = os.getenv("ACCOUNT_ID", "")                                     # 从 .env 读取，禁止硬编码
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")

# ── 标的白名单（严格锁定 T+0 跨境/商品 ETF）────────────────
HAWK_CODES = [
    "513120.SH",   # 港股创新医药ETF
    "518680.SH",   # 黄金ETF
    "513050.SH",   # 中概互联网ETF
    "513300.SH",   # 纳斯达克ETF
    "159892.SZ",   # 恒生医药ETF
    "513880.SH",   # 日经225ETF
    "513090.SH",   # 香港证券ETF
]
# ── 项目根目录（__file__ 绝对化，防 autopilot 以相对路径启动时 cwd 漂移）
_PROJECT_ROOT  = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR       = os.path.join(_PROJECT_ROOT, "logs")
T0_POOL_CSV    = os.path.join(_PROJECT_ROOT, ".state", "t0_absolute_pool.csv")
STATE_DIR      = os.path.join(_PROJECT_ROOT, ".state")
HOLDINGS_FILE  = os.path.join(STATE_DIR, "hawkes_holdings.json")

# ── Hawkes 引擎参数 ───────────────────────────────────────────
ENGINE_PARAMS = {
    "mu":               1.0,
    "alpha":            1.5,    # 激震系数（平方根量纲）
    "beta":             1.2,    # 快速衰减，符合 ETF 余震周期
    "volume_threshold": 500,    # 散户噪音过滤（手）
    "trigger_level":    25.0,   # 传递给引擎的内部阈值（与下方 LAMBDA_THRESHOLD 对齐）
}

# ── 三位一体开火参数 ──────────────────────────────────────────
FIRE_PAUSED       = False       # 总开关：False = 实盘开火 | True = 仅沙盘模拟
FIRE_CAPITAL      = 10_000.0   # 单次开火资金（元）
FIRE_COOLDOWN_SEC = 60          # 锁B：同标的冷却期（秒）
EXPOSURE_CAP      = 20_000.0   # 锁C：单标的敞口封顶（元）
LAMBDA_THRESHOLD  = 25.0        # 嗅探1：λ 开火线
OBI_THRESHOLD     = 0.8         # 嗅探2：OBI 买盘压制阈值

# ── Micro-Exit 退出参数 ───────────────────────────────────────
TP_PROFIT_ABS  = 15.0           # 止盈：浮盈 ≥ +15 元
SL_LOSS_ABS    = -30.0          # 止损：浮亏 ≤ -30 元
TIME_STOP_SEC  = 45             # 时间斩仓：持仓 > 45 秒无论盈亏强平

# ── 交易时间门控（集合竞价阶段禁止开火）────────────────────────
# 09:22 正常启动订阅/热身，Tick 照常进引擎保持 λ 实时更新
# 09:30:00 之前：一律不开火、不执行 Micro-Exit（集合竞价价格失真）
# 14:57:00 之后：禁止新开仓（临近收盘，避免当日无法平仓）
MARKET_OPEN_TIME   = datetime.time(9, 30, 0)   # 连续竞价开始（09:30）
MARKET_CLOSE_TIME  = datetime.time(14, 57, 0)  # 新仓截止（收盘前3分钟）

# ── 目录准备（使用绝对路径，防 cwd 漂移导致 PermissionError）─────
os.makedirs(LOGS_DIR,  exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)


# ==============================================================================
# 📋  LOGGER（独立 logger，propagate=False，防 autopilot root logger 吃掉）
# ==============================================================================
_LOG_FILE = os.path.join(LOGS_DIR, f"{datetime.date.today():%Y%m%d}_hawkes_live.log")
_logger = logging.getLogger("hawk_v3")
_logger.setLevel(logging.DEBUG)
if not _logger.handlers:
    _fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S")
    _fh  = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    _fh.setFormatter(_fmt)
    _sh  = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    _logger.addHandler(_fh)
    _logger.addHandler(_sh)
_logger.propagate = False   # ⚠️  不传播到 root logger

_R = "\033[91m"; _G = "\033[92m"; _Y = "\033[93m"; _B = "\033[1m"; _E = "\033[0m"


# ==============================================================================
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 🏗️  MODULE 1 — 全局状态机 & 绝对防御装甲
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ==============================================================================

# ── 引擎实例池（每只标的独立 O(1) λ 解算器）───────────────────
engines: dict = {
    code: FastHawkesEngine(**ENGINE_PARAMS) for code in HAWK_CODES
}

# ── 增量成交量追踪（差分，O(1)，无未来函数）──────────────────
_last_volume: dict = {code: 0 for code in HAWK_CODES}

# ──────────────────────────────────────────────────────────────
# ▶ 三道物理锁（禁止用外部 API 轮询替代任何一道）
# ──────────────────────────────────────────────────────────────

# 锁A：悲观在途锁 —— 发单即封印，Fill 回调才解除
_pending_locks: set          = set()
_pending_locks_mu            = threading.Lock()
# 锁A超时守卫：记录每个 code 进入 _pending_locks 的 Unix 时间戳
# 若 Fill 回调 180s 内未到达（网络/QMT 丢通知），监控线程自动强制解锁
_PENDING_LOCK_TIMEOUT_SEC    = 180
_pending_lock_timestamps: dict = {}   # {code: float unix}

# 锁B：物理冷却器 —— 记录每标的上次开火 Unix 时间戳
_last_fire_time: dict  = {}                          # {code: float}，tickcb 单线程写，无需锁

# 锁C：敞口锁 —— Fill-Based 精确驱动（铁律一），零初始值
_current_exposure: dict = {code: 0.0 for code in HAWK_CODES}
_exposure_mu            = threading.Lock()

# ── Fill-Based 待确认订单注册表（铁律一核心）──────────────────
_hawk_pending: dict    = {}                          # {seq: {...}}
_hawk_pending_mu       = threading.Lock()

# ── 账本 I/O 锁（铁律四）─────────────────────────────────────
_holdings_mu = threading.Lock()

# ── QMT 单例 ─────────────────────────────────────────────────
_xt_trader = None
_acc       = None

# ──────────────────────────────────────────────────────────────
# 📝  Paper Trading 沙盘（FIRE_PAUSED=True 时启用）
#     不动真实账户，用真实 Tick 价格模拟持仓 + 逐笔收益明细落盘。
#     生命周期与实盘 Micro-Exit 完全对称：
#       _simulate_paper_entry → _paper_holdings["holding"]
#       _paper_exit_monitor   → TP/SL/TimeStop → Paper Ledger CSV → 清除
# ──────────────────────────────────────────────────────────────
_paper_holdings: dict = {}          # {code: {qty, entry_price, entry_unix, lam, obi, ask1}}
_paper_holdings_mu    = threading.Lock()

# Paper Ledger CSV（每笔沙盘盈亏明细，供收盘后复盘）
_PAPER_LEDGER_FILE   = os.path.join(LOGS_DIR, f"{datetime.date.today():%Y%m%d}_hawkes_paper_ledger.csv")
_PAPER_LEDGER_LOCK   = threading.Lock()
_PAPER_LEDGER_HEADER = [
    "trade_id",       # 唯一编号：YYYYMMDD_HHMMSS_code
    "code",
    "entry_time",     # HH:MM:SS.mmm
    "entry_price",    # 模拟买入价（ask1 快照）
    "qty",            # 模拟手数（同实盘计算逻辑）
    "capital",        # 模拟资金（元）= entry_price × qty
    "lam",            # 触发时 Hawkes λ
    "obi",            # 触发时 OBI
    "exit_time",      # 退出时间（HH:MM:SS.mmm）
    "exit_price",     # 模拟卖出价（bid1 快照）
    "hold_sec",       # 持仓秒数
    "exit_reason",    # tp / sl / time_stop
    "gross_pnl",      # 毛盈亏（元）= (exit_price - entry_price) × qty
    "est_commission", # 预估双边手续费估算（万1 × 2）
    "net_pnl",        # 净盈亏估算（元）= gross_pnl - est_commission
]
# 注：沙盘冷却器复用实盘 _last_fire_time，保证信号密度与实盘完全一致


# ==============================================================================
# 📂  账本 I/O（带锁 + 原子替换，铁律四）
# ==============================================================================

def _load_holdings_unlocked() -> dict:
    """读取 hawkes_holdings.json（不加锁，仅供已持有 _holdings_mu 的调用方使用）。"""
    if not os.path.exists(HOLDINGS_FILE):
        return {}
    try:
        with open(HOLDINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_holdings_unlocked(h: dict):
    """写入 hawkes_holdings.json（不加锁，原子 tmp 替换，仅供已持有 _holdings_mu 的调用方）。"""
    tmp = HOLDINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HOLDINGS_FILE)


def _load_holdings() -> dict:
    """读取 hawkes_holdings.json（加 _holdings_mu 锁，供外部调用）。"""
    with _holdings_mu:
        return _load_holdings_unlocked()


def _save_holdings(h: dict):
    """写入 hawkes_holdings.json（加 _holdings_mu 锁，原子 tmp 替换，供外部调用）。"""
    with _holdings_mu:
        _save_holdings_unlocked(h)



# ==============================================================================
# 📡  遥测 CSV（三件套之二）
# ==============================================================================

_TELEM_FILE   = os.path.join(LOGS_DIR, f"{datetime.date.today():%Y%m%d}_hawkes_telemetry.csv")
_TELEM_LOCK   = threading.Lock()
_TELEM_HEADER = [
    "event_type",       # SIGNAL_DETECTED / POSITION_OPENED / HOLDING_LOG / POSITION_CLOSED
    "timestamp",
    "code",
    "last_price",
    "lam",              # Hawkes λ
    "obi",              # OBI 值
    "delta_vol",        # 单笔增量成交量（手）
    "ask1", "bid1",
    "buy_flag",         # 1=外盘 -1=内盘 0=中性
    "entry_price",
    "qty",
    "unrealized_pnl",
    "exit_reason",      # tp / sl / time_stop / exposure_cap / orphan_fill
]


def _write_telemetry(row: dict):
    """线程安全追加遥测行，首次写入自动输出 Header。"""
    with _TELEM_LOCK:
        is_new = not os.path.exists(_TELEM_FILE)
        with open(_TELEM_FILE, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_TELEM_HEADER, extrasaction="ignore")
            if is_new:
                w.writeheader()
            row.setdefault("timestamp", datetime.datetime.now().strftime("%H:%M:%S.%f")[:12])
            w.writerow(row)


# ==============================================================================
# 📣  N8N Webhook（三件套之三）
# ==============================================================================

def send_webhook(title: str, message: str) -> None:
    """N8N 推送，失败静默（timeout=5s，不阻断交易逻辑）。"""
    if not _HAS_REQUESTS or not N8N_WEBHOOK_URL:
        return
    try:
        _requests.post(
            N8N_WEBHOOK_URL,
            json={"title": title, "message": message,
                  "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            timeout=5,
        )
    except Exception:
        pass


# ==============================================================================
# 🛡️  on_tick — 绝对防御装甲（三锁顺序校验）
# ==============================================================================

def _make_tick_callback(code: str):
    """
    工厂函数。默认参数 _code=code 提前绑定，
    规避 for 循环晚绑定陷阱（safe-patterns §9）。
    """
    engine = engines[code]

    def on_tick(data: dict, _code: str = code):
        # ── STEP 0：解包 Tick
        raw = data.get(_code)
        if not raw:
            return
        tick = raw[-1] if isinstance(raw, list) else raw
        if not isinstance(tick, dict):
            return

        last_price: float = tick.get("lastPrice", 0.0)
        if last_price <= 0.0:
            return

        ts_ms: int          = tick.get("time", 0)
        current_time: float = ts_ms / 1000.0 if ts_ms > 0 else time.time()

        # ── STEP 1：增量成交量（O(1) 差分，无未来函数）
        cum_vol: int   = int(tick.get("volume", 0))
        delta_vol: int = cum_vol - _last_volume[_code]
        _last_volume[_code] = cum_vol          # 先刷新，无论后续是否 return
        if delta_vol <= 0:
            return

        # ── STEP 2：提取 5 档盘口
        ask_prices = tick.get("askPrice") or []
        bid_prices = tick.get("bidPrice") or []
        ask_vols   = tick.get("askVol")   or []
        bid_vols   = tick.get("bidVol")   or []

        ask1 = float(ask_prices[0]) if ask_prices and ask_prices[0] > 0 else 0.0
        bid1 = float(bid_prices[0]) if bid_prices and bid_prices[0] > 0 else 0.0

        # ── STEP 3：三道防御锁（顺序不可颠倒）

        # —— 锁A：悲观在途锁（O(1) set lookup）
        with _pending_locks_mu:
            if _code in _pending_locks:
                _logger.debug(f"[锁A·丢票] {_code} 在途中，Tick 物理致盲")
                return

        # —— 锁B：60 秒物理冷却器
        elapsed = current_time - _last_fire_time.get(_code, 0.0)
        if elapsed < FIRE_COOLDOWN_SEC:
            _logger.debug(
                f"[锁B·丢票] {_code} 冷却中 剩余≈{FIRE_COOLDOWN_SEC - elapsed:.1f}s"
            )
            return

        # —— 锁C：敞口封顶 → 触发 Micro-Exit + 丢弃本 Tick
        with _exposure_mu:
            exposure = _current_exposure.get(_code, 0.0)
        if exposure >= EXPOSURE_CAP:
            _logger.info(
                f"{_Y}[锁C·敞口满] {_code} exposure={exposure:.0f}元"
                f" ≥ {EXPOSURE_CAP:.0f}元 → 触发 Micro-Exit{_E}"
            )
            _trigger_micro_exit(_code, last_price, reason="exposure_cap")
            return

        # ── STEP 4：三锁放行 → 三位一体开火引擎（模块二）
        _fire_engine_check(
            _code, engine, last_price, current_time,
            delta_vol, ask1, bid1,
            ask_prices, bid_prices, ask_vols, bid_vols,
        )

    return on_tick


# ==============================================================================
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 🎯  MODULE 2 — 三位一体开火引擎 (The Trigger)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ==============================================================================


def _calc_obi(
    ask_prices: list, bid_prices: list,
    ask_vols:   list, bid_vols:   list,
    depth: int = 5,
) -> float:
    """
    加权 5 档 OBI（Order Book Imbalance）。
    市值加权（量 × 价）+ 档位衰减（w = 1/(i+1)），O(5) ≈ O(1)。
    返回 ∈ [-1.0, 1.0]：>0.8 = 买盘绝对压制（开火阈值）。
    无未来函数：仅读当前 Tick 盘口快照。
    """
    weighted_bid = 0.0
    weighted_ask = 0.0

    for i in range(depth):
        w  = 1.0 / (i + 1)
        bv = float(bid_vols[i])   if (bid_vols   and i < len(bid_vols)   and bid_vols[i]   > 0) else 0.0
        av = float(ask_vols[i])   if (ask_vols   and i < len(ask_vols)   and ask_vols[i]   > 0) else 0.0
        bp = float(bid_prices[i]) if (bid_prices and i < len(bid_prices) and bid_prices[i] > 0) else 0.0
        ap = float(ask_prices[i]) if (ask_prices and i < len(ask_prices) and ask_prices[i] > 0) else 0.0
        weighted_bid += w * bv * bp
        weighted_ask += w * av * ap

    total = weighted_bid + weighted_ask
    if total < 1e-9:
        return 0.0   # 盘口干涸，返回中性
    return (weighted_bid - weighted_ask) / total


def check_toxicity(code: str, delta_vol: int, buy_flag: int) -> bool:
    """
    毒性过滤接口（VPIN / Entropy 预留）。
    True  = 通过（无毒，允许开火）
    False = 拦截（摩擦力过高）
    V3 Titan 占位实现：全部 True，后续接入真实 VPIN 时只需修改此函数。
    """
    # TODO: 接入真实 VPIN 计算（滑动窗口 buy_vol/sell_vol，O(1) deque）
    return True


def _fire_engine_check(
    code:         str,
    engine:       object,   # FastHawkesEngine
    last_price:   float,
    current_time: float,
    delta_vol:    int,
    ask1:         float,
    bid1:         float,
    ask_prices:   list,
    bid_prices:   list,
    ask_vols:     list,
    bid_vols:     list,
):
    """
    三位一体嗅探器（由 on_tick 在三锁放行后调用）。
    任何一关不过 → 冷血日志 + return，不触碰任何状态。
    全部通过 → 原子写 _pending_locks → 发委托。
    """
    # ── 主动方向判断
    if   ask1 > 0 and last_price >= ask1: buy_flag =  1
    elif bid1 > 0 and last_price <= bid1: buy_flag = -1
    else:                                 buy_flag =  0

    # ──────────────────────────────────────────────────────────
    # 嗅探一：Hawkes λ ≥ 25.0（O(1) 指数衰减，无回溯历史）
    # ──────────────────────────────────────────────────────────
    result  = engine.process_tick(
        timestamp_sec=current_time,
        price=last_price,
        volume=delta_vol,
        buy_flag=buy_flag,
    )
    lam: float = result.get("lambda", 0.0)

    # ── 时间门控（λ 引擎已热身，现在检查是否允许开火）───────────
    _now_t = datetime.datetime.now().time()
    if _now_t < MARKET_OPEN_TIME:
        return   # 09:30 前集合竞价：λ 已更新，静默不开火
    if _now_t >= MARKET_CLOSE_TIME:
        return   # 14:57 后：不开新仓，静默退出

    if lam < LAMBDA_THRESHOLD:
        return   # 高频静默路径，不打 INFO，防 I/O 拥堵

    dir_str = "外盘" if buy_flag == 1 else "内盘" if buy_flag == -1 else "中性"
    _logger.info(
        f"{_B}{_R}[嗅探1·PASS] {code} | λ={lam:.3f} ≥ {LAMBDA_THRESHOLD}"
        f" | 价={last_price:.4f} | 单笔={delta_vol}手 | 方向={dir_str}{_E}"
    )

    # ──────────────────────────────────────────────────────────
    # 嗅探二：OBI > 0.8（5 档加权盘口失衡，O(1)）
    # ──────────────────────────────────────────────────────────
    obi: float = _calc_obi(ask_prices, bid_prices, ask_vols, bid_vols)

    if obi <= OBI_THRESHOLD:
        _logger.info(
            f"[嗅探2·OBI_REJECT] {code} | OBI={obi:.3f} ≤ {OBI_THRESHOLD}"
            f"（主震确认，但盘口不支持，放弃）| λ={lam:.3f}"
        )
        return

    _logger.info(
        f"{_B}{_G}[嗅探2·PASS] {code} | OBI={obi:.3f} > {OBI_THRESHOLD}"
        f"（买盘绝对压制）{_E}"
    )

    # ──────────────────────────────────────────────────────────
    # 嗅探三：毒性过滤（VPIN 预留接口）
    # ──────────────────────────────────────────────────────────
    if not check_toxicity(code, delta_vol, buy_flag):
        _logger.info(
            f"[嗅探3·TOXIC] {code} | VPIN 毒性过高，拒绝开火"
            f" | λ={lam:.3f} OBI={obi:.3f}"
        )
        return

    _logger.info(f"[嗅探3·PASS] {code} | 毒性检测通过（预留接口）")

    # ── 最终二次校验：防账本幽灵（三锁放行后的最后一道保险）
    holdings = _load_holdings()
    if code in holdings and holdings[code].get("status") == "holding":
        _logger.info(f"[开火拦截·ALREADY_HOLDING] {code} 账本已有 holding，跳过")
        return

    # ── 计算买入手数（10,000 元 ÷ ask1，向下取 100 整数倍）
    if ask1 <= 0 or ask1 > last_price * 1.05:
        _logger.warning(f"[开火拦截·BAD_ASK1] {code} ask1={ask1:.4f} 盘口异常，跳过")
        return

    qty: int = int(FIRE_CAPITAL / ask1 / 100) * 100
    if qty <= 0:
        _logger.warning(f"[开火拦截·QTY_ZERO] {code} ask1={ask1:.4f} 计算手数=0，跳过")
        return

    # ── FIRE_PAUSED 沙盘模式（三关全过 → 模拟建仓，不实际下单）
    if FIRE_PAUSED:
        # 冷却时间戳刷新：沙盘遵守与实盘相同的 60s 冷却，保证信号密度一致
        _last_fire_time[code] = current_time
        _logger.info(
            f"{_Y}[PAPER·FIRE] {code} | 三关全过，模拟开仓"
            f" | qty={qty}手 ask1={ask1:.4f}"
            f" | lambda={lam:.3f} OBI={obi:.3f}{_E}"
        )
        _simulate_paper_entry(code, ask1, qty, lam, obi)
        return   # 沙盘：不调用 order_stock，不修改实盘任何锁

    # ── 发委托（FIX_PRICE 追 ask1）
    seq: int = _xt_trader.order_stock(
        _acc, code,
        xtconstant.STOCK_BUY,
        qty,
        xtconstant.FIX_PRICE,
        ask1,
        "Hawk_V3",      # strategyName
        "Hawk_Entry",   # orderRemark
    )

    if seq <= 0:
        _logger.error(
            f"{_R}[FIRE·REJECTED] {code} seq={seq} 委托被拒"
            f" | qty={qty}手 ask1={ask1:.4f} λ={lam:.3f}{_E}"
        )
        return

    # ── 原子写入三个内存状态（顺序固定）
    # ① 悲观在途锁（最优先，防下一个 Tick 连发）
    with _pending_locks_mu:
        _pending_locks.add(code)
        _pending_lock_timestamps[code] = time.time()  # 超时守卫计时起点

    # ② Fill-Based 注册表（供 on_order_trade 回调精确入账）
    with _hawk_pending_mu:
        _hawk_pending[seq] = {
            "code":      code,
            "direction": "buy",
            "qty":       qty,
            "ask1":      ask1,
            "lam":       round(lam, 4),
            "obi":       round(obi, 4),
            "sent_at":   time.time(),
        }

    # ③ 冷却时间戳刷新（tick 回调单线程，无需额外锁）
    _last_fire_time[code] = current_time

    _logger.info(
        f"{_B}{_R}🔥 FIRE {code}"
        f" | seq={seq} | qty={qty}手"
        f" | ask1={ask1:.4f} | capital≈{qty * ask1:.0f}元"
        f" | λ={lam:.3f} | OBI={obi:.3f}{_E}"
    )

    _write_telemetry({
        "event_type": "SIGNAL_DETECTED",
        "code":       code,
        "last_price": round(last_price, 4),
        "lam":        round(lam, 4),
        "obi":        round(obi, 4),
        "delta_vol":  delta_vol,
        "ask1":       round(ask1, 4),
        "bid1":       round(bid1, 4),
        "buy_flag":   buy_flag,
    })

    send_webhook(
        f"🔥 [Hawkes V3 开火] {code}",
        f"购入 {qty}手 @ ask1={ask1:.4f}\n"
        f"λ={lam:.3f} | OBI={obi:.3f}\n"
        f"预估资金≈{qty * ask1:.0f}元 | seq={seq}",
    )


# ==============================================================================
# 📝  PAPER TRADING — 沙盘模拟引擎（FIRE_PAUSED=True 专属）
#     _simulate_paper_entry : 模拟建仓，写入 _paper_holdings
#     _paper_exit_monitor   : 每秒扫描，对照 TP/SL/Time 触发模拟退出
#     _write_paper_ledger   : 每笔逐条写入 CSV，供收盘后复盘
# ==============================================================================


def _write_paper_ledger(row: dict):
    """线程安全追加一笔沙盘收益明细到 Paper Ledger CSV。首次自动写 Header。"""
    with _PAPER_LEDGER_LOCK:
        is_new = not os.path.exists(_PAPER_LEDGER_FILE)
        with open(_PAPER_LEDGER_FILE, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_PAPER_LEDGER_HEADER, extrasaction="ignore")
            if is_new:
                w.writeheader()
            row.setdefault("entry_time",  datetime.datetime.now().strftime("%H:%M:%S.%f")[:12])
            row.setdefault("exit_time",   "")
            row.setdefault("exit_price",  "")
            row.setdefault("hold_sec",    "")
            row.setdefault("exit_reason", "")
            row.setdefault("gross_pnl",   "")
            row.setdefault("est_commission", "")
            row.setdefault("net_pnl",     "")
            w.writerow(row)


def _simulate_paper_entry(code: str, ask1: float, qty: int, lam: float, obi: float):
    """
    模拟建仓（沙盘专属）。
    - 以 ask1 快照价格记录入场成本
    - 写入 _paper_holdings（内存），生命周期由 _paper_exit_monitor 管理
    - 同一标的已有沙盘持仓时跳过（防重复）
    """
    with _paper_holdings_mu:
        if code in _paper_holdings:
            _logger.debug(f"[PAPER·SKIP] {code} 沙盘已有持仓，跳过模拟建仓")
            return
        entry_unix = time.time()
        _paper_holdings[code] = {
            "qty":         qty,
            "entry_price": round(ask1, 6),
            "entry_unix":  entry_unix,
            "entry_time":  datetime.datetime.now().strftime("%H:%M:%S.%f")[:12],
            "lam":         round(lam, 4),
            "obi":         round(obi, 4),
            "ask1":        round(ask1, 4),
            "capital":     round(ask1 * qty, 2),
            "trade_id":    f"{datetime.date.today():%Y%m%d}_{datetime.datetime.now():%H%M%S}_{code[:6]}",
        }

    capital = ask1 * qty
    _logger.info(
        f"{_Y}{_B}[PAPER·OPEN] {code}"
        f" | 模拟持仓 {qty}手 @ ask1={ask1:.4f}"
        f" | capital={capital:.0f}元"
        f" | lambda={lam:.3f} OBI={obi:.3f}{_E}"
    )
    send_webhook(
        f"📝 [Paper 开仓] {code}",
        f"模拟 {qty}手 @ {ask1:.4f}\n"
        f"capital={capital:.0f}元 | λ={lam:.3f} OBI={obi:.3f}",
    )


def _paper_exit_monitor():
    """
    后台守护线程（仅 FIRE_PAUSED=True 时启动）。
    每 1 秒扫描 _paper_holdings，按以下优先级触发模拟退出：
      1. 时间止损（最高优先级）：持仓 > TIME_STOP_SEC (45s)
      2. 止盈：浮盈 >= TP_PROFIT_ABS (+15 元)
      3. 止损：浮亏 <= SL_LOSS_ABS  (-30 元)
    退出时：拉取真实 bid1 作为模拟卖价，计算 gross/net pnl，写 Paper Ledger CSV。
    注：手续费估算 = 买入成交额×万1 + 卖出成交额×万1（双边万1，ETF 免印花税）。
    """
    _logger.info("📝 [Paper·Monitor] 沙盘监控线程已启动（1秒/次）")
    while True:
        time.sleep(1.0)
        try:
            with _paper_holdings_mu:
                active = dict(_paper_holdings)   # 快照，不持锁做 I/O
            if not active:
                continue

            # 批量拉取真实 Tick（只读行情，不触碰账户）
            ticks    = xtdata.get_full_tick(list(active.keys()))
            now_unix = time.time()

            for code, info in active.items():
                tick = ticks.get(code)
                if not tick:
                    continue

                last_price  = float(tick.get("lastPrice", 0.0))
                if last_price <= 0.0:
                    continue

                entry_price = float(info["entry_price"])
                qty         = int(info["qty"])
                entry_unix  = float(info["entry_unix"])
                held_sec    = now_unix - entry_unix
                pnl         = (last_price - entry_price) * qty

                # ── 判断退出条件（优先级：时间 > 止盈 > 止损）
                exit_reason = None
                if held_sec > TIME_STOP_SEC:
                    exit_reason = f"time_stop/{held_sec:.1f}s"
                elif pnl >= TP_PROFIT_ABS:
                    exit_reason = f"tp/{pnl:+.1f}"
                elif pnl <= SL_LOSS_ABS:
                    exit_reason = f"sl/{pnl:+.1f}"

                if exit_reason is None:
                    continue   # 未触发，继续持有

                # ── 取真实 bid1 作为模拟卖价
                bid_list = tick.get("bidPrice") or []
                bid1     = float(bid_list[0]) if bid_list and bid_list[0] > 0 else last_price
                if bid1 <= 0:
                    bid1 = last_price

                # ── 计算盈亏
                exit_price   = bid1
                gross_pnl    = round((exit_price - entry_price) * qty, 2)
                # 双边手续费估算：买入成交额×万1 + 卖出成交额×万1（ETF 免印花税）
                est_comm     = round((entry_price * qty + exit_price * qty) * 0.0001, 2)
                net_pnl      = round(gross_pnl - est_comm, 2)
                hold_sec_int = round(held_sec, 1)

                # ── 退出日志
                color  = _G if gross_pnl >= 0 else _R
                symbol = "✅ 止盈" if "tp" in exit_reason else "🔪 止损" if "sl" in exit_reason else "⏰ 时间止损"
                _logger.info(
                    f"{_B}{color}[PAPER·EXIT] {code}"
                    f" | {symbol}"
                    f" | 持仓={hold_sec_int}s"
                    f" | 入场={entry_price:.4f} 模拟出场={exit_price:.4f}"
                    f" | qty={qty}手"
                    f" | 毛盈亏={gross_pnl:+.2f}元"
                    f" | 手续费≈{est_comm:.2f}元"
                    f" | {_R if net_pnl < 0 else _G}净盈亏={net_pnl:+.2f}元{_E}"
                )

                # ── 写 Paper Ledger CSV（每笔完整明细）
                _write_paper_ledger({
                    "trade_id":       info["trade_id"],
                    "code":           code,
                    "entry_time":     info["entry_time"],
                    "entry_price":    entry_price,
                    "qty":            qty,
                    "capital":        info["capital"],
                    "lam":            info["lam"],
                    "obi":            info["obi"],
                    "exit_time":      datetime.datetime.now().strftime("%H:%M:%S.%f")[:12],
                    "exit_price":     round(exit_price, 4),
                    "hold_sec":       hold_sec_int,
                    "exit_reason":    exit_reason,
                    "gross_pnl":      gross_pnl,
                    "est_commission": est_comm,
                    "net_pnl":        net_pnl,
                })

                send_webhook(
                    f"📝 [Paper 平仓] {code} {symbol}",
                    f"入场={entry_price:.4f} 出场={exit_price:.4f}\n"
                    f"持仓={hold_sec_int}s | 毛盈亏={gross_pnl:+.2f}元\n"
                    f"手续费≈{est_comm:.2f}元 | 净盈亏={net_pnl:+.2f}元",
                )

                # ── 移除沙盘持仓（释放下一枪资格）
                with _paper_holdings_mu:
                    _paper_holdings.pop(code, None)

        except Exception as e:
            _logger.error(f"❌ [Paper·Monitor] 扫描异常: {e}")


# ==============================================================================
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 🔪  MODULE 3 — 极速退壳协议 (Micro-Exit Protocol)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ==============================================================================



def _execute_exit(code: str, reason: str, entry_pnl: float):
    """
    物理清仓执行器（铁律三）。
    只被 _trigger_micro_exit 调用，不直接暴露给外部。
    流程：query_stock_positions → can_use_volume → 全额 FIX_PRICE 卖出。
    """
    global _xt_trader, _acc

    # ── 物理查仓（铁律三：不信账本数量，查物理真相）
    try:
        pos_list = _xt_trader.query_stock_positions(_acc) or []
    except Exception as e:
        _logger.error(f"❌ [退出·查仓异常] {code} {e}，人工介入！")
        return

    target = next((p for p in pos_list if p.stock_code == code), None)

    if target is None or int(target.can_use_volume) <= 0:
        _logger.warning(
            f"⚠️ [退出·幽灵] {code} 物理可用量=0，直接清账"
            f" | reason={reason}"
        )
        _clear_holding_and_unlock(code)
        return

    sell_qty: int = int(target.can_use_volume)

    # ── 获取 bid1（追买一价，保证穿透成交）
    try:
        ticks    = xtdata.get_full_tick([code])
        tick     = ticks.get(code, {})
        bid_list = tick.get("bidPrice") or []
        bid1     = float(bid_list[0]) if bid_list and bid_list[0] > 0 else 0.0
        if bid1 <= 0:
            bid1 = float(tick.get("lastPrice", 0.0))
    except Exception:
        bid1 = 0.0

    if bid1 <= 0:
        _logger.error(f"❌ [退出·无价] {code} 无法获取 bid1，人工介入！")
        return

    # ── 委托标识（QMT 终端区分）
    if "time_stop"    in reason: remark = "Hawk_TS"
    elif "exposure_cap" in reason: remark = "Hawk_EC"
    elif entry_pnl >= 0:           remark = "Hawk_TP"
    else:                          remark = "Hawk_SL"

    # ── 发卖单
    seq: int = _xt_trader.order_stock(
        _acc, code,
        xtconstant.STOCK_SELL,
        sell_qty,
        xtconstant.FIX_PRICE,
        bid1,
        "Hawk_V3",
        remark,
    )

    if   "time_stop"     in reason: symbol = "⏰ 时间斩仓"
    elif "exposure_cap"  in reason: symbol = "🛡️ 敞口强平"
    elif entry_pnl >= 0:            symbol = "✅ 止盈"
    else:                           symbol = "🔪 斩仓"
    color = _G if entry_pnl >= 0 else _R

    _logger.info(
        f"{_B}{color}{symbol} {code}"
        f" | seq={seq} | sell={sell_qty}手"
        f" | bid1={bid1:.4f} | pnl≈{entry_pnl:+.1f}元"
        f" | reason={reason}{_E}"
    )

    if seq <= 0:
        _logger.error(
            f"❌ [退出·委托被拒] {code} seq={seq}"
            f" | 保守清账解锁，请人工复核！"
        )
        _clear_holding_and_unlock(code)
        return

    # ── 注册 pending（on_order_trade 回调后精确清账）
    with _hawk_pending_mu:
        _hawk_pending[seq] = {
            "code":      code,
            "direction": "sell",
            "qty":       sell_qty,
            "bid1":      bid1,
            "pnl":       round(entry_pnl, 2),
            "reason":    reason,
            "sent_at":   time.time(),
        }

    emoji_map = {"tp": "💰", "sl": "🔪", "time_stop": "⏰", "exposure_cap": "🛡️"}
    em = next((v for k, v in emoji_map.items() if k in reason), "📤")
    send_webhook(
        f"{em} [Hawkes V3 {symbol}] {code}",
        f"卖出 {sell_qty}手 @ bid1={bid1:.4f}\n"
        f"浮盈亏≈{entry_pnl:+.1f}元 | reason={reason} | seq={seq}",
    )


def _trigger_micro_exit(code: str, last_price: float, reason: str):
    """
    退出统一入口（止盈/止损/时间斩/敞口 → 均收敛至此）。
    防重入护盾：仅 status=="holding" 时执行，非 holding 静默 return。
    封印顺序：
      ① holdings.status → "exiting"（防监控线程并发重入）
      ② _pending_locks.add（防 on_tick 新买单锁A穿透）
      ③ _execute_exit（发卖单）
    """
    holdings = _load_holdings()
    info     = holdings.get(code)

    if info is None:
        _logger.debug(f"[退出·空账本] {code} 账本无记录，忽略")
        return
    if info.get("status") != "holding":
        _logger.debug(
            f"[退出·非holding] {code} status={info.get('status')}，已在退出中，跳过"
        )
        return

    entry_price: float = float(info.get("entry_price", last_price))
    qty:         int   = int(info.get("qty", 0))
    pnl: float = round((last_price - entry_price) * qty, 2)

    _logger.info(
        f"{_Y}[Micro-Exit·触发] {code}"
        f" | reason={reason}"
        f" | 现价={last_price:.4f} | 入场={entry_price:.4f}"
        f" | qty={qty}手 | pnl={pnl:+.2f}元{_E}"
    )

    # ① 账本封印（原子写，防并发重入）
    holdings[code]["status"] = "exiting"
    _save_holdings(holdings)

    # ② _pending_locks 再次封印
    with _pending_locks_mu:
        _pending_locks.add(code)
        _pending_lock_timestamps.setdefault(code, time.time())  # 不刷新，保守计时
    _execute_exit(code, reason, pnl)


def _clear_holding_and_unlock(code: str):
    """
    原子三步清账：清除账本 + 敞口归零 + 解除 _pending_locks。
    调用方：on_order_trade SELL 回调 / 幽灵清账 / 委托被拒保守处理。
    """
    # ① 清除账本
    h = _load_holdings()
    if code in h:
        del h[code]
        _save_holdings(h)

    # ② 敞口归零（Fill-Based，铁律一）
    with _exposure_mu:
        _current_exposure[code] = 0.0

    # ③ 解锁（标的完全释放，可重新交易）
    with _pending_locks_mu:
        _pending_locks.discard(code)
        _pending_lock_timestamps.pop(code, None)  # 清除超时守卫记录

    _logger.info(
        f"{_G}[解锁·完成] {code}"
        f" | 账本已清 · 敞口归零 · _pending_locks 已释放"
        f" | 标的恢复可交易{_E}"
    )


# ==============================================================================
# 📞  HawkTraderCallback（Fill-Based 入账核心，铁律一）
# ==============================================================================

class HawkTraderCallback(XtQuantTraderCallback):
    """Hawkes 专属回调，只处理自己注册的 pending 订单。"""

    def on_disconnected(self):
        _logger.error("❌ [Hawk·QMT] 连接断开，请立即检查 miniQMT！")
        send_webhook("🚨 [Hawkes V3] QMT 连接断开", "请立即检查 miniQMT 状态！")

    def on_order_trade(self, trade):
        """
        成交回调（QMT 推送线程异步调用）。
        BUY  → 写持仓账本 + 充值 _current_exposure + 解除在途锁
        SELL → 写遥测 + _clear_holding_and_unlock（清账/归零/解锁）
        """
        with _hawk_pending_mu:
            meta = _hawk_pending.pop(trade.order_id, None)

        code   = getattr(trade, "stock_code", None) or (meta and meta.get("code"))
        filled = int(trade.traded_volume)
        price  = float(trade.traded_price)

        # ── 孤单兜底（跨进程重启后 pending 丢失）
        if meta is None:
            if code and code in HAWK_CODES:
                h = _load_holdings()
                if code in h and h[code].get("status") in ("holding", "exiting"):
                    ep        = float(h[code].get("entry_price", price))
                    final_pnl = round((price - ep) * filled, 2)
                    _write_telemetry({
                        "event_type":     "POSITION_CLOSED",
                        "code":           code,
                        "last_price":     round(price, 4),
                        "entry_price":    round(ep, 4),
                        "qty":            filled,
                        "unrealized_pnl": final_pnl,
                        "exit_reason":    "orphan_fill",
                    })
                    _clear_holding_and_unlock(code)
                    _logger.info(
                        f"✅ [Fill·孤单·Sell] {code} -{filled}手 @{price:.4f}"
                        f" | 最终盈亏≈{final_pnl:+.1f}元 | 账本已清"
                    )
            return

        direction = meta["direction"]

        # ══════════════════════════════════════════════════════
        # ▶ BUY 成交
        # ══════════════════════════════════════════════════════
        if direction == "buy":
            entry_unix = time.time()   # 实际成交时间戳（供时间止损计算）

            # ⚠️ 铁律四死锁修复：此处已在外层持有 _holdings_mu，
            # 必须使用无锁版 _load_holdings_unlocked / _save_holdings_unlocked，
            # 否则 threading.Lock() 不可重入 → 永久死锁 → 账本写不进去
            # → _micro_exit_monitor 读不到 holding → 45s 时间斩仓永不触发！
            with _holdings_mu:
                h = _load_holdings_unlocked()
                if code in h and h[code].get("status") == "holding":
                    # 分批成交：VWAP 累加
                    old     = h[code]
                    old_qty = int(old.get("qty", 0))
                    old_ep  = float(old.get("entry_price", price))
                    new_qty = old_qty + filled
                    new_ep  = (old_ep * old_qty + price * filled) / new_qty if new_qty else price
                    h[code]["qty"]         = new_qty
                    h[code]["entry_price"] = round(new_ep, 6)
                    h[code].setdefault("entry_unix", entry_unix)
                else:
                    # 首次成交：pending → holding
                    h[code] = {
                        "qty":         filled,
                        "entry_price": round(price, 6),
                        "entry_time":  datetime.datetime.now().strftime("%H:%M:%S"),
                        "entry_unix":  entry_unix,   # ← 时间止损基准
                        "capital":     round(filled * price, 2),
                        "status":      "holding",
                    }
                _save_holdings_unlocked(h)
                snapshot = h[code].copy()

            # 敞口充值（Fill-Based，精确记录实际成交市值）
            with _exposure_mu:
                _current_exposure[code] = _current_exposure.get(code, 0.0) + filled * price

            # 在途锁解除（持仓账本 "holding" 接管守门权）
            with _pending_locks_mu:
                _pending_locks.discard(code)

            ep_final = snapshot["entry_price"]
            exp_val  = _current_exposure.get(code, 0.0)
            _logger.info(
                f"✅ [Fill·Buy] {code}"
                f" +{filled}手 @{price:.4f}"
                f" | entry_price={ep_final:.4f}"
                f" | 敞口={exp_val:.0f}元"
                f" | 在途锁已释放"
            )

            _write_telemetry({
                "event_type":     "POSITION_OPENED",
                "code":           code,
                "last_price":     round(price, 4),
                "lam":            meta.get("lam", 0),
                "obi":            meta.get("obi", 0),
                "ask1":           round(meta.get("ask1", price), 4),
                "entry_price":    ep_final,
                "qty":            snapshot["qty"],
                "unrealized_pnl": 0.0,
            })

        # ══════════════════════════════════════════════════════
        # ▶ SELL 成交
        # ══════════════════════════════════════════════════════
        elif direction == "sell":
            old_h     = _load_holdings()
            old_info  = old_h.get(code, {})
            ep        = float(old_info.get("entry_price", price))
            final_pnl = round((price - ep) * filled, 2)

            _write_telemetry({
                "event_type":     "POSITION_CLOSED",
                "code":           code,
                "last_price":     round(price, 4),
                "entry_price":    round(ep, 4),
                "qty":            filled,
                "unrealized_pnl": final_pnl,
                "exit_reason":    meta.get("reason", "sell"),
            })

            _clear_holding_and_unlock(code)

            _logger.info(
                f"✅ [Fill·Sell] {code}"
                f" -{filled}手 @{price:.4f}"
                f" | 最终盈亏≈{final_pnl:+.1f}元"
                f" | 账本已清 · 敞口归零 · 锁全解"
            )

    def on_order_error(self, order_error):
        """委托被拒 → 撤销对应封印，恢复标的可交易状态。"""
        seq = order_error.order_id
        _logger.error(
            f"❌ [Hawk·委托错误] seq={seq}"
            f" error_id={order_error.error_id}"
            f" msg={order_error.error_msg}"
        )
        with _hawk_pending_mu:
            meta = _hawk_pending.pop(seq, {})
        code = meta.get("code")
        if code:
            direction = meta.get("direction", "buy")
            if direction == "buy":
                # 买单被拒：仅解 pending_locks（敞口未充值，账本无记录）
                with _pending_locks_mu:
                    _pending_locks.discard(code)
                _logger.info(f"🔓 [委托被拒·buy] {code} → _pending_locks 已解除")
            else:
                # 卖单被拒：保守清账（防僵死仓），并告警
                _logger.warning(
                    f"⚠️ [委托被拒·sell] {code} 卖单被拒，保守清账解锁"
                    f"（需人工复核实盘持仓！）"
                )
                _clear_holding_and_unlock(code)
        send_webhook(
            "🚨 [Hawkes V3] 委托异常",
            f"seq={seq}\nerror_id={order_error.error_id}\n{order_error.error_msg}",
        )


# ==============================================================================
# ⏱️  Micro-Exit 监控线程（每秒扫描三条退出线）
# ==============================================================================

def _micro_exit_monitor():
    """
    后台守护线程。每 1 秒扫描全量持仓，按优先级检查三条退出线：
      1. 时间斩仓（最高优先级）：持仓 > TIME_STOP_SEC(45s)
      2. 止盈：浮盈 ≥ TP_PROFIT_ABS (+15 元)
      3. 止损：浮亏 ≤ SL_LOSS_ABS  (-30 元)
    注：锁C 敞口封顶由 on_tick 模块一实时触发，不在此处重复。
    """
    _logger.info("🛡️  [Micro-Exit·Monitor] 监控线程已启动（1秒/次）")
    while True:
        time.sleep(1.0)
        try:
            holdings = _load_holdings()
            if not holdings:
                continue

            active = {c: v for c, v in holdings.items()
                      if v.get("status") == "holding"}
            if not active:
                continue

            # 批量拉 Tick（一次 I/O，降低 API 频率）
            ticks    = xtdata.get_full_tick(list(active.keys()))
            now_unix = time.time()

            for code, info in active.items():
                tick = ticks.get(code)
                if not tick:
                    continue

                last_price  = float(tick.get("lastPrice", 0.0))
                if last_price <= 0.0:
                    continue

                entry_price = float(info.get("entry_price", last_price))
                qty         = int(info.get("qty", 0))
                entry_unix  = float(info.get("entry_unix", now_unix))
                if qty <= 0:
                    continue

                pnl = (last_price - entry_price) * qty

                # ── 退出线1：时间斩仓（最高优先级）
                held_sec = now_unix - entry_unix
                if held_sec > TIME_STOP_SEC:
                    _logger.info(
                        f"{_Y}[TimeStop·触发] {code}"
                        f" 持仓={held_sec:.1f}s > {TIME_STOP_SEC}s"
                        f" | pnl={pnl:+.1f}元（时间到，强平）{_E}"
                    )
                    _trigger_micro_exit(code, last_price,
                                        reason=f"time_stop/{held_sec:.1f}s")
                    continue

                # ── 退出线2：止盈
                if pnl >= TP_PROFIT_ABS:
                    _logger.info(
                        f"{_G}[TP·触发] {code}"
                        f" pnl={pnl:+.1f}元 ≥ +{TP_PROFIT_ABS}元{_E}"
                    )
                    _trigger_micro_exit(code, last_price,
                                        reason=f"tp/{pnl:+.1f}")
                    continue

                # ── 退出线3：止损
                if pnl <= SL_LOSS_ABS:
                    _logger.info(
                        f"{_R}[SL·触发] {code}"
                        f" pnl={pnl:+.1f}元 ≤ {SL_LOSS_ABS}元{_E}"
                    )
                    _trigger_micro_exit(code, last_price,
                                        reason=f"sl/{pnl:+.1f}")

            # ────────────────────────────────────────────────
            # 🛡️  锁A超时守卫（防 Fill SELL 回调丢失导致全天致盲）
            #    每秒扫描 _pending_lock_timestamps，超过180秒且
            #    账本中已无 holding/exiting 记录 → 强制解锁
            # ────────────────────────────────────────────────
            with _pending_locks_mu:
                stale_candidates = {
                    c: ts for c, ts in _pending_lock_timestamps.items()
                    if now_unix - ts > _PENDING_LOCK_TIMEOUT_SEC
                }
            for stale_code, lock_ts in stale_candidates.items():
                h      = _load_holdings()
                status = h.get(stale_code, {}).get("status", "none")
                locked_sec = now_unix - lock_ts
                if status not in ("holding", "exiting"):
                    # 账本已无记录，Fill 回调大概率永久丢失 → 强制解锁
                    _logger.warning(
                        f"{_Y}⚠️ [锁A超时·强制解锁] {stale_code}"
                        f" 锁A已持续 {locked_sec:.0f}s > {_PENDING_LOCK_TIMEOUT_SEC}s"
                        f" 且账本 status={status}，Fill 回调可能丢失，强制释放锁{_E}"
                    )
                    with _pending_locks_mu:
                        _pending_locks.discard(stale_code)
                        _pending_lock_timestamps.pop(stale_code, None)
                    send_webhook(
                        f"⚠️ [Hawkes] 锁A超时强制解锁 {stale_code}",
                        f"锁A滞留 {locked_sec:.0f}s，Fill SELL 回调疑似丢失\n"
                        f"账本 status={status}，强制解锁恢复交易",
                    )

        except Exception as e:
            _logger.error(f"❌ [Micro-Exit·Monitor] 扫描异常: {e}")



# ==============================================================================
# 📊  HOLDING_LOG 遥测线程（每 5 分钟一条浮盈快照）
# ==============================================================================

def _holding_pnl_logger():
    """后台守护线程：每 5 分钟对全量 holding 写一条 HOLDING_LOG 遥测。"""
    _logger.info("📊 [HoldingLog] 遥测线程已启动（每 5 分钟一条）")
    while True:
        time.sleep(300)
        try:
            holdings = _load_holdings()
            active   = {c: v for c, v in holdings.items()
                        if v.get("status") == "holding"}
            if not active:
                continue
            ticks = xtdata.get_full_tick(list(active.keys()))
            for code, info in active.items():
                tick       = ticks.get(code, {})
                last_price = float(tick.get("lastPrice", 0.0))
                if last_price <= 0.0:
                    continue
                ep  = float(info.get("entry_price", 0.0))
                qty = int(info.get("qty", 0))
                if ep <= 0.0 or qty <= 0:
                    continue
                pnl = round((last_price - ep) * qty, 2)
                with _exposure_mu:
                    exp_val = _current_exposure.get(code, 0.0)
                _write_telemetry({
                    "event_type":     "HOLDING_LOG",
                    "code":           code,
                    "last_price":     round(last_price, 4),
                    "entry_price":    round(ep, 4),
                    "qty":            qty,
                    "unrealized_pnl": pnl,
                })
                _logger.info(
                    f"📊 [HoldingLog] {code}"
                    f" | 现价={last_price:.4f} | 入场={ep:.4f}"
                    f" | {qty}手 | pnl={pnl:+.1f}元 | 敞口={exp_val:.0f}元"
                )
        except Exception as e:
            _logger.error(f"❌ [HoldingLog·遥测] 异常: {e}")


# ==============================================================================
# 🛡️  白名单校验（盘前防错）
# ==============================================================================

def _validate_whitelist() -> bool:
    """从 t0_absolute_pool.csv 校验 HAWK_CODES 是否全部在 T+0 池中。"""
    if not os.path.exists(T0_POOL_CSV):
        _logger.warning(f"⚠️  找不到 {T0_POOL_CSV}，跳过白名单校验")
        return True
    pool = set()
    with open(T0_POOL_CSV, encoding="gbk", errors="replace") as f:
        for row in csv.reader(f):
            if row:
                pool.add(row[0].strip())
    missing = [c for c in HAWK_CODES if c not in pool]
    if missing:
        _logger.error(f"⛔ 白名单校验失败！不在 T+0 池: {missing}")
        return False
    _logger.info(f"✅ 白名单校验通过，共 {len(HAWK_CODES)} 只标的")
    return True


# ==============================================================================
# 🚀  主入口
# ==============================================================================

def run_hawkes_executor():
    global _xt_trader, _acc

    _logger.info("=" * 68)
    _logger.info(f"🦅 [Hawkes V3 Titan] 启动 | {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    _logger.info(f"   白名单     : {HAWK_CODES}")
    _logger.info(f"   开火资金   : {FIRE_CAPITAL:.0f}元")
    _logger.info(f"   λ阈值={LAMBDA_THRESHOLD} | OBI阈值={OBI_THRESHOLD}"
                 f" | 冷却={FIRE_COOLDOWN_SEC}s | 敞口封顶={EXPOSURE_CAP:.0f}元")
    _logger.info(f"   止盈={TP_PROFIT_ABS:+}元 | 止损={SL_LOSS_ABS}元"
                 f" | 时间斩仓={TIME_STOP_SEC}s")
    _logger.info(f"   FIRE_PAUSED={FIRE_PAUSED}（True=仅 Micro-Exit + 模拟弹日志）")
    _logger.info("=" * 68)

    # 步骤 1：白名单校验
    if not _validate_whitelist():
        _logger.error("⛔ 启动终止：白名单校验失败")
        return

    # 步骤 2：连接 QMT 交易网关
    _logger.info("🔌 正在连接 QMT 交易网关...")
    _xt_trader = XtQuantTrader(QMT_PATH, int(time.time()))   # Hawk 专属 session
    cb = HawkTraderCallback()
    _xt_trader.register_callback(cb)
    _xt_trader.start()
    time.sleep(3)   # 等待握手（xtquant-api-patterns 规范）
    conn = _xt_trader.connect()
    if conn != 0:
        _logger.error(f"❌ QMT 连接失败 conn={conn}，退出")
        return
    _logger.info("✅ QMT 连接成功")

    _acc = StockAccount(ACCOUNT_ID)
    if _xt_trader.subscribe(_acc) != 0:
        _logger.error("❌ 账户订阅失败，退出")
        return
    _logger.info(f"✅ 账户订阅成功: {ACCOUNT_ID}")

    # 步骤 3：启动后台守护线程
    thread_targets = [
        (_micro_exit_monitor, "MicroExit-Monitor"),
        (_holding_pnl_logger, "HoldingLog"),
    ]
    if FIRE_PAUSED:
        # 沙盘模式：额外启动 Paper Monitor，用真实 Tick 追踪模拟持仓盈亏
        thread_targets.append((_paper_exit_monitor, "Paper-Monitor"))
    for target, name in thread_targets:
        t = threading.Thread(target=target, daemon=True, name=name)
        t.start()
        _logger.info(f"🧵 [{name}] 线程已启动")

    # 步骤 4：订阅实时 Tick（每只标的独立闭包，防晚绑定陷阱）
    for code in HAWK_CODES:
        xtdata.subscribe_quote(
            stock_code=code,
            period="tick",
            count=0,
            callback=_make_tick_callback(code),
        )
        _logger.info(f"   ✅ 已订阅 Tick: {code}")

    _logger.info(f"\n🛰️  进入监听模式 | 日志: {_LOG_FILE}\n")

    # 步骤 5：阻塞主线程（QMT 事件循环）
    xtdata.run()


# ==============================================================================
if __name__ == "__main__":
    run_hawkes_executor()
