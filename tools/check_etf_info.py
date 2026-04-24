from xtquant import xtdata
from dotenv import load_dotenv
import os
import time

load_dotenv()

def check_etf_info():
    print(f"\n--- Checking All ETF Info ---")
    info = xtdata.get_etf_info()
    print(info)
    if not info:
        print("No ETF info found. Trying to download first...")
        xtdata.download_etf_info()
        time.sleep(2)
        info = xtdata.get_etf_info()
        print(info)

if __name__ == "__main__":
    check_etf_info()
