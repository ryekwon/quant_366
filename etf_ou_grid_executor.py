# ==============================================================================
# 🎯 [部署节点] : Quant-PC
# 职责: 盘中无限轮询，执行非对称网格 (浅水区高频 T，深水区爆破)
#
# [架构版本] v4 — 真实下单版：
#   ① 接入 XtQuantTrader 真实下单（buy/sell）
#   ② Fill-Based 延迟记账（铁律一：禁止乐观更新）
#   ③ 盲人摸象隔离（铁律二：只读自身账本，不查全局持仓）
#   ④ 对称清仓（铁律三：卖出量从账本 bought_qty 读取）
#   ⑤ 并发锁保护（铁律四：pending + 账本 IO 均加锁）
#   + 继承 v3 全部特性（总仓位熔断 / 时间止损 / T+1锁 / 全量遥测探针）
#   + 订单标识: strategyName="ETF_OU_Grid", orderRemark="Buy"/"Sell"
# ==============================================================================
import sys, io
# ── 强制 UTF-8 输出（防 Windows GBK 控制台 UnicodeEncodeError 启动即崩）────────
# autopilot 通过 Popen(stdout=fh) 将 sys.stdout 接管为原始 fd，
# 但 fd 的编码默认为操作系统 locale（gbk），emoji/中文 print 立刻崩溃。
# 此处在所有 import 之前把 stdout/stderr 强制包成 UTF-8 TextIOWrapper。
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
if hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

import time, json, os, threading

from datetime import datetime
from dotenv import load_dotenv
from xtquant import xtdata, xtconstant
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

load_dotenv()
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")
QMT_PATH        = os.getenv("QMT_PATH", "")
ACCOUNT_ID      = os.getenv("ACCOUNT_ID", "")

# ── 绝对路径基准（防止 CWD 漂移导致文件找不到）─────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))

# ── 配置区 ───────────────────────────────────────────────────────────────────
SLOTS_FILE      = os.path.join(_DIR, ".state", "etf_grid_slots.json")
POS_FILE        = os.path.join(_DIR, ".state", "etf_grid_positions.json")
TELEMETRY_CSV   = os.path.join(_DIR, ".state", "oracle_telemetry_grid.csv")
BLACKLIST_FILE  = os.path.join(_DIR, ".state", "etf_grid_blacklist.json")  # 🚫 小黑屋持久化

CAPITAL_PER_GRID     = 15_000   # 单格火力（1.5 万）
MAX_SYSTEM_CAPITAL   = 200_000  # 🚨 总仓位熔断墙
TIME_STOP_MULTIPLIER = 2.0      # ⏳ 时间止损倍数（N × halflife_days）
PENDING_TIMEOUT_SEC  = 60       # pending 委托超时巡检阈值（秒）
LOOP_INTERVAL_SEC    = 5        # 主循环轮询间隔（秒）
MARKET_OPEN          = "09:30"  # 交易时段开始
MARKET_CLOSE         = "14:57"  # 交易时段结束（保留 3 分钟余量）

# ── 铁律四：并发锁 ────────────────────────────────────────────────────────────
_pending_lock = threading.Lock()
_pos_lock     = threading.Lock()

# pending 注册表：{seq: {code, direction, grid_lvl, qty, sent_at, bought_qty}}
_pending: dict = {}

# ── 独立失败冷静字典（与 _pending 完全隔离，不被 sweep 误清）────────────────
# {\"fail_{code}_{lvl}_{dir}\": expire_timestamp}
_failed_cooldowns: dict = {}
FAIL_COOLDOWN_SEC = 30   # 发单失败后 30s 内不允许同格同方向重发


def _is_failed_cooling(code: str, grid_lvl: int, direction: str) -> bool:
    """返回 True 表示该格最近失败过，仍在冷静期内"""
    key = f"fail_{code}_{grid_lvl}_{direction}"
    expire = _failed_cooldowns.get(key, 0)
    if time.time() < expire:
        return True
    _failed_cooldowns.pop(key, None)  # 已过期，顺手清理
    return False


def _set_failed_cooling(code: str, grid_lvl: int, direction: str):
    """登记失败冷静，不影响 _pending"""
    key = f"fail_{code}_{grid_lvl}_{direction}"
    _failed_cooldowns[key] = time.time() + FAIL_COOLDOWN_SEC

# ── 🚫 小黑屋（Blacklist）：断头台触发后 48h 禁止该标的再次买入 ─────────────
# {code: blacklist_until_timestamp (float, unix seconds)}
_BLACKLIST_LOCK: threading.Lock = threading.Lock()
_blacklist: dict = {}          # 模块级，多线程共享
_BLACKLIST_HOURS = 48          # 小黑屋时长（小时）


# ── N8N Webhook（失败静默，不阻断主逻辑）────────────────────────────────────
def send_webhook(title: str, message: str) -> None:
    if not N8N_WEBHOOK_URL or not _HAS_REQUESTS:
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
        print(f"⚠️ Webhook 发送失败: {e}")


# ── 工具函数 ─────────────────────────────────────────────────────────────────
def _calculate_days_held(buy_date_str: str, current_date_str: str) -> int:
    """计算物理持仓天数（自然日）"""
    try:
        b = datetime.strptime(buy_date_str, '%Y%m%d')
        c = datetime.strptime(current_date_str, '%Y%m%d')
        return max(0, (c - b).days)
    except Exception:
        return 0


def _calc_buy_qty(price: float, capital: float) -> int:
    """按资金和价格计算买入手数（百股取整）"""
    if price <= 0:
        return 0
    qty = int(capital / price / 100) * 100
    return qty


