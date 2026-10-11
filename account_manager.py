"""account_manager.py — Gestao de contas multi-canal do LeadForge."""

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import db


def init():
    """Cria tabela de contas se nao existir."""
    with db.conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                identifier TEXT NOT NULL,
                display_name TEXT,
                daily_limit INTEGER DEFAULT 30,
                sent_today INTEGER DEFAULT 0,
                last_sent_at TEXT,
                status TEXT DEFAULT 'active',
                auto_prospect INTEGER DEFAULT 0,
                extra_json TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(channel, identifier)
            );
            CREATE INDEX IF NOT EXISTS idx_accounts_channel ON accounts(channel);
        """)


def listar_contas(canal: Optional[str] = None) -> List[Dict]:
    """Lista contas cadastradas."""
    init()
    with db.conn() as con:
        if canal:
            rows = con.execute(
                "SELECT * FROM accounts WHERE channel=? ORDER BY identifier",
                (canal,)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM accounts ORDER BY channel, identifier"
            ).fetchall()
    return [dict(r) for r in rows]


def criar_conta(canal: str, identifier: str, display_name: str = None,
                daily_limit: int = 30) -> int:
    """Cria uma conta."""
    init()
    with db.conn() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO accounts (channel, identifier, display_name, daily_limit) "
            "VALUES (?,?,?,?)",
            (canal, identifier, display_name or identifier, daily_limit)
        )
        return cur.lastrowid


def pode_enviar(account_id: int) -> Dict:
    """Verifica se a conta pode enviar (limite diario + cooldown)."""
    init()
    with db.conn() as con:
        row = con.execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
    if not row:
        return {"ok": False, "motivo": "conta nao encontrada"}

    conta = dict(row)
    if conta["status"] != "active":
        return {"ok": False, "motivo": f"conta {conta['status']}"}

    # O teto e do `limites`, em janela deslizante de 24h.
    import limites
    return limites.pode_enviar(account_id)


def registrar_envio(account_id: int, destino: str = "", lead_id: int = None):
    """Registra um envio na conta."""
    init()
    import limites
    c = limites.conta(account_id)
    if c:
        limites.registrar(account_id, c["channel"], destino, lead_id)
    with db.conn() as con:
        con.execute(
            "UPDATE accounts SET last_sent_at=datetime('now','localtime') WHERE id=?",
            (account_id,))


def resetar_contadores():
    """Nao faz mais nada, e de proposito."""
    return {"ok": True, "obsoleto": True,
            "motivo": "o teto agora e contado da janela de 24h, nao de um contador"}


def contas_prontas(canal: str) -> List[Dict]:
    """Retorna contas que podem enviar agora."""
    init()
    contas = listar_contas(canal)
    prontas = []
    for c in contas:
        resultado = pode_enviar(c["id"])
        if resultado["ok"]:
            c["restam"] = resultado.get("restam", 0)
            prontas.append(c)
    return prontas


def status_contas() -> Dict:
    """Resumo do estado de todas as contas."""
    init()
    with db.conn() as con:
        rows = con.execute(
            "SELECT channel, COUNT(*) as total, "
            "SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) as ativas, "
            "SUM(sent_today) as enviados_hoje "
            "FROM accounts GROUP BY channel"
        ).fetchall()
    return {r["channel"]: dict(r) for r in rows}
