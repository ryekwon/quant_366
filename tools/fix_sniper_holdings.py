import os
import json
from xtquant import xtdata
from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount
import random
from dotenv import load_dotenv

load_dotenv()

HOLDINGS_FILE = r"Z:\QuantpC_Workspace\Quant_Pilot\.state\sniper_holdings.json"
QMT_PATH = os.getenv("QMT_PATH")
ACC_ID = os.getenv("ACCOUNT_ID")

def sync():
    print(f"Connecting to QMT at {QMT_PATH}...")
    session_id = random.randint(100000, 999999)
    xt_trader = XtQuantTrader(QMT_PATH, session_id)
    xt_trader.start()
    res = xt_trader.connect()
    if res != 0:
        print("❌ Failed to connect to QMT")
        return

    print("✅ Connected. Querying positions...")
    acc = StockAccount(ACC_ID)
    positions = xt_trader.query_stock_positions(acc)
    real_codes = {pos.stock_code for pos in positions if pos.volume > 0}
    print(f"Real Positions: {real_codes}")

    if not os.path.exists(HOLDINGS_FILE):
        print("No holdings file to sync.")
        return

    with open(HOLDINGS_FILE, 'r', encoding='utf-8') as f:
        holdings = json.load(f)

    new_holdings = {}
    for code, info in holdings.items():
        if code in real_codes:
            new_holdings[code] = info
            print(f"Keeping {code}")
        else:
            print(f"Removing {code} (Not found in real holdings)")

    with open(HOLDINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(new_holdings, f, ensure_ascii=False, indent=4)
    
    print("✅ Sync complete.")
    xt_trader.stop()

if __name__ == "__main__":
    sync()
