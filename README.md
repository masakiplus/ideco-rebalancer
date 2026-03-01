# iDeCo ポートフォリオ月次判定スクリプト

楽天証券 iDeCo の提供商品から基準価額・リターンを自動取得し、Core-Satellite モメンタム戦略に基づいて月次の掛金割当変更・スイッチング指示を生成する。

---

## 戦略概要（Core-Satellite）

**スコア計算式**

```
スコア = (1M×1 + 3M×2 + 6M×4 + 12M×3) / 10 − 実効信託報酬(%/年)
```

**BUY シグナル条件（2つ全て）**

1. スコア > 0
2. 12ヶ月リターン > 0（絶対モメンタム）

**掛金配分**

- Core: たわらノーロード先進国株式 = 70%（常時固定）
- Satellite: 非Core BUY候補のスコア上位1本 = 30%
- Satellite BUY候補ゼロ時: Core が 100%

**スイッチングルール**

- 2ヶ月連続 SELL の非Core保有商品 → 全額を Core へスイッチング

**Core候補モニタリング**

毎月、設定した候補商品（楽天S&P500・楽天ACWI）とCoreのスコアを比較し、候補が3ヶ月連続で上回ると月次レポートに変更提案を表示する。Core変更は手動で実施する。

---

## バックテスト結果（2018-10〜2025-02、77ヶ月、総拠出額177万円）

| 戦略 | 最終資産額 | 運用益率 | 年率 | MaxDD |
|------|---:|---:|---:|---:|
| **CS固定（推奨）** | **3,244,257円** | **+83.2%** | **+9.9%** | **21.6%** |
| 均等B&H（7本） | 2,544,541円 | +43.7% | +5.8% | 12.0% |
| 先進国株式のみ | 3,331,624円 | +88.1% | +10.3% | 20.3% |
| 全米株式のみ | 3,422,319円 | +93.2% | +10.8% | 19.8% |

---

## 対象商品（2026-03確定）

楽天証券 iDeCo の提供商品から9本をモニタリング対象とする。

| 資産クラス | 商品名 | 信託報酬 | 役割 |
|---|---|---:|---|
| 先進国株式 | たわらノーロード先進国株式 | 0.099% | Core（現在設定） |
| 米国株式 | 楽天・プラス・S&P500インデックス・ファンド | 0.077% | Satellite候補・Core候補 |
| 米国株式 | 楽天・全米株式インデックス・ファンド | 0.162% | Satellite候補 |
| 全世界株式 | 楽天・プラス・オールカントリー株式インデックス・ファンド | 0.056% | Satellite候補・Core候補 |
| 国内株式 | 三井住友・DC日本株インデックスファンド | 0.176% | Satellite候補 |
| 海外REIT | 三井住友・DC外国リートインデックスファンド | 0.297% | Satellite候補 |
| 国内REIT | 三井住友・DC日本リートインデックスファンド | 0.275% | Satellite候補 |
| 先進国債券 | たわらノーロード先進国債券 | 0.187% | Satellite候補 |
| コモディティ | ステートストリート・ゴールドファンド（ヘッジあり） | 0.895% | Satellite候補（ペナルティ×2） |

全BUY候補ゼロ時の退避先: みずほDC定期預金（1年）

---

## セットアップ

```bash
# リポジトリをクローン
git clone https://github.com/masakiplus/ideco-rebalancer.git
cd ideco-rebalancer

# 仮想環境作成・依存パッケージインストール（uv 推奨）
uv venv
uv pip install -r tools/requirements.txt

# Playwright は任意（デフォルトは requests+BeautifulSoup4）
# .venv/bin/python3 -m playwright install chromium
```

---

## 使い方

**月次実行（月末または月初）**

```bash
.venv/bin/python3 tools/ideco_rebalancer.py
```

`output/ideco_report_YYYYMM.md` にレポートが生成される。レポートに従って楽天証券 DC サイトで掛金割当変更・スイッチングを行う。

**その他のオプション**

```bash
# スクレイピングなしで動作確認
.venv/bin/python3 tools/ideco_rebalancer.py --dry-run

# Playwright を使用してスクレイピング
.venv/bin/python3 tools/ideco_rebalancer.py --playwright

# バックキャストシミュレーション
.venv/bin/python3 tools/ideco_backcast.py --use-cached
```

---

## ファイル構成

```
.
├── config/
│   └── ideco_products.json     ← 商品リスト・信託報酬・パラメータ設定
├── data/
│   ├── nav_history.json        ← 月次NAV履歴（自動更新）
│   ├── signal_history.json     ← 月次BUY/SELLシグナル履歴（自動更新）
│   └── core_monitor_history.json ← Core候補の連続上回り月数履歴（自動更新）
├── tools/
│   ├── ideco_rebalancer.py     ← メインスクリプト
│   ├── ideco_scraper.py        ← 楽天証券スクレイパー
│   ├── ideco_scorer.py         ← スコアリング・シグナル判定
│   ├── ideco_backcast.py       ← バックキャストシミュレーション
│   └── requirements.txt
├── output/
│   └── ideco_report_YYYYMM.md  ← 月次レポート（自動生成）
└── ref/
    └── ideco_strategy_spec.md  ← 戦略仕様書
```

---

## 設定変更

`config/ideco_products.json` の `parameters` セクションで主要パラメータを変更できる。

| パラメータ | デフォルト | 説明 |
|---|---:|---|
| `CORE_PRODUCT` | JP90C000CMK4 | Core商品のISINコード |
| `CORE_RATIO` | 0.70 | Core掛金比率 |
| `TOP_N` | 1 | Satelliteに選ぶ商品数 |
| `CONSECUTIVE_SELL` | 2 | スイッチング発動の連続SELL月数 |
| `CORE_CANDIDATES` | [JP90C000Q2U6, JP90C000Q2W2] | Core変更モニタリング対象 |
| `CORE_CHANGE_MONTHS` | 3 | Core変更提案の連続上回り月数 |
