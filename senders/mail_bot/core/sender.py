"""
Campaign sender — one thread per account, all sending in parallel. Prospects are claimed
atomically via SQLite so no duplicates occur.
"""
import random
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path

from .database import get_conn
from .gmail_manager import send_email

# Hub.db path (C:\...\Bots\hub\data\hub.db), derivado do local deste arquivo
_HUB_DB = Path(__file__).resolve().parent.parent.parent.parent / "hub" / "data" / "hub.db"


def _is_blacklisted(email: str) -> bool:
    """Consulta blacklist no hub.db: bloqueia por email exato ou domínio."""
    try:
        if not _HUB_DB.exists():
            return False
        domain = email.split("@")[1].lower() if "@" in email else ""
        email_lower = email.lower()
        with sqlite3.connect(str(_HUB_DB)) as hub:
            row = hub.execute(
                "SELECT 1 FROM blacklist "
                "WHERE (type='email' AND value=?) OR (type='domain' AND value=?) "
                "LIMIT 1",
                (email_lower, domain),
            ).fetchone()
        return row is not None
    except Exception:
        return False

_state: dict = {
    "running": False,
    "campaign_id": None,
    "sent": 0,
    "errors": 0,
    "current": "",
    "log": [],
}
_lock = threading.Lock()
_thread: threading.Thread | None = None


def get_state() -> dict:
    with _lock:
        return dict(_state)


def stop():
    with _lock:
        _state["running"] = False


# ── template rendering ────────────────────────────────────────────────────────

