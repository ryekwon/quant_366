import os
import psutil
import signal
import time
from pathlib import Path

def cleanup():
    print("Starting process cleanup...")
    current_pid = os.getpid()
    
    # Kill all python processes EXCEPT this one and its parents
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['name'] == 'python.exe' and proc.info['pid'] != current_pid:
                # Check if it's our workspace python
                cmdline = " ".join(proc.info['cmdline'] or [])
                if 'Quant_Pilot' in cmdline or 'python310' in cmdline:
                    print(f"Killing hung Python: PID {proc.info['pid']} - {cmdline[:50]}...")
                    proc.kill()
            
            # Also kill QMT if it might be causing the hang
            if proc.info['name'] in ['XtMiniQmt.exe', 'XtItClient.exe']:
                print(f"Killing QMT: PID {proc.info['pid']}...")
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    print("Cleanup phase 1 complete. Waiting 2 seconds...")
    time.sleep(2)

def restart_and_sync():
    # 1. Start QMT
    print("Launching QMT...")
    launcher = r"z:\QuantpC_Workspace\Quant_Pilot\start_miniQMT.py"
    venv_python = r"z:\QuantpC_Workspace\Quant_Pilot\.venv\Scripts\python.exe"
    
    # We use venv python to run the launcher
    os.system(f"{venv_python} {launcher}")
    print("Wait for QMT to initialize (10s)...")
    time.sleep(10)
    
    # 2. Run 1m Downloader
    print("Launching 1m Downloader...")
    downloader = r"z:\QuantpC_Workspace\Quant_Pilot\qmt_1m_downloader.py"
    # Run it directly
    os.system(f"{venv_python} {downloader}")
    print("Sync complete.")

if __name__ == "__main__":
    cleanup()
    restart_and_sync()
