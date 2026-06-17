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
import json, os, hashlib, hmac, secrets, threading, time, socket, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

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
    "sales":     ["sales"],
    "finance":   ["finance"],
    "tasks":     ["tasks", "businesses"],
    "contracts": ["contracts"],
    "people":    ["people", "businesses"],
}
# 各ページが編集できるデータ種別
PAGE_WRITE = {
    "business": ["businesses"], "sales": ["sales"], "finance": ["finance"],
    "breakeven": ["cost"], "tasks": ["tasks"], "contracts": ["contracts"], "people": ["people"],
    "dashboard": [], "analysis": [],
}
ALL_PAGES = list(PAGE_COLLECTIONS.keys())

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
    """サンプルの会社データ（フロントの初期データと同等）"""
    def gid(p): return p + secrets.token_hex(4)
    base = [12, 11.5, 13, 12.8, 14, 15.2, 14.5, 16, 15.8, 17, 18.5, 17.2]
    sales = []
    finance = []
    for i, v in enumerate(base):
        tot = round(v * 1000000)
        sales.append({"month": i, "total": tot,
                      "saas": round(tot * 0.45), "juchu": round(tot * 0.40), "consul": round(tot * 0.15)})
        cogs = round(tot * 0.48); sga = round(tot * 0.30) + (900000 if i % 3 == 0 else 0)
        finance.append({"month": i, "revenue": tot, "cogs": cogs, "sga": sga, "op": tot - cogs - sga})
    def days(n): return time.strftime("%Y-%m-%d", time.localtime(now() + n * 86400))
    return {
        "company": {"name": "株式会社サンプル", "fy": "2026", "cashTarget": 30000000},
        "businesses": [
            {"id": gid("b"), "name": "SaaS事業", "lead": "山田 太郎", "status": "成長", "target": 120000000, "actual": 98000000, "members": 8, "gmRate": 72},
            {"id": gid("b"), "name": "受託開発事業", "lead": "佐藤 花子", "status": "安定", "target": 80000000, "actual": 84000000, "members": 12, "gmRate": 38},
            {"id": gid("b"), "name": "コンサル事業", "lead": "鈴木 一郎", "status": "新規", "target": 30000000, "actual": 18000000, "members": 4, "gmRate": 55},
        ],
        "sales": sales,
        "finance": finance,
        "cost": {"fixedMonthly": 9800000, "variableRate": 0.42, "priceUnit": 50000},
        "tasks": [
            {"id": gid("t"), "title": "Q2 予算レビュー会議", "assignee": "山田 太郎", "due": days(3), "priority": "高", "status": "進行中", "biz": "全社"},
            {"id": gid("t"), "title": "新規顧客向け提案書作成", "assignee": "鈴木 一郎", "due": days(1), "priority": "高", "status": "未着手", "biz": "コンサル事業"},
            {"id": gid("t"), "title": "SaaS解約率の分析", "assignee": "山田 太郎", "due": days(7), "priority": "中", "status": "進行中", "biz": "SaaS事業"},
            {"id": gid("t"), "title": "受託案件Aの納品", "assignee": "佐藤 花子", "due": days(-2), "priority": "高", "status": "進行中", "biz": "受託開発事業"},
            {"id": gid("t"), "title": "採用面接（エンジニア2名）", "assignee": "人事部", "due": days(5), "priority": "中", "status": "未着手", "biz": "全社"},
            {"id": gid("t"), "title": "月次決算の確定", "assignee": "経理部", "due": days(10), "priority": "中", "status": "完了", "biz": "全社"},
        ],
        "contracts": [
            {"id": gid("c"), "client": "株式会社アルファ", "type": "保守契約", "amount": 2400000, "start": "2025-04-01", "end": days(20), "status": "有効", "auto": True},
            {"id": gid("c"), "client": "ベータ商事", "type": "SaaS年間", "amount": 3600000, "start": "2025-07-01", "end": days(95), "status": "有効", "auto": True},
            {"id": gid("c"), "client": "ガンマ製作所", "type": "受託開発", "amount": 12000000, "start": "2026-01-01", "end": days(8), "status": "有効", "auto": False},
            {"id": gid("c"), "client": "デルタ物流", "type": "コンサル", "amount": 4800000, "start": "2025-10-01", "end": days(-5), "status": "更新待ち", "auto": False},
            {"id": gid("c"), "client": "イプシロン", "type": "SaaS年間", "amount": 1800000, "start": "2026-02-01", "end": days(240), "status": "有効", "auto": True},
        ],
        "people": [
            {"id": gid("p"), "name": "山田 太郎", "role": "事業部長", "biz": "SaaS事業", "type": "正社員", "cost": 850000, "joined": "2020-04-01", "rating": "A"},
            {"id": gid("p"), "name": "佐藤 花子", "role": "PM", "biz": "受託開発事業", "type": "正社員", "cost": 720000, "joined": "2021-09-01", "rating": "A"},
            {"id": gid("p"), "name": "鈴木 一郎", "role": "コンサルタント", "biz": "コンサル事業", "type": "正社員", "cost": 680000, "joined": "2023-04-01", "rating": "B"},
            {"id": gid("p"), "name": "田中 美咲", "role": "エンジニア", "biz": "SaaS事業", "type": "正社員", "cost": 580000, "joined": "2022-04-01", "rating": "A"},
            {"id": gid("p"), "name": "高橋 健", "role": "エンジニア", "biz": "受託開発事業", "type": "業務委託", "cost": 700000, "joined": "2024-01-01", "rating": "B"},
            {"id": gid("p"), "name": "伊藤 葵", "role": "デザイナー", "biz": "SaaS事業", "type": "正社員", "cost": 520000, "joined": "2023-10-01", "rating": "B"},
        ],
    }

def fresh_db():
    salt, h = hash_pw("admin123")
    return {
        "rev": 1,
        "users": [{
            "id": "u" + secrets.token_hex(4), "username": "admin", "display_name": "管理者",
            "role": "admin", "pages": ALL_PAGES, "salt": salt, "hash": h, "created_at": now(),
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
            "role": u["role"], "pages": (ALL_PAGES if u["role"] == "admin" else u.get("pages", []))}

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
                 "role": role, "pages": pages, "salt": salt, "hash": h, "created_at": now()}
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
