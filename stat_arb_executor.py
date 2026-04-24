# -*- coding: utf-8 -*-
"""
统计套利波段执行器 (生产修复版)
运行环境: Z690 (Quant-PC) Windows 主机 - miniQMT
业务逻辑: 严格执行 Ernest P. Chan 统计套利均值回归
"""
import os
import json
import time
import pandas as pd
import math
from datetime import datetime
from dotenv import load_dotenv
from xtquant import xtdata, xtconstant
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

# ================= 核心配置 =================
load_dotenv()
_DIR = os.path.dirname(os.path.abspath(__file__))
# 优先从 .env 读取 STATE_DIR，否则使用脚本目录下的 .state
STATE_DIR = os.getenv("STATE_DIR", os.path.join(_DIR, ".state"))

# 执行器只接受盘后计算好的配对参数，绝不能直接读取原始单边 YAML
PAIRS_CSV = os.path.join(STATE_DIR, "tradable_pairs_halflife.csv")
POSITIONS_JSON = os.path.join(STATE_DIR, "stat_arb_positions.json")

LOOKBACK_WINDOW = 60
Z_SCORE_ENTRY = 2.0   
Z_SCORE_EXIT  = 0.0   
MAX_CONCURRENT_PAIRS = 3  

# 新增：独立资金池与等权配额
STAT_ARB_TOTAL_CAPITAL = 60000.0  # 统计套利总资金池 6 万元

# QMT 交易配置 (从 .env 自动注入)
MINI_QMT_PATH = os.getenv("QMT_PATH", r"C:\全量行情交易终端\userdata_mini")
ACCOUNT_ID = os.getenv("ACCOUNT_ID", "YOUR_ACCOUNT_ID")
# ============================================

from quant_logger import record_action

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

class MyXtTraderCallback(XtQuantTraderCallback):
    def on_stock_order(self, order):
        print(f"✅ 委托回报: {order.stock_code} {order.order_status}")

def init_trader():
    """初始化 QMT 交易接口"""
    session_id = int(time.time())
    xt_trader = XtQuantTrader(MINI_QMT_PATH, session_id)
    acc = StockAccount(ACCOUNT_ID)
    callback = MyXtTraderCallback()
    xt_trader.register_callback(callback)
    xt_trader.start()
    res = xt_trader.connect()
    if res != 0:
        raise Exception(f"❌ QMT 连接失败 (Path: {MINI_QMT_PATH})，请检查客户端是否登录。")
    xt_trader.subscribe(acc)
    return xt_trader, acc

