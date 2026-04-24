import os
import re
import json

def refine_logs(log_dir='logs', target_date='2026-04-02'):
    print(f"🧹 开始物理清洗 {target_date} 的海量日志...")
    
    # 核心正则钩子
    order_pattern = re.compile(r'(\d{2}:\d{2}:\d{2}).*📝 \[报单\] ([0-9A-Z.]+).*价格=([0-9.]+)')
    error_pattern = re.compile(r'🔥 (执行器崩溃|Exception|Error): (.*)')
    
    ops_data = {
        "date": target_date,
        "orders_intended": [],
        "critical_errors": []
    }
    
    files_processed = 0
    bytes_scanned = 0
    
    # 遍历该目录下的所有 log
    for filename in os.listdir(log_dir):
        if not filename.endswith('.log'):
            continue
            
        filepath = os.path.join(log_dir, filename)
        files_processed += 1
        bytes_scanned += os.path.getsize(filepath)
        
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                # 抓取报单意愿
                order_match = order_pattern.search(line)
                if order_match:
                    ops_data["orders_intended"].append({
                        "time": order_match.group(1),
                        "code": order_match.group(2).strip(),
                        "intended_price": float(order_match.group(3))
                    })
                    continue
                
                # 抓取致命崩溃
                error_match = error_pattern.search(line)
                if error_match:
                    ops_data["critical_errors"].append({
                        "file": filename,
                        "error_msg": error_match.group(2).strip()
                    })

    # 简单去重
    ops_data["critical_errors"] = [dict(t) for t in {tuple(d.items()) for d in ops_data["critical_errors"]}]

    # 输出极其轻量的 JSON
    out_file = f'.state/settlement/ops_summary_{target_date.replace("-", "")}.json'
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(ops_data, f, ensure_ascii=False, indent=2)
        
    print(f"✅ 清洗完毕。扫描了 {files_processed} 个文件，共 {bytes_scanned / 1024 / 1024:.1f} MB。")
    print(f"📦 浓缩结果 ({len(ops_data['orders_intended'])} 笔报单, {len(ops_data['critical_errors'])} 个报错) 已输出至 {out_file}")

if __name__ == '__main__':
    # 将日志路径指向你实际的 logs 文件夹
    refine_logs(log_dir=r'Z:\QuantpC_Workspace\Quant_Pilot\logs')