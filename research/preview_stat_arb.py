import os
import sys
import pandas as pd
import json
import time
from datetime import datetime
from xtquant import xtdata
from dotenv import load_dotenv

load_dotenv()

# 处理 Windows 下的编码问题
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

# 配置与 stat_arb_executor.py 保持一致
STATE_DIR = os.getenv("STATE_DIR", r"Z:\QuantpC_Workspace\Quant_Pilot\.state")
PAIRS_CSV = os.path.join(STATE_DIR, "tradable_pairs_halflife.csv")
STAT_ARB_TOTAL_CAPITAL = 60000.0
MAX_CONCURRENT_PAIRS = 3

def preview_allocation():
    print(f"📊 [Stat Arb 模拟分配中心] 启动 @ {datetime.now().strftime('%H:%M:%S')}")
    print(f"💰 总资金池: ¥{STAT_ARB_TOTAL_CAPITAL:,.0f} | 席位数: {MAX_CONCURRENT_PAIRS} | 等权比例: ¥{STAT_ARB_TOTAL_CAPITAL/MAX_CONCURRENT_PAIRS:,.2f}/对")
    print("-" * 80)

    if not os.path.exists(PAIRS_CSV):
        print(f"❌ 找不到配对文件: {PAIRS_CSV}")
        return

    df_pairs = pd.read_csv(PAIRS_CSV)
    
    # 获取所有涉及的代码
    all_codes = list(set(df_pairs['Code_A'].tolist() + df_pairs['Code_B'].tolist()))
    
    # 获取实时价格
    print(f"📡 正在拉取 {len(all_codes)} 只标的的实时行情...")
    ticks = xtdata.get_full_tick(all_codes)
    
    results = []
    for _, row in df_pairs.iterrows():
        code_a, name_a = row['Code_A'], row['Name_A']
        code_b, name_b = row['Code_B'], row['Name_B']
        
        tick_a = ticks.get(code_a, {})
        tick_b = ticks.get(code_b, {})
        
        price_a = tick_a.get('lastPrice', 0)
        price_b = tick_b.get('lastPrice', 0)
        
        each_cap = STAT_ARB_TOTAL_CAPITAL / MAX_CONCURRENT_PAIRS
        
        # 模拟计算 A 腿和 B 腿的买入量
        if price_a > 0:
            qty_a = int((each_cap / price_a) // 100 * 100)
            cost_a = qty_a * price_a
        else:
            qty_a, cost_a = 0, 0
            
        if price_b > 0:
            qty_b = int((each_cap / price_b) // 100 * 100)
            cost_b = qty_b * price_b
        else:
            qty_b, cost_b = 0, 0
            
        # 模拟安全拦截逻辑
        veto_reason = None
        if tick_a and tick_b:
            pre_a = tick_a.get('preClose', 0)
            pre_b = tick_b.get('preClose', 0)
            if pre_a > 0 and pre_b > 0:
                ret_a = (price_a - pre_a) / pre_a
                ret_b = (price_b - pre_b) / pre_b
                if ret_a < -0.07 or ret_b < -0.07:
                    vet_a_str = f"{ret_a:.1%}"
                    vet_b_str = f"{ret_b:.1%}"
                    veto_reason = f"下行波动拦截 (A:{vet_a_str}, B:{vet_b_str} > 7%)"
                elif ret_a > 0.095 or ret_b > 0.095:
                    vet_a_str = f"{ret_a:.1%}"
                    vet_b_str = f"{ret_b:.1%}"
                    veto_reason = f"上行涨停拦截 (A:{vet_a_str}, B:{vet_b_str} > 9.5%)"

        results.append({
            'Pair': f"{name_a}/{name_b}",
            'Code_A': code_a, 'Price_A': price_a, 'Qty_A': qty_a, 'Cost_A': cost_a,
            'Code_B': code_b, 'Price_B': price_b, 'Qty_B': qty_b, 'Cost_B': cost_b,
            'Veto': veto_reason
        })

    # 打印前 5 个候选配对的模拟情况
    for res in results[:10]: # 增加展示条目
        print(f"📍 配对: {res['Pair']}")
        if res['Veto']:
            print(f"   🚫 [安全拦截] {res['Veto']}")
        else:
            if res['Price_A'] > 0:
                print(f"   👉 若多 A ({res['Code_A']}): {res['Qty_A']}股 | 现价:{res['Price_A']:.3f} | 占用资金:¥{res['Cost_A']:,.2f}")
            if res['Price_B'] > 0:
                print(f"   👉 若多 B ({res['Code_B']}): {res['Qty_B']}股 | 现价:{res['Price_B']:.3f} | 占用资金:¥{res['Cost_B']:,.2f}")
        print("-" * 40)

if __name__ == "__main__":
    preview_allocation()
