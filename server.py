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
import json, os, hashlib, hmac, secrets, threading, time, socket, sys, datetime, io, zipfile, html, base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR は環境変数で上書き可（クラウドの永続ディスクを指定するため）
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "db.json")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")   # 契約書等のファイル格納（永続ディスク）
MAX_UPLOAD = 15 * 1024 * 1024                     # 1ファイル最大15MB
APP_HTML = os.path.join(BASE_DIR, "app.html")
PORT = int(os.environ.get("PORT", "8000"))
SESSION_TTL = 60 * 60 * 24 * 14  # 14日

# ---- ページ定義（フロントと一致させる） ----
# 各ページが必要とするデータ種別（閲覧範囲の制御に使用）
PAGE_COLLECTIONS = {
    "dashboard": ["company", "businesses", "sales", "finance", "cost", "tasks", "contracts", "people", "inbox", "reminders"],
    "analysis":  ["company", "sales", "finance", "cost", "people"],
    "breakeven": ["cost", "finance"],
    "business":  ["businesses"],
    "sales":     ["sales", "businesses", "salesdeals"],
    "finance":   ["finance"],
    "tasks":     ["tasks", "businesses"],
    "contracts": ["contracts"],
    "people":    ["people", "businesses"],
    "tools":     ["tools", "bizsheets", "businesses"],
    "recruits":  ["recruits"],
    "cashflow":  ["cashflow", "banks", "cftxns"],   # 資金繰り（設定・口座マスタ・入出金明細）
    "memos":     ["memos", "contracts"],            # 打ち合わせメモ（クライアント検索で契約情報も参照）
    "placements":["placements", "businesses"],      # 人材提案・案件管理（BPO等・売上/報酬/粗利）
    "workspace": ["wsclients", "wstasks", "wsnotes"], # クライアント別ワークスペース（議事録＋タスク・進捗共有）
    "clients":   ["clients", "clientdocs", "contracts", "orders", "memos", "wstasks", "wsnotes", "deals", "documents", "placements", "customers", "businesses"], # クライアント管理（情報＋書類格納＋打合せを集約）
    # ── AI業務サポート（拡張） ──
    "inbox":      ["inbox", "contracts", "customers"],                 # 返信ボックス（LINE/メール/Slack/Chatwork）
    "salesai":    ["deals", "customers", "contracts", "memos"],        # 営業サポート（商談・顧客・AI提案）
    "docs":       ["documents", "orgprofile", "customers", "contracts", "businesses"],  # AI文書作成（見積/請求/提案/議事録/報告書）
    "billing":    ["documents", "customers"],                          # 請求・入金管理
    "expenses":   ["expenses", "people", "businesses"],                # 経費精算
    "timesheets": ["timesheets", "businesses", "people"],              # 勤怠・工数
    "reminders":  ["reminders"],                                       # 定型リマインド
}
# 各ページが編集できるデータ種別
PAGE_WRITE = {
    "business": ["businesses"], "sales": ["sales", "salesdeals"], "finance": ["finance"],
    "breakeven": ["cost"], "tasks": ["tasks"], "contracts": ["contracts"], "people": ["people"],
    "tools": ["tools", "bizsheets"], "recruits": ["recruits"], "cashflow": ["cashflow", "banks", "cftxns"], "memos": ["memos"], "placements": ["placements"],
    "workspace": ["wsclients", "wstasks", "wsnotes"], "clients": ["clients", "clientdocs", "contracts", "orders", "memos"],
    "dashboard": [], "analysis": [],
    "inbox": ["inbox"], "salesai": ["deals", "customers"], "docs": ["documents", "orgprofile"],
    "billing": ["documents"], "expenses": ["expenses"], "timesheets": ["timesheets"], "reminders": ["reminders"],
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


# =====================================================================
# AI業務サポート（拡張）：Claude API・チャネル連携・PPTX生成
#   秘匿情報は環境変数（優先）または DB["secrets"]（管理者が画面で登録）から解決する。
#     ANTHROPIC_API_KEY / CHATWORK_TOKEN / SLACK_BOT_TOKEN /
#     GMAIL_ADDRESS / GMAIL_APP_PASSWORD / CLAUDE_MODEL
# =====================================================================
ENV_SECRET = {
    "ai_key": "ANTHROPIC_API_KEY", "chatwork_token": "CHATWORK_TOKEN",
    "slack_token": "SLACK_BOT_TOKEN", "gmail_address": "GMAIL_ADDRESS",
    "gmail_password": "GMAIL_APP_PASSWORD",
}
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")


def get_secret(name):
    env = os.environ.get(ENV_SECRET.get(name, ""), "")
    if env:
        return env
    return (DB.get("secrets") or {}).get(name, "") if DB else ""


def srv_today():
    return datetime.date.today().strftime("%Y-%m-%d")


def _epoch_date(v):
    try:
        ts = float(str(v).split(".")[0])
        if ts > 1e10:  # ミリ秒っぽい値
            ts /= 1000.0
        return datetime.date.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return srv_today()


def claude_complete(system, messages, max_tokens=3000):
    key = get_secret("ai_key")
    if not key:
        raise RuntimeError("AIキーが未設定です。管理者が「設定 > AI・連携」で登録してください。")
    payload = {"model": CLAUDE_MODEL, "max_tokens": int(max_tokens),
               "system": system or "", "messages": messages}
    req = Request("https://api.anthropic.com/v1/messages",
                  data=json.dumps(payload).encode("utf-8"),
                  headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                           "content-type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            msg = json.loads(raw).get("error", {}).get("message", raw)
        except Exception:
            msg = raw
        if e.code == 401:
            raise RuntimeError("AIキーが無効です。設定を確認してください。")
        if e.code == 429:
            raise RuntimeError("AIのレート制限に達しました。少し待って再試行してください。")
        if e.code == 529:
            raise RuntimeError("AIが混雑しています。少し待って再試行してください。")
        raise RuntimeError("AI呼び出しエラー(%s): %s" % (e.code, msg))
    except URLError as e:
        raise RuntimeError("AIに接続できませんでした: %s" % getattr(e, "reason", e))
    if data.get("stop_reason") == "refusal":
        raise RuntimeError("AIがこのリクエストへの回答を控えました。内容を変えてお試しください。")
    txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return txt or "(応答が空でした)"


# ---- チャネル連携（返信ボックスの取り込み元） ----
def _http_json(url, headers=None, data=None, timeout=25):
    req = Request(url, data=data, headers=headers or {})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sync_chatwork():
    token = get_secret("chatwork_token")
    if not token:
        return [], "未設定"
    h = {"X-ChatWorkToken": token, "Accept": "application/json"}
    items = []
    try:
        tasks = _http_json("https://api.chatwork.com/v2/my/tasks?status=open", h)
        for t in (tasks or [])[:50]:
            body = (t.get("body") or "").strip().replace("\n", " ")
            room = (t.get("room") or {}).get("name", "Chatwork")
            items.append({"channel": "chatwork", "external_id": "cw-%s" % t.get("task_id"),
                          "sender": room, "subject": "要対応タスク", "snippet": body[:140],
                          "date": _epoch_date(t.get("limit_time")), "link": ""})
    except Exception as e:
        return items, "エラー: %s" % e
    return items, "ok"


def _slack_user(uid, headers, cache):
    if not uid:
        return "Slack"
    if uid in cache:
        return cache[uid]
    try:
        r = _http_json("https://slack.com/api/users.info?user=%s" % uid, headers)
        name = (r.get("user") or {}).get("real_name") or (r.get("user") or {}).get("name") or uid
    except Exception:
        name = uid
    cache[uid] = name
    return name


def sync_slack():
    token = get_secret("slack_token")
    if not token:
        return [], "未設定"
    h = {"Authorization": "Bearer " + token}
    items, cache = [], {}
    try:
        convs = _http_json("https://slack.com/api/conversations.list?types=im&limit=50", h)
        if not convs.get("ok"):
            return [], "エラー: %s" % convs.get("error")
        for ch in convs.get("channels", [])[:30]:
            hist = _http_json("https://slack.com/api/conversations.history?channel=%s&limit=1" % ch.get("id"), h)
            msgs = hist.get("messages", []) if hist.get("ok") else []
            if not msgs or msgs[0].get("bot_id"):
                continue
            m = msgs[0]
            items.append({"channel": "slack", "external_id": "sl-%s" % ch.get("id"),
                          "sender": _slack_user(ch.get("user"), h, cache), "subject": "DM",
                          "snippet": (m.get("text") or "")[:140], "date": _epoch_date(m.get("ts")), "link": ""})
    except Exception as e:
        return items, "エラー: %s" % e
    return items, "ok"


def sync_gmail():
    addr, pw = get_secret("gmail_address"), get_secret("gmail_password")
    if not (addr and pw):
        return [], "未設定"
    import imaplib, email
    from email.header import decode_header, make_header
    from email.utils import parsedate_to_datetime
    items = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(addr, pw)
        M.select("INBOX")
        typ, data = M.search(None, "UNSEEN")
        ids = (data[0].split() if data and data[0] else [])[-30:]
        for i in reversed(ids):
            typ, md = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            frm = str(make_header(decode_header(msg.get("From", "")))) if msg.get("From") else "(不明)"
            subj = str(make_header(decode_header(msg.get("Subject", "")))) if msg.get("Subject") else "(件名なし)"
            try:
                d = parsedate_to_datetime(msg.get("Date")).date().strftime("%Y-%m-%d")
            except Exception:
                d = srv_today()
            items.append({"channel": "email", "external_id": "gm-%s" % i.decode(),
                          "sender": frm[:80], "subject": subj[:120], "snippet": "", "date": d, "link": ""})
        M.logout()
    except Exception as e:
        return items, "エラー: %s" % e
    return items, "ok"


def run_inbox_sync():
    """全チャネルを取得し、external_id で重複排除して DB.store.inbox にマージ。"""
    results, status = [], {}
    for name, fn in (("chatwork", sync_chatwork), ("slack", sync_slack), ("email", sync_gmail)):
        try:
            got, st = fn()
        except Exception as e:
            got, st = [], "エラー: %s" % e
        results += got
        status[name] = st
    added = 0
    with LOCK:
        inbox = DB["store"].setdefault("inbox", [])
        existing = {x.get("external_id") for x in inbox if x.get("external_id")}
        for it in results:
            if it.get("external_id") and it["external_id"] in existing:
                continue
            inbox.insert(0, {"id": "in" + secrets.token_hex(4), "status": "未対応",
                             "reply": "", **it})
            existing.add(it.get("external_id"))
            added += 1
        if added:
            DB["rev"] += 1
            save_db(DB)
    return {"added": added, "status": status}


# ---- PPTX 生成（Python標準ライブラリの zipfile のみ・OOXML最小構成） ----
def _xesc(s):
    return html.escape(str(s or ""), quote=True)


def build_pptx(title, slides):
    """slides: [{"title": str, "bullets": [str,...]}] → .pptx のバイト列を返す。"""
    W, H = 9144000, 6858000  # 10in x 7.5in (EMU)
    CT = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>',
          '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>',
          '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>',
          '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>']
    n = max(1, len(slides))
    for i in range(1, n + 1):
        CT.append('<Override PartName="/ppt/slides/slide%d.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>' % i)
    CT.append('</Types>')

    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
                 '</Relationships>')

    sldid = "".join('<p:sldId id="%d" r:id="rId%d"/>' % (255 + i, i) for i in range(1, n + 1))
    presentation = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                    '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId%d"/></p:sldMasterIdLst>' % (n + 1) +
                    '<p:sldIdLst>' + sldid + '</p:sldIdLst>'
                    '<p:sldSz cx="%d" cy="%d" type="screen4x3"/>' % (W, H) +
                    '<p:notesSz cx="%d" cy="%d"/></p:presentation>' % (H, W))

    pres_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i in range(1, n + 1):
        pres_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide%d.xml"/>' % (i, i))
    pres_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>' % (n + 1))
    pres_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>' % (n + 2))
    pres_rels.append('</Relationships>')

    theme = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office">'
             '<a:themeElements><a:clrScheme name="Office">'
             '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
             '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
             '<a:dk2><a:srgbClr val="1F2A44"/></a:dk2><a:lt2><a:srgbClr val="EEECE1"/></a:lt2>'
             '<a:accent1><a:srgbClr val="4F8CFF"/></a:accent1><a:accent2><a:srgbClr val="22C55E"/></a:accent2>'
             '<a:accent3><a:srgbClr val="A78BFA"/></a:accent3><a:accent4><a:srgbClr val="F59E0B"/></a:accent4>'
             '<a:accent5><a:srgbClr val="06B6D4"/></a:accent5><a:accent6><a:srgbClr val="EF4444"/></a:accent6>'
             '<a:hlink><a:srgbClr val="4F8CFF"/></a:hlink><a:folHlink><a:srgbClr val="A78BFA"/></a:folHlink></a:clrScheme>'
             '<a:fontScheme name="Office"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
             '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
             '<a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
             '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
             '<a:lnStyleLst><a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
             '<a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
             '<a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>'
             '<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle>'
             '<a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>'
             '<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
             '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>'
             '</a:fmtScheme></a:themeElements></a:theme>')

    master = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
              'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
              'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
              '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
              '<p:grpSpPr/></p:spTree></p:cSld><p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" '
              'accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
              'accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
              '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>')
    master_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>'
                   '</Relationships>')
    layout = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
              'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
              'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">'
              '<p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
              '<p:grpSpPr/></p:spTree></p:cSld><p:clrMapOvr><a:overrideClrMapping bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" '
              'accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
              'accent6="accent6" hlink="hlink" folHlink="folHlink"/></p:clrMapOvr></p:sldLayout>')
    layout_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
                   '</Relationships>')

    def title_box(text):
        return ('<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
                '<p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="457200" y="365760"/>'
                '<a:ext cx="8229600" cy="1143000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
                '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="ja-JP" sz="3200" b="1">'
                '<a:solidFill><a:srgbClr val="1F2A44"/></a:solidFill></a:rPr><a:t>%s</a:t></a:r></a:p></p:txBody></p:sp>' % _xesc(text))

    def body_box(bullets):
        paras = []
        for bl in (bullets or []):
            paras.append('<a:p><a:pPr><a:buChar char="&#8226;"/></a:pPr><a:r><a:rPr lang="ja-JP" sz="1800"/>'
                         '<a:t>%s</a:t></a:r></a:p>' % _xesc(bl))
        if not paras:
            paras.append('<a:p><a:endParaRPr lang="ja-JP"/></a:p>')
        return ('<p:sp><p:nvSpPr><p:cNvPr id="3" name="Content"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
                '<p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="457200" y="1600200"/>'
                '<a:ext cx="8229600" cy="4800600"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
                '<p:txBody><a:bodyPr/><a:lstStyle/>' + "".join(paras) + '</p:txBody></p:sp>')

    slide_xmls = []
    for s in (slides or [{"title": title, "bullets": []}]):
        sx = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
              'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
              'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
              '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
              '<p:grpSpPr/>' + title_box(s.get("title", "")) + body_box(s.get("bullets")) +
              '</p:spTree></p:cSld><p:clrMapOvr><a:overrideClrMapping bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" '
              'accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
              'accent6="accent6" hlink="hlink" folHlink="folHlink"/></p:clrMapOvr></p:sld>')
        slide_xmls.append(sx)
    slide_rel = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
                 '</Relationships>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "".join(CT))
        z.writestr("_rels/.rels", root_rels)
        z.writestr("ppt/presentation.xml", presentation)
        z.writestr("ppt/_rels/presentation.xml.rels", "".join(pres_rels))
        z.writestr("ppt/theme/theme1.xml", theme)
        z.writestr("ppt/slideMasters/slideMaster1.xml", master)
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", master_rels)
        z.writestr("ppt/slideLayouts/slideLayout1.xml", layout)
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", layout_rels)
        for i, sx in enumerate(slide_xmls, 1):
            z.writestr("ppt/slides/slide%d.xml" % i, sx)
            z.writestr("ppt/slides/_rels/slide%d.xml.rels" % i, slide_rel)
    return buf.getvalue()


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

