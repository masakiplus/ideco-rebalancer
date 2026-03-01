#!/usr/bin/env python3
"""
iDeCo バックキャストシミュレーション

旧モメンタム戦略 vs 新Core-Satellite戦略 vs ベンチマーク比較
期間: 2018-10 〜 2025-02（77ヶ月）

実行方法:
  .venv/bin/python3 tools/ideco_backcast.py [--use-cached] [--end YYYY-MM]

オプション:
  --use-cached   data/backcast_cache.json を使用（スクレイピングなし・高速）
  --end YYYY-MM  シミュレーション終了月（デフォルト: 2025-02）
"""

import argparse
import json
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from ideco_scraper import RakutenIdecoScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = BASE_DIR / "config" / "ideco_products.json"
CACHE_PATH = BASE_DIR / "data" / "backcast_cache.json"
OUTPUT_DIR = BASE_DIR / "output"

SIM_START = "2018-10"
SIM_END = "2025-02"

# 旧モメンタム戦略パラメータ（変更前）
OLD_STRATEGY_PARAMS = {
    "WEIGHT_1M": 1,
    "WEIGHT_3M": 2,
    "WEIGHT_6M": 3,
    "WEIGHT_12M": 4,
    "TOP_N": 3,
    "GOLD_EXPENSE_MULTIPLIER": 1.0,
    "USE_MA_FILTER": True,
}


# ============================================================
# 日付ユーティリティ
# ============================================================


