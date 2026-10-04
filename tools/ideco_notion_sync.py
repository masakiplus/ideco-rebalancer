#!/usr/bin/env python3
"""
iDeCo 月次判定の結果を Notion DB「iDeCo資産推移」へ1行記録する。

ideco_rebalancer.py の実行後に run.sh から呼ばれる。判定ロジックには一切
関与せず、生成済みの data/holdings.json と output/ideco_report_YYYYMM.md を
読んで Notion のページを作るだけの後処理として切り出してある。

記録する列:
  日付 / 残高 / 当月拠出額 / 運用商品構成 / リバランス実施 / リバランス内容 / 備考

月次レポート md の全文（全商品スコア一覧・シグナル判定根拠・Core候補モニタリング
など）は、その行のページ本文へ Notion ブロックとして流し込む。表は Notion の
テーブルブロックに変換するのでそのまま読める。自動生成部分の先頭に目印ブロックを
置き、更新時はそれ以降を差し替える。手書きメモは目印より上に書けば残る。

使い方:
  python3 tools/ideco_notion_sync.py [--dry-run] [--append] [--month YYYY-MM]

オプション:
  --dry-run   Notionへ書き込まず、送信予定の内容だけを表示する
  --append    同月の行があっても更新せず新規行を追加する
              (既定は同月の行があれば更新する upsert。再実行で重複させないため)
  --month     対象月を明示指定する。既定は実行時点の年月
  --note      備考の先頭に一文を追記する。実残高同期などの経緯を残すとき用
  --no-body   ページ本文へのレポート流し込みを行わない

認証:
  NOTION_TOKEN を環境変数、または .env から読む。探索順は
    1. 環境変数
    2. tools/ideco-rebalancer/.env
    3. ~/.config/saito-tools/.env  (ツール共通の中央 .env。2026-10-04 に tools/notion_sync/.env から切り替え)
  DBのIDは環境変数 NOTION_IDECO_DB_ID があればそちらを優先する。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
CONFIG_PATH = PROJECT_DIR / "config" / "ideco_products.json"
HOLDINGS_PATH = PROJECT_DIR / "data" / "holdings.json"
OUTPUT_DIR = PROJECT_DIR / "output"

ENV_CANDIDATES = [
    PROJECT_DIR / ".env",
    Path.home() / ".config" / "saito-tools" / ".env",
]

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
DEFAULT_DB_ID = "13b386fd96e94737944aae5747044f52"

# Notionのrich_textは1ブロック2000文字まで。余裕を持たせて切る
MAX_RICH_TEXT = 1900


# --------------------------------------------------------------------------
# 認証・HTTP
# --------------------------------------------------------------------------

def load_env_value(key: str) -> str | None:
    value = os.environ.get(key)
    if value:
        return value.strip()
    for env_path in ENV_CANDIDATES:
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return None


def notion_request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{NOTION_API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Notion-Version", NOTION_VERSION)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Notion API {method} {path} 失敗 ({e.code}): {body}") from e


# --------------------------------------------------------------------------
# 入力データの読み取り
# --------------------------------------------------------------------------

def load_json(path: Path, default=None):
    if not path.exists():
        return default if default is not None else {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_name_map(config: dict) -> dict[str, str]:
    """商品コード → 商品名。設定に無いコードは後段でコードのまま扱う"""
    names = {p["code"]: p["name"] for p in config.get("products", [])}
    guarantee = config.get("capital_guarantee_fund", {})
    if guarantee.get("code"):
        names[guarantee["code"]] = guarantee.get("name", guarantee["code"])
    for code, info in config.get("_non_monitored_holdings", {}).items():
        if code.startswith("_"):
            continue
        names.setdefault(code, info.get("name", code))
    return names


def format_composition(holdings: dict, names: dict[str, str]) -> tuple[str, float]:
    """運用商品構成の文字列と残高合計を返す"""
    items = [(code, amount) for code, amount in holdings.items() if amount and amount > 0]
    total = sum(amount for _, amount in items)
    items.sort(key=lambda x: x[1], reverse=True)

    lines = []
    for code, amount in items:
        ratio = amount / total * 100 if total else 0
        lines.append(f"{names.get(code, code)} {ratio:.1f}% ({amount:,.0f}円)")
    return "\n".join(lines), total


def collect_month_events(
    holdings_data: dict, month: str
) -> tuple[int, list[dict], list[dict], list[dict]]:
    """対象月の 拠出額合計・拠出・スイッチング・掛金配分変更 を取り出す"""
    history = [h for h in holdings_data.get("history", []) if h.get("month") == month]
    contributions = [h for h in history if h.get("event") == "contribution"]
    switchings = [h for h in history if h.get("event") == "switching"]
    allocation_changes = [h for h in history if h.get("event") == "allocation_change"]
    total_contribution = sum(int(h.get("amount", 0)) for h in contributions)
    return total_contribution, contributions, switchings, allocation_changes


def format_rebalance_detail(
    contributions: list[dict], switchings: list[dict], names: dict[str, str],
    allocation_changes: list[dict] | None = None,
) -> str:
    """リバランス内容。スイッチングが無い月は掛金配分を残す"""
    parts = []
    for ac in allocation_changes or []:
        detail = "・".join(
            f"{names.get(code, code)} {ratio*100:.0f}%"
            for code, ratio in (ac.get("details") or {}).items()
        )
        parts.append(f"掛金配分変更: {detail}" + (f"（{ac['note']}）" if ac.get("note") else ""))
    for sw in switchings:
        parts.append(
            f"スイッチング: {names.get(sw.get('from'), sw.get('from'))} → "
            f"{names.get(sw.get('to'), sw.get('to'))} {sw.get('amount', 0):,.0f}円"
        )

    # 掛金の配分先。同月に複数回の拠出があるので商品ごとに合算する
    alloc: dict[str, int] = {}
    for c in contributions:
        for code, amount in (c.get("details") or {}).items():
            alloc[code] = alloc.get(code, 0) + int(amount)
    if alloc:
        total = sum(alloc.values())
        detail = "・".join(
            f"{names.get(code, code)} {amount / total * 100:.0f}% ({amount:,}円)"
            for code, amount in sorted(alloc.items(), key=lambda x: x[1], reverse=True)
        )
        parts.append(f"掛金配分: {detail}")

    if not switchings:
        parts.insert(0, "スイッチングなし")
    if allocation_changes:
        # 掛金配分変更があった月は先頭に出す
        parts.insert(0, parts.pop(next(i for i, x in enumerate(parts) if x.startswith("掛金配分変更"))))
    return "\n".join(parts) if parts else "対象月の記録なし"


def parse_report(report_path: Path) -> dict:
    """レポートmdから備考に載せる情報を拾う。無くても処理は続ける"""
    info: dict[str, str | bool] = {"exists": report_path.exists()}
    if not report_path.exists():
        return info

    text = report_path.read_text(encoding="utf-8")
    m = re.search(r"BUY候補:\s*\*\*(\d+)本\*\*", text)
    if m:
        info["buy_count"] = m.group(1)
    m = re.search(r"スイッチング:\s*(\d+)件（ケースA:\s*(\d+)件、ケースB:\s*(\d+)件）", text)
    if m:
        info["switch_summary"] = f"スイッチング{m.group(1)}件 (A:{m.group(2)}・B:{m.group(3)})"
    info["core_change_alert"] = "Core変更候補あり" in text
    m = re.search(r"現行Core:\s*\*\*(.+?)\*\*", text)
    if m:
        info["core_product"] = m.group(1)
    return info


def format_note(
    report_path: Path, report_info: dict, holdings_data: dict, month: str,
    extra: str | None = None,
) -> str:
    notes = []
    if extra:
        notes.append(extra)
    if report_info.get("buy_count"):
        notes.append(f"BUY候補{report_info['buy_count']}本")
    if report_info.get("switch_summary"):
        notes.append(str(report_info["switch_summary"]))
    if report_info.get("core_product"):
        notes.append(f"現行Core: {report_info['core_product']}")
    if report_info.get("core_change_alert"):
        notes.append("⚠ Core変更候補あり。config/ideco_products.json の CORE_PRODUCT を要検討")
    if holdings_data.get("last_updated") and holdings_data["last_updated"] != month:
        notes.append(
            f"※holdings.json の更新月は {holdings_data['last_updated']} "
            f"で対象月 {month} と不一致"
        )
    if report_info.get("exists"):
        notes.append(f"レポート: {report_path.relative_to(PROJECT_DIR)}")
    else:
        notes.append("※月次レポートmdが見つからず")
    return "\n".join(notes)


# --------------------------------------------------------------------------
# 月次レポート md → Notion ブロック
# --------------------------------------------------------------------------

# 目印の探索は BODY_MARKER_KEY で行う。文面を変えても過去の目印を見失わないよう、
# 探索キーは固定し、表示文面だけを BODY_MARKER 側で変える
BODY_MARKER_KEY = "ideco_notion_sync.py による自動生成"
BODY_MARKER = (
    f"🤖 ここから下は {BODY_MARKER_KEY}です。次回実行時にまるごと置き換わります。"
    "手書きのメモはこのブロックより上に書いてください。"
)


def inline_rich_text(text: str) -> list[dict]:
    """**強調** だけ解釈する。レポートが使う装飾はこれだけ"""
    out = []
    for i, part in enumerate(text.split("**")):
        if not part:
            continue
        out.append({
            "type": "text",
            "text": {"content": part[:MAX_RICH_TEXT]},
            "annotations": {"bold": i % 2 == 1},
        })
    return out or [{"type": "text", "text": {"content": ""}}]


def split_table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown_to_blocks(md: str) -> list[dict]:
    """レポートmdをNotionブロックへ変換する。見出し・表・引用・箇条書き・区切り線のみ"""
    lines = md.splitlines()
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 表: ヘッダ行 + 区切り行 + 本体
        if stripped.startswith("|") and i + 1 < len(lines) and set(
            lines[i + 1].strip().replace("|", "").replace(" ", "")
        ) <= {"-", ":"} and "-" in lines[i + 1]:
            header = split_table_row(stripped)
            rows = [header]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split_table_row(lines[i]))
                i += 1
            width = len(header)
            blocks.append({
                "object": "block",
                "type": "table",
                "table": {
                    "table_width": width,
                    "has_column_header": True,
                    "has_row_header": False,
                    "children": [{
                        "object": "block",
                        "type": "table_row",
                        "table_row": {
                            "cells": [
                                inline_rich_text(c) for c in (r + [""] * width)[:width]
                            ]
                        },
                    } for r in rows],
                },
            })
            continue

        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            body = stripped[level:].strip()
            kind = f"heading_{min(level, 3)}"
            blocks.append({
                "object": "block", "type": kind,
                kind: {"rich_text": inline_rich_text(body)},
            })
        elif stripped.startswith(("---", "___")) and set(stripped) <= {"-", "_"}:
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif stripped.startswith("> "):
            blocks.append({
                "object": "block", "type": "quote",
                "quote": {"rich_text": inline_rich_text(stripped[2:])},
            })
        elif stripped.startswith("- "):
            blocks.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": inline_rich_text(stripped[2:])},
            })
        else:
            blocks.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": inline_rich_text(stripped)},
            })
        i += 1
    return blocks


def replace_page_body(token: str, page_id: str, blocks: list[dict]) -> int:
    """目印ブロック以降を差し替える。目印より前の手書きメモは触らない"""
    results, cursor = [], None
    while True:
        path = f"/blocks/{page_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        page = notion_request("GET", path, token)
        results += page.get("results", [])
        if not page.get("has_more"):
            break
        cursor = page["next_cursor"]

    marker_seen = False
    for b in results:
        if not marker_seen:
            text = "".join(
                x.get("plain_text", "")
                for x in b.get(b["type"], {}).get("rich_text", [])
            )
            if BODY_MARKER_KEY not in text:
                continue
            marker_seen = True
        notion_request("PATCH", f"/blocks/{b['id']}", token, {"archived": True})

    payload = [{
        "object": "block", "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": BODY_MARKER}}],
            "color": "gray_background",
        },
    }] + blocks

    # 1リクエスト100ブロックまで
    for n in range(0, len(payload), 100):
        notion_request(
            "PATCH", f"/blocks/{page_id}/children", token,
            {"children": payload[n:n + 100]},
        )
    return len(payload)


# --------------------------------------------------------------------------
# Notion ページ組み立て
# --------------------------------------------------------------------------

def rich_text(value: str) -> list[dict]:
    if not value:
        return []
    return [{"type": "text", "text": {"content": value[:MAX_RICH_TEXT]}}]


def build_properties(record: dict) -> dict:
    return {
        "Name": {"title": rich_text(record["title"])},
        "日付": {"date": {"start": record["date"]}},
        "残高": {"number": record["balance"]},
        "当月拠出額": {"number": record["contribution"]},
        "運用商品構成": {"rich_text": rich_text(record["composition"])},
        "リバランス実施": {"checkbox": record["rebalanced"]},
        "リバランス内容": {"rich_text": rich_text(record["rebalance_detail"])},
        "備考": {"rich_text": rich_text(record["note"])},
    }


def find_existing_page(token: str, db_id: str, month: str) -> str | None:
    """同月の行を探す。再実行で重複行を作らないため"""
    year, mon = int(month[:4]), int(month[5:7])
    next_year, next_mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    payload = {
        "filter": {
            "and": [
                {"property": "日付", "date": {"on_or_after": f"{year:04d}-{mon:02d}-01"}},
                {"property": "日付", "date": {"before": f"{next_year:04d}-{next_mon:02d}-01"}},
            ]
        },
        "page_size": 1,
    }
    res = notion_request("POST", f"/databases/{db_id}/query", token, payload)
    results = res.get("results", [])
    return results[0]["id"] if results else None


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="iDeCo判定結果をNotionへ記録")
    parser.add_argument("--dry-run", action="store_true", help="Notionへ書き込まず内容を表示")
    parser.add_argument("--append", action="store_true", help="同月の行があっても新規追加する")
    parser.add_argument("--month", default=None, help="対象月 YYYY-MM。既定は今月")
    parser.add_argument("--note", default=None, help="備考の先頭に追記する一文")
    parser.add_argument("--no-body", action="store_true", help="ページ本文へのレポート流し込みを省略")
    args = parser.parse_args()

    now = datetime.now()
    month = args.month or now.strftime("%Y-%m")
    if not re.match(r"^\d{4}-\d{2}$", month):
        logger.error(f"--month の形式が不正: {month}")
        return 1

    holdings_data = load_json(HOLDINGS_PATH)
    if not holdings_data.get("holdings"):
        logger.error(f"保有データが見つかりません: {HOLDINGS_PATH}")
        return 1

    config = load_json(CONFIG_PATH)
    names = build_name_map(config)

    composition, balance = format_composition(holdings_data["holdings"], names)
    contribution, contributions, switchings, allocation_changes = collect_month_events(
        holdings_data, month
    )
    report_path = OUTPUT_DIR / f"ideco_report_{month.replace('-', '')}.md"
    report_info = parse_report(report_path)

    record = {
        "title": f"{month[:4]}年{month[5:7]}月",
        "date": now.strftime("%Y-%m-%d") if month == now.strftime("%Y-%m") else f"{month}-01",
        "balance": round(balance),
        "contribution": contribution,
        "composition": composition,
        "rebalanced": bool(switchings or allocation_changes),
        "rebalance_detail": format_rebalance_detail(
            contributions, switchings, names, allocation_changes
        ),
        "note": format_note(report_path, report_info, holdings_data, month, args.note),
    }

    if args.dry_run:
        print(json.dumps(record, ensure_ascii=False, indent=2))
        logger.info("--dry-run のため Notion へは書き込みません")
        return 0

    token = load_env_value("NOTION_TOKEN")
    if not token:
        logger.error(
            "NOTION_TOKEN が見つかりません。環境変数か "
            f"{' / '.join(str(p) for p in ENV_CANDIDATES)} に設定してください"
        )
        return 1
    db_id = load_env_value("NOTION_IDECO_DB_ID") or DEFAULT_DB_ID

    properties = build_properties(record)
    existing = None if args.append else find_existing_page(token, db_id, month)

    if existing:
        res = notion_request("PATCH", f"/pages/{existing}", token, {"properties": properties})
        logger.info(f"Notion更新: {month} 残高{record['balance']:,}円 → {res.get('url')}")
    else:
        res = notion_request(
            "POST", "/pages", token,
            {"parent": {"database_id": db_id}, "properties": properties},
        )
        logger.info(f"Notion追記: {month} 残高{record['balance']:,}円 → {res.get('url')}")

    if not args.no_body:
        if report_path.exists():
            n = replace_page_body(
                token, res["id"],
                markdown_to_blocks(report_path.read_text(encoding="utf-8")),
            )
            logger.info(f"レポート本文を反映: {n}ブロック ({report_path.name})")
        else:
            logger.warning(f"レポートmdが無いため本文はスキップ: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
