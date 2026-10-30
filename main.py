# -*- coding: utf-8 -*-
"""
全台股量化選股與防衛戰報（整合版）
整合 stock_runner.py（四象限大盤 + 選股）與 ptt_thermometer.py（PTT 溫度計），
並串接 Google Sheets 庫存，依 PRD 輸出順序：
  1.【大盤四象限診斷】 2.【🛡️ 現有庫存健檢】 3.【🏆 明日新標的推薦】 4.【🚨 主力倒貨警戒區】

升級：動態全市場股票池 + 真實三大法人籌碼（TWSE T86 / TPEx）。

必要環境變數：
  LINE_TOKEN                    LINE Messaging API Channel Access Token
  LINE_USER_ID                  (選填) 有填 → Push；沒填 → Broadcast
  GOOGLE_SERVICE_ACCOUNT_JSON   服務帳戶金鑰 JSON 全文（或改用 GOOGLE_APPLICATION_CREDENTIALS 指向檔案路徑）
  SHEET_WORKSHEET               (選填) 工作表名稱，預設第一個分頁

注意：試算表需「共用」給服務帳戶的 client_email（檢視者即可）。
安裝：pip install requests beautifulsoup4 pytz yfinance pandas numpy gspread google-auth
"""

import os
import re
import csv
import json
import time
import datetime
from io import StringIO

import pytz
import requests
import numpy as np  # noqa: F401  (保留與原腳本相容)
import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup

# ======================= 設定區 =======================
LINE_TOKEN = os.environ.get("LINE_TOKEN", "").strip()
LINE_USER_ID = os.environ.get("LINE_USER_ID", "").strip()

SHEET_ID = "1e7cSItMfq6bOai1BEwBWic0gLVGX9QTKzvoomjyEOBM"
SHEET_WORKSHEET = os.environ.get("SHEET_WORKSHEET", "").strip()

MAX_HOLDINGS = 5          # 庫存達此檔數 → 停止推播新標的
MAX_NEW_PICKS = 6         # 新標的推播絕對上限
QUOTA_MAP = {"GREEN": 10, "BLUE": 6, "YELLOW": 1, "RED": 0}  # 燈號配額（再與 MAX_NEW_PICKS 取小）

POS_WORDS = ['噴', '多', '賺', '目標價', '買', '加碼', '歐印', 'all in', '財富自由',
             '舒服', '漲停', '進場', '抄底', '紅']
NEG_WORDS = ['空', '跌停', '崩', '救命', '停損', '畢業', '韭菜', '塊陶', '慘', '綠',
             '出清', '快逃', '丸子', '套牢']

STATUS_HOT = "🔥極度過熱 (當心出貨)"
STATUS_PANIC = "🧊恐慌絕望 (散戶人踩人)"
STATUS_WARM = "🍗溫火加熱 (多空交戰)"
STATUS_NONE = "😶無明顯討論"

HOT_SECTORS = ["23", "24", "30", "32", "35", "37", "49", "54", "61", "62",
               "64", "65", "66", "80", "82"]

# 櫃買中心法人報表欄位索引（若官方改版請在此調整）
TPEX_IDX_FOREIGN = 4    # 外資買賣超(股)
TPEX_IDX_IT = 10        # 投信買賣超(股)

TZ = pytz.timezone("Asia/Taipei")
SEP = "=" * 22
# ======================================================


# ====================== 工具函式 ======================
def to_float(v, default=None):
    """容錯轉數字：支援 '1,234.5'、'NT$950' 等格式"""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.\-]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return default


def normalize_code(v):
    """代號正規化：去空白；純數字且不足 4 碼者補零（Sheets 會吃掉 0050 的開頭 0）"""
    s = str(v).strip()
    if s.isdigit() and len(s) < 4:
        s = s.zfill(4)
    return s


