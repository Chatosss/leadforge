"""experimentos.py — Log de mensagens, métricas por ponto de contato e teste A/B."""

import hashlib
import json
import re
from datetime import datetime
from typing import Dict, List, Optional

import config
import db

# ── Canais em que a medição é confiável ──────────────────────────────────────
# O Instagram fica de fora da DECISÃO até o bot ler a caixa de entrada: sem ler
# resposta, qualquer taxa calculada ali seria zero por construção, não por
# desempenho, e isso envenenaria a comparação entre variantes.
CANAIS_MEDIVEIS = ("whatsapp", "email")


# Experimentos e variantes Cada experimento amarra num ponto de contato (canal +
# etapa) e lista variantes.

EXPERIMENTOS = {
    # DESLIGADO: o dia 0 passou a fechar sempre com "posso te mandar?", que é
    # sim/não por construção.
    "pergunta_dia0": {
        "descricao": "Como fechar o primeiro contato: pergunta calibrada ou sim/não",
        "canal": None,          # None = vale para qualquer canal
        "etapa": "dia0",
        "ativo": False,
        "variantes": {
            "calibrada": {
                "descricao": "'Como vocês lidam com isso hoje?' (obriga a pensar)",
                "config": {"pergunta": "calibrada"},
            },
            "simnao": {
                "descricao": "'Isso já está no radar de vocês?' (mais fácil de responder)",
                "config": {"pergunta": "simnao"},
            },
        },
    },
    "cta_dia2": {
        "descricao": "O convite do dia 2 com ou sem saída explícita",
        "canal": "whatsapp",
        "etapa": "dia2",
        "ativo": False,         # liga quando o dia 0 tiver conclusão
        "variantes": {
            "com_saida": {"descricao": "termina com 'se não fizer sentido, é só dizer'",
                          "config": {"cta_saida": True}},
            "sem_saida": {"descricao": "só o convite, sem a saída",
                          "config": {"cta_saida": False}},
        },
    },
    "assunto_dia4": {
        "descricao": "Assunto do e-mail: dado numérico ou curiosidade",
        "canal": "email",
        "etapa": "dia4",
        "ativo": False,
        "variantes": {
            "dado": {"descricao": "'8º na busca de Arapiraca'",
                     "config": {"assunto": "dado"}},
            "curiosidade": {"descricao": "'quem aparece antes da sua clínica'",
                            "config": {"assunto": "curiosidade"}},
        },
    },
}

# Abaixo disto, o relatório mostra o número mas NÃO sugere vencedor. Base pequena
# transforma ruído em conclusão, e trocar de versão com n baixo é o jeito mais
# rápido de piorar achando que melhorou.
MINIMO_PARA_CONCLUIR = 30


