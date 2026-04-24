import pandas as pd
import json
import requests
from datetime import datetime

def launch_debrief():
    today_str = datetime.now().strftime('%Y%m%d')
    date_dash = datetime.now().strftime('%Y-%m-%d')
    
    csv_file = f'data/deal_reports/202604/deals_{today_str}.csv'
    ops_file = f'.state/settlement/ops_summary_{today_str}.json'
    n8n_webhook = "https://webhook.plumont.com/webhook-test/quant-debrief"

    try:
        # 1. 加载物理数据
        df_csv = pd.read_csv(csv_file, encoding='utf-8')
        with open(ops_file, 'r', encoding='utf-8') as f:
            ops_data = json.load(f)
            
        df_ops = pd.DataFrame(ops_data['orders_intended'])
        
        # 2. 物理算力前置：精准对账计算滑点
        slippage_report = []
        if not df_ops.empty and not df_csv.empty:
            for code in df_ops['code'].unique():
                intended_price = df_ops[df_ops['code'] == code]['intended_price'].mean()
                actual_price = df_csv[df_csv['证券代码'] == code]['成交价格'].mean()
                
                # 滑点差值：买入价越低越好，卖出价越高越好。这里只算绝对偏差率
                diff = actual_price - intended_price
                slippage_pct = diff / intended_price if intended_price > 0 else 0
                
                slippage_report.append({
                    "证券代码": code,
                    "意愿均价": round(intended_price, 4),
                    "实际均价": round(actual_price, 4),
                    "滑点偏差率": round(slippage_pct, 5)
                })

        # 3. 组装极简弹药库 (Payload)
        payload = {
            "date": date_dash,
            "system_errors": ops_data.get('critical_errors', []),
            "total_trades": len(df_csv),
            "total_commission": float(df_csv['手续费'].sum()) if '手续费' in df_csv else 0.0,
            "slippage_analysis": slippage_report
        }

        # 4. 发射给 n8n
        print("🚀 正在将硬核底稿发射至 n8n (vm202)...")
        response = requests.post(n8n_webhook, json=payload)
        if response.status_code == 200:
            print("✅ 汇报成功！AI 员工正在 Matrix 编写战报。")
        else:
            print(f"❌ 发射失败，状态码: {response.status_code}")

    except Exception as e:
        print(f"🔥 数据融合发射失败: {e}")

if __name__ == '__main__':
    launch_debrief()