def render(text: str, prospect: dict) -> str:
    replacements = {
        "{{email}}":   prospect.get("email", ""),
        "{{empresa}}": prospect.get("company", "") or "",
        "{{dominio}}": prospect.get("domain", "") or "",
        # clinica_prospector: corpo personalizado por lead (diagnóstico único)
        "{{corpo}}":   prospect.get("corpo", "") or "",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


# ── per-account worker ────────────────────────────────────────────────────────

def _worker(campaign_id: int, account: dict, templates: list, min_delay: int, max_delay: int, log):
    conn = get_conn()
    today = date.today().isoformat()

    # Reset daily counter if it's a new day
    if account.get("last_reset") != today:
        conn.execute(
            "UPDATE accounts SET sent_today=0, last_reset=? WHERE id=?",
            (today, account["id"]),
        )
        conn.commit()

    tmpl_index = 0

    try:
        while True:
            with _lock:
                if not _state["running"]:
                    break

            # Check quota (fresh from DB)
            fresh = conn.execute("SELECT * FROM accounts WHERE id=?", (account["id"],)).fetchone()
            if not fresh or not fresh["active"]:
                log(f"[{account['email']}] Conta desativada. Encerrando.")
                break
            if fresh["sent_today"] >= fresh["daily_limit"]:
                log(f"[{account['email']}] Limite diário atingido ({fresh['sent_today']}/{fresh['daily_limit']}). Encerrando conta.")
                break

            log(f"[{account['email']}] {fresh['sent_today']}/{fresh['daily_limit']} enviados hoje — buscando próximo prospect...")

            # Atomically claim the next pending prospect using SQLite's serialized writes.
            # The UPDATE is a single statement: SQLite guarantees only one thread wins each row.
            conn.execute("""
                UPDATE prospects SET status='sending', sent_by=?
                WHERE id = (
                    SELECT p.id FROM prospects p
                    JOIN lists l ON l.id = p.list_id
                    JOIN campaigns c ON c.list_id = l.id
                    WHERE c.id = ? AND p.status = 'pending'
                    ORDER BY p.id LIMIT 1
                )
            """, (account["email"], campaign_id))
            conn.commit()

            # Retrieve the row we just claimed
            prospect = conn.execute(
                "SELECT * FROM prospects WHERE status='sending' AND sent_by=? ORDER BY id LIMIT 1",
                (account["email"],),
            ).fetchone()

            if not prospect:
                log(f"[{account['email']}] Sem mais prospects pendentes.")
                break

            prospect = dict(prospect)

            tmpl = templates[tmpl_index % len(templates)]
            tmpl_index += 1
            subject = render(tmpl["subject"], prospect)
            body    = render(tmpl["body"],    prospect)

            with _lock:
                _state["current"] = prospect["email"]

            from .gmail_manager import _sanitize_email
            clean_to = _sanitize_email(prospect["email"])
            if not clean_to:
                log(f"⚠ [{account['email']}] email inválido ignorado: {prospect['email']}")
                conn.execute(
                    "UPDATE prospects SET status='invalid', sent_by=NULL WHERE id=?",
                    (prospect["id"],),
                )
                conn.commit()
                continue

            if _is_blacklisted(clean_to):
                log(f"🚫 [{account['email']}] {clean_to} na blacklist — ignorando")
                conn.execute(
                    "UPDATE prospects SET status='blocked', sent_by=NULL WHERE id=?",
                    (prospect["id"],),
                )
                conn.commit()
                continue

            log(f"[{account['email']}] Enviando → {clean_to}  (template: {tmpl['name']})")

            result, thread_id = send_email(
                account_email=account["email"],
                to=clean_to,
                subject=subject,
                body=body,
                sender_name=account.get("sender_name", ""),
            )

            now = datetime.now().isoformat()
            if result == "ok":
                conn.execute(
                    "UPDATE prospects SET status='sent', sent_at=? WHERE id=?",
                    (now, prospect["id"]),
                )
                conn.execute(
                    "UPDATE accounts SET sent_today = sent_today + 1 WHERE id=?",
                    (account["id"],),
                )
                conn.execute(
                    "INSERT INTO send_log(prospect_id,account_id,campaign_id,template_id,thread_id,status) VALUES(?,?,?,?,?,?)",
                    (prospect["id"], account["id"], campaign_id, tmpl["id"], thread_id, "sent"),
                )
                conn.commit()
                with _lock:
                    _state["sent"] += 1
                log(f"✓ [{account['email']}] Enviado com sucesso → {prospect['email']}")
            else:
                # Registra o erro no log
                conn.execute(
                    "INSERT INTO send_log(prospect_id,account_id,campaign_id,template_id,status,error) VALUES(?,?,?,?,?,?)",
                    (prospect["id"], account["id"], campaign_id, tmpl["id"], "error", result),
                )
                conn.commit()
                with _lock:
                    _state["errors"] += 1
                log(f"✗ [{account['email']}] Falha → {prospect['email']}: {result}")

                # Conta quantas tentativas já foram feitas para este prospect
                fail_count = conn.execute(
                    "SELECT COUNT(*) FROM send_log WHERE prospect_id=? AND status='error'",
                    (prospect["id"],),
                ).fetchone()[0]

                MAX_RETRIES = 3
                if fail_count >= MAX_RETRIES:
                    # Marca como erro permanente — não tenta mais
                    conn.execute(
                        "UPDATE prospects SET status='error', sent_by=NULL WHERE id=?",
                        (prospect["id"],),
                    )
                    conn.commit()
                    log(f"⛔ [{account['email']}] {prospect['email']} marcado como erro permanente após {fail_count} tentativas.")
                else:
                    # Devolve para a fila para outra conta tentar
                    conn.execute(
                        "UPDATE prospects SET status='pending', sent_by=NULL WHERE id=?",
                        (prospect["id"],),
                    )
                    conn.commit()
                    log(f"↩ [{account['email']}] {prospect['email']} devolvido para fila (tentativa {fail_count}/{MAX_RETRIES}).")

            delay = random.randint(min_delay, max_delay)
            log(f"  [{account['email']}] aguardando {delay}s antes do próximo envio...")
            for _ in range(delay):
                with _lock:
                    if not _state["running"]:
                        break
                time.sleep(1)

    except Exception as _exc:
        log(f"[{account['email']}] ❌ ERRO CRÍTICO na thread: {_exc}")
        # Libera qualquer prospect preso em 'sending' por esta conta
        try:
            conn.execute(
                "UPDATE prospects SET status='pending', sent_by=NULL WHERE status='sending' AND sent_by=?",
                (account["email"],),
            )
            conn.commit()
        except Exception:
            pass

    conn.close()


# ── orchestrator ──────────────────────────────────────────────────────────────

def _run(campaign_id: int, min_delay: int, max_delay: int):
    with _lock:
        _state.update(running=True, campaign_id=campaign_id, sent=0, errors=0, log=[])

    def log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        with _lock:
            _state["log"].append(f"[{ts}] {msg}")
            # Sem truncagem — o log vive apenas enquanto o processo roda.
            # Truncar com pop(0) quebra o rastreamento por índice no email_launcher.

    conn = get_conn()

    campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not campaign:
        log("Campanha não encontrada.")
        with _lock:
            _state["running"] = False
        conn.close()
        return

    # Load templates (multi-template A/B, fallback to single)
    templates = [dict(r) for r in conn.execute("""
        SELECT t.* FROM templates t
        JOIN campaign_templates ct ON ct.template_id = t.id
        WHERE ct.campaign_id = ?
        ORDER BY t.id
    """, (campaign_id,)).fetchall()]

    if not templates and campaign["template_id"]:
        row = conn.execute("SELECT * FROM templates WHERE id=?", (campaign["template_id"],)).fetchone()
        if row:
            templates = [dict(row)]

    if not templates:
        log("Nenhum template configurado para esta campanha.")
        with _lock:
            _state["running"] = False
        conn.close()
        return

    accounts = [dict(a) for a in conn.execute("""
        SELECT a.* FROM accounts a
        JOIN campaign_accounts ca ON ca.account_id = a.id
        WHERE ca.campaign_id = ? AND a.active = 1
        ORDER BY a.id
    """, (campaign_id,)).fetchall()]
    conn.close()

    if not accounts:
        log("Nenhuma conta configurada para esta campanha.")
        with _lock:
            _state["running"] = False
        return

    log(f"Iniciando '{campaign['name']}' com {len(accounts)} conta(s) em paralelo e {len(templates)} template(s).")

    workers = []
    for acc in accounts:
        t = threading.Thread(
            target=_worker,
            args=(campaign_id, acc, templates, min_delay, max_delay, log),
            daemon=True,
        )
        t.start()
        workers.append(t)

    for w in workers:
        w.join()

    with _lock:
        _state["running"] = False
        _state["current"] = ""
    log("Disparo finalizado.")


def start(campaign_id: int, min_delay: int = 30, max_delay: int = 90):
    global _thread
    with _lock:
        if _state["running"]:
            return False

    _thread = threading.Thread(
        target=_run,
        args=(campaign_id, min_delay, max_delay),
        daemon=True,
    )
    _thread.start()
    return True