def download_daily(sym, days=65):
    """下載日 K，並攤平 MultiIndex 欄位"""
    end_date = datetime.date.today() + datetime.timedelta(days=1)
    start_date = datetime.date.today() - datetime.timedelta(days=days)
    df = yf.download(sym, start=start_date, end=end_date, progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def resolve_df(code):
    """依代號嘗試 .TW（上市）→ .TWO（上櫃），回傳 (df, ticker)"""
    for suffix in (".TW", ".TWO"):
        sym = f"{code}{suffix}"
        try:
            df = download_daily(sym, days=30)
            if df is not None and len(df) >= 2:
                return df, sym
        except Exception:
            continue
    return None, None


def get_all_tickers():
    """從開放資料自動獲取全台股上市/上櫃代號，轉換為 yfinance 格式"""
    print("⏳ 正在建立全台股監控池...")
    targets = []
    try:
        # 上市
        res_l = requests.get("https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv", timeout=10)
        res_l.encoding = 'utf-8'
        for row in csv.reader(StringIO(res_l.text)):
            if len(row) >= 4 and row[1].strip().isdigit():
                targets.append(f"{row[1].strip()}.TW")
        # 上櫃
        res_o = requests.get("https://mopsfin.twse.com.tw/opendata/t187ap03_O.csv", timeout=10)
        res_o.encoding = 'utf-8'
        for row in csv.reader(StringIO(res_o.text)):
            if len(row) >= 4 and row[1].strip().isdigit():
                targets.append(f"{row[1].strip()}.TWO")
    except Exception as e:
        print(f"⚠️ 股票池建立失敗: {e}")
    return targets


def _to_lots(s):
    """'1,234,000' 股 → 1234 張（容錯：空值/破折號視為 0）"""
    s = str(s).replace(",", "").strip()
    try:
        return int(float(s)) // 1000
    except ValueError:
        return 0


def fetch_institutional_summary():
    """
    一次性抓取全市場上市與上櫃的「最近一個交易日」三大法人買賣超。
    回傳字典格式： { '2330': {'foreign_buy': 1500, 'it_buy': 500}, ... }
    """
    print("📊 正在抓取全市場三大法人籌碼數據...")
    inst_data = {}

    # 往回測試最多 5 天，找到最近有開盤的資料
    for days_back in range(5):
        target_date = datetime.datetime.now(TZ) - datetime.timedelta(days=days_back)
        date_twse = target_date.strftime("%Y%m%d")      # 證交所格式：20231030
        date_tpex = f"{target_date.year - 1911}/{target_date.strftime('%m/%d')}"  # 櫃買格式：112/10/30

        has_data = False

        # 1. 抓取上市 (TWSE) T86
        try:
            url = f"https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date_twse}&selectType=ALL"
            res = requests.get(url, timeout=10).json()
            if res.get("stat") == "OK" and res.get("data"):
                fields = res.get("fields", [])
                # 動態尋找欄位索引，避免證交所改版
                idx_code = fields.index("證券代號")
                # 外資欄位名稱可能為「外陸資買賣超股數(不含外資自營商)」或「外資及陸資買賣超股數」
                idx_foreign = next(
                    i for i, f in enumerate(fields)
                    if ("外陸資買賣超股數" in f or "外資及陸資買賣超股數" in f) and "自營商" not in f.replace("不含外資自營商", "")
                )
                idx_it = next(i for i, f in enumerate(fields) if "投信買賣超股數" in f)

                for row in res["data"]:
                    code = row[idx_code].strip()
                    # 證交所回傳的字串包含逗號，需清除後轉整數，再換算為「張」
                    inst_data[code] = {
                        'foreign_buy': _to_lots(row[idx_foreign]),
                        'it_buy': _to_lots(row[idx_it]),
                    }
                has_data = True
        except Exception as e:
            print(f"⚠️ TWSE 法人資料 {date_twse} 取得失敗：{e}")

        # 2. 抓取上櫃 (TPEx)
        try:
            url = (f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
                   f"?l=zh-tw&o=json&d={date_tpex}&se=EW")
            res = requests.get(url, timeout=10).json()
            if res.get("aaData"):
                for row in res["aaData"]:
                    code = row[0].strip()
                    inst_data[code] = {
                        'foreign_buy': _to_lots(row[TPEX_IDX_FOREIGN]),
                        'it_buy': _to_lots(row[TPEX_IDX_IT]),
                    }
                has_data = True
        except Exception as e:
            print(f"⚠️ TPEx 法人資料 {date_tpex} 取得失敗：{e}")

        if has_data:
            print(f"✅ 成功抓取 {target_date.strftime('%Y-%m-%d')} 法人籌碼資料（{len(inst_data)} 檔）。")
            break

    if not inst_data:
        print("⚠️ 近 5 天皆無法取得法人籌碼資料。")
    return inst_data


# ====================== 股票名稱 ======================
def get_stock_names():
    """抓取上市櫃官方清單，建立 { '2330': '台積電' } 字典"""
    stock_dict = {}
    urls = [
        "https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv",
        "https://mopsfin.twse.com.tw/opendata/t187ap03_O.csv",
    ]
    for url in urls:
        try:
            res = requests.get(url, timeout=10)
            res.encoding = "utf-8"
            reader = csv.reader(StringIO(res.text))
            next(reader)
            for row in reader:
                if len(row) >= 4:
                    stock_dict[row[1].strip()] = row[3].strip()
        except Exception:
            continue
    return stock_dict


# ====================== PTT 溫度計 ======================
def classify_ptt(pos, neg, count):
    """沿用原溫度計門檻"""
    if count == 0:
        return STATUS_NONE
    if pos > neg * 2 and pos > 10:
        return STATUS_HOT
    if neg > pos * 2 and neg > 10:
        return STATUS_PANIC
    return STATUS_WARM


def get_ptt_sentiment(stock_codes, stock_names=None, pages=15):
    """
    掃描 PTT 股版過去 24 小時，回傳指定股票的聲量與多空狀態。
    回傳：{ '2330': {'count': 3, 'pos': 12, 'neg': 4, 'status': '🍗溫火加熱 (多空交戰)'}, ... }
    （未被討論的代號也會回傳 count=0、status=STATUS_NONE）
    """
    targets = {normalize_code(c) for c in stock_codes}
    stats = {c: {"count": 0, "pos": 0, "neg": 0} for c in targets}
    if not targets:
        return {}

    now = datetime.datetime.now(TZ)
    start_time = now - datetime.timedelta(days=1)

    session = requests.Session()
    session.cookies.set("over18", "1")
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    current_url = "https://www.ptt.cc/bbs/Stock/index.html"
    print("🔥 開始掃描 PTT 股版...")

    for _ in range(pages):
        try:
            res = session.get(current_url, timeout=10)
            soup = BeautifulSoup(res.text, "html.parser")
            articles = soup.find_all("div", class_="r-ent")
        except Exception:
            break

        for article in articles:
            a_tag = article.find("a")
            if "標題已被刪除" in article.text or not a_tag:
                continue

            title = a_tag.text
            hit_codes = [c for c in re.findall(r"(?<!\d)\d{4}(?!\d)", title) if c in targets]
            if not hit_codes:
                continue

            link = "https://www.ptt.cc" + a_tag["href"]
            time.sleep(0.1)
            try:
                inner = session.get(link, timeout=5)
                inner_soup = BeautifulSoup(inner.text, "html.parser")
                meta_values = inner_soup.find_all("span", class_="article-meta-value")
                if len(meta_values) < 4:
                    continue
                article_time = TZ.localize(
                    datetime.datetime.strptime(meta_values[3].text, "%a %b %d %H:%M:%S %Y")
                )
                if not (start_time <= article_time <= now):
                    continue

                content = inner_soup.text.lower()
                pos_score = sum(content.count(w) for w in POS_WORDS)
                neg_score = sum(content.count(w) for w in NEG_WORDS)

                for code in set(hit_codes):
                    stats[code]["count"] += 1
                    stats[code]["pos"] += pos_score
                    stats[code]["neg"] += neg_score
            except Exception:
                continue

        paging_div = soup.find("div", class_="btn-group btn-group-paging")
        if paging_div and len(paging_div.find_all("a")) > 1 and paging_div.find_all("a")[1].get("href"):
            current_url = "https://www.ptt.cc" + paging_div.find_all("a")[1]["href"]
        else:
            break

    for code, d in stats.items():
        d["status"] = classify_ptt(d["pos"], d["neg"], d["count"])
    return stats


# ====================== Google Sheets 庫存 ======================
HEADER_ALIASES = {
    "code": ["代號", "股票代號", "代碼", "股號", "code", "symbol"],
    "name": ["名稱", "股票名稱", "股名", "name"],
    "cost": ["成本", "成本價", "買進成本", "平均成本", "cost"],
    "stop": ["原始停損價", "停損價", "停損", "原始停損", "stop"],
}


def _find_col(headers, key):
    norm = [h.strip().lower() for h in headers]
    for alias in HEADER_ALIASES[key]:
        if alias.lower() in norm:
            return norm.index(alias.lower())
    return None


def get_holdings():
    """
    讀取 Google Sheets「現有庫存」。
    回傳 list[dict]：[{'code','name','cost','stop'}, ...]；讀取失敗回傳 (None)。
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if raw:
            creds = Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
        else:
            path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
            if not path:
                raise RuntimeError("未設定 GOOGLE_SERVICE_ACCOUNT_JSON 或 GOOGLE_APPLICATION_CREDENTIALS")
            creds = Credentials.from_service_account_file(path, scopes=scopes)

        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SHEET_ID)
        ws = sh.worksheet(SHEET_WORKSHEET) if SHEET_WORKSHEET else sh.sheet1
        rows = ws.get_all_values()
        if not rows:
            return []

        headers = rows[0]
        idx = {k: _find_col(headers, k) for k in HEADER_ALIASES}
        if idx["code"] is None:
            raise RuntimeError(f"找不到『代號』欄位，現有標題：{headers}")

        holdings = []
        for r in rows[1:]:
            def cell(k):
                i = idx[k]
                return r[i] if i is not None and i < len(r) else ""

            code = normalize_code(cell("code"))
            if not code:
                continue
            holdings.append({
                "code": code,
                "name": cell("name").strip(),
                "cost": to_float(cell("cost")),
                "stop": to_float(cell("stop")),
            })
        return holdings
    except Exception as e:
        print(f"❌ Google Sheets 讀取失敗：{e}")
        return None


# ====================== 大盤四象限 ======================
def fetch_index_metrics(ticker):
    """下載指數日 K，計算收盤、漲跌幅、5/10/20MA 與均線狀態描述"""
    df = download_daily(ticker, days=65)
    if df is None or len(df) < 25:
        return None
    close = df["Close"].dropna()
    if len(close) < 22:
        return None

    c_close = float(close.iloc[-1])
    prev_close = float(close.iloc[-2])
    pct = round((c_close - prev_close) / prev_close * 100, 2)

    ma5 = float(close.rolling(5).mean().iloc[-1])
    ma10 = float(close.rolling(10).mean().iloc[-1])
    ma10_prev = float(close.rolling(10).mean().iloc[-2])
    ma20 = float(close.rolling(20).mean().iloc[-1])
    is_ma10_down = ma10 < ma10_prev

    if c_close >= ma5 and c_close >= ma10 and c_close >= ma20:
        status_str = "站穩 5/10/20MA 多頭攻擊"
    elif c_close >= ma10 and c_close >= ma20:
        status_str = "跌破 5MA 短線小歇，守穩 10MA/月線"
    elif c_close < ma10 and c_close >= ma20:
        status_str = "跌破 10MA 整理，月線(20MA)有守"
    else:
        status_str = "跌破月線(20MA) 結構轉弱"

    return {
        "close": c_close, "pct": pct,
        "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "is_ma10_down": is_ma10_down,
        "strong": (c_close >= ma10 and c_close >= ma20),
        "broken": ((c_close < ma10 and is_ma10_down) or (c_close < ma20)),
        "below_ma5": (c_close < ma5),
        "status_str": status_str,
    }


def get_market_stage():
    """【第 0 層】雙指數四象限總開關，回傳 (燈號, 診斷文字)"""
    try:
        tw = fetch_index_metrics("^TWII")
        otc = fetch_index_metrics("^TWOII")
        if otc is None:  # 櫃買指數偶發缺資料 → 以富櫃50 ETF 備援
            otc = fetch_index_metrics("006201.TWO")
        if tw is None or otc is None:
            return "GREEN", "⚠️ 指數歷史數據讀取異常，系統預設維持常規運作"

        tw_pct = f"{'+' if tw['pct'] > 0 else ''}{tw['pct']}%"
        otc_pct = f"{'+' if otc['pct'] > 0 else ''}{otc['pct']}%"
        summary = (
            f"📈 加權指數：{round(tw['close'], 1)} ({tw_pct})｜{tw['status_str']}\n"
            f"🚀 櫃買指數：{round(otc['close'], 2)} ({otc_pct})｜{otc['status_str']}"
        )

        if tw["broken"] and otc["broken"]:
            return "RED", (
                f"🔴【紅燈：覆巢之下｜雙指數破線修正段】\n{summary}\n"
                f"💡 系統決策：內外資同步撤退、系統性風險高！強制關閉個股推播（上限 0 檔），建議保留 100% 現金空手觀望！"
            )

        if (not tw["broken"] and otc["broken"]) or (tw["below_ma5"] and otc["below_ma5"]):
            sub = "拉積盤（權值獨秀、中小失血）" if otc["broken"] else "雙指數跌破 5MA 短線洗盤"
            return "YELLOW", (
                f"🟡【黃燈：防禦警戒｜{sub}】\n{summary}\n"
                f"💡 系統決策：嚴防中小型股假突破真出貨！啟動極限防禦門檻（量增≥2.5倍+土洋合買+光頭紅K），"
                f"最多只推播「1 檔」供觀察（建議資金 0%~30%）。"
            )

        if tw["close"] < tw["ma10"] and otc["strong"]:
            return "BLUE", (
                f"🔵【藍燈：內資作帳盤｜權值休兵、中小活絡】\n{summary}\n"
                f"💡 系統決策：外資調節大型權值股，但內資主力積極點火中小型股！不封殺上市強勢股，"
                f"並啟動「投信/上櫃作帳加分傾斜」，精選推播上限 6 檔（建議資金 40%~50%）。"
            )

        return "GREEN", (
            f"🟢【綠燈：雙頭馬車｜內外資齊攻段】\n{summary}\n"
            f"💡 系統決策：加權與櫃買多頭結構健康、內外資同步做多！維持常規波段選股，"
            f"推播上限 10 檔（建議資金 50%~75%）。"
        )
    except Exception as e:
        return "GREEN", f"⚠️ 大盤解析異常 ({e})，系統預設維持常規運作"


# ====================== 量化選股 ======================
def scan_candidates(market_stage, stock_names, exclude_codes=()):
    """回傳依總分排序的候選清單（套用真實三大法人數據）"""
    # 1. 取得全市場股票池
    all_targets = get_all_tickers()
    if not all_targets:
        print("⚠️ 股票池為空，請檢查網路連線。")
        return []

    # 2. 一次性取得全市場法人數據
    inst_data_map = fetch_institutional_summary()

    results = []
    print(f"🔍 開始掃描 {len(all_targets)} 檔股票，這可能需要幾分鐘...")

    for sym in all_targets:
        try:
            code = sym.split(".")[0]
            if code in exclude_codes:  # 已持有者不重複推薦
                continue

            # 查表獲取法人籌碼 (若查無資料則預設為 0 張)
            inst = inst_data_map.get(code, {'foreign_buy': 0, 'it_buy': 0})
            today_f_buy = inst['foreign_buy']
            today_it_buy = inst['it_buy']

            # 【初步過濾】：若無法人買盤 (投信沒買 且 外資沒買)，直接跳過，節省 YFinance 下載時間
            if today_f_buy <= 0 and today_it_buy <= 0:
                continue

            df = download_daily(sym, days=60)
            if df is None or len(df) < 25:
                continue
            close = df["Close"].dropna()
            if len(close) < 22:
                continue

            c_close = float(close.iloc[-1])
            prev_close = float(close.iloc[-2])
            is_otc = sym.endswith(".TWO")

            ma5 = float(close.rolling(5).mean().iloc[-1])
            ma10 = float(close.rolling(10).mean().iloc[-1])
            ma20 = float(close.rolling(20).mean().iloc[-1])

            if c_close < ma20:  # 技術防守過濾 (月線之下不玩)
                continue

            score = 40
            reasons = []

            if code[:2] in HOT_SECTORS:
                score += 15
                reasons.append("主流族群")

            # ✅ 採用真實法人數據判定
            is_co_buy = (today_f_buy > 0 and today_it_buy > 0)
            if is_co_buy:
                score += 10
                reasons.append(f"土洋合買(外+{today_f_buy}/投+{today_it_buy})")
            elif today_it_buy > 0:
                reasons.append(f"投信買超({today_it_buy}張)")

            if market_stage == "BLUE":
                bonus = 0
                if today_it_buy >= 200:  # 投信單日大買 200 張以上加分
                    bonus += 10
                if is_otc:
                    bonus += 5
                if bonus:
                    score += bonus
                    reasons.append("💼內資作帳加權")

            if c_close > ma5 > ma10 > ma20:
                score += 15
                reasons.append("多頭排列")

            buy_price = round(c_close, 2)
            stop_price = round(ma20, 2)
            risk = max(c_close - ma20, 0)
            target = round(buy_price + risk * 2.0, 2)
            pct = round((c_close - prev_close) / prev_close * 100, 2)

            results.append({
                "code": code,
                "name": stock_names.get(code, sym),
                "market_tag": "上櫃" if is_otc else "上市",
                "score": score,
                "close": round(c_close, 2),
                "pct": pct,
                "reasons": reasons,
                "buy": buy_price,
                "stop": stop_price,
                "target": target,
            })
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ====================== 庫存健檢 ======================
def check_holdings(holdings, market_stage, ptt_map, stock_names):
    """依 PRD 規則產生庫存健檢文字區塊（list[str]）"""
    lines = []
    weak_market = market_stage in ("YELLOW", "RED")

    for i, h in enumerate(holdings, 1):
        code = h["code"]
        name = h["name"] or stock_names.get(code, "未知")
        cost = h["cost"]
        orig_stop = h["stop"]
        ptt_status = ptt_map.get(code, {}).get("status", STATUS_NONE)
        is_panic = ptt_status == STATUS_PANIC

        cost_str = f"{cost:g}" if cost is not None else "未填"
        stop_str = f"{orig_stop:g}" if orig_stop is not None else "未填"
        lines.append(f"{i}. {code} {name} (成本: {cost_str})")

        df, _ = resolve_df(code)
        today_low = today_close = None
        if df is not None:
            today_low = round(float(df["Low"].iloc[-1]), 2)
            today_close = round(float(df["Close"].iloc[-1]), 2)

        flags = []

        # 規則 3-2：PTT 恐慌絕望 → 死守原始停損（優先於大盤轉弱上調，避免恐慌殺低）
        if is_panic:
            flags.append(
                f"[🚨 持股恐慌警戒] PTT哀號，死守原始停損價 {stop_str} 元，沒破不賣！"
            )
        # 規則 3-1：大盤 YELLOW / RED → 停損上調至今日最低價
        elif weak_market:
            low_str = f"{today_low:g}" if today_low is not None else "(今日最低價取得失敗)"
            flags.append(
                f"[🛡️ 獲利保護機制啟動] 大盤轉弱，停損價上調至今日最低價 {low_str} 元。"
            )

        if not flags:
            flags.append(f"[✅ 持股正常] 續抱，原始停損價 {stop_str} 元。")

        for f in flags:
            lines.append(f"   👉 狀態：{f}")

        # 已實際跌破原始停損 → 額外硬性提醒（紀律優先）
        if orig_stop is not None and today_close is not None and today_close < orig_stop:
            lines.append(f"   ⛔ 收盤 {today_close:g} 已跌破原始停損 {orig_stop:g}，請依紀律執行出場！")
        if ptt_map.get(code, {}).get("count", 0) > 0:
            p = ptt_map[code]
            lines.append(f"   📣 PTT：{p['count']} 篇｜{ptt_status}｜多({p['pos']})/空({p['neg']})")
    return lines


# ====================== LINE 發送 ======================
def _chunk_text(text, limit=4800):
    """LINE 單則文字上限 5000 字，依行切分"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur.rstrip("\n"))
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip("\n"))
    return chunks


