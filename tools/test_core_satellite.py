#!/usr/bin/env python3
"""
Core-Satellite戦略のロジック検証スクリプト

合成データでバックキャストを実行し、以下を検証:
1. Core 70% / Satellite 30% の配分が正しく動作するか
2. Satellite商品が固定になっていないか（ローテーションするか）
3. SATELLITE_EXCLUDE_CATEGORIES による改善効果
4. 旧ロジック vs 改善版の比較

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


def build_synthetic_nav_series(
    start: str, end: str, base_nav: float, monthly_returns: dict
) -> dict:
    """合成NAVシリーズを構築。monthly_returns = {YYYY-MM: return_pct}"""
    months = month_range(start, end)
    series = {start: base_nav}
    nav = base_nav
    for i in range(1, len(months)):
        m = months[i]
        ret = monthly_returns.get(m, 0.0) / 100
        nav = nav * (1 + ret)
        series[m] = nav
    return series


START_DATA = "2017-09"
SIM_START = "2018-10"
SIM_END = "2025-02"
months_all = month_range(START_DATA, SIM_END)

# --- 各商品の月次リターン（%）パターン ---
# Core (先進国株式): 安定的に月1%成長
core_returns = {}
for m in months_all[1:]:
    if m == "2020-03":
        core_returns[m] = -15.0
    elif m in ("2020-04", "2020-05", "2020-06"):
        core_returns[m] = 8.0
    elif "2022-01" <= m <= "2022-06":
        core_returns[m] = -3.0
    elif "2023-01" <= m <= "2023-06":
        core_returns[m] = 2.5
    else:
        core_returns[m] = 1.0

# US株式: Coreと相関高い
us_returns = {}
for m in months_all[1:]:
    base = core_returns.get(m, 1.0)
    us_returns[m] = base * 1.15

# 日本株式
jp_returns = {}
for m in months_all[1:]:
    if m == "2020-03":
        jp_returns[m] = -12.0
    elif "2020-04" <= m <= "2020-06":
        jp_returns[m] = 5.0
    elif "2021-07" <= m <= "2021-12":
        jp_returns[m] = 3.0
    elif "2022-01" <= m <= "2022-06":
        jp_returns[m] = -2.0
    elif "2024-01" <= m <= "2024-06":
        jp_returns[m] = 4.0
    else:
        jp_returns[m] = 0.5

# 海外REIT
reit_returns = {}
for m in months_all[1:]:
    if m == "2020-03":
        reit_returns[m] = -20.0
    elif "2020-04" <= m <= "2020-09":
        reit_returns[m] = 3.0
    elif "2021-01" <= m <= "2021-06":
        reit_returns[m] = 5.0
    elif "2022-01" <= m <= "2022-12":
        reit_returns[m] = -2.5
    else:
        reit_returns[m] = 0.8

# 国内REIT
jp_reit_returns = {}
for m in months_all[1:]:
    if m == "2020-03":
        jp_reit_returns[m] = -18.0
    elif "2020-04" <= m <= "2020-06":
        jp_reit_returns[m] = 4.0
    elif "2019-01" <= m <= "2019-06":
        jp_reit_returns[m] = 3.0
    elif "2022-01" <= m <= "2022-06":
        jp_reit_returns[m] = -1.5
    else:
        jp_reit_returns[m] = 0.6

# 先進国債券
bond_returns = {}
for m in months_all[1:]:
    if "2022-01" <= m <= "2022-12":
        bond_returns[m] = -1.0
    elif "2020-03" <= m <= "2020-04":
        bond_returns[m] = 1.5
    else:
        bond_returns[m] = 0.3

# ゴールド
gold_returns = {}
for m in months_all[1:]:
    if "2020-03" <= m <= "2020-04":
        gold_returns[m] = 3.0
    elif "2024-01" <= m <= "2024-12":
        gold_returns[m] = 3.5
    elif "2022-01" <= m <= "2022-06":
        gold_returns[m] = 1.5
    else:
        gold_returns[m] = 0.4


def build_products():
    return [
        {"code": "JP90C000FHD2", "name": "楽天・全米株式", "category": "us_equity",
         "expense_ratio": 0.162, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, us_returns)},
        {"code": "JP90C000CMK4", "name": "たわら先進国株式", "category": "developed_equity",
         "expense_ratio": 0.0989, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, core_returns)},
        {"code": "JP90C00081U4", "name": "三井住友DC日本株", "category": "domestic_equity",
         "expense_ratio": 0.176, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, jp_returns)},
        {"code": "JP90C000DX82", "name": "三井住友DC外国REIT", "category": "global_reit",
         "expense_ratio": 0.297, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, reit_returns)},
        {"code": "JP90C000DX74", "name": "三井住友DC日本REIT", "category": "domestic_reit",
         "expense_ratio": 0.275, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, jp_reit_returns)},
        {"code": "JP90C000CML2", "name": "たわら先進国債券", "category": "foreign_bond",
         "expense_ratio": 0.187, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, bond_returns)},
        {"code": "JP90C0008QL7", "name": "ステートストリート・ゴールド", "category": "commodity",
         "expense_ratio": 0.895, "nav_series": build_synthetic_nav_series(START_DATA, SIM_END, 10000, gold_returns)},
    ]


def run_simulation_with_stats(products, params, label, monthly_contribution=23000):
    """シミュレーション実行し、統計と月次詳細を返す"""
    core_code = params.get("CORE_PRODUCT", "JP90C000CMK4")
    exclude_cats = set(params.get("SATELLITE_EXCLUDE_CATEGORIES", []))

    months = month_range(SIM_START, SIM_END)
    holdings = {p["code"]: 0.0 for p in products}
    holdings["GUARANTEE"] = 0.0
    signal_history = {}
    contribution_total = 0.0
    code_to_name = {p["code"]: p["name"] for p in products}

    sat_counts = {}
    core_100 = 0
    monthly_details = []

    for sim_month in months:
        signal_month = add_months(sim_month, -1)
        fund_signals = {}
        fund_scores = {}
        for p in products:
            is_commodity = p.get("category") == "commodity"
            signal, score = compute_signal(
                p["nav_series"], p["expense_ratio"], signal_month, params, is_commodity
            )
            fund_signals[p["code"]] = signal
            fund_scores[p["code"]] = score
        signal_history[signal_month] = dict(fund_signals)

        # Case A
        past_months = sorted(signal_history.keys())[-2:]
        if len(past_months) == 2:
            for p in products:
                code = p["code"]
                if code == core_code:
                    continue
                if holdings.get(code, 0) > 0:
                    past_sigs = [signal_history[m].get(code, "SELL") for m in past_months]
                    if all(s == "SELL" for s in past_sigs):
                        amount = holdings[code]
                        holdings[code] = 0.0
                        holdings[core_code] += amount

        # Satellite BUY
        satellite_buys = sorted(
            [p for p in products
             if p["code"] != core_code
             and p.get("category", "") not in exclude_cats
             and fund_signals.get(p["code"]) == "BUY"
             and fund_scores.get(p["code"]) is not None],
            key=lambda p: fund_scores[p["code"]],
            reverse=True,
        )[:params.get("TOP_N", 1)]

        # リターン適用
        for p in products:
            code = p["code"]
            if holdings.get(code, 0) > 0:
                nav_now = p["nav_series"].get(sim_month)
                nav_prev = p["nav_series"].get(add_months(sim_month, -1))
                if nav_now and nav_prev and nav_prev > 0:
                    holdings[code] *= nav_now / nav_prev

        # 掛金配分
        contribution_total += monthly_contribution
        core_ratio = params.get("CORE_RATIO", 0.70)
        if satellite_buys:
            core_amount = monthly_contribution * core_ratio
            sat_amount = monthly_contribution * (1.0 - core_ratio) / len(satellite_buys)
            holdings[core_code] += core_amount
            for sat in satellite_buys:
                holdings[sat["code"]] += sat_amount
            sat_name = code_to_name.get(satellite_buys[0]["code"], "?")
            sat_counts[sat_name] = sat_counts.get(sat_name, 0) + 1
        else:
            holdings[core_code] += monthly_contribution
            sat_name = "Core100%"
            core_100 += 1

        total_value = sum(holdings.values())
        monthly_details.append({
            "month": sim_month, "satellite": sat_name, "value": total_value,
            "gain": total_value - contribution_total,
        })

    # run_core_satellite_simulation でも比較
    history = run_core_satellite_simulation(products, monthly_contribution, params, SIM_START, SIM_END)
    stats = compute_stats(history)

    return {
        "label": label,
        "stats": stats,
        "sat_counts": sat_counts,
        "core_100": core_100,
        "total_months": len(months),
        "monthly_details": monthly_details,
    }


def main():
    products = build_products()
    monthly_contribution = 23000
    core_code = "JP90C000CMK4"

    # 共通パラメータ
    base_params = {
        "STRATEGY_MODE": "core_satellite",
        "CORE_PRODUCT": core_code,
        "CORE_RATIO": 0.70,
        "TOP_N": 1,
        "WEIGHT_1M": 1,
        "WEIGHT_3M": 2,
        "WEIGHT_6M": 4,
        "WEIGHT_12M": 3,
        "GOLD_EXPENSE_MULTIPLIER": 2.0,
        "USE_MA_FILTER": False,
    }

    # --- パターン1: 旧ロジック（除外なし）---
    params_old = {**base_params}
    result_old = run_simulation_with_stats(products, params_old, "旧(除外なし)")

    # --- パターン2: 改善版（us_equity除外）---
    params_new = {**base_params, "SATELLITE_EXCLUDE_CATEGORIES": ["us_equity"]}
    result_new = run_simulation_with_stats(products, params_new, "改善(US除外)")

    # --- ベンチマーク ---
    core_nav = next(p for p in products if p["code"] == core_code)["nav_series"]
    bench_core_hist = run_benchmark_simulation([core_nav], monthly_contribution, SIM_START, SIM_END)
    bench_core = compute_stats(bench_core_hist)

    us_nav = next(p for p in products if p["code"] == "JP90C000FHD2")["nav_series"]
    bench_us_hist = run_benchmark_simulation([us_nav], monthly_contribution, SIM_START, SIM_END)
    bench_us = compute_stats(bench_us_hist)

    all_navs = [p["nav_series"] for p in products]
    bench_eq_hist = run_benchmark_simulation(all_navs, monthly_contribution, SIM_START, SIM_END)
    bench_eq = compute_stats(bench_eq_hist)

    # === 結果表示 ===
    print("=" * 100)
    print("Core-Satellite戦略 比較レポート（合成データ）")
    print("=" * 100)

    def print_stats(label, stats):
        print(
            f"  {label:35s}: {stats['final_value']:>10,.0f}円 "
            f"(利益 {stats['gain']:+,.0f}円, {stats['gain_pct']:+.1f}%, "
            f"年率 {stats['annualized_pct']:+.1f}%, MaxDD {stats['max_drawdown_pct']:.1f}%)"
        )

    print("\n■ パフォーマンス比較")
    print_stats("旧Core-Satellite(除外なし)", result_old["stats"])
    print_stats("改善Core-Satellite(US株除外)", result_new["stats"])
    print_stats("先進国株式のみ(Core100%)", bench_core)
    print_stats("全米株式のみ", bench_us)
    print_stats("均等B&H(7本)", bench_eq)

    # 差分
    old_vs_core = result_old["stats"]["final_value"] - bench_core["final_value"]
    new_vs_core = result_new["stats"]["final_value"] - bench_core["final_value"]
    new_vs_old = result_new["stats"]["final_value"] - result_old["stats"]["final_value"]
    print(f"\n  旧 vs Core100%: {old_vs_core:+,.0f}円 ({old_vs_core/bench_core['final_value']*100:+.1f}%)")
    print(f"  改善 vs Core100%: {new_vs_core:+,.0f}円 ({new_vs_core/bench_core['final_value']*100:+.1f}%)")
    print(f"  改善 vs 旧: {new_vs_old:+,.0f}円 ({new_vs_old/result_old['stats']['final_value']*100:+.1f}%)")

    # === Satellite選択統計 ===
    for result in [result_old, result_new]:
        print(f"\n■ Satellite選択統計 [{result['label']}]")
        print(f"  総月数: {result['total_months']}  Core100%: {result['core_100']}ヶ月")
        for name, count in sorted(result["sat_counts"].items(), key=lambda x: -x[1]):
            print(f"    {name:30s}: {count:>3}回 ({count/result['total_months']*100:.1f}%)")

    # === 月次Satellite選択の比較 ===
    print(f"\n■ 月次Satellite選択比較（旧 vs 改善）")
    print(f"  {'月':>8} | {'旧Satellite':>25} | {'改善Satellite':>25} | {'変更':>4}")
    print("  " + "-" * 80)
    diff_count = 0
    for old_d, new_d in zip(result_old["monthly_details"], result_new["monthly_details"]):
        changed = "***" if old_d["satellite"] != new_d["satellite"] else ""
        if changed:
            diff_count += 1
        print(f"  {old_d['month']:>8} | {old_d['satellite']:>25} | {new_d['satellite']:>25} | {changed}")

    print(f"\n  Satellite選択が変更された月: {diff_count}/{result_old['total_months']}ヶ月")

    # === 判定 ===
    print("\n" + "=" * 100)
    print("■ 判定")
    print("=" * 100)

    if abs(old_vs_core / bench_core["final_value"] * 100) < 2.0:
        print("  [問題] 旧ロジック: Satellite枠の寄与がほぼゼロ（Core100%と差異<2%）")
    if abs(new_vs_core / bench_core["final_value"] * 100) < 2.0:
        print("  [注意] 改善版: Satellite枠の寄与が依然として小さい")
    elif new_vs_core > 0:
        print(f"  [改善] 改善版はCore100%を上回る（+{new_vs_core/bench_core['final_value']*100:.1f}%）")

    if new_vs_old > 0:
        print(f"  [改善] US株除外でSatellite分散効果が向上（+{new_vs_old:,.0f}円）")
    elif new_vs_old < 0:
        print(f"  [注意] US株除外でリターンが低下（{new_vs_old:+,.0f}円）→ 分散 vs リターンのトレードオフ")


if __name__ == "__main__":
    main()
