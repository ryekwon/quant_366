# -*- coding: utf-8 -*-
"""
QuantLab CEO Dashboard (Alpha 1.0)
功能: T0 织布机实时状态全景透视，预留多策略扩展槽位。
运行: streamlit run web_dashboard.py --server.port 8501
"""
import streamlit as st
import yaml
import json
import pandas as pd
import os
from datetime import datetime

# ================= 1. 页面级配置 =================
st.set_page_config(
    page_title="QuantLab | Auto Pilot",
    page_icon="🧬",
    layout="wide", # 宽屏模式，适合大屏/Kanban
    initial_sidebar_state="expanded"
)

# 动态加载数据文件
STATE_FILE   = ".state/grid_state.json"
TARGETS_FILE = ".state/grid_targets.yaml"
STATUS_FILE  = ".state/autopilot_status.json"   # autopilot_master 写入的策略状态中心

def load_data():
    targets, state = {}, {}
    if os.path.exists(TARGETS_FILE):
        with open(TARGETS_FILE, 'r', encoding='utf-8') as f:
            targets = yaml.safe_load(f) or {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f) or {}
    return targets, state

def load_autopilot_status() -> dict:
    """读取 autopilot_master 写入的策略运行状态中心文件"""
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f) or {}
        except Exception:
            pass
    return {}

# ================= 2. 侧边栏 (策略路由预留) =================
with st.sidebar:
    st.title("🧬 QuantLab 中枢")
    st.markdown("---")
    # 这里就是你要求的“留有余地”，以后加策略直接在这里加菜单
    app_mode = st.radio(
        "作战模块导航",
        ["🤖 Auto Pilot 调度中枢", "🕸️ T0 织布机 (Weaver)", "🔫 20CM 狙击雷达 (Sniper)", "📊 万物关联性矩阵 - 待开发"]
    )
    st.markdown("---")
    st.caption(f"系统刷新时间: {datetime.now().strftime('%H:%M:%S')}")
    if st.button("🔄 强制刷新数据"):
        st.rerun()

# ================= 3. 核心大屏面板 =================
# ─── 调度中枢页面 ────────────────────────────────────────────
if app_mode == "🤖 Auto Pilot 调度中枢":
    st.title("🤖 Auto Pilot 调度中枢")
    st.markdown("实时监控 `autopilot_master.py` 调度的所有子策略进程状态。")

    ap_status = load_autopilot_status()

    # 时间轴状态栏
    now_hhmm = datetime.now().strftime("%H%M")
    schedule = [
        ("0850", "08:50", "QMT 看门狗启动"),
        ("0920", "09:20", "早盘策略启动"),
        ("0925", "09:25", "Web Dashboard 启动"),
        ("1530", "15:30", "日终数据同步（阻塞）"),
        ("1600", "16:00", "尾盘策略启动"),
        ("1630", "16:30", "收盘，调度结束"),
    ]
    st.subheader("📅 今日调度时间轴")
    cols = st.columns(len(schedule))
    for i, (hhmm, label, desc) in enumerate(schedule):
        is_past = now_hhmm >= hhmm
        icon = "✅" if is_past else "⏳"
        with cols[i]:
            st.metric(f"{icon} {label}", desc)

    st.markdown("---")
    st.subheader("🗂️ 策略进程状态看板")

    if not ap_status:
        st.info("⏳ 尚无策略状态数据。请先启动 `python autopilot_master.py`，或等待 09:20 节点触发。")
    else:
        STATUS_ICONS = {
            "running":  "🟢 运行中",
            "stopped":  "⚪ 已停止",
            "error":    "🔴 异常退出",
            "blocking": "🔵 阻塞执行中",
        }
        grid = st.columns(min(len(ap_status), 3))
        for idx, (key, info) in enumerate(sorted(ap_status.items())):
            with grid[idx % 3]:
                status_text = STATUS_ICONS.get(info.get("status", ""), "❓ 未知")
                pid  = info.get("pid")
                name = info.get("strategy_name", key)
                desc = info.get("description", "—")
                started = info.get("started_at", "—")
                script  = info.get("script", "—")

                # 用颜色区分状态
                if info.get("status") == "running":
                    st.success(f"**{name}**")
                elif info.get("status") == "error":
                    st.error(f"**{name}**")
                elif info.get("status") == "blocking":
                    st.info(f"**{name}**")
                else:
                    st.warning(f"**{name}**")

                st.write(f"🏷️ **状态**: {status_text}")
                st.write(f"⚙️ **脚本**: `{script}`")
                st.write(f"🔢 **PID**: `{pid if pid else 'N/A'}`")
                st.write(f"🕐 **启动时间**: {started}")
                st.caption(f"📝 {desc}")
                st.markdown("")

    st.markdown("---")
    with st.expander("🔍 查看完整状态 JSON"):
        st.json(ap_status)

