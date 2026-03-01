"""
iDeCo モメンタムスコアリングエンジン

仕様: ideco_strategy_spec.md に基づく実装
  - 加重モメンタムスコア計算（1M×1 + 3M×2 + 6M×3 + 12M×4）/ 10 - 信託報酬
  - BUY/SELLシグナル判定（2段階フィルター）
  - 掛金割当変更・スイッチング提案生成
  - Markdownレポート出力

MA12の近似:
  return_1y > 0 を「現在基準価額 > 12ヶ月MA」の代替指標として使用。
  12ヶ月分のNAV履歴がdata/nav_history.jsonに蓄積されたら自動的に真のMAを使用。
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class IDeCoScorer:
    """iDeCoポートフォリオの月次判定エンジン"""

    DEFAULT_PARAMS = {
        "TOP_N": 3,
        "CONSECUTIVE_SELL": 2,
        "DEVIATION_THRESHOLD": 0.15,
        "DEVIATION_MONTHS": 3,
        "WEIGHT_1M": 1,
        "WEIGHT_3M": 2,
        "WEIGHT_6M": 3,
        "WEIGHT_12M": 4,
        "MA12_THRESHOLD": 1.0,
        "ALLOCATION_METHOD": "equal",
        "MONTHLY_CONTRIBUTION": 23000,
    }

    def __init__(
        self,
        config: dict,
        signal_history: Optional[dict] = None,
        nav_history: Optional[dict] = None,
    ):
        """
        Args:
            config: ideco_products.json の内容
            signal_history: data/signal_history.json（月次BUY/SELLシグナル履歴）
            nav_history: data/nav_history.json（月次基準価額履歴、MA12計算用）
        """
        self.params = {**self.DEFAULT_PARAMS, **config.get("parameters", {})}
        self.products = config["products"]
        self.capital_guarantee = config.get("capital_guarantee_fund", {})
        self.signal_history = signal_history or {}
        self.nav_history = nav_history or {}
        self.current_month = datetime.now().strftime("%Y-%m")

        # コードをキーにした商品設定辞書
        self._config_by_code = {p["code"]: p for p in self.products}

    # ----------------------------------------------------------------
    # スコア計算
    # ----------------------------------------------------------------

    def calculate_score(self, fund: dict) -> Optional[float]:
        """
        加重モメンタムスコアを計算する。

        スコア = (1M×w1 + 3M×w2 + 6M×w3 + 12M×w4) / (w1+w2+w3+w4) - 信託報酬(%/年)

        リターンはすべて % 表示（例: 2.35 は 2.35%）。
        信託報酬も % 表示（例: 0.2916 は 0.2916%/年）。
        """
        r1 = fund.get("return_1m")
        r3 = fund.get("return_3m")
        r6 = fund.get("return_6m")
        r12 = fund.get("return_1y")

        if any(v is None for v in [r1, r3, r6, r12]):
            logger.warning(f"リターンデータ不足 → スコア計算不可: {fund.get('code')}")
            return None

        w1 = self.params["WEIGHT_1M"]
        w2 = self.params["WEIGHT_3M"]
        w3 = self.params["WEIGHT_6M"]
        w4 = self.params["WEIGHT_12M"]
        total_weight = w1 + w2 + w3 + w4

        weighted_return = (r1 * w1 + r3 * w2 + r6 * w3 + r12 * w4) / total_weight
        expense_ratio = fund.get("expense_ratio", 0.0)

        # コモディティ（Gold）は信託報酬ペナルティを倍加してスコアを抑制
        if fund.get("category") == "commodity":
            multiplier = self.params.get("GOLD_EXPENSE_MULTIPLIER", 1.0)
            expense_ratio *= multiplier

        return round(weighted_return - expense_ratio, 4)

    # ----------------------------------------------------------------
    # 移動平均計算（月次リターン履歴から逆算）
    # ----------------------------------------------------------------

    def _backfill_navs(self, current_nav: float, monthly_returns: list) -> list:
        """
        現在NAVと月次リターン履歴（新→旧順）から各月末NAVを逆算して返す。

        Args:
            current_nav: 現在の基準価額
            monthly_returns: [{"year_month": "YYYY-MM", "return_pct": float}, ...] 新→旧順

        Returns:
            [current_nav, end-of-prev-month-1, end-of-prev-month-2, ...] 新→旧順
        """
        navs = [current_nav]
        nav = current_nav
        for entry in monthly_returns:
            ret = entry["return_pct"] / 100
            if ret == -1.0:
                break  # 0除算を防ぐ
            nav = nav / (1 + ret)
            navs.append(nav)
        return navs

    def _check_ma3_gt_ma6(
        self,
        current_nav: Optional[float],
        monthly_returns: list,
    ) -> Optional[bool]:
        """
        3ヶ月移動平均 > 6ヶ月移動平均 を判定する。

        月次NAVを逆算してMA3とMA6を計算する。
        データ不足の場合はNoneを返す。

        MA3 = 直近3ヶ月の月末NAV平均
        MA6 = 直近6ヶ月の月末NAV平均
        """
        if current_nav is None or len(monthly_returns) < 5:
            return None

        navs = self._backfill_navs(current_nav, monthly_returns)

        if len(navs) < 7:
            return None

        ma3 = sum(navs[:3]) / 3
        ma6 = sum(navs[:6]) / 6
        return ma3 > ma6

    # ----------------------------------------------------------------
    # MA12計算
    # ----------------------------------------------------------------

    def _get_ma12_ratio(self, code: str, current_nav: Optional[float]) -> Optional[float]:
        """
        現在基準価額 / 12ヶ月移動平均 を返す。

        12ヶ月分のNAV履歴があれば真のMAを計算。
        不足の場合はNoneを返し、呼び出し側でreturn_1yによるフォールバックを使う。
        """
        if current_nav is None:
            return None

        code_history = self.nav_history.get(code, {})
        # 現在月を除く直近12ヶ月のNAVを取得
        sorted_months = sorted(code_history.keys(), reverse=True)
        past_navs = [
            code_history[m]
            for m in sorted_months
            if m != self.current_month and code_history[m] is not None
        ][:12]

        if len(past_navs) < 6:
            # データ不足 → None（呼び出し側でフォールバック）
            return None

        ma12 = sum(past_navs) / len(past_navs)
        if ma12 == 0:
            return None
        return round(current_nav / ma12, 6)

    # ----------------------------------------------------------------
    # シグナル判定
    # ----------------------------------------------------------------

    def classify_signal(self, fund: dict, score: Optional[float]) -> dict:
        """
        BUY/SELLシグナルを判定し、判定根拠も返す。

        条件①: score > 0
        条件②: 現在価格 > 12ヶ月MA（MA12データがあれば使用、なければreturn_1y > 0）
        条件③（オプション）: return_3m > return_6m（短期トレンド確認）

        Returns:
            {"signal": "BUY"|"SELL", "reasons": [...]}
        """
        reasons = []

        if score is None:
            return {"signal": "SELL", "reasons": ["リターンデータ不足"]}

        # 条件①: スコアがプラス
        if score <= 0:
            reasons.append(f"①スコア≦0 ({score:.4f})")
            return {"signal": "SELL", "reasons": reasons}
        reasons.append(f"①スコア>0 ({score:.4f}) ✓")

        # 条件②: 12ヶ月MA比較
        threshold = self.params["MA12_THRESHOLD"]
        ma12_ratio = self._get_ma12_ratio(fund["code"], fund.get("nav"))

        if ma12_ratio is not None:
            # 真のMA12を使用
            if ma12_ratio <= threshold:
                reasons.append(f"②現在価格/MA12={ma12_ratio:.3f}≦{threshold} (下降トレンド)")
                return {"signal": "SELL", "reasons": reasons}
            reasons.append(f"②現在価格/MA12={ma12_ratio:.3f}>{threshold} ✓")
        else:
            # フォールバック: 12ヶ月リターン > 0
            return_1y = fund.get("return_1y")
            if return_1y is None or return_1y <= 0:
                val_str = f"{return_1y:.2f}%" if return_1y is not None else "N/A"
                reasons.append(f"②12ヶ月リターン={val_str}≦0 (MA12代替判定)")
                return {"signal": "SELL", "reasons": reasons}
            reasons.append(f"②12ヶ月リターン={return_1y:.2f}%>0 (MA12代替) ✓")

        # 条件③（MA3>MA6）は過感度のためコア戦略から除外
        # バックテストで「V字回復の取り逃し」の一因となったため削除

        return {"signal": "BUY", "reasons": reasons}

    # ----------------------------------------------------------------
    # 全商品スコアリング
    # ----------------------------------------------------------------

    def score_all(self, market_data: list[dict]) -> list[dict]:
        """
        全商品のスコア・シグナルを計算して返す（スコア降順ソート）。

        市場データにconfigの expense_ratio・holdings_ratio をマージする。
        """
        results = []
        for fund in market_data:
            code = fund["code"]
            cfg = self._config_by_code.get(code, {})

            enriched = {
                **fund,
                "expense_ratio": cfg.get("expense_ratio", 0.0),
                "redemption_fee": cfg.get("redemption_fee", 0.0),
                "holdings_ratio": cfg.get("holdings_ratio", 0.0),
                "capital_guarantee": cfg.get("capital_guarantee", False),
                "category": cfg.get("category", "other"),
            }

            score = self.calculate_score(enriched)
            signal_result = self.classify_signal(enriched, score)

            results.append({
                **enriched,
                "score": score,
                "signal": signal_result["signal"],
                "signal_reasons": signal_result["reasons"],
            })

        # スコア降順ソート（Noneは末尾）
        results.sort(
            key=lambda x: (x["score"] is not None, x["score"] or -999),
            reverse=True,
        )
        return results

    # ----------------------------------------------------------------
    # BUY候補選定・割当計算
    # ----------------------------------------------------------------

    def select_buy_candidates(self, scored_funds: list[dict]) -> list[dict]:
        """元本保証商品を除くBUY候補をスコア上位TOP_N本選定する"""
        buy_funds = [
            f for f in scored_funds
            if f["signal"] == "BUY" and not f.get("capital_guarantee", False)
        ]
        return buy_funds[:self.params["TOP_N"]]

    def select_core_satellite_allocation(self, scored_funds: list[dict]) -> list[dict]:
        """
        Core-Satellite モードの掛金配分を決定する。

        CORE_ABS_MOMENTUM=True の場合:
          Core が SELL（12M<0 等）→ Core 枠も元本保証へ退避
        CORE_ABS_MOMENTUM=False（または未設定）の場合:
          Core は BUY/SELL に関わらず常に CORE_RATIO を配分

        Satellite（残り商品）: BUY上位 TOP_N 本に (1 - CORE_RATIO) を均等配分
        Satellite BUYなし: Core（または元本保証）が 100% を受け取る

        Returns:
            [{"new_ratio": float, "allocation_type": "core"|"satellite"|"core_escaped", ...}, ...]
        """
        core_code = self.params.get("CORE_PRODUCT", "JP90C000CMK4")
        core_ratio = self.params.get("CORE_RATIO", 0.70)
        top_n = self.params.get("TOP_N", 1)
        core_abs_momentum = self.params.get("CORE_ABS_MOMENTUM", False)

        core_fund = next((f for f in scored_funds if f["code"] == core_code), None)
        core_is_buy = (core_fund is not None and core_fund["signal"] == "BUY")

        # Core以外のBUY候補（スコア降順）
        satellite_buys = [
            f for f in scored_funds
            if f["signal"] == "BUY"
            and not f.get("capital_guarantee", False)
            and f["code"] != core_code
        ][:top_n]

        result = []

        # Core が SELL かつ絶対モメンタムフィルター有効 → Core 枠を元本保証へ
        use_core = not core_abs_momentum or core_is_buy

        if use_core:
            # 通常: Core は固定配分
            if satellite_buys:
                sat_ratio = round((1.0 - core_ratio) / len(satellite_buys), 4)
                if core_fund:
                    result.append({**core_fund, "new_ratio": round(core_ratio, 4), "allocation_type": "core"})
                for f in satellite_buys:
                    result.append({**f, "new_ratio": sat_ratio, "allocation_type": "satellite"})
            else:
                if core_fund:
                    result.append({**core_fund, "new_ratio": 1.0, "allocation_type": "core"})
        else:
            # Core SELL → Core 枠を元本保証へ退避
            cg = self.capital_guarantee
            logger.info(f"Core({core_code}) SELL → Core枠({core_ratio*100:.0f}%)を元本保証へ退避")
            if satellite_buys:
                sat_ratio = round((1.0 - core_ratio) / len(satellite_buys), 4)
                result.append({
                    **cg, "code": cg.get("code", "GUARANTEE"),
                    "new_ratio": round(core_ratio, 4), "allocation_type": "core_escaped",
                })
                for f in satellite_buys:
                    result.append({**f, "new_ratio": sat_ratio, "allocation_type": "satellite"})
            else:
                result.append({
                    **cg, "code": cg.get("code", "GUARANTEE"),
                    "new_ratio": 1.0, "allocation_type": "core_escaped",
                })

        return result

    def calculate_allocation(self, buy_candidates: list[dict]) -> list[dict]:
        """
        掛金割当比率を計算する。

        ALLOCATION_METHOD = "equal": 均等配分（デフォルト）
        ALLOCATION_METHOD = "score": スコア比例配分
        """
        if not buy_candidates:
            return []

        method = self.params["ALLOCATION_METHOD"]

        if method == "score" and all(f.get("score") for f in buy_candidates):
            total_score = sum(f["score"] for f in buy_candidates)
            return [
                {**f, "new_ratio": round(f["score"] / total_score, 4)}
                for f in buy_candidates
            ]

        # 均等配分
        n = len(buy_candidates)
        base = round(1.0 / n, 4)
        # 端数調整: 合計が1.0になるよう最初の商品で吸収
        ratios = [base] * n
        ratios[0] = round(1.0 - base * (n - 1), 4)
        return [{**f, "new_ratio": r} for f, r in zip(buy_candidates, ratios)]

    # ----------------------------------------------------------------
    # スイッチング判定
    # ----------------------------------------------------------------

    def check_switching_case_a(self, scored_funds: list[dict]) -> list[dict]:
        """
        ケースA: CONSECUTIVE_SELL ヶ月連続SELLシグナルの資産を検出する。

        signal_history の直近N月分を参照（現在月は含まない）。
        """
        threshold = self.params["CONSECUTIVE_SELL"]
        case_a = []

        for fund in scored_funds:
            code = fund["code"]
            if fund.get("capital_guarantee") or fund.get("holdings_ratio", 0) == 0:
                continue

            sorted_months = sorted(self.signal_history.keys(), reverse=True)
            past = [
                self.signal_history[m].get(code, "UNKNOWN")
                for m in sorted_months[:threshold]
            ]

            if len(past) >= threshold and all(s == "SELL" for s in past):
                case_a.append({
                    "code": code,
                    "name": fund["name"],
                    "holdings_ratio": fund.get("holdings_ratio", 0.0),
                    "past_signals": past,
                    "reason": f"直近{threshold}ヶ月連続SELLシグナル（{', '.join(sorted_months[:threshold])}）",
                })

        return case_a

    def check_switching_case_b(
        self, scored_funds: list[dict], buy_allocation: list[dict]
    ) -> list[dict]:
        """
        ケースB: 目標配分との乖離が DEVIATION_THRESHOLD 以上の資産を検出する。

        BUY候補でない商品の目標配分は0%として計算する。
        """
        threshold = self.params["DEVIATION_THRESHOLD"]
        target_by_code = {f["code"]: f["new_ratio"] for f in buy_allocation}
        case_b = []

        for fund in scored_funds:
            if fund.get("capital_guarantee"):
                continue

            code = fund["code"]
            current = fund.get("holdings_ratio", 0.0)
            target = target_by_code.get(code, 0.0)
            deviation = abs(current - target)

            if deviation >= threshold and current > 0:
                direction = "→削減" if current > target else "→増加"
                case_b.append({
                    "code": code,
                    "name": fund["name"],
                    "current_ratio": current,
                    "target_ratio": target,
                    "deviation": round(deviation, 4),
                    "direction": direction,
                    "reason": f"目標配分との乖離{deviation*100:.1f}%（閾値{threshold*100:.0f}%超）",
                })

        return case_b

    # ----------------------------------------------------------------
    # Core候補モニタリング
    # ----------------------------------------------------------------

    def check_core_candidates(self, scored_funds: list[dict], core_monitor_history: dict) -> dict:
        """
        Core候補商品がCoreスコアを連続して上回っているか追跡する。

        CORE_CANDIDATES の各商品について:
          - 今月のスコアが現行CoreスコアをBEATS_COREしているか
          - 連続何ヶ月上回り続けているか
          - CORE_CHANGE_MONTHS ヶ月連続で上回ったら変更提案フラグを立てる

        Returns:
            {
                "current_core": code,
                "current_core_name": str,
                "current_core_score": float,
                "change_months_threshold": int,
                "candidates": {
                    code: {
                        "name": str,
                        "score": float,
                        "beats_core": bool,
                        "consecutive_beats": int,
                        "suggest_change": bool,
                    }
                }
            }
        """
        candidates = self.params.get("CORE_CANDIDATES", [])
        change_months = self.params.get("CORE_CHANGE_MONTHS", 3)
        core_code = self.params.get("CORE_PRODUCT", "JP90C000CMK4")

        fund_by_code = {f["code"]: f for f in scored_funds}
        core_fund = fund_by_code.get(core_code)
        core_score = core_fund.get("score") if core_fund else None

        result = {
            "current_core": core_code,
            "current_core_name": core_fund.get("name", core_code) if core_fund else core_code,
            "current_core_score": core_score,
            "change_months_threshold": change_months,
            "candidates": {},
        }

        for code in candidates:
            if code == core_code:
                continue
            fund = fund_by_code.get(code)
            if not fund:
                continue
            score = fund.get("score")
            beats_core = bool(
                score is not None and core_score is not None and score > core_score
            )

            # 連続上回り月数（今月 + 過去の連続）
            consecutive = 0
            if beats_core:
                consecutive = 1
                for prev_month in sorted(core_monitor_history.keys(), reverse=True):
                    prev_info = (
                        core_monitor_history[prev_month].get("candidates", {}).get(code, {})
                    )
                    if prev_info.get("beats_core", False):
                        consecutive += 1
                    else:
                        break

            result["candidates"][code] = {
                "name": fund.get("name", code),
                "score": score,
                "beats_core": beats_core,
                "consecutive_beats": consecutive,
                "suggest_change": consecutive >= change_months,
            }

        return result

    # ----------------------------------------------------------------
    # NAV履歴・シグナル履歴の更新
    # ----------------------------------------------------------------

    def update_nav_history(self, scored_funds: list[dict]) -> dict:
        """今月のNAVをnav_historyに追加して返す"""
        updated = dict(self.nav_history)
        for fund in scored_funds:
            code = fund["code"]
            nav = fund.get("nav")
            if nav is not None:
                if code not in updated:
                    updated[code] = {}
                updated[code][self.current_month] = nav
        return updated

    def update_signal_history(self, scored_funds: list[dict]) -> dict:
        """今月のシグナルをsignal_historyに追加して返す"""
        monthly = {f["code"]: f["signal"] for f in scored_funds}
        return {**self.signal_history, self.current_month: monthly}

    # ----------------------------------------------------------------
    # レポート生成
    # ----------------------------------------------------------------

    def generate_report(
        self,
        scored_funds: list[dict],
        buy_candidates: list[dict],
        buy_allocation: list[dict],
        case_a_targets: list[dict],
        case_b_targets: list[dict],
        output_path: str,
        core_monitor: dict = None,
    ) -> str:
        """月次判定レポートをMarkdown形式で生成する"""
        now = datetime.now()
        year_month = now.strftime("%Y年%m月")
        lines = [
            "# iDeCo ポートフォリオ月次判定レポート",
            "",
            f"実行日: {now.strftime('%Y-%m-%d %H:%M')}",
            f"対象月: {year_month}",
            "",
            "---",
            "",
        ]

        # --- サマリー ---
        buy_count = len(buy_candidates)
        switching_count = len(case_a_targets) + len(case_b_targets)
        lines += ["## サマリー", ""]

        if buy_count == 0:
            cg_name = self.capital_guarantee.get("name", "元本保証商品")
            lines.append(f"BUY候補: **なし** → **{cg_name}へ全額退避**")
        else:
            names = "・".join(f["name"] for f in buy_candidates)
            lines.append(f"BUY候補: **{buy_count}本** （{names}）")

        lines.append(f"スイッチング: {switching_count}件（ケースA: {len(case_a_targets)}件、ケースB: {len(case_b_targets)}件）")
        lines += ["", "---", ""]

        # --- 掛金割当変更 ---
        lines += ["## 掛金割当変更（今月の積立先）", ""]
        monthly = self.params.get("MONTHLY_CONTRIBUTION", 23000)

        is_core_satellite = self.params.get("STRATEGY_MODE") == "core_satellite"

        if buy_count == 0 and not is_core_satellite:
            cg_name = self.capital_guarantee.get("name", "元本保証商品")
            lines.append(f"掛金 **{monthly:,}円** の全額を **{cg_name}** に設定してください。")
        else:
            if is_core_satellite:
                core_code = self.params.get("CORE_PRODUCT", "")
                lines.append(f"掛金 **{monthly:,}円** をCore固定 + Satellite月次判定で振り向けてください。")
            else:
                lines.append(f"掛金 **{monthly:,}円** を以下の商品に振り向けてください。")
            lines.append("")
            lines.append("| 区分 | 商品名 | コード | 割当比率 | 月額（目安） |")
            lines.append("|---|---|---|---:|---:|")
            for f in buy_allocation:
                amount = int(monthly * f["new_ratio"])
                alloc_type = f.get("allocation_type", "")
                if alloc_type == "core":
                    label = "**Core固定**"
                elif alloc_type == "core_escaped":
                    label = "**Core退避→元本保証**"
                elif alloc_type == "satellite":
                    label = "Satellite"
                else:
                    label = "-"
                lines.append(
                    f"| {label} | {f['name']} | {f['code']} | {f['new_ratio']*100:.0f}% | {amount:,}円 |"
                )

        lines += ["", "---", ""]

        # --- スイッチング指示 ---
        lines += ["## スイッチング指示", ""]

        if not case_a_targets and not case_b_targets:
            lines.append("今月のスイッチング対象はありません。")
        else:
            if case_a_targets:
                lines += ["### ケースA: 緊急離脱（高優先度）", ""]
                lines.append(
                    f"以下の資産は{self.params['CONSECUTIVE_SELL']}ヶ月連続SELLシグナルのため、"
                    "全額スイッチングを実行してください。"
                )
                lines.append("")
                lines.append("| FROM（売却） | 現在比率 | 理由 |")
                lines.append("|---|---:|---|")
                for t in case_a_targets:
                    lines.append(
                        f"| {t['name']} | {t['holdings_ratio']*100:.0f}% | {t['reason']} |"
                    )
                lines.append("")
                if buy_allocation:
                    lines.append("スイッチング先: 上記「掛金割当変更」のBUY対象商品（スコア比で按分）")
                else:
                    cg_name = self.capital_guarantee.get("name", "元本保証商品")
                    lines.append(f"スイッチング先: {cg_name}（BUY候補なし）")
                lines.append("")

            if case_b_targets:
                lines += ["### ケースB: 通常リバランス（低優先度）", ""]
                lines.append(
                    f"目標配分との乖離が{self.params['DEVIATION_THRESHOLD']*100:.0f}%以上の資産です。"
                    "乖離分のみスイッチングを検討してください。"
                )
                lines.append("")
                lines.append("| 商品名 | 現在比率 | 目標比率 | 乖離 | 方向 |")
                lines.append("|---|---:|---:|---:|---|")
                for t in case_b_targets:
                    lines.append(
                        f"| {t['name']} | {t['current_ratio']*100:.0f}% | "
                        f"{t['target_ratio']*100:.0f}% | {t['deviation']*100:.1f}% | {t['direction']} |"
                    )
                lines.append("")

        lines += ["---", ""]

        # --- 全商品スコア一覧 ---
        lines += ["## 全商品スコア一覧（スコア降順）", ""]
        lines.append("| # | シグナル | 商品名 | スコア | 1M | 3M | 6M | 12M | 信託報酬/年 |")
        lines.append("|---:|:---:|---|---:|---:|---:|---:|---:|---:|")

        def fmt_pct(v: Optional[float]) -> str:
            if v is None:
                return "-"
            sign = "+" if v > 0 else ""
            return f"{sign}{v:.2f}%"

        for i, f in enumerate(scored_funds, 1):
            if f.get("capital_guarantee"):
                continue
            signal_str = "**BUY**" if f["signal"] == "BUY" else "SELL"
            score_str = f"{f['score']:.4f}" if f["score"] is not None else "-"
            lines.append(
                f"| {i} | {signal_str} | {f['name']} | {score_str} | "
                f"{fmt_pct(f.get('return_1m'))} | {fmt_pct(f.get('return_3m'))} | "
                f"{fmt_pct(f.get('return_6m'))} | {fmt_pct(f.get('return_1y'))} | "
                f"{f.get('expense_ratio', 0):.4f}% |"
            )

        lines += ["", "---", ""]

        # --- シグナル判定根拠 ---
        lines += ["## シグナル判定根拠", ""]
        for f in scored_funds:
            if f.get("capital_guarantee"):
                continue
            signal_str = "BUY" if f["signal"] == "BUY" else "SELL"
            lines.append(f"**{f['name']}** → {signal_str}")
            for reason in f.get("signal_reasons", []):
                lines.append(f"  - {reason}")
            lines.append("")

        lines += ["---", ""]

        # --- スコア計算式 ---
        w1 = self.params["WEIGHT_1M"]
        w2 = self.params["WEIGHT_3M"]
        w3 = self.params["WEIGHT_6M"]
        w4 = self.params["WEIGHT_12M"]
        total_w = w1 + w2 + w3 + w4
        lines += [
            "## パラメータ",
            "",
            f"スコア = (1M×{w1} + 3M×{w2} + 6M×{w3} + 12M×{w4}) / {total_w} − 信託報酬(%/年)",
            "",
            "| パラメータ | 値 |",
            "|---|---:|",
            f"| 選択商品数(TOP_N) | {self.params['TOP_N']}本 |",
            f"| 連続SELL閾値(ケースA) | {self.params['CONSECUTIVE_SELL']}ヶ月 |",
            f"| 乖離閾値(ケースB) | {self.params['DEVIATION_THRESHOLD']*100:.0f}% |",
            f"| 配分方法 | {self.params['ALLOCATION_METHOD']} |",
            "",
        ]

        # --- Core候補モニタリング ---
        if core_monitor and core_monitor.get("candidates"):
            core_name = core_monitor["current_core_name"]
            core_score = core_monitor["current_core_score"]
            threshold = core_monitor["change_months_threshold"]
            core_score_str = f"{core_score:.4f}" if core_score is not None else "-"

            lines += ["---", "", "## Core候補モニタリング", ""]
            lines.append(f"現行Core: **{core_name}** / スコア {core_score_str}")
            lines.append(f"変更提案条件: 候補が **{threshold}ヶ月連続** でCoreスコアを上回ること")
            lines.append("")
            lines.append("| 候補商品 | スコア | Core比差 | 連続上回り | 提案 |")
            lines.append("|---|---:|---:|:---:|:---:|")

            any_suggest = False
            for code, info in core_monitor["candidates"].items():
                score = info["score"]
                diff = (
                    (score - core_score)
                    if (score is not None and core_score is not None)
                    else None
                )
                consecutive = info["consecutive_beats"]
                suggest = info["suggest_change"]
                if suggest:
                    any_suggest = True
                score_str = f"{score:.4f}" if score is not None else "-"
                diff_str = f"{diff:+.4f}" if diff is not None else "-"
                filled = min(consecutive, threshold)
                bar = "▓" * filled + "░" * max(0, threshold - filled)
                suggest_str = "**⚠ 変更検討**" if suggest else "-"
                lines.append(
                    f"| {info['name']} | {score_str} | {diff_str} | "
                    f"{bar} {consecutive}/{threshold}M | {suggest_str} |"
                )

            if any_suggest:
                lines += [
                    "",
                    "> ⚠ Core変更候補あり: `config/ideco_products.json` の `CORE_PRODUCT` 変更を検討してください。",
                ]
            lines += ["", "---", ""]

        # --- データ取得失敗商品 ---
        failed = [f for f in scored_funds if f.get("score") is None and not f.get("capital_guarantee")]
        if failed:
            lines += ["---", "", "## データ取得失敗商品", ""]
            lines.append("以下の商品はデータ不足のためスコア計算から除外されました。")
            lines.append("")
            for f in failed:
                lines.append(f"- {f['name']} ({f['code']})")
            lines.append("")

        markdown = "\n".join(lines)

        with open(output_path, "w", encoding="utf-8") as fp:
            fp.write(markdown)

        logger.info(f"レポート生成完了: {output_path}")
        return markdown
