# iDeCo ポートフォリオ月次判定スクリプト

楽天証券 iDeCo の提供商品から基準価額・リターンを自動取得し、Core-Satellite戦略に基づいて月次の掛金割当変更・スイッチング指示を生成する。

作成日: 2026-02-28
最終更新: 2026-03-01（Core-Satellite戦略へ移行）

---

## 月次運用手順

月末または月初に以下を実行する。

```bash
cd ~/Documents/notes/PRJ/20260228_確定拠出年金_portfolio_rebalancing
.venv/bin/python3 tools/ideco_rebalancer.py
```

`output/ideco_report_YYYYMM.md` にレポートが生成される。レポートに従って楽天証券 DC サイトで掛金割当変更・スイッチングを行う。

---

## ファイル構成

```
.
├── README.md                              ← このファイル
├── config/
│   └── ideco_products.json               ← 商品リスト・信託報酬・パラメータ設定
├── data/
│   ├── nav_history.json                  ← 月次NAV履歴（MA12計算用・自動更新）
│   ├── signal_history.json              ← 月次BUY/SELLシグナル履歴（自動更新）
│   └── backcast_cache.json              ← バックキャスト用スクレイピングキャッシュ
├── tools/
│   ├── ideco_rebalancer.py               ← メインスクリプト（実行エントリーポイント）
│   ├── ideco_scraper.py                  ← 楽天証券 401k ページスクレイパー
│   ├── ideco_scorer.py                   ← モメンタムスコアリング・BUY/SELL判定
│   ├── ideco_backcast.py                 ← バックキャストシミュレーション
│   └── requirements.txt                  ← Python 依存パッケージ
├── output/
│   ├── ideco_report_YYYYMM.md            ← 月次レポート（自動生成）
│   └── ideco_backcast_202502.md          ← バックキャストレポート
└── ref/
    ├── ideco_strategy_spec.md            ← 戦略仕様書（スコア計算式・判定ロジック）
    ├── relative_momentum_strategy.md     ← 相対モメンタム手法リファレンス
    ├── iDeco運用検討シート.xlsx           ← 2018-2019年バックテスト記録
    └── 提供商品一覧...html               ← 楽天証券 iDeCo 提供商品一覧（2026-03取得）
```

---

## 対象商品（2026-03確定）

楽天証券 iDeCo の提供商品から以下 7 本をモニタリング対象とする。

| 資産クラス | 商品名 | ISIN | 信託報酬 | 役割 |
|---|---|---|---:|---|
| 先進国株式 | たわらノーロード先進国株式 | JP90C000CMK4 | 0.099% | **Core（70%固定）** |
| 米国株式 | 楽天・全米株式インデックス・ファンド | JP90C000FHD2 | 0.162% | Satellite候補 |
| 国内株式 | 三井住友・DC日本株インデックスファンド | JP90C00081U4 | 0.176% | Satellite候補 |
| 海外REIT | 三井住友・DC外国リートインデックスファンド | JP90C000DX82 | 0.297% | Satellite候補 |
| 国内REIT | 三井住友・DC日本リートインデックスファンド | JP90C000DX74 | 0.275% | Satellite候補 |
| 先進国債券 | たわらノーロード先進国債券 | JP90C000CML2 | 0.187% | Satellite候補 |
| コモディティ | ステートストリート・ゴールドファンド（ヘッジあり） | JP90C0008QL7 | 0.895% | Satellite候補（ペナルティ×2） |

全 BUY 候補ゼロ時の退避先: **みずほ DC 定期預金（1年）**（元本確保型）

除外した資産クラス:
- 国内債券: たわら国内債券は新規購入停止、代替の明治安田 DC 日本債券は 0.66% でコスト高
- 新興国株式・新興国債券: ISIN コード未取得（`config/ideco_products.json` の `_pending_isin` 参照）

---

## 戦略サマリー（Core-Satellite）

Gary Antonacci の Dual Momentum を日本 iDeCo 向けにアレンジした Core-Satellite 戦略。

**スコア計算式**

```
スコア = (1M×1 + 3M×2 + 6M×4 + 12M×3) / 10 − 実効信託報酬(%/年)
```

Gold のみ: 実効信託報酬 = 信託報酬 × 2.0

**BUY シグナル条件（2つ全て）**

1. スコア > 0
2. 12ヶ月リターン > 0（絶対モメンタム）

**掛金配分**

- Core: たわらノーロード先進国株式 = 70%（BUY/SELLに関わらず固定）
- Satellite: 非Core BUY候補の上位1本 = 30%
- Satellite BUYなし: Core が 100%

**スイッチングルール**

- ケース A: 2ヶ月連続 SELL の非Core商品 → 残高全額を Core（たわら先進国株式）へ
- ケース B: 無効（Core-Satelliteモードでは使用しない）

詳細は `ref/ideco_strategy_spec.md` を参照。

---

## バックテスト結果（2018-10〜2025-02、77ヶ月）

| 戦略 | 最終資産額 | 運用益率 | 年率 | MaxDD |
|------|---:|---:|---:|---:|
| 旧モメンタム | 2,085,099円 | +17.7% | +2.6% | 22.0% |
| **新Core-Satellite** | **3,244,039円** | **+83.2%** | **+9.9%** | **21.6%** |
| 均等B&H（7本） | 2,596,886円 | +46.6% | +6.1% | 15.1% |
| 先進国株式のみ | 3,331,624円 | +88.1% | +10.3% | 20.3% |
| 全米株式のみ | 3,422,319円 | +93.2% | +10.8% | 19.8% |

---

## 環境セットアップ（初回のみ）

```bash
cd ~/Documents/notes/PRJ/20260228_確定拠出年金_portfolio_rebalancing

# uv で仮想環境作成・パッケージインストール
uv venv
uv pip install -r tools/requirements.txt
.venv/bin/python3 -m playwright install chromium
```

---

## オプション

```bash
# スクレイピングなしでシグナル履歴のみ更新（動作確認用）
.venv/bin/python3 tools/ideco_rebalancer.py --dry-run

# Playwright を明示的に使用（デフォルトは requests）
.venv/bin/python3 tools/ideco_rebalancer.py --playwright

# バックキャストシミュレーション（キャッシュ使用）
.venv/bin/python3 tools/ideco_backcast.py --use-cached

# バックキャストシミュレーション（再スクレイピング）
.venv/bin/python3 tools/ideco_backcast.py
```
