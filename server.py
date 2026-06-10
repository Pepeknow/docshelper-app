# server.py — DocsHelper stable backend v1.1
# Backend без psycopg2. Используется современный psycopg[binary].
#
# Render Environment:
# DATABASE_URL=строка Neon PostgreSQL
# BACKEND_SECRET=любой длинный секрет, придумай сам
# ADMIN_PASSWORD=пароль владельца
# ALLOWED_ORIGIN=https://pepeknow.github.io
#
# Где взять DATABASE_URL:
# Neon → Project → Connection Details → pooled connection string.

import os
import uuid
import hmac
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from flask import Flask, request, jsonify
from flask_cors import CORS


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BACKEND_SECRET = os.getenv("BACKEND_SECRET", "change-me-please").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "https://pepeknow.github.io").strip()

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing. Add Neon DATABASE_URL in Render Environment.")

app = Flask(__name__)
CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [
                ALLOWED_ORIGIN,
                "https://pepeknow.github.io",
                "http://localhost:5500",
                "http://127.0.0.1:5500",
            ]
        }
    },
)

pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=8,
    kwargs={"row_factory": dict_row},
)


def now_utc():
    return datetime.now(timezone.utc)


def query(sql, params=None, one=False, commit=False):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            rows = None
            if cur.description:
                rows = cur.fetchall()
            if commit:
                conn.commit()
            if one:
                return rows[0] if rows else None
            return rows


