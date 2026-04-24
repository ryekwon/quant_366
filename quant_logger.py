import os
import json
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(_DIR, ".state", "action_logs")

def record_action(strategy, action, target, price=0.0, reason="", extra=None):
    """
    极速结构化日志落盘
    :param strategy: 策略名称 (如 'T0_Grid', 'Sniper', 'ETF_Rotation')
    :param action: 动作分类 (如 '买入', '卖出', '拦截', '熔断', '挂单失败')
    :param target: 标的代码 (如 '513180.SH')
    :param price: 触发时的价格
    :param reason: 动作的具体原因/参数细节
    :param extra: 其他需要补充的字典信息
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    
    # 按天生成日志文件，例如：action_20260304.jsonl
    today_str = datetime.now().strftime("%Y%m%d")
    log_file = os.path.join(LOG_DIR, f"action_{today_str}.jsonl")
    
    log_entry = {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "strategy": strategy,
        "action": action,
        "target": target,
        "price": round(float(price), 3) if price else 0.0,
        "reason": reason,
        "extra": extra or {}
    }
    
    # 追加模式写入，绝对不阻塞主线程
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f"⚠️ [Logger] 日志写入失败: {e}")