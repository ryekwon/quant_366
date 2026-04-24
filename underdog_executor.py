# ==============================================================================
# 🐕 underdog_executor.py — 落水狗左侧抄底执行器
#
# 策略逻辑：
#   • 扫描器 (scan_underdogs)：三维共振选股（跌幅极值 + 放量点火 + 结构突破）
#   • 巡逻器 (patrol_positions)：四维退出矩阵（断头台 / 时间腐烂 / 均值回归 / 动能衰竭）
#
# 架构合规（四大铁律）：
#   ① Fill-Based 延迟记账 — 发单只注册 pending，on_stock_trade 回调后才写账本
#   ② 盲人摸象隔离 — 只读 underdog_slots.json，不查 query_stock_positions
#   ③ 对称清仓 — 平仓量从账本 bought_qty 读取
#   ④ 并发锁保护 — _ledger_lock / _pending_lock 保护所有 IO
#
# 资金：FUNDS_LIMIT = 50_000（固定，禁止动态缩放）
# 账本：.state/underdog_slots.json
# ==============================================================================

import sys
import io

# ── 强制 UTF-8 输出（防 Windows GBK 控制台崩溃）────────────────────────────
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
if hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

import os
import json
import time
import logging
import threading
from datetime import datetime, date

import numpy as np

from xtquant import xtdata, xtconstant
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

from dotenv import load_dotenv

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

# ── ETF 池获取工具（tools/fetch_etf_universe.py）──────────────────────────
# 接口：get_all_etf_codes() → List[str]，返回全市场纯 ETF code list
try:
    from tools.fetch_etf_universe import get_all_etf_codes as get_etf_pool
except ImportError:
    def get_etf_pool():  # type: ignore
        """Fallback stub：xtquant 未安装时使用"""
        logging.warning("[Underdog] tools.fetch_etf_universe 未找到，返回空 ETF 池")
        return []

# ==============================================================================
# ── 环境变量 & 路径
# ==============================================================================
load_dotenv()
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")
QMT_PATH        = os.getenv("QMT_PATH", "")
ACCOUNT_ID      = os.getenv("ACCOUNT_ID", "")