# ── 🛡️ 护盾一：致命下跌趋势探针 ──────────────────────────────────────────────
def is_fatal_downtrend(code: str) -> bool:
    """
    判断标的是否处于致命单边下跌趋势。
    条件：当前最新价 < MA20 且 MA20 斜率 < 0（均线本身在走低）。
    用于拦截网格向下买入（接飞刀防护）。
    数据源：xtdata.get_market_data_ex 日线，取最近 22 根。
    """
    try:
        import numpy as np
        now_str = datetime.now().strftime('%Y%m%d')
        # 取最近 22 个交易日日线（需要 21 根才能有 MA20 + 前一日 MA20）
        data = xtdata.get_market_data_ex(
            field_list=['close'],
            stock_list=[code],
            period='1d',
            count=22,
        )
        if code not in data or data[code] is None or data[code].empty:
            print(f"⚠️ [趋势护盾] {code} 无日线数据，跳过护盾检测（放行）")
            return False

        close_series = data[code]['close'].dropna()
        if len(close_series) < 21:
            print(f"⚠️ [趋势护盾] {code} 日线不足 21 根 (仅{len(close_series)}根)，放行")
            return False

        closes = close_series.values  # numpy array，最新在末尾
        # 当日最新价（用 get_full_tick 实时价优先，日线收盘价保底）
        tick = xtdata.get_full_tick([code]).get(code, {})
        latest_price = float(tick.get('lastPrice', 0) or closes[-1])

        # 计算最新 MA20 与上一日 MA20
        ma20_latest = float(np.mean(closes[-20:]))      # 含当日：indices [-20:]
        ma20_prev   = float(np.mean(closes[-21:-1]))    # 上一日：indices [-21:-1]
        slope       = ma20_latest - ma20_prev

        if latest_price < ma20_latest and slope < 0:
            print(
                f"\033[91m⛔ [趋势护盾·拦截] {code} "
                f"现价={latest_price:.3f} < MA20={ma20_latest:.3f} "
                f"且斜率={slope:.4f}<0，单边暴跌信号，拒绝接飞刀！\033[0m"
            )
            return True
        return False
    except Exception as e:
        print(f"⚠️ [趋势护盾] {code} 检测异常: {e}，放行（保守原则）")
        return False


# ── ⚔️ 护盾二：ATR-14 波动率工具 ─────────────────────────────────────────────
def get_atr_14(code: str) -> float:
    """
    计算过去 14 个交易日的平均真实波幅 (Average True Range, ATR-14)。
    公式：TR = max(high-low, |high-prev_close|, |low-prev_close|)
    返回 ATR（绝对价格单位）；失败或数据不足时返回 0.0。
    """
    try:
        import numpy as np
        data = xtdata.get_market_data_ex(
            field_list=['high', 'low', 'close'],
            stock_list=[code],
            period='1d',
            count=16,   # 需要 15 根才能算 14 个 TR
        )
        if code not in data or data[code] is None or data[code].empty:
            return 0.0

        df = data[code][['high', 'low', 'close']].dropna()
        if len(df) < 15:
            return 0.0

        highs      = df['high'].values
        lows       = df['low'].values
        closes     = df['close'].values
        prev_close = closes[:-1]          # t-1 收盘
        tr_high    = highs[1:]            # t 最高
        tr_low     = lows[1:]             # t 最低

        tr = np.maximum(
            tr_high - tr_low,
            np.maximum(
                np.abs(tr_high - prev_close),
                np.abs(tr_low  - prev_close)
            )
        )
        atr = float(np.mean(tr[-14:]))
        return atr
    except Exception as e:
        print(f"⚠️ [ATR-14] {code} 计算异常: {e}")
        return 0.0


# ── 小黑屋工具（持久化版：重启后不丢失）────────────────────────────────────
def _load_blacklist_from_disk():
    """进程启动时从磁盘恢复小黑屋状态（防止重启后小黑屋失效）"""
    global _blacklist
    if not os.path.exists(BLACKLIST_FILE):
        return
    try:
        with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        now = time.time()
        # 只恢复尚未过期的条目
        with _BLACKLIST_LOCK:
            _blacklist = {k: v for k, v in data.items() if v > now}
        valid = len(_blacklist)
        print(f"🚫 [小黑屋·恢复] 从磁盘恢复 {valid} 个有效惩罚条目")
    except Exception as e:
        print(f"⚠️ [小黑屋·恢复] 读取失败: {e}，使用空黑名单")


def _save_blacklist_to_disk():
    """将小黑屋状态原子写入磁盘"""
    try:
        os.makedirs(os.path.dirname(BLACKLIST_FILE), exist_ok=True)
        tmp = BLACKLIST_FILE + ".tmp"
        with _BLACKLIST_LOCK:
            data = dict(_blacklist)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        os.replace(tmp, BLACKLIST_FILE)
    except Exception as e:
        print(f"⚠️ [小黑屋·保存] 写盘失败: {e}")


def _is_blacklisted(code: str) -> bool:
    """检查标的是否在小黑屋内（未过 48h 惩罚期）"""
    with _BLACKLIST_LOCK:
        until = _blacklist.get(code, 0)
    return time.time() < until


def _add_to_blacklist(code: str):
    """将标的关入小黑屋 48h，并持久化到磁盘（重启后仍有效）"""
    until = time.time() + _BLACKLIST_HOURS * 3600
    with _BLACKLIST_LOCK:
        _blacklist[code] = until
    _save_blacklist_to_disk()  # 🔒 关键：立即写盘，防止重启丢失
    until_dt = datetime.fromtimestamp(until).strftime('%Y-%m-%d %H:%M')
    print(f"\033[91m🚫 [小黑屋] {code} 已关押，{_BLACKLIST_HOURS}h 内禁止买入，解禁时间: {until_dt}\033[0m")