def init():
    with db.conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS mensagens (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id      INTEGER,
                canal        TEXT NOT NULL,     -- whatsapp | instagram | email
                etapa        TEXT DEFAULT '',   -- dia0.. | conversa
                direcao      TEXT NOT NULL,     -- saida | entrada
                texto        TEXT DEFAULT '',
                experimento  TEXT DEFAULT '',
                variante     TEXT DEFAULT '',
                externo_id   TEXT DEFAULT '',   -- thread do Gmail, id da mensagem
                criado_em    TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_msg_lead ON mensagens(lead_id);
            CREATE INDEX IF NOT EXISTS idx_msg_var  ON mensagens(experimento, variante);
            CREATE INDEX IF NOT EXISTS idx_msg_dir  ON mensagens(direcao, criado_em);
        """)


# ══════════════════════════════════════════════════════════════════════════════
#  Sorteio de variante
# ══════════════════════════════════════════════════════════════════════════════

def experimento_de(canal: str, etapa: str) -> Optional[str]:
    """Qual experimento ativo cobre este ponto de contato."""
    for nome, e in EXPERIMENTOS.items():
        if not e.get("ativo"):
            continue
        if e.get("etapa") and e["etapa"] != etapa:
            continue
        if e.get("canal") and e["canal"] != canal:
            continue
        return nome
    return None


def variante_para(lead_id: int, canal: str, etapa: str) -> Dict:
    """A variante deste lead neste ponto de contato."""
    nome = experimento_de(canal, etapa)
    if not nome:
        return {"experimento": "", "variante": "", "config": {}}
    exp = EXPERIMENTOS[nome]
    rotulos = sorted(exp["variantes"].keys())
    semente = f"{nome}:{lead_id}".encode("utf-8")
    idx = int(hashlib.sha256(semente).hexdigest(), 16) % len(rotulos)
    rotulo = rotulos[idx]
    return {"experimento": nome, "variante": rotulo,
            "config": dict(exp["variantes"][rotulo].get("config") or {})}


# ══════════════════════════════════════════════════════════════════════════════
#  Registro
# ══════════════════════════════════════════════════════════════════════════════

def registrar_saida(lead_id: Optional[int], canal: str, etapa: str, texto: str,
                    experimento: str = "", variante: str = "",
                    externo_id: str = "") -> int:
    init()
    with db.conn() as con:
        cur = con.execute("""
            INSERT INTO mensagens (lead_id, canal, etapa, direcao, texto,
                                   experimento, variante, externo_id)
            VALUES (?,?,?, 'saida', ?,?,?,?)
        """, (lead_id, canal, etapa, texto or "", experimento, variante, externo_id))
        return cur.lastrowid


def registrar_entrada(lead_id: Optional[int], canal: str, texto: str,
                      externo_id: str = "") -> int:
    """
    Uma resposta. Não carrega variante: ela é herdada, na análise, da última saída
    daquele lead no mesmo canal.
    """
    init()
    with db.conn() as con:
        cur = con.execute("""
            INSERT INTO mensagens (lead_id, canal, etapa, direcao, texto, externo_id)
            VALUES (?,?, 'resposta', 'entrada', ?,?)
        """, (lead_id, canal, texto or "", externo_id))
        return cur.lastrowid


# ══════════════════════════════════════════════════════════════════════════════
#  Métricas
# ══════════════════════════════════════════════════════════════════════════════

def _leads_com_reuniao() -> set:
    """Leads que chegaram a agendar. Vem do estágio da conversa."""
    try:
        import conversa_ia
        conversa_ia.init()
    except Exception:
        return set()
    with db.conn() as con:
        rows = con.execute(
            "SELECT DISTINCT lead_id FROM conversa_estado "
            "WHERE estagio='reuniao' AND lead_id IS NOT NULL").fetchall()
    return {r["lead_id"] for r in rows}


def metricas(experimento: Optional[str] = None) -> List[Dict]:
    """Uma linha por (experimento, variante, canal, etapa)."""
    init()
    reunioes = _leads_com_reuniao()

    with db.conn() as con:
        saidas = con.execute("""
            SELECT lead_id, canal, etapa, experimento, variante, criado_em
            FROM mensagens WHERE direcao='saida' AND lead_id IS NOT NULL
            ORDER BY criado_em
        """).fetchall()
        entradas = con.execute("""
            SELECT lead_id, canal, criado_em FROM mensagens
            WHERE direcao='entrada' AND lead_id IS NOT NULL
            ORDER BY criado_em
        """).fetchall()

    # última saída por (lead, canal) antes de cada entrada
    respondidos = set()          # (lead, canal, etapa, experimento, variante)
    tempos = {}
    for ent in entradas:
        anterior = None
        for s in saidas:
            if s["lead_id"] == ent["lead_id"] and s["canal"] == ent["canal"] \
                    and s["criado_em"] <= ent["criado_em"]:
                anterior = s
        if not anterior:
            continue
        chave = (anterior["experimento"], anterior["variante"], anterior["canal"],
                 anterior["etapa"], anterior["lead_id"])
        if chave in respondidos:
            continue
        respondidos.add(chave)
        try:
            t0 = datetime.strptime(anterior["criado_em"][:19], "%Y-%m-%d %H:%M:%S")
            t1 = datetime.strptime(ent["criado_em"][:19], "%Y-%m-%d %H:%M:%S")
            tempos.setdefault(chave[:4], []).append((t1 - t0).total_seconds() / 3600)
        except Exception:
            pass

    grupos = {}
    for s in saidas:
        if experimento and s["experimento"] != experimento:
            continue
        g = (s["experimento"], s["variante"], s["canal"], s["etapa"])
        grupos.setdefault(g, set()).add(s["lead_id"])

    saida = []
    for (exp, var, canal, etapa), leads in sorted(grupos.items()):
        resp = {c[4] for c in respondidos
                if (c[0], c[1], c[2], c[3]) == (exp, var, canal, etapa)}
        reun = leads & reunioes
        horas = tempos.get((exp, var, canal, etapa), [])
        medivel = canal in CANAIS_MEDIVEIS
        saida.append({
            "experimento": exp, "variante": var, "canal": canal, "etapa": etapa,
            "enviados": len(leads),
            "respostas": len(resp) if medivel else None,
            "taxa_resposta": (len(resp) / len(leads)) if (medivel and leads) else None,
            "reunioes": len(reun) if medivel else None,
            "taxa_reuniao": (len(reun) / len(leads)) if (medivel and leads) else None,
            "horas_ate_resposta": (sum(horas) / len(horas)) if horas else None,
            "medivel": medivel,
        })
    return saida


def comparar(experimento: str) -> Dict:
    """Compara as variantes de um experimento e diz se já dá para concluir."""
    linhas = [m for m in metricas(experimento) if m["medivel"]]
    if not linhas:
        return {"experimento": experimento, "conclusivo": False,
                "motivo": "nenhum envio em canal medível ainda", "linhas": []}

    por_var = {}
    for m in linhas:
        v = por_var.setdefault(m["variante"], {"enviados": 0, "respostas": 0,
                                               "reunioes": 0})
        v["enviados"] += m["enviados"]
        v["respostas"] += m["respostas"] or 0
        v["reunioes"] += m["reunioes"] or 0
    for v in por_var.values():
        v["taxa_resposta"] = v["respostas"] / v["enviados"] if v["enviados"] else 0
        v["taxa_reuniao"] = v["reunioes"] / v["enviados"] if v["enviados"] else 0

    menor = min((v["enviados"] for v in por_var.values()), default=0)
    conclusivo = menor >= MINIMO_PARA_CONCLUIR and len(por_var) >= 2
    vencedor = max(por_var, key=lambda k: por_var[k]["taxa_reuniao"]) if conclusivo else None
    return {
        "experimento": experimento, "variantes": por_var,
        "menor_amostra": menor, "minimo": MINIMO_PARA_CONCLUIR,
        "conclusivo": conclusivo, "vencedor": vencedor,
        "motivo": "" if conclusivo else
                  f"amostra insuficiente: a menor variante tem {menor} envio(s), "
                  f"o mínimo para concluir é {MINIMO_PARA_CONCLUIR}",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Relatório
# ══════════════════════════════════════════════════════════════════════════════

def _pct(x) -> str:
    return "  n/d" if x is None else f"{x*100:5.1f}%"


def relatorio():
    linhas = metricas()
    print("=" * 88)
    print("  DESEMPENHO POR PONTO DE CONTATO")
    print("=" * 88)
    if not linhas:
        print("  Nenhuma mensagem registrada ainda.")
        return
    print(f"  {'canal':<10} {'etapa':<14} {'variante':<13} {'env':>4} "
          f"{'resp':>6} {'taxa':>7} {'reun':>5} {'taxa':>7}  {'h/resp':>7}")
    print("  " + "-" * 84)
    for m in linhas:
        h = f"{m['horas_ate_resposta']:.1f}" if m["horas_ate_resposta"] else "   -"
        marca = "" if m["medivel"] else "  (sem leitura de resposta)"
        print(f"  {m['canal']:<10} {m['etapa']:<14} {m['variante'] or '-':<13} "
              f"{m['enviados']:>4} "
              f"{(m['respostas'] if m['respostas'] is not None else '-'):>6} "
              f"{_pct(m['taxa_resposta']):>7} "
              f"{(m['reunioes'] if m['reunioes'] is not None else '-'):>5} "
              f"{_pct(m['taxa_reuniao']):>7}  {h:>7}{marca}")

    ativos = [n for n, e in EXPERIMENTOS.items() if e.get("ativo")]
    for nome in ativos:
        c = comparar(nome)
        print()
        print("=" * 88)
        print(f"  EXPERIMENTO: {nome} — {EXPERIMENTOS[nome]['descricao']}")
        print("=" * 88)
        if not c.get("variantes"):
            print("  ", c.get("motivo"))
            continue
        for var, v in sorted(c["variantes"].items()):
            desc = EXPERIMENTOS[nome]["variantes"].get(var, {}).get("descricao", "")
            print(f"  {var:<13} {v['enviados']:>4} envios · "
                  f"resposta {_pct(v['taxa_resposta'])} · "
                  f"reunião {_pct(v['taxa_reuniao'])}")
            print(f"  {'':<13} {desc}")
        print()
        if c["conclusivo"]:
            print(f"  VENCEDOR pela taxa de reunião: {c['vencedor']}")
        else:
            print(f"  SEM CONCLUSÃO. {c['motivo']}")
            print("  A taxa de resposta acima é sinal, não veredito: versão que gera")
            print("  resposta e não gera reunião consome seu tempo e queima o lead.")


if __name__ == "__main__":
    import sys
    init()
    if "--experimentos" in sys.argv:
        for nome, e in EXPERIMENTOS.items():
            estado = "ATIVO " if e.get("ativo") else "parado"
            alvo = f"{e.get('canal') or 'todos os canais'} · {e.get('etapa')}"
            print(f"[{estado}] {nome:<16} {alvo}")
            print(f"           {e['descricao']}")
            for v, d in e["variantes"].items():
                print(f"             - {v:<12} {d['descricao']}")
    else:
        relatorio()