def send_line_message(msg):
    """透過 LINE Messaging API 發送（有 LINE_USER_ID → push，否則 broadcast）"""
    if not LINE_TOKEN:
        print("⚠️ 未設定 LINE_TOKEN，僅列印結果：\n" + msg)
        return

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_TOKEN}"}
    messages = [{"type": "text", "text": c} for c in _chunk_text(msg.strip())][:5]  # 單次請求最多 5 則

    if LINE_USER_ID:
        url = "https://api.line.me/v2/bot/message/push"
        payload = {"to": LINE_USER_ID, "messages": messages}
    else:
        url = "https://api.line.me/v2/bot/message/broadcast"
        payload = {"messages": messages}

    print("🚀 正在透過 LINE Messaging API 發送戰報...")
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=15)
            if r.status_code == 200:
                print(f"✅ LINE 推播成功！(嘗試次數: {attempt + 1})")
                return
            print(f"❌ LINE 推播失敗，狀態碼: {r.status_code}, 回應: {r.text}")
        except Exception as e:
            print(f"⚠️ LINE 連線異常 (嘗試 {attempt + 1}/3): {e}")
        if attempt < 2:
            time.sleep(3)


# ====================== 主流程 ======================
def main():
    today_str = datetime.date.today().strftime("%m/%d")
    stock_names = get_stock_names()

    # 【1】大盤四象限
    stage, market_text = get_market_stage()
    print(f"大盤四象限判定完成：{stage}")

    # 讀取庫存
    holdings = get_holdings()
    holdings_failed = holdings is None
    holdings = holdings or []
    holding_codes = {h["code"] for h in holdings}

    # 倉位控管：庫存達上限 → 不選新標的
    position_full = len(holdings) >= MAX_HOLDINGS
    scan_new = (stage != "RED") and not position_full and not holdings_failed

    # 量化選股（先取全部候選，之後再套 PTT 過熱封鎖與配額）
    candidates = scan_candidates(stage, stock_names, holding_codes) if scan_new else []

    # PTT：一次掃描「庫存 + 候選」
    ptt_codes = holding_codes | {c["code"] for c in candidates}
    ptt_map = get_ptt_sentiment(ptt_codes, stock_names) if ptt_codes else {}

    # 【2】庫存健檢
    holding_lines = check_holdings(holdings, stage, ptt_map, stock_names) if holdings else []

    # 過熱封鎖線：極度過熱者剔出買進清單 → 警戒區
    buy_list, danger_list = [], []
    for c in candidates:
        ptt = ptt_map.get(c["code"], {})
        status = ptt.get("status", STATUS_NONE)
        if status == STATUS_HOT:
            danger_list.append((c, ptt))
            continue
        if status == STATUS_PANIC:
            c["reasons"].append("[🧊散戶恐慌錯殺，留意籌碼換手]")
        buy_list.append(c)

    quota = min(QUOTA_MAP.get(stage, 10), MAX_NEW_PICKS)
    picks = buy_list[:quota]

    # ---------------- 組合戰報（嚴格依序） ----------------
    out = [f"📊 【{today_str} 全台股量化選股與防衛戰報】", ""]

    # 1. 大盤四象限診斷
    out += ["【大盤四象限診斷】", market_text, SEP, ""]

    # 2. 現有庫存健檢
    out.append("【🛡️ 現有庫存健檢】 (最優先確認出場與防守)")
    if holdings_failed:
        out.append("⚠️ Google Sheets 庫存讀取失敗，本次無法健檢，已暫停新標的推薦（請檢查權限/金鑰）。")
    elif not holdings:
        out.append("目前無庫存。")
    else:
        out += holding_lines
    out += [SEP, ""]

    # 3. 明日新標的推薦
    out.append("【🏆 明日新標的推薦】 (確認防守後，才看進攻)")
    if holdings_failed:
        out.append("⏸️ 因庫存資料無法確認，暫停新標的推薦。")
    elif position_full:
        out.append(f"⏸️ 已達 {MAX_HOLDINGS} 檔持股上限，暫停新標的進場")
    elif stage == "RED":
        out.append("💡 風控紀律：大盤處於覆巢之下修正期，已全面暫停多單推薦，嚴禁逆勢接刀。")
    elif not picks:
        reason = ("拉積盤或短線洗盤期間，無標的符合極限防禦門檻。" if stage == "YELLOW"
                  else "今日無標的通過嚴格多因子共振健檢。")
        out.append(f"💡 今日選股結果：{reason}建議空手觀望。")
    else:
        for rank, s in enumerate(picks, 1):
            out.append(f"第 {rank} 名：{s['code']} {s['name']} [{s['market_tag']}] (收 {s['close']}, {s['pct']}%)")
            out.append(f"  🔍 原因：{' / '.join(s['reasons'])}")
            out.append(f"  👉 突破進場：{s['buy']}")
            out.append(f"  🛑 跌破停損：{s['stop']}")
            out.append(f"  🎯 目標停利：{s['target']}")
            out.append("")
        out.append("💡 風控提醒：隔日開盤直接跳空大漲 >2% 或未過突破價皆放棄不追；同時持股以 5 檔為限。")
    out += [SEP, ""]

    # 4. 主力倒貨警戒區
    out.append("【🚨 主力倒貨警戒區 (PTT極度過熱)】")
    out.append("(量化系統選出，但散戶過熱，系統強制鎖定)")
    if danger_list:
        for c, ptt in danger_list:
            out.append(f"- {c['code']} {c['name']}：系統封鎖進場價，嚴禁追高！"
                       f"（PTT {ptt.get('count', 0)} 篇，多{ptt.get('pos', 0)}/空{ptt.get('neg', 0)}）")
    else:
        out.append("- 目前無")

    send_line_message("\n".join(out))


if __name__ == "__main__":
    main()
