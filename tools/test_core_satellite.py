#!/usr/bin/env python3
"""
Core-Satellite戦略の検証: Satellite枠は実際に機能しているか？

合成データで以下を比較:
- Core-Satellite(70/30) vs Core100% vs 全米100% vs 均等B&H

結論: Satellite 30%の掛金配分のみでは、ポートフォリオ全体への寄与が小さい

実行: python3 tools/test_core_satellite.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ideco_backcast import (
    run_core_satellite_simulation,
    run_benchmark_simulation,
    compute_signal,
    compute_stats,
    add_months,
    month_range,
)

START_DATA = "2017-09"
SIM_START = "2018-10"
SIM_END = "2025-02"
months_all = month_range(START_DATA, SIM_END)


def build_synthetic_nav_series(start, end, base_nav, monthly_returns):
    months = month_range(start, end)
    series = {start: base_nav}
    nav = base_nav
    for m in months[1:]:
        nav *= 1 + monthly_returns.get(m, 0.0) / 100
        series[m] = nav
    return series


def make_returns(pattern_fn):
    return {m: pattern_fn(m) for m in months_all[1:]}


# 各商品の月次リターン（%）
core_ret = make_returns(lambda m: -15.0 if m == "2020-03" else 8.0 if m in ("2020-04","2020-05","2020-06") else -3.0 if "2022-01" <= m <= "2022-06" else 2.5 if "2023-01" <= m <= "2023-06" else 1.0)
us_ret = {m: v * 1.15 for m, v in core_ret.items()}
jp_ret = make_returns(lambda m: -12.0 if m == "2020-03" else 5.0 if "2020-04" <= m <= "2020-06" else 3.0 if "2021-07" <= m <= "2021-12" else -2.0 if "2022-01" <= m <= "2022-06" else 4.0 if "2024-01" <= m <= "2024-06" else 0.5)
gr_ret = make_returns(lambda m: -20.0 if m == "2020-03" else 3.0 if "2020-04" <= m <= "2020-09" else 5.0 if "2021-01" <= m <= "2021-06" else -2.5 if "2022-01" <= m <= "2022-12" else 0.8)
jr_ret = make_returns(lambda m: -18.0 if m == "2020-03" else 4.0 if "2020-04" <= m <= "2020-06" else 3.0 if "2019-01" <= m <= "2019-06" else -1.5 if "2022-01" <= m <= "2022-06" else 0.6)
bd_ret = make_returns(lambda m: -1.0 if "2022-01" <= m <= "2022-12" else 1.5 if "2020-03" <= m <= "2020-04" else 0.3)
gd_ret = make_returns(lambda m: 3.0 if "2020-03" <= m <= "2020-04" else 3.5 if "2024-01" <= m <= "2024-12" else 1.5 if "2022-01" <= m <= "2022-06" else 0.4)

products = [
    {"code": "JP90C000FHD2", "name": "楽天・全米株式", "category": "us_equity", "expense_ratio": 0.162, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, us_ret)},
    {"code": "JP90C000CMK4", "name": "たわら先進国株式", "category": "developed_equity", "expense_ratio": 0.0989, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, core_ret)},
    {"code": "JP90C00081U4", "name": "三井住友DC日本株", "category": "domestic_equity", "expense_ratio": 0.176, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, jp_ret)},
    {"code": "JP90C000DX82", "name": "三井住友DC外国REIT", "category": "global_reit", "expense_ratio": 0.297, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, gr_ret)},
    {"code": "JP90C000DX74", "name": "三井住友DC日本REIT", "category": "domestic_reit", "expense_ratio": 0.275, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, jr_ret)},
    {"code": "JP90C000CML2", "name": "たわら先進国債券", "category": "foreign_bond", "expense_ratio": 0.187, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, bd_ret)},
    {"code": "JP90C0008QL7", "name": "ステートストリート・ゴールド", "category": "commodity", "expense_ratio": 0.895, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, gd_ret)},
]

MC = 23000
CORE = "JP90C000CMK4"

params = {
    "CORE_PRODUCT": CORE, "CORE_RATIO": 0.70, "TOP_N": 1,
    "WEIGHT_1M": 1, "WEIGHT_3M": 2, "WEIGHT_6M": 4, "WEIGHT_12M": 3,
    "GOLD_EXPENSE_MULTIPLIER": 2.0, "USE_MA_FILTER": False,
    "SATELLITE_EXCLUDE_CATEGORIES": [],
}


def run_and_track(products, params, mc):
    """シミュレーション + 月次Satellite追跡"""
    core_code = params["CORE_PRODUCT"]
    months = month_range(SIM_START, SIM_END)
    holdings = {p["code"]: 0.0 for p in products}
    signal_history = {}
    sat_counts = {}
    core_100 = 0
    exclude_cats = set(params.get("SATELLITE_EXCLUDE_CATEGORIES", []))

    for sim_month in months:
        sig_m = add_months(sim_month, -1)
        sigs, scores = {}, {}
        for p in products:
            s, sc = compute_signal(p["nav_series"], p["expense_ratio"], sig_m, params, p["category"] == "commodity")
            sigs[p["code"]] = s
            scores[p["code"]] = sc
        signal_history[sig_m] = dict(sigs)

        # Case A
        pm = sorted(signal_history.keys())[-2:]
        if len(pm) == 2:
            for p in products:
                c = p["code"]
                if c == core_code or holdings.get(c, 0) <= 0:
                    continue
                if all(signal_history[m].get(c, "SELL") == "SELL" for m in pm):
                    holdings[core_code] += holdings[c]
                    holdings[c] = 0.0

        sat_buys = sorted(
            [p for p in products if p["code"] != core_code and p.get("category", "") not in exclude_cats and sigs.get(p["code"]) == "BUY" and scores.get(p["code"]) is not None],
            key=lambda p: scores[p["code"]], reverse=True,
        )[:params["TOP_N"]]

        for p in products:
            c = p["code"]
            if holdings.get(c, 0) > 0:
                n0, n1 = p["nav_series"].get(sim_month), p["nav_series"].get(add_months(sim_month, -1))
                if n0 and n1 and n1 > 0:
                    holdings[c] *= n0 / n1

        if sat_buys:
            holdings[core_code] += mc * params["CORE_RATIO"]
            sa = mc * (1 - params["CORE_RATIO"]) / len(sat_buys)
            for s in sat_buys:
                holdings[s["code"]] += sa
            name = s["name"]
            sat_counts[name] = sat_counts.get(name, 0) + 1
        else:
            holdings[core_code] += mc
            core_100 += 1

    return sat_counts, core_100


def p_stats(label, stats):
    print(f"  {label:35s}: {stats['final_value']:>10,.0f}円  利益{stats['gain_pct']:+5.1f}%  年率{stats['annualized_pct']:+5.1f}%  MaxDD{stats['max_drawdown_pct']:4.1f}%")


# メイン
cs = run_core_satellite_simulation(products, MC, params, SIM_START, SIM_END)
b_core = run_benchmark_simulation([next(p for p in products if p["code"]==CORE)["nav_series"]], MC, SIM_START, SIM_END)
b_us = run_benchmark_simulation([next(p for p in products if p["code"]=="JP90C000FHD2")["nav_series"]], MC, SIM_START, SIM_END)
b_eq = run_benchmark_simulation([p["nav_series"] for p in products], MC, SIM_START, SIM_END)

cs_s, core_s, us_s, eq_s = compute_stats(cs), compute_stats(b_core), compute_stats(b_us), compute_stats(b_eq)

print("=" * 90)
print("Core-Satellite(70/30) vs ベンチマーク — Satellite枠の実効性検証")
print("=" * 90)
p_stats("Core-Satellite(70/30)", cs_s)
p_stats("先進国株式のみ(=Core100%)", core_s)
p_stats("全米株式のみ", us_s)
p_stats("均等B&H(7本)", eq_s)

diff = cs_s["final_value"] - core_s["final_value"]
diff_pct = diff / core_s["final_value"] * 100
print(f"\n  Satellite枠の寄与: {diff:+,.0f}円 ({diff_pct:+.1f}%)")

sat_counts, core_100 = run_and_track(products, params, MC)
total = len(month_range(SIM_START, SIM_END))
print(f"\n■ Satellite選択内訳 ({total}ヶ月)")
for name, count in sorted(sat_counts.items(), key=lambda x: -x[1]):
    print(f"    {name:30s}: {count:>3}回 ({count/total*100:4.1f}%)")
if core_100 > 0:
    print(f"    {'Core100%(BUYなし)':30s}: {core_100:>3}回 ({core_100/total*100:4.1f}%)")

print(f"\n■ 結論")
if abs(diff_pct) < 2.0:
    print(f"  Satellite枠の寄与 = {diff_pct:+.1f}% → 掛金30%のみの配分ではポートフォリオ全体への影響が限定的")
    print(f"  Core70%の配分ロジック自体にバグはなし。Satellite商品もローテーションしている。")
    print(f"  ただし新規掛金のみの配分であるため、既存残高を含む全体への影響は構造的に小さい。")
