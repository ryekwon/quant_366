"""
log_refinery.py — AutoPilot 日志精炼器
=========================================
将指定交易日的全部原始 log 物理提炼为单一 Markdown 文件，
只保留对大模型分析有价值的信号行，去除 99% 的轮询噪音。

输出目录：tools/log_refined/
输出文件：refined_YYYYMMDD_HHMMSS.md（带时间戳，防覆盖）

用法：
    python tools/log_refinery.py                      # 当日
    python tools/log_refinery.py --date 2026-04-01    # 指定日期
    python tools/log_refinery.py --log-dir /path/logs # 指定目录
"""

import os
import re
import sys
import argparse
from datetime import datetime, date
from pathlib import Path

# ═══════════════════════════════════════════════════════════════
# 1. 过滤规则
# ═══════════════════════════════════════════════════════════════

# ── 噪音黑名单（优先，命中即丢弃）─────────────────────────────
# 包括所有高频无价值的轮询状态行
_SKIP_RE = re.compile(
    r'盘口保护窗口'
    r'|Tick\s*无效'
    r'|失明跳过'
    r'|冷却期至'
    r'|base_price=0'
    r'|账本未初始化'
    r'|已有在途委托，跳过'
    r'|距离下轨'
    r'|物理焊死'                  # 尾盘熔断重复描述
    r'|\[价差锁\]'                # 价差锁（无论通过或拦截）都是每轮打印的巡逻日志，不是事件
    r'|\[清道夫监控\]'            # 每轮都打的巡逻
    r'|行情扩订'
    r'|Tick 订阅'
    r'|XtQuant'                   # SDK 内部噪音
    r'|connecting\.\.\.'
    r'|MCP.*ping'
    r'|heartbeat.*MCP'
    r'|⏳.*QMT.*连接未就绪'
    r'|未到开盘'
    r'|📡 \[行情'
    r'|🔍 \[清道夫'
    r'|✅ \[清道夫\] 老标的已空仓'  # 无仓不是事件
    r'|\[Executioner\].*trigger.*False'
    r'|今日非周五'
    r'|跳过已禁用'
    r'|账本一致性校验通过'         # 正常通过无需关注
    r'|等待 t1_master 首次运行'
    r'|\[DRY-RUN\]'
    r'|🔒 \[价差锁\]'             # 和上面价差锁合并兜底
    r'|尾盘熔断'                  # T0特有，下面用限频处理
)

# ── 必须保留的关键事件（命中即保留）────────────────────────────
_KEEP_RE = re.compile(
    # 交易行为
    r'🔴 \[买入\]'
    r'|🟢 \[卖出\]'
    r'|✅ \[T1成交'               # T1 Fill 回调确认
    r'|买单已提交'                # T0 buy submitted
    r'|\[高抛\]'                  # T0 sell
    r'|高抛 \|'
    r'|📝 \[报单\]'               # 报单日志
    r'|Sniper.*拔枪'
    r'|Sniper.*成交'
    r'|Sniper.*被拒'
    r'|\[涨停熔断\]'              # Sniper 涨停拦截

    # 风控 / 熔断
    r'|☠️'                        # Executioner 强平
    r'|🛑 \[HardCap\]'
    r'|🗑️'                        # Phase-out 清仓
    r'|Phase-out'
    r'|收盘弹层清仓'
    r'|防绞杀熔断'
    r'|瀑布熔断'
    r'|灾难止损'
    r'|绝对止盈'
    r'|深渊阻断'

    # 系统生命周期
    r'|系统就绪'
    r'|QMT.*连接成功'
    r'|🔔.*收盘'                  # 收盘退出
    r'|🏁'                        # 脚本结束
    r'|💓 \[Heartbeat\]'          # 定时摘要（买卖总计数）
    r'|收盘.*正常退出'
    r'|手动中断'
    r'|15:00.*收盘'

    # 对账 / 核算
    r'|账本与实盘'
    r'|幽灵.*孤儿'
    r'|对账报告'
    r'|偏差.*清仓'
    r'|🎉 账本与实盘完全一致'

    # 异常与报错
    r'|🔥'
    r'|❌'
    r'|🚨'
    r'|Exception'
    r'|Traceback'
    r'|Error:'
    r'|崩溃'
    r'|下单失败'
    r'|连接失败'
    r'|拒单'
    r'|超过涨跌停'
    r'|seq=-1'

    # Sniper / FatFish 摘要
    r'|精筛.*命中'
    r'|精筛.*全军'
    r'|今日空仓'
    r'|今日休战'
    r'|指令为空'
    r'|已导出候选'
    r'|死亡归因'
    r'|⏸️'

    # 启动类（仅保留明确的启动成功行，不保留每一行 🚀）
    r'|T1_Grid.*已启动'
    r'|T0.*引擎.*启动'
    r'|AutoPilot 启动'
    r'|Sniper.*已启动'
    r'|✅ \[系统就绪\]'
)

