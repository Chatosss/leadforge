"""visao_geral.py — Os números da primeira tela do LeadForge."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List

import config
import db

RAIZ = Path(__file__).resolve().parent
IG_PERFIS = RAIZ / "senders" / "dm_bot" / "perfis"


def _hoje() -> str:
    return date.today().isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Base: quanta gente existe em cada ferramenta
# ══════════════════════════════════════════════════════════════════════════════

def base_leadforge() -> Dict:
    """Leads do LeadForge (vieram do Maps), e quantos são alcançáveis por canal."""
    with db.conn() as con:
        total = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        com_tel = con.execute(
            "SELECT COUNT(*) FROM leads WHERE TRIM(COALESCE(telefone,''))<>'' "
            "OR TRIM(COALESCE(whatsapp,''))<>''").fetchone()[0]
        com_email = con.execute(
            "SELECT COUNT(*) FROM leads WHERE TRIM(COALESCE(email,''))<>''").fetchone()[0]
        com_ig = con.execute(
            "SELECT COUNT(*) FROM leads WHERE TRIM(COALESCE(instagram,''))<>''").fetchone()[0]
        por_kanban = {r["kanban"] or "sem coluna": r["n"] for r in con.execute(
            "SELECT kanban, COUNT(*) n FROM leads GROUP BY kanban")}
        contatados = con.execute(
            "SELECT COUNT(DISTINCT lead_id) FROM touches "
            "WHERE status IN ('sent','queued')").fetchone()[0]
    return {"total": total, "com_telefone": com_tel, "com_email": com_email,
            "com_instagram": com_ig, "ja_contatados": contatados,
            "nunca_contatados": max(0, total - contatados),
            "por_kanban": por_kanban}


def base_email() -> Dict:
    """Prospects do mail bot. É a maior base do sistema, e vive noutro banco."""
    try:
        import email_marketing as em
        with em._conn() as con:
            por_status = {r["status"]: r["n"] for r in con.execute(
                "SELECT status, COUNT(*) n FROM prospects GROUP BY status")}
            listas = con.execute("SELECT COUNT(*) FROM lists").fetchone()[0]
        return {"ok": True, "total": sum(por_status.values()),
                "pendentes": por_status.get("pending", 0),
                "enviados": por_status.get("sent", 0),
                "erros": por_status.get("error", 0),
                "listas": listas}
    except Exception as e:
        return {"ok": False, "erro": str(e), "total": 0, "pendentes": 0}


def base_instagram() -> Dict:
    """Perfis que o bot de DM já processou, por nicho."""
    nichos = []
    total = 0
    if IG_PERFIS.exists():
        for d in sorted(IG_PERFIS.iterdir()):
            if not d.is_dir():
                continue
            arq = d / "processados.txt"
            n = 0
            if arq.exists():
                try:
                    n = sum(1 for l in arq.read_text(encoding="utf-8", errors="ignore")
                            .splitlines() if l.strip())
                except Exception:
                    n = 0
            nichos.append({"nicho": d.name, "processados": n})
            total += n
    return {"total": total, "nichos": nichos}


# ══════════════════════════════════════════════════════════════════════════════
# Ritmo: quanto saiu hoje e quanto ainda cabe
# ══════════════════════════════════════════════════════════════════════════════

def _teto_das_contas(canal: str) -> Dict:
    """O teto do canal é a soma dos limites diários das contas ativas (aba Contas)."""
    import limites
    ativas = [e for e in limites.estado(canal) if e.get("status") == "active"]
    # "hoje" sai do mesmo registro das vagas: hoje + restam fecha com o teto
    return {"hoje": sum(e["usados"] for e in ativas),
            "teto": sum(e["limite"] for e in ativas),
            "restam": sum(e["restam"] for e in ativas), "contas": len(ativas)}


def ritmo_whatsapp() -> Dict:
    """Enviados hoje e vagas, pelo limite diário de cada conta (aba Contas)."""
    saida = {"canal": "WhatsApp"}
    try:
        saida.update(_teto_das_contas("whatsapp"))
    except Exception as e:
        saida["erro"] = str(e)
    return saida


def ritmo_email() -> Dict:
    """Enviados hoje pelo mail bot e a capacidade que sobra nas contas."""
    saida = {"canal": "E-mail"}
    try:
        import email_marketing as em
        with em._conn() as con:
            saida["hoje"] = con.execute(
                "SELECT COUNT(*) FROM send_log "
                "WHERE date(sent_at)=date('now','localtime')").fetchone()[0]
        cts = em.contas()
        ativas = [c for c in cts if c["active"] and c.get("autenticada") is not False]
        saida["teto"] = sum(c["daily_limit"] or 0 for c in ativas)
        saida["restam"] = sum(c["restam"] for c in ativas)
        saida["contas"] = len(ativas)
        saida["contas_sem_oauth"] = sum(
            1 for c in cts if c["active"] and c.get("autenticada") is False)
        saida["disparando"] = bool(em.disparo_status().get("rodando"))
    except Exception as e:
        saida["erro"] = str(e)
    return saida


def ritmo_instagram() -> Dict:
    """DMs de hoje e vagas, pelo limite diário de cada conta (aba Contas)."""
    saida = {"canal": "Instagram"}
    try:
        saida.update(_teto_das_contas("instagram"))
    except Exception as e:
        saida.update({"hoje": 0, "teto": 0, "restam": 0, "erro": str(e)})
    return saida


def envios_por_dia(dias: int = 7) -> List[Dict]:
    """Envios por dia e por canal, para o gráfico."""
    inicio = (date.today() - timedelta(days=dias - 1)).isoformat()
    linhas = {}
    for i in range(dias):
        d = (date.today() - timedelta(days=dias - 1 - i)).isoformat()
        linhas[d] = {"dia": d, "whatsapp": 0, "instagram": 0, "email": 0}

    try:
        with db.conn() as con:
            for r in con.execute(
                    "SELECT date(COALESCE(sent_at, created_at)) d, canal, COUNT(*) n "
                    "FROM touches WHERE status IN ('sent','queued') "
                    "AND date(COALESCE(sent_at, created_at)) >= ? "
                    "GROUP BY d, canal", (inicio,)):
                if r["d"] in linhas and r["canal"] in ("whatsapp", "instagram", "email"):
                    linhas[r["d"]][r["canal"]] += r["n"]
    except Exception:
        pass

    try:
        import email_marketing as em
        with em._conn() as con:
            for r in con.execute(
                    "SELECT date(sent_at) d, COUNT(*) n FROM send_log "
                    "WHERE date(sent_at) >= ? GROUP BY d", (inicio,)):
                if r["d"] in linhas:
                    linhas[r["d"]]["email"] = r["n"]
    except Exception:
        pass

    return list(linhas.values())


# ══════════════════════════════════════════════════════════════════════════════
# O que está de pé
# ══════════════════════════════════════════════════════════════════════════════

def servicos() -> Dict:
    """Estado dos bots e quantas contas estão realmente conectadas em cada canal."""
    saida = {}

    try:
        from senders import whatsapp_sender
        c = whatsapp_sender.contas()
        prontas = [x for x in c.get("contas", []) if x.get("pronto")]
        saida["whatsapp"] = {"no_ar": c.get("no_ar", False),
                             "conectadas": len(prontas),
                             "cadastradas": len(c.get("contas", [])),
                             "numeros": [x.get("numero") for x in prontas if x.get("numero")]}
    except Exception as e:
        saida["whatsapp"] = {"no_ar": False, "erro": str(e), "conectadas": 0}

    try:
        import connection_manager as cm
        bot = cm.ig_status_bot()
        contas_ig = cm.ig_status_contas()
        saida["instagram"] = {"no_ar": bot.get("online", False),
                              "conectadas": sum(1 for c in contas_ig if c.get("sessao_ativa")),
                              "cadastradas": len(contas_ig)}
    except Exception as e:
        saida["instagram"] = {"no_ar": False, "erro": str(e), "conectadas": 0}

    try:
        import email_marketing as em
        cts = em.contas()
        saida["email"] = {"no_ar": em.disponivel().get("ok", False),
                          "conectadas": sum(1 for c in cts
                                            if c["active"] and c.get("autenticada")),
                          "cadastradas": len(cts),
                          "disparando": bool(em.disparo_status().get("rodando"))}
    except Exception as e:
        saida["email"] = {"no_ar": False, "erro": str(e), "conectadas": 0}

    try:
        import scheduler
        saida["scheduler"] = {"rodando": scheduler._is_running()}
    except Exception as e:
        saida["scheduler"] = {"rodando": False, "erro": str(e)}

    return saida


# ══════════════════════════════════════════════════════════════════════════════
# Tudo junto
# ══════════════════════════════════════════════════════════════════════════════

def resumo() -> Dict:
    b_lf, b_em, b_ig = base_leadforge(), base_email(), base_instagram()
    r_wa, r_em, r_ig = ritmo_whatsapp(), ritmo_email(), ritmo_instagram()

    enviados_hoje = (r_wa.get("hoje", 0) or 0) + (r_em.get("hoje", 0) or 0) \
        + (r_ig.get("hoje", 0) or 0)
    capacidade = (r_wa.get("restam", 0) or 0) + (r_em.get("restam", 0) or 0) \
        + (r_ig.get("restam", 0) or 0)

    return {
        "atualizado_em": datetime.now().strftime("%d/%m %H:%M:%S"),
        "base": {"leadforge": b_lf, "email": b_em, "instagram": b_ig,
                 "alcancaveis": b_lf["total"] + b_em.get("total", 0)},
        "ritmo": {"whatsapp": r_wa, "email": r_em, "instagram": r_ig,
                  "enviados_hoje": enviados_hoje, "cabem_hoje": capacidade},
        "servicos": servicos(),
        "por_dia": envios_por_dia(7),
    }


if __name__ == "__main__":
    print(json.dumps(resumo(), indent=2, ensure_ascii=False, default=str))
