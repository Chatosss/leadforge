"""reply_detector.py — Deteccao de replies por e-mail e Instagram."""

import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import db
import config


def detectar_replies_email() -> List[Dict]:
    """
    Detecta replies de e-mail verificando o pcz.db (se existir). Move leads que
    responderam para "Respondeu".
    """
    replies = []
    pcz_path = config.MAIL_BOT_DB

    if not pcz_path.exists():
        return replies

    try:
        import sqlite3
        con = sqlite3.connect(str(pcz_path), timeout=5)
        con.row_factory = sqlite3.Row

        # Busca e-mails com status 'replied'
        rows = con.execute(
            "SELECT pr.email, pr.company FROM prospects pr "
            "WHERE pr.status='replied' AND pr.email IS NOT NULL"
        ).fetchall()
        con.close()

        for row in rows:
            email = (row["email"] or "").strip().lower()
            if not email:
                continue
            # Encontra o lead pelo e-mail
            lead = _find_lead_by_email(email)
            if lead and lead.get("kanban") not in ("respondeu", "conversa", "fechado", "ignorado"):
                _mover_para_respondeu(lead["id"], "email", email)
                replies.append({"lead_id": lead["id"], "canal": "email", "contato": email})

    except Exception as e:
        print(f"[reply_detector] erro ao ler pcz.db: {e}")

    return replies


def detectar_replies_whatsapp() -> List[Dict]:
    """Verifica conversas registradas no banco e move leads que responderam."""
    replies = []
    try:
        with db.conn() as con:
            # Leads em "Contato iniciado" que tem mensagem de 'lead' no historico
            rows = con.execute("""
                SELECT DISTINCT l.id, l.nome, c.numero
                FROM leads l
                JOIN conversas c ON c.lead_id = l.id
                WHERE c.autor = 'lead'
                  AND l.kanban IN ('entrar_contato', 'contato_iniciado')
            """).fetchall()

            for row in rows:
                _mover_para_respondeu(row["id"], "whatsapp", row["numero"])
                replies.append({
                    "lead_id": row["id"],
                    "canal": "whatsapp",
                    "contato": row["numero"],
                    "nome": row["nome"],
                })
    except Exception as e:
        print(f"[reply_detector] erro ao verificar WhatsApp: {e}")

    return replies


def detectar_replies_instagram() -> List[Dict]:
    """Verifica DMs do Instagram recebidos (via hub.db do Hube 02, se existir)."""
    replies = []
    hub_db = Path(__file__).parent.parent / "Hube 02" / "data" / "hub.db"

    if not hub_db.exists():
        return replies

    try:
        import sqlite3
        con = sqlite3.connect(str(hub_db), timeout=5)
        con.row_factory = sqlite3.Row

        rows = con.execute(
            "SELECT contact, account FROM ig_inbox WHERE last_msg_dir='in' AND unread=1"
        ).fetchall()
        con.close()

        for row in rows:
            contact = (row["contact"] or "").strip()
            if not contact:
                continue
            lead = _find_lead_by_instagram(contact)
            if lead and lead.get("kanban") not in ("respondeu", "conversa", "fechado", "ignorado"):
                _mover_para_respondeu(lead["id"], "instagram", contact)
                replies.append({"lead_id": lead["id"], "canal": "instagram", "contato": contact})

    except Exception as e:
        print(f"[reply_detector] erro ao verificar Instagram: {e}")

    return replies


def detectar_todos() -> List[Dict]:
    """Detecta replies de todos os canais."""
    replies = []
    replies.extend(detectar_replies_email())
    replies.extend(detectar_replies_whatsapp())
    replies.extend(detectar_replies_instagram())
    return replies


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_lead_by_email(email: str):
    """Busca lead pelo e-mail."""
    email = email.strip().lower()
    with db.conn() as con:
        row = con.execute(
            "SELECT * FROM leads WHERE LOWER(email)=? LIMIT 1", (email,)
        ).fetchone()
    return dict(row) if row else None


def _find_lead_by_instagram(username: str):
    """Busca lead pelo username do Instagram."""
    username = username.strip().lstrip("@").lower()
    with db.conn() as con:
        row = con.execute(
            "SELECT * FROM leads WHERE LOWER(instagram)=? OR LOWER(whatsapp)=? LIMIT 1",
            (username, username)
        ).fetchone()
    return dict(row) if row else None


def _mover_para_respondeu(lead_id: int, canal: str, contato: str):
    """Move lead para a coluna 'Respondeu' e TIRA ele da regua de follow-up."""
    try:
        import dashboard_db as ddb
        ddb.set_kanban(lead_id, "respondeu")
        ddb.add_nota(lead_id, f"Reply detectado via {canal} ({contato})")
    except Exception as e:
        print(f"[reply_detector] falha ao mover lead {lead_id} no Kanban: {e}")

    try:
        import follow_up
        cancelados = follow_up.cancelar_follow_ups(lead_id)
        if cancelados:
            print(f"[reply_detector] lead {lead_id}: {cancelados} follow-up(s) cancelado(s)")
    except Exception as e:
        print(f"[reply_detector] falha ao cancelar follow-ups do lead {lead_id}: {e}")