# 小文件阈值：行数不超过此值时，只去噪不精筛（全保留）
_SMALL_FILE_THRESHOLD = 1000

# 尾盘熔断限频：同一分钟只保留一行（T0 的重灾区）
_FUSE_THROTTLE_MINUTES = 10  # 每 N 分钟最多一行

# ═══════════════════════════════════════════════════════════════
# 2. 策略名映射
# ═══════════════════════════════════════════════════════════════

def _strategy_label(filename: str) -> str:
    stem = filename.replace('.log', '')
    stem = re.sub(r'^\d{8}_\d{6}_', '', stem)
    stem = re.sub(r'^\d{8}_', '', stem)
    mapping = {
        't0_multigrid_executor':          'T0 多网格执行器',
        't0_master':                      'T0 精选大脑',
        't1_grid_executor':               'T1 网格执行器',
        't1_grid_executor_restart1':      'T1 网格执行器 (重启#1)',
        't1_grid_executor_restart2':      'T1 网格执行器 (重启#2)',
        't1_master_blocking':             'T1 主脑（盘前核算）',
        'sniper_entry_executor':          'Sniper 买入执行器',
        'sniper_exit_guard':              'Sniper 止损卫兵',
        'fat_fish_master':                '胖鱼波段大脑',
        'fat_fish_executor':              '胖鱼火炮',
        'fat_fish_guard':                 '胖鱼止损卫兵',
        'autopilot_master':               'AutoPilot 主调度',
        'intraday_reconcile_blocking':    '盘中持仓对账',
        'daily_trade_settlement_blocking':'每日成交清算',
        'smoke_test_blocking':            '系统冒烟测试',
        'start_miniQMT_blocking':         'miniQMT 启动器',
        'qmt_daily_sync_blocking':        'QMT 日线数据同步',
        'qmt_1m_downloader_blocking':     'QMT 1分钟数据下载',
        'knowledge_manager_blocking':     '知识库/演进日志同步',
        'mcp_server':                     'MCP 服务器',
    }
    return mapping.get(stem, stem.replace('_', ' ').title())


# ═══════════════════════════════════════════════════════════════
# 3. 处理单个文件
# ═══════════════════════════════════════════════════════════════

def _process_file(filepath: str) -> tuple[list[str], int, int]:
    """
    读取单个 log 文件，返回 (精炼行列表, 原始行数, 原始字节数)。
    """
    raw_bytes = os.path.getsize(filepath)
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as fh:
            raw_lines = fh.readlines()
    except Exception as e:
        return [f'⚠️ 无法读取文件: {e}'], 0, raw_bytes

    total_lines = len(raw_lines)
    small_file  = (total_lines <= _SMALL_FILE_THRESHOLD)
    refined: list[str] = []

    # 尾盘熔断限频状态（key = 分钟前两位 HH:MM[:0] → 每N分钟一次）
    fuse_seen_at: str = ''

    for line in raw_lines:
        line = line.rstrip()
        if not line or line.startswith('==='):
            continue

        # ── 步骤1：噪音过滤（无论大小文件都执行）──────────────
        if _SKIP_RE.search(line):
            continue

        # ── 尾盘熔断限频（每10分钟保留第一行）────────────────
        if '尾盘熔断' in line:
            # 取时间前4位 HH:M（精确到10分钟）
            chunk = line[:4]
            if chunk and chunk != fuse_seen_at:
                fuse_seen_at = chunk
                refined.append(line)
            continue

        # ── 步骤2：大文件精筛（小文件保留全部非噪音行）────────
        if not small_file and not _KEEP_RE.search(line):
            continue

        refined.append(line)

    return refined, total_lines, raw_bytes


# ═══════════════════════════════════════════════════════════════
# 4. 主函数
# ═══════════════════════════════════════════════════════════════

# 策略优先级（决定章节顺序）
_PRIORITY = {
    't0_multigrid': 0, 't0_master': 1,
    't1_grid': 2, 't1_master': 3,
    'sniper_entry': 4, 'sniper_exit': 5,
    'fat_fish_master': 6, 'fat_fish_executor': 7, 'fat_fish_guard': 8,
    'autopilot': 9, 'intraday_reconcile': 10, 'smoke_test': 11,
    'start_mini': 12, 'qmt': 13, 'knowledge': 20, 'mcp': 21,
}

