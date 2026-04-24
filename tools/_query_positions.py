"""临时脚本：查询实盘全量持仓并输出，用于修正 sniper_holdings.json"""
import os, sys, random, time, json
sys.path.insert(0, r'Z:\QuantpC_Workspace\Quant_Pilot')
from dotenv import load_dotenv
load_dotenv(r'Z:\QuantpC_Workspace\Quant_Pilot\.env')
from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount
from xtquant import xtdata

acc_id   = os.getenv('ACCOUNT_ID')
qmt_path = os.getenv('QMT_PATH')
print(f'Account: {acc_id}')

session_id = random.randint(100000, 999999)
xt = XtQuantTrader(qmt_path, session_id)
xt.start()
time.sleep(4)
res = xt.connect()
print(f'Connect result: {res}')

if res == 0:
    acc = StockAccount(acc_id)
    positions = xt.query_stock_positions(acc)
    print('\n=== 所有持仓(volume>0) ===')
    for p in positions:
        if p.volume > 0:
            can_use = getattr(p, 'can_use_volume', 'N/A')
            print(f'  {p.stock_code} | 总量={p.volume} | 可用={can_use} | 成本={p.open_price:.3f}')
    
    # 专门检查301373
    print('\n=== 检查 301373.SZ ===')
    hit = [p for p in positions if p.stock_code == '301373.SZ']
    if hit:
        p = hit[0]
        can_use = getattr(p, 'can_use_volume', 0)
        print(f'  找到! volume={p.volume}, can_use={can_use}, open_price={p.open_price}')
    else:
        print('  未找到301373.SZ持仓（可能未成交或已清仓）')

xt.stop()
print('\nDone.')
