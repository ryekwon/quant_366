# -*- coding: utf-8 -*-
"""
auto_history_downloader.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
历史成交数据下载 & 清洗工具
  - 从 QMT query_data(data_type='deal') 拉取指定日期范围的历史成交记录
  - 合并分笔成交（VWAP 重算成交价）
  - 物理打上手续费标签
  - 落盘到 deal_reports/ 目录

所有路径/账号全部从 .env 读取，零硬链接。

用法：
  python auto_history_downloader.py                     # 默认下载当月
  python auto_history_downloader.py --start 20260101 --end 20260331
  python auto_history_downloader.py --start 20260301 --end 20260331 --out my_trades.csv
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

# ── 读取 .env（与本脚本同目录）────────────────────────────────
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

QMT_PATH   = os.getenv('QMT_PATH')
ACCOUNT_ID = os.getenv('ACCOUNT_ID')
STATE_DIR  = os.getenv('STATE_DIR',
             os.path.join(os.path.dirname(os.path.abspath(__file__)), '.state'))

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR  = os.getenv('DEAL_REPORT_DIR',
              os.path.join(_SCRIPT_DIR, 'data', 'deal_reports'))


# ══════════════════════════════════════════════════════════════
# 核心下载器
# ══════════════════════════════════════════════════════════════

class HistoryTradeDownloader(XtQuantTraderCallback):
    """
    QMT 历史成交数据下载 + 清洗引擎。

    物理算费规则（内置）：
      ETF (15x / 51x / 52x / 58x) → 万0.5，最低 0.5 元
      个股                  → 万0.5，最低 5.0 元
    """

    # 沪市 ETF 代码前缀：15x(深市) / 51x / 52x / 58x(沪市)
    # ⚠️ 52x (如 520500/520510/520780) 是沪市宽基/行业 ETF，切勿漏掉
    ETF_PREFIXES = ('15', '51', '52', '58')

    def __init__(self):
        if not QMT_PATH:
            raise EnvironmentError("❌ .env 中未找到 QMT_PATH，请检查配置。")
        if not ACCOUNT_ID:
            raise EnvironmentError("❌ .env 中未找到 ACCOUNT_ID，请检查配置。")

        self.acc = StockAccount(ACCOUNT_ID)

        print(f"🔌 正在连接 QMT 交易网关...")
        print(f"   QMT_PATH   = {QMT_PATH}")
        print(f"   ACCOUNT_ID = {ACCOUNT_ID}")

        session_id = int(time.time())
        self.xt_trader = XtQuantTrader(QMT_PATH, session_id)
        self.xt_trader.register_callback(self)
        self.xt_trader.start()
        time.sleep(1)  # 规范：start() 后至少 1s 再 connect

        connect_result = self.xt_trader.connect()
        if connect_result == 0:
            print("✅ QMT 交易网关连接成功")
        else:
            raise ConnectionError(f"❌ QMT 连接失败，错误码: {connect_result}")

        sub_result = self.xt_trader.subscribe(self.acc)
        if sub_result != 0:
            raise ConnectionError(f"❌ 账户订阅失败，错误码: {sub_result}")
        print(f"✅ 账户 {ACCOUNT_ID} 订阅完成\n")

    # ── 物理算费引擎 ──────────────────────────────────────────
    def _calc_commission(self, code: str, amount: float) -> float:
        """ETF 万0.5低消0.5；个股万0.5低消5.0"""
        raw = float(amount) * 0.00005
        if str(code).startswith(self.ETF_PREFIXES):
            return max(0.5, raw)
        return max(5.0, raw)

    # ── 主流程 ────────────────────────────────────────────────
    def download_and_clean(
        self,
        start_date: str,
        end_date: str,
        output_path: str | None = None,
    ) -> pd.DataFrame | None:
        """
        下载 [start_date, end_date] 的历史成交，清洗后落盘 CSV。

        实现原理：
          query_stock_trades / query_stock_orders 均只返回当日数据。
          query_data(acc, tmp_path, 'deal', start, end) 是唯一支持
          跨日历史范围的接口，内部写 CSV -> 读 DataFrame -> 删 tmp -> 返回。
        """
        # query_data 需要一个临时写入路径
        tmp_path = os.path.join(STATE_DIR, '__deal_export_tmp.csv')

        # 日期转 epoch 整数秒（query_data 的 startTime/endTime 使用 epoch 格式）
        try:
            ts_start = int(datetime.datetime.strptime(start_date, '%Y%m%d').timestamp())
            ts_end   = int((datetime.datetime.strptime(end_date, '%Y%m%d')
                           + datetime.timedelta(days=1, seconds=-1)).timestamp())
        except ValueError as e:
            print(f"❌ 日期格式错误（需 YYYYMMDD）: {e}")
            return None

        print(f"📡 调用 query_data 拉取历史成交 [{start_date} ~ {end_date}]...")
        print(f"   epoch 范围: {ts_start} ~ {ts_end}")
        df_raw = None
        try:
            df_raw = self.xt_trader.query_data(
                self.acc,
                tmp_path,
                'deal',
                ts_start,    # epoch 整数秒
                ts_end,
                {},
            )
        except Exception as e:
            print(f"❌ query_data 调用失败: {e}")
            return None
        finally:
            # 防御性清理临时文件（query_data 内部已尝试删除）
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        # query_data 出错时返回含 'error' 的 dict
        if isinstance(df_raw, dict):
            print(f"❌ 查询失败，服务器返回: {df_raw}")
            return None

        if df_raw is None or (hasattr(df_raw, 'empty') and df_raw.empty):
            print(f"⚠️  [{start_date}~{end_date}] 无成交记录（券商可能限制历史深度）。")
            return None

        print(f"📥 获取到 {len(df_raw)} 行原始数据")
        print(f"   列名: {list(df_raw.columns)}")

        # ── 列名映射（QMT 原生列 → 中文输出列）─────────────────
        col_map = {
            'traded_time'   : '成交时间',
            'stock_code'    : '证券代码',
            'order_type'    : '买卖标记',
            'traded_price'  : '成交价格',
            'traded_volume' : '成交数量',
            'traded_amount' : '成交金额',
            'order_sysid'   : '合同编号',
            'strategy_name' : '策略名称',
            'order_remark'  : '投资备注',
        }
        df_raw = df_raw.rename(
            columns={k: v for k, v in col_map.items() if k in df_raw.columns}
        )

        # 买卖标记文字化
        if '买卖标记' in df_raw.columns:
            df_raw['买卖标记'] = df_raw['买卖标记'].apply(
                lambda x: '买入' if int(x) == 23 else ('卖出' if int(x) == 24 else str(x))
            )

        # 必要列检查
        for col in ['成交数量', '成交金额', '合同编号']:
            if col not in df_raw.columns:
                print(f"⚠️  缺少必要列 [{col}]，原始列名: {list(df_raw.columns)}")
                print("   已返回原始 DataFrame 供调试，请对照列名更新 col_map。")
                return df_raw

        # Step 1：合并分笔（同一合同编号 -> 一条记录）
        print("🔄 合并分笔成交...")
        agg_funcs = {
            k: ('max' if k == '成交时间'
                else 'sum' if k in ('成交数量', '成交金额')
                else 'first')
            for k in df_raw.columns if k != '合同编号'
        }
        df = df_raw.groupby('合同编号', as_index=False).agg(agg_funcs)

        # Step 2：VWAP 重算成交价
        df['成交价格'] = (df['成交金额'] / df['成交数量']).round(4)

        # Step 3：时间格式化（epoch int -> 可读字符串）
        if '成交时间' in df.columns:
            def _fmt(ts):
                try:
                    v = int(ts)
                    if v > 1_000_000_000:  # epoch 秒
                        return datetime.datetime.fromtimestamp(v).strftime('%Y-%m-%d %H:%M:%S')
                    return str(ts)
                except Exception:
                    return str(ts)
            df['成交时间'] = df['成交时间'].apply(_fmt)

        # Step 4：物理算费
        print("💰 物理算费中...")
        df['手续费'] = df.apply(
            lambda r: self._calc_commission(r['证券代码'], r['成交金额']), axis=1
        )

        # Step 5：排序落盘
        df.sort_values('成交时间', inplace=True, ignore_index=True)

        if output_path is None:
            os.makedirs(REPORT_DIR, exist_ok=True)
            fname = f"deals_{start_date}_{end_date}.csv"
            output_path = os.path.join(REPORT_DIR, fname)

        # 如果文件被占用，自动加时间戳后缀避免 PermissionError
        def _safe_write(path: str, df: pd.DataFrame) -> str:
            try:
                df.to_csv(path, index=False, encoding='utf-8-sig')
                return path
            except PermissionError:
                ts = datetime.datetime.now().strftime('%H%M%S')
                alt = path.replace('.csv', f'_{ts}.csv')
                df.to_csv(alt, index=False, encoding='utf-8-sig')
                print(f"⚠️  原路径被占用，已写至: {alt}")
                return alt

        final_path = _safe_write(output_path, df)

        print(f"\n✅ 清洗完毕！共 {len(df)} 条合并订单（原始 {len(df_raw)} 笔分笔）")
        print(f"📊 报告已输出至: {final_path}")
        return df

    def close(self):
        """安全关闭 QMT 连接"""
        try:
            self.xt_trader.stop()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# CLI 入口
# ══════════════════════════════════════════════════════════════

def _default_start() -> str:
    return datetime.date.today().strftime('%Y%m') + '01'

def _default_end() -> str:
    return datetime.date.today().strftime('%Y%m%d')


def main():
    parser = argparse.ArgumentParser(
        description='QMT 历史成交下载 & 清洗工具（零硬链接，路径全部走 .env）'
    )
    parser.add_argument('--start', default=_default_start(),
                        help=f'开始日期 YYYYMMDD（默认当月1号）')
    parser.add_argument('--end',   default=_default_end(),
                        help=f'结束日期 YYYYMMDD（默认今天）')
    parser.add_argument('--out',   default=None,
                        help='输出 CSV 路径（可选）')
    args = parser.parse_args()

    downloader = HistoryTradeDownloader()
    try:
        downloader.download_and_clean(
            start_date=args.start,
            end_date=args.end,
            output_path=args.out,
        )
    finally:
        downloader.close()


if __name__ == '__main__':
    main()