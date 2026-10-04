#!/usr/bin/env python3
"""
data/holdings.json を楽天証券iDeCoの実残高に合わせる。

holdings.json は「レポートの指示どおりに楽天証券側を操作した前提」でNAV変動と
掛金を積み上げる。サイトの掛金配分を変えなかった月やスイッチング指示を実行
しなかった月があると記録と実残高がずれるため、定期的にこのスクリプトで突き
合わせる。2026-08-22 の初回突合を再現可能にしたもの。

使い方:
  # 1. 楽天証券 iDeCoトップ → 保有商品の確認・入替 の表を写した JSON を作る
  python3 tools/ideco_sync_holdings.py --template > /tmp/actual.json

  # 2. 評価額と基準価額を埋めてから差分を確認する
  python3 tools/ideco_sync_holdings.py --input /tmp/actual.json --dry-run

  # 3. 問題なければ反映し、Notionの当月行も更新する
  python3 tools/ideco_sync_holdings.py --input /tmp/actual.json --notion

入力JSONの形式（評価額は必須、基準価額は省略可）:
  {
    "JP90C000FHD2": {"amount": 1000000, "nav": 45484},
    "JP90C000CMK4": {"amount": 500000}
  }

保有していない商品は書かない。書いた商品だけが保有として残る（0円は除外）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import unicodedata
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
CONFIG_PATH = PROJECT_DIR / "config" / "ideco_products.json"
HOLDINGS_PATH = PROJECT_DIR / "data" / "holdings.json"


def pad(text: str, width: int) -> str:
    """全角を2桁として左詰めする。差分表の桁を揃えるため"""
    w = sum(2 if unicodedata.east_asian_width(c) in "WFA" else 1 for c in text)
    while w > width:
        text = text[:-1]
        w = sum(2 if unicodedata.east_asian_width(c) in "WFA" else 1 for c in text)
    return text + " " * (width - w)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def name_map() -> dict[str, str]:
    config = load_json(CONFIG_PATH)
    names = {p["code"]: p["name"] for p in config.get("products", [])}
    for code, info in config.get("_non_monitored_holdings", {}).items():
        if not code.startswith("_"):
            names.setdefault(code, f"{info.get('name', code)}（監視対象外）")
    return names


def print_template() -> None:
    """保有しうる商品を並べた雛形を出す。不要な行は消して使う"""
    names = name_map()
    current = load_json(HOLDINGS_PATH)
    holdings = current.get("holdings", {})
    navs = current.get("last_nav", {})
    body = {
        code: {"amount": int(holdings.get(code, 0)), "nav": int(navs.get(code, 0))}
        for code in names
        if code != "GUARANTEE"
    }
    print(json.dumps(body, ensure_ascii=False, indent=2))
    print(
        "\n// 上は現在の記録値。楽天証券トップの 評価額 → amount、基準価額 → nav に\n"
        "// 書き換え、保有していない商品の行は消すこと",
        file=sys.stderr,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="実残高との突合")
    parser.add_argument("--input", type=Path, help="実残高JSONのパス")
    parser.add_argument("--template", action="store_true", help="入力JSONの雛形を出力")
    parser.add_argument("--dry-run", action="store_true", help="差分表示のみ。保存しない")
    parser.add_argument("--notion", action="store_true", help="反映後にNotionの当月行も更新")
    parser.add_argument("--note", default=None, help="履歴に残す補足")
    args = parser.parse_args()

    if args.template:
        print_template()
        return 0
    if not args.input:
        parser.error("--input か --template のどちらかが要る")

    actual = load_json(args.input)
    if not actual:
        print(f"入力が空: {args.input}", file=sys.stderr)
        return 1

    amounts, navs = {}, {}
    for code, v in actual.items():
        if isinstance(v, dict):
            amount, nav = v.get("amount", 0), v.get("nav")
        else:  # {"CODE": 123} の短縮形も受ける
            amount, nav = v, None
        if amount and amount > 0:
            amounts[code] = int(amount)
        if nav:
            navs[code] = float(nav)

    if not amounts:
        print("評価額が1件も無い", file=sys.stderr)
        return 1

    data = load_json(HOLDINGS_PATH)
    before = data.get("holdings", {})
    names = name_map()

    # 差分表示
    print(f"{pad('商品', 48)} {'記録':>12} {'実残高':>12} {'差分':>12}")
    print("-" * 86)
    for code in sorted(set(before) | set(amounts), key=lambda c: -amounts.get(c, 0)):
        b, a = before.get(code, 0), amounts.get(code, 0)
        mark = "" if b == a else "  ←"
        print(f"{pad(names.get(code, code), 48)} {b:>12,} {a:>12,} {a - b:>+12,}{mark}")
    print("-" * 86)
    tb, ta = sum(before.values()), sum(amounts.values())
    print(f"{pad('合計', 48)} {tb:>12,} {ta:>12,} {ta - tb:>+12,}")

    if args.dry_run:
        print("\n--dry-run のため保存しません")
        return 0
    if tb == ta and before == amounts:
        print("\nずれ無し。保存をスキップします")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if HOLDINGS_PATH.exists():
        backup = HOLDINGS_PATH.with_name(f"{HOLDINGS_PATH.name}.bak_{stamp}")
        shutil.copy2(HOLDINGS_PATH, backup)
        print(f"\nバックアップ: {backup.name}")

    month = datetime.now().strftime("%Y-%m")
    data["holdings"] = amounts
    data["last_nav"] = {**data.get("last_nav", {}), **navs}
    data["last_updated"] = month
    data.setdefault("history", []).append({
        "month": month,
        "event": "sync",
        "amount": ta,
        "details": amounts,
        "note": args.note or "楽天証券iDeCoトップの実残高に同期",
    })
    HOLDINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"保存: {HOLDINGS_PATH} (合計 {ta:,}円)")

    if args.notion:
        cmd = [sys.executable, str(SCRIPT_DIR / "ideco_notion_sync.py")]
        if args.note:
            cmd += ["--note", args.note]
        return subprocess.call(cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
