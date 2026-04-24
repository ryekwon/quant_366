import requests
import json

# Dify 工作流 API 的物理坐标
# 使用 IP 直连防止 Windows 局域网 DNS 解析 ojo.lan 失败
DIFY_API_URL = "http://10.10.8.22/v1/workflows/run" 

# 填入你刚刚在 Dify 生成的 app-xxx 密钥
DIFY_API_KEY = "app-WNMSofJNBWcerCTDXNHPvshL"

def run_quant_debrief(script_name: str):
    """
    向 Dify 发送指令，触发跨网盘后复盘工作流
    """
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # 严格按照 Dify 接口规范构建 Payload
    # inputs 里的 key 必须和 START 节点的变量名完全一致
    payload = {
        "inputs": {
            "script_name": script_name
        },
        "response_mode": "blocking", # 阻塞模式：直到大模型吐出完整 JSON 才返回
        "user": "quant-master-pc"
    }
    
    print(f"🚀 正在呼叫 Dify 质检中心，目标脚本: {script_name}...")
    
    try:
        response = requests.post(DIFY_API_URL, headers=headers, json=payload)
        response.raise_for_status()
        
        # 解析返回的 JSON 数据
        result = response.json()
        
        # 提取我们在 END 节点输出的 review_result
        if "data" in result and "outputs" in result["data"]:
            ai_review = result["data"]["outputs"].get("review_result", "未获取到分析结果")
            print("✅ 质检报告返回：")
            print(ai_review)
            return ai_review
        else:
            print(f"⚠️ 格式异常的返回: {result}")
            
    except Exception as e:
        print(f"❌ 呼叫 Dify 失败: {str(e)}")

if __name__ == "__main__":
    # 测试触发我们刚才连通的那个出错日志
    run_quant_debrief("t0_multigrid_executor")