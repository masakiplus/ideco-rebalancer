"""
iDeCo 保有資産管理モジュール

data/holdings.json で保有資産（金額ベース）を永続管理する。
初回は手動入力 or --initial-value で設定し、以降は毎月の実行時に
NAV変動・掛金追加・スイッチングを反映して自動更新する。
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class HoldingsManager:
    """保有資産の金額・比率を管理するクラス"""

    def __init__(self, holdings_path: Path):
        self.path = holdings_path
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        return {}

    @property
    def is_initialized(self) -> bool:
        return bool(self.data.get("holdings"))

    @property
    def holdings(self) -> dict:
        return self.data.get("holdings", {})

    @property
    def last_nav(self) -> dict:
        return self.data.get("last_nav", {})

    @property
    def last_updated(self) -> Optional[str]:
        return self.data.get("last_updated")

    def initialize(self, amounts: dict[str, float], navs: dict[str, float]) -> None:
        """初回設定: 各商品の金額とNAVを設定する"""
        self.data = {
            "last_updated": datetime.now().strftime("%Y-%m"),
            "last_nav": {k: v for k, v in navs.items() if v is not None},
            "holdings": {k: v for k, v in amounts.items() if v > 0},
            "history": [{
                "month": datetime.now().strftime("%Y-%m"),
                "event": "initialize",
                "details": {k: v for k, v in amounts.items() if v > 0},
            }],
        }
        logger.info(f"保有資産を初期化: {len(self.holdings)}商品, 合計 {self.total:,.0f}円")

    def initialize_from_ratios(
        self, total_value: float, products: list[dict], navs: dict[str, float]
    ) -> None:
        """config の holdings_ratio と総資産額から初期化する"""
        amounts = {}
        for p in products:
            ratio = p.get("holdings_ratio", 0)
            if ratio > 0:
                amounts[p["code"]] = round(total_value * ratio)
        self.initialize(amounts, navs)

    @property
    def total(self) -> float:
        return sum(self.holdings.values())

    def get_summary(self) -> dict:
        """保有状況のサマリーを返す"""
        total = self.total
        items = []
        for code, amount in sorted(self.holdings.items(), key=lambda x: -x[1]):
            if amount <= 0:
                continue
            ratio = amount / total if total > 0 else 0
            items.append({
                "code": code,
                "amount": amount,
                "ratio": ratio,
            })
        return {
            "total": total,
            "last_updated": self.last_updated,
            "items": items,
        }

    def apply_nav_changes(self, current_navs: dict[str, float]) -> dict[str, float]:
        """
        NAV変動を保有額に反映する。

        Returns:
            各商品の変動額 {code: change_amount}
        """
        changes = {}
        prev_navs = self.last_nav

        for code, amount in list(self.holdings.items()):
            if code == "GUARANTEE" or amount <= 0:
                changes[code] = 0
                continue

            prev = prev_navs.get(code)
            curr = current_navs.get(code)
            if prev and curr and prev > 0:
                new_amount = round(amount * curr / prev)
                changes[code] = new_amount - amount
                self.data["holdings"][code] = new_amount
            else:
                changes[code] = 0

        logger.info(
            f"NAV変動反映: {sum(changes.values()):+,.0f}円 "
            f"(合計 {self.total:,.0f}円)"
        )
        return changes

    def add_contribution(
        self, allocation: list[dict], amount: int = 23000
    ) -> None:
        """掛金を配分に従って追加する"""
        for alloc in allocation:
            code = alloc["code"]
            ratio = alloc["new_ratio"]
            add = round(amount * ratio)
            self.data.setdefault("holdings", {})[code] = (
                self.holdings.get(code, 0) + add
            )

        self.data.setdefault("history", []).append({
            "month": datetime.now().strftime("%Y-%m"),
            "event": "contribution",
            "amount": amount,
            "details": {
                a["code"]: round(amount * a["new_ratio"]) for a in allocation
            },
        })
        logger.info(f"掛金追加: {amount:,}円 → 合計 {self.total:,.0f}円")

    def apply_switching(self, switches: list[dict], to_code: str) -> None:
        """
        スイッチングを適用する。

        Args:
            switches: [{"code": "...", "name": "..."}] 売却対象
            to_code: スイッチング先の商品コード
        """
        for sw in switches:
            from_code = sw["code"]
            amount = self.holdings.get(from_code, 0)
            if amount <= 0:
                continue

            self.data["holdings"][from_code] = 0
            self.data.setdefault("holdings", {})[to_code] = (
                self.holdings.get(to_code, 0) + amount
            )

            self.data.setdefault("history", []).append({
                "month": datetime.now().strftime("%Y-%m"),
                "event": "switching",
                "from": from_code,
                "to": to_code,
                "amount": amount,
            })
            logger.info(
                f"スイッチング: {sw['name']} → {to_code} ({amount:,.0f}円)"
            )

    def update_navs(self, current_navs: dict[str, float]) -> None:
        """現在のNAVを記録する（次回のNAV変動計算用）"""
        self.data["last_nav"] = {
            k: v for k, v in current_navs.items() if v is not None
        }
        self.data["last_updated"] = datetime.now().strftime("%Y-%m")

    def save(self) -> None:
        """holdings.json に保存する"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        logger.info(f"保有資産保存: {self.path}")