def _sort_key(fname: str) -> tuple:
    for prefix, pri in _PRIORITY.items():
        if prefix in fname:
            return (pri, fname)
    return (99, fname)


def refine_logs(log_dir: str, target_date: str, out_dir: str) -> str:
    date_tag = target_date.replace('-', '')
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(out_dir, f'refined_{date_tag}_{ts}.md')
    os.makedirs(out_dir, exist_ok=True)

    all_files = [
        f for f in os.listdir(log_dir)
        if date_tag in f and f.endswith('.log')
    ]
    all_files.sort(key=_sort_key)

    if not all_files:
        print(f'❌ 在 {log_dir} 中未找到日期 {target_date} 的 .log 文件。')
        return ''

    total_raw_mb    = 0.0
    total_raw_lines = 0
    total_kept      = 0
    sections: list[str] = []

    print(f'🧹 开始精炼 {target_date} 的日志，共 {len(all_files)} 个文件...')

    for filename in all_files:
        filepath = os.path.join(log_dir, filename)
        label    = _strategy_label(filename)

        refined, raw_lines, raw_bytes = _process_file(filepath)

        total_raw_mb    += raw_bytes / 1048576
        total_raw_lines += raw_lines
        total_kept      += len(refined)

        raw_size_str = (
            f'{raw_bytes/1048576:.1f} MB' if raw_bytes > 1048576
            else f'{raw_bytes/1024:.1f} KB'
        )
        ratio_str = f'{len(refined)}/{raw_lines} 行保留' if raw_lines else '空文件'

        header = (
            f'\n\n---\n\n'
            f'## 📋 {label}\n'
            f'> `{filename}` | {raw_size_str} | {ratio_str}\n\n'
        )

        if refined:
            body = '\n'.join(refined)
        else:
            body = '（无关键事件，全部为轮询噪音或空文件）'

        sections.append(header + '```\n' + body + '\n```')

        kept_count = len(refined)
        flag = '🔴' if kept_count > 500 else ('🟡' if kept_count > 50 else '🟢')
        print(f'  {flag} {filename:<58} {raw_size_str:>8} → {kept_count:>5} 行')

    # ── 组装 Markdown ────────────────────────────────────────────
    compression = total_raw_lines / max(total_kept, 1)
    meta = (
        f'# 🔬 AutoPilot 日志精炼报告 | {target_date}\n\n'
        f'| 项目 | 数值 |\n'
        f'|------|------|\n'
        f'| 生成时间 | {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} |\n'
        f'| 日志日期 | {target_date} |\n'
        f'| 文件数量 | {len(all_files)} 个 |\n'
        f'| 原始总大小 | {total_raw_mb:.1f} MB（{total_raw_lines:,} 行）|\n'
        f'| 精炼保留 | {total_kept:,} 行（信号密度 {total_kept/max(total_raw_lines,1)*100:.1f}%）|\n'
        f'| 压缩比 | {compression:.0f}:1 |\n\n'
        f'> **阅读说明**：每个 `##` 章节对应一个策略进程。'
        f'已过滤：盘口保护/Tick无效/防火墙跳过/价差锁巡逻等高频轮询噪音。'
        f'保留：**交易信号、风控触发、Fill回调、系统事件、报错**。\n'
    )

    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write(meta + ''.join(sections))

    out_kb = os.path.getsize(out_path) / 1024
    print(f'\n✅ 精炼完毕！')
    print(f'   原始：{total_raw_mb:.1f} MB ({total_raw_lines:,} 行)')
    print(f'   精炼：{total_kept:,} 行 → {out_kb:.1f} KB')
    print(f'   压缩比：{compression:.0f}:1')
    print(f'   📄 输出：{out_path}')
    return out_path


# ═══════════════════════════════════════════════════════════════
# 5. 入口
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='AutoPilot 日志精炼器')
    parser.add_argument(
        '--date', '-d',
        default=date.today().strftime('%Y-%m-%d'),
        help='目标日期 YYYY-MM-DD（默认：今日）'
    )
    parser.add_argument(
        '--log-dir', '-l',
        default=r'Z:\QuantpC_Workspace\Quant_Pilot\logs',
        help='日志目录路径'
    )
    parser.add_argument(
        '--out-dir', '-o',
        default=r'Z:\QuantpC_Workspace\Quant_Pilot\tools\log_refined',
        help='输出目录（自动创建）'
    )
    args = parser.parse_args()

    result = refine_logs(
        log_dir     = args.log_dir,
        target_date = args.date,
        out_dir     = args.out_dir,
    )
    sys.exit(0 if result else 1)