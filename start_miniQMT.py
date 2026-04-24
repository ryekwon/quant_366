# -*- coding: utf-8 -*-
"""
start_miniQMT.py — miniQMT 免密自动登录机器人
=================================================
使用前置步骤：
  1. 手动登录一次 QMT，截取以下 3 张图并放到项目根目录：
       pwd.png    —— 密码输入框（点击区域）
       check.png  —— "独立交易" 复选框（务必截 【未勾选】 状态）
       buy.png    —— 登录成功后出现的 "买入" 按钮
  2. .env 里配置好 QMT_EXE_PATH 和 QMT_PASSWORD（见下方说明）
  3. pip install psutil pyautogui pillow opencv-python

.env 新增字段示例:
  QMT_EXE_PATH=C:\\国金证券QMT交易端\\bin.x64\\XtMiniQmt.exe
  QMT_PASSWORD=your_password_here

用法:
  import start_miniQMT
  ok = start_miniQMT.start_miniQMT()
  if not ok:
      print("需要手动登录")
"""
import os
import sys
import time
import subprocess
try:
    import win32gui
    import win32con
    _HAS_WIN32 = True
except ImportError:
    _HAS_WIN32 = False
    print("   ⚠️ pywin32 未安装，win32gui 窗口强制唤醒功能降级（将使用 pygetwindow 兜底）")

# ⚠️ 注意：不要在模块顶层执行 taskkill！
# 清场逻辑已移入 _login_miniQMT()，仅在无法静默启动、需要重拉 GUI 时才执行。
# 顶层 kill 会在 import 时就杀掉用户手动运行的 QMT，是严重陷阱。

# 强制 UTF-8 输出，防止 Windows GBK 控制台在子进程模式下 UnicodeEncodeError
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import psutil
import pyautogui
import pyperclip
import pygetwindow as gw
from dotenv import load_dotenv
from xtquant import xtdata, xttrader

# ================= 配置区（从 .env 读取）=================
load_dotenv()
QMT_EXE_PATH = os.getenv("QMT_EXE_PATH")       # miniQMT 可执行文件完整路径
QMT_PASSWORD = os.getenv("QMT_PASSWORD", "")   # 登录密码
# 图片模板（放在脚本同目录）
_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_PWD   = os.path.join(_DIR, ".png/pwd.png")    # 密码框
IMG_CHECK = os.path.join(_DIR, ".png/check.png")  # 独立交易复选框（未勾选）
IMG_BUY     = os.path.join(_DIR, ".png/buy.png")    # 买入按钮（登录成功标志）
# 可选：截取登录按钮本身（更精准），若 login.png 不存在则回退到 Enter 键
IMG_LOGIN   = os.path.join(_DIR, ".png/login.png")  # 登录按钮（可选）
# 可选：截取验证码输入框，若存在则检测验证码并暂停等待人工填写
IMG_CAPTCHA = os.path.join(_DIR, ".png/captcha.png")  # 验证码输入框（可选）
CAPTCHA_WAIT_SEC = 60  # 等待人工填写验证码的最大秒数

WAIT_TIMEOUT   = 60   # 等待界面元素出现的最长秒数
POLL_INTERVAL  = 1.0  # 轮询间隔（秒）
# =========================================================

pyautogui.FAILSAFE = True   # 鼠标移到左上角可紧急中断
pyautogui.PAUSE    = 0.3    # 每次操作后稍作停停顿，防止过快


# ─── 强制唤醒窗口 ──────────────────────────────────────────

def force_restore_window(keyword):
    """
    遍历寻找包含关键字的窗口句柄并强制还原和置顶
    """
    if _HAS_WIN32:
        def callback(hwnd, extra):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if keyword.lower() in title.lower():
                    extra.append(hwnd)
            return True
        
        hwnds = []
        win32gui.EnumWindows(callback, hwnds)
        
        if hwnds:
            hwnd = hwnds[0]
            title = win32gui.GetWindowText(hwnd)
            print(f"👁️ 找到 QMT 窗口句柄: {hwnd} ({title})，正在强制唤醒...")
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception as e:
                print(f"   ⚠️ SetForegroundWindow 异常 (通常是因为焦点限制): {e}")
            time.sleep(1)
            return True
        
        print(f"   ⚠️ 未找到包含 '{keyword}' 的窗口")
        return False
    else:
        # pywin32 不可用，降级到 pygetwindow
        try:
            import pygetwindow as gw
            wins = [w for w in gw.getAllWindows() if keyword.lower() in w.title.lower()]
            if wins:
                win = wins[0]
                if win.isMinimized:
                    win.restore()
                win.activate()
                time.sleep(1)
                return True
        except Exception as e:
            print(f"   ⚠️ pygetwindow 降级也失败: {e}")
        return False


