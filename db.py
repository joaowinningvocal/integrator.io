"""
Camada de dados do hub. Tudo que toca o banco passa por aqui.
Trocar SQLite por Postgres depois = reescrever só este arquivo.
"""

import json
import os
import re
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from flask import g


def _resolve_data_dir() -> Path:
    """No Railway o volume fica em /data. Local, cai pra ./data."""
    candidate = Path(os.getenv("DATA_DIR", "/data"))
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return candidate
    except OSError:
        fallback = Path(__file__).parent / "data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "hub.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    token         TEXT NOT NULL,
    sender_number TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    label    TEXT NOT NULL,
    norm_key TEXT NOT NULL,
    price    TEXT,
    link     TEXT NOT NULL,
    UNIQUE (venue_id, norm_key)
);

CREATE TABLE IF NOT EXISTS templates (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    body     TEXT NOT NULL,
    UNIQUE (venue_id, name)
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id        INTEGER REFERENCES venues(id) ON DELETE SET NULL,
    slug            TEXT,
    received_at     TEXT NOT NULL,
    remote_ip       TEXT,
    content_type    TEXT,
    headers         TEXT,
    body            TEXT,
    call_session_id TEXT,
    customer_phone  TEXT,
    agent_did       TEXT,
    package         TEXT,
    first_name      TEXT,
    matched_link    TEXT,
    preview_body    TEXT,
    preview_from    TEXT,
    status          TEXT NOT NULL,
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_received ON events(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_session  ON events(venue_id, call_session_id);
CREATE INDEX IF NOT EXISTS idx_events_status   ON events(status);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    # dry_run -> só registra o que enviaria
    # test    -> envia apenas pra allowlist  (Fase 3)
    # live    -> envia de verdade            (Fase 4)
    "mode": "dry_run",
    "test_allowlist": "",
}

# --------------------------------------------------------------------------
# Seed inicial: as duas venues que entram primeiro.
# Os rodapés vieram do texto atual do Make e precisam de conferência.
# --------------------------------------------------------------------------

HUSTLER_TEMPLATE = """Hey {{first_name|there}},
This is the link for you to book your reservation:
{{link}}
This SMS was sent by Heather.
Larry Flynt's Hustler Club
6007 Dean Martin Drive
(702) 795-3131"""

KINGS_TEMPLATE = """Hey {{first_name|there}},
This is the link for you to book your reservation:
{{link}}
This SMS was sent by Ace.
Kings of Hustler
6007 Dean Martin Drive
(702) 795-3131"""

SEED_VENUES = [
    {
        "slug": "hustler-lv",
        "name": "Hustler Club Las Vegas",
        "sender_number": "+17029970961",
        "template": HUSTLER_TEMPLATE,
        "packages": [
            ("Free Ride and Entry Pass", "$0", "https://app.cartvip.com/vegashustlerclub/package/free-ride-and-entry-pass-32/checkout"),
            ("$20 Special", "$20", "https://app.cartvip.com/vegashustlerclub/package/20-special-1/checkout"),
            ("Just the Two of Us", "$150", "https://app.cartvip.com/vegashustlerclub/package/just-the-two-of-us-30/checkout"),
            ("Couch with a View", "$250", "https://app.cartvip.com/vegashustlerclub/package/couch-with-a-view-25/checkout"),
            ("Blowout Fest", "$450", "https://app.cartvip.com/vegashustlerclub/package/blowout-fest-26/checkout"),
            ("What Happens In Vegas", "$800", "https://app.cartvip.com/vegashustlerclub/package/what-happens-in-vegas-28/checkout"),
            ("Guaranteed Over The Top Experience", "$1200", "https://app.cartvip.com/vegashustlerclub/package/over-the-top-29/checkout"),
        ],
    },
    {
        "slug": "kings",
        "name": "Kings of Hustler",
        "sender_number": "+17025471904",
        "template": KINGS_TEMPLATE,
        "packages": [
            ("Free Ride and Free Entry", "$0", "https://app.cartvip.com/kingsofhustler/package/free-ride-and-free-entry-40/checkout"),
            ("Showstopper", "$50", "https://app.cartvip.com/kingsofhustler/package/showstopper-41/checkout"),
            ("Bad Mom's Club", "$300", "https://app.cartvip.com/kingsofhustler/package/bad-moms-club-33/checkout"),
            ("Champagne with a King", "$400", "https://app.cartvip.com/kingsofhustler/package/champagne-with-a-king-13/checkout"),
            ("Rosè All Day", "$700", "https://app.cartvip.com/kingsofhustler/package/rose-all-day-35/checkout"),
            ("Screaming Orgasm", "$900", "https://app.cartvip.com/kingsofhustler/package/screaming-orgasm-36/checkout"),
            ("One Last Hoerahh", "$1400", "https://app.cartvip.com/kingsofhustler/package/one-last-hoerahh-37/checkout"),
            ("Bride and Boujee", "$1800", "https://app.cartvip.com/kingsofhustler/package/bride-and-boujee-38/checkout"),
            ("One King Forever!", "$2000", "https://app.cartvip.com/kingsofhustler/package/one-king-forever-39/checkout"),
        ],
    },
]


# ---------- utilidades ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_token() -> str:
    return secrets.token_urlsafe(24)


def norm_key(text: str) -> str:
    """
    Normaliza o nome do pacote pra casar apesar de caixa, acento e pontuação.
    "$20 SPECIAL" e "$20 Special" -> "20 special"
    "Rosè All Day"               -> "rose all day"
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def to_e164(raw: str) -> str:
    """Normaliza telefone US pra E.164. Devolve vazio se não der."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if str(raw).strip().startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    return ""


def pretty_json(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except Exception:
        return raw or ""


def sms_segments(body: str) -> tuple[int, int, str]:
    """(caracteres, segmentos, encoding). GSM-7 vs UCS-2 muda o tamanho do segmento."""
    if not body:
        return 0, 0, "GSM-7"
    gsm = set(
        "@£$¥èéùìòÇØøÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
        "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
        "\n\r"
    )
    extended = set("^{}\\[~]|€")
    is_gsm = all(c in gsm or c in extended for c in body)
    if is_gsm:
        length = sum(2 if c in extended else 1 for c in body)
        single, multi = 160, 153
        enc = "GSM-7"
    else:
        length = len(body)
        single, multi = 70, 67
        enc = "UCS-2"
    segments = 1 if length <= single else -(-length // multi)
    return length, segments, enc


# ---------- conexão ----------

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    for v in SEED_VENUES:
        row = conn.execute("SELECT id FROM venues WHERE slug = ?", (v["slug"],)).fetchone()
        if row:
            venue_id = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO venues (slug, name, token, sender_number, active, created_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (v["slug"], v["name"], new_token(), v["sender_number"], now_iso()),
            )
            venue_id = cur.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO templates (venue_id, name, body) VALUES (?, ?, ?)",
            (venue_id, "booking_link", v["template"]),
        )
        for label, price, link in v["packages"]:
            conn.execute(
                "INSERT OR IGNORE INTO packages (venue_id, label, norm_key, price, link) "
                "VALUES (?, ?, ?, ?, ?)",
                (venue_id, label, norm_key(label), price, link),
            )
    conn.commit()
    conn.close()


# ---------- settings ----------

def get_setting(key: str, default: str = "") -> str:
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


# ---------- venues ----------

def list_venues():
    return get_db().execute("SELECT * FROM venues ORDER BY name").fetchall()


def get_venue_by_slug(slug: str):
    return get_db().execute("SELECT * FROM venues WHERE slug = ?", (slug,)).fetchone()


def get_venue(venue_id: int):
    return get_db().execute("SELECT * FROM venues WHERE id = ?", (venue_id,)).fetchone()


def update_venue(venue_id: int, name: str, sender_number: str, active: int):
    db = get_db()
    db.execute(
        "UPDATE venues SET name = ?, sender_number = ?, active = ? WHERE id = ?",
        (name, sender_number, active, venue_id),
    )
    db.commit()


def rotate_venue_token(venue_id: int):
    db = get_db()
    db.execute("UPDATE venues SET token = ? WHERE id = ?", (new_token(), venue_id))
    db.commit()


# ---------- packages ----------

def list_packages(venue_id: int):
    return get_db().execute(
        "SELECT * FROM packages WHERE venue_id = ? ORDER BY id", (venue_id,)
    ).fetchall()


def find_package(venue_id: int, raw_label: str):
    key = norm_key(raw_label)
    if not key:
        return None
    return get_db().execute(
        "SELECT * FROM packages WHERE venue_id = ? AND norm_key = ?", (venue_id, key)
    ).fetchone()


def upsert_package(venue_id: int, label: str, price: str, link: str, package_id=None):
    db = get_db()
    if package_id:
        db.execute(
            "UPDATE packages SET label = ?, norm_key = ?, price = ?, link = ? WHERE id = ?",
            (label, norm_key(label), price, link, package_id),
        )
    else:
        db.execute(
            "INSERT INTO packages (venue_id, label, norm_key, price, link) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(venue_id, norm_key) DO UPDATE SET "
            "label = excluded.label, price = excluded.price, link = excluded.link",
            (venue_id, label, norm_key(label), price, link),
        )
    db.commit()


def delete_package(package_id: int):
    db = get_db()
    db.execute("DELETE FROM packages WHERE id = ?", (package_id,))
    db.commit()


# ---------- templates ----------

def get_template(venue_id: int, name: str = "booking_link"):
    return get_db().execute(
        "SELECT * FROM templates WHERE venue_id = ? AND name = ?", (venue_id, name)
    ).fetchone()


def save_template(venue_id: int, name: str, body: str):
    db = get_db()
    db.execute(
        "INSERT INTO templates (venue_id, name, body) VALUES (?, ?, ?) "
        "ON CONFLICT(venue_id, name) DO UPDATE SET body = excluded.body",
        (venue_id, name, body),
    )
    db.commit()


# ---------- events ----------

def find_duplicate(venue_id, call_session_id: str):
    if not call_session_id:
        return None
    return get_db().execute(
        "SELECT id FROM events WHERE venue_id IS ? AND call_session_id = ? "
        "AND status != 'duplicate' ORDER BY id LIMIT 1",
        (venue_id, call_session_id),
    ).fetchone()


EVENT_COLS = (
    "venue_id", "slug", "received_at", "remote_ip", "content_type", "headers",
    "body", "call_session_id", "customer_phone", "agent_did", "package",
    "first_name", "matched_link", "preview_body", "preview_from", "status", "note",
)


def insert_event(**kw) -> int:
    db = get_db()
    cur = db.execute(
        f"INSERT INTO events ({', '.join(EVENT_COLS)}) "
        f"VALUES ({', '.join('?' * len(EVENT_COLS))})",
        [kw.get(c) for c in EVENT_COLS],
    )
    db.commit()
    return cur.lastrowid


def list_events(limit=100, venue_id=None, status=None):
    sql = ["SELECT e.*, v.name AS venue_name FROM events e "
           "LEFT JOIN venues v ON v.id = e.venue_id WHERE 1=1"]
    args = []
    if venue_id:
        sql.append("AND e.venue_id = ?")
        args.append(venue_id)
    if status:
        sql.append("AND e.status = ?")
        args.append(status)
    sql.append("ORDER BY e.id DESC LIMIT ?")
    args.append(limit)
    return get_db().execute(" ".join(sql), args).fetchall()


def get_event(event_id: int):
    return get_db().execute(
        "SELECT e.*, v.name AS venue_name FROM events e "
        "LEFT JOIN venues v ON v.id = e.venue_id WHERE e.id = ?",
        (event_id,),
    ).fetchone()


def event_counts():
    rows = get_db().execute(
        "SELECT status, COUNT(*) AS n FROM events GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}
