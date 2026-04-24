import os
import sys
import time
from datetime import datetime

# Path setup
VENV_PYTHON = r"z:\QuantpC_Workspace\Quant_Pilot\.venv\Scripts\python.exe"

def diag():
    print(f"[{datetime.now()}] Diagnostic starting...")
    print(f"Python: {sys.executable}")
    
    try:
        from xtquant import xtdata
        print(f"[{datetime.now()}] xtdata imported successfully.")
    except Exception as e:
        print(f"[{datetime.now()}] xtdata import FAILED: {e}")
        return

    # Check connection
    print(f"[{datetime.now()}] Attempting xtdata connection...")
    # xtdata usually connects automatically on first use or via get_market_data
    try:
        # MiniQMT usually runs on 58610
        # Check if we can get some basic info
        group_list = xtdata.get_stock_group_names()
        print(f"[{datetime.now()}] Connection OK. Groups found: {len(group_list)}")
    except Exception as e:
        print(f"[{datetime.now()}] Connection FAILED: {e}")
        return

    # Test download
    code = "513320.SH" # One of the targets
    print(f"[{datetime.now()}] Testing 1m download for {code}...")
    try:
        # xtdata.download_history_data2 is blocking
        # Set a timeout in my head... wait, it doesn't have a timeout paramedic.
        # We'll just see if it finishes.
        xtdata.download_history_data2([code], period='1m', start_time='20260305', end_time='20260305')
        print(f"[{datetime.now()}] Download command finished.")
        
        data = xtdata.get_market_data_ex([], [code], period='1m', start_time='20260305', end_time='20260305')
        if code in data and not data[code].empty:
            print(f"[{datetime.now()}] Success! Fetched {len(data[code])} rows for today.")
        else:
            print(f"[{datetime.now()}] Fetch returned NO data for today.")
    except Exception as e:
        print(f"[{datetime.now()}] Download/Fetch FAILED: {e}")

if __name__ == "__main__":
    diag()
