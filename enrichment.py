"""enrichment.py — Enriquecimento de leads."""

import socket
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urlparse

import db


def _ensure_table():
    """Garante que a tabela enrichment_status existe."""
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS enrichment_status (
                lead_id INTEGER PRIMARY KEY REFERENCES leads(id) ON DELETE CASCADE,
                email_valido INTEGER,
                website_responde INTEGER,
                enriched_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)


def enrich_leads(limite: int = 100) -> Dict:
    """Enriquece leads que ainda nao foram enriquecidos."""
    _ensure_table()
    processados = 0
    with db.conn() as con:
        rows = con.execute(
            "SELECT id, email, website FROM leads "
            "WHERE (email IS NOT NULL AND email != '') "
            "AND id NOT IN (SELECT lead_id FROM enrichment_status) "
            "LIMIT ?",
            (limite,)
        ).fetchall()

    for row in rows:
        lead = dict(row)
        _enrich_single(lead)
        processados += 1

    return {"processados": processados}


def _enrich_single(lead: Dict):
    """Enriquece um lead individual."""
    lead_id = lead["id"]
    email = (lead.get("email") or "").strip()
    website = (lead.get("website") or "").strip()

    result = {
        "email_valido": None,
        "website_responde": None,
    }

    # Validacao MX
    if email:
        result["email_valido"] = _check_mx(email)

    # Check de website
    if website:
        result["website_responde"] = _check_website(website)

    # Grava resultado
    _save_enrichment(lead_id, result)


def _check_mx(email: str) -> Optional[bool]:
    """Verifica se o dominio do e-mail tem registro MX."""
    try:
        domain = email.split("@")[-1].strip().lower()
        if not domain:
            return None
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX")
        return len(answers) > 0
    except ImportError:
        # Sem dnspython, usa heuristica basica
        return None
    except Exception:
        return False


def _check_website(url: str) -> Optional[bool]:
    """Verifica se o website responde."""
    try:
        import requests
        if not url.startswith("http"):
            url = "https://" + url
        r = requests.head(url, timeout=10, allow_redirects=True)
        return r.status_code < 400
    except Exception:
        return False


def _save_enrichment(lead_id: int, data: Dict):
    """Grava resultado do enrichment no banco."""
    with db.conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS enrichment_status (
                lead_id INTEGER PRIMARY KEY REFERENCES leads(id) ON DELETE CASCADE,
                email_valido INTEGER,
                website_responde INTEGER,
                enriched_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        con.execute(
            "INSERT OR REPLACE INTO enrichment_status "
            "(lead_id, email_valido, website_responde) VALUES (?,?,?)",
            (lead_id,
             1 if data.get("email_valido") else (0 if data.get("email_valido") is False else None),
             1 if data.get("website_responde") else (0 if data.get("website_responde") is False else None))
        )


def status_enrichment() -> Dict:
    """Resumo do estado do enrichment."""
    with db.conn() as con:
        total = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        enriched = con.execute("SELECT COUNT(*) FROM enrichment_status").fetchone()[0]
        email_ok = con.execute(
            "SELECT COUNT(*) FROM enrichment_status WHERE email_valido=1"
        ).fetchone()[0]
        website_ok = con.execute(
            "SELECT COUNT(*) FROM enrichment_status WHERE website_responde=1"
        ).fetchone()[0]
    return {
        "total_leads": total,
        "enriquecidos": enriched,
        "email_valido": email_ok,
        "website_responde": website_ok,
    }
