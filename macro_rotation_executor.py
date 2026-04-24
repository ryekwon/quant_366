# -*- coding: utf-8 -*-
# ==============================================================================
# 🌍 macro_rotation_executor.py  V4 Titan (夏普动量重铸版)
#    执行时机：每周五 14:42 (由 autopilot_master 调度)
#    核心职责：风险调整后动量排名（夏普式 Scoring），双槽位（各 5 万）宏观轮动
#
#    四大绝对护盾：
#      1. 原子级 JSON 账本读写 (防 0 字节断电损坏)
#      2. 强制物理查仓 (防幽灵持仓)
#      3. 负动量强制切防守（Return_20D < 0 → 511260.SH）
#      4. 严格同步先卖后买 (防资金不足卡单)
#
#    [2026-04-22] Oracle/Odds_Ratio/Z-Score 全线废弃
#    进攻引擎：Score = 20日收益率 / 20日年化波动率（截面夏普近似）
#    防守机制：见 macro_risk_monitor.py（移动止盈 + MA20 均线防守）
# ==============================================================================

import os, sys, time, json, datetime, logging, math, threading
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
load_dotenv()

# ── xtquant 环境
from xtquant import xtdata, xtconstant
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount          # ✅ StockAccount 必须从 xttype 导入

# ==============================================================================
# 📌 全局配置
# ==============================================================================
QMT_PATH      = os.getenv("QMT_PATH", r"C:\国金证券QMT交易端\userdata_mini")  # 从 .env 读取
ACCOUNT_ID    = os.getenv("ACCOUNT_ID", "")                                     # 从 .env 读取，禁止硬编码
STATE_DIR     = ".state"
SLOTS_FILE    = os.path.join(STATE_DIR, "macro_slots.json")

N8N_WEBHOOK   = os.getenv("N8N_WEBHOOK_URL")   # N8N 推送

# 策略参数
SLOT_COUNT     = 2          # 双槽位
DEFENSE_ETF    = "511260.SH" # 国债 ETF（防守锚）

# Regime 驱动资金缩放（沪深300 MA20 判定）
FUNDS_UPTREND   = 75000.0   # 牛市满配：现价 > MA20
FUNDS_DOWNTREND = 20000.0   # 熊市缩仓：现价 ≤ MA20
FUNDS_PER_SLOT  = FUNDS_UPTREND  # 模块级默认（execute_rotation 中覆盖）

BENCHMARK_CODE = "510300.SH"   # 沪深300 ETF，MA20 Regime 判定基准

# 宏观资产全量池（与 macro_risk_monitor.py 保持严格同步）
MACRO_POOL = [
    "510300.SH",   # 沪深300 ETF
    "513500.SH",   # 标普500 ETF
    "512890.SH",   # 红利防守 ETF
    "518880.SH",   # 黄金 ETF
    "511260.SH",   # 国债 ETF（DEFENSE_ETF）
    "513100.SH",   # 纳斯达克科技 ETF
    "513050.SH",   # 中概互联 ETF
    "159915.SZ",   # 创业板 ETF
    "588000.SH",   # 科创50 ETF
    "563300.SH",   # 中证2000 ETF
]

# ==============================================================================
# 📋 独立 Logger
# ==============================================================================
os.makedirs("logs", exist_ok=True)
_LOG_FILE = f"logs/{datetime.date.today():%Y%m%d}_macro_rotation.log"
_logger = logging.getLogger("macro_v4")
_logger.setLevel(logging.INFO)
if not _logger.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    _fh  = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    _fh.setFormatter(_fmt)
    _sh  = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    _logger.addHandler(_fh)
    _logger.addHandler(_sh)
_logger.propagate = False

_R = "\033[91m"; _G = "\033[92m"; _Y = "\033[93m"; _E = "\033[0m"

# ==============================================================================
# 🔐 Fill-Based Pending 注册表（线程安全）
# ==============================================================================
_macro_pending: dict = {}          # {order_id → meta dict}
_macro_pending_lock = threading.Lock()  # 保护 _macro_pending 读写
_macro_slots_lock   = threading.Lock()  # 保护 macro_slots.json 并发写入（IO 锁）