def _next_monthly(day):
    """毎月 day 日の次回発生日を YYYY-MM-DD で返す（今日以降の直近）。"""
    today = datetime.date.today()
    y, m = today.year, today.month
    day = max(1, min(28, int(day)))
    if today.day > day:
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return "%04d-%02d-%02d" % (y, m, day)


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
        "placements": [
            {"id": gid("pl"), "biz": "BPO事業", "name": "候補者A", "client": "取引先A", "status": "稼働中", "proposal": "内定", "proposeDate": days(-40), "closeDate": days(-20), "revenue": 600000, "cost": 420000, "note": "経理BPO・月次"},
            {"id": gid("pl"), "biz": "BPO事業", "name": "候補者B", "client": "取引先E", "status": "提案中", "proposal": "一次面談", "proposeDate": days(-8), "closeDate": "", "revenue": 500000, "cost": 350000, "note": "カスタマーサポート"},
            {"id": gid("pl"), "biz": "BPO事業", "name": "候補者C", "client": "取引先A", "status": "成約", "proposal": "内定", "proposeDate": days(-15), "closeDate": days(-2), "revenue": 450000, "cost": 300000, "note": "データ入力"},
        ],
        # 売上案件（案件単位の売上明細）。登録すると各月・各事業の売上に自動集計される
        "salesdeals": [],
        # ── クライアント管理（CRM・会社マスタ／360°ビュー）──
        "clients": [
            {"id": gid("cl"), "name": "取引先A", "kana": "とりひきさきエー", "person": "田中 一郎", "role": "経営企画部長",
             "tel": "03-1111-2222", "email": "tanaka@example.com", "address": "東京都千代田区…", "website": "https://example.com",
             "status": "既存", "biz": "BPO事業", "owner": "高嶋", "source": "紹介", "amount": 24000000,
             "nextDate": days(5), "nextAction": "追加見積のフォロー", "tags": ["定例あり", "優良"], "note": "毎月定例。追加案件の見込み高い。", "updated": now()},
            {"id": gid("cl"), "name": "取引先C", "kana": "とりひきさきシー", "person": "佐藤 花子", "role": "人事責任者",
             "tel": "06-3333-4444", "email": "sato@example.com", "address": "大阪府大阪市…", "website": "",
             "status": "商談中", "biz": "SES事業", "owner": "早田", "source": "問い合わせ", "amount": 12000000,
             "nextDate": days(1), "nextAction": "エンジニア単価の提示", "tags": ["増員案件"], "note": "エンジニア2名の増員を調整中。", "updated": now()},
            {"id": gid("cl"), "name": "見込みD社", "kana": "みこみディーしゃ", "person": "鈴木 部長", "role": "採用部長",
             "tel": "03-5555-6666", "email": "", "address": "", "website": "",
             "status": "見込み", "biz": "RPO事業", "owner": "高嶋", "source": "展示会・イベント", "amount": 0,
             "nextDate": days(3), "nextAction": "初回提案アポの調整", "tags": ["新規"], "note": "展示会で名刺交換。採用強化に関心。", "updated": now()},
        ],
        # クライアント別 書類格納（契約書／秘密保持／発注書／その他・ファイルアップロード or リンク）
        "clientdocs": [
            {"id": gid("cd"), "client": "取引先A", "type": "契約書",   "name": "業務委託基本契約書", "date": days(-40), "fileId": "", "fileName": "", "url": "", "note": ""},
            {"id": gid("cd"), "client": "取引先A", "type": "秘密保持", "name": "NDA",                 "date": days(-42), "fileId": "", "fileName": "", "url": "", "note": ""},
        ],
        # 発注書（旧・クライアント別）※書類格納に統合。既存データ保持のため定義は残す
        "orders": [
            {"id": gid("or"), "client": "取引先A", "name": "BPO業務 発注書 4月分", "date": days(-25), "amount": 2400000, "url": "", "note": ""},
            {"id": gid("or"), "client": "取引先C", "name": "SESエンジニア 発注書", "date": days(-4), "amount": 1600000, "url": "", "note": "単価80万×2名"},
        ],
        # ── クライアント別ワークスペース（Notion風・議事録＋タスクを進捗共有）──
        "wsclients": [
            {"id": gid("wc"), "name": "取引先A", "owner": "高嶋", "color": "#6366f1", "note": "BPO定例あり", "archived": False},
            {"id": gid("wc"), "name": "取引先C", "owner": "早田", "color": "#10b981", "note": "SES増員案件", "archived": False},
        ],
        "wstasks": [
            {"id": gid("wt"), "client": "取引先A", "title": "追加見積の作成・送付", "assignee": "高嶋", "status": "進行中", "due": days(2), "priority": "高", "note": "BPO追加業務ぶん", "updated": now()},
            {"id": gid("wt"), "client": "取引先A", "title": "契約更新の確認", "assignee": "早田", "status": "未着手", "due": days(9), "priority": "中", "note": "", "updated": now()},
            {"id": gid("wt"), "client": "取引先C", "title": "エンジニア単価の調整", "assignee": "早田", "status": "レビュー", "due": days(1), "priority": "高", "note": "80万×2名で提示予定", "updated": now()},
            {"id": gid("wt"), "client": "取引先C", "title": "稼働開始日の確定", "assignee": "高嶋", "status": "完了", "due": days(-1), "priority": "中", "note": "来月1日開始で合意", "updated": now()},
        ],
        "wsnotes": [
            {"id": gid("wn"), "client": "取引先A", "date": days(-3), "title": "定例MTG", "attendees": "高嶋 / 田中様",
             "body": "・進捗共有\n・追加見積の依頼あり\n・次回までに提出", "url": "", "updated": now()},
            {"id": gid("wn"), "client": "取引先C", "date": days(-1), "title": "SES増員のご相談", "attendees": "早田 / 佐藤様",
             "body": "・エンジニア2名の増員\n・単価と開始時期を調整\n・来月開始の方向", "url": "", "updated": now()},
        ],
        "tools": [
            {"id": gid("k"), "name": "Slack", "url": "https://slack.com", "category": "コミュニケーション", "icon": "💬"},
            {"id": gid("k"), "name": "LINE", "url": "https://line.me/", "category": "コミュニケーション", "icon": "🟢"},
            {"id": gid("k"), "name": "LINE公式アカウント", "url": "https://manager.line.biz/", "category": "コミュニケーション", "icon": "📢"},
            {"id": gid("k"), "name": "Chatwork", "url": "https://www.chatwork.com/", "category": "コミュニケーション", "icon": "💭"},
            {"id": gid("k"), "name": "Messenger", "url": "https://www.messenger.com/", "category": "コミュニケーション", "icon": "🗨️"},
            {"id": gid("k"), "name": "Gmail", "url": "https://mail.google.com", "category": "コミュニケーション", "icon": "✉️"},
            {"id": gid("k"), "name": "Google ドキュメント", "url": "https://docs.google.com", "category": "Googleドライブ", "icon": "📝"},
            {"id": gid("k"), "name": "Google スプレッドシート", "url": "https://sheets.google.com", "category": "Googleドライブ", "icon": "📊"},
            {"id": gid("k"), "name": "Google ドライブ", "url": "https://drive.google.com", "category": "Googleドライブ", "icon": "📁"},
            {"id": gid("k"), "name": "Google カレンダー", "url": "https://calendar.google.com", "category": "カレンダー", "icon": "📅"},
            {"id": gid("k"), "name": "freee 会計", "url": "https://secure.freee.co.jp", "category": "その他", "icon": "💴"},
            {"id": gid("k"), "name": "Notion", "url": "https://www.notion.so", "category": "その他", "icon": "📝"},
        ],
        # 各事業のスプレッドシート格納場所（事業名 → URL）
        "bizsheets": {},
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
        # ── AI業務サポート（拡張）の初期データ ──
        "inbox": [
            {"id": gid("in"), "channel": "email", "sender": "取引先A 田中様", "subject": "追加見積のご依頼",
             "snippet": "先日はありがとうございました。追加でBPO業務の見積もお願いできますでしょうか。", "date": days(0),
             "status": "未対応", "link": "", "external_id": "", "reply": ""},
            {"id": gid("in"), "channel": "chatwork", "sender": "取引先C 佐藤様", "subject": "SES増員の件",
             "snippet": "エンジニア2名の増員、来月から可能でしょうか?単価もご相談したいです。", "date": days(-1),
             "status": "未対応", "link": "", "external_id": "", "reply": ""},
            {"id": gid("in"), "channel": "slack", "sender": "社内: 経理チーム", "subject": "経費精算の締め",
             "snippet": "今月の経費精算、25日締めです。未提出の方はお願いします。", "date": days(-1),
             "status": "対応済み", "link": "", "external_id": "", "reply": ""},
        ],
        "customers": [
            {"id": gid("cu"), "name": "取引先A", "person": "田中 一郎", "contact": "tanaka@example.com",
             "status": "既存顧客", "biz": "BPO事業", "memo": "毎月定例あり。追加案件の見込み高い。"},
            {"id": gid("cu"), "name": "取引先C", "person": "佐藤 花子", "contact": "sato@example.com",
             "status": "既存顧客", "biz": "SES事業", "memo": "増員ニーズ継続。単価交渉中。"},
            {"id": gid("cu"), "name": "見込みD社", "person": "鈴木 部長", "contact": "03-0000-0000",
             "status": "見込み", "biz": "RPO事業", "memo": "展示会で名刺交換。採用強化に関心。"},
        ],
        "deals": [
            {"id": gid("dl"), "customer": "取引先A", "title": "BPO追加業務", "amount": 1800000, "stage": "提案",
             "next": days(2), "memo": "追加見積を提出予定。", "biz": "BPO事業"},
            {"id": gid("dl"), "customer": "取引先C", "title": "SESエンジニア2名増員", "amount": 9600000, "stage": "商談中",
             "next": days(1), "memo": "単価と開始時期を調整中。", "biz": "SES事業"},
            {"id": gid("dl"), "customer": "見込みD社", "title": "RPO新規契約", "amount": 3600000, "stage": "リード",
             "next": days(5), "memo": "初回提案アポ調整中。", "biz": "RPO事業"},
        ],
        "documents": [
            {"id": gid("doc"), "type": "請求書", "number": "INV-2026-0001", "client": "取引先A", "date": days(-20),
             "due": days(-5), "items": [{"name": "BPO業務委託 4月分", "qty": 1, "unit": 2400000}],
             "taxRate": 10, "note": "お振込手数料は御社にてご負担ください。", "status": "入金待ち", "paidDate": ""},
            {"id": gid("doc"), "type": "見積書", "number": "EST-2026-0001", "client": "取引先C", "date": days(-3),
             "due": "", "items": [{"name": "SESエンジニア(単価80万×2名)", "qty": 2, "unit": 800000}],
             "taxRate": 10, "note": "有効期限: 発行から30日", "status": "作成済み", "paidDate": ""},
        ],
        "orgprofile": {
            "company": "自社株式会社", "address": "東京都〇〇区〇〇 1-2-3", "tel": "03-1234-5678",
            "email": "info@example.com", "person": "経営 太郎", "bank": "〇〇銀行 〇〇支店 普通 1234567",
            "seal": "", "logo": "", "service": "BPO/RPO/SES/ライバー事業の受託・支援",
            "strength": "実績多数・柔軟な体制・スピード納品"
        },
        "expenses": [
            {"id": gid("ex"), "date": days(-2), "applicant": "経営 太郎", "category": "交通費", "amount": 1240,
             "biz": "全社", "note": "客先訪問（取引先A）", "status": "申請中", "receipt": ""},
            {"id": gid("ex"), "date": days(-6), "applicant": "経営 太郎", "category": "会議費", "amount": 8800,
             "biz": "SES事業", "note": "取引先Cとの商談ランチ", "status": "承認済み", "receipt": ""},
        ],
        "timesheets": [
            {"id": gid("ts"), "date": days(0), "member": "経営 太郎", "biz": "BPO事業", "task": "追加見積作成", "hours": 2.5, "note": ""},
            {"id": gid("ts"), "date": days(0), "member": "経営 太郎", "biz": "SES事業", "task": "増員調整MTG", "hours": 1.0, "note": ""},
            {"id": gid("ts"), "date": days(-1), "member": "経営 太郎", "biz": "全社", "task": "月次処理", "hours": 3.0, "note": ""},
        ],
        "reminders": [
            {"id": gid("rm"), "title": "月次請求書の発行", "cycle": "毎月", "day": 25, "next": _next_monthly(25), "category": "経理", "note": "全クライアント分", "active": True},
            {"id": gid("rm"), "title": "経費精算の締め処理", "cycle": "毎月", "day": 25, "next": _next_monthly(25), "category": "経理", "note": "", "active": True},
            {"id": gid("rm"), "title": "契約更新チェック", "cycle": "毎月", "day": 1, "next": _next_monthly(1), "category": "契約", "note": "更新30日前アラート確認", "active": True},
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
        # 秘匿情報（APIキー等）はここに保存し、/api/store では返さない
        "secrets": {"ai_key": "", "chatwork_token": "", "slack_token": "",
                    "gmail_address": "", "gmail_password": "", "frictio_token": ""},
        # アップロードファイルのメタ情報（実体は UPLOAD_DIR に保存）。/api/store では返さない
        "files": {},
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
    # 既存DBへのマイグレーション：新コレクション／秘匿領域を補完（既存データは保持）
    seeds = seed_store()
    store = DB.setdefault("store", {})
    changed = False
    for k, v in seeds.items():
        if k not in store:
            store[k] = v
            changed = True
    if "secrets" not in DB:
        DB["secrets"] = {"ai_key": "", "chatwork_token": "", "slack_token": "",
                         "gmail_address": "", "gmail_password": "", "frictio_token": ""}
        changed = True
    if "files" not in DB:
        DB["files"] = {}
        changed = True
    # 既存ユーザーの pages に新ページを自動付与しない（権限は管理者が明示的に設定）
    if changed:
        DB["rev"] = DB.get("rev", 1) + 1
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
        if path.startswith("/api/file/"):
            return self._api_file(path[len("/api/file/"):])
        if path == "/api/cashflow":
            return self._api_cashflow(urlparse(self.path).query)
        if path == "/api/users":
            return self._api_users_list()
        if path == "/api/integrations":
            return self._api_integrations_get()
        if path == "/api/inbox/sync":
            return self._api_inbox_sync()
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
        if path == "/api/ai":
            return self._api_ai()
        if path == "/api/upload":
            return self._api_upload()
        if path == "/api/integrations":
            return self._api_integrations_set()
        if path == "/api/pptx":
            return self._api_pptx()
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

    # ---- ファイル格納（契約書・秘密保持・発注書 等）----
    def _api_upload(self):
        u = self._current_user()
        if not u:
            return self._err(401, "未ログイン")
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0:
            return self._err(400, "ファイルがありません")
        # base64のオーバーヘッド（約1.37倍）を見込んで上限を判定
        if n > int(MAX_UPLOAD * 1.4) + 4096:
            return self._err(413, "ファイルが大きすぎます（1ファイル最大15MB）")
        d = self._body()
        name = (d.get("name") or "file").strip() or "file"
        data = d.get("data") or ""
        if data.startswith("data:") and "," in data:
            data = data.split(",", 1)[1]
        try:
            raw = base64.b64decode(data)
        except Exception:
            return self._err(400, "ファイルの形式が不正です")
        if not raw:
            return self._err(400, "ファイルが空です")
        if len(raw) > MAX_UPLOAD:
            return self._err(413, "ファイルが大きすぎます（1ファイル最大15MB）")
        fid = "f" + secrets.token_hex(10)
        ext = os.path.splitext(name)[1]
        if len(ext) > 12 or "/" in ext or "\\" in ext:
            ext = ""
        stored = fid + ext
        try:
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            with open(os.path.join(UPLOAD_DIR, stored), "wb") as f:
                f.write(raw)
        except OSError as e:
            return self._err(500, "保存に失敗しました: %s" % e)
        mime = (d.get("mime") or "application/octet-stream").strip()[:120]
        with LOCK:
            DB.setdefault("files", {})[fid] = {
                "name": name, "mime": mime, "stored": stored,
                "size": len(raw), "uploaded": now(), "by": u["id"]}
            save_db(DB)
        return self._json(200, {"ok": True, "id": fid, "name": name, "size": len(raw)})

    def _api_file(self, fid):
        u = self._current_user()
        if not u:
            return self._err(401, "未ログイン")
        fid = (fid or "").split("?")[0].split("/")[0]
        with LOCK:
            meta = (DB.get("files") or {}).get(fid)
        if not meta:
            return self._err(404, "ファイルが見つかりません")
        path = os.path.join(UPLOAD_DIR, meta.get("stored", ""))
        if not os.path.abspath(path).startswith(os.path.abspath(UPLOAD_DIR)) or not os.path.exists(path):
            return self._err(404, "ファイル本体がありません")
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError as e:
            return self._err(500, "読み込みに失敗しました: %s" % e)
        mime = meta.get("mime") or "application/octet-stream"
        from urllib.parse import quote as _q
        fn = _q(meta.get("name") or "file")
        disp = "inline" if (mime.startswith("application/pdf") or mime.startswith("image/")) else "attachment"
        self._send(200, body, ctype=mime,
                   extra=[("Content-Disposition", "%s; filename*=UTF-8''%s" % (disp, fn))])

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

    # ---- AI業務サポート（拡張） ----
    def _api_ai(self):
        u = self._current_user()
        if not u:
            return self._err(401, "未ログイン")
        d = self._body()
        system = d.get("system") or ""
        messages = d.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._err(400, "messages が必要です")
        # 安全のためロール/本文のみ通す
        clean = []
        for m in messages:
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
                clean.append({"role": m["role"], "content": m["content"]})
        if not clean:
            return self._err(400, "messages の形式が不正です")
        max_tokens = min(int(d.get("max_tokens") or 3000), 8000)
        try:
            text = claude_complete(system, clean, max_tokens)
            self._json(200, {"text": text})
        except Exception as e:
            self._err(502, str(e))

    def _api_pptx(self):
        u = self._current_user()
        if not u:
            return self._err(401, "未ログイン")
        d = self._body()
        title = (d.get("title") or "資料").strip()
        slides = d.get("slides")
        if not isinstance(slides, list) or not slides:
            return self._err(400, "slides が必要です")
        norm = []
        for s in slides[:60]:
            if not isinstance(s, dict):
                continue
            bl = [str(x) for x in (s.get("bullets") or []) if str(x).strip()][:12]
            norm.append({"title": str(s.get("title") or ""), "bullets": bl})
        try:
            data = build_pptx(title, norm)
        except Exception as e:
            return self._err(500, "PPTX生成に失敗しました: %s" % e)
        fname = "presentation.pptx"
        self._send(200, data,
                   ctype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                   extra=[("Content-Disposition", 'attachment; filename="%s"' % fname)])

    def _api_inbox_sync(self):
        u = self._current_user()
        if not u:
            return self._err(401, "未ログイン")
        pages = ALL_PAGES if u["role"] == "admin" else u.get("pages", [])
        if "inbox" not in pages:
            return self._err(403, "返信ボックスの権限がありません")
        try:
            res = run_inbox_sync()
            self._json(200, {"ok": True, **res, "rev": DB["rev"]})
        except Exception as e:
            self._err(500, str(e))

    def _api_integrations_get(self):
        if not self._require_admin():
            return
        s = DB.get("secrets") or {}
        def configured(name):
            return bool(os.environ.get(ENV_SECRET.get(name, ""), "") or s.get(name, ""))
        self._json(200, {"ai": configured("ai_key"), "chatwork": configured("chatwork_token"),
                         "slack": configured("slack_token"),
                         "gmail": configured("gmail_address") and configured("gmail_password"),
                         "gmail_address": s.get("gmail_address", ""),
                         "model": CLAUDE_MODEL,
                         "env_locked": {k: bool(os.environ.get(v, "")) for k, v in ENV_SECRET.items()}})

    def _api_integrations_set(self):
        if not self._require_admin():
            return
        d = self._body()
        with LOCK:
            sec = DB.setdefault("secrets", {})
            for k in ("ai_key", "chatwork_token", "slack_token", "gmail_address", "gmail_password"):
                if k in d and isinstance(d[k], str):
                    sec[k] = d[k].strip()
            save_db(DB)
        self._api_integrations_get()

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
