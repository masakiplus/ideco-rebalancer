# Core-Satellite戦略 デバッグ調査結果

> 調査日: 2026-03-01
> ブランチ: `claude/debug-core-settings-FWLCE`

---

## 調査の発端

「コア70%の設定でバグがあるかも。コアの商品が固定になってないか」

---

## 結論

**Core 70%の配分ロジックにバグはない。** Satellite商品も5種類にローテーションしており固定ではない。

ただし**Satellite枠の実効性に構造的な問題**がある。

---

## 検証方法

合成データ（7商品×77ヶ月）でCore-Satelliteシミュレーションを実行し、月次のSatellite選択・配分を追跡。

検証スクリプト: `tools/test_core_satellite.py`

---

## 検証結果

### パフォーマンス比較（合成データ）

| 戦略 | 利益率 | 年率 | MaxDD | Core100%比 |
|---|---|---|---|---|
| Core-Satellite(70/30) | +41.0% | +5.5% | 15.0% | **+0.8%** |
| 先進国株式のみ(=Core100%) | +39.9% | +5.4% | 15.7% | 基準 |
| 全米株式のみ | +47.2% | +6.2% | 17.8% | +7.3% |
| 均等B&H(7本) | +34.3% | +4.7% | 10.7% | -5.6% |

### 仕様書記載のバックテスト結果（実データ）

| 戦略 | 年率 | Core100%比 |
|---|---|---|
| Core-Satellite(70/30) | +9.9% | **-0.4%** |
| 先進国株式のみ(=Core100%) | +10.3% | 基準 |
| 全米株式のみ | +10.8% | +0.5% |

**実データでもCore-Satelliteは先進国株式のみに負けている。**

### Satellite選択内訳（合成データ、77ヶ月）

| Satellite商品 | 選択回数 | 選択率 |
|---|---|---|
| 楽天・全米株式 | 27回 | 35.1% |
| ステートストリート・ゴールド | 21回 | 27.3% |
| 三井住友DC日本REIT | 11回 | 14.3% |
| 三井住友DC外国REIT | 10回 | 13.0% |
| 三井住友DC日本株 | 8回 | 10.4% |
| Core100%（BUYなし） | 0回 | 0.0% |

- 5種類でローテーション → **固定ではない**
- ただし全米株式が35%占有（Coreと高相関、分散効果薄い）
- 先進国債券は**一度も選ばれていない**

---

## 構造的な問題

### なぜSatellite枠の寄与が小さいか

1. **新規掛金の30%のみ**がSatelliteに配分される（月6,900円）
2. 既存残高（約750万円）はSatellite配分の影響を受けない
3. 月6,900円 vs 既存750万円 → 全体への影響は微小
4. Case Aスイッチングで非Core残高はCoreに移動 → さらにSatellite残高が減少

### 分散強制（US株除外）の効果

`SATELLITE_EXCLUDE_CATEGORIES: ["us_equity"]` を試行したが:

- 改善幅: **+786円/77ヶ月**（誤差レベル）
- Satellite選択は4商品に均等分散するようになるが、リターンへの影響はほぼゼロ
- **撤回済み**（空配列に戻した）

---

## ローカルで要実行のタスク

### 1. 実データバックキャスト

```bash
python3 tools/ideco_backcast.py
# or
python3 tools/ideco_backcast.py --use-cached
```

レポートに「Satellite選択統計」セクションが追加されているので、実データでの選択内訳を確認する。

### 2. BS4フォールバックの検証

```bash
python3 -c "
from tools.ideco_scraper import RakutenIdecoScraper
s = RakutenIdecoScraper()
d = s._fetch_with_requests(
    'https://www.rakuten-sec.co.jp/web/fund/detail/401k.html?ID=JP90C000CMK4&401k_no=',
    'JP90C000CMK4')
print(d.get('nav'), d.get('monthly_returns', [])[:3])
"
```

NAVと月次リターンが取れればPlaywright不要で運用可能。

### 3. 戦略の判断

実データバックキャストの結果を踏まえ、以下を検討:

- **現状維持**: Satellite枠は小さいが害もない。モメンタムシグナルの学習用として継続
- **Core100%に簡素化**: Satellite枠を廃止し、掛金100%をCoreに固定。コード・運用の複雑性を削減
- **Satellite強化**: 比率を50/50にする、TOP_N=2にする等で影響力を上げる（ただしリスクも増加）

---

## CORE_RATIO縮小・可変化の検討

> 上記「Satellite強化」の具体案として、CORE_RATIOを小さくする or 動的にする方向性。

### 案A: CORE_RATIOを固定で引き下げ（例: 0.50〜0.60）

| 設定 | Core配分 | Satellite配分（TOP_N=1） | 月額イメージ |
|------|----------|--------------------------|-------------|
| 現行 0.70 | 70% | 30%（6,900円） | Core 16,100円 / Sat 6,900円 |
| 0.60 | 60% | 40%（9,200円） | Core 13,800円 / Sat 9,200円 |
| 0.50 | 50% | 50%（11,500円） | Core 11,500円 / Sat 11,500円 |

- **メリット**: config変更のみ（`CORE_RATIO: 0.50`）。コード変更不要
- **デメリット**: Satellite側のモメンタムシグナルが弱い月でも高配分してしまう
- **次のアクション**: バックテストで 0.50 / 0.60 / 0.70 を比較

### 案B: CORE_RATIOを動的に変動（シグナル強度連動）

Satelliteのモメンタムスコアに応じてCORE_RATIOを段階的に変える:

```
サテライト最上位の composite_score に応じて:
  score > 0.8  → CORE_RATIO = 0.50（強シグナル → Satellite多め）
  score > 0.5  → CORE_RATIO = 0.60（中シグナル）
  score ≤ 0.5  → CORE_RATIO = 0.70（弱シグナル → 現行維持）
  BUYゼロ      → CORE_RATIO = 1.00（現行通りフォールバック）
```

- **メリット**: 市場環境に適応。強いトレンド時にSatelliteを多く取れる
- **デメリット**: パラメータ増加（閾値×比率のペア）。バックテスト調整が複雑化、過学習リスク

### 推奨アプローチ

1. **まず案Aで CORE_RATIO = 0.50〜0.60 のバックテスト比較を実行**
2. 改善が確認できればその比率を採用
3. さらに攻めたい場合に案Bへ段階的に進化

---

## 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `config/ideco_products.json` | `SATELLITE_EXCLUDE_CATEGORIES: []` パラメータ追加 |
| `tools/ideco_scorer.py` | 除外カテゴリフィルター対応 |
| `tools/ideco_backcast.py` | 除外カテゴリ対応 + Satellite選択統計セクション追加 |
| `tools/ideco_scraper.py` | requests+BS4フォールバック追加 |
| `tools/test_core_satellite.py` | 合成データ検証スクリプト（新規） |