# ─── 底层工具函数 ──────────────────────────────────────────

def _is_process_running(name: str) -> bool:
    """检查指定进程名是否在运行"""
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == name.lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def _close_running_process(name: str):
    """强制终止指定进程（大 QMT / miniQMT 均适用）"""
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == name.lower():
                proc.kill()
                print(f"   💀 已终止进程: {name}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def _wait_for_image(img_path: str, timeout: int = WAIT_TIMEOUT,
                    confidence: float = 0.75) -> bool:
    """轮询屏幕，直到找到目标图像或超时"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # 捕获异常：如果 Windows 处于锁定状态或非交互式会话，pyautogui 获取屏幕会报错
            loc = pyautogui.locateOnScreen(img_path, confidence=confidence)
            if loc:
                return True
        except pyautogui.ImageNotFoundException:
            pass
        except Exception as e:
            # 这里的 e 可能是 'screen grab failed'
            print(f"   ⚠️ 图像识别异常: {e}")
            if "screen grab failed" in str(e).lower():
                print("   🚨 [CRITICAL] 无法获取屏幕图像。可能是 RDP 断开、屏幕锁定或正在作为后台任务运行。")
                print("   💡 请确保在交互式环境下（远程桌面窗口保持开启）启动此脚本。")
                return False  # 无法捕捉屏幕时没必要继续轮询
        time.sleep(POLL_INTERVAL)
    return False


def _find_and_click(img_path: str, confidence: float = 0.75) -> bool:
    """找到图像中心并单击"""
    try:
        loc = pyautogui.locateCenterOnScreen(img_path, confidence=confidence)
        if loc:
            pyautogui.click(loc)
            return True
    except pyautogui.ImageNotFoundException:
        pass
    return False


def _test_connection() -> bool:
    """尝试连接 xtdata 和 xttrader，验证 miniQMT 是否真正可用"""
    try:
        # 1. 验证行情数据连接
        xtdata.connect()
        print("   ✅ xtdata (行情) 连接成功")
        
        # 2. 验证交易网关连接 (重要：Silent Launch 必须要这一步成功才算真成功)
        qmt_path = os.getenv("QMT_PATH")
        account_id = os.getenv("ACCOUNT_ID")
        
        if not qmt_path or not account_id:
            print("   ⚠️ 缺少 QMT_PATH 或 ACCOUNT_ID 配置，跳过交易网关检查")
            return True # 降级处理，仅验证行情
            
        session_id = int(time.time())
        trader = xttrader.XtQuantTrader(qmt_path, session_id)
        trader.start()
        # 【Bug修复】xtquant 规范：同一 session 两次 connect 间隔必须 >3 秒
        # start() 到 connect() 之间若无等待，交易网关尚未初始化，必然返回 -1
        print("   ⏳ 等待交易网关初始化（5 秒）...")
        time.sleep(5)
        res = trader.connect()
        
        if res == 0:
            print("   ✅ XtQuantTrader (交易) 连接成功")
            trader.stop() # 验证完即关闭，释放 session
            return True
        else:
            print(f"   ❌ XtQuantTrader 连接失败 (错误码: {res})")
            return False
            
    except Exception as e:
        print(f"   ❌ 连接验证异常: {e}")
        return False


# ─── 主登录流程 ───────────────────────────────────────────

def _login_miniQMT() -> bool:
    """执行完整的 miniQMT 自动登录流程"""
    if not QMT_EXE_PATH:
        print("❌ .env 缺少 QMT_EXE_PATH，请配置 miniQMT 可执行文件路径")
        return False
    if not os.path.exists(QMT_EXE_PATH):
        print(f"❌ 找不到 miniQMT 可执行文件: {QMT_EXE_PATH}")
        return False

    for img, label in [(IMG_PWD, "pwd.png"), (IMG_CHECK, "check.png"), (IMG_BUY, "buy.png")]:
        if not os.path.exists(img):
            print(f"❌ 缺少模板图片: {label}，请截图后放置到项目根目录")
            return False

    # 1. 优先尝试：静默启动（Headless 模式，尝试绕过 GUI 登录）
    # 使用 -Xtxmini 标志尝试静默拉起后端服务
    print(f"🚀 尝试静默启动 (Silent Launch): {QMT_EXE_PATH}")
    subprocess.Popen([QMT_EXE_PATH, "-Xtxmini"], shell=True)
    
    print("   ⏳ 等待后端服务初始化（45 秒）...《 WOL 冷启动后 XtQuantTrader 网关需要更长时间就绪 》")
    time.sleep(45)
    
    if _test_connection():
        print("   ✨ 静默启动成功！已绕过 GUI 登录界面。")
        return True
        
    print("   ⚠️ 静默启动未能在预期内建立连接，尝试切换至 GUI 自动化模式...")

    # 2. 兜底方案：GUI 自动化登录
    # 如果静默启动没成功，可能需要物理拉起 UI 进行交互
    # 先清理卡住的静默进程，并物理清场残留句柄
    print("🧹 正在物理歼灭残留的 QMT 进程...")
    os.system("taskkill /F /IM XtMiniQmt.exe /T >nul 2>&1")
    os.system("taskkill /F /IM XtItClient.exe /T >nul 2>&1")
    _close_running_process("XtMiniQmt.exe")
    time.sleep(3)  # 等待句柄彻底释放

    print(f"🚀 物理启动 QMT GUI: {QMT_EXE_PATH}")
    subprocess.Popen(QMT_EXE_PATH, shell=True)
    print("   ⏳ 等待 QMT 窗口渲染（8 秒）...")
    time.sleep(8) 

    # ✨ 补丁：确保窗口未被最小化且处于最前端
    try:
        title = "XtMiniQmt"
        wins = gw.getWindowsWithTitle(title)
        if wins:
            win = wins[0]
            if win.isMinimized:
                print("   📥 检测到窗口已最小化，正在恢复...")
                win.restore()
            print("   🔝 正在将 QMT 窗口推至最前端...")
            win.activate()
            time.sleep(1)
    except Exception as e:
        print(f"   ⚠️ 激活窗口失败: {e}")

    # ✨ 补丁：强制唤醒窗口（利用 win32gui 底层接口）
    force_restore_window("QMT")

    # 3. 等待密码输入框出现
    print("⏳ 轮询屏幕，等待密码输入框出现（最长 60 秒）...")
    if not _wait_for_image(IMG_PWD, timeout=WAIT_TIMEOUT):
        # 无论为何种失败（超时或图像截取失败），都尝试截图留存
        debug_shot = os.path.join(_DIR, "logs", f"debug_shot_{int(time.time())}.png")
        os.makedirs(os.path.dirname(debug_shot), exist_ok=True)
        try:
            pyautogui.screenshot(debug_shot)
            print(f"   📸 已保存当前屏幕截图至: {debug_shot}")
        except Exception as e:
            print(f"   ⚠️ 自动截图也失败了: {e}")
        
        print("❌ 启动失败：未检测到登录界面。QMT 程序可能未渲染 UI，或者当前环境缺乏 GUI 访问权限。")
        return False
    print("   ✅ 登录界面已就绪")

    # 4. 点击密码框并粘贴密码（用剪贴板避免 typewrite 不支持特殊字符）
    _find_and_click(IMG_PWD)
    time.sleep(0.4)
    pyautogui.hotkey('ctrl', 'a')   # 清空旧内容
    pyperclip.copy(QMT_PASSWORD)    # 写入剪贴板
    pyautogui.hotkey('ctrl', 'v')   # 粘贴
    print("   ✅ 密码已输入")

    # ✨ 补丁：检测是否出现了验证码（CAPTCHA）
    # 如果存在 captcha.png 模板且屏幕上能找到验证码输入框，则等待人工填写
    _has_captcha = False
    if os.path.exists(IMG_CAPTCHA):
        try:
            loc = pyautogui.locateOnScreen(IMG_CAPTCHA, confidence=0.75)
            if loc:
                _has_captcha = True
        except Exception:
            pass

    if _has_captcha:
        # 验证码出现 → 自动化无法处理 → 立即告警 + 快速失败
        # 用户可能不在电脑旁，60秒等待没有意义，直接失败让 autopilot 告警通知
        print("   🚨 检测到验证码！自动化无法处理，立即发送告警并退出...")
        _n8n = os.getenv('N8N_WEBHOOK_URL', '')
        if _n8n:
            try:
                import requests as _req
                _req.post(_n8n, json={
                    "title": "🚨 QMT 出现验证码，需要手动登录",
                    "message": "start_miniQMT 检测到验证码，自动化登录无法继续。\n请手动登录 QMT 后，再重新启动 autopilot_master.py。",
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                }, timeout=5)
            except Exception:
                pass
        return False  # 快速失败，不继续执行后续步骤
    else:
        # 无验证码，给 2 秒缓冲
        print("   🔍 正在检查界面状态（未检测到验证码）...")
        time.sleep(2)

    # 5. 勾选「独立交易」(miniQMT 模式)
    time.sleep(0.5)
    if _find_and_click(IMG_CHECK, confidence=0.90):
        print("   ✅ 已勾选独立交易（miniQMT 模式）")
    else:
        print("   ⚠️ 未找到独立交易复选框，跳过（可能已勾选）")

    # 6. 重新点击密码框确保焦点，再点击登录按钮 / 按 Enter
    time.sleep(0.4)
    _find_and_click(IMG_PWD)        # 重新聚焦密码框，防止勾选框操作抢走焦点
    time.sleep(0.3)

    # 优先尝试点击登录按钮图片（更可靠），回退到按 Enter
    if os.path.exists(IMG_LOGIN) and _find_and_click(IMG_LOGIN, confidence=0.80):
        print("   ✅ 已点击登录按钮")
    else:
        pyautogui.press('enter')
        time.sleep(0.8)
        pyautogui.press('enter')    # 双击保险，防止第一次 Enter 被截断
        print("   ✅ 已按回车登录（如仍未跳转，请截取 login.png 放入项目目录）")
    print("   ⏳ 正在登录，等待主界面出现...")

    # 7. 等待买入按钮（登录成功标志）
    if not _wait_for_image(IMG_BUY, timeout=WAIT_TIMEOUT):
        print("   ⚠️ 图像检测超时（buy.png 匹配失败，可能是屏幕分辨率与模板截图时不一致）")
        print("   🔄 尝试 API 级兑底验证（直接测试交易网关连接）...")
        # 先截图存档
        debug_shot = os.path.join(_DIR, "logs", f"debug_shot_{int(time.time())}.png")
        try:
            pyautogui.screenshot(debug_shot)
            print(f"   📸 已保存当前屏幕截图至: {debug_shot}")
        except Exception:
            pass
        # 用 API 实际验证 QMT 是否已就绪（不信图像，信网关）
        if _test_connection():
            print("   ✅ API 验证通过！QMT 已正常运行，图像检测为误报。")
            print("   💡 建议：在实际运行环境（相同分辨率）下重新截取 buy.png。")
            return True
        print("❌ 超时：未检测到买入按钮，且 API 验证也失败。登录可能未完成")

    print("   ✅ 主界面出现")

    # 8. 额外等待交易网关完成握手，再做连接验证
    print("   ⏳ 等待交易网关完成握手（3 秒）...")
    time.sleep(3)
    return _test_connection()


# ─── 对外接口 ─────────────────────────────────────────────

def start_miniQMT() -> bool:
    """
    启动并登录 miniQMT。
    
    Returns:
        True  — 启动并连接成功
        False — 失败，需要人工介入
    """
    print("=" * 50)
    print("🤖 miniQMT 自动登录机器人启动")
    print("=" * 50)

    # 如果 miniQMT 已在运行，直接验证连接
    # ⚠️ [BUG FIX 2026-04-16] 进程存在 ≠ 服务就绪：冷启动后进程刚拉起，
    # xtquant 网关需要额外时间初始化握手。增加最多 3 次 × 15s 重试。
    if _is_process_running("XtMiniQmt.exe"):
        print("ℹ️  检测到 miniQMT 已在运行，跳过启动流程，直接验证连接...")
        _MAX_RETRY = 3
        _RETRY_WAIT = 15  # 秒
        for attempt in range(1, _MAX_RETRY + 1):
            if _test_connection():
                return True
            if attempt < _MAX_RETRY:
                print(f"   ⏳ 连接未就绪，{_RETRY_WAIT}s 后重试（第 {attempt}/{_MAX_RETRY} 次）...")
                time.sleep(_RETRY_WAIT)
        print(f"   ❌ 经 {_MAX_RETRY} 次重试仍无法连接，触发重新登录流程...")
        # 重试耗尽仍失败 → 走完整登录流程（自动清场旧进程并重拉 GUI）
        return _login_miniQMT()

    success = _login_miniQMT()
    if success:
        print("\n🟢 miniQMT 自动登录完成，Auto Pilot 可以开始工作！")
    else:
        print("\n🔴 自动登录失败，请手动登录 miniQMT 后再启动交易引擎。")
    return success


# ─── 独立运行入口 ──────────────────────────────────────────
if __name__ == "__main__":
    result = start_miniQMT()
    sys.exit(0 if result else 1)
