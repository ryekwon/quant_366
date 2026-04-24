# ==========================================
# tools/migrate_telemetry_csv.py
# 职责: oracle_telemetry_macro.csv 原地迁移
#   1. 补 [名称] 列（通过 xtdata.get_instrument_detail 查询）
#   2. 重新编码为 utf-8-sig（带 BOM，Excel 直接打开不乱码）
# 运行一次即可，幂等（多次运行结果相同）
# ==========================================
import sys
import pandas as pd
from pathlib import Path

# 强制 UTF-8 输出，防止 Windows GBK 终端吞 Emoji
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── 路径 ──────────────────────────────────────────────────────────────────────
_SCRIPT_DIR  = Path(__file__).parent.resolve()
_PROJECT_DIR = _SCRIPT_DIR.parent
_TELEMETRY   = _PROJECT_DIR / ".state" / "oracle_telemetry_macro.csv"
_BACKUP      = _PROJECT_DIR / ".state" / "oracle_telemetry_macro.bak.csv"

# ── 名称查询（直连 xtdata，不依赖 QMT 交易账户）──────────────────────────────
def _get_names(codes: list) -> dict:
    name_map = {}
    try:
        from xtquant import xtdata
        for code in codes:
            try:
                detail = xtdata.get_instrument_detail(code)
                name_map[code] = (detail or {}).get('InstrumentName', '') if isinstance(detail, dict) else ''
            except Exception:
                name_map[code] = ''
    except ImportError:
        print("  ⚠️  xtquant 未安装，名称列将保留为空字符串")
    return name_map


def migrate():
    if not _TELEMETRY.exists():
        print(f"❌ 文件不存在: {_TELEMETRY}")
        sys.exit(1)

    print(f"📂 读取原始文件: {_TELEMETRY}")

    # 尝试多种编码读取（旧文件可能是 utf-8 或 gbk）
    df = None
    for enc in ('utf-8-sig', 'utf-8', 'gbk', 'gb18030'):
        try:
            df = pd.read_csv(_TELEMETRY, encoding=enc, dtype=str)
            print(f"  ✅ 以 [{enc}] 成功读取，共 {len(df)} 行，列: {list(df.columns)}")
            break
        except Exception as e:
            print(f"  ⚠️  [{enc}] 读取失败: {e}")

    if df is None or df.empty:
        print("❌ 无法读取文件，退出")
        sys.exit(1)

    # ── 备份原始文件 ──────────────────────────────────────────────────────────
    import shutil
    shutil.copy2(_TELEMETRY, _BACKUP)
    print(f"  💾 原始文件已备份至: {_BACKUP.name}")

    # ── 标准化列名（兼容旧版无名称列 / 新版已有名称列）──────────────────────
    cols = list(df.columns)

    # 执行器旧版列: Timestamp,Code,P0,Q20,Q50,Q80,Odds_Ratio,Momentum_Pass,Action
    # 守卫旧版列:   Timestamp,Code,P0,Q20,Q50,Q80,Odds_Ratio,Action
    # 目标列:       Timestamp,Code,名称,P0,Q20,Q50,Q80,Odds_Ratio,(Momentum_Pass,)Action

    if '名称' in cols:
        print("  ℹ️  检测到已有 [名称] 列，仅重新编码")
    else:
        print("  🔧 补充 [名称] 列...")
        unique_codes = df['Code'].dropna().unique().tolist()
        print(f"     共 {len(unique_codes)} 只唯一标的，正在查询名称...")
        name_map = _get_names(unique_codes)

        # 插入名称列（Code 之后，P0 之前）
        insert_pos = cols.index('P0') if 'P0' in cols else 2
        df.insert(insert_pos, '名称', df['Code'].map(name_map).fillna(''))
        print(f"  ✅ 名称列已插入到第 {insert_pos+1} 列")

    # ── 以 utf-8-sig 写回（原地替换）────────────────────────────────────────
    df.to_csv(_TELEMETRY, index=False, encoding='utf-8-sig')
    print(f"\n✅ 迁移完成！")
    print(f"   文件: {_TELEMETRY}")
    print(f"   行数: {len(df)}")
    print(f"   列数: {len(df.columns)}  ->  {list(df.columns)}")
    print(f"   编码: utf-8-sig（Excel/WPS 直接打开不乱码）")
    print(f"   备份: {_BACKUP.name}（如有问题可手动还原）")

    # 打印后5行预览
    print(f"\n📊 最后 5 行预览:")
    print(df.tail(5).to_string(index=False))


if __name__ == '__main__':
    migrate()