# ── 遥测探针 ─────────────────────────────────────────────────────────────────
def write_grid_telemetry(code: str, action: str, grid_lvl: int,
                         price: float, ma20: float,
                         profit_pct, reversion_pct, remark: str = ""):
    """
    👁️ [网格探针]：全量沉淀状态机每一次心跳的物理切片。
    编码：utf-8-sig（BOM 头，Excel/WPS 直接打开中文不乱码）
    """
    os.makedirs(os.path.dirname(TELEMETRY_CSV), exist_ok=True)
    file_exists = os.path.exists(TELEMETRY_CSV)
    p_pct = profit_pct    if profit_pct    is not None else 0.0
    r_pct = reversion_pct if reversion_pct is not None else 0.0
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(TELEMETRY_CSV, mode='a', newline='', encoding='utf-8-sig') as f:
        if not file_exists:
            f.write("Timestamp,Code,Action,Grid_Lvl,Price,MA20,Profit_Pct,Reversion_Pct,Remark\n")
        f.write(f"{timestamp},{code},{action},{grid_lvl},"
                f"{price:.3f},{ma20:.3f},{p_pct:.4f},{r_pct:.4f},{remark}\n")


# ── 账本 I/O（原子写回 + 并发锁）────────────────────────────────────────────
def _load_positions() -> dict:
    """铁律四：加锁读账本"""
    with _pos_lock:
        if os.path.exists(POS_FILE):
            with open(POS_FILE, 'r', encoding='utf-8-sig') as f:  # utf-8-sig 兼容 BOM
                return json.load(f)
        return {}


def _save_positions(positions: dict):
    """铁律四：加锁原子写账本"""
    os.makedirs(os.path.dirname(POS_FILE), exist_ok=True)
    tmp = POS_FILE + ".tmp"
    with _pos_lock:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(positions, f, indent=4, ensure_ascii=False)
        os.replace(tmp, POS_FILE)


# ── QMT 交易回调（Fill-Based 记账）──────────────────────────────────────────
class GridCallback(XtQuantTraderCallback):
    """
    铁律一：只在 on_stock_trade 成交回调后才更新账本，严禁乐观写账。
    铁律四：通过 _pending_lock 和 _pos_lock 保护并发写入。
    """

    def on_stock_trade(self, trade):
        """成交回调：按实际 traded_volume 写账本（修复一：分笔成交不pop，累加至满额再pop）"""
        seq = trade.order_id
        with _pending_lock:
            meta = _pending.get(seq)  # 【修复一】只读不弹
        if meta is None:
            return  # 不是本引擎的委托，忽略（铁律二：盲人摸象）

        code       = meta["code"]
        direction  = meta["direction"]
        grid_lvl   = meta["grid_lvl"]
        filled     = int(trade.traded_volume)
        fill_price = float(trade.traded_price)

        if filled <= 0:
            return

        # 【修复一】累加分笔成交量
        with _pending_lock:
            meta['filled_so_far'] = meta.get('filled_so_far', 0) + filled
            filled_so_far = meta['filled_so_far']
            target_qty    = meta.get('qty', filled)

        positions = _load_positions()

        if direction == "buy":
            pos_list = positions.get(code, [])
            existing = [p for p in pos_list if p['grid_lvl'] == grid_lvl]
            if existing:
                # 分笔累加 bought_qty
                existing[0]['bought_qty'] = existing[0].get('bought_qty', 0) + filled
                print(f"✅ [Fill-Buy 分笔] {code} 格{grid_lvl} +{filled}股 累计={existing[0]['bought_qty']}股 @ {fill_price:.3f}")
            else:
                pos_list.append({
                    "grid_lvl":   grid_lvl,
                    "buy_price":  fill_price,
                    "bought_qty": filled,        # 铁律三：记录实际买入量，卖时原数归还
                    "buy_date":   datetime.now().strftime('%Y%m%d'),
                    "trade_rule": meta.get("trade_rule", "T+1"),
                })
                print(f"✅ [Fill-Buy] {code} 格{grid_lvl} 成交 {filled}股 @ {fill_price:.3f}")
            positions[code] = pos_list
        else:  # sell
            pos_list = positions.get(code, [])
            new_list = [p for p in pos_list if p['grid_lvl'] != grid_lvl]
            positions[code] = new_list
            print(f"✅ [Fill-Sell] {code} 格{grid_lvl} 已平仓 {filled}股(本笔) @ {fill_price:.3f} | 累计={filled_so_far}")

        _save_positions(positions)

        # 【修复一】满额才pop，分笔成交期间保留pending
        if filled_so_far >= target_qty:
            with _pending_lock:
                _pending.pop(seq, None)
            print(f"📋 [Pending清除] {code} 格{grid_lvl} {direction} 委托已全额成交，移出pending")

    def on_stock_order(self, order):
        """
        委托状态回调（修复一）：只在极其明确的【废单/部撤/已撤】状态下才清理 pending。
        对于未报(48)/已报(56)/部成(53) 等中间态，保留 pending，不干扰午休挂单。
        xtconstant 状态码：
          ORDER_CANCELED      = 50  # 已撤
          ORDER_JUNK          = 52  # 废单
          ORDER_PART_CANCLE   = 54  # 部撤
        """
        TERMINAL_STATES = {50, 52, 54}   # 只允许这三种终态清理 pending
        if order.order_status not in TERMINAL_STATES:
            return  # 中间态（未报/已报/部成）一律忽略，保留 pending

        seq = order.order_seq
        with _pending_lock:
            meta = _pending.pop(seq, None)
        if meta:
            status_map = {50: "已撤", 52: "废单", 54: "部撤"}
            label = status_map.get(order.order_status, str(order.order_status))
            print(f"⚠️ [订单{label}] {meta['code']} 格{meta['grid_lvl']} "
                  f"方向={meta['direction']} seq={seq}，解除 pending 锁定")
            # 关键：撤单/废单后也要设冷静期，防止立刻重发
            _set_failed_cooling(meta['code'], meta['grid_lvl'], meta['direction'])

    def on_order_error(self, order_error):
        """委托废单：从 pending 移除，账本不动，并设冷静防止立刻重发"""
        seq = order_error.order_id
        with _pending_lock:
            meta = _pending.pop(seq, None)
        if meta:
            print(f"❌ [废单] {meta['code']} 格{meta['grid_lvl']} "
                  f"方向={meta['direction']} 错误码={order_error.error_id}")
            # 关键：设置 30s 冷静（防止交易所派回废单后立刻重发机枪）
            _set_failed_cooling(meta['code'], meta['grid_lvl'], meta['direction'])

    def on_disconnected(self):
        print("⚠️ [GridCallback] QMT 连接断开！")

    def on_connected(self):
        print("✅ [GridCallback] QMT 连接恢复。")


