from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import os
import re
import datetime

# 初始化跨网 MCP 服务器，并物理级关闭 DNS 重绑定拦截，允许 Mac mini 跨网调用
mcp = FastMCP(
    "Quant-PC-Observer",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"]
    )
)

# 精准挂载你提供的物理路径
EXECUTION_LOG_DIR = r"Z:\QuantpC_Workspace\Quant_Pilot\logs"
ACTION_LOG_DIR = r"Z:\QuantpC_Workspace\Quant_Pilot\.state\action_logs"
SKILLS_DIR = r"Z:\QuantpC_Workspace\Quant_Pilot\.agents\skills"


@mcp.tool()
def read_execution_log(script_name: str) -> str:
    """
    探头1：智能系统日志提取器。
    用法：传入脚本名 (如 t0_multigrid_executor)。
    逻辑：提取全天所有包含 ERROR/WARNING/异常/失败 的行 + 强制保留最后 50 行收盘快照。
    """
    import os
    import datetime as safe_dt
    
    LOG_DIR = r"Z:\QuantpC_Workspace\Quant_Pilot\logs"
    today_str = safe_dt.datetime.now().strftime("%Y%m%d")
    log_filename = f"{today_str}_{script_name}.log"
    log_path = os.path.join(LOG_DIR, log_filename)
    
    if not os.path.exists(log_path):
        return f"未找到日志文件: {log_path}"
        
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        if not lines:
            return "日志文件为空"

        error_lines = []
        # 遍历全天日志，提取所有异动点
        for line in lines:
            upper_line = line.upper()
            if "ERROR" in upper_line or "WARNING" in upper_line or "失败" in line or "异常" in line:
                error_lines.append(line.strip())

        # 提取最后 50 行作为上下文兜底
        tail_lines = [line.strip() for line in lines[-50:]]
        
        # 组装给大模型的最终情报
        report = []
        report.append(f"=== 【全天异常监控过滤 (共 {len(error_lines)} 条)】 ===")
        if error_lines:
            report.extend(error_lines)
        else:
            report.append("全天未检测到 ERROR 或 WARNING 级别日志。")
            
        report.append("\n=== 【收盘期最后 50 行系统快照】 ===")
        report.extend(tail_lines)
        
        return "\n".join(report)
        
    except Exception as e:
        return f"日志读取失败: {str(e)}"

@mcp.tool()
def read_trade_actions() -> str:
    """
    探头2：读取当天全量真实交易动作(JSONL格式)。
    彻底移除行数限制，确保 AI 拿到 09:30 到 15:00 的每一个操作细节。
    """
    import os
    import datetime as safe_dt
    
    # 请确保全局变量 ACTION_LOG_DIR 正确，或替换为绝对路径
    ACTION_LOG_DIR = r"Z:\QuantpC_Workspace\Quant_Pilot\.state\action_logs"
    
    today_str = safe_dt.datetime.now().strftime("%Y%m%d")
    log_filename = f"action_{today_str}.jsonl"
    log_path = os.path.join(ACTION_LOG_DIR, log_filename)
    
    if not os.path.exists(log_path):
        return f"今日无交易动作记录: {log_filename}"
        
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            
        if not content:
            return "日志文件存在，但内容为空。"
            
        return content
    except Exception as e:
        return f"读取 action_log 失败: {str(e)}"

@mcp.tool()
def read_skill_knowledge(category_name: str) -> str:
    """
    探头3：RAG 知识库智能读取路由器 (高容错版)
    免疫 Markdown 格式偏差，精准截取当日增量。
    """
    import os
    import re
    import datetime as safe_dt

    SKILLS_DIR = r"Z:\QuantpC_Workspace\Quant_Pilot\.agents\skills"
    file_path = os.path.join(SKILLS_DIR, category_name, "SKILL.md")
    
    if not os.path.exists(file_path):
        return f"错误：未找到文件 {file_path}"
        
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # ==========================================
        # 路由分支 1：时间轴日志 -> 容错增量截取
        # ==========================================
        if "evolution-log" in category_name:
            import datetime as safe_dt
            now = safe_dt.datetime.now()
            today_full = now.strftime("%Y-%m-%d")
            today_short = f"{now.year}-{now.month}-{now.day}"
            
            lines = content.split('\n')
            capturing = False
            captured_text = []
            
            for line in lines:
                if not capturing:
                    # 触发点：碰到 H1 或 H2 标题，且包含今天日期
                    if (line.startswith('# ') or line.startswith('## ')) and (today_full in line or today_short in line):
                        capturing = True
                        captured_text.append(line)
                else:
                    # 停止点：碰到了下一个 H1 或 H2 标题（意味着进入了昨天的日志）
                    # 严格放行 H3 (###) 和 H4 (####) 及其下属内容
                    if line.startswith('# ') or line.startswith('## '):
                        break
                    captured_text.append(line)
            
            if captured_text:
                return "\n".join(captured_text).strip()
            return "NO_UPDATE_TODAY"

        # ==========================================
        # 路由分支 2：主题结构手册 -> 执行全量快照
        # ==========================================
        else:
            return content
            
    except Exception as e:
        return f"知识库读取受阻: {str(e)}"
if __name__ == "__main__":
    import sys
    import socket
    import uvicorn

    # 🔒 单实例保护：检测端口 8000 是否已被占用
    # 若已有 MCP Server 在运行（任务计划或 autopilot 先启动了），静默退出，不争抢端口
    _PORT = 8000
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _sock:
        _sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if _sock.connect_ex(("127.0.0.1", _PORT)) == 0:
            print(f"[MCP] 端口 {_PORT} 已被占用，MCP Server 已在运行，本次静默退出。")
            sys.exit(0)

    # 强制启用 SSE 协议，监听所有网卡，允许 Mac mini 跨网调用
    # FastMCP.run() 不支持 host/port 参数，改用底层的 Starlette/FastAPI 运行
    starlette_app = mcp.sse_app()
    uvicorn.run(starlette_app, host="0.0.0.0", port=_PORT)
