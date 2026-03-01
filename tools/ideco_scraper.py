"""
楽天証券 iDeCo 商品ページ スクレイパー

対象URL:
  https://www.rakuten-sec.co.jp/web/fund/detail/401k.html?ID={ISIN}&401k_no=

取得データ:
  - 商品名 (h1)
  - 基準価額 (テーブル#1 row0 col1)
  - 管理費用/信託報酬 (テーブル#6 row1 col0)
  - 6ヶ月・1年リターン（期間）(パフォーマンステーブル)
  - 月次リターン履歴 (テーブル#11+)

1M・3Mリターン計算:
  月次リターン履歴の直近月から複利計算で求める。

MA12計算サポート:
  月次リターン履歴と現在NAVから過去12ヶ月のNAVを逆算して提供。
  ideco_scorer.py の nav_history に月次NAVを蓄積することでMA12精度を向上する。
"""

import logging
import re
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://www.rakuten-sec.co.jp/web/fund/detail/401k.html?ID={code}&401k_no="

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
}

REQUEST_INTERVAL_SECONDS = 2
MAX_RETRIES = 3
RETRY_INTERVAL_SECONDS = 5


class RakutenIdecoScraper:
    """楽天証券iDeCo商品ページから基準価額・リターン・信託報酬を取得するスクレイパー"""

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._playwright_ctx = None

    # ----------------------------------------------------------------
    # 公開API
    # ----------------------------------------------------------------

    def fetch_product(self, product_code: str) -> Optional[dict]:
        """
        商品コード（ISIN）を受け取り、価格・リターンデータを返す。
        まずrequests+BS4を試行し、失敗時はPlaywrightにフォールバック。
        """
        url = BASE_URL.format(code=product_code)
        logger.info(f"取得開始: {product_code}")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # まず requests+BS4 で試行（軽量・asyncio互換）
                data = self._fetch_with_requests(url, product_code)
                if data and data.get("nav") is not None:
                    return data
                # BS4で取れなければPlaywrightにフォールバック
                logger.info(f"BS4でNAV取得失敗、Playwright試行 [{product_code}]")
                data = self._fetch_with_playwright(url, product_code)
                if data and data.get("nav") is not None:
                    return data
                logger.warning(f"NAV取得失敗 [{product_code}] 試行{attempt}/{MAX_RETRIES}")
            except Exception as e:
                logger.warning(f"エラー [{product_code}] 試行{attempt}/{MAX_RETRIES}: {e}")

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_INTERVAL_SECONDS)

        logger.error(f"取得失敗（最大リトライ到達）: {product_code}")
        return None

    def fetch_all(self, product_codes: list) -> list:
        """
        複数商品を順次取得する（サーバー負荷軽減のためインターバル付き）。
        取得失敗した商品はスキップされる。
        """
        results = []
        try:
            for i, code in enumerate(product_codes):
                data = self.fetch_product(code)
                if data:
                    results.append(data)
                    logger.info(f"  取得OK: {data['name'][:30]} | NAV={data.get('nav')}")
                else:
                    logger.warning(f"  取得NG: {code}")
                if i < len(product_codes) - 1:
                    time.sleep(REQUEST_INTERVAL_SECONDS)
        finally:
            self._close_playwright()

        logger.info(f"取得完了: {len(results)}/{len(product_codes)} 商品")
        return results

    # ----------------------------------------------------------------
    # requests + BeautifulSoup による取得（軽量・asyncio互換）
    # ----------------------------------------------------------------

    def _fetch_with_requests(self, url: str, product_code: str) -> Optional[dict]:
        """requests + BeautifulSoup でデータ取得を試みる"""
        try:
            import requests
            from bs4 import BeautifulSoup
        except ImportError:
            logger.debug("requests/bs4 未インストール、スキップ")
            return None

        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            soup = BeautifulSoup(resp.text, "lxml")
            return self._parse_soup(soup, product_code)
        except Exception as e:
            logger.debug(f"requests取得失敗 [{product_code}]: {e}")
            return None

    def _parse_soup(self, soup, product_code: str) -> dict:
        """BeautifulSoupオブジェクトからデータを抽出する"""
        # 商品名
        h1 = soup.find("h1")
        name = h1.get_text(strip=True) if h1 else product_code

        # テーブルを全取得（Playwright版と同じ構造に変換）
        table_data = []
        for tbl in soup.find_all("table"):
            rows = []
            for tr in tbl.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                rows.append(cells)
            table_data.append(rows)

        nav = self._extract_nav(table_data)
        expense_ratio = self._extract_expense_ratio(table_data)
        return_6m, return_1y = self._extract_period_returns(table_data)
        monthly_returns = self._extract_monthly_returns(table_data)
        return_1m = self._calc_compound_return(monthly_returns, 1)
        return_3m = self._calc_compound_return(monthly_returns, 3)

        if nav is None:
            logger.debug(f"BS4: NAV取得失敗 {product_code} (テーブル数={len(table_data)})")

        return {
            "code": product_code,
            "name": name,
            "nav": nav,
            "expense_ratio": expense_ratio,
            "return_1m": return_1m,
            "return_3m": return_3m,
            "return_6m": return_6m,
            "return_1y": return_1y,
            "monthly_returns": monthly_returns,
            "fetched_at": datetime.now().isoformat(),
        }

    # ----------------------------------------------------------------
    # Playwright による取得
    # ----------------------------------------------------------------

    def _ensure_playwright(self):
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
            self._playwright_ctx = sync_playwright().__enter__()
            self._browser = self._playwright_ctx.chromium.launch(headless=True)
            logger.info("Playwright(Chromium)起動")
        except Exception as e:
            logger.error(f"Playwright起動失敗: {e}")
            raise

    def _close_playwright(self):
        if self._browser:
            try:
                self._browser.close()
                self._playwright_ctx.__exit__(None, None, None)
            except Exception:
                pass
            self._browser = None

    def _fetch_with_playwright(self, url: str, product_code: str) -> Optional[dict]:
        self._ensure_playwright()

        page = self._browser.new_page()
        try:
            page.set_extra_http_headers(HEADERS)
            response = page.goto(url, wait_until="domcontentloaded", timeout=25000)

            if response and response.status >= 400:
                logger.warning(f"HTTP {response.status} [{product_code}]")
                return None

            # JS描画待ち
            time.sleep(2)

            return self._parse_page(page, product_code)

        finally:
            page.close()

    # ----------------------------------------------------------------
    # ページパース
    # ----------------------------------------------------------------

    def _parse_page(self, page, product_code: str) -> dict:
        """ページ内容をパースして商品データを抽出する"""

        # 商品名
        name = product_code
        h1 = page.query_selector("h1")
        if h1:
            name = h1.inner_text().strip() or product_code

        # テーブルを全取得
        tables = page.query_selector_all("table")
        table_data = []
        for tbl in tables:
            rows = tbl.query_selector_all("tr")
            table_rows = []
            for row in rows:
                cells = row.query_selector_all("td, th")
                table_rows.append([c.inner_text().strip() for c in cells])
            table_data.append(table_rows)

        # 各データを抽出
        nav = self._extract_nav(table_data)
        expense_ratio = self._extract_expense_ratio(table_data)
        return_6m, return_1y = self._extract_period_returns(table_data)
        monthly_returns = self._extract_monthly_returns(table_data)
        return_1m = self._calc_compound_return(monthly_returns, 1)
        return_3m = self._calc_compound_return(monthly_returns, 3)

        # NAVが取れない場合はログして返す（returnは部分的でもOK）
        if nav is None:
            logger.warning(f"NAV取得失敗: {product_code}")

        result = {
            "code": product_code,
            "name": name,
            "nav": nav,
            "expense_ratio": expense_ratio,
            "return_1m": return_1m,
            "return_3m": return_3m,
            "return_6m": return_6m,
            "return_1y": return_1y,
            "monthly_returns": monthly_returns,
            "fetched_at": datetime.now().isoformat(),
        }

        logger.debug(
            f"パース完了: {product_code} | NAV={nav} | 1M={return_1m} | "
            f"3M={return_3m} | 6M={return_6m} | 1Y={return_1y} | 信託報酬={expense_ratio}"
        )
        return result

    # ----------------------------------------------------------------
    # データ抽出メソッド
    # ----------------------------------------------------------------

    def _extract_nav(self, table_data: list) -> Optional[float]:
        """
        基準価額を抽出する。
        「基準価額 | 33,013 円 （2/27）」の形式のセルを探す。
        """
        for rows in table_data:
            for row in rows:
                if len(row) >= 2 and "基準価額" in row[0]:
                    nav_text = row[1]
                    nav = self._parse_price(nav_text)
                    if nav:
                        return nav
        return None

    def _extract_expense_ratio(self, table_data: list) -> float:
        """
        管理費用（信託報酬）を抽出する（%単位で返す）。
        「管理費用（含む信託報酬） | 0.179％」の形式を探す。
        また「運用管理費用（信託報酬）（税込） | 0.179%」も探す。
        """
        keywords = ["管理費用", "信託報酬"]
        for rows in table_data:
            for i, row in enumerate(rows):
                for kw in keywords:
                    if len(row) >= 1 and kw in row[0]:
                        # 次の行または同じ行の値セルを探す
                        for search_rows in [row, rows[i + 1] if i + 1 < len(rows) else []]:
                            for cell in search_rows:
                                ratio = self._parse_percent_value(cell)
                                if ratio is not None and 0 < ratio < 5:
                                    return ratio
        return 0.0

    def _extract_period_returns(self, table_data: list) -> tuple:
        """
        パフォーマンステーブルから6ヶ月・1年のリターン（期間）を抽出する。

        テーブル構造:
          ['パフォーマンス', '6ヵ月', '1年', '3年', '5年']
          ['リターン(年率）', '39.73', '30.80', ...]
          ['リターン（期間）', '18.21', '31.41', ...]  ← この行を使う

        Returns:
            (return_6m, return_1y) - いずれか取得できなければNone
        """
        for rows in table_data:
            # ヘッダー行を探す
            header_row = None
            for i, row in enumerate(rows):
                if len(row) >= 3 and "パフォーマンス" in row[0] and ("6ヵ月" in row[1] or "6か月" in row[1]):
                    header_row = row
                    # リターン（期間）行を探す
                    for data_row in rows[i + 1:]:
                        if len(data_row) >= 3 and "リターン（期間）" in data_row[0]:
                            r6m = self._parse_float(data_row[1])
                            r1y = self._parse_float(data_row[2]) if len(data_row) > 2 else None
                            return r6m, r1y
        return None, None

    def _extract_monthly_returns(self, table_data: list) -> list:
        """
        月次リターン履歴を抽出して時系列順（新→旧）のリストで返す。

        テーブル構造（複数のテーブルに分かれている）:
          ['', 'リターン']
          ['2026年01月', '1.41%']
          ['2025年12月', '1.95%']
          ...

        Returns:
            [{"year_month": "2026-01", "return_pct": 1.41}, ...]  新→旧順
        """
        monthly = []
        year_month_pattern = re.compile(r"(\d{4})年(\d{1,2})月")

        for rows in table_data:
            for row in rows:
                if len(row) < 2:
                    continue
                m = year_month_pattern.match(row[0])
                if m:
                    year, month = m.group(1), m.group(2).zfill(2)
                    ret = self._parse_percent_value(row[1])
                    if ret is not None:
                        monthly.append({
                            "year_month": f"{year}-{month}",
                            "return_pct": ret,
                        })

        # 新→旧順でソート
        monthly.sort(key=lambda x: x["year_month"], reverse=True)
        return monthly

    def _calc_compound_return(self, monthly_returns: list, months: int) -> Optional[float]:
        """
        最新N ヶ月の月次リターンから複利累積リターン（%）を計算する。

        Args:
            monthly_returns: [{"year_month": "...", "return_pct": ...}, ...] 新→旧順
            months: 計算する月数

        Returns:
            累積リターン（%）。データ不足の場合はNone。
        """
        recent = monthly_returns[:months]
        if len(recent) < months:
            return None

        compound = 1.0
        for entry in recent:
            compound *= 1 + entry["return_pct"] / 100

        return round((compound - 1) * 100, 4)

    # ----------------------------------------------------------------
    # ユーティリティ
    # ----------------------------------------------------------------

    def _parse_price(self, text: str) -> Optional[float]:
        """「33,013 円 （2/27）」などから数値を抽出する"""
        if not text:
            return None
        # 最初に出てくる数字列（カンマ区切り）を取得
        m = re.search(r"([\d,]+)\s*円", text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        return None

    def _parse_percent_value(self, text: str) -> Optional[float]:
        """
        「0.179％」「+18.21%」「-0.49%」「18.21」などから数値を返す（%単位）。
        """
        if not text or text in ("---", "－", ""):
            return None
        normalized = text.replace("％", "%").replace("－", "-").replace("▲", "-").strip()
        m = re.search(r"([+\-]?\d+\.?\d*)\s*%", normalized)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        # % がない場合はそのまま数値として解釈（パフォーマンステーブルの値）
        try:
            val = float(normalized.replace(",", ""))
            if -100 < val < 100:  # 合理的な範囲のみ
                return val
        except ValueError:
            pass
        return None

    def _parse_float(self, text: str) -> Optional[float]:
        """「18.21」「---」などを float に変換する"""
        if not text or text in ("---", "－", ""):
            return None
        try:
            return float(text.replace(",", ""))
        except ValueError:
            return None
