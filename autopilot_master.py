# -*- coding: utf-8 -*-
"""
autopilot_master.py — 量化交易中枢调度引擎 V1.0
================================================
职责：
  - 交易日校验 (akshare)
  - YAML 插件化策略管理 (strategy_registry.yaml)
  - QMT 客户端看门狗 (psutil)
  - 时间流状态机：08:50 / 09:20 / 09:25 / 15:30 / 16:00
  - 子进程管理 (subprocess.Popen) + 日志持久化
  - N8N Webhook 实时通知
  - 状态中心 (.state/autopilot_status.json) 供 Dashboard 消费

用法:
  python autopilot_master.py
"""
import os
import sys
import socket
import json
import time
import yaml
import signal
import logging
import subprocess
import threading
import argparse
from datetime import datetime, date
from pathlib import Path
from dotenv import load_dotenv

# ==============================================================================
# 🛡️ 物理防线：单例运行锁 (Singleton Lock)
# 防止 Autopilot 被计划任务或手工误操作"双开"，导致并发发单灾难
# 原理：利用 TCP 端口排他性绑定——进程死亡后 OS 立即回收，无文件锁残留风险
# ==============================================================================
def enforce_single_instance():
    global _singleton_socket
    _singleton_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # 尝试霸占本地的 38888 端口
        _singleton_socket.bind(('127.0.0.1', 38888))
        print("✅ [进程锁] 端口 38888 绑定成功，Autopilot 唯一实例启动。")
    except socket.error:
        print("🚨 [致命拦截] 检测到另一个 Autopilot 进程正在运行！当前进程将立即自杀。")
        sys.exit(1)  # 发现已被占用，立刻退出，绝不执行后续逻辑

# 在脚本最开始调用——任何业务逻辑启动前完成排他性占位
enforce_single_instance()

# ─── 强制 UTF-8 输出 ────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─── 依赖延迟导入（避免未安装时整体崩溃）──────────────────────
try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

try:
    import akshare as ak
    _HAS_AKSHARE = True
except ImportError:
    _HAS_AKSHARE = False

# ═══════════════════════════════════════════════════════════════
# 配置区
# ═══════════════════════════════════════════════════════════════
load_dotenv()
N8N_WEBHOOK_URL  = os.getenv("N8N_WEBHOOK_URL", "")
QMT_PATH         = os.getenv("QMT_PATH", "")

_DIR        = Path(__file__).parent.resolve()
LOG_DIR     = _DIR / "logs"
STATE_DIR   = _DIR / ".state"
STATUS_FILE = STATE_DIR / "autopilot_status.json"
REGISTRY    = _DIR / "strategy_registry.yaml"
SYNC_SCRIPT            = _DIR / "qmt_daily_sync.py"
REFINE_UNIVERSE_SCRIPT = _DIR / "tools" / "refine_core_universe.py"  # 达尔文 ETF 宇宙精选
DL_1M_SCRIPT           = _DIR / "qmt_1m_downloader.py"
T0_MASTER    = _DIR / "t0_master.py"
DEBRIEF_SCRIPT = _DIR / "quant_debrief.py"
SETTLEMENT_SCRIPT = _DIR / "trade_settlement.py"
DAILY_TRADE_SCRIPT = _DIR / "daily_trade_settlement.py"  # 每日成交落盘（17:30）
QMT_LAUNCHER = _DIR / "start_miniQMT.py"
DASHBOARD    = _DIR / "web_dashboard.py"
ACCOUNTING_AUDIT = _DIR / "tools" / "accounting_audit.py"
SEARCH_SCRIPT = _DIR / "research" / "pair_researcher.py"
KNOWLEDGE_SYNC_SCRIPT = _DIR / "knowledge_manager.py"
SMOKE_TEST   = _DIR / "smoke_test.py"
RECONCILE_SCRIPT = _DIR / "intraday_reconcile.py"   # 盘中持仓对账引擎
MCP_SERVER_SCRIPT = _DIR / "MCP" / "mcp_server.py"  # MCP 服务器
T1_MASTER    = _DIR / "t1_master.py"                 # T+1 网格盘前参数核算
T1_EXECUTOR  = _DIR / "t1_grid_executor.py"          # T+1 网格盘中极速执行
DASH_PORT    = 8501

MACRO_EXECUTOR  = _DIR / "macro_rotation_executor.py"   # 周五进攻
MACRO_SENTINEL  = _DIR / "macro_risk_monitor.py"        # 周一至周四防守

# ── ETF_OU_Grid 均值回归网格引擎（2026-04-16 正式接替 T1 Grid）─────────────────
ETF_OU_GRID_MASTER   = _DIR / "etf_ou_grid_master.py"    # 盘前盘后解算：席位参数 + 同质化甄别
ETF_OU_GRID_EXECUTOR = _DIR / "etf_ou_grid_executor.py"  # 盘中轮询：非对称混合协议 + 双重熔断

# ── 截面动量系统（2026-04-16 正式接入 AutoPilot）────────────────────────────────
MOMENTUM_MASTER   = _DIR / "momentum_master.py"           # 盘后选股：截面动量司令部 → momentum_slots.json
MOMENTUM_EXECUTOR = _DIR / "momentum_vector_executor.py"  # 盘中执行：VWAP 入场 + 移动止盈退出（看门狗）

QMT_PROC_NAMES = {"xtminiqmt.exe", "xtitclient.exe"}

LOG_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)

# ─── 日志配置 ────────────────────────────────────────────────
# ─── 日记辅助 ────────────────────────────────────────────────
def get_today_str():
    return datetime.now().strftime("%Y%m%d")

PILOT_LOG = LOG_DIR / "pilot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # 1. 每日归档日志 (按日期物理归类)
        logging.FileHandler(LOG_DIR / f"{get_today_str()}_autopilot_master.log", encoding="utf-8"),
        # 2. 持续追踪日志 (方便用户直接查看 pilot.log)
        logging.FileHandler(PILOT_LOG, encoding="utf-8"),
        # 3. 控制台输出
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("AutoPilot")


# ═══════════════════════════════════════════════════════════════
# 1. 工具函数
# ═══════════════════════════════════════════════════════════════