# ── 修复二：在途雷达（Pending Guard）────────────────────────────────────────
def _is_order_pending(code: str, grid_lvl: int, direction: str) -> bool:
    """
    检查是否已有同标的、同格数、同方向的委托在途。
    同时检查失败冷静字典——返回 True → 拒绝重复发单。
    """
    # ① 先检查独立冷静字典（失败后 30s 内不重发）
    if _is_failed_cooling(code, grid_lvl, direction):
        return True
    # ② 再检查在途 pending（正常委托尚未成交）
    with _pending_lock:
        for meta in _pending.values():
            if (meta.get('code') == code
                    and meta.get('grid_lvl') == grid_lvl
                    and meta.get('direction') == direction):
                return True
    return False


# ── pending 超时巡检（补丁 C）────────────────────────────────────────────────
def _sweep_stale_pending(xt_trader, acc):
    """
    每轮检测：超过 PENDING_TIMEOUT_SEC 仍在 pending 的委托，
    物理查成交记录补账，然后从 pending 中清除。
    """
    now = time.time()
    with _pending_lock:
        stale = {seq: m for seq, m in _pending.items()
                 if now - m['sent_at'] > PENDING_TIMEOUT_SEC}

    for seq, meta in stale.items():
        print(f"⏰ [Sweep] {meta['code']} 格{meta['grid_lvl']} "
              f"委托超时 {PENDING_TIMEOUT_SEC}s，物理补账...")
        try:
            trades = xt_trader.query_stock_trades(acc) or []
            filled = sum(int(t.traded_volume) for t in trades if t.order_id == seq)
            if filled > 0:
                print(f"  ✅ [Sweep] 实盘确认成交 {filled}股，补写账本")
                # 复用 on_stock_trade 逻辑
                class _FakeTrade:
                    order_id     = seq
                    traded_volume = filled
                    traded_price  = meta.get('price', 0)
                GridCallback().on_stock_trade(_FakeTrade())
            else:
                print(f"  ⚠️ [Sweep] 实盘无成交记录，视为废单，从 pending 移除")
        except Exception as e:
            print(f"  ❌ [Sweep] 查询成交异常: {e}")
        finally:
            with _pending_lock:
                _pending.pop(seq, None)


# ── 发单辅助（统一标识规范）─────────────────────────────────────────────────
def _place_order(xt_trader, acc, code, direction, qty, price,
                 grid_lvl, trade_rule, remark):
    """
    发送限价单，注册 pending（禁止乐观更新账本）。
    strategyName="ETF_OU_Grid"，orderRemark 由调用方传入。
    返回 seq（> 0 为成功）。
    """
    side = xtconstant.STOCK_BUY if direction == "buy" else xtconstant.STOCK_SELL
    # 限价单，FIX_PRICE Taker 模式：以当前价挂单确保成交
    seq = xt_trader.order_stock(
        acc, code, side, qty,
        xtconstant.FIX_PRICE, round(price, 3),
        strategy_name="ETF_OU_Grid",
        order_remark=remark,
    )
    if seq > 0:
        with _pending_lock:
            _pending[seq] = {
                "code":       code,
                "direction":  direction,
                "grid_lvl":   grid_lvl,
                "qty":        qty,
                "trade_rule": trade_rule,
                "price":      price,
                "sent_at":    time.time(),
            }
        print(f"📤 [下单] {code} {direction} 格{grid_lvl} | "
              f"{qty}股 @ {price:.3f} | seq={seq}")
    else:
        print(f"❌ [下单失败] {code} {direction} 格{grid_lvl} | "
              f"{qty}股 @ {price:.3f} | seq={seq}")
        # ⛲️ 根本修复：写入独立冷静字典，不进 _pending，不会被 sweep 误清
        _set_failed_cooling(code, grid_lvl, direction)
    return seq