def add_months(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m += n
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def month_range(start: str, end: str) -> list:
    result = []
    current = start
    while current <= end:
        result.append(current)
        current = add_months(current, 1)
    return result


# ============================================================
# NAVシリーズ構築
# ============================================================


def build_nav_series(product_data: dict) -> dict:
    """月次リターン履歴と現在NAVから月末NAVシリーズを構築"""
    current_nav = product_data.get("nav")
    monthly_returns = product_data.get("monthly_returns", [])

    if not monthly_returns or current_nav is None:
        return {}

    series = {}
    nav = current_nav
    latest_ym = monthly_returns[0]["year_month"]
    series[latest_ym] = nav

    for entry in monthly_returns:
        ret = entry["return_pct"] / 100
        if ret == -1.0:
            break
        prev_nav = nav / (1 + ret)
        prev_ym = add_months(entry["year_month"], -1)
        series[prev_ym] = prev_nav
        nav = prev_nav

    return series


# ============================================================
# シグナル計算（configパラメータ対応）
# ============================================================


def compute_signal(
    nav_series: dict,
    expense_ratio: float,
    signal_month: str,
    params: dict,
    is_commodity: bool = False,
) -> tuple:
    """
    params に基づくシグナルとスコアを計算。

    params keys:
      WEIGHT_1M, WEIGHT_3M, WEIGHT_6M, WEIGHT_12M
      GOLD_EXPENSE_MULTIPLIER (commodity のみ適用)
      USE_MA_FILTER (True=MA3>MA6条件あり)

    Returns:
        ("BUY"|"SELL", score_or_None)
    """
    w1 = params.get("WEIGHT_1M", 1)
    w3 = params.get("WEIGHT_3M", 2)
    w6 = params.get("WEIGHT_6M", 4)
    w12 = params.get("WEIGHT_12M", 3)
    total_w = w1 + w3 + w6 + w12
    use_ma_filter = params.get("USE_MA_FILTER", False)
    gold_mult = params.get("GOLD_EXPENSE_MULTIPLIER", 1.0) if is_commodity else 1.0

    nav_0 = nav_series.get(signal_month)
    nav_1 = nav_series.get(add_months(signal_month, -1))
    nav_3 = nav_series.get(add_months(signal_month, -3))
    nav_6 = nav_series.get(add_months(signal_month, -6))
    nav_12 = nav_series.get(add_months(signal_month, -12))

    if any(v is None for v in [nav_0, nav_1, nav_3, nav_6, nav_12]):
        return "SELL", None

    r1 = (nav_0 / nav_1 - 1) * 100
    r3 = (nav_0 / nav_3 - 1) * 100
    r6 = (nav_0 / nav_6 - 1) * 100
    r12 = (nav_0 / nav_12 - 1) * 100

    effective_expense = expense_ratio * gold_mult
    score = (r1 * w1 + r3 * w3 + r6 * w6 + r12 * w12) / total_w - effective_expense

    if score <= 0:
        return "SELL", score

    if r12 <= 0:
        return "SELL", score

    if use_ma_filter:
        navs = [nav_series.get(add_months(signal_month, -k)) for k in range(6)]
        if all(n is not None for n in navs):
            ma3 = sum(navs[:3]) / 3
            ma6 = sum(navs) / 6
            if ma3 <= ma6:
                return "SELL", score

    return "BUY", score


# ============================================================
# モメンタム戦略シミュレーション
# ============================================================


def run_strategy_simulation(
    products: list,
    monthly_contribution: int,
    params: dict,
    start: str,
    end: str,
) -> list:
    """
    モメンタム戦略シミュレーション（Case A スイッチングあり）。

    - TOP_N BUY候補に均等配分
    - Case A: 2ヶ月連続SELL かつ残高あり → BUY上位へ全額スイッチ
    """
    top_n = params.get("TOP_N", 3)
    months = month_range(start, end)
    holdings = {p["code"]: 0.0 for p in products}
    holdings["GUARANTEE"] = 0.0

    signal_history = {}
    history = []
    contribution_total = 0.0

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

        # Case A: 2ヶ月連続SELL の保有商品をスイッチ
        past_months = sorted(signal_history.keys())[-2:]
        if len(past_months) == 2:
            buy_top_codes = [
                p["code"]
                for p in sorted(
                    [q for q in products if fund_signals.get(q["code"]) == "BUY"],
                    key=lambda q: fund_scores.get(q["code"]) or -999,
                    reverse=True,
                )[:top_n]
            ]
            for p in products:
                code = p["code"]
                if holdings.get(code, 0) > 0:
                    past_sigs = [signal_history[m].get(code, "SELL") for m in past_months]
                    if all(s == "SELL" for s in past_sigs):
                        amount = holdings[code]
                        holdings[code] = 0.0
                        if buy_top_codes:
                            per_fund = amount / len(buy_top_codes)
                            for bc in buy_top_codes:
                                holdings[bc] = holdings.get(bc, 0) + per_fund
                        else:
                            holdings["GUARANTEE"] += amount

        # BUY候補選定
        buy_candidates = sorted(
            [
                p
                for p in products
                if fund_signals.get(p["code"]) == "BUY"
                and fund_scores.get(p["code"]) is not None
            ],
            key=lambda p: fund_scores[p["code"]],
            reverse=True,
        )[:top_n]

        # 今月のリターンを適用
        for p in products:
            code = p["code"]
            if holdings.get(code, 0) > 0:
                nav_now = p["nav_series"].get(sim_month)
                nav_prev = p["nav_series"].get(add_months(sim_month, -1))
                if nav_now and nav_prev and nav_prev > 0:
                    holdings[code] *= nav_now / nav_prev

        # 掛金追加
        contribution_total += monthly_contribution
        if buy_candidates:
            per_fund = monthly_contribution / len(buy_candidates)
            for cand in buy_candidates:
                holdings[cand["code"]] = holdings.get(cand["code"], 0) + per_fund
        else:
            holdings["GUARANTEE"] += monthly_contribution

        total_value = sum(holdings.values())
        history.append(
            {
                "month": sim_month,
                "value": total_value,
                "contribution_total": contribution_total,
                "gain": total_value - contribution_total,
                "buy_codes": [c["code"] for c in buy_candidates],
            }
        )

    return history


# ============================================================
# Core-Satellite戦略シミュレーション
# ============================================================


def run_core_satellite_simulation(
    products: list,
    monthly_contribution: int,
    params: dict,
    start: str,
    end: str,
    core_abs_momentum: bool = False,
) -> list:
    """
    Core-Satellite戦略シミュレーション。

    core_abs_momentum=False（デフォルト）:
      Core商品は常に CORE_RATIO × 掛金を受け取る（BUY/SELL 不問）

    core_abs_momentum=True:
      Core が SELL（12M<0 等）のとき → Core 枠を GUARANTEE へ退避
      Case A: Core が2ヶ月連続SELL かつ残高あり → GUARANTEE へスイッチ

    共通:
      Satellite: 非Core BUY TOP_N が残り (1-CORE_RATIO) を均等分割
      Case A: 非Core保有が2ヶ月連続SELL → Core（または GUARANTEE）へスイッチ
    """
    core_code = params.get("CORE_PRODUCT", "JP90C000CMK4")
    core_ratio = params.get("CORE_RATIO", 0.70)
    top_n = params.get("TOP_N", 1)

    months = month_range(start, end)
    holdings = {p["code"]: 0.0 for p in products}
    holdings["GUARANTEE"] = 0.0

    signal_history = {}
    history = []
    contribution_total = 0.0

    for sim_month in months:
        signal_month = add_months(sim_month, -1)

        # 全商品のシグナル計算
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

        core_is_buy = fund_signals.get(core_code) == "BUY"

        # Case A: 2ヶ月連続SELL の保有をスイッチ
        past_months = sorted(signal_history.keys())[-2:]
        if len(past_months) == 2:
            for p in products:
                code = p["code"]
                if holdings.get(code, 0) <= 0:
                    continue
                past_sigs = [signal_history[m].get(code, "SELL") for m in past_months]
                if not all(s == "SELL" for s in past_sigs):
                    continue

                # CS固定モード: Core は常時保持（Case A スキップ）
                if code == core_code and not core_abs_momentum:
                    continue

                amount = holdings[code]
                holdings[code] = 0.0

                if code == core_code and core_abs_momentum:
                    # Core が SELL → GUARANTEE へ
                    holdings["GUARANTEE"] += amount
                elif code != core_code:
                    # 非Core → Core（Core BUY時）or GUARANTEE（Core SELL時）
                    if core_abs_momentum and not core_is_buy:
                        holdings["GUARANTEE"] += amount
                    else:
                        holdings[core_code] = holdings.get(core_code, 0) + amount

        # Satellite BUY候補選定（Core除く）
        satellite_buys = sorted(
            [
                p
                for p in products
                if p["code"] != core_code
                and fund_signals.get(p["code"]) == "BUY"
                and fund_scores.get(p["code"]) is not None
            ],
            key=lambda p: fund_scores[p["code"]],
            reverse=True,
        )[:top_n]

        # 今月のリターンを適用
        for p in products:
            code = p["code"]
            if holdings.get(code, 0) > 0:
                nav_now = p["nav_series"].get(sim_month)
                nav_prev = p["nav_series"].get(add_months(sim_month, -1))
                if nav_now and nav_prev and nav_prev > 0:
                    holdings[code] *= nav_now / nav_prev

        # 掛金配分
        contribution_total += monthly_contribution
        use_core = not core_abs_momentum or core_is_buy

        if use_core:
            core_dest = core_code
        else:
            core_dest = "GUARANTEE"  # Core SELL → 元本保証へ

        if satellite_buys:
            core_amount = monthly_contribution * core_ratio
            sat_amount = monthly_contribution * (1.0 - core_ratio) / len(satellite_buys)
            holdings[core_dest] = holdings.get(core_dest, 0) + core_amount
            for sat in satellite_buys:
                holdings[sat["code"]] = holdings.get(sat["code"], 0) + sat_amount
        else:
            holdings[core_dest] = holdings.get(core_dest, 0) + monthly_contribution

        total_value = sum(holdings.values())
        sat_codes = [s["code"] for s in satellite_buys]
        history.append(
            {
                "month": sim_month,
                "value": total_value,
                "contribution_total": contribution_total,
                "gain": total_value - contribution_total,
                "buy_codes": ([core_code] if use_core else ["GUARANTEE"]) + sat_codes,
                "core_escaped": not use_core,
            }
        )

    return history


# ============================================================
# ベンチマーク（均等B&H）
# ============================================================


def run_benchmark_simulation(
    nav_series_list: list,
    monthly_contribution: int,
    start: str,
    end: str,
) -> list:
    months = month_range(start, end)
    n = len(nav_series_list)
    if n == 0:
        return []

    holdings = [0.0] * n
    contribution_total = 0.0
    history = []

    for sim_month in months:
        for i, nav_series in enumerate(nav_series_list):
            if holdings[i] > 0:
                nav_now = nav_series.get(sim_month)
                nav_prev = nav_series.get(add_months(sim_month, -1))
                if nav_now and nav_prev and nav_prev > 0:
                    holdings[i] *= nav_now / nav_prev

        contribution_total += monthly_contribution
        per_fund = monthly_contribution / n
        for i in range(n):
            holdings[i] += per_fund

        total_value = sum(holdings)
        history.append(
            {
                "month": sim_month,
                "value": total_value,
                "contribution_total": contribution_total,
                "gain": total_value - contribution_total,
            }
        )

    return history


# ============================================================
# 統計
# ============================================================


def compute_stats(history: list) -> dict:
    if not history:
        return {}

    final = history[-1]
    months = len(history)
    years = months / 12
    final_value = final["value"]
    contribution_total = final["contribution_total"]
    gain = final["gain"]
    gain_pct = gain / contribution_total * 100 if contribution_total > 0 else 0

    peak_gain = float("-inf")
    max_dd = 0.0
    for record in history:
        g = record["gain"]
        if g > peak_gain:
            peak_gain = g
        dd = peak_gain - g
        if peak_gain > 0 and dd > 0:
            dd_pct = dd / (peak_gain + record["contribution_total"]) * 100
            if dd_pct > max_dd:
                max_dd = dd_pct

    annualized = (
        (final_value / contribution_total) ** (1 / years) - 1
        if contribution_total > 0 and years > 0
        else 0
    )

    return {
        "final_value": final_value,
        "contribution_total": contribution_total,
        "gain": gain,
        "gain_pct": gain_pct,
        "annualized_pct": annualized * 100,
        "max_drawdown_pct": max_dd,
        "months": months,
        "years": years,
    }


def year_end_values(history: list) -> dict:
    result = {}
    for record in history:
        m = record["month"]
        if m.endswith("-12") or record is history[-1]:
            year = m[:4]
            result[year] = record["value"]
    return result


# ============================================================
# レポート生成
# ============================================================


def generate_report(
    old_strategy: list,
    cs_fixed: list,
    cs_dynamic: list,
    bench_equal: list,
    bench_dev: list,
    bench_us: list,
    products: list,
    start: str,
    end: str,
    output_path: Path,
    core_comparison: dict = None,
) -> None:
    old_stats = compute_stats(old_strategy)
    csf_stats = compute_stats(cs_fixed)
    csd_stats = compute_stats(cs_dynamic)
    eq_stats = compute_stats(bench_equal)
    dev_stats = compute_stats(bench_dev)
    us_stats = compute_stats(bench_us)

    code_to_name = {p["code"]: p["name"] for p in products}
    months = old_stats.get("months", 0)

    lines = [
        "# iDeCo バックキャストレポート",
        "",
        f"シミュレーション期間: {start} 〜 {end}（{months}ヶ月）",
        f"月次拠出額: 23,000円  |  総拠出額: {old_stats.get('contribution_total', 0):,.0f}円",
        f"生成日: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "---",
        "",
        "## 総合比較",
        "",
        "| 戦略 | 最終資産額 | 運用益 | 運用益率 | 年率換算 | 最大DD |",
        "|---|---:|---:|---:|---:|---:|",
    ]

    def row(label, stats):
        return (
            f"| {label} | {stats['final_value']:,.0f}円 | "
            f"{stats['gain']:+,.0f}円 | "
            f"{stats['gain_pct']:+.1f}% | "
            f"{stats['annualized_pct']:+.1f}%/年 | "
            f"{stats['max_drawdown_pct']:.1f}% |"
        )

    lines += [
        row("旧モメンタム（weights 1/2/3/4・MA3>MA6・TOP3・Gold×1）", old_stats),
        row("Core-Satellite 固定（Core常時70%）", csf_stats),
        row("**Core-Satellite 可変（Core SELL時→元本保証）**", csd_stats),
        row("均等B&H（7本）", eq_stats),
        row("先進国株式のみ", dev_stats),
        row("全米株式のみ", us_stats),
        "",
        "> 年率換算: (最終資産額 ÷ 総拠出額)^(12÷月数) − 1  ※ IRR ではなく簡易換算",
        "> 最大DD: 利益額ピーク比の最大下落率",
        "",
        "---",
        "",
        "## 年次パフォーマンス（年末資産額）",
        "",
        "| 年 | 旧モメンタム | CS固定 | **CS可変** | 均等B&H | 先進国株 | 全米株式 | 拠出累計 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    old_ye = year_end_values(old_strategy)
    csf_ye = year_end_values(cs_fixed)
    csd_ye = year_end_values(cs_dynamic)
    eq_ye = year_end_values(bench_equal)
    dev_ye = year_end_values(bench_dev)
    us_ye = year_end_values(bench_us)

    all_years = sorted(set(list(old_ye.keys()) + list(csf_ye.keys())))
    contrib_by_year = {r["month"][:4]: r["contribution_total"] for r in old_strategy}

    for year in all_years:
        contrib = contrib_by_year.get(year, 0)
        lines.append(
            f"| {year} | {old_ye.get(year, 0):,.0f} | {csf_ye.get(year, 0):,.0f} | "
            f"{csd_ye.get(year, 0):,.0f} | {eq_ye.get(year, 0):,.0f} | "
            f"{dev_ye.get(year, 0):,.0f} | {us_ye.get(year, 0):,.0f} | {contrib:,.0f} |"
        )

    lines += [
        "",
        "---",
        "",
        "## 月次詳細（Core-Satellite 可変）",
        "",
        "| 月 | 資産額 | 利益額 | Core状態 | Satellite |",
        "|---|---:|---:|---|---|",
    ]

    core_code = "JP90C000CMK4"
    for record in cs_dynamic:
        codes = record.get("buy_codes", [])
        escaped = record.get("core_escaped", False)
        core_label = "退避→元本保証" if escaped else code_to_name.get(core_code, core_code)[:16]
        sat_codes = [c for c in codes if c not in (core_code, "GUARANTEE")]
        sat_names = " / ".join(code_to_name.get(c, c)[:14] for c in sat_codes)
        if not sat_names:
            sat_names = "なし"
        lines.append(
            f"| {record['month']} | {record['value']:,.0f} | "
            f"{record['gain']:+,.0f} | {core_label} | {sat_names} |"
        )

    lines += [
        "",
        "---",
        "",
        "## 戦略パラメータ比較",
        "",
        "| パラメータ | 旧モメンタム | CS固定 | CS可変 |",
        "|---|---|---|---|",
        "| スコア重み (1M/3M/6M/12M) | 1/2/3/4 | 1/2/4/3 | 1/2/4/3 |",
        "| MA3>MA6フィルター | あり | なし | なし |",
        "| TOP_N | 3 | 1 | 1 |",
        "| Goldペナルティ | ×1.0 | ×2.0 | ×2.0 |",
        "| Core配分 | なし | 常時70% | BUY時70%・SELL時→元本保証 |",
        "| Core SELL時の既存残高 | - | Core維持 | GUARANTEE へスイッチ |",
        "",
        "---",
        "",
        "## 対象商品",
        "",
    ]
    for p in products:
        lines.append(f"- {p['name']}（{p['code']}、信託報酬 {p['expense_ratio']:.3f}%）")

    lines.append("")

    # --- Core商品比較 ---
    if core_comparison:
        lines += ["", "---", "", "## Core商品比較", ""]

        fp = core_comparison["full_period"]
        lines += [
            f"### フル期間 Core比較（{fp['start']} 〜 {fp['end']}）",
            "",
            "| Core商品 | 最終資産額 | 運用益率 | 年率 | MaxDD |",
            "|---|---:|---:|---:|---:|",
        ]
        for item in fp["results"]:
            s = item["stats"]
            if s:
                lines.append(
                    f"| {item['label']} | {s['final_value']:,.0f}円 | "
                    f"{s['gain_pct']:+.1f}% | {s['annualized_pct']:+.1f}%/年 | {s['max_drawdown_pct']:.1f}% |"
                )
        lines += ["", "> Satelliteの選定ロジックは同一。Core商品のみ変更した比較。", ""]

        rp = core_comparison["recent_period"]
        n = rp.get("n_months", "?")
        lines += [
            f"### 直近期間 Core比較（{rp['start']} 〜 {rp['end']}、{n}ヶ月）",
            "",
            "| Core商品 | 最終資産額 | 運用益率 | 年率 | MaxDD |",
            "|---|---:|---:|---:|---:|",
        ]
        for item in rp["results"]:
            s = item["stats"]
            if s:
                lines.append(
                    f"| {item['label']} | {s['final_value']:,.0f}円 | "
                    f"{s['gain_pct']:+.1f}% | {s['annualized_pct']:+.1f}%/年 | {s['max_drawdown_pct']:.1f}% |"
                )
        lines += [
            "",
            f"> 楽天プラスS&P500・オールカントリーは2023-11以降のデータ（{rp['start']}〜比較）。",
            "",
        ]

    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"レポート生成完了: {output_path}")


# ============================================================
# データ取得
# ============================================================


def fetch_or_load(config: dict, use_cached: bool) -> list:
    if use_cached and CACHE_PATH.exists():
        logger.info(f"キャッシュ読み込み: {CACHE_PATH}")
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    codes = [p["code"] for p in config["products"] if not p.get("capital_guarantee")]
    logger.info(f"スクレイピング開始: {len(codes)} 商品")
    scraper = RakutenIdecoScraper()
    results = scraper.fetch_all(codes)

    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"キャッシュ保存: {CACHE_PATH}")
    return results


