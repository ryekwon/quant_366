import json
import math
import os
import time
import threading
from datetime import datetime
from xtquant import xtdata, xtconstant
from quant_logger import record_action
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
import random
import calendar
from dotenv import load_dotenv
import requests
import sys
import csv

# 🛡️ 解决 Windows 控制台打印 Emoji 导致的 UnicodeEncodeError
if sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        # 兼容旧版本 Python 3.6
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

load_dotenv()
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")

def send_n8n_alert(title, message):
    if not N8N_WEBHOOK_URL: return
    try:
        payload = {"title": title, "message": message, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
        # POST 隐式传递 JSON
        requests.post(N8N_WEBHOOK_URL, json=payload, timeout=10)
    except Exception:
        pass

# 物理路径配置（基于 __file__ 动态定位，避免硬编码机器路径）
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR_BASE = os.getenv("STATE_DIR", os.path.join(_PROJECT_ROOT, ".state"))
TARGETS_FILE = os.path.join(STATE_DIR_BASE, "sniper_targets.json")
HOLDINGS_FILE = os.path.join(STATE_DIR_BASE, "sniper_holdings.json")
SNIPER_TOTAL_CAPITAL = 1000       # 绝对火力上限：10万元
TARGET_COUNT = 2              # 取前 2 名动量最强标的
APPROACH_RATE_THRESHOLD = 0.6    # 逼近率门槛：60%（未封死涨停才可买）

# ── 遥测 CSV（与 exit_guard 共享同一文件，event_type 区分）──────
_SNIPER_STATE_DIR  = STATE_DIR_BASE
_SNIPER_TELEM_FILE = os.path.join(_SNIPER_STATE_DIR, "sniper_telemetry.csv")
_SNIPER_TELEM_LOCK = threading.Lock()
_SNIPER_TELEM_FIELDS = [
    "event_type",        # SIGNAL_DETECTED / POSITION_OPENED / HOLDING_LOG / POSITION_CLOSED
    "ts",                # 时间戳 HH:MM:SS
    "code", "name",
    "approach_rate",     # 逼近率（SIGNAL 时有效）
    "momentum_score",    # 动量分
    "ask1",              # 下单盘口卖一
    "buy_price",         # 实际下单价
    "qty",               # 委托/持仓手数
    "entry_price",       # VWAP 均价（成交后）
    "last_price",        # 当前现价（HOLDING_LOG）
    "unrealized_pnl_pct",# 浮动盈亏 %
    "exit_price", "exit_reason",  # POSITION_CLOSED 时有效
    "pnl_pct",
]

def _write_sniper_telem(row: dict):
    """线程安全追加遥测行，首次写入自动输出 Header。"""
    with _SNIPER_TELEM_LOCK:
        is_new = not os.path.exists(_SNIPER_TELEM_FILE)
        with open(_SNIPER_TELEM_FILE, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_SNIPER_TELEM_FIELDS, extrasaction="ignore")
            if is_new:
                w.writeheader()
            row.setdefault("ts", datetime.now().strftime("%H:%M:%S"))
            w.writerow(row)

# 🛡️ 线程安全写账锁：防止多笔成交回调并发写 JSON 产生竞态覆盖
_HOLDINGS_LOCK = threading.Lock()



def acquire_lock_with_ttl(lock_path, max_age_seconds=600):
    """带 TTL 的自愈型进程锁。默认 600 秒 (10 分钟) 超时。"""
    if os.path.exists(lock_path):
        file_age = time.time() - os.path.getmtime(lock_path)
        if file_age > max_age_seconds:
            print(f"⚠️ [系统自愈] 发现残留孤儿锁 ({file_age:.1f}秒前)。强行粉碎并接管控制权...")
            try:
                os.remove(lock_path)
            except OSError: pass
        else:
            print(f"🚫 [并发拦截] 进程锁生效中 (存活 {file_age:.1f}秒)，本次调度自动静默退让。")
            return False
    try:
        with open(lock_path, 'w') as f: f.write(str(os.getpid()))
        return True
    except: return False

def release_lock(lock_path):
    if os.path.exists(lock_path):
        try: os.remove(lock_path)
        except OSError: pass

def safe_execute_and_lock(xt_trader, acc, code, order_type, qty, price, strategy_name, order_remark):
    """原子化下单封装（移除即时落盘，改为回调落盘）"""
    try:
        res = xt_trader.order_stock(acc, code, order_type, qty, xtconstant.FIX_PRICE, price, strategy_name, order_remark)
        return res
    except Exception as e:
        print(f"❌ Execution Critical Error: {e}")
        return -1

def _write_holding(code, name, buy_price, buy_qty, filled_price, filled_qty):
    """
    真实成交后，将记录写入底仓账本。
    🛡️ [线程安全] 使用 _HOLDINGS_LOCK 串行化读-改-写，防止多笔成交回调
    并发执行时后写覆盖先写，导致部分成交记录永久丢失（2026-04-13 301373竞态Bug）。
    """
    with _HOLDINGS_LOCK:
        holdings = {}
        if os.path.exists(HOLDINGS_FILE):
            try:
                with open(HOLDINGS_FILE, 'r', encoding='utf-8') as f:
                    holdings = json.load(f)
            except:
                holdings = {}
        holdings[code] = {
            "name": name,
            "buy_price": filled_price,   # 使用真实成交价
            "qty": filled_qty,           # 使用真实成交量
            "ordered_price": buy_price,
            "ordered_qty": buy_qty,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "entry_ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # 精确成交时间戳，供 Auditor MFE/MAE 分钟级切割
        }
        with open(HOLDINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(holdings, f, ensure_ascii=False, indent=4)
        print(f"✅ [{code}] 真实成交已入账：{filled_qty}股 @ {filled_price}")


def is_trading_safe_zone():
    """
    周末效应避险 + 月末流动性枯竭避险
    """
    now = datetime.now()
    
    # 防线 1：周末效应避险 (周五绝对不买，weekday=4为周五)
    if now.weekday() == 4:
        print("🛑 周末避险协议触发：今日为周五，Sniper 空仓过周末。")
        return False
        
    # 防线 2：月末流动性枯竭避险 (月底最后 2 天绝对不买)
    # 计算当前月的总天数
    _, last_day_of_month = calendar.monthrange(now.year, now.month)
    days_to_eom = last_day_of_month - now.day
    
    if days_to_eom <= 2: # 包含最后一天，倒数第二天，倒数第三天 (视具体定义)
        print(f"🛑 月末避险协议触发：距离月末仅剩 {days_to_eom} 天，流动性极度枯竭，Sniper 强制休战。")
        return False
        
    return True


def calculate_limit_up_physical(symbol: str, pre_close: float) -> float:
    """
    抛弃 QMT 脆弱的 API，直接进行物理维度的涨停推演。
    依据 A 股物理铁律：创业板/科创板 20%，主板 10%。
    严格四舍五入到 2 位小数，与交易所一致。
    """
    if pre_close <= 0:
        return 0.0
    if symbol.startswith('300') or symbol.startswith('301') \
            or symbol.startswith('688') or symbol.startswith('689'):
        multiplier = 1.20  # 创业板 / 科创板 20%
    elif symbol.startswith('60') or symbol.startswith('00'):
        multiplier = 1.10  # 主板 10%
    else:
        multiplier = 1.10  # 默认降级为 10%
    return round(pre_close * multiplier, 2)


def _get_sector_codes():
    """获取 600/000/300 三大板块（沪市主板 + 深市主板 + 创业板）代码列表。"""
    try:
        sh_codes = xtdata.get_stock_list_in_sector('上证A股') or []
        sz_codes = xtdata.get_stock_list_in_sector('深证A股') or []
    except Exception as e:
        print(f"⚠️ 获取板块列表失败: {e}")
        return []
    valid_prefixes = (
        '600', '601', '603', '605',   # 沪市主板
        '000', '001', '002', '003',   # 深市主板
        '300', '301',                 # 创业板
    )
    all_codes = sh_codes + sz_codes
    return [c for c in all_codes if c[:3] in valid_prefixes]


def build_ranked_candidates():
    """
    全市场动量扫描：600 / 000 / 300 三大板块。

    逼近率 = (当前价 - 昨收价) / (涨停价 - 昨收价)
    最终动量分 = 逼近率 × (当日成交额 / 自由流通市值)

    筛选条件：逼近率 > APPROACH_RATE_THRESHOLD 且卖一价有效（未封死涨停）
    返回：按动量分降序排列的前 TARGET_COUNT 名候选列表
    """
    codes = _get_sector_codes()
    if not codes:
        print("❌ [Sniper 雷达] 无法获取板块列表，中止扫描。")
        return []

    print(f"🔍 [Sniper 雷达] 扫描 {len(codes)} 只标的 (600/000/300 三大板块)...")

    # ─── 阶段一：批量拉取全量 Tick，快速预筛（涨幅 > 7%）───────────────
    try:
        tick_map = xtdata.get_full_tick(codes)
    except Exception as e:
        print(f"❌ [Sniper 雷达] 批量 Tick 拉取失败: {e}")
        return []

    # ── 向量化逼近率前置过滤（废除硬编码 7% 涨幅，直接按逼近率精准卡位）────────
    # 无需逐支调 API：根据代码前缀推导涨停幅（主板 10%，创业板/科创板 20%）
    def _est_limit_pct(c: str) -> float:
        return 0.20 if c[:3] in ('300', '301', '688', '689') else 0.10

    pre_filtered = []
    for code in codes:
        tick = tick_map.get(code)
        if not tick:
            continue
        last_price = tick.get('lastPrice', 0)
        pre_close  = tick.get('lastClose', tick.get('preClose', 0))
        if last_price <= 0 or pre_close <= 0:
            continue
        up_est = pre_close * (1 + _est_limit_pct(code))
        denom  = up_est - pre_close
        if denom <= 0:
            continue
        if (last_price - pre_close) / denom >= APPROACH_RATE_THRESHOLD:
            pre_filtered.append(code)

    print(f"   预筛通过: {len(pre_filtered)} 只 (估算逼近率 ≥ {APPROACH_RATE_THRESHOLD:.0%}，前缀推导涨停幅)")
    if not pre_filtered:
        print("📭 [Sniper 雷达] 全市场无强势标的，今日休战。")
        return []

    # ─── 阶段二：精算逼近率与动量分 + 对账日志采集 ──────────────────────────
    scored         = []
    candidates_log = []   # CEO 对账 CSV，无论命中与否都落盘
    rejection_stats = {
        "涨停价数据无效":       0,
        "逼近率不足(<60%)": 0,
        "已封死涨停(无买点)": 0,
    }
    for code in pre_filtered:
        tick       = tick_map[code]
        last_price = tick.get('lastPrice', 0)
        pre_close  = tick.get('lastClose', tick.get('preClose', 0))
        amount     = tick.get('amount', 0)
        ask1       = tick.get('askPrice', [0])[0]

        instr        = xtdata.get_instrument_detail(code) or {}
        # 🛡️ [物理兜底] UpLimit 由 API 获取；若 API 返回 0 或小于昨收价（数据无效），
        # 立即切换为 calculate_limit_up_physical() 手动推算，拒绝 NA 污染。
        api_up_limit = instr.get('UpLimit', 0)
        if api_up_limit > 0 and api_up_limit > pre_close:
            up_limit = api_up_limit
        else:
            up_limit = calculate_limit_up_physical(code, pre_close)
        float_volume = instr.get('FloatVolume', 0)
        total_volume = instr.get('TotalVolume', 0)
        name         = instr.get('InstrumentName', code)

        # ── 对账底稿公共字段（无论是否通过筛选都记录）───────────────────────
        gain_pct     = last_price / pre_close - 1 if pre_close > 0 else 0
        cap_volume   = float_volume if float_volume > 0 else total_volume
        free_float_cap = cap_volume * last_price if cap_volume > 0 else 0
        turnover_rate  = amount / free_float_cap if free_float_cap > 0 else 0
        rec = {
            '代码':   code,
            '名称':   name,
            '现价':   f'{last_price:.3f}',
            '涨幅%':  f'{gain_pct:.2%}',
            '逼近率': '',
            '换手率%': f'{turnover_rate:.2%}',
            '动量分': '',
            '精筛结果': '',
        }

        # 物理兜底后 up_limit 理论上永远 > pre_close，此判断仅作最后防线
        if up_limit <= 0 or up_limit <= pre_close:
            rejection_stats["涨停价数据无效"] += 1
            rec['精筛结果'] = '涨停价数据无效(物理兜底失败)'
            candidates_log.append(rec)
            continue

        # ── 逼近率（精确值，已有真实 up_limit）────────────────────────────
        approach_rate = (last_price - pre_close) / (up_limit - pre_close)
        rec['逼近率'] = f'{approach_rate:.2%}'

        if approach_rate <= APPROACH_RATE_THRESHOLD:
            rejection_stats["逼近率不足(<60%)"] += 1
            rec['精筛结果'] = '逼近率不足'
            candidates_log.append(rec)
            continue

        if ask1 <= 0 or ask1 >= up_limit:
            rejection_stats["已封死涨停(无买点)"] += 1
            rec['精筛结果'] = '已封死涨停'
            candidates_log.append(rec)
            continue

        # ── 动量分 = 逼近率 × 换手资金比 ─────────────────────────────────
        if free_float_cap > 0:
            momentum_score = approach_rate * (amount / free_float_cap)
        else:
            momentum_score = approach_rate
        rec['动量分']   = f'{momentum_score:.4f}'
        rec['精筛结果'] = '通过精筛'
        candidates_log.append(rec)

        scored.append({
            'code':           code,
            'name':           name,
            'approach_rate':  approach_rate,
            'momentum_score': momentum_score,
            'ask1':           ask1,
            'up_limit':       up_limit,
        })

    # ── 对账 CSV 落地（无论是否有命中，都物理写盘）──────────────────────────
    today_str = datetime.now().strftime('%Y%m%d')
    logs_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    csv_path  = os.path.join(logs_dir, f'{today_str}_sniper_candidates.csv')
    if candidates_log:
        _fieldnames = ['代码', '名称', '现价', '涨幅%', '逼近率', '换手率%', '动量分', '精筛结果']
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as _f:
            _w = csv.DictWriter(_f, fieldnames=_fieldnames)
            _w.writeheader()
            _w.writerows(candidates_log)
        print(
            f"✅ [Sniper对账] 已导出候选股详细数据至 {os.path.basename(csv_path)}，"
            f"共 {len(candidates_log)} 只，请手动验证数据。"
        )

    if not scored:
        print("📭 [Sniper 雷达] 精筛全军覆没，死亡归因：")
        for reason, count in rejection_stats.items():
            if count > 0:
                icon = "⛔" if "无效" not in reason and "缺失" not in reason else "⚠️"
                print(f"   {icon} {reason}: {count} 只")
        print("📭 [Sniper 雷达] 精筛后无有效候选，今日休战。")
        return []

    # 降序排列，取前 TARGET_COUNT 名
    scored.sort(key=lambda x: x['momentum_score'], reverse=True)
    top = scored[:TARGET_COUNT]

    medals = ['🥇', '🥈', '🥉']
    print(f"✅ [Sniper 雷达] 精筛命中 {len(scored)} 只，取前 {len(top)} 名：")
    for i, s in enumerate(top):
        medal = medals[i] if i < len(medals) else f"#{i+1}"
        print(f"   {medal} [{s['code']}] {s['name']} "
              f"| 逼近率: {s['approach_rate']:.2%} "
              f"| 动量分: {s['momentum_score']:.4f}")
    return top


# ─── 维护一个全局状态，用于追踪待确认订单 ───────────────────────
# 字段说明：
#   code / name / qty / price → 委托原始参数
#   fill_qty_total   → 累计成交量（分笔 fill 逐步累加）
#   fill_amount_total→ 累计成交金额（用于结算 VWAP）
#   status           → 'pending' | 'partial' | 'filled' | 'error'
PENDING_ORDERS = {}  # { order_id: {...} }

# ─── 交易所回调：监听真实委托结果 ────────────────────────────────
class SniperCallback(XtQuantTraderCallback):
    """
    捕获交易所真实回报：
    - on_order_stock: 报单确认
    - on_stock_trade/on_order_stock_transaction: 成交回报（真正买入成功）
    - on_order_error: 委托被拒（超涨跌停、资金不足等）
    """
    def on_order_stock(self, response):
        """报单推送"""
        oid = response.order_id
        if oid in PENDING_ORDERS:
            print(f"📝 [Sniper 报单确认] {PENDING_ORDERS[oid]['name']}({PENDING_ORDERS[oid]['code']}) | 状态: {response.order_status}")

    def on_stock_trade(self, response):
        """
        分笔/全量成交推送（Fill-Based 抽屉累积）。

        同一委托可能触发多次回调（部分成交），此处采用 T0 Fill-Based 架构：
        每次 fill 都累加到 fill_qty_total / fill_amount_total，
        再用 VWAP 均价 + 合计数量覆写账本，确保最终账本记录的是真实合计持仓。
        """
        oid = response.order_id
        if oid not in PENDING_ORDERS:
            return

        code        = PENDING_ORDERS[oid]['code']
        name        = PENDING_ORDERS[oid]['name']
        this_price  = response.traded_price
        this_qty    = response.traded_volume

        # ── Fill-Based 累积：本笔 fill 追加到抽屉 ──────────────────────
        PENDING_ORDERS[oid]['fill_qty_total']    += this_qty
        PENDING_ORDERS[oid]['fill_amount_total'] += this_price * this_qty

        total_qty    = PENDING_ORDERS[oid]['fill_qty_total']
        total_amount = PENDING_ORDERS[oid]['fill_amount_total']
        vwap_price   = round(total_amount / total_qty, 4) if total_qty > 0 else this_price
        ordered_price = PENDING_ORDERS[oid]['price']
        ordered_qty   = PENDING_ORDERS[oid]['qty']

        # 🛡️ 每次 fill 都用累积值覆写账本（VWAP + 合计量），确保最终正确
        _write_holding(code, name, ordered_price, ordered_qty, vwap_price, total_qty)

        print(
            f"📦 [Sniper 分笔成交] {name}({code}) "
            f"本笔: {this_qty}股 @ {this_price} "
            f"| 累计: {total_qty}股 / 委托: {ordered_qty}股 "
            f"| VWAP: {vwap_price}"
        )

        # ── 全量成交 → 发送最终成交通知 ─────────────────────────────────
        if total_qty >= ordered_qty:
            PENDING_ORDERS[oid]['status'] = 'filled'
            msg = (
                f"✅ [Sniper 全量成交]\n"
                f"目标: {name}({code})\n"
                f"实际成交: {total_qty}股 | VWAP均价: {vwap_price}\n"
                f"委托价: {ordered_price}  委托量: {ordered_qty}\n"
                f"订单号: {oid}"
            )
            print(msg)
            send_n8n_alert("✅ Sniper 进场成功", msg)
            record_action(
                strategy="Sniper", action="买入", target=code,
                price=vwap_price, reason="全量成交确认",
                extra={"qty": total_qty, "vwap": vwap_price, "seq": oid}
            )
            # ── 遥测：POSITION_OPENED ─────────────────────────────
            _pnd = PENDING_ORDERS[oid]
            _write_sniper_telem({
                "event_type":     "POSITION_OPENED",
                "code":           code,
                "name":           name,
                "approach_rate":  round(_pnd.get("approach_rate", 0), 4),
                "momentum_score": round(_pnd.get("momentum_score", 0), 4),
                "buy_price":      ordered_price,
                "qty":            total_qty,
                "entry_price":    vwap_price,
            })
        else:
            # 部分成交：更新状态，继续等待剩余 fill
            PENDING_ORDERS[oid]['status'] = 'partial'
            record_action(
                strategy="Sniper", action="买入(部分)", target=code,
                price=this_price, reason=f"分笔成交 {total_qty}/{ordered_qty}",
                extra={"qty": this_qty, "total_qty": total_qty, "seq": oid}
            )

    def on_order_error(self, response):
        """委托被拒推送 — 交易所打回来的真实错误。"""
        oid = response.order_id
        if oid not in PENDING_ORDERS:
            return
            
        code = PENDING_ORDERS[oid]['code']
        name = PENDING_ORDERS[oid]['name']
        err_msg = getattr(response, 'error_msg', '未知原因')
        err_id  = getattr(response, 'error_id', -1)
        
        msg = (
            f"❌ [Sniper 委托被拒]\n"
            f"目标: {name}({code})\n"
            f"错误码: {err_id}\n"
            f"错误信息: {err_msg}\n"
            f"订单号: {oid}"
        )
        print(msg)
        send_n8n_alert("🚨 Sniper 委托被拒", msg)
        record_action(strategy="Sniper", action="挂单失败", target=code, price=0, reason=err_msg, extra={"seq": oid})
        
        # 标记为已出错
        PENDING_ORDERS[oid]['status'] = 'error'


def execute_sniper_entry(xt_trader, acc):
    LOCK_FILE = os.path.join(os.path.dirname(TARGETS_FILE), "sniper_entry.lock")
    if not acquire_lock_with_ttl(LOCK_FILE): return

    try:
        # ── 0. 物理锁：避险周期 ───────────────────────────────────────────
        if not is_trading_safe_zone():
            return

        # ── 1. 全市场动量扫描：取前 TARGET_COUNT 名 ─────────────────────
        ranked = build_ranked_candidates()
        if not ranked:
            print("🏁 Sniper：全市场无命中，今日空仓。")
            return

        # ── 2. 资金 1/N 均分 ─────────────────────────────────────────────
        n = len(ranked)
        capital_per_target = SNIPER_TOTAL_CAPITAL / n
        filled_count = 0

        # 读取已有持仓，避免本程序内重复下单
        holdings = {}
        if os.path.exists(HOLDINGS_FILE):
            try:
                with open(HOLDINGS_FILE, 'r', encoding='utf-8') as f:
                    holdings = json.load(f)
            except: pass

        # ── 3. 逐一击发 ──────────────────────────────────────────────────
        for target in ranked:
            code           = target['code']
            name           = target['name']
            up_limit       = target['up_limit']
            approach_rate  = target['approach_rate']
            momentum_score = target['momentum_score']

            if code in holdings:
                print(f"⚠️ [{code}] 已有持仓记录，跳过。")
                continue

            # 实时刷新流动性（排名后到下单之间可能有微小时间差）
            tick_data = xtdata.get_full_tick([code])
            tick      = tick_data.get(code, {})
            ask_prices = tick.get('askPrice', [0, 0, 0])   # [ask1, ask2, ask3, ...]
            ask1       = ask_prices[0] if len(ask_prices) > 0 else 0
            ask3       = ask_prices[2] if len(ask_prices) > 2 and ask_prices[2] > 0 else ask1
            volume     = tick.get('volume', 0)

            if volume == 0:
                print(f"🚧 [{code}] 暂无成交量（可能临停），跳过。")
                continue

            if ask1 <= 0 or (up_limit > 0 and ask1 >= up_limit):
                print(f"🔒 [{code}] 涨停封死 (ask={ask1})，买入通路关闭，跳过。")
                continue

            # ── 【手术A】越档扫单：取 ask3（卖三价）越档买入，近乎保证秒成交 ─────
            # ask3 > ask1 时越档吃掉三档卖盘，极大降低 Pending 概率。
            # ask3 若无效（停牌/封单）则 fallback 到 ask1；两者均受涨停板天花板保护。
            sweep_price = ask3 if ask3 > ask1 else ask1
            buy_price   = min(round(sweep_price, 2), up_limit - 0.01 if up_limit > 0 else sweep_price)
            sweep_label = "ask3越档" if ask3 > ask1 else "ask1"

            # 🛡️ [涨停熔断] 目标委托价不得触及涨停板，否则被柜台物理拒单
            if up_limit > 0 and buy_price >= up_limit:
                print(f"🚫 [{code}] [涨停熔断] 目标价 {buy_price} >= 涨停价 {up_limit}，放弃买入。")
                continue

            buy_qty = math.floor((capital_per_target / buy_price) / 100) * 100
            if buy_qty < 100:
                print(f"⚠️ [{code}] 分仓资金不足一手，跳过。")
                continue

            print(
                f"🔫 [Sniper 拔枪] {name}({code}) "
                f"| 逼近率: {approach_rate:.2%} "
                f"| 动量分: {momentum_score:.4f} "
                f"| 分仓: {buy_qty} 股 @ {buy_price} [{sweep_label}扫单]"
            )

            # 🛡️ 发单并追踪
            seq = safe_execute_and_lock(
                xt_trader, acc, code, xtconstant.STOCK_BUY,
                buy_qty, buy_price, 'Sniper_V2', 'SniperBuy'
            )

            if seq > 0:
                filled_count += 1
                PENDING_ORDERS[seq] = {
                    'code':  code,
                    'name':  name,
                    'qty':   buy_qty,
                    'price': buy_price,
                    'status': 'pending',
                    # Fill-Based 累积抽屉（分笔成交累加，避免覆盖Bug）
                    'fill_qty_total':    0,
                    'fill_amount_total': 0.0,
                    # 携带选股指标供成交回调写遥测
                    'approach_rate':  approach_rate,
                    'momentum_score': momentum_score,
                }
                print(f"🚀 [Sniper] {name} 委托已发送 | 单号: {seq} | 等待成交确认...")
                # ── 遥测：SIGNAL_DETECTED（发单即记）────────────────
                _write_sniper_telem({
                    "event_type":     "SIGNAL_DETECTED",
                    "code":           code,
                    "name":           name,
                    "approach_rate":  round(approach_rate, 4),
                    "momentum_score": round(momentum_score, 4),
                    "ask1":           ask1,
                    "buy_price":      buy_price,
                    "qty":            buy_qty,
                })

            time.sleep(0.5)

        # ── 4. 等待所有报单完成（超时 30 秒）────────────────────────────
        if filled_count > 0:
            print(f"\n⏳ 正在监视 {filled_count} 笔 Sniper 委托，等待实盘成交回报...")
            timeout    = 30
            start_wait = time.time()
            while time.time() - start_wait < timeout:
                all_done = all(info['status'] != 'pending' for info in PENDING_ORDERS.values())
                if all_done:
                    break
                time.sleep(1)

            # ── 【手术B】清道夫（The Sweeper）：超时后冷血处决 ───────────────
            # 原则：有成交则写账（哪怕1股）；有剩余则撤单释放资金；两者均做。
            for oid, info in PENDING_ORDERS.items():
                if info['status'] not in ('pending', 'partial'):
                    continue  # 已完结（filled/error）跳过

                code_sw    = info['code']
                name_sw    = info['name']
                total_fill = info['fill_qty_total']
                ordered    = info['qty']
                remaining  = ordered - total_fill

                # Step B-1：物理查询真实成交量（防止回调丢失导致漏记）
                try:
                    real_trades = xt_trader.query_stock_trades(acc) or []
                    real_fill   = sum(
                        t.traded_volume for t in real_trades
                        if getattr(t, 'order_id', None) == oid
                    )
                    if real_fill > total_fill:
                        # 发现回调丢失的成交量：物理补写账本
                        real_amount = sum(
                            t.traded_price * t.traded_volume for t in real_trades
                            if getattr(t, 'order_id', None) == oid and t.traded_volume > 0
                        )
                        vwap_sw = round(real_amount / real_fill, 4) if real_fill > 0 else info['price']
                        _write_holding(code_sw, name_sw, info['price'], ordered, vwap_sw, real_fill)
                        print(
                            f"🧹 [扫地僧·补录] {name_sw}({code_sw}) "
                            f"回调漏记修正: 账本={total_fill}股 → 实盘={real_fill}股 @ VWAP={vwap_sw}"
                        )
                        total_fill = real_fill
                        remaining  = ordered - total_fill
                except Exception as _qe:
                    print(f"⚠️ [扫地僧] {code_sw} 物理查询失败，跳过补录: {_qe}")

                # Step B-2：若有剩余未成交 → 无条件撤单，释放资金防幽灵仓位
                if remaining > 0:
                    try:
                        cancel_res = xt_trader.cancel_order_stock(acc, oid)
                        print(
                            f"🚨 [扫地僧·撤单] {name_sw}({code_sw}) "
                            f"单号 {oid} 剩余 {remaining}股未成交，已强制撤单 (res={cancel_res})，"
                            f"防止幽灵挂单占用资金。"
                        )
                        send_n8n_alert(
                            "🧹 Sniper 扫地僧撤单",
                            f"{name_sw}({code_sw}) 剩余{remaining}股超时未成，已撤单。"
                            f"实际入账: {total_fill}股"
                        )
                    except Exception as _ce:
                        print(f"⚠️ [扫地僧] {code_sw} 撤单指令异常: {_ce}")

                # Step B-3：若有任何成交（哪怕1股），确保账本已正确写入
                if total_fill > 0:
                    # _write_holding 内置锁，重复调用会覆写但数据一致，安全
                    vwap_final = (
                        round(info['fill_amount_total'] / total_fill, 4)
                        if info['fill_amount_total'] > 0 else info['price']
                    )
                    _write_holding(code_sw, name_sw, info['price'], ordered, vwap_final, total_fill)
                    print(
                        f"✅ [扫地僧·终稿] {name_sw}({code_sw}) "
                        f"账本最终锁定: {total_fill}股 @ VWAP={vwap_final}"
                    )
                    record_action(
                        strategy="Sniper", action="买入(Sweeper兜底)", target=code_sw,
                        price=vwap_final, reason=f"Sweeper终稿: {total_fill}/{ordered}股成交",
                        extra={"qty": total_fill, "remaining_cancelled": remaining, "seq": oid}
                    )
                else:
                    print(
                        f"📭 [扫地僧] {name_sw}({code_sw}) "
                        f"单号 {oid} 0股成交，账本无需写入。"
                    )

        if filled_count == 0:
            print("🏁 Sniper 全轮扫描结束，未能撮合任何有效目标。")
        else:
            print(f"🏁 Sniper 任务执行完毕，共计出击 {filled_count} 次（1/{n} 均分，每档 {capital_per_target:.0f} 元）。")
    finally:
        release_lock(LOCK_FILE)


if __name__ == "__main__":
    acc_id   = os.getenv("ACCOUNT_ID")
    qmt_path = os.getenv("QMT_PATH")

    if not acc_id or not qmt_path:
        print("❌ 环境变量缺失 ACCOUNT_ID 或 QMT_PATH")
    else:
        session_id = random.randint(100000, 999999)
        xt_trader = XtQuantTrader(qmt_path, session_id)
        xt_trader.start()
        time.sleep(5)  # xtquant 规范：start() 后必须等待 ≥3秒再 connect()
        res = xt_trader.connect()
        if res == 0:
            # ── 注册 Sniper 专属回调 ──
            from xtquant.xttype import StockAccount
            acc = StockAccount(acc_id)          # 必须先建账户对象
            xt_trader.register_callback(SniperCallback())
            xt_trader.subscribe(acc)            # 再订阅，才能接收回调
            execute_sniper_entry(xt_trader, acc)
            
            # 停止前额外等待几秒，确保日志刷完
            time.sleep(2)
            xt_trader.stop()
        else:
            print("❌ QMT 链路连接失败，请确认 MiniQMT 已启动。")