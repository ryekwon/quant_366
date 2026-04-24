from xtquant import xtdata
from datetime import datetime
import pandas as pd

def check_today():
    code = "513320.SH"
    print(f"Checking {code} for 2026-03-05...")
    
    # Try to download
    xtdata.download_history_data2([code], period='1m', start_time='20260305', end_time='20260305')
    
    # Fetch
    data = xtdata.get_market_data_ex([], [code], period='1m', start_time='20260305', end_time='20260305')
    
    if code in data and not data[code].empty:
        df = data[code]
        print(f"SUCCESS: Found {len(df)} rows.")
        print("Last 3 rows:")
        print(df.tail(3))
    else:
        print("FAILURE: No data found for today.")
        # Check what we DO have
        full_data = xtdata.get_market_data_ex([], [code], period='1m', start_time='20260301')
        if code in full_data and not full_data[code].empty:
            print(f"Full range available: {full_data[code].index.min()} to {full_data[code].index.max()}")
        else:
            print("No data available at all for this code via xtdata.")

if __name__ == "__main__":
    check_today()