def send_webhook(title: str, message: str) -> None:
    """向 N8N 发送状态通知（失败静默）"""
    if not N8N_WEBHOOK_URL or not _HAS_REQUESTS:
        return
    try:
        payload = {
            "title": title,
            "message": message,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        # 遵循 POST 标准协议，确保复杂数据不溢出/不乱码
        requests.post(N8N_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        log.debug(f"Webhook 发送失败: {e}")


def _is_momentum_slots_stale() -> bool:
    """
    检测 momentum_slots.json 是否过期（不存在或不是今日内容）。
    用于 09:00 盘前居安节点：昨日 15:30 Step4 失败时补跳一次选股，
    确保执行器 09:30 启动时席位数据一定是最新的。
    """
    slots_path = STATE_DIR / "momentum_slots.json"
    if not slots_path.exists():
        log.info("[动量居安] momentum_slots.json 不存在，需要盘前生成")
        return True
    try:
        mtime = datetime.fromtimestamp(slots_path.stat().st_mtime)
        if mtime.date() < date.today():
            log.info(f"[动量居安] momentum_slots.json 上次更新于 {mtime:%Y-%m-%d %H:%M}，非今日，需要盘前重新选股")
            return True
        return False   # 今日已更新
    except Exception as e:
        log.warning(f"[动量居安] 读取 slots 文件状态失败: {e}，保守处理为过期")
        return True


def is_trading_day() -> bool:
    """
    校验今日是否 A 股交易日。
    优先使用 akshare；若未安装则降级为星期判断（粗略）。
    """
    today = date.today().strftime("%Y%m%d")
    if _HAS_AKSHARE:
        try:
            cal = ak.tool_trade_date_hist_sina()
            trade_dates = cal["trade_date"].astype(str).str.replace("-", "").tolist()
            result = today in trade_dates
            log.info(f"交易日校验 (akshare): {today} → {'交易日' if result else '非交易日'}")
            return result
        except Exception as e:
            log.warning(f"akshare 交易日校验失败（降级为星期判断）: {e}")
    # 降级：非周末视为交易日
    weekday = date.today().weekday()
    result = weekday < 5
    log.info(f"交易日校验 (降级): {today} 星期{weekday+1} → {'交易日' if result else '非交易日'}")
    return result


def _is_qmt_running() -> bool:
    """检查 miniQMT 相关进程是否存活"""
    if not _HAS_PSUTIL:
        return False
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() in QMT_PROC_NAMES:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


# ═══════════════════════════════════════════════════════════════
# 2. 状态中心（供 Dashboard 消费）
# ═══════════════════════════════════════════════════════════════

def _load_status() -> dict:
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_status(status: dict) -> None:
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"写入状态文件失败: {e}")


def _update_strategy_status(name: str, pid: int | None, state: str,
                            script: str = "", description: str = "") -> None:
    """更新单个策略的运行状态记录"""
    all_status = _load_status()
    all_status[name] = {
        "strategy_name": name,
        "script": script,
        "pid": pid,
        "status": state,           # running / stopped / error / blocking
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "description": description,
    }
    _save_status(all_status)


# ═══════════════════════════════════════════════════════════════
# 3. 子进程管理
# ═══════════════════════════════════════════════════════════════

# ─── 强制使用当前虚拟环境 (venv) 如果存在 ──────────────────────
_venv_py = _DIR / ".venv" / "Scripts" / "python.exe"
if _venv_py.exists():
    _py_executable = str(_venv_py)
    _py_dir = str(_venv_py.parent)
    log.info(f"📍 检测到内置虚拟环境: {_py_executable}")
else:
    _py_executable = sys.executable
    _py_dir = str(Path(sys.executable).parent)
    log.info(f"📍 使用系统或全局 Python: {sys.executable}")

_sub_env = {
    **os.environ, 
    "PYTHONIOENCODING": "utf-8",
    "PATH": _py_dir + os.pathsep + os.environ.get("PATH", "")
}


def _open_log(script_name: str, suffix: str = "") -> tuple:
    """打开日志文件并返回 (file_handle, path_str)"""
    tag = f"{get_today_str()}_{Path(script_name).stem}{suffix}"
    log_path = LOG_DIR / f"{tag}.log"
    fh = open(log_path, "a", encoding="utf-8", buffering=1)
    return fh, str(log_path)


WATCHDOG_STOP_HHMM   = "1530"  # 15:30 后不再复活
WATCHDOG_MAX_RESTART = 5       # 最多尝试复活次数


def run_daemon(name: str, script_rel_path: str, description: str = "", watchdog: bool = True):
    """
    非阻塞运行子进程（守护模式）。
    直接兼容用户要求的 run_daemon(name, script) 格式。
    """
    cfg = {
        "name": name,
        "script": script_rel_path,
        "description": description,
        "watchdog": watchdog
    }
    return launch_strategy(cfg)

def launch_strategy(cfg: dict) -> subprocess.Popen | None:
    """
    非阻塞启动单个策略子进程（Popen）。
    stdout/stderr 强制重定向到日志文件——绝不允许日志黑洞。
    watchdog: true 的策略在盘中崩溃后自动复活。
    """
    name        = cfg.get("name", "未知策略")
    script      = str(_DIR / cfg.get("script", ""))
    desc        = cfg.get("description", "")
    is_watchdog = cfg.get("watchdog", False)

    if not Path(script).exists():
        log.error(f"[{name}] 找不到脚本: {script}")
        _update_strategy_status(name, None, "error", cfg.get("script", ""), desc)
        send_webhook(f"🚨 {name} 启动失败", f"找不到脚本: {script}")
        return None

    def _spawn(restart_count: int = 0):
        """内部：创建一个新子进程，stdout/stderr 必须落盘"""
        suffix = f"_restart{restart_count}" if restart_count > 0 else ""
        try:
            fh, log_path = _open_log(cfg.get("script", name), suffix)
            label = f"(第 {restart_count} 次复活)" if restart_count else ""
            fh.write(
                f"\n{'='*60}\n"
                f"[{datetime.now()}] AutoPilot 启动 {name} {label}\n"
                f"{'='*60}\n"
            )
            fh.flush()
            proc = subprocess.Popen(
                [_py_executable, script],
                stdout=fh,   # 强制接管 stdout
                stderr=fh,   # 强制接管 stderr —— 绝不吞噬 Traceback
                env=_sub_env,
            )
            return proc, fh, log_path
        except Exception as e:
            log.error(f"[{name}] spawn 失败: {e}")
            return None, None, None

    proc, fh, log_path = _spawn(0)
    if proc is None:
        _update_strategy_status(name, None, "error", cfg.get("script", ""), desc)
        send_webhook(f"🚨 {name} 启动异常", "初始 spawn 失败")
        return None

    log.info(f"🚀 [{name}] PID={proc.pid} | watchdog={'ON' if is_watchdog else 'OFF'} | 日志 -> {log_path}")
    send_webhook(f"🚀 {name} 已启动", f"PID={proc.pid}\n{desc}")
    _update_strategy_status(name, proc.pid, "running", cfg.get("script", ""), desc)

    # ── 看门狗复活线程（仅 watchdog=true）────────────────────────────
    def _watchdog_thread():
        nonlocal proc, fh, log_path
        restarts = 0
        while True:
            _start_ts = time.time()
            proc.wait()                      # 阻塞等待当前进程退出
            rc       = proc.returncode
            _alive_sec = time.time() - _start_ts
            now_hhmm = datetime.now().strftime("%H%M")

            # 正常退出：rc==0 且进程稳定运行超过 60秒 → 认为合法退出不再复活
            # 快速退出：rc==0 但存活 < 60秒 → 视为启动崩溃（如 UnicodeError），仍需复活
            is_stable_exit = (rc == 0 and _alive_sec >= 60)
            if is_stable_exit or now_hhmm >= WATCHDOG_STOP_HHMM:
                status = "stopped" if rc == 0 else "error"
                log.info(f"[{name}] PID={proc.pid} 退出 exitcode={rc}，看门狗停止复活")
                _update_strategy_status(name, proc.pid, status, cfg.get("script", ""), desc)
                break


            # 盘中异常崩溃 —— 触发复活协议
            restarts += 1
            log.warning(f"� [{name}] 崩溃 exitcode={rc}，第 {restarts}/{WATCHDOG_MAX_RESTART} 次复活...")
            send_webhook(
                f"🔄 {name} 崩溃复活 (#{restarts})",
                f"exitcode={rc} | 正在重新拉起\n日志: {log_path}"
            )
            _update_strategy_status(name, None, "restarting", cfg.get("script", ""), desc)

            if restarts > WATCHDOG_MAX_RESTART:
                fatal = f"连续崩溃 {WATCHDOG_MAX_RESTART} 次，放弃复活，请人工介入！"
                log.error(f"🚨 [{name}] {fatal}")
                send_webhook(f"🚨 {name} 放弃复活", fatal)
                _update_strategy_status(name, None, "error", cfg.get("script", ""), desc)
                break

            time.sleep(5)                    # 等 5 秒再复活，防止立刻再崩
            proc, fh, log_path = _spawn(restarts)
            if proc is None:
                send_webhook(f"🚨 {name} spawn 失败", f"第 {restarts} 次复活失败")
                break
            log.info(f"✅ [{name}] 复活成功 PID={proc.pid}")
            _update_strategy_status(name, proc.pid, "running", cfg.get("script", ""), desc)

    # ── 普通监控线程（watchdog=false）────────────────────────────────
    def _simple_monitor():
        proc.wait()
        rc     = proc.returncode
        status = "stopped" if rc == 0 else "error"
        log.info(f"[{name}] PID={proc.pid} 退出 exitcode={rc}")
        _update_strategy_status(name, proc.pid, status, cfg.get("script", ""), desc)
        if rc != 0:
            send_webhook(f"� {name} 意外退出",
                         f"PID={proc.pid}，exitcode={rc}\n日志: {log_path}")

    if is_watchdog:
        threading.Thread(target=_watchdog_thread, daemon=True, name=f"watchdog-{name}").start()
    else:
        threading.Thread(target=_simple_monitor, daemon=True).start()

    return proc



def launch_dashboard() -> subprocess.Popen | None:
    """启动 Streamlit Web Dashboard（非阻塞）"""
    name = "Web Dashboard"
    if not DASHBOARD.exists():
        log.warning(f"[{name}] 找不到 {DASHBOARD}")
        return None
    try:
        fh, log_path = _open_log("web_dashboard")
        fh.write(f"\n[{datetime.now()}] AutoPilot 启动 Streamlit Dashboard\n")
        fh.flush()
        proc = subprocess.Popen(
            [_py_executable, "-m", "streamlit", "run", str(DASHBOARD),
             "--server.port", str(DASH_PORT),
             "--server.headless", "true"],
            stdout=fh, stderr=fh,
            env=_sub_env,
        )
        log.info(f"🌐 [{name}] PID={proc.pid} -> http://localhost:{DASH_PORT}")
        send_webhook("🌐 Dashboard 已启动",
                     f"PID={proc.pid}\nhttp://localhost:{DASH_PORT}")
        _update_strategy_status(
            name, proc.pid, "running", "web_dashboard.py",
            f"Streamlit 策略状态看板，端口 {DASH_PORT}"
        )
        return proc
    except Exception as e:
        log.error(f"[{name}] 启动失败: {e}")
        return None


def run_blocking(script_path: Path, name: str = "日终同步") -> bool:
    """
    阻塞执行脚本，等待完全结束后返回。
    用于 15:30 qmt_daily_sync.py。
    """
    if not script_path.exists():
        log.error(f"[{name}] 找不到脚本: {script_path}")
        send_webhook(f"🚨 {name} 脚本缺失", str(script_path))
        return False

    fh, log_path = _open_log(script_path.name, "_blocking")
    fh.write(f"\n[{datetime.now()}] AutoPilot 阻塞执行 {name}\n")
    fh.flush()

    log.info(f"⏳ [{name}] 开始阻塞执行... 日志 -> {log_path}")
    send_webhook(f"⏳ {name} 开始", f"脚本: {script_path.name}")
    _update_strategy_status(name, None, "blocking", script_path.name,
                            "日终数据同步，必须阻塞等待完成")
    try:
        result = subprocess.run(
            [_py_executable, str(script_path)],
            stdout=fh, stderr=fh,
            env=_sub_env,
        )
        fh.close()
        ok = result.returncode == 0
        if ok:
            log.info(f"✅ [{name}] 执行完成")
            send_webhook(f"✅ {name} 完成", f"退出码=0，日志: {log_path}")
            _update_strategy_status(name, None, "stopped", script_path.name,
                                    "日终数据同步已完成")
        else:
            log.error(f"❌ [{name}] 执行失败，exitcode={result.returncode}")
            send_webhook(f"🚨 {name} 失败",
                         f"exitcode={result.returncode}\n日志: {log_path}")
            _update_strategy_status(name, None, "error", script_path.name,
                                    f"日终数据同步失败，exitcode={result.returncode}")
        return ok
    except Exception as e:
        fh.close()
        log.error(f"[{name}] 运行异常: {e}")
        send_webhook(f"🚨 {name} 异常", str(e))
        return False


def run_blocking_with_args(script_path: Path, name: str = "定时任务",
                            extra_args: list = None) -> bool:
    """
    阻塞执行脚本，支持额外命令行参数。
    用于 intraday_reconcile.py 等需要 --mode 参数的脚本。
    """
    if not script_path.exists():
        log.error(f"[{name}] 找不到脚本: {script_path}")
        return False

    cmd = [_py_executable, str(script_path)] + (extra_args or [])
    fh, log_path = _open_log(script_path.name, "_blocking")
    fh.write(f"\n[{datetime.now()}] AutoPilot 阻塞执行 {name} {extra_args or ''}\n")
    fh.flush()

    log.info(f"⏳ [{name}] 开始... 日志 -> {log_path}")
    try:
        result = subprocess.run(cmd, stdout=fh, stderr=fh, env=_sub_env)
        fh.close()
        ok = result.returncode in (0, 1)   # exit 1 表示发现偏差但正常执行
        if ok:
            log.info(f"✅ [{name}] 执行完成 (rc={result.returncode})")
        else:
            log.error(f"❌ [{name}] 执行失败，exitcode={result.returncode}")
            send_webhook(f"🚨 {name} 失败", f"exitcode={result.returncode}\n日志: {log_path}")
        return ok
    except Exception as e:
        fh.close()
        log.error(f"[{name}] 运行异常: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# 4. QMT 看门狗
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# 4. QMT 看门狗与健康监控
# ═══════════════════════════════════════════════════════════════

def ensure_qmt_ready() -> bool:
    """
    检测 miniQMT 是否运行；若未运行则自动触发 start_miniQMT.py。
    无论结果均发送 N8N 通知。
    """
    if _is_qmt_running():
        log.info("✅ miniQMT 客户端已在运行")
        return True

    log.warning("⚠️ 未检测到 miniQMT，尝试自动拉起...")
    send_webhook("⚠️ QMT 客户端未启动", "监测到 QMT 进程缺失，正在尝试自愈合启动...")

    if not QMT_LAUNCHER.exists():
        log.error(f"❌ 找不到启动器: {QMT_LAUNCHER}")
        return False

    try:
        # 使用 CREATE_NEW_CONSOLE 确保启动器有自己的窗口，不干扰主进程
        proc = subprocess.Popen(
            [_py_executable, str(QMT_LAUNCHER)],
            env=_sub_env,
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0,
        )
        # 等待完成或超时
        rc = proc.wait(timeout=180) 
        return rc == 0
    except Exception as e:
        log.error(f"❌ 启动 QMT 失败: {e}")
        return False


def qmt_health_monitor():
    """
    后台线程：实时监控 QMT 存活状态。
    仅在交易时段 (09:15 - 15:30) 活跃。
    """
    log.info("🛡️ [Watchdog] QMT 存活监控线程已启动")
    while True:
        try:
            now_hhmm = datetime.now().strftime("%H%M")
            # 仅在交易时段监控（盘中掉线自动补位）
            if "0915" <= now_hhmm <= "1530":
                if not _is_qmt_running():
                    log.error("🚨 [Watchdog] 检测到 QMT 进程异常失踪！触发紧急恢复...")
                    send_webhook("🚨 QMT 异常掉线", "检测到 XtMiniQmt 进程已退出，正在尝试重新拉起...")
                    ok = ensure_qmt_ready()
                    if ok:
                        log.info("✅ [Watchdog] QMT 进程恢复成功")
                        send_webhook("✅ QMT 恢复成功", "QMT 已重新登录并就绪。")
                    else:
                        log.error("❌ [Watchdog] QMT 恢复失败，请人工介入！")
                        send_webhook("🚨 QMT 恢复失败", "自动恢复尝试失败，请立即检查服务器！")
            
            time.sleep(120)  # 每 2 分钟巡检一次
        except Exception as e:
            log.error(f"🔥 QMT 监控线程崩溃: {e}")
            time.sleep(30)


# ═══════════════════════════════════════════════════════════════
# 5. 策略注册表
# ═══════════════════════════════════════════════════════════════

def load_registry() -> dict:
    """读取 strategy_registry.yaml"""
    if not REGISTRY.exists():
        log.warning(f"找不到 {REGISTRY}，使用空注册表")
        return {}
    with open(REGISTRY, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def _kill_all(procs: list) -> None:
    """
    16:30 物理歼灭协议 — 三步清场，确保无僵尸进程残留：
    1. SIGTERM → 给进程优雅退出机会
    2. 等待 3 秒 → 宽限期
    3. SIGKILL → 定斩不饶
    4. taskkill / netstat → 双保险清理 Streamlit 端口占用
    """
    log.info(f"🔫 [EOD] 物理歼灭协议启动，共 {len(procs)} 个子进程待清场...")

    # Step 1 — 发 SIGTERM
    for p in procs:
        try:
            if p.poll() is None:
                p.terminate()
                log.info(f"  ⇢ terminate() PID={p.pid}")
        except Exception:
            pass

    time.sleep(3)               # 宽限 3 秒让进程自行退出

    # Step 2 — 补刀 SIGKILL
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
                log.info(f"  ☠ kill(SIGKILL) PID={p.pid}")
        except Exception:
            pass

    # Step 3 — Streamlit 双保险：按进程名 + 按端口
    log.info(f"  🌐 清理 Streamlit 端口 {DASH_PORT} 占用...")
    subprocess.run(
        ["taskkill", "/F", "/IM", "streamlit.exe", "/T"],
        capture_output=True
    )
    # netstat 找出占用 DASH_PORT 的 PID 并强杀（处理 python -m streamlit 形式）
    subprocess.run(
        f'for /f "tokens=5" %a in (\'netstat -ano ^| findstr :{DASH_PORT}\') '
        f'do taskkill /f /pid %a',
        shell=True, capture_output=True
    )
    log.info("✅ [EOD] 物理歼灭完成，端口和内存已释放。")



# ═══════════════════════════════════════════════════════════════
# 7. 全自动化全天候调度引擎 (The Loop)
# ═══════════════════════════════════════════════════════════════

class _TimeFlag:
    """
    时间节点触发标志：每天每个 key 只触发一次。
    ✅ 落盘持久化：进程重启后读取 .state/timeflag_state.json，
       不会因为 autopilot 自我重启而在同一天重复触发同一节点。
    ✅ 精确分钟匹配（宽容 ±30 分钟大窗口，防止 30s 轮询错过）：
       触发条件 = 当前时间 >= hhmm  AND  当前时间 < hhmm + 30 min
    """
    _STATE_FILE = Path(__file__).parent / ".state" / "timeflag_state.json"

    def __init__(self):
        self._today: date = date.today()
        self._fired: set[str] = set()
        self._load()

    def _load(self):
        """从磁盘恢复今日已触发的 key。"""
        try:
            if self._STATE_FILE.exists():
                data = json.loads(self._STATE_FILE.read_text(encoding="utf-8"))
                if data.get("date") == str(self._today):
                    self._fired = set(data.get("fired", []))
        except Exception:
            self._fired = set()

    def _save(self):
        """将当日已触发 key 落盘。"""
        try:
            self._STATE_FILE.parent.mkdir(exist_ok=True)
            self._STATE_FILE.write_text(
                json.dumps({"date": str(self._today), "fired": sorted(self._fired)},
                           ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def should_fire(self, key: str, hhmm: str) -> bool:
        # 跨天：重置内存 + 落盘
        if date.today() != self._today:
            self._fired.clear()
            self._today = date.today()
            self._save()

        if key in self._fired:
            return False

        now_int  = int(datetime.now().strftime("%H%M"))   # e.g. 1430
        tgt_int  = int(hhmm.replace(":", ""))             # e.g. 1400
        deadline = tgt_int + 30                           # 30 分钟宽容窗口

        # 允许触发区间：[hhmm, hhmm+30min)
        return tgt_int <= now_int < deadline

    def mark(self, key: str):
        self._fired.add(key)
        self._save()   # 立即落盘，重启后不再重复触发

def task_loop():
    """无限循环模式：像 Crontab 一样守候全天"""
    log.info("🔄 [Orchestration] 全天候自动化循环已激活...")

    # ── 非交易日日志抑制 ──────────────────────────────────────────────
    # 周末/节假日打开电脑测试时，不产生空的日志文件（*.log 只应在交易日生成）
    _is_td = is_trading_day()
    if not _is_td:
        # 移除所有 FileHandler，只保留控制台输出
        _root_logger = logging.getLogger()
        for _h in list(_root_logger.handlers):
            if isinstance(_h, logging.FileHandler):
                _root_logger.removeHandler(_h)
                _h.close()
        print(f"[AutoPilot] 今日非交易日，日志文件已抑制（仅控制台输出）。系统将保持静默待机。")
    else:
        send_webhook("🔄 AutoPilot 循环启动", "系统进入 24/7 时间监控模式")

    tf = _TimeFlag()

    # ── MCP 服务器已停用（改用外部方案）
    # task_mcp_server()

    # 启动后台监控线程
    threading.Thread(target=qmt_health_monitor, daemon=True, name="QMT-Monitor").start()


    while True:
        try:
            # 0. 09:00 盘前动量选股居安（兜底）
            # 仅当 momentum_slots.json 不存在或非今日生成时触发
            # （防御层：昨日 15:30 Step4 失败或断电时，盘前补跑一次确保数据完整）
            if tf.should_fire("momentum_preopen", "09:00"):
                if is_trading_day() and _is_momentum_slots_stale():
                    now_str = datetime.now().strftime('%H:%M:%S')
                    log.info(f"[{now_str}] ⚠️ [动量居安] momentum_slots.json 过期，盘前补运行动量选股...")
                    run_blocking(MOMENTUM_MASTER, "动量选股（盘前居安）")
                elif is_trading_day():
                    log.info("[动量居安] momentum_slots.json 今日已生成，盘前节点跳过")
                tf.mark("momentum_preopen")

            # 1. 08:50 初始化
            if tf.should_fire("init", "08:50"):
                task_init()
                tf.mark("init")
            
            # 2. 09:25 早盘启动 (包含 T0 精选与实盘拉起)
            if tf.should_fire("morning", "09:22"):
                task_morning()
                tf.mark("morning")

            # 2.5. 09:20 ETF_OU_Grid 大脑：盘前解算席位参数 + 同质化甄别（阻塞，必须在 executor 前完成）
            if tf.should_fire("etf_ou_grid_master", "09:20"):
                if is_trading_day():
                    now_str = datetime.now().strftime('%H:%M:%S')
                    log.info(f"[{now_str}] 🛸 激活 ETF_OU_Grid 大脑（盘前解算）...")
                    success = run_blocking(ETF_OU_GRID_MASTER, "ETF_OU_Grid 席位解算")
                    if success:
                        log.info("✅ [ETF_OU_Grid_Master] 席位解算完成，etf_grid_slots.json 已写入。")
                    else:
                        log.warning("⚠️ [ETF_OU_Grid_Master] 解算失败，executor 将沿用昨日席位参数。")
                tf.mark("etf_ou_grid_master")

            # 2.6. 09:30 ETF_OU_Grid 执行器（看门狗守护，接替 T1 Grid）
            if tf.should_fire("etf_ou_grid_executor", "09:30"):
                if is_trading_day():
                    now_str = datetime.now().strftime('%H:%M:%S')
                    log.info(f"[{now_str}] 🤖 激活 ETF_OU_Grid 执行器（非对称网格盘中守护）...")
                    run_daemon(
                        name="ETF_OU_Grid执行器",
                        script_rel_path=str(ETF_OU_GRID_EXECUTOR.relative_to(_DIR)),
                        description="OU均值回归网格 盘中极速执行，09:30~15:00 看门狗守护",
                        watchdog=True,
                    )
                tf.mark("etf_ou_grid_executor")

            # 2.7. 09:22 动量系统 T+1 信号执行（集合竞价挂单，阻塞）
            # 必须在 morning 任务之前、ETF_OU_Grid master 同期触发（均在 09:20 宽容窗口内）
            if tf.should_fire("momentum_t1_auction", "09:20"):
                if is_trading_day():
                    task_momentum_t1_auction()
                tf.mark("momentum_t1_auction")

            # 2.8. 09:30 动量向量执行器（看门狗守护，盘中 VWAP 确认入场 + 移动止盈）
            if tf.should_fire("momentum_executor", "09:30"):
                if is_trading_day():
                    now_str = datetime.now().strftime('%H:%M:%S')
                    log.info(f"[{now_str}] 🚀 激活动量向量执行器（右侧趋势追踪守护）...")
                    run_daemon(
                        name="动量向量执行器",
                        script_rel_path=str(MOMENTUM_EXECUTOR.relative_to(_DIR)),
                        description="截面动量右侧入场，VWAP确认+移动止盈，09:30~15:00 看门狗守护",
                        watchdog=True,
                    )
                tf.mark("momentum_executor")

            # ── ⬇️  T1 Grid 已退役（2026-04-16 正式切换为 ETF_OU_Grid）────────
            # 2.5. [已停用] 09:22 T+1 网格盘前参数核算
            # if tf.should_fire("t1_master", "09:22"):
            #     if is_trading_day():
            #         task_t1_master()
            #     tf.mark("t1_master")

            # 2.6. [已停用] 09:30 T+1 网格盘中执行器
            # if tf.should_fire("t1_executor", "09:30"):
            #     if is_trading_day():
            #         task_t1_grid_executor()
            #     tf.mark("t1_executor")
            # ── ⬆️  T1 Grid 已退役 ────────────────────────────────────────────

            # 3. 14:40 胖鱼大脑：Data Fusion 缝合今日 Tick + 计算突破信号
            if tf.should_fire("fat_fish", "14:40"):
                task_fat_fish()
                tf.mark("fat_fish")

            # 3.5. 14:46 尾盘狙击（三大板块全市场扫描，预计耗时3-5min）
            if tf.should_fire("sniper", "14:46"):
                task_sniper()
                tf.mark("sniper")

            # 3.6. 14:50 胖鱼火炮：执行 Master 生成的买卖指令
            # Sniper 扫描耗时3-5min，到 14:50 理论上已完成并断连
            if tf.should_fire("fat_fish_exec", "14:50"):
                task_fat_fish_executor()
                tf.mark("fat_fish_exec")

            # 3.5. 11:28 午盘前对账（仅检查，不操作）
            if tf.should_fire("reconcile_noon", "11:28"):
                if is_trading_day():
                    task_intraday_reconcile("check")
                tf.mark("reconcile_noon")

            # 3.7. 14:52 收盘前对账（检查 + T0 残差清仓）提前到14:52确保有成交时间
            if tf.should_fire("reconcile_eod", "14:52"):
                if is_trading_day():
                    task_intraday_reconcile("eod_clear")
                tf.mark("reconcile_eod")

            # 4. 宏观守卫（雷达）：每整点触发，全量扫描 9 只 ETF ─────────────────
            # 交易时间：09:30 / 10:30 / 11:30 / 13:00 / 14:00（共 5 次/天）
            # 交易执行时间不变：仅在周五 14:42 进攻（macro_rotation_executor）
            # 每个时间节点使用独立 key，_TimeFlag 保证每天每节点只触发一次

            for _s_key, _s_hhmm in [
                ("sentinel_0930", "09:30"),
                ("sentinel_1030", "10:30"),
                ("sentinel_1130", "11:30"),
                ("sentinel_1300", "13:00"),
                ("sentinel_1400", "14:00"),
            ]:
                if tf.should_fire(_s_key, _s_hhmm):
                    if is_trading_day() and datetime.now().weekday() != 4:  # 周一至周四
                        log.info(f"🛡️ [{_s_hhmm}] 宏观全量雷达扫描触发（周一至周四）")
                        task_macro_sentinel()
                    elif is_trading_day() and datetime.now().weekday() == 4:
                        log.info(f"ℹ️ [{_s_hhmm}] 周五守卫跳过（进攻由 14:42 负责）")
                    tf.mark(_s_key)

            # 4b. 14:42 宏观轮动进攻窗口（仅周五）
            if tf.should_fire("macro_rotation_attack", "14:42"):
                if is_trading_day():
                    if datetime.now().weekday() == 4:  # 周五
                        task_macro_rotation()
                    else:
                        log.info("📅 非周五，14:42 进攻窗口跳过（守卫已在整点触发）")
                else:
                    log.info("📅 非交易日，跳过 macro_rotation。")
                tf.mark("macro_rotation_attack")



            # 4b. 14:55 ETF 轮动（旧系统已禁用，仕历史兼容保留此帧）
            if tf.should_fire("rotation", "14:55"):
                log.info("⏩ [rotation] 旧 ETF 轮动已切换为 macro_rotation_executor，忽略此帧。")
                tf.mark("rotation")

            
            # 4.5. 16:05 每日物理对账清算 — 已停用
            # if tf.should_fire("settlement", "16:05"):
            #     if is_trading_day():
            #         task_settlement()
            #     tf.mark("settlement")

            # 4.5.5. 16:08 穿透式会计核算与持仓对齐 — 已停用
            # if tf.should_fire("audit", "16:08"):
            #     if is_trading_day():
            #         task_accounting_audit()
            #     tf.mark("audit")

            # 4.6. 16:10 每日 AI 复盘报告 — 已停用
            # if tf.should_fire("debrief", "16:10"):
            #     if is_trading_day():
            #         task_debrief()
            #     tf.mark("debrief")

            # 4.7. 16:15 统计套利配对挖掘 (脑部计算)
            # if tf.should_fire("research", "16:15"):
            #     if is_trading_day():
            #         task_research()
            #     tf.mark("research")

            # 4.9. 17:30 每日成交落盘（券商结算后，按月分文件夹归档）
            if tf.should_fire("daily_trade", "17:30"):
                if is_trading_day():
                    task_daily_trade()
                tf.mark("daily_trade")

            # 4.8. 18:00 每日知识/逻辑同步 (Evolution Sync)
            if tf.should_fire("knowledge", "18:00"):
                task_knowledge_sync()
                tf.mark("knowledge")

            # 5. 15:05 盘后下载与同步（A 股 15:00 收盘，QMT 数据服务器约 1-3 分钟内落盘完整日 K）
            # watchdog 全局 WATCHDOG_STOP_HHMM=1530 保持不变（executor 自身 15:00 已内部退出）
            if tf.should_fire("eod", "15:05"):
                task_eod()
                tf.mark("eod")
            
            # 5. 16:30 收盘物理清场 — 已取消，改为 23:00 自动关机
            # if tf.should_fire("clean", "16:30"):
            #     task_clean()
            #     tf.mark("clean")

            # 6. MCP 服务器已停用（改用外部方案）

            # 7. 23:00 自动关机 — 已停用（Task Manager 负责强制关机）
            # if tf.should_fire("shutdown", "23:00"):
            #     task_shutdown()
            #     tf.mark("shutdown")
            
            # 每 30 秒轮询一次，节省 CPU
            time.sleep(30)
            
        except KeyboardInterrupt:
            log.info("⏹️ 监控循环被手动停止。")
            break
        except Exception as e:
            log.error(f"🔥 调度循环遭遇未知错误: {e}")
            time.sleep(60)

# ═══════════════════════════════════════════════════════════════
# 8. 5 大时间轴路由拓扑
# ═══════════════════════════════════════════════════════════════

def task_intraday_reconcile(mode: str = "check"):
    """11:28 / 14:55 触发：盘中持仓对账，发现残差持仓并告警/清仓"""
    label = "午盘对账" if mode == "check" else "收盘前残差清仓"
    now_str = datetime.now().strftime('%H:%M:%S')
    print(f"[{now_str}] 🔍 激活{label}...")
    result = run_blocking_with_args(RECONCILE_SCRIPT, f"盘中对账({mode})", ["--mode", mode])
    return result


def task_accounting_audit():
    """16:08 触发：全系统持仓会计核算与策略归属审计"""
    now_str = datetime.now().strftime('%H:%M:%S')
    print(f"[{now_str}] ⚖️ 激活全系统会计核算审计...")
    success = run_blocking(ACCOUNTING_AUDIT, "全系统持仓审计")
    if success:
        log.info("✅ [Accounting] 会计核算完成，各策略账本已对齐物理实盘。")
        send_webhook("⚖️ 会计核算完成", "收盘对账审计已执行，幽灵持仓已清除，手动持仓已隔离。")
    return success

def task_init():
    """08:50 触发：强制重启 QMT，建立初始通信握手，并执行全系统冒烟测试"""
    now_str = datetime.now().strftime('%H:%M:%S')
    print(f"[{now_str}] 🔌 激活早盘初始化协议...")
    
    # 1. 启动 QMT
    success = run_blocking(QMT_LAUNCHER, "QMT 物理自愈启动")
    if not success:
        msg = "❌ QMT 自动化登录失败，交易系统无法正常初始化。请手动检查并登录 QMT！"
        log.error(msg)
        send_webhook("🚨 预警：QMT 启动失败", msg)
        return False
        
    # 2. 自动运行冒烟测试与物理自愈
    log.info("🧪 [SmokeTest] 正在执行全系统冒烟测试与逻辑体检...")
    smoke_success = run_blocking(SMOKE_TEST, "自动化冒烟测试")
    if smoke_success:
        log.info("🎉 [SmokeTest] 冒烟测试全部通过，系统极度健康！")
        send_webhook("✅ 系统初始化成功", "QMT 已启动且全量脚本逻辑校验通过，今日环境完美。")
    else:
        log.warning("🚑 [SmokeTest] 冒烟提示存在隐患，但已尝试部分物理自愈。")
        send_webhook("⚠️ 系统初始化警告", "全量冒烟测试未完全通过，请检查日志并确认是否需要人工修正。")
    
    return True

def task_morning():
    """09:25 触发：先执行猎犬精选，再拉起早盘军团"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ☀️ 激活早盘军团...")
    registry = load_registry()
    
    # --- Part 1: 猎犬/精选系列 (通常需要阻塞等待结果) ---
    hunters = registry.get("hunter_strategies", [])
    for cfg in hunters:
        if cfg.get("enabled", True):
            proc = launch_strategy(cfg)
            if proc and cfg.get("wait", True):
                log.info(f"⏳ [{cfg.get('name')}] 正在生成实盘目标，请稍候...")
                proc.wait()
    
    # --- Part 2: 实盘执行系列 (非阻塞/看门狗保护) ---
    strategies = registry.get("morning_strategies", [])
    if not strategies:
        log.warning("⚠️ 注册表中未定义早盘策略")
        return

    for cfg in strategies:
        if cfg.get("enabled", True):
            proc = launch_strategy(cfg)
            # 如果要求等待（如选股脚本），则阻塞主线程
            if proc and cfg.get("wait", False):
                log.info(f"⏳ [{cfg.get('name')}] 为同步准备任务，等待完成...")
                proc.wait()
        else:
            log.info(f"⏩ 跳过已禁用的策略: {cfg.get('name')}")

def task_t1_master():
    """
    09:22 触发：T+1 网格盘前调度。
    阻塞执行 t1_master.py（解冻 + ATR 核算），完成后 executor 才可启动。
    """
    now_str = datetime.now().strftime('%H:%M:%S')
    log.info(f"[{now_str}] 📦 激活 T+1 网格参数核算（t1_master）...")
    success = run_blocking(T1_MASTER, "T+1网格参数核算")
    if success:
        log.info("✅ [T1_Master] 解冻与 ATR 参数核算完成。")
    else:
        log.warning("⚠️ [T1_Master] 参数核算失败，executor 将使用旧账本参数继续运行。")
    return success


def task_t1_grid_executor():
    """
    09:30 触发：以看门狗守护进程方式拉起 T+1 网格执行器。
    盘中自动轮询至 15:00 后退出，watchdog 保证崩溃后自动复活。
    """
    now_str = datetime.now().strftime('%H:%M:%S')
    log.info(f"[{now_str}] 🤖 激活 T+1 网格执行器（t1_grid_executor）...")
    run_daemon(
        name="T1网格执行器",
        script_rel_path=str(T1_EXECUTOR.relative_to(_DIR)),
        description="T+1 纯机械化网格盘中极速执行，09:30~15:00 定时守护",
        watchdog=True,
    )


def task_fat_fish_executor():
    """14:50 触发：胖鱼火炮，执行 Master 在 14:40 生成的买卖指令"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 💥 激活胖鱼火炮...")
    registry   = load_registry()
    strategies = registry.get("closing_strategies", [])
    for cfg in strategies:
        if cfg.get("name") == "胖鱼火炮" and cfg.get("enabled", True):
            proc = launch_strategy(cfg)
            if proc and cfg.get("wait", False):
                proc.wait()
            break

def task_fat_fish():
    """14:40 触发：胖鱼波段大脑，重算止损线 + 横截面突破扫描 + 生成交易指令文件"""

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🐟 激活胖鱼波段大脑...")
    registry = load_registry()
    strategies = registry.get("closing_strategies", [])
    for cfg in strategies:
        if cfg.get("name") == "胖鱼波段大脑" and cfg.get("enabled", True):
            proc = launch_strategy(cfg)
            if proc and cfg.get("wait", True):
                log.info("⏳ [胖鱼大脑] 计算中，等待完成...")
                proc.wait()
            break

def task_sniper():
    """14:46 触发：拉起注册表中的尾盘序列（三大板块全市场动量扫描）"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔫 激活尾盘狙击序列...")
    registry = load_registry()
    strategies = registry.get("closing_strategies", [])
    
    for cfg in strategies:
        if cfg.get("enabled", True):
            # 修正脚本路径为绝对路径以确保稳定性
            script = cfg.get("script", "")
            if script and not Path(script).is_absolute():
                cfg["script"] = str(Path(script)) # launch_strategy 会自动补全 _DIR
            proc = launch_strategy(cfg)
            # 如果要求等待（如雷达扫描），则阻塞主线程
            if proc and cfg.get("wait", False):
                log.info(f"⏳ [{cfg.get('name')}] 正在生成目标，等待扫描结束...")
                proc.wait()
        else:
            log.info(f"⏩ 跳过已禁用的策略: {cfg.get('name')}")

def task_eod():
    """15:30 触发：盘后算力暴破（四步串行，顺序强制保证）"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔋 激活盘后算力引擎...")
    # Step 1: 日线数据落盘（必须最先完成，为选股提供最新价格）
    run_blocking(SYNC_SCRIPT, "日线数据同步")
    # Step 2: 基于最新日线重算 ETF 宇宙（达尔文精选，更新 oracle_v2_universe.json）
    run_blocking(REFINE_UNIVERSE_SCRIPT, "ETF宇宙达尔文精选")
    # Step 3: 用最新宇宙下载 1m 数据（包含新入选 ETF 的分钟线）
    run_blocking(DL_1M_SCRIPT, "1m 高频增量缝合")
    # Step 4: 截面动量司令部扫描（依赖 Step1 最新日线 + Step2 最新宇宙）
    #         输出 .state/momentum_slots.json → 供明日 momentum_vector_executor 消费
    run_blocking(MOMENTUM_MASTER, "截面动量司令部选股")

def task_rotation():
    """14:53 触发：ETF 截面轮动与无情调仓 (仅限周五)"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🎡 激活 ETF 轮动引擎 (Friday Mode)...")
    registry = load_registry()
    strategies = registry.get("rotation_strategies", [])
    
    for cfg in strategies:
        if cfg.get("enabled", True):
            proc = launch_strategy(cfg)
            # 必须阻塞，确保信号生成后再执行调仓
            if proc and cfg.get("wait", True):
                log.info(f"⏳ [{cfg.get('name')}] 执行中，等待信号/指令完成...")
                proc.wait()
        else:
            log.info(f"⏩ 跳过已禁用的策略: {cfg.get('name')}")


def task_macro_rotation():
    """
    14:42 周五触发：宏观轮动 V2 进攻窗口。
    调用 macro_rotation_executor.py，全量扫描 9 只标的，执行双槽阈值洗牌。
    脚本内部已有时间线守卫（非周五自动退出），此处同样有 weekday 过滤。
    """
    now_str = datetime.now().strftime('%H:%M:%S')
    log.info(f"[{now_str}] 🚀 激活宏观轮动 V2 进攻窗口（周五）...")
    success = run_blocking(MACRO_EXECUTOR, "MacroRotation-V2进攻")
    if success:
        log.info("✅ [MacroRotation] 进攻窗口执行完毕，槽位已更新。")
        send_webhook("✅ MacroRotation V2 完成", "周五进攻窗口：双槽已按预言机赔率洗牌。")
    else:
        log.warning("⚠️ [MacroRotation] 进攻执行异常，请检查日志。")
        send_webhook("⚠️ MacroRotation V2 异常", "请检查 macro_rotation_executor 日志！")
    return success


def task_macro_sentinel():
    """
    14:42 周一至周四触发：物理熔断守卫（The Sentinel）。
    调用 macro_risk_monitor.py，仅扫描当前持仓，触发阈值则强制切换至国债。
    """
    now_str = datetime.now().strftime('%H:%M:%S')
    log.info(f"[{now_str}] 🛡️ 激活宏观轮动 V2 守卫巡逻（周一至周四）...")
    success = run_blocking(MACRO_SENTINEL, "MacroRotation-Sentinel守卫")
    if success:
        log.info("✅ [Sentinel] 守卫巡逻完毕。")
    else:
        log.warning("⚠️ [Sentinel] 守卫巡逻异常，请检查日志。")
    return success


def task_momentum_t1_auction():
    """
    09:20 触发：执行动量系统昨日记录的 T+1 挂单信号（集合竞价阻塞执行）。
    momentum_vector_executor 启动时内部会在 09:25 自动处理，
    此处提前以独立阻塞脚本形式执行，确保集合竞价不被遗漏。
    注意：脚本本身也有自我保护（只在 09:20-09:30 区间处理 T+1 信号）。
    """
    now_str = datetime.now().strftime('%H:%M:%S')
    log.info(f"[{now_str}] ⏰ 激活动量系统 T+1 集合竞价挂单...")
    # momentum_vector_executor 以 --t1-only 模式运行：只执行 T+1 信号，不启动主循环
    success = run_blocking_with_args(
        MOMENTUM_EXECUTOR, "动量T+1集合竞价", ["--t1-only"]
    )
    if success:
        log.info("✅ [MomentumT1] T+1 集合竞价挂单执行完毕。")
    else:
        log.warning("⚠️ [MomentumT1] T+1 挂单执行异常或无待处理信号（正常）。")
    return success


def task_settlement():

    """16:05 触发：由于 QMT 只有在收盘后数据最稳，此时执行物理清算"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚖️ 激活物理清算引擎...")
    run_blocking(SETTLEMENT_SCRIPT, "每日账目清算")

def task_daily_trade():
    """17:30 触发：券商结算完成后，拉取当日成交，按月分文件夹落盘 CSV"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📋 激活每日成交落盘...")
    run_blocking(DAILY_TRADE_SCRIPT, "每日成交落盘")

def task_debrief():
    """16:10 触发：读取今日日志与清算 JSON，并生成 AI 战报"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📝 激活每日复盘引擎...")
    run_blocking(DEBRIEF_SCRIPT, "每日复盘报告")

def task_research():
    """16:15 触发：由于计算量巨大，放在盘后执行，更新下一交易日的配对参数"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🧠 [DISABLED] 统计套利参数挖掘已禁用")
    # run_blocking(RESEARCH_SCRIPT, "统计套利参数挖掘")

def task_knowledge_sync():
    """18:00 触发：自动分析今日代码变更并记录进入进化日志 (Skill)，并备份记忆系统"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🧬 激活每日知识进化同步...")
    run_blocking(KNOWLEDGE_SYNC_SCRIPT, "每日知识进化同步")

    # 🗂️ 备份 GEMINI.md 到 Z:\（KI knowledge 目录已是 Junction，自动同步）
    try:
        import shutil
        _gemini_src = Path(r"C:\Users\quant366\.gemini\GEMINI.md")
        _backup_dir = _DIR.parent / "Agent_Memory" / "gemini_config"
        _backup_dir.mkdir(parents=True, exist_ok=True)
        if _gemini_src.exists():
            shutil.copy2(str(_gemini_src), str(_backup_dir / "GEMINI.md"))
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ [Memory Backup] GEMINI.md → {_backup_dir}")
    except Exception as _e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ [Memory Backup] GEMINI.md 备份失败: {_e}")

