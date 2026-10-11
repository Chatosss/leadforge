"""crm.py — Módulo E (Acompanhar)."""

import importlib.util
from typing import Dict, List

import config
import db


# ── Status ────────────────────────────────────────────────────────────────────

def avancar_status(lead_id: int, novo: str):
    db.set_status(lead_id, novo)


def marcar_resposta(lead_id: int):
    """Lead respondeu — vira prioridade máxima."""
    db.set_status(lead_id, "replied")


def registrar_touch_enviado(lead_id: int, canal: str, etapa: str, detalhe: str = ""):
    """Marca a etapa agendada como enviada; cria a linha se ainda não existir."""
    for t in db.touches_for(lead_id):
        if t["canal"] == canal and t["etapa"] == etapa:
            db.mark_touch(t["id"], "sent", detalhe or None)
            return
    tid = db.schedule_touch(lead_id, canal, etapa, "", detalhe)
    db.mark_touch(tid, "sent", detalhe or None)


# ── Consultas de trabalho ─────────────────────────────────────────────────────

def funil() -> Dict[str, int]:
    return db.funnel_counts()


def leads_que_responderam() -> List[Dict]:
    with db.conn() as con:
        rows = con.execute(
            "SELECT * FROM leads WHERE status='replied' ORDER BY score DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def leads_esfriando(dias: int = 5) -> List[Dict]:
    """
    Leads já contatados que não responderam e cujo último toque enviado passou de `dias`
    dias — precisam de re-toque. Ordenados por score (maior 1º).
    """
    ativos = ("contacted", "diagnosed_sent", "proposed")
    with db.conn() as con:
        rows = con.execute(f"""
            SELECT l.*, MAX(t.sent_at) AS ultimo_envio
            FROM leads l
            JOIN touches t ON t.lead_id = l.id AND t.status='sent'
            WHERE l.status IN ({','.join('?'*len(ativos))})
            GROUP BY l.id
            HAVING julianday('now') - julianday(MAX(t.sent_at)) >= ?
            ORDER BY l.score DESC
        """, (*ativos, dias)).fetchall()
    return [dict(r) for r in rows]


def proximos_toques(limit: int = 20) -> List[Dict]:
    """Toques agendados (ainda não enviados), do mais próximo ao mais distante."""
    with db.conn() as con:
        rows = con.execute("""
            SELECT t.*, l.nome, l.score
            FROM touches t JOIN leads l ON l.id = t.lead_id
            WHERE t.status='scheduled'
            ORDER BY t.scheduled_for LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── Sincronização de respostas por e-mail (opcional) ──────────────────────────

def sincronizar_respostas_email() -> Dict:
    """
    Reusa check_all_replies() da CÓPIA do mail bot para detectar respostas e reconcilia
    com os leads (casando por nome da empresa). Requer o mail bot configurado (contas
    Gmail/OAuth). Se indisponível, não quebra — só reporta.
    """
    try:
        base = config.BASE_DIR / "senders" / "mail_bot" / "core"
        # carrega reply_checker de forma isolada
        spec = importlib.util.spec_from_file_location(
            "mail_reply_checker", base / "reply_checker.py")
        rc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rc)
        resultado = rc.check_all_replies()  # atualiza prospects.replied_at no mail DB
    except Exception as e:
        return {"status": "indisponivel",
                "motivo": f"mail bot não configurado ou dependências ausentes: {e}"}

    # reconcilia: prospects que responderam -> marca o lead como 'replied'
    reconciliados = 0
    try:
        mdb_spec = importlib.util.spec_from_file_location(
            "mail_core_db2", base / "database.py")
        mdb = importlib.util.module_from_spec(mdb_spec)
        mdb_spec.loader.exec_module(mdb)
        mconn = mdb.get_conn()
        respondidos = mconn.execute(
            "SELECT company FROM prospects WHERE replied_at IS NOT NULL"
        ).fetchall()
        mconn.close()
        nomes = {r["company"] for r in respondidos if r["company"]}
        for nome in nomes:
            with db.conn() as con:
                lead = con.execute(
                    "SELECT id FROM leads WHERE nome=? AND status<>'replied'", (nome,)
                ).fetchone()
            if lead:
                marcar_resposta(lead["id"])
                reconciliados += 1
    except Exception as e:
        return {"status": "parcial", "detectadas": resultado, "erro_reconciliacao": str(e)}

    return {"status": "ok", "detectadas": resultado, "leads_marcados_replied": reconciliados}


# ── Relatório ─────────────────────────────────────────────────────────────────

def imprimir_funil():
    f = funil()
    ordem = db.STATUS_FLUXO
    print("📊 FUNIL DE PROSPECÇÃO")
    for s in ordem:
        print(f"   {s:<16} {f.get(s, 0)}")
    resp = leads_que_responderam()
    print(f"\n🔥 Responderam (prioridade): {len(resp)}")
    for l in resp[:5]:
        print(f"   • {l['nome']} (score {l['score']})")
    esf = leads_esfriando()
    print(f"\n🧊 Esfriando (precisam re-toque): {len(esf)}")
    for l in esf[:5]:
        print(f"   • {l['nome']} — último envio {l.get('ultimo_envio')}")


if __name__ == "__main__":
    db.init_db()
    imprimir_funil()
