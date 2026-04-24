# dr_receiver.py — 灾备监听探针 v2
# 职责：7×24 轻量监听，等待 N8N Webhook 触发核重置
# 安全：Token 鉴权，防止未授权触发
# 运行：python dr_receiver.py（由 Windows 任务计划程序开机自启）

import os
import logging
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from flask import Flask, request
from dotenv import load_dotenv

# 加载 .env 文件（必须在读取环境变量前调用）
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ── 基础配置 ──────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.resolve()          # dr_receiver.py 所在目录
BAT_PATH    = BASE_DIR / "nuclear_reset.bat"
LOG_PATH    = BASE_DIR / "logs" / "dr_receiver.log"

# 鉴权 Token：从环境变量读取，未设置则拒绝所有请求
# 在 .env 中添加：RESET_TOKEN=your_secret_here
RESET_TOKEN = os.getenv("RESET_TOKEN", "")

# ── 日志配置（审计追踪）────────────────────────────────────────────
LOG_PATH.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("DR_Receiver")

# 屏蔽 Flask 的 HTTP 请求噪音日志
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ── Flask App ─────────────────────────────────────────────────────
app = Flask(__name__)


def execute_reset():
    """异步执行 nuclear_reset.bat，不阻塞 Webhook 响应"""
    log.info(f"🔥 [核重置] 开始执行 {BAT_PATH} ...")
    try:
        result = subprocess.run(
            ["cmd.exe", "/c", str(BAT_PATH)],
            cwd=str(BASE_DIR),          # ← 关键：确保 bat 文件在正确工作目录
            capture_output=True,
            text=True,
            encoding="gbk",            # Windows cmd 默认 GBK
            errors="replace"
        )
        log.info(f"✅ [核重置] 执行完毕 (exitcode={result.returncode})")
        if result.stdout:
            log.info(f"   stdout: {result.stdout.strip()}")
        if result.stderr:
            log.warning(f"   stderr: {result.stderr.strip()}")
    except Exception as e:
        log.error(f"❌ [核重置] 执行异常: {e}")


@app.route("/nuclear_strike", methods=["POST", "GET"])
def strike():
    # ── Token 鉴权 ─────────────────────────────────────────────────
    if not RESET_TOKEN:
        log.error("🚫 [安全] RESET_TOKEN 未配置，拒绝所有请求！请在 .env 中设置 RESET_TOKEN。")
        return "Server misconfigured: RESET_TOKEN not set", 500

    # 支持 URL 参数 (?token=xxx) 或 Header (X-Reset-Token: xxx)
    provided = request.args.get("token", "") or request.headers.get("X-Reset-Token", "")
    if provided != RESET_TOKEN:
        ip = request.remote_addr
        log.warning(f"🚫 [安全] Token 错误，来自 {ip}，拒绝！")
        return "Unauthorized", 403

    ip = request.remote_addr
    log.info(f"🚀 [授权] 核重置指令已授权，来自 {ip}，异步点火中...")
    threading.Thread(target=execute_reset, daemon=True).start()
    return f"Nuclear Reset Initiated at {datetime.now().strftime('%H:%M:%S')}", 200


@app.route("/health", methods=["GET"])
def health():
    """N8N/监控系统可定时 ping 此接口确认探针存活"""
    return f"OK | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 200


if __name__ == "__main__":
    log.info("=" * 55)
    log.info("🛡️  灾备监听探针 (DR Receiver) v2 启动")
    log.info(f"   监听地址: http://0.0.0.0:9999")
    log.info(f"   核按钮:   /nuclear_strike?token=<RESET_TOKEN>")
    log.info(f"   健康探针: /health")
    log.info(f"   Token 鉴权: {'✅ 已配置' if RESET_TOKEN else '❌ 未配置（危险！）'}")
    log.info(f"   BAT 路径: {BAT_PATH}")
    log.info("=" * 55)
    app.run(host="0.0.0.0", port=9999, threaded=True)