def load_positions():
    if os.path.exists(POSITIONS_JSON):
        with open(POSITIONS_JSON, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_positions(positions):
    os.makedirs(os.path.dirname(POSITIONS_JSON), exist_ok=True)
    with open(POSITIONS_JSON, 'w', encoding='utf-8') as f:
        json.dump(positions, f, indent=4, ensure_ascii=False)

def calculate_current_zscore(code_A, code_B, hedge_ratio):
    """计算当天的实时 Z-Score，严格隔离历史与实时数据，消除未来函数"""
    # 历史数据已本地化，此处调用极其快速
    today_str = datetime.now().strftime('%Y%m%d')
    start_time = (datetime.now() - pd.Timedelta(days=120)).strftime('%Y%m%d')
    
    xtdata.download_history_data2([code_A, code_B], '1d', start_time, today_str)
    data = xtdata.get_market_data_ex(['close'], [code_A, code_B], '1d')
    
    if data[code_A].empty or data[code_B].empty:
        return None, None, None
        
    df_merged = pd.concat([data[code_A]['close'], data[code_B]['close']], axis=1).dropna()
    
    # 【核心逻辑修复】必须剔除今日数据，否则当天的极端异动会污染均值和标准差
    df_history = df_merged[df_merged.index < today_str].tail(LOOKBACK_WINDOW)
    
    if len(df_history) < LOOKBACK_WINDOW * 0.8:
        return None, None, None
        
    spreads = df_history.iloc[:, 0] - hedge_ratio * df_history.iloc[:, 1]
    mu, sigma = spreads.mean(), spreads.std()
    
    xtdata.subscribe_quote(code_A, period='tick', count=1)
    xtdata.subscribe_quote(code_B, period='tick', count=1)
    time.sleep(0.1) 
    
    tick_A, tick_B = xtdata.get_full_tick([code_A]), xtdata.get_full_tick([code_B])
    if code_A not in tick_A or code_B not in tick_B: return None, None, None
    
    price_A = tick_A[code_A]['lastPrice']
    price_B = tick_B[code_B]['lastPrice']
    
    # 用历史参数验证当前价差，避免方差扩大导致的漏单
    current_spread = price_A - hedge_ratio * price_B
    z_score = (current_spread - mu) / sigma
    
    return z_score, price_A, price_B

def execute_trade(xt_trader, acc, code, action, price, volume=100):
    """QMT 真实下单封装 (返回单号)"""
    # 23:买, 24:卖
    order_type = xtconstant.STOCK_BUY if action == "买入" else xtconstant.STOCK_SELL
    # A股买入必须是100的整数倍
    volume = math.ceil(volume / 100) * 100 if action == "买入" else volume
    
    # 切换为同步下单以获取订单 ID
    seq = xt_trader.order_stock(acc, code, order_type, volume, xtconstant.FIX_PRICE, price, "StatArb", "Auto")
    return seq

def check_and_exit_positions(positions, xt_trader, acc):
    print("\n🛡️ [阶段 1] 启动持仓退出审查...")
    today = datetime.now()
    keys_to_remove = []
    
    for pair_key, info in list(positions.items()):
        entry_date = datetime.strptime(info['entry_date'], "%Y-%m-%d")
        days_held = (today - entry_date).days
        half_life = info['half_life']
        code_A, code_B, hedge_ratio = info['code_A'], info['code_B'], info['hedge_ratio']
        trade_code = info['trade_code'] 
        
        z_score, price_A, price_B = calculate_current_zscore(code_A, code_B, hedge_ratio)
        if z_score is None: continue
        
        print(f"📦 持仓: {pair_key} ({trade_code}) | 持仓天数: {days_held}/{half_life*2} | Z-Score: {z_score:.2f}")
        
        exit_triggered = False
        current_price = price_A if info['side'] == 'A' else price_B
        reason = ""

        if days_held > (half_life * 2):
            exit_triggered, reason = True, "超过两倍半衰期强制斩仓"
        elif info['side'] == 'A' and z_score >= Z_SCORE_EXIT:
            exit_triggered, reason = True, "Z-Score 回归均值止盈"
        elif info['side'] == 'B' and z_score <= Z_SCORE_EXIT:
            exit_triggered, reason = True, "Z-Score 回归均值止盈"

        if exit_triggered:
            # 🛡️ Pattern 3 & V6: 严格虚拟账本 + 原子化
            sell_qty = info.get('volume', 100) 
            
            def exit_save():
                del positions[pair_key]
                save_positions(positions)

            seq = safe_execute_and_lock(
                xt_trader, acc, trade_code, xtconstant.STOCK_SELL,
                sell_qty, current_price, "StatArb", "Exit", exit_save
            )
            print(f"💰 [回归离场] {pair_key} 卖出 {trade_code} | 价格: {current_price} | 数量: {sell_qty} | 理由: {reason} | 单号: {seq}")
            record_action(strategy="StatArb", action="平仓", target=trade_code, price=current_price, reason=reason, extra={"volume": sell_qty, "seq": seq})

    return positions

def scan_for_new_entries(positions, xt_trader, acc):
    print("\n🔭 [阶段 2] 启动新目标雷达扫描...")
    
    if len(positions) >= MAX_CONCURRENT_PAIRS:
        msg = f"🚫 [防线一] 触发席位锁 ({len(positions)}/{MAX_CONCURRENT_PAIRS})，跳过开仓。"
        print(msg)
        record_action(strategy="StatArb", action="拦截", target="Global", reason=msg)
        return positions

    if not os.path.exists(PAIRS_CSV): 
        print("⚠️ 缺少配对参数文件，停止扫描。")
        return positions
        
    df_pairs = pd.read_csv(PAIRS_CSV)
    
    for _, row in df_pairs.iterrows():
        # 支持两种列名格式 (Code_A vs code_A)
        code_A = row.get('Code_A', row.get('code_A'))
        code_B = row.get('Code_B', row.get('code_B'))
        half_life = row.get('Half_Life_Days', row.get('half_life', 10))
        hedge_ratio = row.get('Hedge_Ratio', row.get('hedge_ratio', 1.0))
        
        # 【键值修复】使用组合 ID 作为账本标识，防止单标的覆盖
        pair_key = f"{code_A}_{code_B}"
        if pair_key in positions: continue
            
        z_score, price_A, price_B = calculate_current_zscore(code_A, code_B, hedge_ratio)
        if z_score is None: continue
        
        # 🛡️ Pattern 1: 防御性行情获取
        tick_A = xtdata.get_full_tick([code_A]).get(code_A, {})
        tick_B = xtdata.get_full_tick([code_B]).get(code_B, {})
        pre_A = tick_A.get('lastClose', tick_A.get('preClose', tick_A.get('lastPrice', 0)))
        pre_B = tick_B.get('lastClose', tick_B.get('preClose', tick_B.get('lastPrice', 0)))

        if pre_A > 0 and pre_B > 0:
            ret_A = (price_A - pre_A) / pre_A
            ret_B = (price_B - pre_B) / pre_B
            
            # 1. 防接飞刀：跌幅超 7%
            if ret_A < -0.07 or ret_B < -0.07:
                msg = f"🛡️ [防线三] 下行波动否决：跌幅 > 7%，禁入。"
                print(msg)
                record_action(strategy="StatArb", action="拦截", target=pair_key, reason=msg)
                continue
                
            # 2. 防幽灵仓位：涨幅超 9.5%（涨停板买不到）
            if ret_A > 0.095 or ret_B > 0.095:
                msg = f"🛡️ [防线三] 上行涨停拦截：超过 9.5% 接近涨停，放弃。"
                print(msg)
                record_action(strategy="StatArb", action="拦截", target=pair_key, reason=msg)
                continue

        # 注意：在A股受限做空机制下，仅做多严重低估的单腿（Beta敞口极大）
        # 严格意义上这退化为均值回归，而非纯粹的中性统计套利
        target_code, target_side, entry_price = None, None, None
        if z_score < -Z_SCORE_ENTRY:
            target_code, target_side, entry_price = code_A, "A", price_A
        elif z_score > Z_SCORE_ENTRY:
            target_code, target_side, entry_price = code_B, "B", price_B

        if target_code:
            # 动态资金分配计算
            each_pair_capital = STAT_ARB_TOTAL_CAPITAL / MAX_CONCURRENT_PAIRS
            buy_qty = int((each_pair_capital / entry_price) // 100 * 100)
            
            if buy_qty < 100:
                print(f"⚠️ [资金防线] {target_code} 价格 {entry_price} 过高，分配资金不足1手，放弃买入。")
                continue
                
            # 🛡️ Pattern 2 & V6: 原子化发单与落盘
            def entry_save():
                positions[pair_key] = {
                    "side": target_side,
                    "trade_code": target_code,
                    "code_A": code_A, "code_B": code_B,
                    "hedge_ratio": hedge_ratio, "half_life": half_life,
                    "entry_date": datetime.now().strftime("%Y-%m-%d"),
                    "entry_price": entry_price, 
                    "entry_zscore": z_score,
                    "volume": buy_qty
                }
                save_positions(positions)

            seq = safe_execute_and_lock(
                xt_trader, acc, target_code, xtconstant.STOCK_BUY,
                buy_qty, entry_price, "StatArb", "Auto", entry_save
            )
            print(f"🎯 [猎杀信号] {pair_key} 成功发送买入指令 (数量: {buy_qty}, 单号: {seq})")
            record_action(strategy="StatArb", action="买入", target=target_code, price=entry_price, extra={"volume": buy_qty, "seq": seq})

            if len(positions) >= MAX_CONCURRENT_PAIRS: break 

    return positions

def is_gap_break():
    benchmark = "510300.SH" 
    today_str = datetime.now().strftime('%Y%m%d')
    start_time = (datetime.now() - pd.Timedelta(days=5)).strftime('%Y%m%d')
    xtdata.download_history_data2([benchmark], '1d', start_time, today_str)
    
    tick = xtdata.get_full_tick([benchmark]).get(benchmark, {})
    if not tick: return False
        
    open_p = tick.get('open', 0)
    # 🛡️ Pattern 1: 防御性回归
    pre_close = tick.get('lastClose', tick.get('preClose', tick.get('lastPrice', 0)))
    if pre_close == 0 or open_p == 0: return False
    gap_ratio = abs(open_p - pre_close) / pre_close
    
    if gap_ratio > 0.03:
        msg = f"🔥 [防线二] 宏观熔断！{benchmark} 跳空 {gap_ratio:.2%} 判定为 Regime Shift，今日禁开新仓。"
        print(msg)
        record_action(strategy="StatArb", action="熔断", target=benchmark, reason=msg)
        return True
    return False

def run_stat_arb():
    LOCK_FILE = os.path.join(STATE_DIR, "stat_arb.lock")
    if not acquire_lock_with_ttl(LOCK_FILE): return

    try:
        print(f"====== 统计套利引擎启动 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ======")
        xt_trader, acc = init_trader()
        positions = load_positions()
        
        positions = check_and_exit_positions(positions, xt_trader, acc)
        if is_gap_break():
            print("⏸️ 阶段 2 已被熔断，仅执行减仓逻辑。")
        else:
            positions = scan_for_new_entries(positions, xt_trader, acc)
        
        save_positions(positions)
        xt_trader.stop()
        print("====== 引擎物理静默 ======\n")
    finally:
        release_lock(LOCK_FILE)

if __name__ == "__main__":
    run_stat_arb()