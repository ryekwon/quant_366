import json
import time
import os
import csv
from datetime import datetime
from pathlib import Path
from xtquant import xtdata, xtconstant
from quant_logger import record_action
from xtquant.xttrader import XtQuantTrader
import random
import msvcrt
from dotenv import load_dotenv
import sys
try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

# 🛡️ 解决 Windows 控制台打印 Emoji 导致的 UnicodeEncodeError
if sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        # 兼容旧版本 Python 3.6
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

load_dotenv()

HOLDINGS_FILE   = r"Z:\QuantpC_Workspace\Quant_Pilot\.state\sniper_holdings.json"
LOCK_FILE       = r"Z:\QuantpC_Workspace\Quant_Pilot\.state\sniper_exit.lock"
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")

TAKE_PROFIT = 1.05  # +5%
STOP_LOSS = 0.95    # -5%

# ─── The Auditor — 游走核查程序 ────────────────────────────────────────────
# 只收集数据，不参与交易决策。
_STATE_DIR    = Path(HOLDINGS_FILE).parent
TELEMETRY_CSV = _STATE_DIR / "sniper_telemetry.csv"
_TELEMETRY_FIELDS = [
    "exit_ts",      # 出场时间戳
    "code",         # 股票代码
    "name",         # 股票名称
    "entry_date",   # 入场日期
    "entry_price",  # 入场（成交）价
    "exit_price",   # 出场触发价（lastPrice）
    "exit_reason",  # 出场原因：止盈 / 止损 / 时间死线
    "pnl_pct",      # 盈亏百分比（相对入场价）
    "intraday_high",# T+1日内（从建仓当日至出场当日）最高价  → 计算 MFE
    "mfe_pct",      # Maximum Favorable Excursion 最大有利偏移 %
    "intraday_low", # T+1日内最低价 → 计算 MAE
    "mae_pct",      # Maximum Adverse Excursion 最大不利偏移 %
]


