"""
iDeCo 月次ポートフォリオ判定スクリプト（仕様書準拠版）

仕様書: ideco_strategy_spec.md
設計書: .tmp/design.md

使い方:
  cd tools
  python3 ideco_rebalancer.py [--dry-run] [--playwright]

オプション:
  --dry-run      スクレイピングをスキップしてシグナル履歴のみ更新する
  --playwright   スクレイピング時にPlaywrightを使用（デフォルトはrequests）

設定:
  config/ideco_products.json  商品リスト・信託報酬・現在残高比率・パラメータ

入力:
  data/signal_history.json  月次シグナル履歴（自動生成・更新）
  data/nav_history.json     月次NAV履歴（自動生成・更新、MA12計算用）

出力:
  output/ideco_report_YYYYMM.md  月次判定レポート
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# tools/ ディレクトリを sys.path に追加（同ディレクトリから import できるように）
sys.path.insert(0, str(Path(__file__).parent))

from ideco_scraper import RakutenIdecoScraper
from ideco_scorer import IDeCoScorer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
CONFIG_PATH = PROJECT_DIR / "config" / "ideco_products.json"
SIGNAL_HISTORY_PATH = PROJECT_DIR / "data" / "signal_history.json"
NAV_HISTORY_PATH = PROJECT_DIR / "data" / "nav_history.json"
CORE_MONITOR_HISTORY_PATH = PROJECT_DIR / "data" / "core_monitor_history.json"
OUTPUT_DIR = PROJECT_DIR / "output"


def load_json(path: Path, default=None):
    if not path.exists():
        return default if default is not None else {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"保存完了: {path}")


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        logger.error(f"設定ファイルが見つかりません: {config_path}")
        sys.exit(1)

    config = load_json(config_path)

    products = config.get("products", [])
    if not products:
        logger.error("設定ファイルに products が定義されていません")
        sys.exit(1)

    non_cg = [p for p in products if not p.get("capital_guarantee")]
    holdings_total = sum(p.get("holdings_ratio", 0) for p in non_cg)
    non_monitored = config.get("_non_monitored_holdings", {})
    non_monitored_total = sum(v.get("ratio", 0) for v in non_monitored.values() if isinstance(v, dict))
    effective_total = holdings_total + non_monitored_total
    if abs(holdings_total) > 0.01 and abs(effective_total - 1.0) > 0.05:
        logger.warning(
            f"holdings_ratio の合計（監視商品 {holdings_total:.3f} + 非監視 {non_monitored_total:.3f}"
            f" = {effective_total:.3f}）が1.0から大きく乖離しています。"
            "config/ideco_products.json を確認してください。"
        )
    elif non_monitored:
        logger.info(
            f"holdings_ratio: 監視商品 {holdings_total:.3f} + 非監視 {non_monitored_total:.3f}"
            f" = {effective_total:.3f}（非監視: {', '.join(v['name'] for v in non_monitored.values() if isinstance(v, dict))}）"
        )

    logger.info(
        f"設定読み込み完了: {len(products)} 商品 "
        f"（うち元本保証: {sum(1 for p in products if p.get('capital_guarantee'))}本）"
    )
    return config


def print_summary(
    scored_funds: list[dict],
    buy_allocation: list[dict],
    case_a: list[dict],
    case_b: list[dict],
    output_path: Path,
):
    """判定結果サマリーを標準出力に表示する"""
    print("\n" + "=" * 60)
    print("iDeCo 月次判定 結果サマリー")
    print("=" * 60)

    buy_funds = [f for f in scored_funds if f["signal"] == "BUY" and not f.get("capital_guarantee")]
    sell_funds = [f for f in scored_funds if f["signal"] == "SELL" and not f.get("capital_guarantee")]

    print(f"\nBUY: {len(buy_funds)}本  SELL: {len(sell_funds)}本")

    if buy_allocation:
        print("\n[掛金割当変更]")
        for f in buy_allocation:
            print(f"  {f['name']}: {f['new_ratio']*100:.0f}%")
    else:
        print("\n[掛金割当変更] BUY候補なし → 元本保証商品へ全額")

    if case_a:
        print(f"\n[スイッチング ケースA] {len(case_a)}件")
        for t in case_a:
            print(f"  - {t['name']}: {t['reason']}")

    if case_b:
        print(f"\n[スイッチング ケースB] {len(case_b)}件")
        for t in case_b:
            print(f"  - {t['name']}: 乖離{t['deviation']*100:.1f}% {t['direction']}")

    print(f"\nレポート: {output_path}")
    print("=" * 60 + "\n")


def make_dummy_market_data(products: list[dict]) -> list[dict]:
    """
    --dry-run 時に使用するダミーデータ。
    信託報酬のみ設定し、リターンはすべてNoneとする。
    """
    return [
        {
            "code": p["code"],
            "name": p["name"],
            "nav": None,
            "return_1m": None,
            "return_3m": None,
            "return_6m": None,
            "return_1y": None,
            "fetched_at": datetime.now().isoformat(),
        }
        for p in products
        if not p.get("capital_guarantee")
    ]


def main():
    parser = argparse.ArgumentParser(description="iDeCo 月次ポートフォリオ判定")
    parser.add_argument("--dry-run", action="store_true", help="スクレイピングなしでテスト実行")
    parser.add_argument("--playwright", action="store_true", help="Playwrightを使用")
    args = parser.parse_args()

    logger.info("=== iDeCo 月次判定 開始 ===")

    # 1. 設定・履歴読み込み
    config = load_config(CONFIG_PATH)
    signal_history = load_json(SIGNAL_HISTORY_PATH, default={})
    nav_history = load_json(NAV_HISTORY_PATH, default={})
    core_monitor_history = load_json(CORE_MONITOR_HISTORY_PATH, default={})

    non_cg_products = [p for p in config["products"] if not p.get("capital_guarantee")]
    product_codes = [p["code"] for p in non_cg_products]

    logger.info(f"シグナル履歴: {len(signal_history)}ヶ月分")
    logger.info(f"NAV履歴: {len(nav_history)}商品分")

    # 2. スクレイピング
    if args.dry_run:
        logger.info("--dry-run: スクレイピングをスキップ")
        market_data = make_dummy_market_data(non_cg_products)
    else:
        logger.info(f"データ取得開始: {len(product_codes)} 商品")
        scraper = RakutenIdecoScraper()
        market_data = scraper.fetch_all(product_codes)

        if not market_data:
            logger.error("全商品のデータ取得に失敗しました。処理を中断します。")
            logger.error("inspect_page.py を実行してURLとセレクタを確認してください。")
            sys.exit(1)

        logger.info(f"取得完了: {len(market_data)}/{len(product_codes)} 商品")

    # 3. スコアリング・シグナル判定
    scorer = IDeCoScorer(config, signal_history, nav_history)
    scored_funds = scorer.score_all(market_data)

    # 4. BUY候補選定・割当計算
    strategy_mode = config.get("parameters", {}).get("STRATEGY_MODE", "momentum")
    if strategy_mode == "core_satellite":
        buy_allocation = scorer.select_core_satellite_allocation(scored_funds)
        # サマリー表示用: satellite商品のみ buy_candidates として扱う
        buy_candidates = [f for f in buy_allocation if f.get("allocation_type") == "satellite"]
        logger.info(
            f"Core-Satellite モード: Core={config['parameters'].get('CORE_PRODUCT')} "
            f"({config['parameters'].get('CORE_RATIO', 0.7)*100:.0f}%) / "
            f"Satellite={[f['name'][:15] for f in buy_candidates]}"
        )
    else:
        buy_candidates = scorer.select_buy_candidates(scored_funds)
        buy_allocation = scorer.calculate_allocation(buy_candidates)

    # 4b. Core候補モニタリング
    core_monitor = scorer.check_core_candidates(scored_funds, core_monitor_history)

    # 5. スイッチング判定
    case_a = scorer.check_switching_case_a(scored_funds)
    # Core-Satelliteモードでは Case B 不使用
    # （全米株式などレガシー保有を誤って削減対象にしてしまうため）
    if strategy_mode == "core_satellite":
        case_b = []
    else:
        case_b = scorer.check_switching_case_b(scored_funds, buy_allocation)

    # 6. 履歴更新・保存
    if not args.dry_run:
        updated_nav = scorer.update_nav_history(scored_funds)
        save_json(NAV_HISTORY_PATH, updated_nav)

    updated_signals = scorer.update_signal_history(scored_funds)
    save_json(SIGNAL_HISTORY_PATH, updated_signals)

    # Core監視履歴を更新（今月のCore vs 候補スコアを記録）
    current_month = datetime.now().strftime("%Y-%m")
    updated_core_monitor = {
        **core_monitor_history,
        current_month: core_monitor,
    }
    save_json(CORE_MONITOR_HISTORY_PATH, updated_core_monitor)

    # 7. レポート生成
    OUTPUT_DIR.mkdir(exist_ok=True)
    year_month = datetime.now().strftime("%Y%m")
    output_path = OUTPUT_DIR / f"ideco_report_{year_month}.md"

    scorer.generate_report(
        scored_funds, buy_candidates, buy_allocation, case_a, case_b, str(output_path),
        core_monitor=core_monitor,
    )

    # 8. サマリー表示
    print_summary(scored_funds, buy_allocation, case_a, case_b, output_path)

    logger.info("=== iDeCo 月次判定 完了 ===")


if __name__ == "__main__":
    main()