_DIR        = os.path.dirname(os.path.abspath(__file__))
STATE_DIR   = os.path.join(_DIR, ".state")
LEDGER_FILE = os.path.join(STATE_DIR, "underdog_slots.json")
LOG_DIR     = os.path.join(_DIR, "logs")

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ==============================================================================
# ── 日志配置
# ==============================================================================
_log_date  = datetime.now().strftime("%Y%m%d")
_log_file  = os.path.join(LOG_DIR, f"{_log_date}_underdog_executor.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("underdog")

# ==============================================================================
# ── 策略参数（固定，禁止动态缩放）
# ==============================================================================
FUNDS_LIMIT       = 50_000.0    # 固定分配资金（5 万）
LOOKBACK_DAYS     = 250         # 跌幅极值回溯窗口（日线根数）
LOW_PERCENTILE    = 10          # 跌幅极值：过去 250 日最低价的 10% 分位
IGNITION_GAIN     = 0.03        # 放量点火：当日涨幅 > 3%
VOLUME_MULTIPLIER = 2.0         # 放量点火：当日量 > 20 日均量 × 2
BREAKOUT_WINDOW   = 10          # 结构突破：收盘价 > 过去 10 日最高价

HOLD_DAYS_DECAY   = 3           # 防线B：时间腐烂天数阈值
DECAY_PROFIT_MAX  = 0.005       # 防线B：未脱离成本区（成本×1.005以内）
HALF_DRAWDOWN     = 0.02        # 防线D：高点回撤 2% 触发移动止盈

# ── 分裂作息时间窗口 ──────────────────────────────────────────────────────────
# Patrol（四维退出矩阵）：盘中高频巡航，手起刀落
PATROL_OPEN       = "09:30"     # 巡逻开始
PATROL_CLOSE      = "14:55"     # 巡逻结束
PATROL_INTERVAL   = 30          # 巡逻轮询间隔（秒）

# Scanner（三维共振扫描）：每日 14:45 尾盘唤醒一次，量价结构已定型
SCANNER_OPEN      = "14:45"     # 扫描窗口开始
SCANNER_CLOSE     = "14:55"     # 扫描窗口结束（5分钟内只跑一次）

PENDING_TIMEOUT   = 90          # pending 委托超时阈值（秒）

# 订单标识规范（V4 §3）
STRATEGY_NAME = "Underdog"
REMARK_BUY    = "UD_Buy"
REMARK_SELL_A = "UD_SL_A"      # 防线A 断头台
REMARK_SELL_B = "UD_SL_B"      # 防线B 时间腐烂
REMARK_SELL_C = "UD_TP_C"      # 防线C 均值回归减仓
REMARK_SELL_D = "UD_TP_D"      # 防线D 动能衰竭止盈

# ==============================================================================
# ── 铁律四：并发锁
# ==============================================================================
_ledger_lock  = threading.Lock()
_pending_lock = threading.Lock()
_pending: dict = {}  # {seq: {code, direction, qty, sent_at, remark}}

# ==============================================================================
# ── N8N Webhook（失败静默，不阻断主逻辑）
# ==============================================================================
def send_webhook(title: str, message: str) -> None:
    """N8N 推送，超时 5s，失败静默。"""
    if not _HAS_REQUESTS or not N8N_WEBHOOK_URL:
        return
    try:
        requests.post(
            N8N_WEBHOOK_URL,
            json={
                "title":     title,
                "message":   message,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"[Webhook] 推送失败: {e}")

# ==============================================================================
# ── 账本 IO（原子写回 + 并发锁 — 铁律四）
# ==============================================================================
def load_ledger() -> dict:
    """加锁读账本；不存在时返回空字典。"""
    with _ledger_lock:
        if not os.path.exists(LEDGER_FILE):
            return {}
        try:
            with open(LEDGER_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[Ledger] 读取失败: {e}，返回空账本")
            return {}


def save_ledger(ledger: dict) -> None:
    """加锁原子写账本（tmp → replace）。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = LEDGER_FILE + ".tmp"
    with _ledger_lock:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(ledger, f, indent=4, ensure_ascii=False)
            os.replace(tmp, LEDGER_FILE)
        except Exception as e:
            logger.error(f"[Ledger] 写入失败: {e}")

# ==============================================================================
# ── 工具函数
# ==============================================================================
def _calc_qty(price: float, capital: float) -> int:
    """按资金和价格计算买入手数（100 股整数倍）。"""
    if price <= 0:
        return 0
    return int(capital / price / 100) * 100


def _is_patrol_time() -> bool:
    """当前是否在 Patrol 巡逻时段（09:30 ~ 14:55）。"""
    now_str = datetime.now().strftime("%H:%M")
    return PATROL_OPEN <= now_str <= PATROL_CLOSE


def _is_scanner_window() -> bool:
    """当前是否在 Scanner 扫描窗口（14:45 ~ 14:55）。"""
    now_str = datetime.now().strftime("%H:%M")
    return SCANNER_OPEN <= now_str <= SCANNER_CLOSE


def _today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def _days_since(date_str: str) -> int:
    """计算自 date_str（YYYYMMDD）至今的自然日天数。"""
    try:
        d = datetime.strptime(date_str, "%Y%m%d").date()
        return (date.today() - d).days
    except Exception:
        return 0


def _is_occupied(code: str, ledger: dict) -> bool:
    """该标的是否已在本策略账本中（防重复建仓）。"""
    return code in ledger and ledger[code].get("status") in ("FULL", "HALF")

# ==============================================================================
# ── QMT 交易回调（Fill-Based 记账 — 铁律一）
# ==============================================================================
class UnderdogCallback(XtQuantTraderCallback):
    """
    铁律一：只在 on_stock_trade 成交后才更新账本。
    铁律四：_pending_lock / _ledger_lock 保护并发。
    """

    def on_stock_trade(self, trade):
        seq    = trade.order_id
        filled = int(trade.traded_volume)
        price  = float(trade.traded_price)

        with _pending_lock:
            meta = _pending.get(seq)
        if meta is None:
            return  # 铁律二：不属于本引擎的委托，忽略

        if filled <= 0:
            return

        code      = meta["code"]
        direction = meta["direction"]
        remark    = meta.get("remark", "")

        # 累加分笔成交（修复：不提前 pop，满额才 pop）
        with _pending_lock:
            meta["filled_so_far"] = meta.get("filled_so_far", 0) + filled
            filled_so_far = meta["filled_so_far"]
            target_qty    = meta.get("qty", filled)

        ledger = load_ledger()

        if direction == "buy":
            # 新建仓位记录
            if code not in ledger:
                ledger[code] = {}
            slot = ledger[code]
            slot["bought_qty"]    = slot.get("bought_qty", 0) + filled
            slot["entry_price"]   = price   # 以实际成交价为准
            slot["status"]        = "FULL"
            slot["buy_date"]      = _today_str()
            slot["hold_days"]     = 0
            # ignition_low / highest_price 由扫描器写入，这里仅保留不覆盖
            if "ignition_low" not in slot:
                slot["ignition_low"] = price
            if "highest_price" not in slot:
                slot["highest_price"] = price
            logger.info(
                f"✅ [Fill·Buy] {code} 成交 {filled}股 @ {price:.3f} "
                f"累计={slot['bought_qty']}股"
            )

        else:  # sell
            slot = ledger.get(code, {})
            prev_qty = slot.get("bought_qty", 0)
            new_qty  = max(0, prev_qty - filled)
            slot["bought_qty"] = new_qty
            if new_qty == 0:
                # 全仓已清，归零该记录
                ledger[code]["status"] = "CLOSED"
                logger.info(
                    f"✅ [Fill·Sell·全平] {code} 全部成交 {filled}股 @ {price:.3f} "
                    f"[{remark}]"
                )
            else:
                # 半仓卖出（防线C）
                slot["status"] = "HALF"
                logger.info(
                    f"✅ [Fill·Sell·半仓] {code} 成交 {filled}股 @ {price:.3f} "
                    f"剩余={new_qty}股 [{remark}]"
                )

        save_ledger(ledger)

        if filled_so_far >= target_qty:
            with _pending_lock:
                _pending.pop(seq, None)
            logger.debug(f"[Pending清除] {code} {direction} 全额成交，移出 pending")

    def on_stock_order(self, order):
        """废单/撤单 → 清除 pending（不乐观写账，铁律一）。"""
        TERMINAL = {50, 52, 54}  # 已撤 / 废单 / 部撤
        if order.order_status not in TERMINAL:
            return
        seq = order.order_seq
        with _pending_lock:
            meta = _pending.pop(seq, None)
        if meta:
            status_map = {50: "已撤", 52: "废单", 54: "部撤"}
            label = status_map.get(order.order_status, str(order.order_status))
            logger.warning(
                f"⚠️ [订单{label}] {meta['code']} {meta['direction']} "
                f"seq={seq}，解除 pending 锁定"
            )

    def on_order_error(self, order_error):
        seq = order_error.order_id
        with _pending_lock:
            meta = _pending.pop(seq, None)
        if meta:
            logger.error(
                f"❌ [废单] {meta['code']} {meta['direction']} "
                f"错误码={order_error.error_id}"
            )

    def on_disconnected(self):
        logger.warning("⚠️ [UnderdogCallback] QMT 连接断开！")
        send_webhook("⚠️ Underdog QMT 断线", "QMT 连接断开，请检查 miniQMT 进程状态")

    def on_connected(self):
        logger.info("✅ [UnderdogCallback] QMT 连接恢复。")

# ==============================================================================
# ── 发单辅助（统一标识 + pending 注册 — 铁律一/四）
# ==============================================================================
def _is_pending_for(code: str, direction: str) -> bool:
    """检查该标的同方向是否已有委托在途（防重复发单）。"""
    with _pending_lock:
        for meta in _pending.values():
            if meta.get("code") == code and meta.get("direction") == direction:
                return True
    return False


def _place_market_order(
    xt_trader: XtQuantTrader,
    acc: StockAccount,
    code: str,
    direction: str,
    qty: int,
    price: float,
    remark: str,
) -> int:
    """
    以市价单（LATEST_PRICE）下单，注册 pending。
    返回 seq（> 0 成功，≤ 0 失败）。
    """
    if qty <= 0:
        logger.warning(f"[下单] {code} {direction} qty=0，跳过")
        return -1

    side = xtconstant.STOCK_BUY if direction == "buy" else xtconstant.STOCK_SELL

    try:
        seq = xt_trader.order_stock(
            acc, code, side, qty,
            xtconstant.LATEST_PRICE, round(price, 3),
            strategy_name=STRATEGY_NAME,
            order_remark=remark,
        )
    except Exception as e:
        logger.error(f"[下单异常] {code} {direction} qty={qty}: {e}")
        return -1

    if seq and seq > 0:
        with _pending_lock:
            _pending[seq] = {
                "code":         code,
                "direction":    direction,
                "qty":          qty,
                "remark":       remark,
                "price":        price,
                "sent_at":      time.time(),
                "filled_so_far": 0,
            }
        logger.info(
            f"📤 [下单] {code} {direction} {qty}股 @ ~{price:.3f} "
            f"remark={remark} seq={seq}"
        )
    else:
        logger.error(
            f"❌ [下单失败] {code} {direction} {qty}股 seq={seq}"
        )
        seq = -1

    return seq


def _sweep_stale_pending(xt_trader: XtQuantTrader, acc: StockAccount) -> None:
    """超时巡检：pending 超过 PENDING_TIMEOUT 秒的委托，物理查成交补账后移出。"""
    now = time.time()
    with _pending_lock:
        stale = {
            seq: m for seq, m in _pending.items()
            if now - m["sent_at"] > PENDING_TIMEOUT
        }
    for seq, meta in stale.items():
        logger.warning(
            f"⏰ [Sweep] {meta['code']} 委托超时 {PENDING_TIMEOUT}s，物理补账..."
        )
        try:
            trades = xt_trader.query_stock_trades(acc) or []
            filled = sum(
                int(t.traded_volume)
                for t in trades
                if t.order_id == seq
            )
            if filled > 0:
                logger.info(f"  ✅ [Sweep] 实盘确认 {filled}股，补写账本")
                class _FT:
                    order_id      = seq
                    traded_volume = filled
                    traded_price  = meta.get("price", 0)
                UnderdogCallback().on_stock_trade(_FT())
            else:
                logger.warning("  ⚠️ [Sweep] 无成交记录，视为废单")
        except Exception as e:
            logger.error(f"  ❌ [Sweep] 查询异常: {e}")
        finally:
            with _pending_lock:
                _pending.pop(seq, None)


# ==============================================================================
# ── 核心任务一：三维共振扫描器 (The Ignition Scanner)
# ==============================================================================
def scan_underdogs(pool: list) -> list:
    """
    盘后/盘中扫描，从 pool（全市场 ETF code list）中筛选同时满足三维条件的标的。

    条件一（跌幅极值）：当前价 < 过去 250 日最低价序列的 10% 分位
    条件二（放量点火）：最新涨幅 > 3% 且 当日量 > 20 日均量 × 2
    条件三（结构突破）：最新收盘价 > 过去 10 日最高价

    Returns:
        list of dict — 符合条件的标的信息列表，每项包含:
            code, current_price, ignition_low, pct_change, volume_ratio
    """
    if not pool:
        logger.warning("[Ignition Scanner] ETF 池为空，跳过扫描")
        return []

    logger.info(f"[Ignition Scanner] 开始扫描，共 {len(pool)} 只标的...")
    results = []

    # ── 批量拉取日线数据（减少 API 调用次数）───────────────────────────────
    # 需要字段：close, high, low, volume
    try:
        market_data = xtdata.get_market_data_ex(
            field_list=["close", "high", "low", "volume"],
            stock_list=pool,
            period="1d",
            count=LOOKBACK_DAYS + 5,   # 多取 5 根作为缓冲
        )
    except Exception as e:
        logger.error(f"[Ignition Scanner] 批量拉取日线异常: {e}")
        return []

    # ── 批量拉取实时 Tick（获取最新价和当日量）─────────────────────────────
    try:
        full_tick = xtdata.get_full_tick(pool)
    except Exception as e:
        logger.error(f"[Ignition Scanner] 批量拉取 Tick 异常: {e}")
        full_tick = {}

    for code in pool:
        try:
            # ── 数据安全访问（quant-safe-patterns §2.1）────────────────────
            if code not in market_data or market_data[code] is None or market_data[code].empty:
                continue

            df = market_data[code][["close", "high", "low", "volume"]].dropna()

            if len(df) < LOOKBACK_DAYS:
                continue  # 数据不足，跳过

            close_arr  = df["close"].values
            high_arr   = df["high"].values
            low_arr    = df["low"].values
            volume_arr = df["volume"].values

            # ── 获取实时最新价 & 当日数据 ──────────────────────────────────
            tick = full_tick.get(code, {}) or {}
            current_price = float(tick.get("lastPrice", 0) or 0)
            if current_price <= 0:
                current_price = float(close_arr[-1])  # fallback 到昨收

            prev_close = float(
                tick.get("lastClose",
                tick.get("preClose", close_arr[-2] if len(close_arr) >= 2 else current_price))
            )

            today_volume = float(tick.get("volume", 0) or 0)
            if today_volume <= 0:
                today_volume = float(volume_arr[-1])  # fallback 到日线最后一根

            # ── 条件一：跌幅极值（10% 分位筛选器）─────────────────────────
            # 用过去 250 根日线的最低价序列，取 10% 分位
            low_series  = low_arr[-LOOKBACK_DAYS:]          # 最近 250 根 low
            low_10pct   = float(np.percentile(low_series, LOW_PERCENTILE))

            if current_price >= low_10pct:
                continue  # 不在地下室，不符合

            # ── 条件二：放量点火（涨幅 > 3% + 量比 > 2×）──────────────────
            if prev_close <= 0:
                continue

            pct_change = (current_price - prev_close) / prev_close

            if pct_change <= IGNITION_GAIN:
                continue  # 涨幅不足

            vol_20_avg = float(np.mean(volume_arr[-21:-1]))  # 前 20 日均量（不含今日）
            if vol_20_avg <= 0:
                continue

            volume_ratio = today_volume / vol_20_avg
            if volume_ratio <= VOLUME_MULTIPLIER:
                continue  # 放量不足

            # ── 条件三：结构突破（今日收盘 > 过去 10 日最高价）────────────
            # 用最新价近似当日收盘（盘中扫描）
            high_10d     = float(np.max(high_arr[-BREAKOUT_WINDOW - 1:-1]))  # 过去 10 根（不含今日）
            if current_price <= high_10d:
                continue  # 未突破前高，不符合

            # ── 三维共振成立，提取点火蜡烛的最低价（用于防线 A）──────────
            # 「点火蜡烛」= 今日K线，ignition_low 取 tick 中当日最低价
            ignition_low = float(tick.get("low", current_price) or current_price)
            # 保守：若 tick 低价异常为 0，fallback 到当日收盘价
            if ignition_low <= 0:
                ignition_low = current_price

            results.append({
                "code":          code,
                "current_price": round(current_price, 3),
                "ignition_low":  round(ignition_low, 3),
                "pct_change":    round(pct_change, 4),
                "volume_ratio":  round(volume_ratio, 2),
                "low_10pct":     round(low_10pct, 3),
                "high_10d":      round(high_10d, 3),
            })

            logger.info(
                f"🔥 [Ignition] {code} | "
                f"现价={current_price:.3f} | 10%分位={low_10pct:.3f} | "
                f"涨幅={pct_change*100:.2f}% | 量比={volume_ratio:.1f}x | "
                f"突破前高={high_10d:.3f}"
            )

        except Exception as e:
            logger.warning(f"[Ignition Scanner] {code} 处理异常，跳过: {e}")
            continue

    logger.info(
        f"[Ignition Scanner] 扫描完毕，{len(pool)} 只中发现 {len(results)} 只三维共振标的"
    )
    return results


# ==============================================================================
# ── 核心任务二：四维退出矩阵 (The Exit Matrix)
# ==============================================================================
def patrol_positions(xt_trader: XtQuantTrader, acc: StockAccount) -> None:
    """
    持仓巡逻：遍历账本中 status=FULL/HALF 的持仓，依次触发四道防线。
    触碰任意一条立刻市价全平（或半仓），并更新账本。

    防线 A (结构断头台)：现价 < ignition_low → 点火失败，市价全平
    防线 B (时间腐烂)  ：hold_days >= 3 且 现价 <= entry_price × 1.005 → 市价全平
    防线 C (均值回归)  ：status=FULL 且 现价 >= MA20 → 市价卖出 50%，status→HALF
    防线 D (动能衰竭)  ：status=HALF，现价 < highest_price × 0.98 → 清掉剩余半仓
    """
    ledger = load_ledger()
    if not ledger:
        logger.info("[Patrol] 账本为空，空仓静候")
        return

    # 收集需要巡逻的持仓列表（FULL 或 HALF）
    active_codes = [
        code for code, slot in ledger.items()
        if slot.get("status") in ("FULL", "HALF")
    ]
    if not active_codes:
        logger.info("[Patrol] 无活跃持仓，空仓静候")
        return

    logger.info(f"[Patrol] 开始巡逻，活跃持仓: {active_codes}")

    # ── 批量拉取日线（用于计算 MA20）────────────────────────────────────────
    try:
        hist_data = xtdata.get_market_data_ex(
            field_list=["close"],
            stock_list=active_codes,
            period="1d",
            count=22,   # 需要 21 根算 MA20
        )
    except Exception as e:
        logger.error(f"[Patrol] 批量拉取日线异常: {e}，跳过本轮巡逻")
        return

    # ── 批量拉取实时 Tick ─────────────────────────────────────────────────────
    try:
        full_tick = xtdata.get_full_tick(active_codes)
    except Exception as e:
        logger.error(f"[Patrol] 批量拉取 Tick 异常: {e}，跳过本轮巡逻")
        return

    for code in active_codes:
        try:
            slot = ledger.get(code, {})
            status      = slot.get("status", "")
            entry_price = float(slot.get("entry_price", 0))
            ignition_low= float(slot.get("ignition_low", 0))
            highest_price = float(slot.get("highest_price", entry_price))
            buy_date    = slot.get("buy_date", _today_str())
            bought_qty  = int(slot.get("bought_qty", 0))

            if status not in ("FULL", "HALF") or entry_price <= 0:
                continue

            # ── 获取现价 ─────────────────────────────────────────────────────
            tick = full_tick.get(code, {}) or {}
            current_price = float(tick.get("lastPrice", 0) or 0)
            if current_price <= 0:
                logger.warning(f"[Patrol] {code} Tick 现价为 0，跳过本标的")
                continue

            # ── 更新 hold_days（每次巡逻刷新）───────────────────────────────
            hold_days = _days_since(buy_date)
            ledger[code]["hold_days"]     = hold_days
            ledger[code]["highest_price"] = max(highest_price, current_price)
            highest_price = ledger[code]["highest_price"]

            # ── 计算 MA20 ─────────────────────────────────────────────────────
            ma20 = 0.0
            try:
                if code in hist_data and hist_data[code] is not None and not hist_data[code].empty:
                    close_ser = hist_data[code]["close"].dropna()
                    if len(close_ser) >= 20:
                        ma20 = float(close_ser.values[-20:].mean())
            except Exception as _e:
                logger.warning(f"[Patrol] {code} MA20 计算失败: {_e}")

            # ── 防线优先级：A → B → C → D（触碰任意一条立即处理，跳过后续）──

            # ═══════════════════════════════════════════════════════════════
            # 防线 A：结构断头台（ignition_low 跌破 → 点火失败）
            # ═══════════════════════════════════════════════════════════════
            if ignition_low > 0 and current_price < ignition_low:
                logger.warning(
                    f"\033[91m💥 [防线A·断头台] {code} | "
                    f"现价={current_price:.3f} < ignition_low={ignition_low:.3f} | "
                    f"点火失败，市价全平！\033[0m"
                )
                send_webhook(
                    "💥 Underdog 防线A 断头台触发",
                    f"[{code}] 现价={current_price:.3f} < ignition_low={ignition_low:.3f}\n"
                    f"点火失败，市价全平，记入亏损。"
                )
                if _is_pending_for(code, "sell"):
                    logger.warning(f"[防线A] {code} 已有卖单在途，跳过重复发单")
                    continue
                # 铁律三：全仓量从账本 bought_qty 读取
                _place_market_order(
                    xt_trader, acc, code, "sell",
                    bought_qty, current_price, REMARK_SELL_A
                )
                continue  # 防线A 已触发，不再检查 B/C/D

            # ═══════════════════════════════════════════════════════════════
            # 防线 B：时间腐烂（hold_days >= 3 且未脱离成本区）
            # ═══════════════════════════════════════════════════════════════
            decay_threshold = entry_price * (1.0 + DECAY_PROFIT_MAX)
            if hold_days >= HOLD_DAYS_DECAY and current_price <= decay_threshold:
                logger.warning(
                    f"\033[91m⏳ [防线B·时间腐烂] {code} | "
                    f"持有 {hold_days} 天，现价={current_price:.3f} ≤ 成本区={decay_threshold:.3f} | "
                    f"市价全平，回收资金！\033[0m"
                )
                send_webhook(
                    "⏳ Underdog 防线B 时间腐烂",
                    f"[{code}] 持有 {hold_days} 天\n"
                    f"现价={current_price:.3f} ≤ 成本区{DECAY_PROFIT_MAX*100:.1f}%={decay_threshold:.3f}\n"
                    f"市价全平，回收资金。"
                )
                if _is_pending_for(code, "sell"):
                    logger.warning(f"[防线B] {code} 已有卖单在途，跳过")
                    continue
                _place_market_order(
                    xt_trader, acc, code, "sell",
                    bought_qty, current_price, REMARK_SELL_B
                )
                continue  # 防线B 触发，不再检查 C/D

            # ═══════════════════════════════════════════════════════════════
            # 防线 C：均值回归减仓（FULL → 现价 >= MA20 → 卖出 50%）
            # ═══════════════════════════════════════════════════════════════
            if status == "FULL" and ma20 > 0 and current_price >= ma20:
                sell_half = int(bought_qty // 2 // 100) * 100  # 50%，向下取整到百股
                if sell_half <= 0:
                    # 持仓量太小无法整手卖出，直接全平
                    sell_half = bought_qty
                logger.info(
                    f"\033[92m💰 [防线C·均值回归] {code} | "
                    f"现价={current_price:.3f} ≥ MA20={ma20:.3f} | "
                    f"市价卖出 {sell_half} 股（50%），status→HALF\033[0m"
                )
                send_webhook(
                    "💰 Underdog 防线C 均值回归减仓",
                    f"[{code}] 现价={current_price:.3f} ≥ MA20={ma20:.3f}\n"
                    f"市价卖出 50%（{sell_half}股），剩余持仓等待防线D。"
                )
                if _is_pending_for(code, "sell"):
                    logger.warning(f"[防线C] {code} 已有卖单在途，跳过")
                    continue
                seq_c = _place_market_order(
                    xt_trader, acc, code, "sell",
                    sell_half, current_price, REMARK_SELL_C
                )
                if seq_c > 0:
                    # 乐观预写 status（回调会用 bought_qty 精确更新）
                    ledger[code]["status"] = "HALF"
                    save_ledger(ledger)
                continue  # 防线C 触发，不再检查 D（等下一轮巡逻处理 HALF）

            # ═══════════════════════════════════════════════════════════════
            # 防线 D：动能衰竭移动止盈（HALF → 高点回撤 2% → 清掉剩余半仓）
            # ═══════════════════════════════════════════════════════════════
            if status == "HALF":
                trailing_stop = highest_price * (1.0 - HALF_DRAWDOWN)
                if current_price < trailing_stop:
                    logger.info(
                        f"\033[92m🏁 [防线D·动能衰竭] {code} | "
                        f"现价={current_price:.3f} < 高点回撤止盈={trailing_stop:.3f} "
                        f"（highest={highest_price:.3f}）| 清掉剩余半仓！\033[0m"
                    )
                    send_webhook(
                        "🏁 Underdog 防线D 动能衰竭止盈",
                        f"[{code}] 现价={current_price:.3f}\n"
                        f"高点={highest_price:.3f} | 回撤止盈线={trailing_stop:.3f}\n"
                        f"清掉剩余半仓（{bought_qty}股），结束本次猎杀。"
                    )
                    if _is_pending_for(code, "sell"):
                        logger.warning(f"[防线D] {code} 已有卖单在途，跳过")
                        continue
                    _place_market_order(
                        xt_trader, acc, code, "sell",
                        bought_qty, current_price, REMARK_SELL_D
                    )
                else:
                    logger.debug(
                        f"[防线D] {code} HALF持仓中 | "
                        f"现价={current_price:.3f} | 高点={highest_price:.3f} | "
                        f"止盈线={trailing_stop:.3f} | 未触发，继续持有"
                    )

        except Exception as e:
            logger.error(f"[Patrol] {code} 巡逻异常，跳过: {e}")
            continue

    # ── 刷新所有 highest_price 并持久化 ────────────────────────────────────
    save_ledger(ledger)
    logger.info("[Patrol] 本轮巡逻完成")


# ==============================================================================
# ── 买入执行器：消耗 FUNDS_LIMIT 开仓（单持仓策略）
# ==============================================================================
def execute_buys(
    candidates: list,
    xt_trader: XtQuantTrader,
    acc: StockAccount,
) -> None:
    """
    对扫描到的三维共振标的执行买入。
    规则：
    - 同一时刻账本只允许 1 个活跃持仓（FULL 或 HALF）
    - 资金固定 FUNDS_LIMIT = 50,000，禁止动态缩放
    - 预写账本 ignition_low / highest_price，bought_qty 由回调填写
    """
    if not candidates:
        return

    ledger = load_ledger()

    # ── 仓位熔断：已有活跃持仓则不开新仓 ─────────────────────────────────────
    active = [
        code for code, slot in ledger.items()
        if slot.get("status") in ("FULL", "HALF", "PENDING_BUY")
    ]
    if active:
        logger.info(f"[ExecuteBuy] 当前已有持仓 {active}，跳过所有新候选")
        return

    for candidate in candidates:
        code          = candidate["code"]
        current_price = candidate["current_price"]
        ignition_low  = candidate["ignition_low"]

        # 今日已操作过该标的（含 CLOSED）则跳过，防止当日重复买入
        if code in ledger:
            logger.info(
                f"[ExecuteBuy] {code} 已存在账本（{ledger[code].get('status')}），跳过"
            )
            continue

        if _is_pending_for(code, "buy"):
            logger.warning(f"[ExecuteBuy] {code} 已有买单在途，跳过")
            continue

        qty = _calc_qty(current_price, FUNDS_LIMIT)
        if qty <= 0:
            logger.warning(
                f"[ExecuteBuy] {code} 价格={current_price:.3f} 导致 qty=0，跳过"
            )
            continue

        logger.info(
            f"🐕 [ExecuteBuy] 开仓 {code} | "
            f"价={current_price:.3f} | qty={qty} | "
            f"资金≈{qty*current_price:,.0f} | ignition_low={ignition_low:.3f}"
        )

        # ── 预写账本占位（ignition_low/highest_price），回调再填 bought_qty ──
        ledger[code] = {
            "status":        "PENDING_BUY",
            "ignition_low":  ignition_low,
            "highest_price": current_price,
            "entry_price":   current_price,
            "bought_qty":    0,
            "buy_date":      _today_str(),
            "hold_days":     0,
        }
        save_ledger(ledger)

        seq = _place_market_order(
            xt_trader, acc, code, "buy",
            qty, current_price, REMARK_BUY
        )

        if seq > 0:
            send_webhook(
                "🐕 Underdog 开仓信号",
                f"[{code}] 三维共振触发！\n"
                f"现价={current_price:.3f} | qty={qty}股 | "
                f"资金≈{qty*current_price:,.0f}元\n"
                f"ignition_low={ignition_low:.3f} | "
                f"涨幅={candidate['pct_change']*100:.2f}% | "
                f"量比={candidate['volume_ratio']:.1f}x"
            )
            break   # 单持仓：买入成功后不再处理后续候选
        else:
            # 买单失败，清除预写占位
            ledger.pop(code, None)
            save_ledger(ledger)
            logger.error(f"[ExecuteBuy] {code} 买单失败，已清除预写账本")


# ==============================================================================
# ── QMT 连接初始化
# ==============================================================================
def _init_qmt() -> tuple:
    """初始化 XtQuantTrader，返回 (xt_trader, acc)；失败返回 (None, None)。"""
    if not QMT_PATH or not ACCOUNT_ID:
        logger.error("[Init] QMT_PATH 或 ACCOUNT_ID 未配置，请检查 .env 文件")
        return None, None
    try:
        acc       = StockAccount(ACCOUNT_ID)
        xt_trader = XtQuantTrader(QMT_PATH, int(time.time()) % 10000)
        callback  = UnderdogCallback()
        xt_trader.register_callback(callback)
        xt_trader.start()
        result = xt_trader.connect()
        if result != 0:
            logger.error(f"[Init] connect 失败，result={result}")
            return None, None
        sub = xt_trader.subscribe(acc)
        if sub != 0:
            logger.error(f"[Init] subscribe 失败，result={sub}")
            return None, None
        logger.info(f"✅ [Init] QMT 连接成功，账户={ACCOUNT_ID}")
        return xt_trader, acc
    except Exception as e:
        logger.error(f"[Init] QMT 初始化异常: {e}")
        return None, None


# ==============================================================================
# ── 主循环（分裂作息）
#
# 作息设计：
#   Patrol  09:30~14:55  每 30s 轮询一次四维退出矩阵（手起刀落）
#   Scanner 14:45~14:55  每日唤醒一次三维共振扫描（尾盘量价已定型）
# ==============================================================================
def main():
    logger.info("=" * 60)
    logger.info("🐕 Underdog Executor 启动（落水狗左侧抄底）")
    logger.info(f"   资金限额  : {FUNDS_LIMIT:,.0f} 元")
    logger.info(f"   账本文件  : {LEDGER_FILE}")
    logger.info(f"   Patrol 窗口: {PATROL_OPEN} ~ {PATROL_CLOSE}  每 {PATROL_INTERVAL}s")
    logger.info(f"   Scanner 窗口: {SCANNER_OPEN} ~ {SCANNER_CLOSE}  每日唤醒一次")
    logger.info("=" * 60)

    xt_trader, acc = _init_qmt()
    if xt_trader is None:
        logger.critical("[Main] QMT 初始化失败，进程退出")
        return

    # ── 每日扫描状态：记录已完成扫描的日期，防止同一天重复扫描 ────────────────
    _scan_done_date: str = ""   # 格式 YYYYMMDD

    while True:
        try:
            now      = datetime.now()
            now_hhmm = now.strftime("%H:%M")
            now_str  = now.strftime("%H:%M:%S")
            today    = now.strftime("%Y%m%d")

            # ────────────────────────────────────────────────────────────────
            # 阵地巡逻（Patrol）：09:30 ~ 14:55，每 30s 一次
            # ────────────────────────────────────────────────────────────────
            if _is_patrol_time():
                # Step 1：pending 超时巡检（每轮都跑）
                try:
                    _sweep_stale_pending(xt_trader, acc)
                except Exception as e:
                    logger.error(f"[Patrol] pending 巡检异常: {e}")

                # Step 2：四维退出矩阵（手起刀落）
                try:
                    patrol_positions(xt_trader, acc)
                except Exception as e:
                    logger.error(f"[Patrol] patrol_positions 异常: {e}")

            # ────────────────────────────────────────────────────────────────
            # 雷达扫描（Scanner）：14:45 ~ 14:55，当日只唤醒一次
            # ────────────────────────────────────────────────────────────────
            if _is_scanner_window() and _scan_done_date != today:
                logger.info(
                    f"🔭 [Scanner] 尾盘扫描窗口开启 @ {now_str}，"
                    f"量价结构已定型，启动三维共振扫描..."
                )
                try:
                    pool       = get_etf_pool()
                    candidates = scan_underdogs(pool)
                    if candidates:
                        execute_buys(candidates, xt_trader, acc)
                        send_webhook(
                            "🔭 Underdog 尾盘扫描完毕",
                            f"发现 {len(candidates)} 只三维共振标的，"
                            f"已触发开仓逻辑。首选: {candidates[0]['code']}"
                        )
                    else:
                        logger.info("[Scanner] 本日无三维共振标的，空仓等待")
                    # 无论是否成功，今日只扫描一次
                    _scan_done_date = today
                    logger.info(f"[Scanner] 今日扫描完毕，标记 _scan_done_date={today}")

                except Exception as e:
                    logger.error(f"[Scanner] 扫描异常: {e}")
                    # 异常时不标记 done，允许窗口内重试

            # ────────────────────────────────────────────────────────────────
            # 非活跃时段：静默等待，打 INFO 心跳（每 30s 一次）
            # ────────────────────────────────────────────────────────────────
            if not _is_patrol_time() and not _is_scanner_window():
                logger.info(f"[Main] {now_str} 静默心跳（Patrol {PATROL_OPEN}~{PATROL_CLOSE} / Scanner {SCANNER_OPEN}~{SCANNER_CLOSE}）")

        except KeyboardInterrupt:
            logger.info("[Main] 用户中断，退出")
            break
        except Exception as e:
            logger.error(f"[Main] 主循环未知异常: {e}")

        time.sleep(PATROL_INTERVAL)

    logger.info("[Main] Underdog Executor 已停止")


if __name__ == "__main__":
    main()