class MacroCallback(XtQuantTraderCallback):
    """Macro Rotation Fill-Based 成交回调。
    on_order_trade 触发时原子写入 macro_slots.json，支持分笔累加。
    """

    def on_disconnected(self):
        _send_webhook("🚨 MacroRotation", "QMT 交易网关已断开！")

    def on_order_trade(self, trade):
        """成交回调：原子更新 macro_slots.json 中的 shares / cost / hwm。"""
        filled_qty = trade.traded_volume
        filled_px  = trade.traded_price
        code       = trade.stock_code

        with _macro_pending_lock:
            meta = _macro_pending.get(trade.order_id)

        if meta is None:
            # 非本引擎委托，透传日志即可
            _logger.info(f"[MacroCB·过路] {code} 价={filled_px:.4f} qty={filled_qty}")
            return

        slot      = meta["slot"]        # "slot_a" or "slot_b"
        direction = meta["direction"]   # "buy" or "sell"
        target_qty = meta.get("target_qty", 0)

        # ── 累加已成交量，完全成交才从 pending 移除
        with _macro_pending_lock:
            m = _macro_pending.get(trade.order_id)
            if m:
                m["filled_so_far"] = m.get("filled_so_far", 0) + filled_qty
                filled_so_far = m["filled_so_far"]
                if target_qty > 0 and filled_so_far >= target_qty:
                    _macro_pending.pop(trade.order_id, None)
            else:
                filled_so_far = filled_qty

        # ── 原子写账本（IO 锁防并发回调竞态写）
        with _macro_slots_lock:
            slots = _load_slots()

            if direction == "buy":
                old_shares = slots.get(f"{slot}_shares", 0)
                old_cost   = slots.get(f"{slot}_cost",   filled_px)
                new_shares = old_shares + filled_qty
                # 加权均价
                if new_shares > 0:
                    new_cost = round(
                        (old_cost * old_shares + filled_px * filled_qty) / new_shares, 4
                    ) if old_shares > 0 else round(float(filled_px), 4)
                else:
                    new_cost = round(float(filled_px), 4)

                slots[f"{slot}_shares"] = new_shares
                slots[f"{slot}_cost"]   = new_cost
                # HWM：首次买入时初始化，此后只升不降
                old_hwm = slots.get(f"{slot}_hwm", 0.0) or 0.0
                if old_hwm <= 0:
                    slots[f"{slot}_hwm"] = round(float(filled_px), 4)
                # code / capital 由 execute_rotation 预先写入，不覆盖

            elif direction == "sell":
                old_shares = slots.get(f"{slot}_shares", 0)
                slots[f"{slot}_shares"] = max(0, old_shares - filled_qty)
                if slots[f"{slot}_shares"] == 0:
                    slots[f"{slot}"]         = None
                    slots[f"{slot}_hwm"]     = None
                    slots[f"{slot}_cost"]    = 0.0
                    slots[f"{slot}_capital"] = 0.0

            slots["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _save_slots(slots)  # 持锁期间完成原子落盘

        action = "买入成交" if direction == "buy" else "卖出成交"
        _logger.info(
            f"[MacroCB·Fill] {action} {slot} {code} | "
            f"价={filled_px:.4f} qty={filled_qty} | "
            f"累计={filled_so_far}/{target_qty} | "
            f"shares={slots.get(f'{slot}_shares',0)} cost={slots.get(f'{slot}_cost',0):.4f}"
        )
        _send_webhook(
            f"{'✅ 买入' if direction=='buy' else '📤 卖出'} MacroRotation {code}",
            f"{slot.upper()} {code} | 成交价: {filled_px:.4f} | 数量: {filled_qty}\n"
            f"累计: {filled_so_far}/{target_qty} | "
            f"总股数: {slots.get(f'{slot}_shares',0)} | 均价: {slots.get(f'{slot}_cost',0):.4f}"
        )

    def on_order_error(self, order_error):
        msg = f"❌ [{order_error.stock_code}] 下单失败: {order_error.error_msg}"
        _logger.error(msg)
        _send_webhook("🚨 MacroRotation 下单失败", msg)

    def on_order_stock(self, order):
        _logger.info(f"📝 [报单确认] {order.stock_code} | {order.order_remark} | 价={order.price}")



def _send_webhook(title: str, message: str):
    """N8N 推送（失败静默吃掉）。"""
    if not N8N_WEBHOOK:
        return
    try:
        requests.post(
            N8N_WEBHOOK,
            json={"title": title, "message": message,
                  "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            timeout=8,
        )
    except Exception:
        pass

# ==============================================================================
# 🛡️ 护盾 1：原子级状态机 I/O
# ==============================================================================
def _load_slots() -> dict:
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(SLOTS_FILE):
        return {"slot_a": None, "slot_b": None}
    try:
        with open(SLOTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _logger.error(f"{_R}读取状态文件异常，返回空槽位: {e}{_E}")
        return {"slot_a": None, "slot_b": None}

def _save_slots(slots: dict):
    tmp_file = SLOTS_FILE + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(slots, f, ensure_ascii=False, indent=4)
        os.replace(tmp_file, SLOTS_FILE)  # 原子替换！
        _logger.debug("✅ 状态机已原子保存")
    except Exception as e:
        _logger.error(f"{_R}致命错误：原子写入失败: {e}{_E}")

# ==============================================================================
# 📐  动量引擎：风险调整后动量评分（夏普式 Scoring）
# ==============================================================================

def _build_momentum_df(codes: list) -> pd.DataFrame:
    """
    从 QMT 本地缓存拉取近 25 根日线 close，
    计算风险调整后动量（截面夏普近似）：
      Return_20D = (P_last - P_{-20}) / P_{-20}
      Ann_Vol    = std(daily_returns[-20:]) * sqrt(252)
      Score      = Return_20D / Ann_Vol

    防御规则：
      - 数据不足 21 根 → 跳过
      - Ann_Vol < 1e-8 → 跳过（防零除）
      - Return_20D < 0 → Score 保留负值（供 calculate_dynamic_targets 强制换防守）

    返回 DataFrame(Code, Return_20D, Ann_Vol, Score)，数据不足的标的自动剔除。
    无未来函数：count=25 只取到当前最新 bar，纯读本地 O(N) 计算。
    """
    try:
        raw = xtdata.get_market_data_ex(
            field_list=["close"],
            stock_list=codes,
            period="1d",
            count=25,    # 取 25 根：保证至少 21 根用于动量 + 20 根用于波动率
        )
    except Exception as e:
        _logger.error(f"❌ [动量构建] get_market_data_ex 异常: {e}")
        return pd.DataFrame(columns=["Code", "Return_20D", "Ann_Vol", "Score"])

    rows = []
    for code in codes:
        df = raw.get(code)
        if df is None or df.empty:
            continue
        closes = df["close"].dropna().values  # numpy array

        if len(closes) < 21 or closes[-21] == 0:
            _logger.debug(f"  [动量构建] {code} 数据不足/锚点为0，跳过")
            continue

        # 20日收益率
        ret_20d = (closes[-1] - closes[-21]) / closes[-21]

        # 20日日收益率序列 → 年化波动率
        price_window = closes[-21:]                          # 21根 → 20个日收益率
        daily_rets   = np.diff(price_window) / price_window[:-1]
        ann_vol      = float(np.std(daily_rets) * np.sqrt(252))

        if ann_vol < 1e-8:
            _logger.debug(f"  [动量构建] {code} 年化波动率≈0，跳过")
            continue

        score = ret_20d / ann_vol

        rows.append({
            "Code":       code,
            "Return_20D": round(float(ret_20d), 6),
            "Ann_Vol":    round(ann_vol, 6),
            "Score":      round(float(score), 6),
        })

    df_out = pd.DataFrame(rows)

    if df_out.empty:
        _logger.warning(f"{_Y}  ⚠️ [动量构建] 所有标的均无有效数据！{_E}")
        return df_out

    df_sorted = df_out.sort_values("Score", ascending=False).reset_index(drop=True)
    _logger.info(
        f"  📈 [动量构建] 有效标的={len(df_out)}/{len(codes)} | "
        f"Top3 Score: {df_sorted[['Code','Return_20D','Score']].head(3).to_dict('records')}"
    )
    return df_sorted


# ==============================================================================
# 📐  基准 Regime 判定（沪深300 MA20，决定资金缩放）
# ==============================================================================

def _get_benchmark_stats() -> tuple:
    """
    获取基准指数（510300.SH 沪深300 ETF）当前价格和 MA20。
    MA20 = 本地缓存最近 20 根日线 close 均值（纯读本地，0 网络请求）。
    当前价使用 get_full_tick 动态拼接（静态底座 + 动态缝合）。
    返回 (benchmark_price: float, benchmark_ma20: float)，失败时返回 (0.0, 0.0)。
    """
    try:
        raw = xtdata.get_market_data_ex(
            field_list=["close"],
            stock_list=[BENCHMARK_CODE],
            period="1d",
            count=20,
        )
        df = raw.get(BENCHMARK_CODE)
        if df is None or df.empty or len(df) < 20:
            _logger.warning(f"  ⚠️ [基准] {BENCHMARK_CODE} 数据不足 20 根")
            return 0.0, 0.0

        closes = df["close"].dropna().tolist()
        ma20   = sum(closes[-20:]) / 20

        # 动态缝合：拉取实时 lastPrice 作为最新价格
        tick       = xtdata.get_full_tick([BENCHMARK_CODE]).get(BENCHMARK_CODE, {})
        last_price = float((tick or {}).get("lastPrice", 0.0))
        if last_price <= 0:
            last_price = closes[-1]   # 盘后/休市：使用昨收补位

        _logger.info(
            f"  📐 [基准 Regime] {BENCHMARK_CODE} | 现价={last_price:.4f} | MA20={ma20:.4f}"
            f" | 距MA20 {(last_price-ma20)/ma20*100:+.2f}%"
        )
        return last_price, ma20

    except Exception as e:
        _logger.error(f"❌ [基准] 获取失败: {e}")
        return 0.0, 0.0


def calculate_dynamic_targets(df_momentum: pd.DataFrame) -> dict:
    """
    夏普动量决策器（V4 纯动量版）。

    逻辑：
      1. 按 Score 降序排列（Score = Return_20D / Ann_Vol）
      2. 取 Top2 作为 Slot_A / Slot_B 目标
      3. 若某标的 Return_20D < 0（负动量），该位置强制切换为 DEFENSE_ETF

    注意：资金分配（alloc）由 execute_rotation() 根据 Regime 判定后传入，
          此函数仅负责标的选择，alloc 使用模块级 FUNDS_PER_SLOT 作为占位符。

    返回 dict：
      target_a : Slot_A 目标代码
      target_b : Slot_B 目标代码
      alloc_a  : Slot_A 分配资金占位（execute_rotation 会覆盖）
      alloc_b  : Slot_B 分配资金占位
      scores   : {code: Score}（日志用）
    """
    _SAFE = DEFENSE_ETF

    # ── 防冰路径：动量 DataFrame 为空时全线防御
    if df_momentum.empty:
        _logger.error(f"{_R}❌ [决策引擎] 动量 DataFrame 为空，全线切防守！{_E}")
        return {
            "target_a": _SAFE, "target_b": _SAFE,
            "alloc_a": FUNDS_PER_SLOT, "alloc_b": FUNDS_PER_SLOT,
            "scores": {},
        }

    # ── 步骤1：按 Score 降序排列
    df_sorted = df_momentum.sort_values("Score", ascending=False).reset_index(drop=True)

    # ── 步骤2：冷血日志：全量评分明细
    _logger.info("  📊 [决策引擎] 全量夏普动量评分明细：")
    for i, row in df_sorted.iterrows():
        star    = "★" if i < 2 else " "
        neg_tag = f"{_R}【负动量→防守】{_E}" if row["Return_20D"] < 0 else ""
        _logger.info(
            f"     {star} [{row['Code']}]"
            f"  Return_20D={row['Return_20D']*100:+.2f}%"
            f"  Ann_Vol={row['Ann_Vol']*100:.1f}%"
            f"  Score={row['Score']:+.4f}"
            f"  {neg_tag}"
        )

    scores = dict(zip(df_sorted["Code"], df_sorted["Score"].round(4)))

    # ── 步骤3：取 Top2，负动量强制换防守
    def _pick(rank: int) -> str:
        if rank >= len(df_sorted):
            _logger.warning(f"  ⚠️ [决策引擎] 排名第{rank+1}位无标的，切防守")
            return _SAFE
        row = df_sorted.iloc[rank]
        if row["Return_20D"] < 0:
            _logger.warning(
                f"  {_Y}⚠️ [决策引擎] 排名第{rank+1}位 [{row['Code']}] "
                f"Return_20D={row['Return_20D']*100:.2f}%<0，强制切换为防守 [{_SAFE}]{_E}"
            )
            return _SAFE
        return str(row["Code"])

    target_a = _pick(0)
    target_b = _pick(1)

    _logger.info(
        f"  {_G}🏆 [决策引擎] Slot_A=[{target_a}] Slot_B=[{target_b}]"
        f" | 各分配 {FUNDS_PER_SLOT:.0f} 元{_E}"
    )

    return {
        "target_a": target_a,
        "target_b": target_b,
        "alloc_a":  FUNDS_PER_SLOT,
        "alloc_b":  FUNDS_PER_SLOT,
        "scores":   scores,
    }


# ==============================================================================
# 🛡️ 护盾 2：物理查仓与同步执行
# ==============================================================================
def sync_sell(trader, acc, code: str, slot: str, known_qty: int = 0) -> bool:
    """强制卖出并注册 Fill-Based pending。
    优先使用 known_qty（账本股数）精准卖出，防止误卖手动仓位。
    成交落盘由 MacroCallback.on_order_trade 处理。
    """
    if not code: return True

    if known_qty > 0:
        # ✅ 按账本精准数量卖出
        qty = int((known_qty // 100) * 100)
        if qty <= 0:
            _logger.warning(f"{_Y}⚠️ 账本 [{code}] qty={known_qty} 不足100股，跳过卖出{_E}")
            return True
    else:
        # 兜底：物理查仓全量卖出
        positions  = trader.query_stock_positions(acc) or []
        target_pos = next((p for p in positions if p.stock_code == code), None)
        if not target_pos or target_pos.can_use_volume <= 0:
            _logger.warning(f"{_Y}⚠️ 物理查仓：[{code}] 可用量为 0，跳过卖出{_E}")
            return True
        qty = int(target_pos.can_use_volume)

    # Taker 价：bid1 - 0.002，确保必成
    tick      = xtdata.get_full_tick([code]).get(code, {})
    bid_list  = tick.get("bidPrice", []) if tick else []
    bid1      = float(bid_list[0]) if bid_list and bid_list[0] > 0 else 0.0
    if bid1 <= 0:
        bid1 = float(tick.get("lastClose", 0.0) or tick.get("lastPrice", 0.0))
    if bid1 <= 0:
        _logger.error(f"❌ 无法获取 [{code}] 有效卖价，卖出失败")
        return False
    sell_price = round(bid1 - 0.002, 3)

    _logger.info(f"📤 卖出下单 [{code}] {qty}股 @ {sell_price}（bid1={bid1}）...")
    seq = trader.order_stock(
        acc, code, xtconstant.STOCK_SELL, qty,
        xtconstant.FIX_PRICE, sell_price, "Macro_V4", "Rotation_Sell"
    )

    if seq > 0:
        # ✅ 注册 pending，由 MacroCallback.on_order_trade 原子落盘
        with _macro_pending_lock:
            _macro_pending[seq] = {
                "slot":       slot,
                "direction":  "sell",
                "target_qty": qty,
                "filled_so_far": 0,
            }
        _logger.info(f"{_G}✅ [{code}] 卖出指令已下达 {qty}股 (seq={seq})，等待 Fill-Based 回调{_E}")
        return True
    _logger.error(f"❌ [{code}] 卖出委托被拒 seq={seq}")
    return False


def sync_buy(trader, acc, code: str, slot: str, alloc: float = FUNDS_PER_SLOT) -> tuple:
    """
    买入指定资金额度的标的。注册 Fill-Based pending。
    返回 (buy_price, qty)；失败返回 (0.0, 0)。
    成交落盘由 MacroCallback.on_order_trade 处理。
    """
    tick       = xtdata.get_full_tick([code]).get(code, {})
    ask_list   = tick.get("askPrice", []) if tick else []
    ask1       = float(ask_list[0]) if ask_list and ask_list[0] > 0 else 0.0
    last_price = float((tick or {}).get("lastPrice", 0.0))
    ref_price  = ask1 if ask1 > 0 else last_price

    if ref_price <= 0:
        _logger.error(f"❌ 无法获取 [{code}] 最新价，买入失败")
        return 0.0, 0

    qty = int((alloc / ref_price) / 100) * 100
    if qty <= 0:
        _logger.warning(f"⚠️ [{code}] 计算手数=0（ref_price={ref_price:.4f} alloc={alloc:.0f}），跳过")
        return 0.0, 0

    buy_price = round((ask1 + 0.002) if ask1 > 0 else last_price * 1.002, 3)

    _logger.info(
        f"📥 正在买入 [{code}] {qty}股"
        f" @ {buy_price}（ask1={ask1:.4f}）| 预算={alloc:.0f}元 ..."
    )
    seq = trader.order_stock(
        acc, code, xtconstant.STOCK_BUY, qty,
        xtconstant.FIX_PRICE, buy_price, "Macro_V4", "Rotation_Buy"
    )
    if seq > 0:
        # ✅ 注册 pending，由 MacroCallback.on_order_trade 原子落盘
        with _macro_pending_lock:
            _macro_pending[seq] = {
                "slot":       slot,
                "direction":  "buy",
                "target_qty": qty,
                "filled_so_far": 0,
            }
        _logger.info(f"{_G}✅ [{code}] 买入指令已下达 {qty}股 (seq={seq})，等待 Fill-Based 回调{_E}")
        return buy_price, qty
    _logger.error(f"❌ [{code}] 买入委托被拒 seq={seq}")
    return 0.0, 0


# ==============================================================================
# 🚫 缝隙补丁1：无情清场（撤单兜底）
# ==============================================================================
def _cancel_pending_orders(trader, acc) -> int:
    """sleep(8) 醒来后，对所有仍在 _macro_pending 的委托发撤单。
    
    逻辑：
      1. 从 QMT 查询当日委托列表
      2. 遍历 _macro_pending，找到状态仍为"未成交/部分成交"的委托
      3. 发 cancel_order，并从 pending 表移除（防回调再次写入）
    
    返回已撤单数量。宁可少买，也不留幽灵挂单过夜。
    """
    cancelled = 0
    with _macro_pending_lock:
        pending_ids = list(_macro_pending.keys())

    if not pending_ids:
        return 0

    # 查询当日委托状态
    try:
        orders = trader.query_stock_orders(acc, cancelable_only=False) or []
    except Exception as e:
        _logger.error(f"❌ [清场] 查询委托列表失败: {e}")
        return 0

    # 建立 order_id → order 映射
    order_map = {o.order_id: o for o in orders}

    for seq in pending_ids:
        order = order_map.get(seq)
        if order is None:
            # QMT 没有该委托记录，清理 pending
            with _macro_pending_lock:
                _macro_pending.pop(seq, None)
            continue

        # 委托状态：未报/待成交/部分成交 → 撤单
        # QMT 状态码：50=未报, 51=待成交, 52=部分成交
        status = getattr(order, 'order_status', -1)
        if status in (50, 51, 52):
            try:
                trader.cancel_order_stock(acc, seq)
                _logger.warning(
                    f"🚫 [无情清场] 撤单 {order.stock_code} seq={seq} "
                    f"status={status} 已成交={getattr(order,'traded_volume',0)}"
                )
                _send_webhook(
                    f"🚫 MacroRotation 撤单",
                    f"{order.stock_code} 挂单 {getattr(order,'order_volume',0)}股 "
                    f"仅成交 {getattr(order,'traded_volume',0)}股，8s 内未完全成交，主动撤单防止幽灵挂单。"
                )
                cancelled += 1
            except Exception as e:
                _logger.error(f"❌ [清场] 撤单 seq={seq} 失败: {e}")
        else:
            # 已完成/已撤/废单 → 直接清理 pending
            _logger.info(f"[清场] seq={seq} 状态={status}（已完结），清理 pending")

        with _macro_pending_lock:
            _macro_pending.pop(seq, None)

    if cancelled > 0:
        _logger.warning(f"🚫 [清场完毕] 共撤销 {cancelled} 笔未完全成交挂单")
    return cancelled


# ==============================================================================
# 🚀 主控循环
# ==============================================================================
def execute_rotation():
    _logger.info("="*60)
    _logger.info(f"🌍 [Macro V4 Titan] 宏观轮动引擎启动（夏普动量版）")

    # ── 时间窗口守卫：只允许周五 14:40~14:58 执行（双重防误触发）
    now  = datetime.datetime.now()
    wd   = now.isoweekday()   # 1=周一 … 5=周五
    hhmm = now.hour * 100 + now.minute
    if wd != 5:
        _logger.warning(f"[时间守卫] 今天是周{['一','二','三','四','五','六','日'][wd-1]}，非进攻日，退出")
        return
    if not (1440 <= hhmm <= 1458):
        _logger.warning(f"[时间守卫] 当前 {now:%H:%M}，不在 14:40~14:58 进攻窗口，退出")
        return
    _logger.info(f"[时间守卫] 周五 {now:%H:%M} ✅ 进攻窗口确认")

    # 1. 连接柜台（注册 MacroCallback — Fill-Based 成交回调）
    trader = XtQuantTrader(QMT_PATH, int(time.time()))
    trader.register_callback(MacroCallback())
    trader.start()
    time.sleep(1)
    if trader.connect() != 0:
        _logger.error("❌ QMT 柜台连接失败")
        return
    acc = StockAccount(ACCOUNT_ID)
    if trader.subscribe(acc) != 0:
        _logger.error("❌ 账户订阅失败，退出")
        return
    _logger.info("✅ QMT 交易网关已连接（MacroCallback Fill-Based 已注册）")

    # 2. 三步流水线（V4 夏普动量 + Regime 资金缩放，无 Oracle）
    #    Step1: 构建风险调整后动量（本地 QMT 缓存，0 网络请求）
    df_momentum = _build_momentum_df(MACRO_POOL)
    #    Step2: 夏普动量决策，负动量强制切防守
    decision = calculate_dynamic_targets(df_momentum)
    #    Step3: 沪深300 MA20 Regime 判定 → 动态资金缩放
    benchmark_price, benchmark_ma20 = _get_benchmark_stats()
    if benchmark_price <= 0 or benchmark_ma20 <= 0:
        # 基准数据异常：保守降档，用熊市资金
        regime         = "UNKNOWN"
        alloc_per_slot = FUNDS_DOWNTREND
        _logger.warning(f"{_Y}[Regime] 基准数据无效，保守降档至 {alloc_per_slot:.0f}元/槽{_E}")
    elif benchmark_price > benchmark_ma20:
        regime         = "UPTREND"
        alloc_per_slot = FUNDS_UPTREND
        _logger.info(f"  🟢 [Regime] UPTREND（现价 {benchmark_price:.4f} > MA20 {benchmark_ma20:.4f}）→ 满配 {alloc_per_slot:.0f}元/槽")
    else:
        regime         = "DOWNTREND"
        alloc_per_slot = FUNDS_DOWNTREND
        _logger.info(f"  🔴 [Regime] DOWNTREND（现价 {benchmark_price:.4f} ≤ MA20 {benchmark_ma20:.4f}）→ 缩仓 {alloc_per_slot:.0f}元/槽")

    # 用 Regime 驱动的动态资金覆盖决策占位值
    target_a = decision["target_a"]
    target_b = decision["target_b"]
    alloc_a  = alloc_per_slot
    alloc_b  = alloc_per_slot

    _logger.info(
        f"🎯 [决策结果] Slot_A=[{target_a}/{alloc_a:.0f}元] "
        f"Slot_B=[{target_b}/{alloc_b:.0f}元] | Regime={regime}"
    )

    # 3. 读取当前账本
    slots     = _load_slots()
    _logger.info(f"📦 当前账本持仓: {slots}")
    current_a = slots.get("slot_a")
    current_b = slots.get("slot_b")

    # ── 卖出阶段（Fill-Based：下单后由 MacroCallback 原子落盘）
    if current_a and current_a != target_a:
        known_qty_a = slots.get("slot_a_shares", 0)
        if sync_sell(trader, acc, current_a, slot="slot_a", known_qty=known_qty_a):
            # 预写 slot=None，回调会写 shares/cost/hwm；sleep 等回调完成
            slots["slot_a"] = None
            slots["slot_a_capital"] = 0.0
            _save_slots(slots)
            time.sleep(5)   # 等待 on_order_trade 回调落盘
            _cancel_pending_orders(trader, acc)  # 🚫 无情清场：防幽灵挂单

    if current_b and current_b != target_b:
        known_qty_b = slots.get("slot_b_shares", 0)
        if sync_sell(trader, acc, current_b, slot="slot_b", known_qty=known_qty_b):
            slots["slot_b"] = None
            slots["slot_b_capital"] = 0.0
            _save_slots(slots)
            time.sleep(5)
            _cancel_pending_orders(trader, acc)  # 🚫 无情清场：防幽灵挂单

    # ── 买入阶段（Fill-Based：下单注册 pending，回调原子写 shares/cost/hwm）
    slots = _load_slots()   # 重读最新（卖出回调可能已更新）

    if slots.get("slot_a") is None:
        # 预写 slot_a=target_a / capital，回调补 shares/cost/hwm
        slots["slot_a"]         = target_a
        slots["slot_a_capital"] = alloc_a
        _save_slots(slots)
        buy_price_a, qty_a = sync_buy(trader, acc, target_a, slot="slot_a", alloc=alloc_a)
        if buy_price_a > 0:
            time.sleep(8)   # 等待 on_order_trade 回调写 shares/cost/hwm
            _cancel_pending_orders(trader, acc)  # 🚫 无情清场：防幽灵挂单
        else:
            slots["slot_a"] = None  # 下单失败回滚
            slots["slot_a_capital"] = 0.0
            _save_slots(slots)
    else:
        # ── 同标的持仓不变：Topup 补仓差额 ──────────────────────────────────
        old_capital_a = slots.get("slot_a_capital", alloc_a)
        delta_a = alloc_a - old_capital_a
        if delta_a >= 1000:
            _logger.info(
                f"  📈 [Topup-A] [{target_a}] 持仓不变，资金档位 {old_capital_a:.0f}→{alloc_a:.0f}，"
                f"补仓差额 {delta_a:.0f}元"
            )
            slots["slot_a_capital"] = alloc_a  # 预更新 capital
            _save_slots(slots)
            sync_buy(trader, acc, target_a, slot="slot_a", alloc=delta_a)
            time.sleep(8)   # 等待回调（加权均价在回调里重算）
            _cancel_pending_orders(trader, acc)  # 🚫 无情清场：防幽灵挂单
        else:
            slots["slot_a_capital"] = alloc_a
            _save_slots(slots)

    slots = _load_slots()   # 重读（买入回调可能已落盘 shares）

    if slots.get("slot_b") is None:
        slots["slot_b"]         = target_b
        slots["slot_b_capital"] = alloc_b
        _save_slots(slots)
        buy_price_b, qty_b = sync_buy(trader, acc, target_b, slot="slot_b", alloc=alloc_b)
        if buy_price_b > 0:
            time.sleep(8)
            _cancel_pending_orders(trader, acc)  # 🚫 无情清场：防幽灵挂单
        else:
            slots["slot_b"] = None
            slots["slot_b_capital"] = 0.0
            _save_slots(slots)
    else:
        old_capital_b = slots.get("slot_b_capital", alloc_b)
        delta_b = alloc_b - old_capital_b
        if delta_b >= 1000:
            _logger.info(
                f"  📈 [Topup-B] [{target_b}] 持仓不变，资金档位 {old_capital_b:.0f}→{alloc_b:.0f}，"
                f"补仓差额 {delta_b:.0f}元"
            )
            slots["slot_b_capital"] = alloc_b
            _save_slots(slots)
            sync_buy(trader, acc, target_b, slot="slot_b", alloc=delta_b)
            time.sleep(8)
            _cancel_pending_orders(trader, acc)  # 🚫 无情清场：防幽灵挂单
        else:
            slots["slot_b_capital"] = alloc_b
            _save_slots(slots)

    # 最终落盘：重读回调最新结果，补 updated_at
    slots = _load_slots()
    slots["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_slots(slots)

    _logger.info(f"🎉 轮动执行完毕，最终槽位状态: {slots}")
    _logger.info("="*60)

    # N8N 推送：轮动完成汇报
    _send_webhook(
        f"✅ MacroRotation V4 完成 | 夏普动量 | {regime}",
        f"Slot_A=[{slots.get('slot_a')}] {alloc_a:.0f}元 HWM={slots.get('slot_a_hwm', 'N/A')}\n"
        f"Slot_B=[{slots.get('slot_b')}] {alloc_b:.0f}元 HWM={slots.get('slot_b_hwm', 'N/A')}\n"
        f"Regime={regime} | 基准={benchmark_price:.4f} MA20={benchmark_ma20:.4f}\n"
        f"Top Scores: {dict(list(decision['scores'].items())[:3])}\n"
        f"updated_at: {slots.get('updated_at', '')}",
    )


if __name__ == "__main__":
    execute_rotation()