# ── 主执行函数 ────────────────────────────────────────────────────────────────
def run_hybrid_executor(xt_trader, acc):
    today_str = datetime.now().strftime('%Y%m%d')

    if not os.path.exists(SLOTS_FILE):
        print("⚠️  SLOTS_FILE 不存在，跳过本轮执行")
        return

    with open(SLOTS_FILE, 'r', encoding='utf-8') as f:
        slots = json.load(f)['slots']

    # 铁律二：只读自身账本（_load_positions()），不查 QMT 全局持仓
    positions = _load_positions()
    codes     = [s['code'] for s in slots]
    full_tick = xtdata.get_full_tick(codes)

    # ── 🚨 Step 0：pending 超时巡检（每轮都跑）────────────────────────────────
    _sweep_stale_pending(xt_trader, acc)

    # ── 🚨 Step 1：系统级熔断雷达（遍历 slots 之前先算全局已部署资金）─────────
    total_deployed_capital = sum(
        CAPITAL_PER_GRID
        for pos_list in positions.values()
        for _ in pos_list
    )
    is_meltdown = total_deployed_capital >= MAX_SYSTEM_CAPITAL
    if is_meltdown:
        print(f"🚨 [系统级熔断] 已部署 {total_deployed_capital:,} ≥ 上限 {MAX_SYSTEM_CAPITAL:,}！禁止新开仓！")
        send_webhook(
            "🚨 ETF_OU_Grid 系统级熔断",
            f"已部署资金 {total_deployed_capital:,} 元 ≥ 熔断上限 {MAX_SYSTEM_CAPITAL:,} 元\n"
            f"本轮所有网格强制停止买入，等待仓位自然释放。"
        )

    new_positions = {}

    for slot in slots:
        code        = slot['code']
        step        = slot['dynamic_step']
        ma20        = slot['ma20_baseline']
        trade_rule  = slot.get('trade_rule', 'T+1')
        halflife    = float(slot.get('halflife_days', 5.0))
        time_stop_days = halflife * TIME_STOP_MULTIPLIER

        tick = full_tick.get(code, {})
        price = tick.get('lastPrice', 0)
        if price <= 0:
            new_positions[code] = positions.get(code, [])
            continue

        # ask1 用于买入报价（取卖一价，贴近成交）
        ask_list  = tick.get('askPrice', [price])
        ask1      = ask_list[0] if ask_list else price
        ask3      = ask_list[2] if len(ask_list) > 2 and ask_list[2] > 0 else ask1
        buy_price = round(min(ask3, ask1 * 1.005), 3)   # ask3 扫单，上浮 0.5% 保护

        pos_list  = positions.get(code, [])
        surviving = []
        any_sold_this_round = False   # 🛡️ 防止止盈/止损后同轮触发首网买入（低卖高买竞态）

        # ── 🟢 1. 平仓侦测 ────────────────────────────────────────────────────
        # ⚔️ 预计算 ATR-14（每只标的每轮只算一次，供断头台使用）
        try:
            _atr14 = get_atr_14(code)
        except Exception as _e:
            _atr14 = 0.0
            print(f"⚠️ [ATR预算] {code} 异常: {_e}")

        for p in pos_list:
            buy_date  = p.get('buy_date', today_str)
            profit    = (price / p['buy_price']) - 1
            reversion = (price / ma20) - 1
            grid_lvl  = p['grid_lvl']
            days_held = _calculate_days_held(buy_date, today_str)

            # 铁律三：卖出量从账本的 bought_qty 精确读取，消灭碎股
            sell_qty = int(p.get('bought_qty', 0))

            # 🔒 T+1 物理机械锁
            if trade_rule == "T+1" and buy_date == today_str:
                surviving.append(p)
                write_grid_telemetry(code, "LOCKED_T1", grid_lvl, price, ma20,
                                     profit, reversion, "买入首日物理锁死")
                continue

            sold = False

            # ─────────────────────────────────────────────────────────────────
            # ⚔️ 护盾二·断头台（优先级 > 时间止损 > 其他）
            # 条件A：最新价 < 动态止损价（ATR 动态空间底线）
            # 条件B：持仓天数 >= 2 且账面浮亏
            # 满足任一条件 → 无条件市价清仓 + 关入小黑屋
            # ─────────────────────────────────────────────────────────────────
            try:
                avg_cost      = float(p['buy_price'])
                atr_stop_price = (avg_cost - 1.5 * _atr14) if _atr14 > 0 else 0.0
                hit_atr_stop  = (_atr14 > 0) and (price < atr_stop_price)
                hit_time_stop = (days_held >= 2) and (profit < 0)

                if hit_atr_stop or hit_time_stop:
                    reason = "ATR动态防线" if hit_atr_stop else "时间防线(≥2天浮亏)"
                    detail = (
                        f"现价={price:.3f} 止损价={atr_stop_price:.3f} "
                        f"ATR={_atr14:.4f} 持仓={days_held}天 浮亏={profit*100:.2f}%"
                    )
                    print(
                        f"\033[91m💥 [断头台触发] {code} 格{grid_lvl} — "
                        f"{reason}击穿，无条件止损！\n       {detail}\033[0m"
                    )
                    write_grid_telemetry(
                        code, "GUILLOTINE_SL", grid_lvl, price, ma20,
                        profit, reversion,
                        f"断头台:{reason}|atr={_atr14:.4f}|stop={atr_stop_price:.4f}"
                    )
                    send_webhook(
                        "💥 ETF_OU_Grid 断头台触发",
                        f"[{code}] 格{grid_lvl} | {reason}\n"
                        f"现价={price:.3f} | 止损价={atr_stop_price:.3f}\n"
                        f"持仓={days_held}天 | 浮亏={profit*100:.2f}%\n"
                        f"⚠️ 无条件市价清仓，标的关入48h小黑屋！"
                    )
                    if sell_qty > 0:
                        # 【修复二】在途雷达：防重复卖出
                        if _is_order_pending(code, grid_lvl, "sell"):
                            print(f"\033[93m⏳ [断头台] {code} 格{grid_lvl} 已有卖单在途，跳过重复发单\033[0m")
                            surviving.append(p)
                            continue
                        # 【修复四】卖出使用 bid1 Taker 价（bid1 - 0.002）
                        bid1_list  = tick.get('bidPrice', [price])
                        bid1       = float(bid1_list[0]) if bid1_list and bid1_list[0] > 0 else price
                        sell_price = round(bid1 - 0.002, 3)
                        seq_sl = _place_order(
                            xt_trader, acc, code, "sell", sell_qty,
                            sell_price, grid_lvl, trade_rule, "Sell_Guillotine"
                        )
                    else:
                        seq_sl = -1  # 无需卖出（空仓），直接关黑屋
                    # ⚠️ 铁律一：只有卖单成功提交（seq>0）才标记sold，
                    # 卖单下单失败(seq<0)时不能将格从surviving剔除，
                    # 否则下一轮surviving=[]会触发首网重复买入！
                    if seq_sl > 0 or sell_qty <= 0:
                        # 关入小黑屋（48h 禁止买入）
                        _add_to_blacklist(code)
                        sold = True
                        any_sold_this_round = True  # 🛡️ 本轮卖出标记
                    else:
                        print(f"\033[93m⚠️ [断头台] {code} 格{grid_lvl} 卖单下单失败(seq={seq_sl})，"
                              f"格保留在surviving，小黑屋不激活，下轮重试\033[0m")
            except Exception as _e:
                print(f"⚠️ [断头台] {code} 格{grid_lvl} 判断异常: {_e}，跳过断头台")

            if sold:
                # 断头台已清仓，跳过后续止盈/止损逻辑
                pass

            # ⏳ 时间止损（优先级：仅次于断头台）
            elif days_held > time_stop_days:
                print(f"💀 [时间止损] {code} 格{grid_lvl} 站岗 {days_held}天 > 阈值 {time_stop_days:.1f}天")
                write_grid_telemetry(code, "TIME_STOP", grid_lvl, price, ma20,
                                     profit, reversion, f"站岗{days_held}天超阈值强平")
                send_webhook("💀 ETF_OU_Grid 时间止损",
                             f"[{code}] 格{grid_lvl}\n站岗 {days_held}天 > {time_stop_days:.1f}天\n"
                             f"价 {price:.3f} | 浮盈 {profit*100:.2f}%")
                # 【修复二】在途雷达：防重复时间止损发单
                if _is_order_pending(code, grid_lvl, "sell"):
                    print(f"⏳ [时间止损] {code} 格{grid_lvl} 已有卖单在途，跳过")
                    surviving.append(p)
                    continue
                _seq_ts = -1
                if sell_qty > 0:
                    # 【修复四】bid1 Taker 价
                    bid1_list  = tick.get('bidPrice', [price])
                    bid1       = float(bid1_list[0]) if bid1_list and bid1_list[0] > 0 else price
                    sell_price = round(bid1 - 0.002, 3)
                    _seq_ts = _place_order(xt_trader, acc, code, "sell", sell_qty,
                                 sell_price, grid_lvl, trade_rule, "Sell_TimeStop")
                if _seq_ts > 0 or sell_qty <= 0:
                    sold = True
                    any_sold_this_round = True  # 🛡️ 本轮卖出标记
                    # 【修复三】时间止损成功 → 强制加入小黑屋（防止立刻触发首网）
                    _add_to_blacklist(code)
                    print(f"🚫 [时间止损] {code} 格{grid_lvl} 卖单成功，关入小黑屋48h")
                else:
                    print(f"⚠️ [时间止损] {code} 格{grid_lvl} 卖单失败(seq={_seq_ts})，格保留surviving，下轮重试")

            elif grid_lvl <= 2:
                # 浅水区：反弹 1 个步长止盈
                if profit >= step:
                    print(f"💰 [{code}] 浅水区 格{grid_lvl} 止盈 +{profit*100:.2f}%")
                    write_grid_telemetry(code, "SELL_SHALLOW", grid_lvl, price, ma20,
                                         profit, reversion, "浅水区榨取周转率")
                    send_webhook("💰 ETF_OU_Grid 浅水区止盈",
                                 f"[{code}] 格{grid_lvl}\n"
                                 f"价 {price:.3f} | 盈利 +{profit*100:.2f}%\n"
                                 f"释放资金 {CAPITAL_PER_GRID:,} 元")
                    # 【修复二】在途雷达
                    if _is_order_pending(code, grid_lvl, "sell"):
                        print(f"⏳ [浅水止盈] {code} 格{grid_lvl} 已有卖单在途，跳过")
                        surviving.append(p)
                        continue
                    # 【修复四】bid1 Taker 价
                    bid1_list  = tick.get('bidPrice', [price])
                    bid1       = float(bid1_list[0]) if bid1_list and bid1_list[0] > 0 else price
                    sell_price = round(bid1 - 0.002, 3)
                    if sell_qty > 0:
                        _seq_sh = _place_order(xt_trader, acc, code, "sell", sell_qty,
                                     sell_price, grid_lvl, trade_rule, "Sell_Shallow")
                    else:
                        _seq_sh = -1
                    if _seq_sh > 0 or sell_qty <= 0:
                        sold = True
                        any_sold_this_round = True  # 🛡️ 止盈后本轮禁止重新买入
                        # 【修复三】浅水止盈成功 → 强制加入小黑屋（防止立刻触发首网）
                        _add_to_blacklist(code)
                        print(f"🚫 [浅水止盈] {code} 格{grid_lvl} 卖单成功，关入小黑屋48h")
                    else:
                        print(f"⚠️ [浅水止盈] {code} 格{grid_lvl} 卖单失败(seq={_seq_sh})，格保留surviving，下轮重试")
                else:
                    write_grid_telemetry(code, "HOLD_SHALLOW", grid_lvl, price, ma20,
                                         profit, reversion, "未达动态步长")

            else:
                # 深水区：死等 MA20 收敛
                if reversion >= -0.005:
                    print(f"🌋 [{code}] 深水区 格{grid_lvl} 均值回归！全仓爆破！")
                    write_grid_telemetry(code, "SELL_DEEP", grid_lvl, price, ma20,
                                         profit, reversion, "深潜均值回归爆破")
                    send_webhook("🌋 ETF_OU_Grid 深水区均值回归爆破",
                                 f"[{code}] 格{grid_lvl}\n"
                                 f"价 {price:.3f} | MA20={ma20:.3f} | 偏离 {reversion*100:.2f}%\n"
                                 f"浮盈 {profit*100:.2f}% | 全仓爆破！")
                    # 【修复二】在途雷达
                    if _is_order_pending(code, grid_lvl, "sell"):
                        print(f"⏳ [深水爆破] {code} 格{grid_lvl} 已有卖单在途，跳过")
                        surviving.append(p)
                        continue
                    # 【修复四】bid1 Taker 价
                    bid1_list  = tick.get('bidPrice', [price])
                    bid1       = float(bid1_list[0]) if bid1_list and bid1_list[0] > 0 else price
                    sell_price = round(bid1 - 0.002, 3)
                    if sell_qty > 0:
                        _seq_dp = _place_order(xt_trader, acc, code, "sell", sell_qty,
                                     sell_price, grid_lvl, trade_rule, "Sell_Deep")
                    else:
                        _seq_dp = -1
                    if _seq_dp > 0 or sell_qty <= 0:
                        sold = True
                        any_sold_this_round = True  # 🛡️ 爆破后本轮禁止重新买入
                    else:
                        print(f"⚠️ [深水爆破] {code} 格{grid_lvl} 卖单失败(seq={_seq_dp})，格保留surviving，下轮重试")
                else:
                    write_grid_telemetry(code, "HOLD_DEEP", grid_lvl, price, ma20,
                                         profit, reversion, "深水锁定死等0轴")

            # ⚠️ 铁律一：sold=True 时，账本清除由 on_stock_trade 回调完成。
            # 此处仅从"本轮 surviving"中剔除，账本的物理持久化在回调里。
            # 如果委托废单（on_order_error），surviving 会在下一轮读账本时恢复。
            if not sold:
                surviving.append(p)

        new_positions[code] = surviving

        # ── 🔴 2. 建仓侦测 ────────────────────────────────────────────────────
        # 🛡️ 关键守卫：若本轮刚发生止盈/止损/断头台卖出，禁止同轮触发首网买入
        # 原因：卖单尚未成交（异步Fill），账本格尚未清除，surviving=[]
        # 是「本轮主动卖出」的副产品，不应触发「市场消失了，需要重新建仓」逻辑
        # 下一轮（5s后）账本回调更新后，再判断是否需要首网
        if any_sold_this_round:
            print(f"🛡️ [{code}] 本轮已发生卖出，跳过首网建仓侦测，下轮再判断")
        elif not surviving:
            if price < ma20 * (1 - step):
                if is_meltdown:
                    write_grid_telemetry(code, "MELTDOWN_REJECT", 1, price, ma20,
                                         None, (price / ma20) - 1, "系统熔断拒绝开仓")
                # ── 🛡️ 首网护盾一：小黑屋拦截（BUG修复：原代码此处缺失！）────
                elif _is_blacklisted(code):
                    print(f"\033[91m🚫 [小黑屋] {code} 仍在惩罚期内，拒绝首网买入\033[0m")
                    write_grid_telemetry(code, "BLACKLIST_REJECT", 1, price, ma20,
                                         None, (price / ma20) - 1, "断头台小黑屋禁买首网")
                # ── 🛡️ 首网护盾二：致命下跌趋势拦截────────────────────────────
                elif is_fatal_downtrend(code):
                    write_grid_telemetry(code, "DOWNTREND_REJECT", 1, price, ma20,
                                         None, (price / ma20) - 1, "趋势护盾拦截首网飞刀")
                    # 告警日志已在 is_fatal_downtrend 内部打印（红色）
                else:
                    # ── 🛡️ 首网护盾三：孤儿持仓检测（防账本清零后重复建仓）────
                    # 场景：账本被手动清零，但实盘仍有该标的持仓（残留/Bug遗留）
                    # 若贸然开首网，会造成重复建仓。应收编孤儿持仓后跳过买入。
                    try:
                        phys_list = xt_trader.query_stock_positions(acc) or []
                        phys_pos  = next((p for p in phys_list if p.stock_code == code), None)
                        phys_vol  = int(phys_pos.volume) if phys_pos else 0
                    except Exception as _qe:
                        phys_vol = 0
                        print(f"⚠️ [{code}] 孤儿检测查仓异常: {_qe}，假设无持仓继续")
                    if phys_vol > 0:
                        # 实盘有仓但账本为空 → 孤儿持仓！收编，不重复买入
                        orphan_cost = float(phys_pos.open_price) if phys_pos and phys_pos.open_price else price
                        print(f"\033[93m⚠️ [孤儿持仓] {code} 实盘有 {phys_vol}股(成本≈{orphan_cost:.3f})"
                              f"但账本为空！收编为 格1，跳过首网买入\033[0m")
                        send_webhook("⚠️ ETF_OU_Grid 孤儿持仓收编",
                                     f"[{code}] 实盘持仓 {phys_vol}股 @ {orphan_cost:.3f}\n"
                                     f"账本为空，自动收编为格1，不重复开仓\n"
                                     f"请人工确认该持仓是否正常！")
                        write_grid_telemetry(code, "ORPHAN_ADOPTED", 1, price, ma20,
                                             None, (price / ma20) - 1,
                                             f"孤儿持仓收编:{phys_vol}股@{orphan_cost:.3f}")
                        # 收编：写入账本（格1，用实盘成本价）
                        adopted_qty = min(phys_vol, _calc_buy_qty(orphan_cost, CAPITAL_PER_GRID) * 2)
                        new_positions.setdefault(code, [])
                        new_positions[code].append({
                            "grid_lvl":   1,
                            "buy_price":  round(orphan_cost, 4),
                            "bought_qty": phys_vol,
                            "buy_date":   today_str,
                            "trade_rule": trade_rule,
                        })
                    else:
                        qty = _calc_buy_qty(buy_price, CAPITAL_PER_GRID)
                        if qty > 0:
                            # 【修复二】在途雷达：防止机枪式重复买入首网
                            if _is_order_pending(code, 1, "buy"):
                                print(f"⏳ [{code}] 首网买单已在途，跳过重复发单")
                            else:
                                print(f"🌊 [{code}] 偏离0轴，打入首网！qty={qty} @ {buy_price:.3f}")
                                seq = _place_order(xt_trader, acc, code, "buy", qty,
                                                   buy_price, 1, trade_rule, "Buy")
                                write_grid_telemetry(code, "BUY_FIRST", 1, buy_price, ma20,
                                                     None, (buy_price / ma20) - 1, "偏离0轴开火首网")
                                send_webhook("🌊 ETF_OU_Grid 首网开仓",
                                             f"[{code}] trade_rule={trade_rule}\n"
                                             f"开仓价 {buy_price:.3f} | MA20={ma20:.3f} | "
                                             f"偏离 {((buy_price/ma20)-1)*100:.2f}%\n"
                                             f"动用资金 {CAPITAL_PER_GRID:,} 元 | 步长 {step*100:.2f}%")
                        else:
                            print(f"⚠️ [{code}] 首网计算买入量=0，跳过（价格过高或资金不足）")
        else:
            last_p   = surviving[-1]['buy_price']
            next_lvl = surviving[-1]['grid_lvl'] + 1
            if price < last_p * (1 - step):
                if is_meltdown:
                    write_grid_telemetry(code, "MELTDOWN_REJECT", next_lvl, price, ma20,
                                         None, (price / ma20) - 1, "系统熔断拒绝加仓")
                # ── 🛡️ 护盾一：趋势护盾（BUY_NEXT 前必查）────────────────────
                elif _is_blacklisted(code):
                    print(f"\033[91m🚫 [小黑屋] {code} 仍在惩罚期内，拒绝第{next_lvl}网买入\033[0m")
                    write_grid_telemetry(code, "BLACKLIST_REJECT", next_lvl, price, ma20,
                                         None, (price / ma20) - 1, "断头台小黑屋禁买")
                elif is_fatal_downtrend(code):
                    write_grid_telemetry(code, "DOWNTREND_REJECT", next_lvl, price, ma20,
                                         None, (price / ma20) - 1, "趋势护盾拦截飞刀买入")
                    # 告警日志已在 is_fatal_downtrend 内部打印（红色）
                else:
                    qty = _calc_buy_qty(buy_price, CAPITAL_PER_GRID)
                    if qty > 0:
                        # 【修复二】在途雷达：防止深潜加仓机枪连发
                        if _is_order_pending(code, next_lvl, "buy"):
                            print(f"⏳ [{code}] 第{next_lvl}网买单已在途，跳过重复发单")
                        else:
                            print(f"⚓ [{code}] 深潜加仓，打入第{next_lvl}网！qty={qty} @ {buy_price:.3f}")
                            seq = _place_order(xt_trader, acc, code, "buy", qty,
                                               buy_price, next_lvl, trade_rule, "Buy")
                            write_grid_telemetry(code, "BUY_NEXT", next_lvl, buy_price, ma20,
                                                 None, (buy_price / ma20) - 1, f"深潜加至第{next_lvl}网")
                            send_webhook("⚓ ETF_OU_Grid 深潜加仓",
                                         f"[{code}] trade_rule={trade_rule}\n"
                                         f"第{next_lvl}网 | 加仓价 {buy_price:.3f} | 上格买价 {last_p:.3f}\n"
                                         f"偏离 {((buy_price/ma20)-1)*100:.2f}% | 追加 {CAPITAL_PER_GRID:,} 元")
                    else:
                        print(f"⚠️ [{code}] 第{next_lvl}网计算买入量=0，跳过")

    # ⚠️ 铁律一注意：new_positions 只用于本轮"临时逻辑状态"显示。
    # 账本的真实状态由 on_stock_trade 回调维护。
    # 这里不再调用 _save_positions（物理账本由回调原子写入）。


