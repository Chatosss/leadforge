"""envio_fila.py — Manda o primeiro contato um lead por vez, verificando na hora."""

import random
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import config
import db

FMT = "%Y-%m-%d %H:%M:%S"
COLUNA_PROBLEMA = "problema"
COLUNA_FEITO = "contato_iniciado"

# Estado da rodada em memória. Um trabalhador por processo: duas rodadas ao
# mesmo tempo mandariam mensagens coladas e furariam o espaçamento.
_LOCK = threading.Lock()
_ESTADO: Dict = {
    "rodando": False, "total": 0, "feitos": 0, "enviados": 0, "problemas": 0,
    "repetidos": 0,
    "atual": None, "proximo_em": None, "iniciado_em": None, "fim": None,
    "motivo_parada": "", "log": [],
}


def estado() -> Dict:
    with _LOCK:
        return dict(_ESTADO, log=list(_ESTADO["log"])[-40:])


def _anotar(texto: str):
    with _LOCK:
        _ESTADO["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {texto}")
        del _ESTADO["log"][:-200]


def _intervalo() -> float:
    return random.uniform(config.ENVIO_DELAY_MIN, config.ENVIO_DELAY_MAX)


def _para_problema(lead_id: int, nome: str, motivo: str):
    """Tira o lead da fila e escreve o porquê onde o operador vai olhar."""
    import dashboard_db as ddb
    try:
        ddb.set_kanban(lead_id, COLUNA_PROBLEMA)
    except Exception as e:
        _anotar(f"não deu para mover {nome} para Problema: {e}")
    try:
        ddb.add_nota(lead_id, f"Envio não saiu: {motivo}")
    except Exception:
        pass
    _anotar(f"PROBLEMA · {nome}: {motivo}")


def _trabalhar(ids: List[int], etapa: str):
    import dashboard_db as ddb
    import esteira_wa as ew
    import modo
    from senders import whatsapp_sender

    parada = ""
    try:
        for pos, lead_id in enumerate(ids):
            lead = db.get_lead(lead_id)
            if not lead:
                continue
            lead = dict(lead)
            nome = (lead.get("nome") or f"lead {lead_id}")[:40]
            with _LOCK:
                pedido_parar = _ESTADO.get("parar_pedido")
                _ESTADO["atual"] = {"lead_id": lead_id, "nome": nome,
                                    "posicao": pos + 1}
            if pedido_parar:
                parada = "parado pelo operador"
                break

            if not ew.dentro_da_janela():
                ini, fim = config.ESTEIRA_JANELA
                parada = f"fora da janela de envio ({ini}h-{fim}h)"
                break

            # campanha: a dele, ou a próxima do rodízio
            campanhas = ddb.campanhas_para_rodizio()
            if not campanhas:
                parada = "nenhuma campanha ativa com mensagem de abertura"
                break
            por_id = {c["id"]: c for c in campanhas}
            camp = por_id.get(lead.get("campanha_id"))
            if not camp:
                camp = ddb.proxima_campanha(campanhas)

            texto = ew.renderizar(lead, camp["mensagens"][0].get("corpo") or "")
            if texto:
                # A abertura DESTE lead, se a personalização do WhatsApp estiver
                # ligada. Falhou ou foi reprovada: volta o texto acima.
                import personalizar
                texto = personalizar.aplicar(lead, "whatsapp", texto, etapa=etapa)
            if not texto:
                _para_problema(lead_id, nome,
                               f"a abertura da campanha '{camp['nome']}' "
                               f"rendeu mensagem vazia")
                with _LOCK:
                    _ESTADO["problemas"] += 1
                    _ESTADO["feitos"] += 1
                continue

            # ── A VERIFICAÇÃO ACONTECE AQUI, e não no início da rodada ──────
            # `enviar_lote` de um alvo só: ele aplica identidade e modo de envio
            # imediatamente antes de o bot mandar esta mensagem.
            r = whatsapp_sender.enviar_lote(
                [{"lead_id": lead_id, "mensagem": texto, "etapa": etapa,
                  "experimento": "campanha", "variante": camp["nome"]}],
                etapa=etapa)

            bloqueados = r.get("bloqueados") or []
            if bloqueados:
                bl = bloqueados[0]
                if bl.get("repeticao"):
                    # não é defeito do lead: ele já recebeu. O lugar dele é
                    # "Contato iniciado", e deixá-lo na fila só faria a próxima
                    # rodada tentar de novo
                    try:
                        ddb.set_kanban(lead_id, COLUNA_FEITO)
                    except Exception:
                        pass
                    with _LOCK:
                        _ESTADO["repetidos"] = _ESTADO.get("repetidos", 0) + 1
                        _ESTADO["feitos"] += 1
                    _anotar(f"já tinha recebido · {nome}")
                    continue
                _para_problema(lead_id, nome,
                               bl.get("motivo", "verificação recusou"))
                with _LOCK:
                    _ESTADO["problemas"] += 1
                    _ESTADO["feitos"] += 1
                continue

            if not r.get("ok"):
                # infraestrutura: o lead não tem culpa e não pode ser punido com
                # a coluna Problema. A rodada para e ele continua na fila.
                parada = r.get("erro") or "o envio não foi confirmado"
                break

            try:
                ddb.definir_campanha_do_lead(lead_id, camp["id"])
                ddb.registrar_campanha_usada(camp["id"])
                ddb.set_kanban(lead_id, COLUNA_FEITO)
            except Exception as e:
                _anotar(f"enviado, mas não moveu {nome}: {e}")
            with _LOCK:
                _ESTADO["enviados"] += 1
                _ESTADO["feitos"] += 1
            _anotar(f"enviado · {nome} · campanha {camp['nome']}")

            if pos < len(ids) - 1:
                espera = _intervalo()
                with _LOCK:
                    _ESTADO["proximo_em"] = round(espera)
                time.sleep(espera)
    except Exception as e:
        parada = f"erro inesperado: {e}"
        _anotar(parada)
    finally:
        with _LOCK:
            _ESTADO["rodando"] = False
            _ESTADO["atual"] = None
            _ESTADO["proximo_em"] = None
            _ESTADO["fim"] = datetime.now().strftime(FMT)
            _ESTADO["motivo_parada"] = parada
        if parada:
            _anotar(f"rodada interrompida: {parada}")
        else:
            _anotar("rodada concluída")


def disparar(ids: List[int], etapa: str = "dia0_ping") -> Dict:
    """Sobe o trabalhador. Devolve na hora: quem espera é o operador, na tela."""
    with _LOCK:
        if _ESTADO["rodando"]:
            return {"ok": False, "erro": "já existe uma rodada de envio em "
                                         "andamento", "estado": dict(_ESTADO)}
        _ESTADO.update({"rodando": True, "total": len(ids), "feitos": 0,
                        "enviados": 0, "problemas": 0, "repetidos": 0,
                        "atual": None,
                        "proximo_em": None, "fim": None, "motivo_parada": "",
                        "parar_pedido": False,
                        "iniciado_em": datetime.now().strftime(FMT), "log": []})
    _anotar(f"rodada iniciada: {len(ids)} lead(s)")
    threading.Thread(target=_trabalhar, args=(list(ids), etapa),
                     daemon=True).start()
    return {"ok": True, "total": len(ids)}


def parar() -> Dict:
    """Impede os PRÓXIMOS. O lead que já está no ar termina."""
    with _LOCK:
        if not _ESTADO["rodando"]:
            return {"ok": False, "erro": "nenhuma rodada em andamento"}
        _ESTADO["parar_pedido"] = True
    _anotar("parada pedida: termina o lead atual e não começa o próximo")
    return {"ok": True}