elif app_mode == "🕸️ T0 织布机 (Weaver)":
    st.title("🕸️ T0 织布机实时看板 (Weaver Dashboard)")
    st.markdown("监控 `t0_multigrid_executor.py` 底层物理运行状态。")
    
    targets, state = load_data()
    
    if not targets and not state:
        st.warning("⚠️ 暂无数据：未能读取到 YAML 或 JSON 配置。请确认猎犬和织布机已运行。")
        st.stop()

    # --- 模块 A: 全局概览 (Top Metrics) ---
    col1, col2, col3, col4 = st.columns(4)
    active_targets = len(targets)
    total_positions = sum(1 for v in state.values() if v.get('position', 0) > 0)
    orphans = [code for code in state.keys() if code not in targets and state[code].get('position', 0) > 0]
    
    col1.metric("今日猎物数量", f"{active_targets} 只")
    col2.metric("当前有仓位标的", f"{total_positions} 只")
    col3.metric("待清退孤儿仓位", f"{len(orphans)} 笔", delta="-待抛售" if orphans else "健康", delta_color="inverse")
    col4.metric("引擎状态", "🟢 在线守护" if os.path.exists(STATE_FILE) else "🔴 离线")

    st.markdown("---")
    
    # --- 模块 B: Kanban 卡片视图 (视觉直观) ---
    st.subheader("🗂️ 活跃猎物阵列 (Active Grid)")
    
    if targets:
        # 根据目标数量动态生成列
        grid_cols = st.columns(len(targets))
        for idx, (code, cfg) in enumerate(targets.items()):
            with grid_cols[idx]:
                s_data = state.get(code, {})
                base_price = s_data.get('base_price', 0.0)
                position = s_data.get('position', 0)
                spread_pct = cfg.get('spread_pct', 0)
                
                # 计算上下轨 (理论值)
                buy_line = base_price * (1 - spread_pct) if base_price > 0 else 0
                sell_line = base_price * (1 + spread_pct) if base_price > 0 else 0
                
                # 卡片 UI
                st.info(f"**{cfg['name']}** \n`{code}`")
                st.write(f"📦 **持仓**: `{position}` 股")
                st.write(f"🎯 **中轴价**: `{base_price:.3f}`")
                st.write(f"📉 **下轨(低吸)**: `{buy_line:.3f}`")
                st.write(f"📈 **上轨(高抛)**: `{sell_line:.3f}`")
                st.caption(f"单层额度: {cfg.get('trade_amount', 0)}元 | 间距: {spread_pct*100:.2f}%")
    else:
        st.info("猎犬尚未生成今日目标 YAML。")

    st.markdown("---")

    # --- 模块 C: 孤儿仓位警告板 (明日 09:25 将被强平) ---
    if orphans:
        st.error("🚨 发现历史遗留孤儿仓位！这些标的已跌出今日雷达榜，将在下次织布机启动时触发【市价强平】！")
        orphan_data = []
        for code in orphans:
            orphan_data.append({
                "代码": code,
                "被套持仓": state[code].get('position', 0),
                "最后记忆中轴": state[code].get('base_price', 0)
            })
        st.dataframe(pd.DataFrame(orphan_data), use_container_width=True)

    # --- 模块 D: 原始数据透视表 (List 模式) ---
    with st.expander("👁️ 透视底层数据源 (JSON & YAML Raw Data)"):
        rc1, rc2 = st.columns(2)
        with rc1:
            st.markdown("**grid_targets.yaml (雷达指令)**")
            st.json(targets)
        with rc2:
            st.markdown("**grid_state.json (机器记忆)**")
            st.json(state)

elif app_mode == "🔫 20CM 狙击雷达 (Sniper)":
    st.title("🔫 游资狙击雷达 (Sniper V5.1)")
    st.markdown("监控每日 16:30 扫描出的 **创业板 20CM** 高换手极端赔率标的，并按资金参与度排序。")
    
    sniper_file = ".state/sniper_targets.json"
    if os.path.exists(sniper_file):
        with open(sniper_file, 'r', encoding='utf-8') as f:
            sniper_data = json.load(f)
            
        if sniper_data:
            # 提取排序第一的标的作为“龙一推荐”
            top_target = sniper_data[0]
            
            st.success("🎯 资金流向识别完成。今日首推标的 (龙一)：")
            
            # --- 龙一专属高亮展示区 ---
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("👑 龙头标的", f"{top_target['name']} ({top_target['code'][:6]})")
            col2.metric("收盘价 / 涨幅", f"{top_target['close']} 元", f"{top_target['pct_chg']}%")
            col3.metric("主力成交额", top_target['amount_str'])
            # 如果收盘价远高于 MA5，说明是加速阶段
            ma5_gap = round((top_target['close'] - top_target['ma5']) / top_target['ma5'] * 100, 2)
            col4.metric("5日线 (MA5)", f"{top_target['ma5']} 元", f"偏离 +{ma5_gap}%")

            st.markdown("---")
            st.subheader("📋 备选猎物阵列 (按资金热度降序，列表可滚动)")
            
            # --- 构造优雅的数据表格 ---
            df_sniper = pd.DataFrame(sniper_data)
            # 丢弃不需要显示的后台排序用 raw 数据
            display_df = df_sniper[['code', 'name', 'close', 'pct_chg', 'amount_str', 'ma5', 'ma10']].copy()
            # 重命名列名以供人类阅读
            display_df.columns = ['代码', '名称', '收盘价', '涨幅(%)', '成交额', 'MA5', 'MA10']
            
            # height=300 强制锁定表格高度，内部出现滚动条，绝不撑破屏幕
            st.dataframe(
    display_df, 
    height=450, # 稍微拉高表格，减少局促感
    use_container_width=True, 
    hide_index=True # 杀手锏：隐藏无意义的 0,1,2,3 索引，瞬间清爽
)
            
        else:
            st.info("🧊 今日市场未产生符合【创业板 + 涨幅>15% + 成交>3亿】的标的。")
    else:
        st.warning("⚠️ 尚未生成今日狙击名单，等待 16:30 引擎执行。")
elif app_mode == "📊 万物关联性矩阵 - 待开发":
    st.title("📊 万物关联性矩阵")
    st.info("架构师提示：詹姆斯·西蒙斯探索区。未来将在这里绘制『硬盘内存价格 vs 美光科技股价』的皮尔逊相关系数热力图。")