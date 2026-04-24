# -*- coding: utf-8 -*-
"""
register_autopilot_task.py — 一键注册 Windows 任务计划
======================================================
运行一次即可注册，之后每个工作日 08:45 自动启动 autopilot_master.py。
无需 PM2、无需 BAT 文件。

用法:
  python register_autopilot_task.py          # 注册 / 更新任务
  python register_autopilot_task.py --delete # 删除任务
  python register_autopilot_task.py --status # 查询任务状态
"""
import subprocess
import sys
import os
from pathlib import Path

# 强制 UTF-8，防止 Windows GBK 控制台 emoji 乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── 配置 ────────────────────────────────────────────────────
TASK_NAME   = "QuantPilot_AutoPilot"
START_TIME  = "08:45"           # 每天触发时间
SCRIPT      = Path(__file__).parent / "autopilot_master.py"
PYTHON_EXE  = Path(sys.executable)   # 当前 venv 的 python
LOG_XML     = Path(__file__).parent / "logs" / "task_scheduler.log"
# ────────────────────────────────────────────────────────────

Path(LOG_XML).parent.mkdir(exist_ok=True)


def _run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="gbk", errors="replace")
    return r.returncode, (r.stdout + r.stderr).strip()


def register():
    """注册（或覆盖）任务计划"""
    # schtasks 参数说明：
    #   /sc WEEKLY /d MON,TUE,WED,THU,FRI  → 周一到周五
    #   /st 08:45                           → 每天 08:45
    #   /tr "cmd /c ..."                    → 触发命令
    #   /rl HIGHEST                         → 最高权限运行
    #   /f                                  → 强制覆盖已有同名任务
    cmd_str = f'cmd /c "cd /d "{SCRIPT.parent}" && "{PYTHON_EXE}" "{SCRIPT}" --task loop >> "{LOG_XML}" 2>&1"'

    cmd = [
        "schtasks", "/create",
        "/tn", TASK_NAME,
        "/tr", cmd_str,
        "/sc", "WEEKLY",
        "/d",  "MON,TUE,WED,THU,FRI",
        "/st", START_TIME,
        "/rl", "HIGHEST",
        "/f",                     # 强制覆盖
    ]
    rc, out = _run(cmd)
    if rc == 0:
        print(f"✅ 任务已注册成功！")
        print(f"   任务名称 : {TASK_NAME}")
        print(f"   触发时间 : 每个工作日 {START_TIME}")
        print(f"   执行脚本 : {SCRIPT}")
        print(f"   Python   : {PYTHON_EXE}")
        print(f"   日志输出 : {LOG_XML}")
        print(f"\n💡 可在「任务计划程序」→「任务计划程序库」中查看和管理此任务。")
    else:
        print(f"❌ 注册失败：{out}")
        print(f"   请以管理员身份运行此脚本，或手动在任务计划程序中创建。")


def delete():
    rc, out = _run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])
    if rc == 0:
        print(f"✅ 任务 [{TASK_NAME}] 已删除。")
    else:
        print(f"❌ 删除失败（任务可能不存在）：{out}")


def status():
    rc, out = _run(["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST"])
    if rc == 0:
        print(out)
    else:
        print(f"⚠️ 未找到任务 [{TASK_NAME}]（尚未注册）。")


if __name__ == "__main__":
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if arg == "--delete":
        delete()
    elif arg == "--status":
        status()
    else:
        register()
