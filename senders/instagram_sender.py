"""
instagram_sender.py — Envio REAL de DM no Instagram pela cópia do bot (porta 5022).
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import config
import db
import identidade
import modo

BASE = config.DM_BOT_URL.rstrip("/")
_TIMEOUT = 20

# Onde o bot registra o que REALMENTE saiu, com hora.
_RASTREIO = config.BASE_DIR / "senders" / "dm_bot" / "perfis"


def _rastreio(nicho: str) -> Dict:
    arq = _RASTREIO / nicho / "rastreio_dms.json"
    try:
        return json.loads(arq.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _confirmar(alvos: List[Dict], nicho: str, etapa: str, desde: str,
               limite_s: int = 240):
    """Confere no rastreio do bot o que saiu de verdade e acerta o CRM."""
    espera, passo = 0, 5
    pendentes = {a["username"].lower(): a for a in alvos}
    while pendentes and espera < limite_s:
        time.sleep(passo)
        espera += passo
        registro = _rastreio(nicho)
        for user in list(pendentes):
            dado = registro.get(user) or {}
            if dado.get("data", "") > desde:
                a = pendentes.pop(user)
                _registrar(a["lead_id"], etapa, "sent",
                           f"DM para @{user} confirmada às {dado['data']} "
                           f"(identidade {a['confianca']}/100)")
    # quem sobrou não saiu: o bot pulou ou falhou
    for user, a in pendentes.items():
        _registrar(a["lead_id"], etapa, "skipped",
                   f"bot não confirmou o envio para @{user} "
                   f"(provável dedupe: o perfil já recebeu DM antes)")


def _estado_do_bot() -> Dict:
    """
    Contas abertas no painel e se há disparo rodando (vazio se o bot não responde).
    """
    try:
        with urllib.request.urlopen(f"{BASE}/api/janela-manual/status", timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def _contas_com_sessao() -> List[str]:
    try:
        import connection_manager as cm
        with db.conn() as con:
            contas = [r["identifier"] for r in con.execute(
                "SELECT identifier FROM accounts WHERE channel='instagram' "
                "AND status='active' ORDER BY id")]
        return [config.ig_conta_normalizada(i) for i in contas
                if cm.ig_perfil_sessao(i).get("ativa")]
    except Exception:
        return []


def conta_padrao() -> str:
    """Conta do Instagram que envia, quando ninguem escolheu uma."""
    contas = _contas_com_sessao()
    abertas = _contas_ocupadas(_estado_do_bot())
    livres = [c for c in contas if c not in abertas]
    return (livres or contas or [""])[0]


def _contas_ocupadas(st) -> set:
    """
    Pasta presa numa janela externa, ou conta que o operador está OLHANDO no painel. A
    regra mora no bot (`_contas_ocupadas`); a união abaixo é só para um bot antigo que
    ainda não publica o campo.
    """
    if st.get("contas_ocupadas") is not None:
        return set(st["contas_ocupadas"])
    return set(st.get("perfis_abertos") or []) | set(st.get("contas_vistas") or [])


def conta_do_primeiro_contato(usuario: str) -> str:
    """
    A conta do Instagram que mandou a PRIMEIRA DM para este @ ('' se nenhuma).
    `envios_conta` guarda o @ de destino (não o lead) e a conta que enviou.
    """
    u = str(usuario or "").strip().lstrip("@").lower()
    if not u:
        return ""
    try:
        with db.conn() as con:
            r = con.execute(
                "SELECT a.identifier FROM envios_conta e JOIN accounts a ON a.id = e.account_id "
                "WHERE e.canal='instagram' AND lower(e.destino)=? ORDER BY e.sent_at, e.id LIMIT 1",
                (u,)).fetchone()
        return config.ig_conta_normalizada(r["identifier"]) if r else ""
    except Exception:
        return ""


def _conta_do_lead(alvos) -> str:
    """
    Follow-up de UM lead sai pela conta que falou com ele primeiro, se ela tem sessão e
    não está presa numa janela externa: chegar de um perfil diferente "retomando a
    conversa" tem cara de spam. Sem isso, vale a conta padrão.
    """
    if len(alvos) != 1:
        return ""
    original = conta_do_primeiro_contato(alvos[0].get("username"))
    if not original or original not in _contas_com_sessao():
        return ""
    if original in _contas_ocupadas(_estado_do_bot()):
        return ""
    return original


def sem_conta_livre() -> str:
    """
    Motivo para NÃO tentar agora (todas as contas abertas no painel, ou um disparo
    rodando), ou "" se dá. O follow-up pergunta ANTES de pedir o texto à IA: sem isto,
    cada adiamento gastava uma chamada de personalização.
    """
    st = _estado_do_bot()
    if not st:
        return ""                     # bot fora do ar: o envio diz o resto
    if st.get("prospeccao_rodando"):
        return "o bot de Instagram está com um disparo em andamento"
    contas = _contas_com_sessao()
    abertas = _contas_ocupadas(st)
    if contas and all(c in abertas for c in contas):
        return "todas as contas do Instagram estão abertas no painel"
    return ""


def _post(caminho: str, corpo: Dict) -> Dict:
    dados = json.dumps(corpo).encode("utf-8")
    req = urllib.request.Request(f"{BASE}{caminho}", data=dados, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"status": "erro", "mensagem": f"HTTP {e.code}"}
    except Exception as e:
        return {"status": "erro",
                "mensagem": f"bot de DM inacessível em {BASE}: {e}"}


def status() -> Dict:
    """O bot de DM está no ar? Ele não tem /api/status, então testamos a porta."""
    try:
        with urllib.request.urlopen(BASE, timeout=5) as r:
            r.read(1)
        return {"no_ar": True, "url": BASE}
    except urllib.error.HTTPError:
        return {"no_ar": True, "url": BASE}       # respondeu, mesmo que 404
    except Exception as e:
        return {"no_ar": False, "url": BASE, "erro": str(e)}


def enviar_username(username: str, mensagem: str, nicho: str = "clinicas",
                    conta: str = "") -> Dict:
    """Envio cru para um @, SEM trava de identidade e sem registrar toque."""
    u = (username or "").strip().lstrip("@")
    if not u or not mensagem:
        return {"ok": False, "erro": "username e mensagem são obrigatórios"}
    remetente = config.ig_conta_normalizada(conta) or conta_padrao()
    r = _post("/api/enviar_lote",
              {"nicho": nicho, "conta": remetente,
               "alvos": [{"username": u, "mensagem": mensagem}]})
    return {"ok": r.get("status") == "sucesso", "username": u,
            "conta": remetente or None, "resposta": r}


def enviar_lote(itens: List[Dict], etapa: str = "dia0",
                nicho: str = "clinicas", revalidar: bool = False,
                confirmar: bool = True, bloquear_ate_confirmar: bool = False,
                conta: str = "") -> Dict:
    """itens: [{lead_id, mensagem}]."""
    alvos, bloqueados = [], []
    for it in itens:
        lead_id = it.get("lead_id")
        gate = identidade.pode_enviar(lead_id, "instagram", revalidar=revalidar)
        if not gate["ok"]:
            bloqueados.append({"lead_id": lead_id, "motivo": gate["motivo"]})
            _registrar(lead_id, etapa, "skipped", gate["motivo"])
            continue
        livre = modo.permite("instagram", gate["valor"])
        if not livre["ok"]:
            bloqueados.append({"lead_id": lead_id, "motivo": livre["motivo"]})
            _registrar(lead_id, etapa, "skipped", livre["motivo"])
            continue
        alvos.append({"username": gate["valor"], "mensagem": it["mensagem"],
                      "lead_id": lead_id, "confianca": gate["confianca"],
                      "experimento": it.get("experimento", ""),
                      "variante": it.get("variante", "")})

    if not alvos:
        return {"ok": False, "erro": "nenhum perfil confirmado pelo cruzamento",
                "bloqueados": bloqueados}

    # marca o instante ANTES de despachar: só conta como enviado o registro do
    # bot com hora posterior a este
    desde = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    remetente = config.ig_conta_normalizada(conta) or _conta_do_lead(alvos) or conta_padrao()
    r = _post("/api/enviar_lote",
              {"nicho": nicho, "conta": remetente,
               "alvos": [{"username": a["username"], "mensagem": a["mensagem"]}
                         for a in alvos]})
    aceito = r.get("status") == "sucesso"
    if not aceito and r.get("ocupada"):
        # a conta remetente está aberta no painel: nada saiu e nada deu errado
        # com o lead — não grava toque `failed`, só avisa quem chamou
        return {"ok": False, "ocupada": True, "aceitos": 0, "bloqueados": bloqueados,
                "erro": str(r.get("mensagem") or "conta remetente aberta no painel"),
                "resposta": r}
    if not aceito:
        for a in alvos:
            _registrar(a["lead_id"], etapa, "failed", str(r.get("mensagem", r)))
        return {"ok": False, "aceitos": 0, "bloqueados": bloqueados, "resposta": r}

    # Aceito não é enviado. O CRM só recebe `sent` depois que o rastreio do bot
    # confirmar, o que roda em segundo plano para não travar quem chamou.
    for a in alvos:
        _registrar(a["lead_id"], etapa, "queued",
                   f"DM para @{a['username']} na fila do bot")
        try:
            import experimentos
            experimentos.registrar_saida(a["lead_id"], "instagram", etapa,
                                         a["mensagem"], a.get("experimento", ""),
                                         a.get("variante", ""))
        except Exception:
            pass
        lead = db.get_lead(a["lead_id"])
        if lead and lead.get("status") == "qualified":
            db.set_status(a["lead_id"], "contacted")

    if confirmar:
        t = threading.Thread(target=_confirmar,
                             args=(alvos, nicho, etapa, desde), daemon=True)
        t.start()
        if bloquear_ate_confirmar:
            t.join()

    return {"ok": True, "aceitos": len(alvos), "bloqueados": bloqueados,
            "resposta": r,
            "aviso": "aceito pelo bot; o CRM confirma o envio pelo rastreio"}


def reconciliar(nicho: str = "clinicas", limite_horas: int = 48) -> Dict:
    """Acerta toques que ficaram presos em `queued`."""
    registro = _rastreio(nicho)
    corrigidos, sem_registro = [], []
    with db.conn() as con:
        pendentes = con.execute("""
            SELECT id, lead_id, etapa, detalhe, created_at FROM touches
            WHERE canal='instagram' AND status='queued'
              AND julianday('now') - julianday(created_at) <= ?
        """, (limite_horas / 24.0,)).fetchall()

    for t in pendentes:
        m = re.search(r"@([A-Za-z0-9_.]+)", t["detalhe"] or "")
        if not m:
            continue
        user = m.group(1).lower()
        dado = registro.get(user) or {}
        if dado.get("data"):
            db.mark_touch(t["id"], "sent",
                          f"DM para @{user} confirmada às {dado['data']}")
            corrigidos.append(user)
        else:
            db.mark_touch(t["id"], "skipped",
                          f"bot não registrou envio para @{user} "
                          f"(provável dedupe: o perfil já recebeu DM antes)")
            sem_registro.append(user)
    return {"ok": True, "confirmados": corrigidos, "sem_registro": sem_registro,
            "pendentes": len(pendentes)}


def enviar(lead_id: int, mensagem: str, etapa: str = "dia0", **kw) -> Dict:
    """Conveniência para um lead só."""
    r = enviar_lote([{"lead_id": lead_id, "mensagem": mensagem}], etapa=etapa, **kw)
    if not r.get("ok") and r.get("bloqueados"):
        return {"ok": False, "bloqueado": True, "erro": r["bloqueados"][0]["motivo"]}
    return r


def _registrar(lead_id: int, etapa: str, status: str, detalhe: str):
    try:
        alvo = None
        for t in db.touches_for(lead_id):
            # `queued` também entra: é o toque que este mesmo envio criou ao ser
            # aceito pelo bot, e a confirmação tem que ATUALIZAR ele em vez de
            # criar um segundo toque para a mesma DM.
            if (t["canal"] == "instagram" and t["etapa"] == etapa
                    and t["status"] in ("scheduled", "queued")):
                alvo = t
                break
        if alvo:
            db.mark_touch(alvo["id"], status, detalhe)
        else:
            from datetime import date
            tid = db.schedule_touch(lead_id, "instagram", etapa, date.today().isoformat())
            db.mark_touch(tid, status, detalhe)
    except Exception:
        pass


if __name__ == "__main__":
    import sys
    print("Bot de DM:", status())
    if len(sys.argv) >= 3:
        print(enviar(int(sys.argv[1]), " ".join(sys.argv[2:])))
    else:
        print("uso: python -m senders.instagram_sender <lead_id> <mensagem>")
