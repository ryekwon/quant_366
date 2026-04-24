import pandas as pd
import os
import glob
import sys

# Windows terminal UTF-8 support
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

save_dir = r"Z:\QuantpC_Workspace\Data\Market_Minute"
files = glob.glob(os.path.join(save_dir, "*.parquet"))

print(f"Total files: {len(files)}")

checked = 0
for f in files:
    try:
        df = pd.read_parquet(f)
        if df.empty:
            continue
        
        last_dt = df.index.max()
        # If it's 2026-03-05
        last_dt_str = str(last_dt)
        if "2026-03-05" in last_dt_str:
            checked += 1
            if checked <= 5: # Only print first 5 with today's data
                print(f"{os.path.basename(f)}: {len(df)} rows | Last: {last_dt}")
        
    except Exception as e:
        pass

print(f"\nFiles with 2026-03-05 data: {checked} / {len(files)}")

# Specifically check 159985
f_target = os.path.join(save_dir, "159985_SZ_1m.parquet")
if os.path.exists(f_target):
    df = pd.read_parquet(f_target)
    print(f"\n159985_SZ_1m.parquet: {len(df)} rows | {df.index.min()} to {df.index.max()}")
    # Show how many rows are for 2026-03-05
    today_rows = df[df.index >= '2026-03-05']
    print(f"Rows for today (2026-03-05): {len(today_rows)}")
    if not today_rows.empty:
        print("Last 5 rows for today:")
        print(today_rows.tail(5))
else:
    print("\n159985_SZ_1m.parquet NOT FOUND")