def _write_sniper_telemetry(
    code: str,
    name: str,
    entry_date: str,
    entry_price: float,
    exit_price: float,
    exit_reason: str,
    pnl_pct: float,
    entry_timestamp: str = "",   # 可选：精确入场时间戳 "YYYY-MM-DD HH:MM:SS"，不传则 fallback 到入场日开盘前
):
    """
    The Auditor：在每次 Sniper 平仓后，将物理切面数据追写到 CSV 探针。

    【高精度-v2】MFE / MAE 使用 1m 分钟线 + 绝对时间切割计算：
    - 拉取过去 1000 根前复权分钟线（约 4 个交易日），坐标系与外部软件对齐
    - 以 entry_timestamp（或 entry_date 09:25）为下界，只取"建仓时间点之后"的 bar
    - 从切割后的 high/low 提取真实物理极值
    """
    try:
        import pandas as pd
        import traceback as _tb

        # ── §1  计算切割基准时间戳（全程 tz-naive，杜绝时区偏移陷阱）──────────────
        # 铁律：涉及 > / < / >= / <= 切片，两端必须都是 pd.to_datetime() 朴素时间戳
        _cutoff_dt = None
        try:
            if entry_timestamp:
                # pd.to_datetime 自动解析 "YYYY-MM-DD HH:MM:SS" → naive Timestamp
                _cutoff_dt = pd.to_datetime(entry_timestamp)
            else:
                # Fallback：入场日 09:25（集合竞价结束，连续竞价刚开始前）
                _cutoff_dt = pd.to_datetime(f"{entry_date} 09:25:00")
        except Exception as _te:
            print(f"  ⚠️ [Auditor] {code} 时间戳解析失败，将使用全量数据: {_te}")

        # ── §2  分钟级物理切割（降维打击）──────────────────────────────────
        intraday_high = 0.0
        intraday_low  = 0.0
        mfe_pct       = 0.0
        mae_pct       = 0.0

        def _parse_qmt_index_naive(idx, period: str):
            """
            QMT index 格式因 period 而异，统一输出 tz-naive DatetimeIndex：
              1m  → YYYYMMDDHHMMSS 整数 → pd.to_datetime(..., format="%Y%m%d%H%M%S")
              1d  → epoch-ms 整数       → to_datetime(unit="ms") + tz剥离
            永远不做 tz_localize / tz_convert，保持朴素时间戳，切片两端对齐。
            """
            _sample = int(idx[0])
            if 20_000_101_000000 <= _sample <= 21_000_101_000000:
                # 1m: YYYYMMDDHHMMSS → naive datetime（不加时区）
                return pd.to_datetime(idx.astype(str), format="%Y%m%d%H%M%S")
            else:
                # 1d: epoch-ms → UTC → Asia/Shanghai → 剥离时区 → naive
                return (
                    pd.to_datetime(idx, unit="ms")
                    .tz_localize("UTC")
                    .tz_convert("Asia/Shanghai")
                    .tz_localize(None)          # 剥离时区，保持本地朴素时间
                )

        try:
            from xtquant import xtdata

            # 【Fix-1】主动订阅 1m 数据，确保本地缓存被填充
            try:
                xtdata.subscribe_quote(code, period="1m", count=-1)
            except Exception as _sub_e:
                print(f"  ⚠️ [Auditor] {code} 订阅 1m 数据失败（不影响读取）: {_sub_e}")

            # 前复权 1m 分钟线，拉 1000 根 ≈ 约 4 个完整交易日，覆盖 Sniper 最长持仓期
            raw_1m = xtdata.get_market_data_ex(
                field_list=["high", "low"],
                stock_list=[code],
                period="1m",
                count=1000,
                dividend_type="front",
            )
            df_1m = raw_1m.get(code)
            if df_1m is not None and not df_1m.empty:
                # 1m index: YYYYMMDDHHMMSS → naive datetime（两端朴素，切割物理可信）
                df_1m.index = _parse_qmt_index_naive(df_1m.index, period="1m")

                # 绝对时间切割：entry_timestamp 之后（严格大于，不包含入场那根未完成的 bar）
                if _cutoff_dt is not None:
                    df_trade = df_1m[df_1m.index > _cutoff_dt]
                else:
                    df_trade = df_1m   # 解析失败时兜底

                if not df_trade.empty:
                    intraday_high = float(df_trade["high"].max())
                    intraday_low  = float(df_trade["low"].min())
                    if entry_price > 0:
                        mfe_pct = round((intraday_high / entry_price - 1) * 100, 4)
                        mae_pct = round((intraday_low  / entry_price - 1) * 100, 4)
                    print(
                        f"  🔬 [Auditor] {code} 分钟线切割: "
                        f"切割基准={_cutoff_dt}, "
                        f"有效Bar数={len(df_trade)}, "
                        f"最高={intraday_high:.3f}, 最低={intraday_low:.3f}"
                    )
                else:
                    # 【Fix-2】1m 切割为空时，降级使用日线数据兜底
                    print(f"  ⚠️ [Auditor] {code} 1m 切割后无 Bar（基准={_cutoff_dt}），降级日线兜底")
                    raw_1d = xtdata.get_market_data_ex(
                        field_list=["high", "low"],
                        stock_list=[code],
                        period="1d",
                        count=5,
                        dividend_type="front",
                    )
                    df_1d = raw_1d.get(code)
                    if df_1d is not None and not df_1d.empty:
                        # 日线切割：entry_date 当天及之后的 bar（同样 naive）
                        df_1d.index = _parse_qmt_index_naive(df_1d.index, period="1d")
                        _entry_day = pd.to_datetime(f"{entry_date} 00:00:00")
                        df_1d_trade = df_1d[df_1d.index >= _entry_day]
                        if not df_1d_trade.empty:
                            intraday_high = float(df_1d_trade["high"].max())
                            intraday_low  = float(df_1d_trade["low"].min())
                            if entry_price > 0:
                                mfe_pct = round((intraday_high / entry_price - 1) * 100, 4)
                                mae_pct = round((intraday_low  / entry_price - 1) * 100, 4)
                            print(
                                f"  🔬 [Auditor-1d] {code} 日线兜底: "
                                f"Bar数={len(df_1d_trade)}, "
                                f"最高={intraday_high:.3f}, 最低={intraday_low:.3f}"
                            )
                        else:
                            print(f"  ⚠️ [Auditor] {code} 日线也无有效 Bar，MFE/MAE 置零")
                    else:
                        print(f"  ⚠️ [Auditor] {code} 日线数据也返回空，MFE/MAE 置零")
            else:
                # 【Fix-2b】1m 整体为空，直接走日线兜底
                print(f"  ⚠️ [Auditor] {code} 1m 分钟线返回空，降级日线兜底")
                raw_1d = xtdata.get_market_data_ex(
                    field_list=["high", "low"],
                    stock_list=[code],
                    period="1d",
                    count=5,
                    dividend_type="front",
                )
                df_1d = raw_1d.get(code)
                if df_1d is not None and not df_1d.empty:
                    df_1d.index = _parse_qmt_index_naive(df_1d.index, period="1d")
                    _entry_day = pd.to_datetime(f"{entry_date} 00:00:00")
                    df_1d_trade = df_1d[df_1d.index >= _entry_day]
                    if not df_1d_trade.empty:
                        intraday_high = float(df_1d_trade["high"].max())
                        intraday_low  = float(df_1d_trade["low"].min())
                        if entry_price > 0:
                            mfe_pct = round((intraday_high / entry_price - 1) * 100, 4)
                            mae_pct = round((intraday_low  / entry_price - 1) * 100, 4)
                        print(
                            f"  🔬 [Auditor-1d] {code} 日线兜底: "
                            f"Bar数={len(df_1d_trade)}, "
                            f"最高={intraday_high:.3f}, 最低={intraday_low:.3f}"
                        )
        except Exception as _e:
            # 【Fix-3】暴露完整 traceback，帮助快速定位真实根因
            print(f"  ⚠️ [Auditor] {code} MFE/MAE 数据拉取失败，写入 0:")
            print(_tb.format_exc())


        # ── §3  追写 CSV ──────────────────────────────────────────────────
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        write_header = not TELEMETRY_CSV.exists()
        exit_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with open(TELEMETRY_CSV, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_TELEMETRY_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow({
                "exit_ts":       exit_ts,
                "code":          code,
                "name":          name,
                "entry_date":    entry_date,
                "entry_price":   round(entry_price, 4),
                "exit_price":    round(exit_price, 4),
                "exit_reason":   exit_reason,
                "pnl_pct":       round(pnl_pct, 4),
                "intraday_high": round(intraday_high, 4),
                "mfe_pct":       mfe_pct,
                "intraday_low":  round(intraday_low, 4),
                "mae_pct":       mae_pct,
            })
        print(
            f"  📊 [Auditor] {code} 复盘数据已记录 "
            f"| MFE={mfe_pct:+.2f}% (最高{intraday_high:.3f}) "
            f"| MAE={mae_pct:+.2f}% (最低{intraday_low:.3f}) "
            f"| 出场={exit_reason}"
        )
    except Exception as _e:
        print(f"  ⚠️ [Auditor] 写入 telemetry 失败（不影响主逻辑）: {_e}")

def send_n8n_alert(title: str, message: str):
    """向 N8N Webhook 推送告警（失败静默，不阻断主逻辑）"""
    if not N8N_WEBHOOK_URL or not _HAS_REQUESTS:
        return
    try:
        payload = {
            "title": title,
            "message": message,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        resp = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"⚠️ [Sniper] N8N 告警发送失败 (HTTP {resp.status_code}): {message}")
    except Exception as e:
        print(f"⚠️ [Sniper] N8N 告警发送异常: {e}")
 
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

def safe_execute_and_lock(xt_trader, acc, code, order_type, qty, price, strategy_name, order_remark, save_func):
    """原子化下单与落盘封装"""
    try:
        res = xt_trader.order_stock(acc, code, order_type, qty, xtconstant.FIX_PRICE, price, strategy_name, order_remark)
        if res > 0:
            save_func()
        return res
    except Exception as e:
        print(f"❌ Execution Critical Error: {e}")
        return -1

def _load_holdings():
    if os.path.exists(HOLDINGS_FILE):
        try:
            with open(HOLDINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except: return {}
    return {}

def _write_holdings(holdings):
    with open(HOLDINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(holdings, f, ensure_ascii=False, indent=4)

def reconcile_positions_with_real(xt_trader, account, local_json_path):
    """
    物理状态强制对齐引擎 (在 while True 之前调用)
    """
    print(f"🔄 [Reconciliation] 开始进行物理实盘与本地账本 {local_json_path} 的强制对齐...")
    
    # 1. 读取本地日记本
    local_state = {}
    if os.path.exists(local_json_path):
        try:
            with open(local_json_path, 'r', encoding='utf-8') as f:
                local_state = json.load(f)
        except Exception as e:
            print(f"⚠️ 读取本地账本失败: {e}")

    # 2. 拉取绝对物理真相
    try:
        real_positions = xt_trader.query_stock_positions(account)
    except Exception as e:
        print(f"❌ 物理持仓查询失败，跳过对齐: {e}")
        return local_state

    # 过滤掉 ETF (假设 Sniper 只做股票，或者根据需要调整)
    # 这里简单处理：只要是持仓量 > 0 且在 local_state 中有记录，或者是非 ETF 的股票
    real_dict = {pos.stock_code: pos for pos in real_positions if pos.volume > 0}
    updated_state = {}

    # 获取 T0 的状态文件，避免把 T0 的标的误抓进 Sniper
    T0_STATE_FILE = r"Z:\QuantpC_Workspace\Quant_Pilot\.state\grid_state.json"
    t0_codes = []
    if os.path.exists(T0_STATE_FILE):
        try:
            with open(T0_STATE_FILE, 'r') as f:
                t0_codes = list(json.load(f).keys())
        except: pass

    # ── 领土防线定义 ────────────
    # Sniper 只收容 股票 (30/60/00/688等)，绝对不碰 ETF/基金 (1/5开头)
    # 同时也避开 T0 YAML 中明确定义的标的
    
    # 3. 遍历实盘真相，处理正向补录与同步
    for code, pos in real_dict.items():
        is_etf = code.startswith(('1', '5', '51', '15'))
        if is_etf:
            continue
            
        if code in t0_codes:
            continue
            
        if code in local_state:
            # 状态同步：强行纠正仓位和成本，date 字段保留原始内容
            updated_state[code] = local_state[code]
            updated_state[code]['qty'] = int(pos.volume)
            updated_state[code]['buy_price'] = float(pos.open_price)
            # 保证 date 字段存在（防止旧数据缺字段）
            if 'date' not in updated_state[code]:
                can_use = getattr(pos, 'can_use_volume', 0)
                if can_use > 0:
                    from datetime import timedelta
                    updated_state[code]['date'] = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                else:
                    updated_state[code]['date'] = datetime.now().strftime("%Y-%m-%d")
        else:
            # 孤儿资产被发现，强制收容入账 (仅限非 T0 标的)
            print(f"⚠️ [Ghost Found] 发现 Sniper 遗漏资产 {code}，正在反向生成底仓账本...")
            detail = xtdata.get_instrument_detail(code) or {}
            target_name = detail.get('InstrumentName', code)

            # 关键：用 can_use_volume 推断真实买入日期
            # can_use_volume > 0 → 已可卖出，证明是前交易日建仓，用昨日日期防 T+1 防线误封
            # can_use_volume == 0 → 今日建仓，T+1 锁定中，用今日日期
            can_use = getattr(pos, 'can_use_volume', 0)
            if can_use > 0:
                from datetime import timedelta
                inferred_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                inferred_date = datetime.now().strftime("%Y-%m-%d")

            print(f"   推断买入日: {inferred_date} (can_use_volume={can_use})，T+1防线将正确生效")
            updated_state[code] = {
                "name": target_name,
                "buy_price": float(pos.open_price),
                "qty": int(pos.volume),
                "ordered_price": float(pos.open_price),
                "ordered_qty": int(pos.volume),
                "date": inferred_date
            }
            
    # 4. 遍历本地账本，处理反向抹除 (实盘已没货了)
    for code in local_state.keys():
        if code not in real_dict:
            print(f"🗑️ [Ghost Cleared] 发现本地幽灵资产 {code} (实盘已清空)，执行物理抹除...")
            
    # 5. 覆写物理硬盘
    try:
        with open(local_json_path, 'w', encoding='utf-8') as f:
            json.dump(updated_state, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ 账本覆写失败: {e}")
        
    print(f"✅ [Reconciliation] 对齐完毕。Sniper 当前监管数: {len(updated_state)}")
    return updated_state

def get_session():
    acc_id = os.getenv("ACCOUNT_ID")
    qmt_path = os.getenv("QMT_PATH")
    if not acc_id or not qmt_path:
        return None, None
    session_id = random.randint(100000, 999999)
    xt_trader = XtQuantTrader(qmt_path, session_id)
    xt_trader.start()
    res = xt_trader.connect()
    if res == 0:
        from xtquant.xttype import StockAccount
        acc = StockAccount(acc_id)
        return xt_trader, acc
    return None, None

def run_exit_guard():
    LOCK_FILE = os.path.join(os.path.dirname(HOLDINGS_FILE), "sniper_exit.lock")
    if not acquire_lock_with_ttl(LOCK_FILE): return

    try:
        print("🛡️ Sniper 退出卫士已上线，进入冷血巡逻模式...")
        xt_trader, acc = get_session()
        if not xt_trader: return

        # 物理状态强制对齐
        holdings = reconcile_positions_with_real(xt_trader, acc, HOLDINGS_FILE)
        if not holdings:
            print("🏁 无狙击目标持仓（对齐后），卫士下线。")
            return

        active_codes = list(holdings.keys())
        for code in active_codes:
            xtdata.subscribe_quote(code, period='tick')

        _holding_log_last = 0   # HOLDING_LOG 上次写入的 time.time()
        try:
            while True:
                holdings = _load_holdings()
                if not holdings:
                    print("🏁 所有狙击猎物已清空，卫士下线。")
                    return

                active_codes = list(holdings.keys())
                now_hhmm = datetime.now().strftime("%H%M")
                ticks = xtdata.get_full_tick(active_codes)
                
                for code in active_codes:
                    tick = ticks.get(code, {})
                    if not tick or tick.get('volume', 0) == 0:
                        continue
                    
                    info = holdings[code]
                    current_price = tick.get('lastPrice', 0)

                    # ── 🔍 运行时日期自愈校验 ────────────────────────────────────
                    # 若账本 date==今日 但 QMT 显示 can_use_volume>0（已可卖出）
                    # → 日期字段被错误写入（常见于 reconcile 幽灵补录 Bug）
                    # → 自动纠正为昨日，防止 T+1 防线错误封锁合法的平仓触发
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    if info.get('date', '') == today_str:
                        try:
                            pos_list = xt_trader.query_stock_positions(acc) or []
                            real_pos  = next((p for p in pos_list if p.stock_code == code), None)
                            can_use   = int(getattr(real_pos, 'can_use_volume', 0)) if real_pos else 0
                            if can_use > 0:
                                from datetime import timedelta
                                corrected = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                                holdings[code]['date'] = corrected
                                _write_holdings(holdings)
                                print(f"  🔧 [日期自愈] {code} 账本 date 错误为今日，"
                                      f"但 can_use_volume={can_use}，已自动纠正为 {corrected}")
                        except Exception as _e:
                            print(f"  ⚠️ [日期自愈] {code} 查询失败，跳过校验: {_e}")

                    # T+1 防线（使用校验/纠正后的最新 date）
                    if holdings.get(code, {}).get('date', '') == today_str:
                        continue


                    bid1 = tick.get('bidPrice', [current_price])[0]
                    sell_price = round(bid1 - 0.01, 2) if bid1 > 0 else current_price
                    
                    triggered, reason = False, ""
                    if current_price >= info['buy_price'] * TAKE_PROFIT:
                        triggered, reason = True, "止盈"
                    elif current_price <= info['buy_price'] * STOP_LOSS:
                        triggered, reason = True, "止损"
                    elif now_hhmm >= "1445":
                        triggered, reason = True, "时间死线"

                    if triggered:
                        def exit_save():
                            del holdings[code]
                            _write_holdings(holdings)

                        seq = safe_execute_and_lock(
                            xt_trader, acc, code, xtconstant.STOCK_SELL,
                            info['qty'], sell_price, "Sniper", "Exit", exit_save
                        )
                        _name = info.get('name', code)
                        _pnl_pct = (current_price / info['buy_price'] - 1) * 100
                        _pnl_sign = '+' if _pnl_pct >= 0 else ''
                        _exit_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        # ── 详细触发日志：补全价格/数量/浮盈/时间 ──────────────────
                        print(
                            f"💰 [{_exit_ts}] [猎杀收网] {code}({_name})\n"
                            f"   理由: {reason} | 触发价: {current_price:.3f}"
                            f" | 成本价: {info['buy_price']:.3f}"
                            f" | 浮盈: {_pnl_sign}{_pnl_pct:.2f}%\n"
                            f"   卖出挂单价: {sell_price:.3f} | 数量: {info['qty']}手"
                            f" | 序列号: {seq}"
                        )
                        send_n8n_alert(
                            "🔫 Sniper 平仓战报",
                            f"{code} {_name} | 理由: {reason}\n"
                            f"触发价: {current_price:.3f} | 成本: {info['buy_price']:.3f} | "
                            f"浮盈: {_pnl_sign}{_pnl_pct:.2f}% | 数量: {info['qty']} | Seq={seq}"
                        )
                        record_action(
                            strategy="Sniper", action="平仓", target=code,
                            price=current_price, reason=reason,
                            extra={
                                "name": _name,
                                "qty": info['qty'],
                                "cost_price": info['buy_price'],
                                "sell_price": sell_price,
                                "pnl_pct": round(_pnl_pct, 4),
                                "seq": seq,
                                "exit_ts": _exit_ts,
                            }
                        )
                        # ── The Auditor：平仓后立即记录复盘切面数据 ────────────────
                        # 【Fix-3】传入精确入场时间戳（若账本有记录），切割精度从"入场日 09:25" 提升至分钟级
                        _entry_ts = info.get('entry_ts', '')   # sniper_entry_executor 写入的精确入场时间
                        _write_sniper_telemetry(
                            code=code,
                            name=_name,
                            entry_date=info.get('date', ''),
                            entry_price=info['buy_price'],
                            exit_price=current_price,
                            exit_reason=reason,
                            pnl_pct=_pnl_pct,
                            entry_timestamp=_entry_ts,
                        )
            
                # ── HOLDING_LOG：每 300 秒写一条浮盈快照 ─────────────
                _now_ts = time.time()
                if _now_ts - _holding_log_last >= 300:
                    _holding_log_last = _now_ts
                    for _hl_code, _hl_info in holdings.items():
                        _hl_tick  = ticks.get(_hl_code, {})
                        _hl_price = float(_hl_tick.get('lastPrice', 0))
                        _hl_ep    = float(_hl_info.get('buy_price', 0))
                        if _hl_price > 0 and _hl_ep > 0:
                            _hl_pnl = round((_hl_price / _hl_ep - 1) * 100, 4)
                            try:
                                import csv as _csv_m
                                _hl_is_new = not TELEMETRY_CSV.exists()
                                with open(TELEMETRY_CSV, "a", encoding="utf-8", newline="") as _hf:
                                    _hw = _csv_m.DictWriter(_hf, fieldnames=_TELEMETRY_FIELDS, extrasaction="ignore")
                                    if _hl_is_new:
                                        _hw.writeheader()
                                    _hw.writerow({
                                        "exit_ts":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        "code":        _hl_code,
                                        "name":        _hl_info.get('name', _hl_code),
                                        "entry_date":  _hl_info.get('date', ''),
                                        "entry_price": round(_hl_ep, 4),
                                        "exit_price":  round(_hl_price, 4),
                                        "exit_reason": f"HOLDING_LOG pnl={_hl_pnl:+.2f}%",
                                        "pnl_pct":     _hl_pnl,
                                        "intraday_high": 0, "mfe_pct": 0,
                                        "intraday_low": 0,  "mae_pct": 0,
                                    })
                                print(f"  📊 [HoldingLog] {_hl_code} 现价={_hl_price:.3f} | 入场={_hl_ep:.3f} | pnl={_hl_pnl:+.2f}%")
                            except Exception as _hle:
                                print(f"  ⚠️ [HoldingLog] {_hl_code} 写入失败: {_hle}")

                time.sleep(1)
        except KeyboardInterrupt: pass
        finally:
            xt_trader.stop()
    except Exception as e:
        print(f"🔥 卫士遭遇未知错误: {e}")
    finally:
        release_lock(LOCK_FILE)

if __name__ == "__main__":
    run_exit_guard()