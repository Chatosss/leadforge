import sqlite3
from pathlib import Path

# ISOLAMENTO (clinica_prospector): esta é a CÓPIA do mail bot. Aponta para um
# banco próprio (~/.pcz_clinica/pcz.db), separado do banco de produção (~/.pcz/).
DB_PATH = Path.home() / ".pcz_clinica" / "pcz.db"


_DB_READY = False


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-8000")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def init_db():
    global _DB_READY
    if _DB_READY:
        return
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS accounts (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        email         TEXT UNIQUE NOT NULL,
        sender_name   TEXT,
        forwarding_to TEXT,
        daily_limit   INTEGER DEFAULT 95,
        sent_today    INTEGER DEFAULT 0,
        last_reset    TEXT,
        filters_set   INTEGER DEFAULT 0,
        active        INTEGER DEFAULT 1,
        created_at    TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS templates (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        subject    TEXT NOT NULL,
        body       TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS lists (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS prospects (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        list_id    INTEGER REFERENCES lists(id) ON DELETE CASCADE,
        email      TEXT NOT NULL,
        company    TEXT,
        domain     TEXT,
        corpo      TEXT DEFAULT '',   -- corpo personalizado por lead (clinica_prospector)
        status     TEXT DEFAULT 'pending',
        sent_at    TEXT,
        sent_by    TEXT,
        replied_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS campaigns (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        template_id INTEGER REFERENCES templates(id),
        list_id     INTEGER REFERENCES lists(id),
        status      TEXT DEFAULT 'paused',
        created_at  TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS campaign_accounts (
        campaign_id INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
        account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
        PRIMARY KEY (campaign_id, account_id)
    );

    CREATE TABLE IF NOT EXISTS send_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        prospect_id INTEGER REFERENCES prospects(id),
        account_id  INTEGER REFERENCES accounts(id),
        campaign_id INTEGER REFERENCES campaigns(id),
        template_id INTEGER REFERENCES templates(id),
        thread_id   TEXT,
        status      TEXT,
        error       TEXT,
        replied_at  TEXT,
        sent_at     TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS campaign_templates (
        campaign_id INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
        template_id INTEGER REFERENCES templates(id) ON DELETE CASCADE,
        PRIMARY KEY (campaign_id, template_id)
    );

    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    # Migrations for existing DBs
    migrations = [
        "ALTER TABLE send_log ADD COLUMN template_id INTEGER REFERENCES templates(id)",
        "ALTER TABLE send_log ADD COLUMN thread_id TEXT",
        "ALTER TABLE send_log ADD COLUMN replied_at TEXT",
        "ALTER TABLE prospects ADD COLUMN replied_at TEXT",
        "ALTER TABLE prospects ADD COLUMN corpo TEXT DEFAULT ''",  # clinica_prospector
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass
    conn.commit()
    conn.close()
    _DB_READY = True


# ── helpers ──────────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
    conn.commit()
    conn.close()


def get_accounts(only_active=False):
    conn = get_conn()
    where = "WHERE a.active=1" if only_active else ""
    rows = conn.execute(f"""
        SELECT a.*, MAX(sl.sent_at) AS last_sent_at
        FROM accounts a
        LEFT JOIN send_log sl ON sl.account_id = a.id AND sl.status = 'sent'
        {where}
        GROUP BY a.id
        ORDER BY a.id
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_templates():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM templates ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_lists():
    conn = get_conn()
    rows = conn.execute("""
        SELECT l.*, COUNT(p.id) AS total,
               SUM(CASE WHEN p.status='pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN p.status='sent'    THEN 1 ELSE 0 END) AS sent
        FROM lists l
        LEFT JOIN prospects p ON p.list_id = l.id
        GROUP BY l.id ORDER BY l.id DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_campaigns():
    conn = get_conn()
    rows = conn.execute("""
        SELECT c.*, t.name AS template_name, l.name AS list_name
        FROM campaigns c
        LEFT JOIN templates t ON t.id = c.template_id
        LEFT JOIN lists     l ON l.id = c.list_id
        ORDER BY c.id DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_campaign_templates(campaign_id: int):
    conn = get_conn()
    rows = conn.execute("""
        SELECT t.* FROM templates t
        JOIN campaign_templates ct ON ct.template_id = t.id
        WHERE ct.campaign_id = ?
        ORDER BY t.id
    """, (campaign_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_template_stats(campaign_id: int):
    conn = get_conn()
    rows = conn.execute("""
        SELECT t.id, t.name, t.subject,
               COUNT(CASE WHEN sl.status='sent' THEN 1 END)        AS total_sent,
               COUNT(CASE WHEN sl.replied_at IS NOT NULL THEN 1 END) AS replies
        FROM templates t
        JOIN campaign_templates ct ON ct.template_id = t.id
        LEFT JOIN send_log sl ON sl.template_id = t.id AND sl.campaign_id = ?
        WHERE ct.campaign_id = ?
        GROUP BY t.id
    """, (campaign_id, campaign_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_threads_to_check(account_email: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT sl.id AS log_id, sl.thread_id, sl.prospect_id, sl.template_id, sl.campaign_id
        FROM send_log sl
        JOIN accounts a ON a.id = sl.account_id
        JOIN prospects p ON p.id = sl.prospect_id
        WHERE a.email = ?
          AND sl.thread_id IS NOT NULL
          AND sl.status = 'sent'
          AND p.replied_at IS NULL
    """, (account_email,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_campaign_accounts(campaign_id: int):
    conn = get_conn()
    rows = conn.execute("""
        SELECT a.* FROM accounts a
        JOIN campaign_accounts ca ON ca.account_id = a.id
        WHERE ca.campaign_id = ?
    """, (campaign_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def dashboard_stats():
    conn = get_conn()
    stats = {}
    stats["accounts"]  = conn.execute("SELECT COUNT(*) FROM accounts WHERE active=1").fetchone()[0]
    stats["templates"] = conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
    stats["lists"]     = conn.execute("SELECT COUNT(*) FROM lists").fetchone()[0]
    stats["prospects"] = conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]
    stats["sent"]      = conn.execute("SELECT COUNT(*) FROM prospects WHERE status='sent'").fetchone()[0]
    stats["pending"]   = conn.execute("SELECT COUNT(*) FROM prospects WHERE status='pending'").fetchone()[0]
    stats["campaigns_active"] = conn.execute("SELECT COUNT(*) FROM campaigns WHERE status='active'").fetchone()[0]
    from datetime import date as _date
    today = _date.today().isoformat()
    # Only count accounts whose counter was reset today; stale counters are treated as 0
    stats["sent_today"] = conn.execute(
        "SELECT COALESCE(SUM(sent_today),0) FROM accounts WHERE last_reset=?", (today,)
    ).fetchone()[0]
    conn.close()
    return stats
