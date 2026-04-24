# -*- coding: utf-8 -*-
"""
daily_trade_settlement.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
每日成交结算脚本（在券商结算后执行，建议 17:30+ 由 autopilot_master 调度）

功能：
  - 调用 query_stock_trades(acc) 拉取当日全量成交
  - 合并分笔成交（VWAP 重算成交价）
  - 物理打上手续费标签
  - 按月分文件夹落盘：
      data/deal_reports/202603/deals_20260331.csv
      data/deal_reports/202604/deals_20260401.csv

所有路径/账号全部从 .env 读取，零硬链接。

用法：
  python daily_trade_settlement.py              # 默认今天
  python daily_trade_settlement.py --date 20260331
"""
import os
import sys
import time
import argparse
import datetime
import pandas as pd
from dotenv import load_dotenv
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

# ── 读取 .env ─────────────────────────────────────────────────
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

QMT_PATH   = os.getenv('QMT_PATH')
ACCOUNT_ID = os.getenv('ACCOUNT_ID')

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 根目录：Quant_Pilot/data/deal_reports/
REPORT_ROOT = os.getenv('DEAL_REPORT_DIR',
              os.path.join(_SCRIPT_DIR, 'data', 'deal_reports'))


# ══════════════════════════════════════════════════════════════
# 每日结算器
# ══════════════════════════════════════════════════════════════

