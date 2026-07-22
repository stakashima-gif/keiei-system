#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
経営管理システム — バックエンドサーバー
Mac/Linux標準のPython3だけで動作（追加ライブラリ不要）。

起動:  python3 server.py
停止:  Ctrl + C

データは data/db.json に保存されます（全員で共有）。
初期管理者アカウント:  ID = admin  /  パスワード = admin123
※ 初回ログイン後、必ずパスワードを変更してください。
"""
import json, os, hashlib, hmac, secrets, threading, time, socket, sys, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR は環境変数で上書き可（クラウドの永続ディスクを指定するため）
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "db.json")
APP_HTML = os.path.join(BASE_DIR, "app.html")
PORT = int(os.environ.get("PORT", "8000"))
SESSION_TTL = 60 * 60 * 24 * 14  # 14日

# ---- ページ定義（フロントと一致させる） ----
# 各ページが必要とするデータ種別（閲覧範囲の制御に使用）
PAGE_COLLECTIONS = {
    "dashboard": ["company", "businesses", "sales", "finance", "cost", "tasks", "contracts", "people"],
    "analysis":  ["company", "sales", "finance", "cost", "people"],
    "breakeven": ["cost", "finance"],
    "business":  ["businesses"],
    "sales":     ["sales", "businesses"],
    "finance":   ["finance"],
    "tasks":     ["tasks", "businesses"],
    "contracts": ["contracts"],
    "people":    ["people", "businesses"],
    "tools":     ["tools"],
    "recruits":  ["recruits"],
    "cashflow":  ["cashflow", "banks", "cftxns"],   # 資金繰り（設定・口座マスタ・入出金明細）
    "memos":     ["memos", "contracts"],            # 打ち合わせメモ（クライアント検索で契約情報も参照）
}
# 各ページが編集できるデータ種別
PAGE_WRITE = {
    "business": ["businesses"], "sales": ["sales"], "finance": ["finance"],
    "breakeven": ["cost"], "tasks": ["tasks"], "contracts": ["contracts"], "people": ["people"],
    "tools": ["tools"], "recruits": ["recruits"], "cashflow": ["cashflow", "banks", "cftxns"], "memos": ["memos"],
    "dashboard": [], "analysis": [],
}
ALL_PAGES = list(PAGE_COLLECTIONS.keys())

# =====================================================================
# 資金繰り（GMOあおぞらネット銀行 連携）
#   トークンは環境変数でのみ設定し、共有DBには保存しない。
#     GMO_MODE          : mock | sunabar | production（既定 mock）
#     GMO_ACCESS_TOKEN  : アクセストークン
#     GMO_ACCOUNT_ID    : 口座ID（必要な場合）
#     GMO_BASE_URL      : APIベースURL
#     GMO_ACCOUNT_TYPE  : corporation | personal
#     GMO_TXN_PATH      : 入出金明細照会のパス（{type}を口座種別で置換）
# =====================================================================
CF_CATEGORIES = {
    "sales":       {"label": "売上入金",         "group": "operating_in"},
    "other_in":    {"label": "その他営業収入",   "group": "operating_in"},
    "purchase":    {"label": "仕入・外注",       "group": "operating_out"},
    "payroll":     {"label": "人件費",           "group": "operating_out"},
    "tax":         {"label": "税金・社会保険",   "group": "operating_out"},
    "expense":     {"label": "経費・その他",     "group": "operating_out"},
    "finance_in":  {"label": "財務収入（借入）", "group": "finance_in"},
    "finance_out": {"label": "財務支出（返済）", "group": "finance_out"},
}
CF_RULES = {
    "in": [
        {"category": "finance_in", "keywords": ["借入", "融資", "ローン", "貸付", "ﾕｳｼ"]},
        {"category": "other_in",   "keywords": ["利息", "還付", "助成", "補助金", "配当", "返金"]},
    ],
    "out": [
        {"category": "finance_out", "keywords": ["返済", "約定", "元金", "ﾍﾝｻｲ", "ﾘｰｽ", "リース"]},
        {"category": "payroll",     "keywords": ["給与", "賞与", "役員報酬", "給料", "ｷｭｳﾖ", "賃金", "ｼﾞｮｳﾖ"]},
        {"category": "tax",         "keywords": ["税", "社会保険", "年金", "労働保険", "ｾﾞｲ", "健康保険", "ﾎｹﾝ", "ﾈﾝｷﾝ"]},
        {"category": "purchase",    "keywords": ["仕入", "外注", "業務委託", "ｼｲﾚ", "ｶﾞｲﾁｭｳ", "仕入れ"]},
        {"category": "expense",     "keywords": ["家賃", "水道", "電気", "ガス", "通信", "電話", "広告", "手数料", "ﾔﾁﾝ", "ﾃﾞﾝｷ", "ﾂｳｼﾝ"]},
    ],
}


def cf_bank_config():
    return {
        "mode": os.environ.get("GMO_MODE", "off"),
        "base_url": os.environ.get("GMO_BASE_URL", "https://api.sunabar.gmo-aozora.com"),
        "account_type": os.environ.get("GMO_ACCOUNT_TYPE", "corporation"),
        "access_token": os.environ.get("GMO_ACCESS_TOKEN", ""),
        "account_id": os.environ.get("GMO_ACCOUNT_ID", ""),
        "txn_path": os.environ.get("GMO_TXN_PATH", "/{type}/v1/accounts/transactions"),
    }


def cf_get_transactions(date_from, date_to):
    """銀行API（GMOあおぞら）から取得した明細を返す。mode=off なら取得しない（=手入力/CSVのみ運用）。"""
    cfg = cf_bank_config()
    mode = cfg["mode"]
    if mode in ("sunabar", "production"):
        try:
            raw = cf_fetch_bank(cfg, date_from, date_to)
            txns = [t for t in (cf_normalize(x) for x in raw) if t]
            return txns, {"source": mode, "count": len(txns), "error": None}
        except Exception as e:
            return [], {"source": "error", "count": 0,
                        "error": "GMOあおぞらからの取得に失敗しました: %s" % e}
    if mode == "mock":
        txns = cf_mock(date_from, date_to)
        return txns, {"source": "mock", "count": len(txns), "error": None}
    # off（既定）: 銀行APIは使わず、手入力/CSV取込の明細だけで集計する
    return [], {"source": "off", "count": 0, "error": None}


def cf_fetch_bank(cfg, date_from, date_to):
    token = (cfg.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("アクセストークン未設定（環境変数 GMO_ACCESS_TOKEN）")
    path = cfg["txn_path"].replace("{type}", cfg["account_type"])
    base = cfg["base_url"].rstrip("/")
    params = {"dateFrom": date_from.replace("-", ""), "dateTo": date_to.replace("-", "")}
    if cfg.get("account_id"):
        params["accountId"] = cfg["account_id"]
    url = base + path + "?" + urlencode(params)
    req = Request(url, headers={"Authorization": "Bearer " + token,
                                "x-access-token": token, "Accept": "application/json"})
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return cf_extract_list(data)


def cf_extract_list(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("transactions", "transactionList", "meisai", "details", "list"):
        if isinstance(data.get(key), list):
            return data[key]
    accts = data.get("accounts") or data.get("accountList")
    if isinstance(accts, list):
        out = []
        for a in accts:
            if isinstance(a, dict):
                for key in ("transactions", "transactionList", "details"):
                    if isinstance(a.get(key), list):
                        out.extend(a[key])
        if out:
            return out
    for v in data.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return []


def cf_normalize(t):
    if not isinstance(t, dict):
        return None

    def pick(*keys):
        for k in keys:
            if k in t and t[k] not in (None, ""):
                return t[k]
        return None

    date = cf_fmt_date(pick("transactionDate", "valueDate", "date", "transaction_date", "torihikiDate"))
    if not date:
        return None
    amount = cf_to_int(pick("transactionAmount", "amount", "value", "kingaku"))
    remarks = pick("remarks", "itemName", "transactionContent", "summary",
                   "description", "content", "tekiyo", "counterPartyName") or ""
    direction = None
    dw = pick("depositWithdrawalCategory", "transactionType", "valueClass",
              "creditDebitType", "torihikiKubun")
    if dw is not None:
        s = str(dw).strip().lower()
        if s in ("1", "入金", "credit", "cr", "deposit", "nyukin", "in"):
            direction = "in"
        elif s in ("2", "出金", "debit", "dr", "withdrawal", "shukkin", "out"):
            direction = "out"
    if direction is None:
        if amount < 0:
            direction = "out"
        elif cf_to_int(t.get("creditAmount")):
            direction = "in"
        elif cf_to_int(t.get("debitAmount")):
            direction = "out"
        else:
            direction = "in"
    if "creditAmount" in t or "debitAmount" in t:
        amount = cf_to_int(t.get("creditAmount")) or cf_to_int(t.get("debitAmount"))
    return {"date": date, "amount": abs(amount), "direction": direction, "remarks": str(remarks)}


def cf_fmt_date(v):
    if v is None:
        return None
    digits = "".join(ch for ch in str(v) if ch.isdigit())
    if len(digits) >= 8:
        return "%s-%s-%s" % (digits[0:4], digits[4:6], digits[6:8])
    return None


def cf_to_int(v):
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v)
    neg = s.strip().startswith(("-", "△", "▲"))
    s = "".join(ch for ch in s if ch.isdigit())
    if not s:
        return 0
    return -int(s) if neg else int(s)


def cf_classify(txn):
    # 手入力/CSV取込で科目が明示されていればそれを優先
    cat = txn.get("category")
    if cat in CF_CATEGORIES:
        return cat
    remarks = txn.get("remarks", "")
    direction = txn.get("direction", "in")
    for rule in CF_RULES.get(direction, []):
        for kw in rule["keywords"]:
            if kw and kw in remarks:
                return rule["category"]
    return "sales" if direction == "in" else "expense"


def cf_month_range(date_from, date_to):
    y, m = int(date_from[0:4]), int(date_from[5:7])
    y2, m2 = int(date_to[0:4]), int(date_to[5:7])
    out = []
    while (y, m) <= (y2, m2):
        out.append("%04d-%02d" % (y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def cf_build(txns, months, opening0):
    per = {mm: {c: 0 for c in CF_CATEGORIES} for mm in months}
    detail = {mm: [] for mm in months}
    for t in txns:
        mm = t["date"][:7]
        if mm not in per:
            continue
        cat = cf_classify(t)
        per[mm][cat] += t["amount"]
        detail[mm].append({**t, "category": cat, "category_label": CF_CATEGORIES[cat]["label"]})
    rows = []
    opening = int(opening0)
    for mm in months:
        c = per[mm]
        op_in = c["sales"] + c["other_in"]
        op_out = c["purchase"] + c["payroll"] + c["tax"] + c["expense"]
        fin_in, fin_out = c["finance_in"], c["finance_out"]
        month_net = (op_in - op_out) + (fin_in - fin_out)
        closing = opening + month_net
        rows.append({"month": mm, "opening": opening, "categories": c,
                     "operating_in": op_in, "operating_out": op_out,
                     "operating_net": op_in - op_out, "finance_in": fin_in,
                     "finance_out": fin_out, "finance_net": fin_in - fin_out,
                     "month_net": month_net, "closing": closing,
                     "detail_count": len(detail[mm])})
        opening = closing
    return {"rows": rows, "detail_by_month": detail}


def cf_mock(date_from, date_to):
    txns = []
    for mm in cf_month_range(date_from, date_to):
        y, mo = int(mm[0:4]), int(mm[5:7])
        seed = int(hashlib.md5(mm.encode()).hexdigest(), 16)

        def var(base, pct, n):
            r = (seed >> (n * 5)) % 1000 / 1000.0
            return int(base * (1 + (r - 0.5) * 2 * pct))

        for i, name in enumerate(["ｶ)ｱｵｿﾞﾗｼｮｳｼﾞ", "ｶ)ﾐﾗｲﾃｯｸ", "ｹﾞﾝｷ ｹｱ ｺﾞｳ", "ｶ)ｻｸﾗﾌｰｽﾞ"]):
            txns.append(cf_mk(y, mo, 5 + i * 6, var(650000, 0.25, i), "in", name))
        if (seed % 3) == 0:
            txns.append(cf_mk(y, mo, 20, 3000000, "in", "ﾆﾎﾝｾｲｻｸｺﾞﾝ ﾕｳｼ"))
        txns.append(cf_mk(y, mo, 10, var(720000, 0.2, 1), "out", "ｶ)ﾀﾞｲｲﾁ ｼｲﾚ"))
        txns.append(cf_mk(y, mo, 15, var(280000, 0.3, 2), "out", "ﾌﾘｰﾗﾝｽ ｶﾞｲﾁｭｳ ﾋ"))
        txns.append(cf_mk(y, mo, 25, var(1250000, 0.05, 3), "out", "ｷｭｳﾖ ｼﾊﾗｲ"))
        txns.append(cf_mk(y, mo, 1, 220000, "out", "ﾔﾁﾝ ｵﾌｨｽ"))
        txns.append(cf_mk(y, mo, 27, var(58000, 0.2, 4), "out", "ﾃﾞﾝｷ ｶﾞｽ ｽｲﾄﾞｳ"))
        txns.append(cf_mk(y, mo, 27, var(42000, 0.15, 5), "out", "ﾂｳｼﾝﾋ ｹｲﾀｲ"))
        txns.append(cf_mk(y, mo, 28, var(320000, 0.1, 6), "out", "ｼｬｶｲﾎｹﾝﾘｮｳ"))
        txns.append(cf_mk(y, mo, 26, 180000, "out", "ｼｬｸﾆｭｳｷﾝ ﾍﾝｻｲ ｶﾞﾝｷﾝ"))
    txns = [t for t in txns if date_from <= t["date"] <= date_to]
    txns.sort(key=lambda t: t["date"])
    return txns


def cf_mk(y, mo, day, amount, direction, remarks):
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1]
    return {"date": "%04d-%02d-%02d" % (y, mo, min(day, last)),
            "amount": int(amount), "direction": direction, "remarks": remarks}

LOCK = threading.RLock()

# =====================================================================
# データ層（JSONファイル・プロセス内ロックで保護）
# =====================================================================
def now(): return int(time.time())

def hash_pw(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000)
    return salt, h.hex()

def verify_pw(password, salt, hexhash):
    _, h = hash_pw(password, salt)
    return hmac.compare_digest(h, hexhash)

def seed_store():
    """サンプルの会社データ。売上は各事業のID(=列キー)ごとに保持する。"""
    def gid(p): return p + secrets.token_hex(4)
    def days(n): return time.strftime("%Y-%m-%d", time.localtime(now() + n * 86400))
    # 4事業（売上は各事業のID(=列キー)ごとに保持。事業を追加すれば売上列も自動で増える）
    businesses = [
        {"id": gid("b"), "name": "BPO事業",   "lead": "—", "status": "成長", "target": 72000000, "actual": 60000000, "members": 10, "gmRate": 40},
        {"id": gid("b"), "name": "RPO事業",   "lead": "—", "status": "成長", "target": 48000000, "actual": 41000000, "members": 8,  "gmRate": 45},
        {"id": gid("b"), "name": "SES事業",   "lead": "—", "status": "安定", "target": 96000000, "actual": 99000000, "members": 22, "gmRate": 25},
        {"id": gid("b"), "name": "ライバー事業", "lead": "—", "status": "新規", "target": 36000000, "actual": 21000000, "members": 6,  "gmRate": 55},
    ]
    monthly_base = [5.0, 3.4, 8.2, 1.8]  # 各事業の月次売上ベース（百万円）
    sales = []
    finance = []
    for i in range(12):
        row = {"month": i, "total": 0}
        for j, b in enumerate(businesses):
            amt = round(monthly_base[j] * 1000000 * (1 + i * 0.02))
            row[b["id"]] = amt
            row["total"] += amt
        sales.append(row)
        tot = row["total"]
        cogs = round(tot * 0.55); sga = round(tot * 0.30) + (900000 if i % 3 == 0 else 0)
        finance.append({"month": i, "revenue": tot, "cogs": cogs, "sga": sga, "op": tot - cogs - sga})
    return {
        "company": {"name": "自社", "fy": "2026", "cashTarget": 30000000},
        "businesses": businesses,
        "sales": sales,
        "finance": finance,
        "cost": {"fixedMonthly": 9800000, "variableRate": 0.55, "priceUnit": 50000},
        "tasks": [
            {"id": gid("t"), "title": "Q2 予算レビュー会議", "assignee": "—", "due": days(3), "priority": "高", "status": "進行中", "biz": "全社"},
            {"id": gid("t"), "title": "SES新規エンジニア面談", "assignee": "—", "due": days(1), "priority": "高", "status": "未着手", "biz": "SES事業"},
            {"id": gid("t"), "title": "RPO提案資料の作成", "assignee": "—", "due": days(7), "priority": "中", "status": "進行中", "biz": "RPO事業"},
            {"id": gid("t"), "title": "BPO案件の納品", "assignee": "—", "due": days(-2), "priority": "高", "status": "進行中", "biz": "BPO事業"},
            {"id": gid("t"), "title": "ライバー新規スカウト", "assignee": "—", "due": days(5), "priority": "中", "status": "未着手", "biz": "ライバー事業"},
            {"id": gid("t"), "title": "月次決算の確定", "assignee": "—", "due": days(10), "priority": "中", "status": "完了", "biz": "全社"},
        ],
        "contracts": [
            {"id": gid("c"), "client": "取引先A", "type": "BPO業務委託", "amount": 2400000, "start": "2025-04-01", "end": days(20), "status": "有効", "auto": True},
            {"id": gid("c"), "client": "取引先B", "type": "RPO契約", "amount": 3600000, "start": "2025-07-01", "end": days(95), "status": "有効", "auto": True},
            {"id": gid("c"), "client": "取引先C", "type": "SES契約", "amount": 12000000, "start": "2026-01-01", "end": days(8), "status": "有効", "auto": False},
            {"id": gid("c"), "client": "取引先D", "type": "ライバー業務委託", "amount": 4800000, "start": "2025-10-01", "end": days(-5), "status": "更新待ち", "auto": False},
        ],
        "people": [
            {"id": gid("p"), "name": "—", "role": "事業部長", "biz": "BPO事業", "type": "正社員", "cost": 700000, "joined": "2022-04-01", "rating": "A"},
            {"id": gid("p"), "name": "—", "role": "リクルーター", "biz": "RPO事業", "type": "正社員", "cost": 600000, "joined": "2023-04-01", "rating": "B"},
            {"id": gid("p"), "name": "—", "role": "エンジニア", "biz": "SES事業", "type": "正社員", "cost": 650000, "joined": "2021-09-01", "rating": "A"},
            {"id": gid("p"), "name": "—", "role": "マネージャー", "biz": "ライバー事業", "type": "正社員", "cost": 550000, "joined": "2024-01-01", "rating": "B"},
        ],
        # 資金繰り：口座マスタ（銀行）と入出金明細（手入力／CSV取込）
        "banks": [
            {"id": gid("bk"), "name": "GMOあおぞらネット銀行", "kind": "普通", "api": "gmo", "note": "API自動連携（要トークン設定）"},
            {"id": gid("bk"), "name": "メインバンク（例）", "kind": "普通", "api": "", "note": "CSV取込／手入力"},
        ],
        "cftxns": [],
        "cashflow": {"opening_balance": 0},
        "memos": [
            {"id": gid("m"), "date": days(-3), "client": "取引先A", "title": "定例ミーティング", "attendees": "—", "body": "進捗確認。次回までに追加見積を提出予定。", "biz": "BPO事業"},
            {"id": gid("m"), "date": days(-10), "client": "取引先C", "title": "SES増員のご相談", "attendees": "—", "body": "エンジニア2名の増員依頼あり。単価と開始時期を調整中。", "biz": "SES事業"},
        ],
        "tools": [
            {"id": gid("k"), "name": "Slack", "url": "https://slack.com", "category": "コミュニケーション", "icon": "💬"},
            {"id": gid("k"), "name": "Gmail", "url": "https://mail.google.com", "category": "コミュニケーション", "icon": "✉️"},
            {"id": gid("k"), "name": "freee 会計", "url": "https://secure.freee.co.jp", "category": "会計・経理", "icon": "💴"},
            {"id": gid("k"), "name": "Google Drive", "url": "https://drive.google.com", "category": "ドキュメント", "icon": "📁"},
            {"id": gid("k"), "name": "Notion", "url": "https://www.notion.so", "category": "ドキュメント", "icon": "📝"},
            {"id": gid("k"), "name": "Google カレンダー", "url": "https://calendar.google.com", "category": "スケジュール", "icon": "📅"},
        ],
        "recruits": [
            {"id": gid("r"), "name": "田中 陽菜", "kana": "たなか はるな", "position": "バックエンドエンジニア", "stage": "面接",     "source": "Airワーク", "airworkId": "AW-10231", "applied": days(-10), "note": "React経験3年。技術力が高く即戦力。",
             "scores": {"skill": 5, "experience": 4, "motivation": 4, "culture": 4, "communication": 3}},
            {"id": gid("r"), "name": "佐藤 健太", "kana": "さとう けんた", "position": "営業",                 "stage": "書類選考", "source": "Airワーク", "airworkId": "AW-10245", "applied": days(-5),  "note": "前職で新規開拓トップ。ポテンシャル高い。",
             "scores": {"skill": 3, "experience": 4, "motivation": 5, "culture": 4, "communication": 5}},
            {"id": gid("r"), "name": "鈴木 美咲", "kana": "すずき みさき", "position": "デザイナー",           "stage": "内定",     "source": "リファラル", "airworkId": "",         "applied": days(-20), "note": "SNS運用・ブランディングに強み。",
             "scores": {"skill": 4, "experience": 3, "motivation": 4, "culture": 5, "communication": 4}},
            {"id": gid("r"), "name": "山本 大輔", "kana": "やまもと だいすけ", "position": "バックエンドエンジニア", "stage": "応募", "source": "Indeed",   "airworkId": "",         "applied": days(-2),  "note": "",
             "scores": {"skill": 3, "experience": 3, "motivation": 4, "culture": 3, "communication": 4}},
        ],
    }

def fresh_db():
    salt, h = hash_pw("admin123")
    return {
        "rev": 1,
        "users": [{
            "id": "u" + secrets.token_hex(4), "username": "admin", "display_name": "管理者",
            "role": "admin", "pages": ALL_PAGES, "biz": "全社", "salt": salt, "hash": h, "created_at": now(),
        }],
        "sessions": {},
        "store": seed_store(),
    }

def load_db():
    if not os.path.exists(DB_PATH):
        os.makedirs(DATA_DIR, exist_ok=True)
        db = fresh_db()
        save_db(db)
        return db
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_PATH)

DB = None
def init_db():
    global DB
    DB = load_db()
    # 期限切れセッションの掃除
    DB["sessions"] = {t: s for t, s in DB["sessions"].items() if s["expires"] > now()}
    save_db(DB)

# =====================================================================
# 権限ヘルパ
# =====================================================================
def user_public(u):
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"],
            "role": u["role"], "biz": u.get("biz", "全社"),
            "pages": (ALL_PAGES if u["role"] == "admin" else u.get("pages", []))}

def scope_sales(u, sales):
    """所属事業に紐付くメンバーには、その事業の売上のみ返す（管理者・全社は全件）"""
    if u["role"] == "admin":
        return sales
    biz = u.get("biz", "全社")
    if not biz or biz == "全社":
        return sales
    key = None
    for b in DB["store"].get("businesses", []):
        if b["name"] == biz:
            key = b["id"]
    if not key:
        return sales
    return [{"month": r.get("month"), key: r.get(key, 0), "total": r.get(key, 0), "_scoped": biz} for r in sales]

def readable_collections(u):
    if u["role"] == "admin":
        return set(sum(PAGE_COLLECTIONS.values(), []))
    cols = set()
    for p in u.get("pages", []):
        cols.update(PAGE_COLLECTIONS.get(p, []))
    return cols

def writable_collections(u):
    if u["role"] == "admin":
        return set(sum(PAGE_WRITE.values(), []))
    if u["role"] == "viewer":
        return set()
    cols = set()
    for p in u.get("pages", []):
        cols.update(PAGE_WRITE.get(p, []))
    return cols

# =====================================================================
# HTTPハンドラ
# =====================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "KeieiSrv/1.0"

    def log_message(self, *a):  # 静かに
        pass

    # ---- 低レベル出力 ----
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra:
                self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

    def _err(self, code, msg):
        self._json(code, {"error": msg})

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _cookie(self, name):
        c = self.headers.get("Cookie", "")
        for part in c.split(";"):
            kv = part.strip().split("=", 1)
            if len(kv) == 2 and kv[0] == name:
                return kv[1]
        return None

    def _current_user(self):
        token = self._cookie("sid")
        if not token:
            return None
        with LOCK:
            sess = DB["sessions"].get(token)
            if not sess or sess["expires"] < now():
                return None
            for u in DB["users"]:
                if u["id"] == sess["user_id"]:
                    return u
        return None

    # ---- ルーティング ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/app.html"):
            return self._serve_app()
        if path == "/favicon.ico":
            return self._send(204)
        if path == "/api/me":
            return self._api_me()
        if path == "/api/rev":
            with LOCK:
                return self._json(200, {"rev": DB["rev"]})
        if path == "/api/store":
            return self._api_store_get()
        if path == "/api/cashflow":
            return self._api_cashflow(urlparse(self.path).query)
        if path == "/api/users":
            return self._api_users_list()
        return self._err(404, "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            return self._api_login()
        if path == "/api/logout":
            return self._api_logout()
        if path == "/api/password":
            return self._api_password()
        if path == "/api/users":
            return self._api_users_create()
        # 1件だけ追加/更新（複数タブでもコレクション全体を壊さない）
        if path.startswith("/api/store/") and path.endswith("/item"):
            return self._api_item_upsert(path[len("/api/store/"):-len("/item")])
        return self._err(404, "not found")

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/api/store/"):
            return self._api_store_put(path[len("/api/store/"):])
        if path.startswith("/api/users/"):
            return self._api_users_update(path[len("/api/users/"):])
        return self._err(404, "not found")

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/users/"):
            return self._api_users_delete(path[len("/api/users/"):])
        # /api/store/<coll>/item/<id> — 1件だけ削除
        if path.startswith("/api/store/"):
            parts = path[len("/api/store/"):].split("/")
            if len(parts) == 3 and parts[1] == "item":
                return self._api_item_delete(parts[0], parts[2])
        return self._err(404, "not found")

    # ---- 静的 ----
    def _serve_app(self):
        try:
            with open(APP_HTML, "rb") as f:
                body = f.read()
            self._send(200, body, ctype="text/html; charset=utf-8")
        except FileNotFoundError:
            self._send(500, b"app.html not found", ctype="text/plain; charset=utf-8")

    # ---- 認証 ----
    def _api_login(self):
        d = self._body()
        username = (d.get("username") or "").strip()
        password = d.get("password") or ""
        with LOCK:
            u = next((x for x in DB["users"] if x["username"] == username), None)
            if not u or not verify_pw(password, u["salt"], u["hash"]):
                return self._err(401, "IDまたはパスワードが違います")
            token = secrets.token_urlsafe(32)
            DB["sessions"][token] = {"user_id": u["id"], "expires": now() + SESSION_TTL}
            save_db(DB)
            cookie = "sid=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (token, SESSION_TTL)
            self._json(200, {"user": user_public(u)}, extra=[("Set-Cookie", cookie)])

    def _api_logout(self):
        token = self._cookie("sid")
        with LOCK:
            if token and token in DB["sessions"]:
                del DB["sessions"][token]
                save_db(DB)
        self._json(200, {"ok": True}, extra=[("Set-Cookie", "sid=; Path=/; Max-Age=0")])

    def _api_me(self):
        u = self._current_user()
        if not u:
            return self._err(401, "未ログイン")
        self._json(200, {"user": user_public(u)})

    def _api_password(self):
        u = self._current_user()
        if not u:
            return self._err(401, "未ログイン")
        d = self._body()
        old = d.get("old") or ""; new = d.get("new") or ""
        if len(new) < 6:
            return self._err(400, "新しいパスワードは6文字以上にしてください")
        with LOCK:
            if not verify_pw(old, u["salt"], u["hash"]):
                return self._err(400, "現在のパスワードが違います")
            u["salt"], u["hash"] = hash_pw(new)
            save_db(DB)
        self._json(200, {"ok": True})

    # ---- データ ----
    def _api_store_get(self):
        u = self._current_user()
        if not u:
            return self._err(401, "未ログイン")
        cols = readable_collections(u)
        with LOCK:
            data = {k: v for k, v in DB["store"].items() if k in cols}
            if "sales" in data:
                data["sales"] = scope_sales(u, data["sales"])
            rev = DB["rev"]
        self._json(200, {"store": data, "rev": rev})

    def _api_store_put(self, collection):
        u = self._current_user()
        if not u:
            return self._err(401, "未ログイン")
        if collection not in writable_collections(u):
            return self._err(403, "このデータを編集する権限がありません")
        d = self._body()
        if "value" not in d:
            return self._err(400, "value がありません")
        with LOCK:
            DB["store"][collection] = d["value"]
            DB["rev"] += 1
            save_db(DB)
            rev = DB["rev"]
        self._json(200, {"ok": True, "rev": rev})

    def _api_item_upsert(self, collection):
        """1件だけ追加/更新（id一致で置換、無ければ追加）。他の項目には触れない。"""
        u = self._current_user()
        if not u:
            return self._err(401, "未ログイン")
        if collection not in writable_collections(u):
            return self._err(403, "このデータを編集する権限がありません")
        d = self._body()
        item = d.get("value")
        if not isinstance(item, dict) or not item.get("id"):
            return self._err(400, "id 付きの item が必要です")
        with LOCK:
            lst = DB["store"].get(collection)
            if not isinstance(lst, list):
                lst = []
                DB["store"][collection] = lst
            for i, x in enumerate(lst):
                if x.get("id") == item["id"]:
                    lst[i] = item
                    break
            else:
                lst.append(item)
            DB["rev"] += 1
            save_db(DB)
            rev = DB["rev"]
        self._json(200, {"ok": True, "rev": rev, "item": item})

    def _api_item_delete(self, collection, item_id):
        """1件だけ削除。他の項目には触れない。"""
        u = self._current_user()
        if not u:
            return self._err(401, "未ログイン")
        if collection not in writable_collections(u):
            return self._err(403, "このデータを編集する権限がありません")
        with LOCK:
            lst = DB["store"].get(collection)
            if isinstance(lst, list):
                DB["store"][collection] = [x for x in lst if x.get("id") != item_id]
            DB["rev"] += 1
            save_db(DB)
            rev = DB["rev"]
        self._json(200, {"ok": True, "rev": rev})

    # ---- 資金繰り（銀行API連携） ----
    def _api_cashflow(self, query):
        u = self._current_user()
        if not u:
            return self._err(401, "未ログイン")
        pages = ALL_PAGES if u["role"] == "admin" else u.get("pages", [])
        if "cashflow" not in pages:
            return self._err(403, "資金繰りページの権限がありません")
        q = parse_qs(query)
        today = datetime.date.today()
        y, mo = today.year, today.month - 5
        while mo <= 0:
            mo += 12
            y -= 1
        date_from = q.get("from", ["%04d-%02d-01" % (y, mo)])[0]
        date_to = q.get("to", [today.strftime("%Y-%m-%d")])[0]
        if len(date_from) == 7:
            date_from += "-01"
        if len(date_to) == 7:
            date_to += "-28"
        with LOCK:
            settings = DB["store"].get("cashflow") or {}
            stored = list(DB["store"].get("cftxns") or [])
        opening = settings.get("opening_balance", 0)
        try:
            api_txns, source = cf_get_transactions(date_from, date_to)
            # 手入力／CSV取込の明細（期間内のみ）を銀行APIの明細に合算する
            manual = []
            for t in stored:
                d = (t.get("date") or "")[:10]
                if not d or d < date_from or d > date_to:
                    continue
                try:
                    amt = abs(int(float(t.get("amount") or 0)))
                except (TypeError, ValueError):
                    continue
                if amt <= 0:
                    continue
                manual.append({"date": d, "amount": amt,
                               "direction": "out" if t.get("direction") == "out" else "in",
                               "remarks": t.get("remarks") or "",
                               "category": t.get("category"),
                               "bank": t.get("bank") or "",
                               "source": t.get("source") or "manual"})
            txns = api_txns + manual
            source = {**source, "manual_count": len(manual), "total_count": len(txns)}
            months = cf_month_range(date_from, date_to)
            cf = cf_build(txns, months, opening)
            self._json(200, {"ok": True, "source": source,
                             "range": {"from": date_from, "to": date_to},
                             "opening_balance": opening, "categories": CF_CATEGORIES,
                             "cashflow": cf["rows"], "detail_by_month": cf["detail_by_month"]})
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._err(500, str(e))

    # ---- アカウント管理（管理者のみ） ----
    def _require_admin(self):
        u = self._current_user()
        if not u:
            self._err(401, "未ログイン"); return None
        if u["role"] != "admin":
            self._err(403, "管理者のみ操作できます"); return None
        return u

    def _api_users_list(self):
        if not self._require_admin():
            return
        with LOCK:
            self._json(200, {"users": [user_public(x) for x in DB["users"]]})

    def _api_users_create(self):
        if not self._require_admin():
            return
        d = self._body()
        username = (d.get("username") or "").strip()
        password = d.get("password") or ""
        if not username or len(password) < 6:
            return self._err(400, "ユーザー名と6文字以上のパスワードが必要です")
        role = d.get("role") if d.get("role") in ("admin", "editor", "viewer") else "viewer"
        pages = [p for p in (d.get("pages") or []) if p in ALL_PAGES]
        with LOCK:
            if any(x["username"] == username for x in DB["users"]):
                return self._err(409, "そのユーザー名は既に使われています")
            salt, h = hash_pw(password)
            u = {"id": "u" + secrets.token_hex(4), "username": username,
                 "display_name": (d.get("display_name") or username).strip(),
                 "role": role, "pages": pages, "biz": (d.get("biz") or "全社").strip(),
                 "salt": salt, "hash": h, "created_at": now()}
            DB["users"].append(u)
            DB["rev"] += 1
            save_db(DB)
            self._json(200, {"user": user_public(u)})

    def _api_users_update(self, uid):
        admin = self._require_admin()
        if not admin:
            return
        d = self._body()
        with LOCK:
            u = next((x for x in DB["users"] if x["id"] == uid), None)
            if not u:
                return self._err(404, "ユーザーが見つかりません")
            if "display_name" in d:
                u["display_name"] = (d["display_name"] or u["display_name"]).strip()
            if "role" in d and d["role"] in ("admin", "editor", "viewer"):
                # 最後の管理者の降格を防ぐ
                if u["role"] == "admin" and d["role"] != "admin" and sum(1 for x in DB["users"] if x["role"] == "admin") <= 1:
                    return self._err(400, "管理者は最低1人必要です")
                u["role"] = d["role"]
            if "pages" in d:
                u["pages"] = [p for p in (d["pages"] or []) if p in ALL_PAGES]
            if "biz" in d:
                u["biz"] = (d["biz"] or "全社").strip()
            if d.get("password"):
                if len(d["password"]) < 6:
                    return self._err(400, "パスワードは6文字以上にしてください")
                u["salt"], u["hash"] = hash_pw(d["password"])
            DB["rev"] += 1
            save_db(DB)
            self._json(200, {"user": user_public(u)})

    def _api_users_delete(self, uid):
        admin = self._require_admin()
        if not admin:
            return
        with LOCK:
            u = next((x for x in DB["users"] if x["id"] == uid), None)
            if not u:
                return self._err(404, "ユーザーが見つかりません")
            if u["id"] == admin["id"]:
                return self._err(400, "自分自身は削除できません")
            if u["role"] == "admin" and sum(1 for x in DB["users"] if x["role"] == "admin") <= 1:
                return self._err(400, "管理者は最低1人必要です")
            DB["users"] = [x for x in DB["users"] if x["id"] != uid]
            DB["sessions"] = {t: s for t, s in DB["sessions"].items() if s["user_id"] != uid}
            DB["rev"] += 1
            save_db(DB)
            self._json(200, {"ok": True})


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def main():
    init_db()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ip = lan_ip()
    print("=" * 56)
    print("  経営管理システム サーバー 起動中")
    print("=" * 56)
    print("  このPCから      : http://localhost:%d" % PORT)
    print("  同じWi-Fiの端末 : http://%s:%d   (スマホ・他PC)" % (ip, PORT))
    print("  初期管理者      : admin / admin123  （要パスワード変更）")
    print("  データ保存先    : %s" % DB_PATH)
    print("  停止            : Ctrl + C")
    print("=" * 56)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。")
        httpd.shutdown()


if __name__ == "__main__":
    main()