def init_db():
    schema = """
    CREATE TABLE IF NOT EXISTS users (
        id UUID PRIMARY KEY,
        tg_id TEXT UNIQUE,
        email TEXT UNIQUE,
        name TEXT NOT NULL DEFAULT 'User',
        ref_code TEXT UNIQUE NOT NULL,
        invited_by TEXT,
        docscoin INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS subscriptions (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        plan TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        starts_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS projects (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'document',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS referrals (
        id UUID PRIMARY KEY,
        inviter_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        invited_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        reward_docscoin INTEGER NOT NULL DEFAULT 1000,
        status TEXT NOT NULL DEFAULT 'credited',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(inviter_id, invited_user_id)
    );

    CREATE TABLE IF NOT EXISTS payments (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        plan TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT 'cryptobot',
        provider_invoice_id TEXT,
        amount NUMERIC(12,2),
        asset TEXT DEFAULT 'USDT',
        status TEXT NOT NULL DEFAULT 'created',
        pay_url TEXT,
        raw JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_users_ref_code ON users(ref_code);
    CREATE INDEX IF NOT EXISTS idx_subscriptions_user_status ON subscriptions(user_id, status);
    CREATE INDEX IF NOT EXISTS idx_projects_user_created ON projects(user_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_payments_user_created ON payments(user_id, created_at DESC);
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(schema)
        conn.commit()
    logging.info("Database initialized")


def make_ref_code():
    return "dh_" + uuid.uuid4().hex[:10]


def sign_token(user_id):
    msg = str(user_id).encode("utf-8")
    sig = hmac.new(BACKEND_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"{user_id}.{sig}"


def verify_token(token):
    if not token or "." not in token:
        return None
    user_id, sig = token.rsplit(".", 1)
    expected = hmac.new(BACKEND_SECRET.encode("utf-8"), user_id.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return user_id


def require_user(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "").strip()
        user_id = verify_token(token)
        if not user_id:
            return api_error("unauthorized", 401)
        request.user_id = user_id
        return fn(*args, **kwargs)
    return wrapper


def api_ok(data=None, status=200):
    return jsonify({"ok": True, "data": data or {}}), status


def api_error(message, status=400, extra=None):
    payload = {"ok": False, "error": message}
    if extra:
        payload["extra"] = extra
    return jsonify(payload), status


def serialize_row(row):
    if not row:
        return None
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, (datetime,)):
            out[k] = v.isoformat()
        else:
            out[k] = str(v) if k.endswith("_id") or k == "id" else v
    return out


def get_active_subscription(user_id):
    return query(
        """
        SELECT plan, status, starts_at, expires_at
        FROM subscriptions
        WHERE user_id=%s AND status='active' AND expires_at > NOW()
        ORDER BY expires_at DESC
        LIMIT 1
        """,
        (user_id,),
        one=True,
    )


def user_payload(user):
    sub = get_active_subscription(user["id"])
    return {
        "id": str(user["id"]),
        "name": user["name"],
        "email": user["email"],
        "tg_id": user["tg_id"],
        "ref_code": user["ref_code"],
        "invited_by": user["invited_by"],
        "docscoin": user["docscoin"],
        "subscription": serialize_row(sub) if sub else None,
        "has_raffle_access": bool(sub) or bool(user["invited_by"]),
    }


@app.get("/")
def root():
    return jsonify({"ok": True, "service": "DocsHelper backend", "version": "1.1"})


@app.get("/health")
def health():
    try:
        row = query("SELECT 1 AS ok", one=True)
        return jsonify({"ok": True, "db": row["ok"] == 1, "version": "1.1"})
    except Exception as exc:
        logging.exception("health failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/auth/guest")
def auth_guest():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "Guest").strip()[:120]
    email = data.get("email")
    tg_id = data.get("tg_id")
    invited_by = data.get("ref")

    existing = None
    if tg_id:
        existing = query("SELECT * FROM users WHERE tg_id=%s", (str(tg_id),), one=True)
    if not existing and email:
        existing = query("SELECT * FROM users WHERE email=%s", (str(email).lower(),), one=True)

    if existing:
        token = sign_token(existing["id"])
        return api_ok({"token": token, "user": user_payload(existing)})

    inviter = None
    if invited_by:
        inviter = query("SELECT * FROM users WHERE ref_code=%s", (str(invited_by),), one=True)

    user = query(
        """
        INSERT INTO users (id, tg_id, email, name, ref_code, invited_by)
        VALUES (%s,%s,%s,%s,%s,%s)
        RETURNING *
        """,
        (
            str(uuid.uuid4()),
            str(tg_id) if tg_id else None,
            str(email).lower() if email else None,
            name,
            make_ref_code(),
            str(invited_by) if inviter else None,
        ),
        one=True,
        commit=True,
    )

    if inviter and str(inviter["id"]) != str(user["id"]):
        query(
            """
            INSERT INTO referrals (id, inviter_id, invited_user_id, reward_docscoin)
            VALUES (%s,%s,%s,1000)
            ON CONFLICT (inviter_id, invited_user_id) DO NOTHING
            """,
            (str(uuid.uuid4()), str(inviter["id"]), str(user["id"])),
            commit=True,
        )
        query(
            "UPDATE users SET docscoin = docscoin + 1000, updated_at=NOW() WHERE id=%s",
            (str(inviter["id"]),),
            commit=True,
        )

    token = sign_token(user["id"])
    fresh_user = query("SELECT * FROM users WHERE id=%s", (str(user["id"]),), one=True)
    return api_ok({"token": token, "user": user_payload(fresh_user)})


@app.get("/api/me")
@require_user
def me():
    user = query("SELECT * FROM users WHERE id=%s", (request.user_id,), one=True)
    if not user:
        return api_error("user_not_found", 404)
    return api_ok({"user": user_payload(user)})


@app.get("/api/projects")
@require_user
def list_projects():
    rows = query(
        """
        SELECT id, title, body, kind, created_at, updated_at
        FROM projects
        WHERE user_id=%s
        ORDER BY created_at DESC
        LIMIT 100
        """,
        (request.user_id,),
    )
    return api_ok({"projects": [serialize_row(r) for r in rows]})


@app.post("/api/projects")
@require_user
def create_project():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "Без названия").strip()[:200]
    body = str(data.get("body") or "").strip()
    kind = str(data.get("kind") or "document").strip()[:50]

    row = query(
        """
        INSERT INTO projects (id, user_id, title, body, kind)
        VALUES (%s,%s,%s,%s,%s)
        RETURNING id, title, body, kind, created_at, updated_at
        """,
        (str(uuid.uuid4()), request.user_id, title, body, kind),
        one=True,
        commit=True,
    )
    return api_ok({"project": serialize_row(row)}, 201)


@app.delete("/api/projects/<project_id>")
@require_user
def delete_project(project_id):
    query("DELETE FROM projects WHERE id=%s AND user_id=%s", (project_id, request.user_id), commit=True)
    return api_ok({"deleted": True})


@app.get("/api/referrals")
@require_user
def referrals():
    user = query("SELECT * FROM users WHERE id=%s", (request.user_id,), one=True)
    rows = query(
        """
        SELECT r.id, r.reward_docscoin, r.status, r.created_at, u.name AS invited_name
        FROM referrals r
        JOIN users u ON u.id = r.invited_user_id
        WHERE r.inviter_id=%s
        ORDER BY r.created_at DESC
        """,
        (request.user_id,),
    )
    return api_ok({
        "ref_code": user["ref_code"],
        "docscoin": user["docscoin"],
        "referrals": [serialize_row(r) for r in rows],
    })


@app.post("/api/admin/grant")
def admin_grant():
    data = request.get_json(silent=True) or {}
    password = str(data.get("password") or "")
    if not ADMIN_PASSWORD or password != ADMIN_PASSWORD:
        return api_error("bad_admin_password", 403)

    user_id = data.get("user_id")
    plan = str(data.get("plan") or "standard")
    days = int(data.get("days") or 30)

    user = query("SELECT * FROM users WHERE id=%s", (user_id,), one=True)
    if not user:
        return api_error("user_not_found", 404)

    expires = now_utc() + timedelta(days=days)
    sub = query(
        """
        INSERT INTO subscriptions (id, user_id, plan, status, starts_at, expires_at, source)
        VALUES (%s,%s,%s,'active',NOW(),%s,'admin')
        RETURNING *
        """,
        (str(uuid.uuid4()), user_id, plan, expires),
        one=True,
        commit=True,
    )
    return api_ok({"subscription": serialize_row(sub)})


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
