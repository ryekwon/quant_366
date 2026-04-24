
import os
import sys
import pandas as pd
from datetime import datetime
from xtquant import xtdata
from dotenv import load_dotenv

load_dotenv()

STATE_DIR = os.getenv("STATE_DIR", r"Z:\QuantpC_Workspace\Quant_Pilot\.state")
PAIRS_CSV = os.path.join(STATE_DIR, "tradable_pairs_halflife.csv")
STAT_ARB_TOTAL_CAPITAL = 60000.0
MAX_CONCURRENT_PAIRS = 3

def preview():
    print(f"--- Stat Arb Allocation Preview ({datetime.now().strftime('%H:%M:%S')}) ---")
    print(f"Total: {STAT_ARB_TOTAL_CAPITAL} | Slots: {MAX_CONCURRENT_PAIRS} | Each: {STAT_ARB_TOTAL_CAPITAL/MAX_CONCURRENT_PAIRS:.2f}")

    if not os.path.exists(PAIRS_CSV): return

    df_pairs = pd.read_csv(PAIRS_CSV)
    all_codes = list(set(df_pairs['Code_A'].tolist() + df_pairs['Code_B'].tolist()))
    ticks = xtdata.get_full_tick(all_codes)
    
    for _, row in df_pairs.head(10).iterrows():
        code_a, name_a = row['Code_A'], row['Name_A']
        code_b, name_b = row['Code_B'], row['Name_B']
        tick_a, tick_b = ticks.get(code_a, {}), ticks.get(code_b, {})
        pa, pb = tick_a.get('lastPrice', 0), tick_b.get('lastPrice', 0)
        
        each = STAT_ARB_TOTAL_CAPITAL / MAX_CONCURRENT_PAIRS
        qa = int((each / pa) // 100 * 100) if pa > 0 else 0
        qb = int((each / pb) // 100 * 100) if pb > 0 else 0
        
        veto = None
        if tick_a and tick_b:
            pre_a, pre_b = tick_a.get('preClose', 0), tick_b.get('preClose', 0)
            if pre_a > 0 and pre_b > 0:
                ra, rb = (pa-pre_a)/pre_a, (pb-pre_b)/pre_b
                if ra < -0.07 or rb < -0.07: veto = "Downside Veto (>7%)"
                elif ra > 0.095 or rb > 0.095: veto = "Limit-up Veto (>9.5%)"

        print(f"Pair: {code_a}({pa:.3f}) / {code_b}({pb:.3f})")
        if veto: print(f"  VETO: {veto}")
        else: print(f"  A Qty: {qa} (Cost: {qa*pa:.2f}) | B Qty: {qb} (Cost: {qb*pb:.2f})")
        print("-" * 20)

if __name__ == "__main__":
    preview()