class DailySettlement(XtQuantTraderCallback):
    """
    当日成交数据拉取 + 清洗 + 落盘。

    物理算费规则：
      ETF (15x / 51x / 52x / 58x) → 万0.5，最低 0.5 元
      个股                  → 万0.5，最低 5.0 元
    """

    # 沪市 ETF 代码前缀：15x(深市) / 51x / 52x / 58x(沪市)
    # ⚠️ 52x (如 520500/520510/520780) 是沪市宽基/行业 ETF，切勿漏掉
    ETF_PREFIXES = ('15', '51', '52', '58')

    def __init__(self):
        if not QMT_PATH:
            raise EnvironmentError("❌ .env 中未找到 QMT_PATH")
        if not ACCOUNT_ID:
            raise EnvironmentError("❌ .env 中未找到 ACCOUNT_ID")

        self.acc = StockAccount(ACCOUNT_ID)

        print(f"🔌 连接 QMT 交易网关...")
        print(f"   QMT_PATH   = {QMT_PATH}")
        print(f"   ACCOUNT_ID = {ACCOUNT_ID}")

        session_id = int(time.time())
        self.xt_trader = XtQuantTrader(QMT_PATH, session_id)
        self.xt_trader.register_callback(self)
        self.xt_trader.start()
        time.sleep(1)  # 规范：start() 后至少 1s 再 connect

        connect_result = self.xt_trader.connect()
        if connect_result == 0:
            print("✅ QMT 连接成功")
        else:
            raise ConnectionError(f"❌ QMT 连接失败，错误码: {connect_result}")

        sub_result = self.xt_trader.subscribe(self.acc)
        if sub_result != 0:
            raise ConnectionError(f"❌ 账户订阅失败，错误码: {sub_result}")
        print(f"✅ 账户 {ACCOUNT_ID} 订阅完成\n")

    # ── 物理算费 ──────────────────────────────────────────────
    def _calc_commission(self, code: str, amount: float) -> float:
        raw = float(amount) * 0.00005
        if str(code).startswith(self.ETF_PREFIXES):
            return max(0.5, raw)
        return max(5.0, raw)

    # ── 输出路径：按月分文件夹 ────────────────────────────────
    @staticmethod
    def _build_output_path(trade_date: str) -> str:
        """
        trade_date: YYYYMMDD
        返回: .../deal_reports/YYYYMM/deals_YYYYMMDD.csv
        """
        month_folder = trade_date[:6]           # e.g. '202603'
        dir_path = os.path.join(REPORT_ROOT, month_folder)
        os.makedirs(dir_path, exist_ok=True)
        return os.path.join(dir_path, f"deals_{trade_date}.csv")

    # ── 主流程 ────────────────────────────────────────────────
    def run(self, trade_date: str | None = None) -> pd.DataFrame | None:
        """
        拉取 trade_date 当日成交，清洗后落盘。

        Args:
            trade_date: YYYYMMDD，None 表示今天
        Returns:
            清洗后的 DataFrame，失败返回 None
        """
        if trade_date is None:
            trade_date = datetime.date.today().strftime('%Y%m%d')

        print(f"📅 结算日期: {trade_date}")
        print(f"📡 拉取当日成交（query_stock_trades）...")

        trades = self.xt_trader.query_stock_trades(self.acc) or []

        if not trades:
            print("⚠️  当日无成交记录（可能非交易日，或结算前调用）。")
            return None

        print(f"📥 获取到 {len(trades)} 笔原始分笔成交，进入清洗流水线...")

        # Step 1：对象解包 → 字典列表
        rows = []
        for t in trades:
            direction = '买入' if t.order_type == 23 else \
                        '卖出' if t.order_type == 24 else str(t.order_type)
            rows.append({
                '成交时间': t.traded_time,        # epoch int，后面格式化
                '证券代码': t.stock_code,
                '买卖标记': direction,
                '成交价格': t.traded_price,
                '成交数量': t.traded_volume,
                '成交金额': t.traded_amount,
                '合同编号': t.order_sysid,        # 核心合并主键（分笔同编号）
                '策略名称': getattr(t, 'strategy_name', ''),
                '投资备注': getattr(t, 'order_remark', ''),
            })

        df_raw = pd.DataFrame(rows)

        # Step 2：合并分笔（同一合同编号 → 一条记录）
        print("🔄 合并分笔成交...")
        agg_funcs = {
            '成交时间': 'max',
            '证券代码': 'first',
            '买卖标记': 'first',
            '成交数量': 'sum',
            '成交金额': 'sum',
            '策略名称': 'first',
            '投资备注': 'first',
        }
        df = df_raw.groupby('合同编号', as_index=False).agg(agg_funcs)

        # Step 3：VWAP 重算成交价
        df['成交价格'] = (df['成交金额'] / df['成交数量']).round(4)

        # Step 4：时间格式化（epoch int → 可读字符串）
        def _fmt_time(ts):
            try:
                v = int(ts)
                if v > 1_000_000_000:
                    return datetime.datetime.fromtimestamp(v).strftime('%Y-%m-%d %H:%M:%S')
                return str(ts)
            except Exception:
                return str(ts)
        df['成交时间'] = df['成交时间'].apply(_fmt_time)

        # Step 5：物理算费
        print("💰 物理算费中...")
        df['手续费'] = df.apply(
            lambda r: self._calc_commission(r['证券代码'], r['成交金额']), axis=1
        )

        # Step 6：排序
        df.sort_values('成交时间', inplace=True, ignore_index=True)

        # 调整列顺序
        cols = ['成交时间', '证券代码', '买卖标记', '成交价格',
                '成交数量', '成交金额', '手续费', '合同编号',
                '策略名称', '投资备注']
        df = df[[c for c in cols if c in df.columns]]

        # Step 7：落盘（按月分文件夹）
        output_path = self._build_output_path(trade_date)

        # 文件被占用时自动加时间戳后缀
        try:
            df.to_csv(output_path, index=False, encoding='utf-8-sig')
        except PermissionError:
            ts_suffix = datetime.datetime.now().strftime('%H%M%S')
            output_path = output_path.replace('.csv', f'_{ts_suffix}.csv')
            df.to_csv(output_path, index=False, encoding='utf-8-sig')
            print(f"⚠️  原路径被占用，已写至: {output_path}")

        print(f"\n✅ 结算完毕！共 {len(df)} 条合并订单（原始 {len(df_raw)} 笔分笔）")
        print(f"📊 落盘路径: {output_path}")
        return df

    def close(self):
        try:
            self.xt_trader.stop()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# CLI 入口
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='每日成交结算（券商结算后调用，建议 17:30+）'
    )
    parser.add_argument(
        '--date', default=None,
        help='结算日期 YYYYMMDD（默认今天）'
    )
    args = parser.parse_args()

    settler = DailySettlement()
    try:
        settler.run(trade_date=args.date)
    finally:
        settler.close()


if __name__ == '__main__':
    main()