def task_mcp_server():
    """[已停用] MCP 服务器改用外部方案，此函数保留为空操作。"""
    pass


def task_shutdown():
    """
    23:00 触发：自动关机。
    - 优先尝试休眠 (shutdown /h)，保留登录状态
    - 若休眠失败（系统未启用 hiberfil），fallback 到普通关机 (shutdown /s /t 60)
    """
    now_str = datetime.now().strftime('%H:%M:%S')
    print(f"[{now_str}] 💤 23:00 定时关机指令已发出...")
    log.info("💤 23:00 定时关机指令已发出，10s 后执行...")
    send_webhook("💤 定时关机", "AutoPilot 触发 23:00 定时关机，次日由 WOL 唤醒。")
    time.sleep(10)

    # ── 优先尝试休眠 ────────────────────────────────────────────────
    log.info("💤 [Shutdown] 尝试休眠 (shutdown /h)...")
    rc_h = subprocess.call(
        ["cmd", "/c", "shutdown /h"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if rc_h != 0:
        # 休眠不可用（系统未配置 hiberfil），fallback 到普通关机
        log.warning(f"⚠️ [Shutdown] 休眠指令返回 rc={rc_h}，系统可能未启用休眠，"
                    f"fallback 到普通关机 (shutdown /s /t 60)...")
        send_webhook("⚠️ 休眠失败，已切换为关机",
                     f"shutdown /h 返回 rc={rc_h}，已改发 shutdown /s /t 60")
        rc_s = subprocess.call(
            ["cmd", "/c", "shutdown /s /t 60"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if rc_s != 0:
            log.error(f"🚨 [Shutdown] 关机指令也失败了！rc={rc_s}，请手动处理。")
            send_webhook("🚨 关机彻底失败",
                         f"shutdown /s /t 60 返回 rc={rc_s}，需要人工介入关机！")
        else:
            log.info("✅ [Shutdown] 关机指令已发出，60秒后系统将关闭。")
    else:
        log.info("✅ [Shutdown] 休眠指令已接受，系统即将休眠。")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto Pilot 终极路由网关")
    parser.add_argument("--task", type=str, required=True,
                        choices=['init', 'morning', 'sniper', 'eod', 'loop'])
    parser.add_argument("--restart-count", type=int, default=0,
                        help="内部参数：自拉起计数器（用户无需手动指定）")
    args = parser.parse_args()

    # 交易日校验（循环监控除外）
    if args.task != 'loop' and not is_trading_day():
        print(f"今日 ({date.today()}) 非交易日，跳过任务: {args.task}")
        sys.exit(0)

    if args.task == 'init':      task_init()
    elif args.task == 'morning': task_morning()
    elif args.task == 'sniper':  task_sniper()
    elif args.task == 'eod':     task_eod()
    elif args.task == 'loop':
        # ── 自我复活守护壳 ────────────────────────────────────────────────
        MAX_SELF_RESTART = 10   # 每天最多自拉起 10 次，防止无限崩溃风暴
        RESTART_COOLDOWN = 30   # 崩溃后冷静 30 秒再重启
        restart_count = args.restart_count

        while True:
            try:
                task_loop()        # 正常退出时（如 KeyboardInterrupt）跳出外层
                break
            except KeyboardInterrupt:
                print("⏹️ 主循环被用户手动停止，不再自拉起。")
                break
            except Exception as crash_err:
                restart_count += 1
                err_msg = (f"🔥 [自愈] AutoPilot 主循环崩溃 (第 {restart_count} 次) "
                           f"| 原因: {crash_err}")
                print(err_msg)
                try:
                    send_webhook("🚨 AutoPilot 主循环崩溃，正在自拉起", err_msg)
                except Exception:
                    pass

                if restart_count > MAX_SELF_RESTART:
                    fatal_msg = (f"💀 [自愈熔断] 今日已崩溃 {restart_count} 次，"
                                 f"超出上限 {MAX_SELF_RESTART}，放弃自拉起，请人工干预！")
                    print(fatal_msg)
                    try:
                        send_webhook("💀 AutoPilot 自愈熔断", fatal_msg)
                    except Exception:
                        pass
                    sys.exit(1)

                print(f"   ⏳ 冷静 {RESTART_COOLDOWN} 秒后重启循环...")
                time.sleep(RESTART_COOLDOWN)
                # 继续外层 while 循环，重新调用 task_loop()