# ── 主入口 ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🛸 极速网格 (真实下单版 v4.1 — 断头台竞态修复版) 已上线！")
    # 🚫 进程启动时恢复小黑屋状态（防止重启后黑名单丢失导致立刻买入）
    _load_blacklist_from_disk()

    if not QMT_PATH or not ACCOUNT_ID:
        print("❌ .env 缺少 QMT_PATH 或 ACCOUNT_ID，退出")
        exit(1)

    session_id = int(time.time())
    xt_trader  = XtQuantTrader(QMT_PATH, session_id)
    callback   = GridCallback()
    xt_trader.register_callback(callback)
    xt_trader.start()

    print("⏳ 等待交易网关初始化（5 秒）...")
    time.sleep(5)
    res = xt_trader.connect()
    if res != 0:
        print(f"❌ XtQuantTrader 连接失败 (错误码: {res})，退出")
        xt_trader.stop()
        exit(1)
    print("✅ 交易网关连接成功")

    acc = StockAccount(ACCOUNT_ID)
    xt_trader.subscribe(acc)

    print(f"🔄 开始盘中轮询（{LOOP_INTERVAL_SEC}s/轮，交易窗口 {MARKET_OPEN}~{MARKET_CLOSE}）")

    while True:
        now_hhmm = datetime.now().strftime("%H:%M")

        # ── 修复三：午休硬熔断（11:30~13:00 物理禁止发单）─────────────────────
        if "11:30" <= now_hhmm < "13:00":
            print(f"💤 [{now_hhmm}] 市场午休中，网格暂停探测，等待 13:00 开盘...")
            time.sleep(10)
            continue

        if MARKET_OPEN <= now_hhmm <= MARKET_CLOSE:
            try:
                run_hybrid_executor(xt_trader, acc)
            except Exception as e:
                import traceback
                print(f"🔥 [执行器异常] {e}")
                print(traceback.format_exc())
        else:
            print(f"[{now_hhmm}] [开市/休市恢复日]，等待交易窗口...")

        time.sleep(LOOP_INTERVAL_SEC)