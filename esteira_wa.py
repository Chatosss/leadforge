"""esteira_wa.py — Onde cada lead está na esteira de WhatsApp, e quando vence."""

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import config
import db

FMT = "%Y-%m-%d %H:%M:%S"


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    for f in (FMT, "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(ts)[:19], f)
        except ValueError:
            continue
    return None


def _etapa_canonica(etapa: str) -> Optional[str]:
    """Traduz etapas antigas para a esteira nova (ver ESTEIRA_ETAPAS_LEGADAS)."""
    if etapa in config.ESTEIRA_ETAPAS:
        return etapa
    return config.ESTEIRA_ETAPAS_LEGADAS.get(etapa)


def _por_etapa(etapa: str) -> Dict:
    for e in config.ESTEIRA_WHATSAPP:
        if e["etapa"] == etapa:
            return e
    return {}


def estado(lead_id: int, agora: Optional[datetime] = None) -> Dict:
    """O estado da esteira para um lead."""
    agora = agora or datetime.now()
    with db.conn() as con:
        linhas = con.execute(
            "SELECT etapa, sent_at, status FROM touches "
            "WHERE lead_id=? AND canal='whatsapp' AND status IN ('sent',?) "
            "ORDER BY sent_at", (lead_id, STATUS_MANUAL)).fetchall()

    # Dois relógios, e confundi-los quebra coisas diferentes: `quando` a PRIMEIRA
    # vez que cada etapa saiu.
    enviadas, quando = [], {}
    ultimo_envio = None
    for r in linhas:
        can = _etapa_canonica(r["etapa"])
        ts = _parse(r["sent_at"])
        # o passo marcado À MÃO conta para saber onde o lead está, mas NÃO para
        # o "nunca duas mensagens no mesmo dia": nada foi enviado, então nada
        # tem que esperar 20h por causa dele
        if (r["status"] == "sent" and ts
                and (ultimo_envio is None or ts > ultimo_envio)):
            ultimo_envio = ts
        if not can:
            continue
        if can not in quando or (ts and quando[can] and ts < quando[can]):
            quando[can] = ts
        if can not in enviadas:
            enviadas.append(can)

    enviadas.sort(key=lambda e: config.ESTEIRA_ETAPAS.index(e))
    ultima = enviadas[-1] if enviadas else None
    ultima_em = quando.get(ultima) if ultima else None
    primeira_em = quando.get(enviadas[0]) if enviadas else None

    faltando = [e for e in config.ESTEIRA_ETAPAS if e not in enviadas]
    proxima = faltando[0] if faltando else None

    if proxima is None:
        return {"enviadas": enviadas, "ultima_etapa": ultima, "ultima_em": ultima_em,
                "ultimo_envio": ultimo_envio, "proxima": None, "proxima_rotulo": "", "passo": len(enviadas),
                "total": len(config.ESTEIRA_ETAPAS), "vence_em": None,
                "pronto": False, "concluida": True}

    cfg = _por_etapa(proxima)
    if not enviadas or primeira_em is None:
        # nunca recebeu nada: o primeiro toque pode sair agora
        vence_em = agora
    else:
        vence_em = primeira_em + timedelta(days=cfg.get("dia", 0))
        # nunca duas mensagens coladas para o mesmo contato, mesmo que o
        # calendário diga que sim (ex.: lead devolvido à esteira com atraso)
        if ultimo_envio:
            minimo = ultimo_envio + timedelta(hours=config.ESTEIRA_MIN_HORAS_ENTRE_TOQUES)
            vence_em = max(vence_em, minimo)

    return {"enviadas": enviadas, "ultima_etapa": ultima, "ultima_em": ultima_em,
            "ultimo_envio": ultimo_envio, "proxima": proxima, "proxima_rotulo": cfg.get("rotulo", proxima),
            "passo": len(enviadas), "total": len(config.ESTEIRA_ETAPAS),
            "vence_em": vence_em, "pronto": vence_em <= agora, "concluida": False}


def dentro_da_janela(agora: Optional[datetime] = None) -> bool:
    """Horário comercial. Mensagem fria fora dele irrita e rende denúncia."""
    agora = agora or datetime.now()
    ini, fim = config.ESTEIRA_JANELA
    return ini <= agora.hour < fim


# Toque entregue ao bot conta para o teto mesmo antes da confirmação voltar.
STATUS_QUE_CONTAM = ("sent", "queued")


def enviados_hoje() -> int:
    marcas = ",".join("?" * len(STATUS_QUE_CONTAM))
    with db.conn() as con:
        return con.execute(
            f"SELECT COUNT(*) FROM touches WHERE canal='whatsapp' "
            f"AND status IN ({marcas}) "
            f"AND date(COALESCE(sent_at, created_at))=date('now','localtime')",
            STATUS_QUE_CONTAM).fetchone()[0]


# ── Primeiro contato ─────────────────────────────────────────────────────────
# Até 08/10 havia aqui um teto geral de leads novos por dia.
ETAPAS_PRIMEIRO_CONTATO = ("dia0_ping", "dia0", "wa1_abertura")

# Passo marcado à mão no card: conta para saber onde o lead está na régua e
# NUNCA para "mensagem enviada". Um `status='sent'` aqui seria o banco dizendo
# que mandou algo que não saiu, que é a regra mais dura do projeto.
STATUS_MANUAL = "manual"


def enviados_novos_hoje() -> int:
    """Mensagens de PRIMEIRO contato que saíram hoje (conta mensagem, não lead)."""
    marcas = ",".join("?" * len(ETAPAS_PRIMEIRO_CONTATO))
    status = ",".join("?" * len(STATUS_QUE_CONTAM))
    with db.conn() as con:
        return con.execute(
            f"SELECT COUNT(*) FROM touches WHERE canal='whatsapp' "
            f"AND status IN ({status}) AND etapa IN ({marcas}) "
            f"AND date(COALESCE(sent_at, created_at))=date('now','localtime')",
            (*STATUS_QUE_CONTAM, *ETAPAS_PRIMEIRO_CONTATO)).fetchone()[0]


def definir_passo(lead_id: int, ordem: int) -> Dict:
    """
    Move o lead para uma posição da sequência: `ordem` é a PRÓXIMA mensagem (0 =
    abertura). `ordem` igual ao total marca a esteira como concluída.
    """
    etapas = config.ESTEIRA_ETAPAS
    if not 0 <= ordem <= len(etapas):
        raise ValueError(f"posição inválida: {ordem}")

    with db.conn() as con:
        linhas = [dict(r) for r in con.execute(
            "SELECT id, etapa, status FROM touches WHERE lead_id=? AND canal='whatsapp' "
            "AND status IN ('sent',?)", (lead_id, STATUS_MANUAL))]

    real, manuais = {}, {}
    for r in linhas:
        can = _etapa_canonica(r["etapa"])
        if not can:
            continue
        if r["status"] == "sent":
            real[can] = True
        else:
            manuais.setdefault(can, []).append(r["id"])

    presos = [e for i, e in enumerate(etapas) if i >= ordem and real.get(e)]
    if presos:
        rotulos = ", ".join(_por_etapa(e).get("rotulo", e) for e in presos)
        raise ValueError(f"não dá para voltar o lead: {rotulos} já saiu de "
                         f"verdade para esta clínica")

    criados, removidos = [], 0
    with db.conn() as con:
        for i, etapa in enumerate(etapas):
            if i < ordem and etapa not in real and etapa not in manuais:
                con.execute(
                    "INSERT INTO touches(lead_id,canal,etapa,status,scheduled_for,"
                    "sent_at,detalhe) VALUES(?,?,?,?,date('now','localtime'),"
                    "datetime('now','localtime'),?)",
                    (lead_id, "whatsapp", etapa, STATUS_MANUAL,
                     "passo marcado à mão pelo dono (nada foi enviado)"))
                criados.append(etapa)
            if i >= ordem and etapa in manuais:
                for tid in manuais[etapa]:
                    con.execute("DELETE FROM touches WHERE id=?", (tid,))
                    removidos += 1
    return {"ok": True, "marcados": criados, "desmarcados": removidos}


def ordem_da_etapa(etapa: str) -> int:
    """A posição da etapa na sequência da campanha (0 = abertura)."""
    etapa = _etapa_canonica(etapa) or etapa
    try:
        return config.ESTEIRA_ETAPAS.index(etapa)
    except ValueError:
        return -1


def respondeu(lead_id: int) -> bool:
    """O lead já respondeu alguma coisa? Se respondeu, a esteira não o toca mais."""
    try:
        import bridge
        return bridge.ja_respondeu(lead_id)
    except Exception as e:
        print(f"[esteira_wa] lead {lead_id} segurado: não deu para checar se "
              f"já respondeu ({e})")
        return True


def fila(agora: Optional[datetime] = None) -> List[Dict]:
    """Os leads da coluna "Contato iniciado" que têm toque vencido, em ordem."""
    agora = agora or datetime.now()
    with db.conn() as con:
        leads = [dict(r) for r in con.execute(
            "SELECT id, nome FROM leads WHERE kanban='contato_iniciado'")]
    saida = []
    for l in leads:
        st = estado(l["id"], agora)
        if st["pronto"] and not st["concluida"] and not respondeu(l["id"]):
            saida.append({**l, **st})
    saida.sort(key=lambda x: x["vence_em"] or agora)
    return saida


def resumo_coluna(agora: Optional[datetime] = None) -> List[Dict]:
    """Todo lead de 'Contato iniciado' com seu passo, para a tela."""
    agora = agora or datetime.now()
    with db.conn() as con:
        leads = [dict(r) for r in con.execute(
            "SELECT id, nome FROM leads WHERE kanban='contato_iniciado' ORDER BY nome")]
    for l in leads:
        st = estado(l["id"], agora)
        l.update({
            "passo": st["passo"], "total": st["total"],
            "proxima": st["proxima"], "proxima_rotulo": st["proxima_rotulo"],
            "concluida": st["concluida"], "pronto": st["pronto"],
            "ultima_em": st["ultima_em"].strftime(FMT) if st["ultima_em"] else None,
            "vence_em": st["vence_em"].strftime(FMT) if st["vence_em"] else None,
            # sem texto para a próxima posição, este lead não sai: a coluna
            # precisa contar isso como parado, não como pronto
            "sem_texto": bool(st["proxima"]) and not tem_texto(
                dict(db.get_lead(l["id"]) or {}), st["proxima"]),
        })
    return leads


# ── Disparo ──────────────────────────────────────────────────────────────────

def montar_mensagem(lead: Dict, etapa: str) -> str:
    """O texto da etapa, a partir do template editável dela."""
    import dashboard_db as ddb

    tpl = None
    cid = lead.get("campanha_id")
    if cid:
        # O lead fica na campanha que recebeu na abertura, e SÓ nela: cair no template
        # solto da etapa misturaria o reforço de uma campanha com a abertura de outra —
        # textos que se contradizem e medição que não vale.
        tpl = ddb.template_da_campanha(cid, ordem_da_etapa(etapa))
    else:
        tpl = ddb.template_da_etapa(etapa)
    if not tpl:
        return ""
    return renderizar(lead, tpl.get("corpo") or "")


def tem_texto(lead: Dict, etapa: str) -> bool:
    """Existe mensagem para este lead nesta posição?"""
    import dashboard_db as ddb

    if not etapa:
        return False
    cid = lead.get("campanha_id")
    tpl = (ddb.template_da_campanha(cid, ordem_da_etapa(etapa)) if cid
           else ddb.template_da_etapa(etapa))
    return bool(tpl and (tpl.get("corpo") or "").strip())


def renderizar(lead: Dict, corpo: str, limpar: bool = True) -> str:
    """Troca as variáveis do template pelos dados do lead."""
    import personalizar
    return personalizar.renderizar(lead, corpo, limpar=limpar)


def rodar(agora: Optional[datetime] = None, limite: Optional[int] = None,
          verbose: bool = True, seco: bool = False) -> Dict:
    """Envia os toques vencidos. É o coração da automação."""
    agora = agora or datetime.now()
    saida = {"quando": agora.strftime(FMT), "enviados": [], "pulados": [],
             "motivo": ""}

    if not dentro_da_janela(agora):
        ini, fim = config.ESTEIRA_JANELA
        saida["motivo"] = f"fora da janela de envio ({ini}h-{fim}h)"
        return saida

    pendentes = fila(agora)
    if limite is not None:
        pendentes = pendentes[:limite]
    if not pendentes:
        saida["motivo"] = "nenhum toque vencido"
        return saida

    # Só vai para a rodada o que cabe nas vagas de hoje das contas: sem este
    # corte, a IA personalizava todos os vencidos (de novo a cada 5 min) e o
    # lote recusava a sobra. O limite de cada conta ainda vale no envio.
    import limites
    vagas = limites.vagas_hoje("whatsapp")
    if vagas <= 0:
        saida["motivo"] = ("todas as contas de WhatsApp atingiram o limite de hoje "
                           "(limites na aba Contas)")
        return saida
    pendentes = pendentes[:vagas]

    if not seco:
        from senders import whatsapp_sender
    import personalizar

    lote, dados = [], {}
    for item in pendentes:
        lead = db.get_lead(item["id"])
        if not lead:
            continue
        lead = dict(lead)
        etapa = item["proxima"]
        texto = montar_mensagem(lead, etapa)
        if not texto:
            saida["pulados"].append({
                "lead_id": lead["id"], "nome": lead.get("nome"), "etapa": etapa,
                "motivo": ("a campanha deste lead não tem mensagem nesta "
                           "posição: a sequência dela terminou"
                           if lead.get("campanha_id")
                           else "etapa sem template cadastrado")})
            continue
        if seco:
            # O seco não chama a IA: é conferência rápida, e 40 leads seriam 40
            # chamadas. Diz só se o envio de verdade vai personalizar.
            saida["enviados"].append({"lead_id": lead["id"], "nome": lead.get("nome"),
                                      "etapa": etapa, "seco": True,
                                      "previa": texto[:120],
                                      "personaliza": personalizar.ativa("whatsapp")})
            continue
        # Cada lead com a SUA mensagem, se a personalização do WhatsApp estiver
        # ligada. Falhou ou foi reprovada: volta o texto acima, e o envio segue.
        texto = personalizar.aplicar(lead, "whatsapp", texto, etapa=etapa)
        dados[lead["id"]] = {"nome": lead.get("nome"), "etapa": etapa}
        lote.append({"lead_id": lead["id"], "mensagem": texto, "etapa": etapa,
                     "experimento": "esteira_wa", "variante": etapa})

    if seco or not lote:
        return saida

    # UM lote só, com todos os leads vencidos.
    try:
        r = whatsapp_sender.enviar_lote(lote, etapa="")
    except Exception as e:
        for lid, d in dados.items():
            saida["pulados"].append({"lead_id": lid, "nome": d["nome"],
                                     "etapa": d["etapa"],
                                     "motivo": f"erro no envio: {e}"})
        return saida

    bloqueados = {b["lead_id"]: b.get("motivo", "")
                  for b in (r.get("bloqueados") or [])}
    for lid, d in dados.items():
        motivo = bloqueados.get(lid)
        if motivo or not r.get("ok"):
            saida["pulados"].append({"lead_id": lid, "nome": d["nome"],
                                     "etapa": d["etapa"],
                                     "motivo": motivo or r.get("erro", "não saiu")})
            continue
        saida["enviados"].append({"lead_id": lid, "nome": d["nome"],
                                  "etapa": d["etapa"]})
        if verbose:
            print(f"  esteira: {(d['nome'] or '')[:36]} <- {d['etapa']}")
    saida["intervalo_seg"] = [config.ENVIO_DELAY_MIN, config.ENVIO_DELAY_MAX]
    return saida


_LACO = {"thread": None, "parar": False}


def iniciar_laco(intervalo_seg: int = 300):
    """Sobe o laço de fundo que roda `rodar()` de tempos em tempos."""
    import threading

    if _LACO["thread"] and _LACO["thread"].is_alive():
        return _LACO["thread"]

    def _loop():
        import time
        while not _LACO["parar"]:
            try:
                r = rodar(verbose=False)
                if r["enviados"]:
                    print(f"[esteira] {len(r['enviados'])} toque(s) enviados")
            except Exception as e:
                print(f"[esteira] erro no laço: {e}")
            time.sleep(intervalo_seg)

    t = threading.Thread(target=_loop, daemon=True, name="esteira_wa")
    _LACO["parar"] = False
    _LACO["thread"] = t
    t.start()
    return t


def parar_laco():
    _LACO["parar"] = True


if __name__ == "__main__":
    print(f"{'id':>5} {'passo':>7}  {'próxima':18} {'vence em':20} pronto")
    for l in resumo_coluna():
        print(f"{l['id']:>5} {l['passo']}/{l['total']:<5}  "
              f"{(l['proxima_rotulo'] or 'concluída'):18} "
              f"{str(l['vence_em'] or '-'):20} {'sim' if l['pronto'] else ''}")
