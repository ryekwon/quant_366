# -*- coding: utf-8 -*-
"""
tools/etf_name_fetcher.py
职责：扫描两个标的池（T0 目标 + ETF 宇宙），查询 ETF 全称，
      并按 A 股监管规则推断每只 ETF 的交易制度（T+0 / T+1）。

A 股 ETF 交易制度说明（xtdata 不暴露该字段，用规则推断）：
  ✅ T+0（当日买当日可卖）:
      - 跨境/QDII ETF:  513xxx, 520xxx,
                        159xxx(海外指数如恒生/纳指/中概), 159506/159131...
      - 黄金 ETF:       518880
      - 债券 ETF:       511xxx (货币/国债/信用债)
      - 部分特殊债基:    159xxx 前缀但跟踪债券
  ❌ T+1（当日买次日才能卖）:
      - A 股宽基 ETF:   510xxx, 512xxx, 588xxx (科创板)
      - A 股行业/主题:  515xxx, 516xxx 等
      - 大多数 159xxx A 股主题 ETF
"""
import os
import sys
import csv
import json
import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_DIR          = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YAML_PATH     = os.path.join(_DIR, ".state", "fixed_t0_target.yaml")
UNIVERSE_JSON = os.path.join(_DIR, ".state", "oracle_v2_universe.json")
OUT_CSV       = os.path.join(_DIR, ".state", "etf_names.csv")

# ── T+0 判定规则集（A 股监管规则，xtdata 无此字段只能规则推断）────────────────
# T+0 QDII/跨境 ETF 的 SH 常见前缀
_T0_SH_PREFIXES = ("513", "520", "518")   # 跨境+QDII+黄金
# T+0 债券/货币 ETF 的 SH 常见前缀
_T0_BOND_SH_PREFIXES = ("511",)
# T+1 A 股股票型 ETF 的 SH 常见前缀
_T1_SH_PREFIXES = ("510", "512", "515", "516", "517", "588")

# SZ 159xxx：部分 T+0（跨境/债券），部分 T+1（A 股主题）
# 用已知 T+0 的 SZ 代码白名单来覆盖，其余 159 归 T+1 保守处理
_T0_SZ_WHITELIST = {
    # 港股通/跨境 ETF（SZ 159xxx 中已知 T+0 的）
    "159501", "159502", "159503", "159504", "159505", "159506",
    "159507", "159508", "159509", "159510", "159511", "159512",
    "159515", "159519",   # 港股国企ETF
    "159520", "159521", "159522", "159523", "159524", "159525",
    "159526", "159527", "159528", "159529", "159530",
    "159615",   # 恒生生物科技QDII
    "159131",   # 港股通信息技术ETF华宝（QDII）
    "159570",   # 港股通创新药ETF汇添富（跨境）
    "159712",   # 港股通50ETF国泰（跨境）
    "159740",   # 恒生科技ETF大成（跨境）
    "159792",   # 港股通互联网ETF富国（跨境）
    # 纳指/标普/中概 QDII（SZ）
    "159632", "159659", "159660", "159661", "159662", "159663",
    # 债券类 SZ（T+0）
    "159972", "159200", "159700", "159600", "159816",
}

def classify_t0_t1(code: str) -> str:
    """
    根据 A 股监管规则推断 ETF 的交易制度。
    返回：'T+0' / 'T+1' / '未知'
    """
    num, exch = code.split(".") if "." in code else (code, "")
    prefix3 = num[:3]
    prefix4 = num[:4]

    if exch == "SH":
        if prefix3 in _T0_SH_PREFIXES:
            return "T+0 ✅"
        if prefix3 in _T0_BOND_SH_PREFIXES:
            return "T+0 ✅"   # 债券/货币ETF
        if prefix3 in _T1_SH_PREFIXES:
            return "T+1 ❌"
        return "未知"

    if exch == "SZ":
        if num in _T0_SZ_WHITELIST:
            return "T+0 ✅"
        if prefix3 == "159":
            return "T+1 ❌"   # 保守处理：未认定的 SZ 159 归 T+1
        return "未知"

    return "未知"


# ── 读取两个标的池 ────────────────────────────────────────────────────────────
def _load_yaml_codes(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        # 策略 A: 标准 YAML
        try:
            data = yaml.safe_load(content)
            raw = None
            if isinstance(data, dict) and "targets" in data:
                raw = data["targets"]
            elif isinstance(data, list):
                raw = data
            if raw:
                codes = [str(i).split(",")[0].strip() for i in raw if "." in str(i)]
                if codes:
                    return codes
        except Exception:
            pass
        # 策略 B: 逐行 CSV
        codes = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            code = line.split(",")[0].strip()
            if code and "." in code:
                codes.append(code)
        return codes
    except Exception:
        return []


def _load_universe_codes(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [item["code"] for item in data.get("universe", []) if item.get("code")]
    except Exception:
        return []


# ── 主逻辑 ───────────────────────────────────────────────────────────────────
def fetch_etf_names():
    from xtquant import xtdata

    yaml_codes     = _load_yaml_codes(YAML_PATH)
    universe_codes = _load_universe_codes(UNIVERSE_JSON)

    # 合并，记录来源标签
    pool = {}   # code -> set of sources
    for c in yaml_codes:
        pool.setdefault(c, set()).add("T0池")
    for c in universe_codes:
        pool.setdefault(c, set()).add("ETF宇宙")

    all_codes = sorted(pool.keys())
    print(f"\n📋 标的池总览：T0池={len(yaml_codes)} | ETF宇宙={len(universe_codes)} | 合并去重={len(all_codes)}\n")

    header = f"   {'代码':<14}  {'交易制度':<10}  {'来源':<10}  {'ETF 全称'}"
    sep    = f"   {'─'*14}  {'─'*10}  {'─'*10}  {'─'*35}"
    print(header)
    print(sep)

    results = []
    t0_count = 0
    t1_count = 0

    for code in all_codes:
        regime = classify_t0_t1(code)
        sources = "/".join(sorted(pool[code]))

        try:
            detail = xtdata.get_instrument_detail(code) or {}
            name = detail.get("InstrumentName", "") or "—"
        except Exception as e:
            name = f"查询异常: {e}"

        print(f"   {code:<14}  {regime:<10}  {sources:<10}  {name}")
        results.append({"code": code, "regime": regime.split()[0], "source": sources, "name": name})

        if "T+0" in regime:
            t0_count += 1
        elif "T+1" in regime:
            t1_count += 1

    print(sep)
    print(f"\n📊 汇总：T+0={t0_count} | T+1={t1_count} | 未知={len(all_codes)-t0_count-t1_count}")

    # ── 写 CSV ──────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["code", "regime", "source", "name"])
        w.writeheader()
        w.writerows(results)

    print(f"✅ 结果已写入: {OUT_CSV}\n")


if __name__ == "__main__":
    fetch_etf_names()