# ============================================================
# メイン
# ============================================================


def main():
    parser = argparse.ArgumentParser(description="iDeCo バックキャストシミュレーション")
    parser.add_argument("--use-cached", action="store_true", help="キャッシュデータを使用")
    parser.add_argument(
        "--end", default=SIM_END, help=f"終了月 YYYY-MM（デフォルト: {SIM_END}）"
    )
    args = parser.parse_args()
    sim_end = args.end

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    monthly_contribution = config["parameters"]["MONTHLY_CONTRIBUTION"]
    new_params = {
        **config["parameters"],
        "USE_MA_FILTER": False,  # MA3>MA6条件は廃止
    }
    config_by_code = {p["code"]: p for p in config["products"]}

    raw_data = fetch_or_load(config, args.use_cached)
    if not raw_data:
        logger.error("データ取得失敗")
        sys.exit(1)

    logger.info(f"取得商品数: {len(raw_data)}")

    products = []
    for d in raw_data:
        code = d["code"]
        cfg = config_by_code.get(code, {})
        nav_series = build_nav_series(d)
        products.append(
            {
                "code": code,
                "name": d["name"],
                "category": cfg.get("category", ""),
                "expense_ratio": cfg.get("expense_ratio", d.get("expense_ratio", 0)),
                "nav_series": nav_series,
            }
        )
        logger.info(
            f"  {d['name'][:30]}: NAVシリーズ {len(nav_series)}ヶ月 "
            f"({min(nav_series, default='?')} 〜 {max(nav_series, default='?')})"
        )

    nav_by_code = {p["code"]: p["nav_series"] for p in products}
    developed_code = "JP90C000CMK4"
    us_code = "JP90C000FHD2"

    logger.info(f"\n旧モメンタム戦略: {SIM_START} 〜 {sim_end}")
    old_history = run_strategy_simulation(
        products=products,
        monthly_contribution=monthly_contribution,
        params=OLD_STRATEGY_PARAMS,
        start=SIM_START,
        end=sim_end,
    )

    logger.info(f"Core-Satellite 固定（Core常時70%）: {SIM_START} 〜 {sim_end}")
    cs_fixed = run_core_satellite_simulation(
        products=products,
        monthly_contribution=monthly_contribution,
        params=new_params,
        start=SIM_START,
        end=sim_end,
        core_abs_momentum=False,
    )

    logger.info(f"Core-Satellite 可変（Core SELL時→元本保証）: {SIM_START} 〜 {sim_end}")
    cs_dynamic = run_core_satellite_simulation(
        products=products,
        monthly_contribution=monthly_contribution,
        params=new_params,
        start=SIM_START,
        end=sim_end,
        core_abs_momentum=True,
    )

    logger.info("ベンチマーク: 均等B&H（7本）")
    bench_equal = run_benchmark_simulation(
        nav_series_list=list(nav_by_code.values()),
        monthly_contribution=monthly_contribution,
        start=SIM_START,
        end=sim_end,
    )

    logger.info(f"ベンチマーク: 先進国株式のみ ({developed_code})")
    bench_dev = run_benchmark_simulation(
        nav_series_list=[nav_by_code.get(developed_code, {})],
        monthly_contribution=monthly_contribution,
        start=SIM_START,
        end=sim_end,
    )

    logger.info(f"ベンチマーク: 全米株式のみ ({us_code})")
    bench_us = run_benchmark_simulation(
        nav_series_list=[nav_by_code.get(us_code, {})],
        monthly_contribution=monthly_contribution,
        start=SIM_START,
        end=sim_end,
    )

    # Core候補比較 - フル期間（楽天全米株式は全期間データあり）
    logger.info("Core候補比較（フル期間）: Core=楽天全米株式")
    cs_core_us = run_core_satellite_simulation(
        products=products,
        monthly_contribution=monthly_contribution,
        params={**new_params, "CORE_PRODUCT": "JP90C000FHD2"},
        start=SIM_START,
        end=sim_end,
        core_abs_momentum=False,
    )

    # Core候補比較 - 直近期間（楽天プラスS&P500・ACWIはデータが2023-11以降）
    recent_start = "2024-11"
    recent_end = "2026-01"
    logger.info(f"Core候補比較（直近 {recent_start}〜{recent_end}）: 3候補")
    cs_recent_dev = run_core_satellite_simulation(
        products=products,
        monthly_contribution=monthly_contribution,
        params={**new_params, "CORE_PRODUCT": "JP90C000CMK4"},
        start=recent_start,
        end=recent_end,
        core_abs_momentum=False,
    )
    cs_recent_sp500 = run_core_satellite_simulation(
        products=products,
        monthly_contribution=monthly_contribution,
        params={**new_params, "CORE_PRODUCT": "JP90C000Q2U6"},
        start=recent_start,
        end=recent_end,
        core_abs_momentum=False,
    )
    cs_recent_acwi = run_core_satellite_simulation(
        products=products,
        monthly_contribution=monthly_contribution,
        params={**new_params, "CORE_PRODUCT": "JP90C000Q2W2"},
        start=recent_start,
        end=recent_end,
        core_abs_momentum=False,
    )
    core_comparison = {
        "full_period": {
            "start": SIM_START,
            "end": sim_end,
            "results": [
                {"label": "CS固定 / Core=たわら先進国株式（現行・0.099%）", "stats": compute_stats(cs_fixed)},
                {"label": "CS固定 / Core=楽天全米株式（0.162%）", "stats": compute_stats(cs_core_us)},
            ],
        },
        "recent_period": {
            "start": recent_start,
            "end": recent_end,
            "n_months": len(month_range(recent_start, recent_end)),
            "results": [
                {"label": "Core=たわら先進国株式（現行・0.099%）", "stats": compute_stats(cs_recent_dev)},
                {"label": "Core=楽天プラスS&P500（0.077%）", "stats": compute_stats(cs_recent_sp500)},
                {"label": "Core=楽天プラスオールカントリー（0.056%）", "stats": compute_stats(cs_recent_acwi)},
            ],
        },
    }

    # 結果サマリー
    total_contrib = old_history[-1]["contribution_total"]
    print("\n" + "=" * 70)
    print(f"  iDeCo バックキャスト ({SIM_START} 〜 {sim_end})  総拠出額: {total_contrib:,.0f}円")
    print("=" * 70)
    for label, hist in [
        ("旧モメンタム      ", old_history),
        ("CS固定（Core常時）", cs_fixed),
        ("CS可変（Core退避）", cs_dynamic),
        ("均等B&H（7本）    ", bench_equal),
        ("先進国株式のみ    ", bench_dev),
        ("全米株式のみ      ", bench_us),
    ]:
        stats = compute_stats(hist)
        print(
            f"  {label}: {stats['final_value']:>10,.0f}円 "
            f"（利益 {stats['gain']:+,.0f}円 / {stats['gain_pct']:+.1f}% / "
            f"年率 {stats['annualized_pct']:+.1f}% / MaxDD {stats['max_drawdown_pct']:.1f}%）"
        )
    print("=" * 70)

    OUTPUT_DIR.mkdir(exist_ok=True)
    output_path = OUTPUT_DIR / "ideco_backcast_202502.md"
    generate_report(
        old_strategy=old_history,
        cs_fixed=cs_fixed,
        cs_dynamic=cs_dynamic,
        bench_equal=bench_equal,
        bench_dev=bench_dev,
        bench_us=bench_us,
        products=products,
        start=SIM_START,
        end=sim_end,
        output_path=output_path,
        core_comparison=core_comparison,
    )
    print(f"\n  レポート: {output_path}")


if __name__ == "__main__":
    main()
