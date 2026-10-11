"""app.py — Dashboard visual (Flask) do LeadForge."""

import csv
import io
import os
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

from flask import (Flask, jsonify, request, send_file, send_from_directory,
                   render_template, Response)

# raiz do projeto no path (app está em dashboard/)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config          # noqa: E402
import db              # noqa: E402
import dashboard_db as ddb   # noqa: E402
import gov_enrich      # noqa: E402
import web_enrich      # noqa: E402
import whatsapp_check  # noqa: E402
import pagespeed       # noqa: E402
import diagnostico as diag_mod   # noqa: E402
import proposta as prop_mod      # noqa: E402
import bridge          # noqa: E402
import documento_pdf   # noqa: E402

ATTACH_DIR = config.DATA_DIR / "attachments"
ATTACH_DIR.mkdir(parents=True, exist_ok=True)

# Palavra que libera a exclusão DEFINITIVA de lead real. Digitada, no mesmo
# espírito do "AO VIVO" do modo.py: o clique já foi dado uma vez sem querer, e o
# CASCADE leva identidade, laudo, diagnóstico, proposta, notas e toques junto.
CONFIRMA_APAGAR = "APAGAR"

app = Flask(__name__)

# progresso do scrap ao vivo, em memória: sid -> {screenshot, businesses, done, msg}
SCRAP_PROGRESS = {}


# ══════════════════════════════════════════════════════════════════════════════
#  PÁGINA
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.after_request
def _sem_cache_estatico(resp):
    """O navegador não pode servir `app.js` velho."""
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    # O painel não pode ser posto dentro de outro site: num <iframe> invisível,
    # um site induziria cliques que caem na conta do Instagram aberta no Direct.
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    return resp


# Guarda de Host contra DNS rebinding (ver servidor_local.py): um site aberto no
# navegador do operador não fala com o painel nem passando por 127.0.0.1. Sem
# try de propósito: sem a guarda o painel não sobe (falha fechado).
import servidor_local as _servidor_local   # noqa: E402
_servidor_local.instalar_guarda_host(app)


@app.route("/api/bootstrap")
def bootstrap():
    ddb.init_dashboard()
    ativo = ddb.quadro_ativo()
    return jsonify({
        "kanban_cols": [{"key": c["chave"], "label": c["rotulo"], "cor": c["cor"],
                         "sistema": c["sistema"]} for c in ddb.kanban_cols()],
        "canais": ddb.CANAIS,
        "ferramentas": ddb.get_ferramentas(),
        "stats": ddb.stats(ativo),
        "quadros": ddb.quadros(),
        "quadro_ativo": ativo,
        "esteira_etapas": config.ESTEIRA_WHATSAPP,
    })


# ══════════════════════════════════════════════════════════════════════════════
#  LEADS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/leads")
def api_leads():
    # ?quadro=todos vê a carteira inteira (só leitura, para conferência);
    # o padrão é o quadro aberto na tela.
    q = request.args.get("quadro")
    if q == "todos":
        return jsonify(ddb.leads_full())
    try:
        qid = int(q) if q else ddb.quadro_ativo()
    except ValueError:
        qid = ddb.quadro_ativo()
    return jsonify(ddb.leads_full(qid))


@app.route("/api/leads/<int:lead_id>")
def api_lead(lead_id):
    d = ddb.lead_detalhe(lead_id)
    if not d:
        return jsonify({"erro": "não encontrado"}), 404
    # a campanha e o passo vão junto para o card mostrar (e deixar mudar) onde o
    # lead está na régua, sem uma segunda chamada só para isso
    import esteira_wa as ew
    st = ew.estado(lead_id)
    camp = ddb.obter_campanha(d.get("campanha_id")) if d.get("campanha_id") else None
    d["campanha"] = ({"id": camp["id"], "nome": camp["nome"], "ativa": camp["ativa"],
                      "mensagens": len(camp["mensagens"])} if camp else None)
    d["esteira"] = {
        "passo": st["passo"], "total": st["total"], "concluida": st["concluida"],
        "proxima": st["proxima"], "proxima_rotulo": st["proxima_rotulo"],
        "proxima_ordem": (ew.ordem_da_etapa(st["proxima"]) if st["proxima"]
                          else st["total"]),
        "enviadas": st["enviadas"],
        "vence_em": st["vence_em"].strftime(ew.FMT) if st["vence_em"] else None,
        "pronto": st["pronto"],
        "sem_texto": bool(st["proxima"]) and not ew.tem_texto(d, st["proxima"]),
    }
    return jsonify(d), 200


@app.route("/api/leads", methods=["POST"])
def api_lead_criar():
    dados = request.json or {}
    if not dados.get("nome"):
        return jsonify({"erro": "nome é obrigatório"}), 400
    lead_id = ddb.inserir_manual(dados)
    return jsonify({"ok": True, "id": lead_id})


@app.route("/api/leads/<int:lead_id>", methods=["PATCH"])
def api_lead_editar(lead_id):
    try:
        ddb.atualizar_lead(lead_id, request.json or {})
    except ValueError as e:
        # 400: o campo é válido, o valor é que não serve. A mensagem vai
        # inteira para a tela porque ela já diz o que está errado.
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/leads/<int:lead_id>", methods=["DELETE"])
def api_lead_excluir(lead_id):
    forcar = request.args.get("forcar") in ("1", "true", "sim")
    confirmacao = (request.args.get("confirmacao") or "").strip().upper()
    if forcar and confirmacao != CONFIRMA_APAGAR:
        return jsonify({"erro": f"exclusão definitiva exige a confirmação "
                                f"'{CONFIRMA_APAGAR}'"}), 400
    try:
        ddb.excluir_lead(lead_id, forcar=forcar)
    except ddb.LeadProtegido as e:
        # 409: o pedido é válido, o estado é que não permite. A mensagem vai
        # inteira para a tela porque ela diz o que fazer no lugar de excluir.
        # `pode_forcar` é o que deixa a tela oferecer a exclusão definitiva.
        return jsonify({"erro": str(e), "pode_forcar": e.forcavel,
                        "confirmacao": CONFIRMA_APAGAR}), 409
    return jsonify({"ok": True, "definitivo": forcar})


@app.route("/api/leads/<int:lead_id>/kanban", methods=["POST"])
def api_lead_kanban(lead_id):
    kanban = (request.json or {}).get("kanban")
    try:
        ddb.set_kanban(lead_id, kanban)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/kanban/reorder", methods=["POST"])
def api_kanban_reorder():
    """Grava a ordem manual dos cards de uma coluna (não usado na 'lista')."""
    d = request.json or {}
    kanban = d.get("kanban")
    ids = d.get("ids", [])
    try:
        ddb.reordenar(kanban, ids)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/leads/bulk", methods=["POST"])
def api_leads_bulk():
    """Ações em massa: mover ou excluir vários leads de uma vez."""
    d = request.json or {}
    ids = d.get("ids", [])
    acao = d.get("acao")
    if not ids:
        return jsonify({"erro": "nenhum lead selecionado"}), 400
    if acao == "excluir":
        # Exclusão definitiva exige a palavra digitada, verificada AQUI e não só na
        # tela: quem chama esta rota por script ou por engano não passa por diálogo
        # nenhum, e o CASCADE leva identidade, laudo, diagnóstico, proposta, notas e
        # toques junto.
        forcar = bool(d.get("forcar"))
        if forcar and (d.get("confirmacao") or "").strip().upper() != CONFIRMA_APAGAR:
            return jsonify({"erro": f"exclusão definitiva exige a confirmação "
                                    f"'{CONFIRMA_APAGAR}'"}), 400
        try:
            n = ddb.bulk_excluir(ids, forcar=forcar)
        except ddb.LeadProtegido as e:
            return jsonify({"erro": str(e), "pode_forcar": e.forcavel,
                            "confirmacao": CONFIRMA_APAGAR}), 409
        return jsonify({"ok": True, "n": n, "definitivo": forcar})
    elif acao == "mover":
        try:
            ddb.bulk_mover(ids, d.get("kanban"))
        except ValueError as e:
            return jsonify({"erro": str(e)}), 400
    else:
        return jsonify({"erro": "ação inválida"}), 400
    return jsonify({"ok": True, "n": len(ids)})


def _vagas_whatsapp():
    try:
        import limites
        return limites.vagas_hoje("whatsapp")
    except Exception:
        return None


@app.route("/api/esteira")
def api_esteira():
    """Em que passo está cada lead de 'Contato iniciado', e quando vence."""
    import esteira_wa as ew
    return jsonify({
        "etapas": config.ESTEIRA_WHATSAPP,
        "leads": ew.resumo_coluna(),
        "enviados_hoje": ew.enviados_hoje(),
        "vagas_hoje": _vagas_whatsapp(),
        "janela": list(config.ESTEIRA_JANELA),
        "dentro_da_janela": ew.dentro_da_janela(),
    })


@app.route("/api/esteira/rodar", methods=["POST"])
def api_esteira_rodar():
    """Dispara agora os toques vencidos."""
    import esteira_wa as ew
    d = request.json or {}
    return jsonify(ew.rodar(seco=bool(d.get("seco")), verbose=False))


@app.route("/api/kanban/entrar-contato/enviar", methods=["POST"])
def api_entrar_contato_enviar():
    """Dispara o PRIMEIRO contato para a coluna "Entrar em contato"."""
    import envio_fila
    import esteira_wa as ew
    import modo
    from senders import whatsapp_sender

    if envio_fila.estado()["rodando"]:
        return jsonify({"ok": False, "erro": "já existe uma rodada de envio em "
                                             "andamento"}), 409

    campanhas = ddb.campanhas_para_rodizio()
    if not campanhas:
        return jsonify({"ok": False,
                        "erro": "nenhuma campanha ativa com mensagem de abertura. "
                                "Ative uma na aba Templates."}), 400

    if not ew.dentro_da_janela():
        ini, fim = config.ESTEIRA_JANELA
        return jsonify({"ok": False,
                        "erro": f"fora da janela de envio ({ini}h-{fim}h)"}), 409

    # o bot é conferido ANTES de mexer em qualquer lead: sem isso a rodada
    # começaria, o primeiro envio falharia e a leva pararia na primeira volta
    est = whatsapp_sender.status()
    if not est.get("pronto"):
        return jsonify({"ok": False,
                        "erro": "nada foi enviado: o WhatsApp não está conectado "
                                "(conecte a conta na aba Contas, ou espere "
                                "terminar de sincronizar)", "bot": est}), 409

    d = request.json or {}
    ids = d.get("ids")
    # O quadro entra na conta ANTES de montar a fila. Sem isso o disparo pegaria
    # "Entrar em contato" de todos os quadros ao mesmo tempo — a carteira da
    # empresa B recebendo a mensagem da empresa A, e sem volta depois de enviada.
    quadro = d.get("quadro_id") or ddb.quadro_ativo()
    quadro = int(quadro)
    if ids:
        alvos = [ddb.lead_detalhe(i) for i in ids]
        alvos = [a for a in alvos if a and (a.get("kanban") == "entrar_contato")
                 and int(a.get("quadro_id") or 0) == quadro]
    else:
        alvos = [l for l in ddb.leads_full(quadro)
                 if (l.get("kanban") or "") == "entrar_contato"]
    if not alvos:
        return jsonify({"ok": False, "erro": "nenhum lead na coluna Entrar em contato"}), 400

    # ordem estável: a mesma leva sempre sai na mesma sequência
    alvos.sort(key=lambda x: x["id"])
    # sem teto geral: cabe o que somam as vagas de hoje das contas ativas, e o
    # limite de CADA conta ainda é conferido no envio de cada lead
    import limites
    cabem = limites.vagas_hoje("whatsapp")
    if cabem <= 0:
        return jsonify({"ok": False,
                        "erro": "todas as contas de WhatsApp atingiram o limite de hoje "
                                "(limites na aba Contas)"}), 409
    fila = [a["id"] for a in alvos[:cabem]]
    sobra = [{"lead_id": a["id"], "nome": a.get("nome", ""),
              "motivo": "sem vaga hoje nas contas de WhatsApp: fica para amanhã"}
             for a in alvos[cabem:]]

    r = envio_fila.disparar(fila)
    if not r.get("ok"):
        return jsonify({"ok": False, "erro": r.get("erro")}), 409
    return jsonify({
        "ok": True, "iniciado": True, "modo": modo.atual(),
        "total": len(alvos), "na_rodada": len(fila),
        "campanhas": [c["nome"] for c in campanhas],
        "parados": sobra,
        "intervalo_seg": [config.ENVIO_DELAY_MIN, config.ENVIO_DELAY_MAX],
    })


@app.route("/api/kanban/entrar-contato/progresso")
def api_entrar_contato_progresso():
    """Como vai a rodada: a tela sonda isto enquanto o trabalhador anda."""
    import envio_fila
    return jsonify(envio_fila.estado())


@app.route("/api/kanban/entrar-contato/parar", methods=["POST"])
def api_entrar_contato_parar():
    """Não começa o próximo. O que já foi entregue ao bot não volta atrás."""
    import envio_fila
    r = envio_fila.parar()
    return (jsonify(r), 409) if not r.get("ok") else jsonify(r)


@app.route("/api/leads/artificiais", methods=["DELETE"])
def api_excluir_artificiais():
    """Remove os leads de SEED/demo, mantendo os vindos de prospecção/manuais."""
    n = ddb.excluir_artificiais()
    return jsonify({"ok": True, "removidos": n})


@app.route("/api/ia/status")
def api_ia_status():
    import ia_hook
    return jsonify(ia_hook.status())


# ══════════════════════════════════════════════════════════════════════════════
#  WHATSAPP + HISTÓRICO LEGADO DE CONVERSAS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/conversa/responder", methods=["POST"])
def api_conversa_responder():
    """Registra a entrada da sessão WhatsApp e preserva o legado configurado."""
    import conversa_ia
    d = request.json or {}
    numero, mensagem = d.get("numero", ""), d.get("mensagem", "")
    if not numero:
        return jsonify({"responder": False, "motivo": "sem número"})
    try:
        # Caixa manual separa o mesmo cliente quando ele escreve para contas diferentes.
        texto_chat = str(d.get("texto_chat") or mensagem or "").strip()
        if texto_chat:
            import chat_service
            chat_service.record("whatsapp", d.get("conta") or "padrão", numero,
                                texto_chat, d.get("nome") or "", d.get("message_id"),
                                quando=d.get("timestamp"))
        return jsonify(conversa_ia.responder(numero, mensagem))
    except Exception as e:
        return jsonify({"responder": False, "motivo": f"erro: {e}"})


@app.route("/api/chat/conversations")
def api_chat_conversations():
    import chat_service
    conversations = chat_service.list_threads(
        request.args.get("channel", ""), request.args.get("account", ""),
        request.args.get("search", ""),
        so_nao_lidas=request.args.get("unread") == "1",
        manter_id=request.args.get("keep", type=int))
    connected = set()
    if any(c["channel"] == "whatsapp" for c in conversations):
        try:
            status = requests.get(f"{config.WHATSAPP_BOT_URL}/api/contas", timeout=2).json()
            connected = {str(c.get("conta") or "padrão") for c in status.get("contas", [])
                         if c.get("pronto")}
        except Exception:
            pass
    ig_conectadas = {}
    if any(c["channel"] == "instagram" for c in conversations):
        import instagram_api
        try:
            ig_conectadas = instagram_api.contas_ativas()
        except Exception:
            pass
    for conversation in conversations:
        if conversation["channel"] == "whatsapp":
            conversation["available"] = conversation["account"] in connected
            conversation["unavailable_reason"] = ("" if conversation["available"]
                                                  else "Conecte esta conta WhatsApp para enviar.")
            continue
        # Instagram: precisa da conta ligada à API oficial E da janela de 24h
        # aberta (a Meta recusa resposta depois disso).
        aberta, fecha = instagram_api.janela(conversation)
        ligada = conversation["account"] in ig_conectadas
        conversation["window_open"], conversation["window_closes_at"] = aberta, fecha
        conversation["api_connected"] = ligada
        conversation["available"] = ligada and aberta
        conversation["unavailable_reason"] = (
            "" if conversation["available"] else
            f"@{conversation['account']} não está conectada à API oficial do Instagram."
            if not ligada else
            "Passaram 24h desde a última mensagem do lead: responda pelo app do Instagram.")
    return jsonify({"ok": True, "conversations": conversations})


@app.route("/api/chat/inbound", methods=["POST"])
def api_chat_inbound():
    """Entrada interna comum para conectores locais, sem resposta automática."""
    import chat_service
    if not _servidor_local.desta_maquina(request.remote_addr):
        return jsonify({"ok": False, "erro": "A entrada do conector aceita somente conexões locais."}), 403
    body = request.get_json(silent=True) or {}
    channel = str(body.get("channel") or "").lower()
    account = str(body.get("account") or "").strip()
    peer_id = str(body.get("peer_id") or "").strip()
    text = str(body.get("text") or "")
    if channel not in {"whatsapp", "instagram"} or not account or not peer_id or not text.strip():
        return jsonify({"ok": False, "erro": "canal, conta, peer_id e texto são obrigatórios."}), 400
    # v023: o bot do WhatsApp também manda o que SAI da conta (`direction: out`)
    saiu = body.get("direction") == "out"
    thread_id = chat_service.record(channel, account, peer_id, text,
                                    body.get("display_name") or "",
                                    body.get("provider_id"),
                                    direction="out" if saiu else "in",
                                    delivery_state="sent" if saiu else "received",
                                    quando=body.get("timestamp"))
    if not thread_id:
        return jsonify({"ok": False, "erro": "Mensagem inválida."}), 400
    return jsonify({"ok": True, "thread_id": thread_id})


# ── Mídia do chat (v024): foto, áudio, vídeo e documento do WhatsApp ─────── O
# bot entrega o arquivo logo depois de gravar a mensagem (POST /api/chat/midia).
_MIDIA_BUSCA = threading.Semaphore(2)       # conversa longa pede dezenas de uma vez


@app.route("/api/chat/midia", methods=["POST"])
def api_chat_midia():
    """Entrada local: o bot do WhatsApp anexa o arquivo a uma mensagem gravada."""
    import base64
    import chat_service
    if not _servidor_local.desta_maquina(request.remote_addr):
        return jsonify({"ok": False, "erro": "A entrada do conector aceita somente conexões locais."}), 403
    # teto desta entrada (não do painel todo: o backup sobe ~260 MB)
    if (request.content_length or 0) > 40 * 1024 * 1024:
        return jsonify({"ok": False, "erro": "mídia grande demais"}), 413
    b = request.get_json(silent=True) or {}
    try:
        dados = base64.b64decode(str(b.get("data") or ""), validate=True)
    except Exception:
        return jsonify({"ok": False, "erro": "arquivo inválido"}), 400
    msg_id = chat_service.anexar_midia(str(b.get("channel") or "whatsapp"), str(b.get("account") or ""),
                                       b.get("provider_id"), b.get("mimetype"), b.get("filename"), dados)
    if not msg_id:
        return jsonify({"ok": False, "erro": "mensagem não encontrada ou arquivo grande demais"}), 404
    return jsonify({"ok": True, "message_id": msg_id})


def _buscar_midia_no_bot(info):
    """
    Pede ao bot do WhatsApp o arquivo de uma mensagem antiga e o guarda. Devolve True
    (guardou), False (não deu) ou "ocupado" (fila cheia agora).
    """
    import base64
    import chat_service
    if info.get("channel") != "whatsapp" or not chat_service.pode_buscar_midia(info):
        return False
    pedido = {"conta": info["account"] if info["account"] != "padrão" else ""}
    if info.get("provider_id"):
        pedido["message_id"] = info["provider_id"]
    else:
        try:
            quando = datetime.fromisoformat(info["created_at"]).timestamp()
        except Exception:
            return False
        # o tipo do rótulo e o que já está em outra linha decidem qual mídia é
        # esta (num álbum, várias têm o mesmo horário)
        pedido.update(numero=info["peer_id"], timestamp=int(quando), from_me=info["direction"] == "out",
                      tipo=chat_service.tipo_do_rotulo(info.get("text")) or "",
                      indice=chat_service.indice_no_instante(info),
                      excluir=chat_service.ids_de_midia_da_conversa(info["thread_id"]))
    # prazo na fila: uma conversa com dezenas de fotos não pode segurar as
    # conexões do navegador (o resto do painel parava atrás delas)
    if not _MIDIA_BUSCA.acquire(timeout=3):
        return "ocupado"
    try:
        r = requests.post(f"{config.WHATSAPP_BOT_URL}/api/midia", json=pedido, timeout=45)
        d = r.json()
    except Exception:
        r, d = None, {}
    finally:
        _MIDIA_BUSCA.release()
    if not d.get("ok") or not d.get("data"):
        motivo = ("sumiu" if d.get("sumiu") else "empate" if d.get("empate")
                  else "grande" if d.get("grande") else "temporaria")
        chat_service.marcar_falha_midia(info["id"], motivo)
        return False
    try:
        dados = base64.b64decode(d["data"])
    except Exception:
        return False
    chat_service.gravar_midia_da_mensagem(info["id"], d.get("mimetype"), d.get("filename"), dados,
                                          provider_id=d.get("message_id"))
    return True


@app.route("/api/chat/mensagens/<int:msg_id>/midia")
def api_chat_mensagem_midia(msg_id):
    """O arquivo de uma mensagem do chat. Só mostra no painel o que é seguro mostrar."""
    import chat_service
    # Fotos, áudios e documentos de leads: só neste computador. Pela rede,
    # qualquer aparelho percorreria os ids e baixaria o acervo inteiro (LGPD).
    if not _servidor_local.desta_maquina(request.remote_addr):
        return jsonify({"ok": False, "erro": "as mídias das conversas abrem só no computador "
                                             "do painel"}), 403
    info = chat_service.midia_da_mensagem(msg_id)
    if not info:
        return jsonify({"ok": False, "erro": "mensagem não encontrada"}), 404
    if not info.get("caminho"):
        achou = _buscar_midia_no_bot(info)
        if achou == "ocupado":
            return (jsonify({"ok": False, "erro": "buscando outras mídias; tente de novo"}),
                    503, {"Retry-After": "3"})
        if achou:
            info = chat_service.midia_da_mensagem(msg_id)
    if not info or not info.get("caminho"):
        return jsonify({"ok": False, "erro": "arquivo indisponível (o WhatsApp não o tem mais, "
                                             "ou a conta está desconectada)"}), 404
    mime = chat_service.mime_limpo(info.get("media_mime"))
    na_tela = mime in chat_service.MIME_NA_TELA
    resp = send_file(info["caminho"], mimetype=mime if na_tela else "application/octet-stream",
                     as_attachment=not na_tela, download_name=info.get("media_name") or None,
                     conditional=True, max_age=86400)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.route("/api/chat/conversations/<int:thread_id>/messages", methods=["GET", "POST"])
def api_chat_messages(thread_id):
    import chat_service
    conversation = chat_service.thread(thread_id)
    if not conversation:
        return jsonify({"ok": False, "erro": "Conversa não encontrada."}), 404
    if request.method == "GET":
        return jsonify({"ok": True, "messages": chat_service.history(thread_id)})

    body = request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    if not text or len(text) > 10000:
        return jsonify({"ok": False, "erro": "A mensagem deve ter de 1 a 10.000 caracteres."}), 400
    if conversation["channel"] == "instagram":
        return _chat_enviar_instagram(conversation, text)
    if not conversation["peer_id"].isdigit() or not 8 <= len(conversation["peer_id"]) <= 15:
        return jsonify({"ok": False, "erro": "O contato não tem um telefone WhatsApp válido."}), 400

    try:
        response = requests.post(
            f"{config.WHATSAPP_BOT_URL}/api/enviar",
            json={"numero": conversation["peer_id"], "mensagem": text,
                  "conta": None if conversation["account"] == "padrão" else conversation["account"]},
            timeout=30)
        result = response.json()
    except Exception as exc:
        return jsonify({"ok": False, "erro": f"Não foi possível alcançar o WhatsApp: {exc}"}), 502
    if not isinstance(result, dict):
        return jsonify({"ok": False, "erro": "Resposta inválida do serviço WhatsApp."}), 502
    if not response.ok or not result.get("ok"):
        return jsonify({"ok": False, "erro": result.get("erro", "Falha no envio WhatsApp.")}), 502

    # com o id da mensagem, o aviso `message_create` da mesma resposta cai neste
    # registro em vez de duplicar a linha
    chat_service.record("whatsapp", conversation["account"], conversation["peer_id"],
                        text, conversation["display_name"], result.get("message_id"),
                        direction="out", delivery_state="sent")
    return jsonify({"ok": True})


def _chat_enviar_instagram(conversation, text):
    """Resposta pela API oficial, pela conta que recebeu a conversa."""
    import chat_service
    import instagram_api
    aberta, _ = instagram_api.janela(conversation)
    if not aberta:
        return jsonify({"ok": False, "erro": "Passaram 24h desde a última mensagem do lead: "
                        "a Meta não aceita resposta pela API. Responda pelo app do Instagram."}), 409
    try:
        mid = instagram_api.enviar_texto(conversation["account"], conversation["peer_id"], text)
    except ValueError as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 400
    except instagram_api.ErroAPI as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 502
    # o eco do webhook traz o mesmo `mid` e cai neste registro
    chat_service.record("instagram", conversation["account"], conversation["peer_id"],
                        text, provider_id=mid, direction="out", delivery_state="sent")
    return jsonify({"ok": True})


# ── Instagram pela API oficial: contas, tokens e relé ─────────────────────────
# Colar/trocar/remover token só de dentro desta máquina: o painel escuta na rede
# local sem login, e token de API lê e envia DM pela conta por 60 dias.

def _so_local():
    return _servidor_local.desta_maquina(request.remote_addr)


def _ig_oficial_recusa():
    return jsonify({"ok": False, "erro": "Gerencie os tokens do Instagram no próprio computador "
                    "do painel (http://localhost:5020)."}), 403


@app.route("/api/instagram-oficial/status")
def api_ig_oficial_status():
    import instagram_api
    return jsonify({"ok": True, **instagram_api.status()})


@app.route("/api/instagram-oficial/contas", methods=["POST"])
def api_ig_oficial_conectar():
    import instagram_api
    if not _so_local():
        return _ig_oficial_recusa()
    token = (request.get_json(silent=True) or {}).get("token")
    try:
        conta = instagram_api.conectar(token)
    except ValueError as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 400
    except instagram_api.ErroAPI as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 502
    return jsonify({"ok": True, "conta": conta})


@app.route("/api/instagram-oficial/contas/<ig_id>", methods=["DELETE"])
def api_ig_oficial_remover(ig_id):
    import instagram_api
    if not _so_local():
        return _ig_oficial_recusa()
    if not ig_id.isdigit():
        return jsonify({"ok": False, "erro": "conta inválida"}), 400
    return jsonify({"ok": True, "removidas": instagram_api.remover(ig_id)})


@app.route("/api/instagram-oficial/contas/<ig_id>/<acao>", methods=["POST"])
def api_ig_oficial_acao(ig_id, acao):
    import instagram_api
    if not _so_local():
        return _ig_oficial_recusa()
    if not ig_id.isdigit() or acao not in {"renovar", "inscrever"}:
        return jsonify({"ok": False, "erro": "ação inválida"}), 400
    try:
        feito = (instagram_api.renovar(ig_id, forcar=True) if acao == "renovar"
                 else instagram_api.inscrever(ig_id))
    except instagram_api.ErroAPI as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 502
    conta = next((c for c in instagram_api.contas() if c["ig_id"] == ig_id), None)
    if not feito:
        erro = (conta or {}).get("ultimo_erro") or "a Meta não confirmou"
        return jsonify({"ok": False, "erro": erro, "conta": conta}), 502
    return jsonify({"ok": True, "conta": conta})


@app.route("/api/instagram-oficial/sincronizar", methods=["POST"])
def api_ig_oficial_sincronizar():
    import instagram_api
    try:
        r = instagram_api.puxar()
    except instagram_api.ErroAPI as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 502
    if r.get("configurado") is False:
        return jsonify({"ok": False, "erro": "Relé não configurado: preencha IG_API_APP_SECRET, "
                        "IG_RELE_URL e IG_RELE_TOKEN no .env."}), 409
    return jsonify(r)


@app.route("/api/conversa/lista")
def api_conversa_lista():
    import conversa_ia
    return jsonify(conversa_ia.conversas_ativas())


@app.route("/api/conversa/<numero>")
def api_conversa_historico(numero):
    import conversa_ia
    return jsonify({"numero": numero, "mensagens": conversa_ia.historico(numero, 100),
                    "estado": conversa_ia.estado(numero)})


@app.route("/api/conversa/<numero>/pausar", methods=["POST"])
def api_conversa_pausar(numero):
    import conversa_ia
    pausar = bool((request.json or {}).get("pausado", True))
    conversa_ia.set_estado(numero, pausado=1 if pausar else 0,
                           estagio="humano" if pausar else "conversando")
    return jsonify({"ok": True, "pausado": pausar})


@app.route("/api/whatsapp/status")
def api_whatsapp_status():
    """
    Espelha o status do bot de WhatsApp (porta 5021), com os números do teto diário
    junto.
    """
    try:
        # `conta` é repassada: o bot é multi-conta, e sem isso esta rota fala
        # sempre com a conta padrão — que hoje pode nem estar pareada.
        r = requests.get(f"{config.WHATSAPP_BOT_URL}/api/status", timeout=5,
                         params={"conta": request.args.get("conta", "")})
        dados = r.json()
    except Exception as e:
        dados = {"pronto": False, "erro": f"bot de WhatsApp offline ({e})"}
    return jsonify(dados)


@app.route("/api/whatsapp/qr")
def api_whatsapp_qr():
    """O QR de pareamento, quando o bot está esperando ser escaneado."""
    try:
        r = requests.get(f"{config.WHATSAPP_BOT_URL}/api/qr", timeout=5,
                         params={"conta": request.args.get("conta", "")})
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"qr": None, "erro": f"bot de WhatsApp offline ({e})"})


@app.route("/api/whatsapp/iniciar", methods=["POST"])
def api_whatsapp_iniciar():
    """
    Sobe o CLIENTE de WhatsApp. O servidor Node no ar não basta: sem esta chamada ele
    fica de pé sem conta conectada, e o envio falha em silêncio.
    """
    try:
        d = request.json if request.is_json else {}
        conta = (d or {}).get("conta", "")
        # Com conta, CADASTRA no bot (cria o cliente e gera o QR dela). Sem
        # conta, mantém o comportamento antigo: inicia a conta padrão.
        if conta:
            r = requests.post(f"{config.WHATSAPP_BOT_URL}/api/contas",
                              json={"conta": conta}, timeout=20)
        else:
            r = requests.post(f"{config.WHATSAPP_BOT_URL}/api/iniciar",
                              json={}, timeout=20)
        return jsonify(r.json() if r.content else {"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "erro": f"bot de WhatsApp offline ({e})"}), 502


@app.route("/api/whatsapp/resultado", methods=["POST"])
def api_whatsapp_resultado():
    """O bot avisa aqui o resultado REAL de cada alvo de um lote."""
    d = request.json or {}
    lead_id = d.get("lead_id")
    if not lead_id:
        return jsonify({"ok": False, "erro": "sem lead_id"}), 400
    etapa = d.get("etapa") or ""
    novo = "sent" if d.get("ok") else "failed"
    detalhe = (f"bot: {d.get('numero','')}" if d.get("ok")
               else f"bot falhou: {d.get('erro','sem motivo')}")
    # Aqui — e só aqui — uma mensagem de WhatsApp está de fato confirmada.
    try:
        import limites
        if d.get("ok"):
            # O bot manda `conta: null` quando usou a conta padrão dele; o
            # remetente só é preciso se o lote for de antes da reserva.
            remetente = d.get("conta") or ""
            if not remetente:
                import sys as _s
                _s.path.insert(0, str(ROOT / "senders"))
                import whatsapp_sender as _ws
                remetente = _ws._conta_padrao()
            if not limites.confirmar_fila("whatsapp", lead_id, remetente,
                                          destino=d.get("numero", "")):
                app.logger.warning(
                    f"[limites] envio para {d.get('numero')} não foi atribuído: "
                    f"remetente {remetente!r} não está em Contas")
        else:
            limites.devolver_fila("whatsapp", lead_id)
    except Exception as e:
        app.logger.warning(f"[limites] não deu para registrar o envio: {e}")

    with db.conn() as con:
        # o toque mais recente daquela etapa que ainda está esperando resposta
        row = con.execute(
            "SELECT id FROM touches WHERE lead_id=? AND canal='whatsapp' "
            "AND status='queued' AND (etapa=? OR ?='') "
            "ORDER BY id DESC LIMIT 1", (lead_id, etapa, etapa)).fetchone()
        if not row:
            # o teto já foi consumido acima, de propósito: a mensagem saiu de
            # verdade, e não achar o toque para promover é problema da
            # escrituração do funil, não motivo para a vaga não ser cobrada
            return jsonify({"ok": True, "nota": "nenhum toque em queued para promover"})
        con.execute("UPDATE touches SET status=?, detalhe=?, "
                    "sent_at=datetime('now','localtime') WHERE id=?",
                    (novo, detalhe, row["id"]))

    return jsonify({"ok": True, "touch": row["id"], "status": novo})


@app.route("/api/leads/<int:lead_id>/conteudo", methods=["POST"])
def api_lead_conteudo(lead_id):
    import conteudo
    tipo = (request.json or {}).get("tipo", "post")
    return jsonify(conteudo.gerar(lead_id, tipo))


@app.route("/api/leads/<int:lead_id>/esteira", methods=["POST"])
def api_lead_esteira(lead_id):
    import esteira
    d = request.json or {}
    return jsonify(esteira.rodar_esteira(lead_id, com_pagespeed=bool(d.get("pagespeed"))))


@app.route("/api/leads/bulk/esteira", methods=["POST"])
def api_bulk_esteira():
    import esteira
    d = request.json or {}
    ids = d.get("ids", [])
    if not ids:
        return jsonify({"erro": "nenhum lead selecionado"}), 400
    return jsonify(esteira.rodar_lote(ids, com_pagespeed=bool(d.get("pagespeed"))))


@app.route("/api/leads/<int:lead_id>/notes", methods=["POST"])
def api_lead_nota(lead_id):
    texto = (request.json or {}).get("texto", "").strip()
    if not texto:
        return jsonify({"erro": "texto vazio"}), 400
    ddb.add_nota(lead_id, texto)
    return jsonify({"ok": True})


# ── Anexos ──
@app.route("/api/leads/<int:lead_id>/attachments", methods=["POST"])
def api_lead_anexo(lead_id):
    if "file" not in request.files:
        return jsonify({"erro": "sem arquivo"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"erro": "arquivo sem nome"}), 400
    dest_dir = ATTACH_DIR / str(lead_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = Path(f.filename).name
    dest = dest_dir / safe
    f.save(str(dest))
    ddb.add_anexo(lead_id, safe, str(dest))
    return jsonify({"ok": True, "filename": safe})


@app.route("/api/attachments/<int:att_id>")
def api_anexo_download(att_id):
    a = ddb.anexo(att_id)
    if not a or not Path(a["path"]).exists():
        return jsonify({"erro": "anexo não encontrado"}), 404
    return send_file(a["path"], as_attachment=True, download_name=a["filename"])


@app.route("/api/attachments/<int:att_id>", methods=["DELETE"])
def api_anexo_excluir(att_id):
    """Remove o material anexado ao lead, e o arquivo em disco junto."""
    r = ddb.excluir_anexo(att_id)
    if not r.get("ok"):
        return jsonify(r), 404
    return jsonify(r)


@app.route("/api/esteira/lead/<int:lead_id>", methods=["POST"])
def api_esteira_lead(lead_id):
    """Manda o próximo toque de UM lead, fora do lote."""
    import esteira_wa as ew
    import modo
    d = request.json or {}
    seco = bool(d.get("seco"))
    ignorar = bool(d.get("ignorar_prazo"))

    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"ok": False, "erro": "lead não encontrado"}), 404
    lead = dict(lead)

    # Quem respondeu sai da régua, e aqui a trava precisa ser explícita: o lote
    # filtra pela coluna "Contato iniciado", mas este endpoint é unitário e aceita
    # qualquer lead_id, então sozinho ele fura a regra.
    if ew.respondeu(lead_id):
        return jsonify({"ok": False,
                        "erro": "este lead já respondeu: a esteira parou nele e "
                                "o atendimento passou a ser manual"}), 409

    st = ew.estado(lead_id)
    if st["concluida"]:
        return jsonify({"ok": False, "erro": "esteira concluída para este lead"}), 400

    etapa = st["proxima"]
    texto = ew.montar_mensagem(lead, etapa)
    if not texto:
        return jsonify({"ok": False,
                        "erro": f"etapa {etapa} sem template cadastrado"}), 400

    # A prévia já mostra a mensagem DESTE lead, se o WhatsApp estiver com a
    # personalização ligada — e o envio reaproveita a mesma, guardada. O que o
    # operador aprova na tela é o que sai.
    import personalizar
    perso = personalizar.personalizar(lead, "whatsapp", texto, etapa=etapa)
    texto = perso["texto"]

    vence = st["vence_em"].strftime(ew.FMT) if st["vence_em"] else None

    # A prévia é montada ANTES da checagem de prazo, de propósito: quem vai
    # decidir se adianta um toque precisa ver o texto que sairia. A primeira
    # versão recusava antes de montar, e a confirmação aparecia sem a mensagem.
    if seco:
        return jsonify({"ok": True, "seco": True, "etapa": etapa,
                        "rotulo": st["proxima_rotulo"], "previa": texto,
                        "pronto": st["pronto"], "vence_em": vence,
                        "pode_forcar": not st["pronto"], "modo": modo.atual(),
                        "personalizada": perso["personalizado"],
                        "motivo_ia": perso["motivo"]})

    if not st["pronto"] and not ignorar:
        return jsonify({"ok": False, "erro": "ainda não venceu",
                        "vence_em": vence, "proxima": etapa,
                        "pode_forcar": True}), 409

    # a janela e o teto valem também no envio unitário: são proteção do número,
    # não conveniência da tela
    if not ew.dentro_da_janela():
        ini, fim = config.ESTEIRA_JANELA
        return jsonify({"ok": False, "erro": f"fora da janela de envio ({ini}h-{fim}h)"}), 409

    from senders import whatsapp_sender
    r = whatsapp_sender.enviar_lote(
        [{"lead_id": lead_id, "mensagem": texto,
          "experimento": "esteira_wa", "variante": etapa}], etapa=etapa)
    if not r.get("ok"):
        motivos = "; ".join(b.get("motivo", "") for b in (r.get("bloqueados") or []))
        return jsonify({"ok": False, "etapa": etapa, "modo": modo.atual(),
                        "erro": motivos or r.get("erro", "não saiu")}), 409
    return jsonify({"ok": True, "etapa": etapa, "rotulo": st["proxima_rotulo"],
                    "modo": modo.atual()})


# ── Enriquecimento governo ──
@app.route("/api/leads/<int:lead_id>/enrich", methods=["POST"])
def api_lead_enrich(lead_id):
    cnpj = (request.json or {}).get("cnpj", "").strip()
    if cnpj:
        ddb.atualizar_lead(lead_id, {"cnpj": cnpj})
    res = gov_enrich.enriquecer_lead(lead_id)
    return jsonify(res)


# ── Apuração de contatos pelo site ──
@app.route("/api/leads/<int:lead_id>/apurar", methods=["POST"])
def api_lead_apurar(lead_id):
    site = (request.json or {}).get("website", "").strip()
    if site:
        ddb.atualizar_lead(lead_id, {"website": site})
        with db.conn() as con:
            con.execute("UPDATE leads SET tem_site=1 WHERE id=?", (lead_id,))
    return jsonify(web_enrich.apurar_lead(lead_id))


# ── Confirmar WhatsApp ──
@app.route("/api/leads/<int:lead_id>/whatsapp", methods=["POST"])
def api_lead_whatsapp(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"erro": "lead não encontrado"}), 404
    numero = (request.json or {}).get("numero") or lead.get("telefone", "")
    res = whatsapp_check.verificar_whatsapp(numero)
    with db.conn() as con:
        con.execute("UPDATE leads SET whatsapp=?, tem_whatsapp=? WHERE id=?",
                    (res.get("link") or "",
                     1 if res.get("tem") else (0 if res.get("tem") is False else None), lead_id))
    return jsonify(res)


# ── PageSpeed (nota + screenshot do site) ──
@app.route("/api/leads/<int:lead_id>/pagespeed", methods=["POST"])
def api_lead_pagespeed(lead_id):
    estrategia = (request.json or {}).get("strategy", "mobile")
    return jsonify(pagespeed.pagespeed_lead(lead_id, estrategia))


# ── Gerar material (diagnóstico + proposta + PDF) ──
@app.route("/api/leads/<int:lead_id>/material", methods=["POST"])
def api_lead_material(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"erro": "lead não encontrado"}), 404
    # score se ainda não tiver
    if lead.get("score") is None:
        import scorer
        score, bd = scorer.score_lead(lead)
        db.set_score(lead_id, score, bd)
        lead = db.get_lead(lead_id)
    diag_mod.gerar_diagnostico(lead)
    prop_mod.gerar_proposta(lead)
    # PDF via subprocess (evita asyncio/Playwright em thread do Flask no Windows)
    pdf_ok, pdf_path = _gerar_pdf_subprocess(lead_id)
    if pdf_ok and pdf_path and Path(pdf_path).exists():
        ddb.add_anexo(lead_id, Path(pdf_path).name, str(pdf_path))
    ddb.set_kanban_opcional(lead_id, "material")
    return jsonify({"ok": True, "pdf": pdf_ok, "arquivo": Path(pdf_path).name if pdf_path else None})


def _gerar_pdf_subprocess(lead_id):
    try:
        r = subprocess.run([sys.executable, "proposta_designs.py", "--final", str(lead_id)],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        lead = db.get_lead(lead_id)
        slug = documento_pdf._slug(lead["nome"])
        pdf = config.OUT_DIR / f"proposta_{slug}.pdf"
        return (r.returncode == 0 and pdf.exists()), str(pdf)
    except Exception as e:
        print("Erro PDF:", e)
        return False, None


# ══════════════════════════════════════════════════════════════════════════════
#  TEMPLATES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/templates")
def api_templates():
    return jsonify(ddb.listar_templates(request.args.get("canal")))


# ── Campanhas ─────────────────────────────────────────────────────────────────
# Campanha = abertura + reforços, a sequência inteira de um modelo.

# ── Colunas do Kanban ─────────────────────────────────────────────────────────

@app.route("/api/kanban/colunas")
def api_colunas():
    return jsonify(ddb.kanban_cols())


@app.route("/api/kanban/colunas/ordem", methods=["POST"])
def api_colunas_ordem():
    """Grava a nova ordem das colunas depois de arrastar."""
    chaves = (request.json or {}).get("chaves") or []
    try:
        n = ddb.reordenar_colunas(chaves)
    except ValueError as e:
        return jsonify({"ok": False, "erro": str(e)}), 400
    return jsonify({"ok": True, "movidas": n, "colunas": ddb.kanban_cols()})


# ══════════════════════════════════════════════════════════════════════════════
#  QUADROS  (um funil por empresa atendida)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/quadros")
def api_quadros():
    return jsonify({"quadros": ddb.quadros(), "ativo": ddb.quadro_ativo()})


@app.route("/api/quadros", methods=["POST"])
def api_quadro_criar():
    d = request.json or {}
    r = ddb.quadro_criar(d.get("nome", ""), d.get("cor", "#00ff88"))
    return jsonify(r), (200 if r.get("ok") else 400)


@app.route("/api/quadros/<int:qid>", methods=["PATCH", "PUT"])
def api_quadro_editar(qid):
    d = request.json or {}
    r = ddb.quadro_renomear(qid, d.get("nome"), d.get("cor"))
    return jsonify(r), (200 if r.get("ok") else 400)


@app.route("/api/quadros/<int:qid>", methods=["DELETE"])
def api_quadro_excluir(qid):
    destino = (request.get_json(silent=True) or {}).get("destino_id")
    r = ddb.quadro_excluir(qid, destino)
    return jsonify(r), (200 if r.get("ok") else 400)


@app.route("/api/quadros/ativo", methods=["POST"])
def api_quadro_ativo():
    qid = (request.json or {}).get("quadro_id")
    try:
        r = ddb.set_quadro_ativo(int(qid))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "erro": "quadro_id inválido"}), 400
    if r.get("ok"):
        r["stats"] = ddb.stats(r["quadro_id"])
    return jsonify(r), (200 if r.get("ok") else 400)


@app.route("/api/limites")
def api_limites():
    """Consumo de cada conta HOJE. Zera à meia-noite; ver `limites._corte`."""
    import limites
    canal = request.args.get("canal") or None
    return jsonify({"janela_horas": limites.JANELA_HORAS,
                    "zera_em": limites._proxima_meia_noite().strftime("%Y-%m-%d %H:%M:%S"),
                    "contas": limites.estado(canal)})


@app.route("/api/limites/sincronizar", methods=["POST"])
def api_limites_sincronizar():
    """Realinha o contador do mail bot com a janela (o daemon já faz sozinho)."""
    import limites
    return jsonify(limites.sincronizar_email())


@app.route("/api/leads/sem-quadro")
def api_leads_sem_quadro():
    """Leads coletados que ainda não têm destino (`quadro_id=0`)."""
    with db.conn() as con:
        total = con.execute("SELECT COUNT(*) FROM leads WHERE quadro_id=0").fetchone()[0]
        amostra = [r[0] for r in con.execute(
            "SELECT nome FROM leads WHERE quadro_id=0 ORDER BY id DESC LIMIT 6")]
    return jsonify({"total": total, "amostra": amostra})


@app.route("/api/leads/sem-quadro/atribuir", methods=["POST"])
def api_leads_sem_quadro_atribuir():
    """Manda todos os leads sem destino para um quadro."""
    d = request.json or {}
    try:
        qid = int(d.get("quadro_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "erro": "quadro_id inválido"}), 400
    if qid not in {q["id"] for q in ddb.quadros()}:
        return jsonify({"ok": False, "erro": "quadro não existe"}), 400
    with db.conn() as con:
        cur = con.execute("UPDATE leads SET quadro_id=?, "
                          "updated_at=datetime('now','localtime') WHERE quadro_id=0", (qid,))
        movidos = cur.rowcount
    return jsonify({"ok": True, "movidos": movidos, "quadro_id": qid})


@app.route("/api/leads/mover-quadro", methods=["POST"])
def api_leads_mover_quadro():
    d = request.json or {}
    try:
        n = ddb.mover_para_quadro(d.get("ids") or [], int(d.get("quadro_id")))
    except (TypeError, ValueError) as e:
        return jsonify({"ok": False, "erro": str(e)}), 400
    return jsonify({"ok": True, "movidos": n})


@app.route("/api/kanban/colunas", methods=["POST"])
def api_coluna_criar():
    d = request.json or {}
    try:
        col = ddb.criar_coluna(d.get("rotulo", ""), d.get("cor", "#64748b"))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "coluna": col, "colunas": ddb.kanban_cols()})


@app.route("/api/kanban/colunas/<chave>", methods=["PATCH", "PUT"])
def api_coluna_editar(chave):
    d = request.json or {}
    try:
        mudou = ddb.atualizar_coluna(chave, d)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    if not mudou:
        return jsonify({"erro": "coluna não encontrada ou nada a mudar"}), 404
    return jsonify({"ok": True, "colunas": ddb.kanban_cols()})


@app.route("/api/kanban/colunas/<chave>", methods=["DELETE"])
def api_coluna_excluir(chave):
    destino = (request.get_json(silent=True) or {}).get("destino")
    try:
        r = ddb.excluir_coluna(chave, destino)
    except ddb.ColunaProtegida as e:
        return jsonify({"erro": str(e)}), 409
    if not r.get("ok"):
        return jsonify(r), 404
    # `movidos`/`destino` seguem para a tela: é o que ela usa para dizer quantos
    # leads mudaram de lugar. Sem isso a exclusão parecia não ter mexido em nada.
    return jsonify({**r, "colunas": ddb.kanban_cols()})


@app.route("/api/leads/<int:lead_id>/campanha", methods=["PATCH"])
def api_lead_campanha(lead_id):
    """Troca a campanha do lead, tira ele da campanha, e/ou move o passo da régua."""
    import esteira_wa as ew
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"erro": "lead não encontrado"}), 404
    d = request.json or {}

    if "campanha_id" in d:
        cid = d.get("campanha_id")
        if cid in (None, "", "null"):
            ddb.definir_campanha_do_lead(lead_id, None)
        else:
            try:
                cid = int(cid)
            except Exception:
                return jsonify({"erro": "campanha inválida"}), 400
            if not ddb.obter_campanha(cid):
                return jsonify({"erro": "campanha não encontrada"}), 404
            ddb.definir_campanha_do_lead(lead_id, cid)

    if d.get("ordem") is not None:
        try:
            ew.definir_passo(lead_id, int(d["ordem"]))
        except ValueError as e:
            return jsonify({"erro": str(e)}), 409

    return api_lead(lead_id)


@app.route("/api/campanhas")
def api_campanhas():
    return jsonify({
        "campanhas": ddb.listar_campanhas("whatsapp"),
        "passos": config.ESTEIRA_WHATSAPP,
        "max_mensagens": ddb.MAX_MENSAGENS_CAMPANHA(),
        "em_rodizio": [c["nome"] for c in ddb.campanhas_para_rodizio()],
    })


@app.route("/api/campanhas", methods=["POST"])
def api_campanha_criar():
    d = request.json or {}
    try:
        cid = ddb.criar_campanha(d.get("nome", ""))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "id": cid, "campanha": ddb.obter_campanha(cid)})


@app.route("/api/campanhas/<int:cid>", methods=["PUT", "PATCH"])
def api_campanha_editar(cid):
    """Renomeia ou liga/desliga a campanha."""
    d = request.json or {}
    try:
        mudou = ddb.atualizar_campanha(cid, d)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    if not mudou:
        return jsonify({"erro": "campanha não encontrada ou nada a mudar"}), 404
    return jsonify({"ok": True, "campanha": ddb.obter_campanha(cid),
                    "em_rodizio": [c["nome"] for c in ddb.campanhas_para_rodizio()]})


@app.route("/api/campanhas/<int:cid>", methods=["DELETE"])
def api_campanha_excluir(cid):
    r = ddb.excluir_campanha(cid)
    return (jsonify(r), 409) if not r.get("ok") else jsonify(r)


@app.route("/api/campanhas/<int:cid>/mensagens", methods=["POST"])
def api_campanha_mensagem(cid):
    """Cria ou substitui a mensagem de uma POSIÇÃO da campanha."""
    d = request.json or {}
    camp = ddb.obter_campanha(cid)
    if not camp:
        return jsonify({"erro": "campanha não encontrada"}), 404
    try:
        ordem = int(d.get("ordem"))
    except Exception:
        return jsonify({"erro": "posição inválida"}), 400
    if not 0 <= ordem < ddb.MAX_MENSAGENS_CAMPANHA():
        return jsonify({"erro": f"a sequência vai de 1 abertura a "
                                f"{ddb.MAX_MENSAGENS_CAMPANHA() - 1} reforços "
                                f"(os dias vêm do config)"}), 400
    corpo = (d.get("corpo") or "").strip()
    if not corpo:
        return jsonify({"erro": "o corpo não pode ficar vazio"}), 400
    nome = (d.get("nome") or "").strip() or (
        "Abertura" if ordem == 0 else f"Reforço {ordem}")
    existente = ddb.template_da_campanha(cid, ordem)
    if existente:
        ddb.atualizar_template(existente["id"], {"nome": nome, "corpo": corpo,
                                                 "campanha_id": cid, "ordem": ordem})
        tid = existente["id"]
    else:
        tid = ddb.criar_template("whatsapp", nome, "", corpo,
                                 campanha_id=cid, ordem=ordem)
    return jsonify({"ok": True, "id": tid, "campanha": ddb.obter_campanha(cid)})


@app.route("/api/templates", methods=["POST"])
def api_template_criar():
    d = request.json or {}
    if d.get("canal") not in ddb.CANAIS or not d.get("corpo"):
        return jsonify({"erro": "canal/corpo inválidos"}), 400
    tid = ddb.criar_template(d["canal"], d.get("nome", "Sem nome"),
                             d.get("assunto", ""), d["corpo"])
    return jsonify({"ok": True, "id": tid})


@app.route("/api/templates/<int:tid>", methods=["PUT", "PATCH"])
def api_template_editar(tid):
    """Edita um template no lugar, sem trocar o id."""
    d = request.json or {}
    if "corpo" in d and not (d.get("corpo") or "").strip():
        return jsonify({"erro": "o corpo não pode ficar vazio"}), 400
    if not ddb.atualizar_template(tid, d):
        return jsonify({"erro": "template não encontrado ou nada a mudar"}), 404
    return jsonify({"ok": True, "template": ddb.obter_template(tid)})


@app.route("/api/templates/<int:tid>", methods=["DELETE"])
def api_template_excluir(tid):
    ddb.excluir_template(tid)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
#  SCRAP
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/scrap")
def api_scrap_listar():
    """A fila de buscas, com o que cada uma rendeu."""
    import scrap_destino
    itens = ddb.listar_scrap()
    for s in itens:
        try:
            s["leads"] = scrap_destino.resumo(s["id"])
        except Exception:
            s["leads"] = {"total": 0, "sem_quadro": 0, "por_quadro": {}, "amostra": []}
    return jsonify(itens)


@app.route("/api/scrap/<int:sid>/analisar", methods=["POST"])
def api_scrap_analisar(sid):
    """O que aconteceria ao mandar esta busca para um quadro. Não altera nada."""
    import scrap_destino
    try:
        qid = int((request.json or {}).get("quadro_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "erro": "quadro_id inválido"}), 400
    return jsonify({"ok": True, **scrap_destino.analisar(sid, qid)})


@app.route("/api/scrap/<int:sid>/enviar", methods=["POST"])
def api_scrap_enviar(sid):
    """Manda os leads desta busca para um quadro."""
    import scrap_destino
    d = request.json or {}

    qid = d.get("quadro_id")
    if d.get("novo_quadro"):
        r = ddb.quadro_criar(str(d["novo_quadro"]).strip())
        if not r.get("ok"):
            return jsonify(r), 400
        qid = r["id"]
    try:
        qid = int(qid)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "erro": "escolha um quadro de destino"}), 400

    r = scrap_destino.enviar(sid, qid, d.get("politica", "ignorar"))
    if r.get("ok"):
        r["quadros"] = ddb.quadros()
    return jsonify(r), (200 if r.get("ok") else 400)


@app.route("/api/scrap", methods=["POST"])
def api_scrap_add():
    """Enfileira uma busca."""
    import varredura
    d = request.json or {}
    if not d.get("termo"):
        return jsonify({"erro": "termo obrigatório"}), 400

    local = d.get("localizacao", "")
    modo = "estado" if d.get("modo") == "estado" else "busca"
    if modo == "estado" and not varredura.resolver_uf(local):
        return jsonify({"erro": f"para varrer um estado, a localização precisa ser "
                                f"o estado sozinho (ex.: 'Alagoas' ou 'AL'). "
                                f"'{local}' não é um estado."}), 400

    sid = ddb.add_scrap(d["termo"], local, modo=modo,
                        orcamento=int(d.get("orcamento") or 30))
    return jsonify({"ok": True, "id": sid, "modo": modo})


@app.route("/api/scrap/plano")
def api_scrap_plano():
    """Quanto custaria varrer este estado — antes de começar."""
    import varredura
    uf = varredura.resolver_uf(request.args.get("local", ""))
    if not uf:
        return jsonify({"ok": False, "estado": None})
    return jsonify(varredura.plano(uf, request.args.get("orcamento", 30, type=int)))


@app.route("/api/scrap/<int:sid>", methods=["DELETE"])
def api_scrap_excluir(sid):
    """Apaga uma busca da fila."""
    forcar = request.args.get("forcar") in ("1", "true", "sim")
    import scrap_destino
    r = scrap_destino.resumo(sid)
    if r["sem_quadro"] and not forcar:
        return jsonify({
            "ok": False, "precisa_decidir": True, "sem_quadro": r["sem_quadro"],
            "erro": f"esta busca tem {r['sem_quadro']} lead(s) sem quadro: "
                    f"mande-os para um quadro, descarte-os junto com a busca, "
                    f"ou apague so a busca (eles vao para o quadro padrao)."}), 409

    if r["sem_quadro"] and forcar:
        with db.conn() as con:
            con.execute("UPDATE leads SET quadro_id=? WHERE scrap_id=? AND quadro_id=0",
                        (ddb.quadro_padrao_id(), sid))
    ddb.excluir_scrap(sid)
    return jsonify({"ok": True, "realocados": r["sem_quadro"] if forcar else 0})


@app.route("/api/scrap/<int:sid>/leads", methods=["DELETE"])
def api_scrap_descartar(sid):
    """Joga fora os leads desta coleta que ainda não foram para quadro nenhum."""
    import scrap_destino
    r = scrap_destino.descartar(sid)
    if r.get("ok") and (request.args.get("apagar_busca") in ("1", "true", "sim")):
        # a linha da fila só some quando nada mais depende dela
        if not scrap_destino.resumo(sid)["total"]:
            ddb.excluir_scrap(sid)
            r["busca_apagada"] = True
    return jsonify(r)


# Sem sinal por este tempo, a coleta é dada como morta. A margem é larga de
# propósito: uma rolagem leva ~2 s, mas abrir uma área nova do mapa leva até 30,
# e declarar morta uma coleta viva faria duas rodarem sobre a mesma busca.
SILENCIO_ATE_MORRER = 120


def _coleta_viva(sid: int) -> bool:
    """Esta coleta deu sinal de vida há pouco?"""
    p = SCRAP_PROGRESS.get(sid)
    if not p or p.get("done"):
        return False
    return (time.time() - float(p.get("quando") or 0)) < SILENCIO_ATE_MORRER


@app.route("/api/scrap/<int:sid>/parar", methods=["POST"])
def api_scrap_parar(sid):
    """Pede que a coleta pare."""
    if not _coleta_viva(sid):
        # nada para parar: o status ficou de uma coleta que morreu sem avisar
        ddb.set_scrap_status(sid, "pausado")
        return jsonify({"ok": True, "status": "pausado",
                        "aviso": "a coleta já não estava respondendo; "
                                 "a busca foi marcada como pausada"})
    r = ddb.pedir_parada(sid)
    return jsonify(r), (200 if r.get("ok") else 409)


@app.route("/api/scrap/<int:sid>/run", methods=["POST"])
def api_scrap_run(sid):
    """
    Dispara a coleta AO VIVO (scrap_live.py) para o termo/localização. O subprocesso
    transmite screenshots + a lista em tempo real via /api/scrap/progress.
    """
    itens = {s["id"]: s for s in ddb.listar_scrap()}
    s = itens.get(sid)
    if not s:
        return jsonify({"erro": "item não encontrado"}), 404
    import varredura
    modo = (s.get("modo") or "busca")
    uf = varredura.resolver_uf(s.get("localizacao") or "") if modo == "estado" else None
    if modo == "estado" and not uf:
        return jsonify({"ok": False, "erro": "esta busca está marcada como varredura "
                                             "de estado, mas a localização não é um "
                                             "estado"}), 400
    # Na varredura a localização NÃO entra no texto: quem diz onde procurar é o
    # enquadramento do mapa, e repetir "Alagoas" na frase faria o Google
    # recentralizar toda consulta no mesmo ponto — que é exatamente o defeito que a
    # varredura existe para corrigir.
    if s.get("status") in ("rodando", "parando") and _coleta_viva(sid):
        return jsonify({"ok": False, "erro": "esta busca já está rodando"}), 409
    query = s["termo"].strip() if uf else f'{s["termo"]} {s["localizacao"]}'.strip()
    ddb.set_scrap_status(sid, "rodando")
    SCRAP_PROGRESS[sid] = {"screenshot": "", "businesses": [], "done": False,
                           "msg": "Iniciando...", "count": 0}
    # A leitura do nº de avaliações (abre a página de cada lugar, um por um) NÃO
    # roda por padrão: é a etapa lenta que aparecia no fim de toda coleta, e o
    # operador pediu enriquecimento manual.
    cmd = [sys.executable, "scrap_live.py", str(sid), query]
    if uf:
        cmd += ["--estado", uf, "--orcamento", str(s.get("orcamento") or 30)]
    # Continuar e o padrao quando ha ponto de retomada: quem clica no play numa
    # busca PAUSADA quer seguir, nao recomecar. Recomecar do zero e pedido
    # explicito (`recomecar`), porque desperdicaria as areas ja varridas.
    corpo = request.get_json(silent=True) or {}
    if ddb.ler_progresso(sid) and not corpo.get("recomecar"):
        cmd.append("--retomar")
    elif corpo.get("recomecar"):
        ddb.salvar_progresso(sid, None)
    # `get_json(silent=True)`: a rota também é chamada sem corpo, e `request.json`
    # levanta 400 nesse caso em vez de devolver vazio
    if not (request.get_json(silent=True) or {}).get("avaliacoes"):
        cmd.append("--sem-avaliacoes")
    try:
        # sem janela: a coleta ja mostra a tela do navegador dentro do painel
        subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if uf:
            previsao = varredura.plano(uf, s.get("orcamento") or 30)
            return jsonify({"ok": True, "sid": sid, "varredura": previsao,
                            "mensagem": f"Varredura de {previsao['estado']} iniciada: "
                                        f"até {previsao['orcamento']} áreas, cerca de "
                                        f"{previsao['minutos_estimados']} min."})
        return jsonify({"ok": True, "sid": sid, "mensagem": f"Coleta ao vivo iniciada: {query}"})
    except Exception as e:
        ddb.set_scrap_status(sid, "erro")
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/scrap/progress", methods=["POST"])
def api_scrap_progress():
    """Recebe o progresso do scrap_live (screenshot + lista)."""
    d = request.json or {}
    sid = d.get("sid")
    if sid is not None:
        SCRAP_PROGRESS[sid] = {
            "screenshot": d.get("screenshot", ""),
            "businesses": d.get("businesses", []),
            "count": d.get("count", 0),
            "done": bool(d.get("done")),
            "msg": d.get("msg", ""),
            # quando chegou este sinal. É o que distingue uma coleta viva de um
            # status 'rodando' que sobrou de uma coleta que morreu.
            "quando": time.time(),
        }
    return jsonify({"ok": True})


@app.route("/api/scrap/live")
def api_scrap_live():
    """Front consulta aqui (polling) o progresso da coleta ao vivo."""
    sid = request.args.get("sid", type=int)
    return jsonify(SCRAP_PROGRESS.get(sid, {"done": True, "businesses": [], "msg": "sem coleta ativa"}))


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG / FERRAMENTAS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/config/ferramentas", methods=["GET", "POST"])
def api_ferramentas():
    if request.method == "POST":
        ddb.set_config("ferramentas", json.dumps(request.json or {}))
    return jsonify(ddb.get_ferramentas())


# ══════════════════════════════════════════════════════════════════════════════
#  PROSPECÇÃO (preview / envio via bridge)
# ══════════════════════════════════════════════════════════════════════════════

def _mensagem_para(lead, canal, template):
    """Monta a mensagem final do lead para o canal."""
    import personalizar
    corpo = (template or {}).get("corpo", "") or ""
    etapa = (template or {}).get("etapa") or "avulso"
    msg = personalizar.renderizar(lead, corpo, limpar=(canal != "email"))
    perso = personalizar.personalizar(lead, canal, msg, etapa=etapa)
    extra = {"personalizada": perso["personalizado"], "motivo_ia": perso["motivo"]}
    if canal == "email":
        assunto = personalizar.renderizar(lead, (template or {}).get("assunto", "") or "",
                                          limpar=False)
        return {"assunto": assunto, "corpo": perso["texto"], **extra}
    return {"corpo": perso["texto"], **extra}


# ── Personalização das mensagens (personalizar.py + ia_roteador.py) ──────────

@app.route("/api/personalizar")
def api_personalizar():
    """O cartão de cada canal: liga/desliga, instrução, 9router e últimas decisões."""
    import ia_roteador
    import personalizar
    return jsonify({
        "canais": personalizar.ajustes(),
        "roteador": ia_roteador.estado(),
        "variaveis": [{"nome": n, "descricao": d} for n, d in personalizar.VARIAVEIS],
        "variaveis_perfil": [{"nome": n, "descricao": d}
                             for n, d in personalizar.VARIAVEIS_PERFIL],
        "ultimas": {c: personalizar.ultimas(c, 8) for c in personalizar.CANAIS},
    })


@app.route("/api/personalizar", methods=["POST"])
def api_personalizar_salvar():
    import ia_roteador
    import personalizar
    d = request.get_json(silent=True) or {}
    canal = d.get("canal")
    if canal not in personalizar.CANAIS:
        return jsonify({"ok": False, "erro": "canal inválido"}), 400
    ligada = d.get("ativa")
    personalizar.salvar(canal, None if ligada is None else bool(ligada), d.get("instrucao"))
    roteador = None
    if ligada and ia_roteador.configurado():
        # Ligou agora: se o 9router estiver fechado, abre já. Descobrir aqui é
        # melhor do que no primeiro envio, com o lote esperando.
        roteador = ia_roteador.garantir()
    return jsonify({"ok": True, "canais": personalizar.ajustes(),
                    "roteador": ia_roteador.estado(), "abertura": roteador})


def _lead_de_exemplo(canal: str):
    """Um lead qualquer com dados para mostrar a personalização funcionando."""
    # Sem exigir `cidade`: a coluna está vazia na base inteira (o Maps não a
    # trouxe) — foi exigindo-a que o botão "Testar" respondeu "lead não
    # encontrado". Do mais completo para o que houver.
    contato = {"whatsapp": "COALESCE(telefone,'') != ''",
               "instagram": "COALESCE(instagram,'') != ''",
               "email": "COALESCE(email,'') != ''"}.get(canal, "1=1")
    with db.conn() as con:
        for filtro in (f"avaliacao_num IS NOT NULL AND {contato}", contato, "1=1"):
            r = con.execute(f"SELECT * FROM leads WHERE {filtro} "
                            f"ORDER BY RANDOM() LIMIT 1").fetchone()
            if r:
                return dict(r)
    return None


@app.route("/api/personalizar/testar", methods=["POST"])
def api_personalizar_testar():
    """
    O que a IA faria com UM lead (ou um perfil do Instagram): sem enviar e sem guardar.
    Roda mesmo com o canal desligado — é para decidir se liga.
    """
    import personalizar
    d = request.get_json(silent=True) or {}
    canal = d.get("canal") or "whatsapp"
    corpo = d.get("corpo") or ""
    if canal not in personalizar.CANAIS:
        return jsonify({"ok": False, "erro": "canal inválido"}), 400
    if not corpo.strip():
        return jsonify({"ok": False, "erro": "escreva o texto base para testar"}), 400
    t0 = time.time()
    perfil = d.get("perfil") if canal == "instagram" else None
    if perfil and str(perfil.get("usuario") or "").strip():
        perfil = {k: str(perfil.get(k) or "")[:600]
                  for k in ("usuario", "nome", "nome_perfil", "bio")}
        perfil["nicho"] = str(d.get("nicho") or "")[:80]
        r = personalizar.texto_do_perfil(corpo, perfil, forcar=True, usar_cache=False)
        alvo = "@" + perfil["usuario"].lstrip("@")
    else:
        lead_id = d.get("lead_id")
        lead = None
        if str(lead_id or "").strip():
            try:
                lead = db.get_lead(int(lead_id))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "erro": "lead inválido"}), 400
        else:
            lead = _lead_de_exemplo(canal)
        if not lead:
            return jsonify({"ok": False, "erro": "lead não encontrado"}), 404
        lead = dict(lead)
        base = personalizar.renderizar(lead, corpo, limpar=(canal != "email"))
        r = personalizar.personalizar(lead, canal, base, etapa="teste",
                                      usar_cache=False, forcar=True)
        alvo = f"lead {lead['id']} · {lead.get('nome') or ''}"
    return jsonify({"ok": True, "alvo": alvo, "segundos": round(time.time() - t0, 1), **r})


@app.route("/api/personalizar/9router", methods=["POST"])
def api_personalizar_9router():
    """O botão "abrir o 9router" do cartão."""
    import ia_roteador
    g = ia_roteador.garantir()
    return jsonify({"ok": g.get("no_ar", False), "resultado": g,
                    "roteador": ia_roteador.estado()})


@app.route("/api/prospect", methods=["POST"])
def api_prospect():
    d = request.json or {}
    canal = d.get("canal")
    lead_ids = d.get("lead_ids", [])
    enviar = bool(d.get("enviar"))
    template_id = d.get("template_id")
    if canal not in ddb.CANAIS:
        return jsonify({"erro": "canal inválido"}), 400
    if not ddb.get_ferramentas().get(canal, False):
        return jsonify({"erro": f"canal {canal} está desativado nas ferramentas"}), 400

    tpls = {t["id"]: t for t in ddb.listar_templates(canal)}
    template = tpls.get(template_id) or (ddb.listar_templates(canal) or [None])[0]

    import identidade

    previews, fila = [], []
    for lid in lead_ids:
        lead = db.get_lead(lid)
        if not lead:
            continue
        msg = _mensagem_para(lead, canal, template)
        # O cruzamento roda aqui, por lead, se não houver laudo ou se o salvo
        # estiver vencido. Assim o preview já mostra o que vai sair de verdade,
        # sem passo manual antes.
        laudo = identidade.garantir(lid) or {}
        cand = (laudo.get("escolhido") or {}).get(canal) or {}
        previews.append({
            "lead_id": lid, "nome": lead["nome"], **msg,
            "identidade": {
                "cruzado": bool(laudo.get("ok")),
                "contato": cand.get("rotulo", ""),
                "confianca": cand.get("confianca"),
                "veredito": cand.get("veredito", ""),
                "liberado": bool((laudo.get("liberado") or {}).get(canal)),
            },
        })
        item = {"lead_id": lid, "mensagem": msg["corpo"]}
        if canal == "email":
            item = {"lead_id": lid, "assunto": msg.get("assunto", ""),
                    "corpo": msg["corpo"]}
        fila.append(item)

    resultado = {"ok": True, "previews": previews, "enviado": False}

    if enviar:
        # Os senders reaplicam a trava de identidade lead a lead: Instagram e
        # e-mail exigem veredito confirmado, WhatsApp aceita provável.
        from senders import email_sender, instagram_sender, whatsapp_sender
        if canal == "instagram":
            envio = instagram_sender.enviar_lote(fila)
        elif canal == "email":
            envio = email_sender.enviar_lote(fila)
        elif canal == "whatsapp":
            envio = whatsapp_sender.enviar_lote(fila)
        else:
            envio = {"ok": False, "erro": f"canal {canal} sem sender"}

        resultado["envio"] = envio
        resultado["enviado"] = bool(envio.get("ok"))
        # só avança no Kanban quem realmente saiu
        bloqueados = {b["lead_id"] for b in envio.get("bloqueados", [])}
        falhados = {f["lead_id"] for f in envio.get("falhas", [])}
        for lid in lead_ids:
            if lid in bloqueados or lid in falhados or not envio.get("ok"):
                continue
            try:
                ddb.set_kanban(lid, "contato_iniciado")
            except Exception:
                pass
    return jsonify(resultado)


@app.route("/api/identidade/<int:lead_id>")
def api_identidade(lead_id):
    """Laudo salvo do cruzamento. ?rodar=1 refaz o cruzamento (vai à web)."""
    import identidade
    if request.args.get("rodar"):
        return jsonify(identidade.resolver(lead_id))
    laudo = identidade.carregar(lead_id)
    return jsonify(laudo or {"ok": False, "erro": "lead ainda não cruzado"})


@app.route("/api/identidade/<int:lead_id>/cruzar", methods=["POST"])
def api_identidade_cruzar(lead_id):
    import identidade
    usar_busca = bool((request.json or {}).get("usar_busca", True))
    return jsonify(identidade.resolver(lead_id, usar_busca=usar_busca))


# ══════════════════════════════════════════════════════════════════════════════
#  IMPORT / EXPORT
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/export")
def api_export():
    fmt = request.args.get("fmt", "csv")
    leads = ddb.leads_full()
    if fmt == "json":
        return Response(json.dumps(leads, ensure_ascii=False, indent=2),
                        mimetype="application/json",
                        headers={"Content-Disposition": "attachment; filename=leads.json"})
    # CSV
    cols = ["id", "nome", "categoria", "nicho", "kanban", "score", "avaliacao_num",
            "total_avaliacoes_num", "posicao", "telefone", "email", "instagram",
            "website", "cnpj", "razao_social", "roi", "nivel", "endereco"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for l in leads:
        w.writerow(l)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=leads.csv"})


@app.route("/api/import", methods=["POST"])
def api_import():
    if "file" not in request.files:
        return jsonify({"erro": "sem arquivo"}), 400
    f = request.files["file"]
    conteudo = f.read().decode("utf-8-sig", errors="replace")
    nome = (f.filename or "").lower()
    registros = []
    try:
        if nome.endswith(".json"):
            registros = json.loads(conteudo)
            if isinstance(registros, dict):
                registros = [registros]
        else:
            registros = list(csv.DictReader(io.StringIO(conteudo)))
    except Exception as e:
        return jsonify({"erro": f"falha ao ler arquivo: {e}"}), 400

    n = 0
    for r in registros:
        if not (r.get("nome") or r.get("nome_fantasia")):
            continue
        ddb.inserir_manual({
            "nome": r.get("nome") or r.get("nome_fantasia"),
            "categoria": r.get("categoria", ""),
            "nicho": r.get("nicho", config.NICHO_PADRAO),
            "telefone": r.get("telefone", ""),
            "email": r.get("email", ""),
            "website": r.get("website", ""),
            "instagram": r.get("instagram", ""),
            "endereco": r.get("endereco", ""),
            "cnpj": r.get("cnpj", ""),
        })
        n += 1
    return jsonify({"ok": True, "importados": n})


@app.route("/api/stats")
def api_stats():
    return jsonify(ddb.stats())


# ── Rotas: Scheduler / Automação ───────────────────────────────────────────────

@app.route("/api/scheduler/status")
def api_scheduler_status():
    """Status do scheduler daemon."""
    try:
        import scheduler
        return jsonify(scheduler.status())
    except Exception as e:
        return jsonify({"rodando": False, "erro": str(e)})


@app.route("/api/scheduler/iniciar", methods=["POST"])
def api_scheduler_iniciar():
    """Inicia o scheduler em background."""
    import subprocess
    try:
        subprocess.Popen(
            [sys.executable, "scheduler.py"],
            cwd=str(ROOT),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return jsonify({"ok": True, "mensagem": "Scheduler iniciado"})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/follow-up/rules")
def api_follow_up_rules():
    """Lista regras de follow-up."""
    try:
        import follow_up
        return jsonify(follow_up.listar_regras())
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/follow-up/pendentes")
def api_follow_up_pendentes():
    """Conta follow-ups pendentes por canal."""
    try:
        import follow_up
        return jsonify(follow_up.contar_pendentes())
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/follow-up/resumo")
def api_follow_up_resumo():
    """Fila por status, ultimas falhas e regras quebradas."""
    try:
        import follow_up
        return jsonify(follow_up.resumo_fila())
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/follow-up/reprocessar", methods=["POST"])
def api_follow_up_reprocessar():
    """Devolve para a fila os follow-ups marcados como falha."""
    try:
        import follow_up
        canal = (request.json or {}).get("canal")
        n = follow_up.reprocessar_falhas(canal)
        return jsonify({"ok": True, "reprocessados": n})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/backup/listar")
def api_backup_listar():
    """Lista backups existentes."""
    try:
        import backup_manager
        return jsonify(backup_manager.listar_backups())
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/backup/previsao")
def api_backup_previsao():
    """O que entra no pacote e quanto pesa, antes de gerar."""
    import backup_manager
    sess = request.args.get("sessoes") in ("1", "true", "sim")
    logs = request.args.get("logs", "1") in ("1", "true", "sim")
    return jsonify(backup_manager.previsao(incluir_pesados=sess, incluir_logs=logs))


@app.route("/api/backup/exportar", methods=["POST"])
def api_backup_exportar():
    """Gera o pacote de dados para levar a outra instalação."""
    d = request.get_json(silent=True) or {}
    try:
        import backup_manager
        return jsonify(backup_manager.exportar(
            incluir_sessoes=bool(d.get("sessoes", True)),
            incluir_logs=bool(d.get("logs", True))))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/api/backup/exports")
def api_backup_exports():
    import backup_manager
    return jsonify({"exports": backup_manager.listar_exports()})


@app.route("/api/backup/baixar")
def api_backup_baixar():
    """Entrega o zip pelo navegador."""
    import backup_manager
    alvo = Path(request.args.get("arquivo", "")).resolve()
    permitidos = (backup_manager.EXPORT_DIR.resolve(), backup_manager.BACKUP_DIR.resolve())
    if not any(str(alvo).startswith(str(p)) for p in permitidos) or not alvo.is_file():
        return jsonify({"erro": "arquivo fora das pastas de backup"}), 403
    return send_file(str(alvo), as_attachment=True, download_name=alvo.name)


@app.route("/api/backup/inspecionar", methods=["POST"])
def api_backup_inspecionar():
    """Lê o manifesto de um pacote (enviado ou já no disco) sem restaurar nada."""
    import backup_manager
    arq = request.files.get("arquivo")
    if arq:
        tmp = backup_manager.EXPORT_DIR / f"_recebido_{arq.filename}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        arq.save(str(tmp))
        r = backup_manager.inspecionar(str(tmp))
        r["arquivo"] = str(tmp)
        return jsonify(r)
    caminho = (request.get_json(silent=True) or {}).get("arquivo", "")
    r = backup_manager.inspecionar(caminho)
    r["arquivo"] = caminho
    return jsonify(r)


@app.route("/api/backup/importar", methods=["POST"])
def api_backup_importar():
    """Restaura um pacote nesta instalação."""
    d = request.get_json(silent=True) or {}
    try:
        import backup_manager
        r = backup_manager.importar(
            d.get("arquivo", ""),
            restaurar_sessoes=bool(d.get("sessoes", True)),
            restaurar_logs=bool(d.get("logs", False)),
            forcar=bool(d.get("forcar", False)))
        return jsonify(r), (200 if r.get("ok") else 409)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/api/backup/criar", methods=["POST"])
def api_backup_criar():
    """Cria um backup manual."""
    try:
        import backup_manager
        resultado = backup_manager.run_backup()
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/enrichment/status")
def api_enrichment_status():
    """Status do enrichment."""
    try:
        import enrichment
        return jsonify(enrichment.status_enrichment())
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/accounts")
def api_accounts():
    """Lista contas multi-canal."""
    try:
        import account_manager
        return jsonify(account_manager.listar_contas())
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/follow-up/rules", methods=["POST"])
def api_follow_up_rules_create():
    """Cria regra de follow-up."""
    try:
        import follow_up
        d = request.json or {}
        rule_id = follow_up.criar_regra(d["canal"], d["delay_days"], d.get("template_id"))
        return jsonify({"ok": True, "id": rule_id})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/accounts", methods=["POST"])
def api_accounts_create():
    """Cria conta multi-canal."""
    try:
        import account_manager
        import limites
        d = request.json or {}
        try:
            limite = limites.validar_limite(d.get("daily_limit", 30))
        except ValueError as e:
            return jsonify({"ok": False, "erro": str(e)}), 400
        acc_id = account_manager.criar_conta(
            d["channel"], d["identifier"], d.get("display_name"), limite
        )
        return jsonify({"ok": True, "id": acc_id})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/accounts/<int:acc_id>/auto-prospect", methods=["PATCH"])
def api_accounts_toggle_auto(acc_id):
    """Ativa/desativa auto-prospect."""
    try:
        d = request.json or {}
        with db.conn() as con:
            con.execute(
                "UPDATE accounts SET auto_prospect=? WHERE id=?",
                (d.get("auto_prospect", 0), acc_id)
            )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/accounts/<int:acc_id>", methods=["PATCH", "PUT"])
def api_accounts_editar(acc_id):
    """Ajusta limite diário, nome e situação de uma conta de WhatsApp/Instagram."""
    try:
        d = request.json if request.is_json else {}
        campos, valores = [], []
        import limites
        if (d or {}).get("daily_limit") is not None:
            try:
                limite = limites.validar_limite(d["daily_limit"])
            except ValueError as e:
                return jsonify({"ok": False, "erro": str(e)})
            campos.append("daily_limit=?"); valores.append(limite)
        if (d or {}).get("display_name") is not None:
            campos.append("display_name=?"); valores.append(str(d["display_name"]).strip())
        if (d or {}).get("status") is not None:
            situacao = str(d["status"]).strip()
            if situacao not in limites.SITUACOES_CONTA:
                return jsonify({"ok": False, "erro": "situação inválida: use "
                                + ", ".join(limites.SITUACOES_CONTA)})
            campos.append("status=?"); valores.append(situacao)
        if not campos:
            return jsonify({"ok": False, "erro": "nada para alterar"})
        with db.conn() as con:
            cur = con.execute("UPDATE accounts SET " + ", ".join(campos) + " WHERE id=?",
                              valores + [acc_id])
            if not cur.rowcount:
                return jsonify({"ok": False, "erro": "conta não encontrada"})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/accounts/<int:acc_id>", methods=["DELETE"])
def api_accounts_delete(acc_id):
    """Exclui conta."""
    try:
        with db.conn() as con:
            con.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/funnel")
def api_funnel():
    """Funil de prospecção (contagem por status do Kanban)."""
    try:
        with db.conn() as con:
            rows = con.execute(
                "SELECT kanban as status, COUNT(*) as total FROM leads "
                "GROUP BY kanban ORDER BY total DESC"
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/weekly-report")
def api_weekly_report():
    """Gera relatório semanal."""
    try:
        import weekly_report
        return jsonify(weekly_report.gerar_relatorio())
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/enrichment/executar", methods=["POST"])
def api_enrichment_executar():
    """Executa enrichment nos leads."""
    try:
        import enrichment
        resultado = enrichment.enrich_leads(limite=100)
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/leads")
def api_leads_list():
    """Lista leads com opção de ordenação e limite."""
    try:
        limit = request.args.get("limit", 50, type=int)
        order = request.args.get("order", "score")
        with db.conn() as con:
            if order == "score":
                rows = con.execute(
                    "SELECT * FROM leads WHERE score IS NOT NULL ORDER BY score DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/follow-up/enviar", methods=["POST"])
def api_follow_up_enviar():
    """Processa o próximo follow-up vencido e devolve o que aconteceu."""
    try:
        import follow_up
        r = follow_up.processar_proximo()
        r["ok"] = r.get("acao") == "enviado"
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "acao": "erro", "erro": str(e)})


@app.route("/api/scheduler/parar", methods=["POST"])
def api_scheduler_parar():
    """Para o scheduler."""
    try:
        import scheduler
        if scheduler._is_running():
            pid_file = scheduler.PID_FILE
            import os
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 9)
            pid_file.unlink(missing_ok=True)
            return jsonify({"ok": True, "mensagem": "Scheduler parado"})
        return jsonify({"ok": False, "mensagem": "Scheduler não está rodando"})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/config/modo")
def api_config_modo_ler():
    """O modo de envio ATUAL e os destinos liberados no modo teste."""
    try:
        import modo
        return jsonify({"modo": modo.atual(), "ao_vivo": modo.ao_vivo(),
                        "destinos_teste": modo.destinos_teste()})
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/sistema")
def api_sistema():
    """Diagnóstico: onde estão os bancos, quais portas, o que está no ar."""
    import os
    from pathlib import Path as _P

    def _tam(p):
        try:
            return round(_P(p).stat().st_size / 1048576, 1)
        except Exception:
            return None

    saida = {"portas": {"dashboard": 5020, "whatsapp": 5021, "instagram": 5022},
             "bancos": [], "sessoes": []}
    saida["bancos"].append({"nome": "leadforge.db (leads, toques, contas)",
                            "caminho": str(config.LEADFORGE_DB),
                            "mb": _tam(config.LEADFORGE_DB),
                            "existe": config.LEADFORGE_DB.exists()})
    saida["bancos"].append({"nome": "pcz.db (e-mail: listas, campanhas, envios)",
                            "caminho": str(config.MAIL_BOT_DB),
                            "mb": _tam(config.MAIL_BOT_DB),
                            "existe": config.MAIL_BOT_DB.exists()})
    local = _P(os.environ.get("LOCALAPPDATA") or _P.home())
    for nome, caminho in [("Sessão do WhatsApp", local / "clinica_prospector" / "wa-sessao"),
                          ("Sessões do Instagram", config.IG_SESSOES_DIR),
                          ("Nichos do bot de DM", config.BASE_DIR / "senders" / "dm_bot" / "perfis")]:
        try:
            itens = sorted(x.name for x in _P(caminho).iterdir()) if _P(caminho).exists() else []
        except Exception:
            itens = []
        saida["sessoes"].append({"nome": nome, "caminho": str(caminho),
                                 "existe": _P(caminho).exists(), "itens": itens})
    # o banco dentro do OneDrive é fonte conhecida de "database is locked"
    saida["aviso_onedrive"] = "OneDrive" in str(config.DATA_DIR)
    return jsonify(saida)


@app.route("/api/config/modo", methods=["POST"])
def api_config_modo():
    """Altera modo de envio (teste/ao_vivo)."""
    try:
        import modo
        d = request.json or {}
        m = d.get("modo", "teste")
        modo.definir(m)
        return jsonify({"ok": True, "modo": m})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/campanhas")
def api_campanhas_list():
    """Lista campanhas."""
    try:
        with db.conn() as con:
            rows = con.execute(
                "SELECT id, nome, ativa FROM campanhas ORDER BY id"
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"erro": str(e)})

# ── INSTAGRAM CHANNEL ───────────────────────────────────────────────────────────
@app.route("/api/ig/nichos")
def api_ig_nichos():
    try:
        import channel_backends as cb
        return jsonify(cb.ig_listar_nichos())
    except Exception as e:
        return jsonify({"erro": str(e)})

@app.route("/api/ig/nicho/<nome>/dados")
def api_ig_nicho_dados(nome):
    try:
        import channel_backends as cb
        return jsonify(cb.ig_carregar_nicho_dados(nome))
    except Exception as e:
        return jsonify({"erro": str(e)})

@app.route("/api/ig/nicho/<nome>/salvar", methods=["POST"])
def api_ig_nicho_salvar(nome):
    try:
        import channel_backends as cb
        dados = request.json or {}
        return jsonify(cb.ig_salvar_nicho_dados(nome, dados))
    except Exception as e:
        return jsonify({"erro": str(e)})

@app.route("/api/ig/estado")
def api_ig_estado():
    """Estado e log ao vivo do bot de DM — é o que o painel mostra enquanto roda."""
    try:
        r = requests.get(f"{config.DM_BOT_URL}/api/estado", timeout=5)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"rodando": False, "log": [],
                        "erro": f"bot de DM inacessível ({e})"})


@app.route("/api/ig/parar", methods=["POST"])
def api_ig_parar():
    """Aborta a operação em andamento no bot de DM."""
    recusa = _so_desta_maquina("O disparo do Instagram")
    if recusa:
        return recusa
    try:
        r = requests.post(f"{config.DM_BOT_URL}/api/parar", json={}, timeout=10)
        d = r.json() if r.content else {}
        return jsonify({"ok": d.get("status") in ("sucesso", "ok"),
                        "mensagem": d.get("mensagem")})
    except Exception as e:
        return jsonify({"ok": False, "erro": f"bot de DM inacessível ({e})"})


@app.route("/api/ig/nichos/importaveis")
def api_ig_nichos_importaveis():
    """Nichos do bot original que ainda não foram copiados para esta cópia."""
    try:
        import channel_backends as cb
        return jsonify({"nichos": cb.ig_nichos_importaveis(),
                        "origem": str(cb.ig_perfis_dir_origem() or "")})
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/ig/nichos/importar", methods=["POST"])
def api_ig_nicho_importar():
    """Copia um nicho do bot original para a pasta que o bot desta cópia usa."""
    try:
        import channel_backends as cb
        d = request.json if request.is_json else {}
        return jsonify(cb.ig_importar_nicho((d or {}).get("nicho", "")))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/ig/nicho/criar", methods=["POST"])
def api_ig_nicho_criar():
    try:
        import channel_backends as cb
        dados = request.json or {}
        return jsonify(cb.ig_criar_nicho(dados.get("nome", "")))
    except Exception as e:
        return jsonify({"erro": str(e)})

@app.route("/api/ig/status")
def api_ig_status():
    try:
        import channel_backends as cb
        return jsonify(cb.ig_bot_status())
    except Exception as e:
        return jsonify({"erro": str(e)})

@app.route("/api/ig/auth/<username>", methods=["POST"])
def api_ig_auth(username):
    try:
        import channel_backends as cb
        return jsonify(cb.ig_iniciar_autenticacao(username))
    except Exception as e:
        return jsonify({"erro": str(e)})

# ── EMAIL CHANNEL
# ────────────────────────────────────────────────────────────── ── AGENDAMENTO
# DE ENVIOS ───────────────────────────────────────────────────── O agendamento
# aperta os botões que já existem, por HTTP.

@app.route("/api/agendamentos")
def api_agendamentos():
    try:
        import agendamentos as ag
        canal = request.args.get("canal") or None
        return jsonify({"agendamentos": ag.listar(canal),
                        "proximo": ag.proximo(canal)})
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/agendamentos", methods=["POST"])
def api_agendamento_criar():
    try:
        import agendamentos as ag
        d = request.json if request.is_json else {}
        return jsonify(ag.criar(d.get("canal", ""), d.get("hora", ""),
                                d.get("dias", "0,1,2,3,4"), d.get("config") or {}))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/agendamentos/<int:aid>", methods=["PUT", "PATCH"])
def api_agendamento_editar(aid):
    try:
        import agendamentos as ag
        d = request.json if request.is_json else {}
        return jsonify(ag.editar(aid, **d))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/agendamentos/<int:aid>", methods=["DELETE"])
def api_agendamento_excluir(aid):
    try:
        import agendamentos as ag
        return jsonify(ag.excluir(aid))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/agendamentos/<int:aid>/rodar", methods=["POST"])
def api_agendamento_rodar(aid):
    """Dispara agora, sem esperar a hora. Serve para conferir se está de pé."""
    try:
        import agendamentos as ag
        alvo = next((a for a in ag.listar() if a["id"] == aid), None)
        if not alvo:
            return jsonify({"ok": False, "erro": "agendamento não encontrado"})
        return jsonify(ag.disparar(alvo))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/ig/prospectar", methods=["POST"])
def api_ig_prospectar():
    """Manda o bot de DM prospectar um nicho."""
    recusa = _so_desta_maquina("O disparo do Instagram")
    if recusa:
        return recusa
    try:
        d = request.json if request.is_json else {}
        nicho = (d or {}).get("nicho", "").strip()
        if not nicho:
            return jsonify({"ok": False, "erro": "informe o nicho"})
        mensagem = (d or {}).get("mensagem", "").strip()
        if not mensagem:
            return jsonify({"ok": False, "erro": "informe a mensagem a enviar"})
        # A conta define QUAL perfil de Chromium o bot abre.
        conta = str((d or {}).get("conta", "")).strip() or "todas"
        corpo = {"nicho": nicho, "mensagem_ia": mensagem,
                 "nome_modelo": "LeadForge", "conta": conta}
        r = requests.post(f"{config.DM_BOT_URL}/api/iniciar", json=corpo, timeout=20)
        dados = r.json() if r.content else {}
        ok = dados.get("status") == "sucesso"
        return jsonify({"ok": ok, "conta": conta,
                        "mensagem": dados.get("mensagem"), "resposta": dados})
    except Exception as e:
        return jsonify({"ok": False, "erro": f"bot de DM inacessível ({e})"})


@app.route("/api/visao-geral")
def api_visao_geral():
    """Os números da primeira tela: base por ferramenta, ritmo do dia, serviços."""
    try:
        import visao_geral as vg
        return jsonify(vg.resumo())
    except Exception as e:
        return jsonify({"erro": str(e)})


# ── E-MAIL MARKETING ──────────────────────────────────────────────────────────
# Tudo aqui fala com `email_marketing`, que opera no pcz.db — o banco do mail
# bot, onde estão as listas, os templates, as campanhas e as contas que de fato
# enviam.

def _email_json():
    return request.json if request.is_json else {}


@app.route("/api/email/stats")
def api_email_stats():
    try:
        import email_marketing as em
        return jsonify(em.stats())
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/email/listas")
def api_email_listas():
    try:
        import email_marketing as em
        return jsonify(em.listas())
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/email/listas", methods=["POST"])
@app.route("/api/email/lista/importar", methods=["POST"])
def api_email_lista_importar():
    try:
        import email_marketing as em
        d = _email_json()
        return jsonify(em.lista_importar(d.get("nome", ""), d.get("texto", "")))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/listas/<int:lid>", methods=["DELETE"])
def api_email_lista_excluir(lid):
    try:
        import email_marketing as em
        return jsonify(em.lista_excluir(lid))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/templates")
def api_email_templates():
    try:
        import email_marketing as em
        return jsonify(em.templates())
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/email/templates", methods=["POST"])
def api_email_template_criar():
    try:
        import email_marketing as em
        d = _email_json()
        return jsonify(em.template_criar(d.get("nome", ""), d.get("assunto", ""),
                                         d.get("corpo", "")))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/templates/<int:tid>", methods=["PUT", "PATCH"])
def api_email_template_editar(tid):
    try:
        import email_marketing as em
        d = _email_json()
        return jsonify(em.template_editar(tid, d.get("nome", ""), d.get("assunto", ""),
                                          d.get("corpo", "")))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/templates/<int:tid>", methods=["DELETE"])
def api_email_template_excluir(tid):
    try:
        import email_marketing as em
        return jsonify(em.template_excluir(tid))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/contas")
def api_email_contas():
    try:
        import email_marketing as em
        return jsonify(em.contas())
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/email/contas", methods=["POST"])
def api_email_conta_criar():
    try:
        import email_marketing as em
        d = _email_json()
        return jsonify(em.conta_criar(d.get("email", ""), d.get("nome", ""),
                                      d.get("limite", 70)))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/contas/<int:aid>", methods=["PUT", "PATCH"])
def api_email_conta_editar(aid):
    try:
        import email_marketing as em
        d = _email_json()
        return jsonify(em.conta_editar(aid, limite=d.get("limite"),
                                       ativo=d.get("ativo"), nome=d.get("nome")))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/contas/<int:aid>", methods=["DELETE"])
def api_email_conta_excluir(aid):
    try:
        import email_marketing as em
        return jsonify(em.conta_excluir(aid))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/contas/<int:aid>/validar", methods=["POST"])
def api_email_conta_validar(aid):
    """Bate na API do Gmail. Token no disco não prova que a conta ainda envia."""
    try:
        import email_marketing as em
        return jsonify(em.conta_validar(aid))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/contas/<int:aid>/autenticar", methods=["POST"])
def api_email_conta_autenticar(aid):
    try:
        import email_marketing as em
        return jsonify(em.conta_autenticar(aid))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/campanhas")
def api_email_campanhas():
    try:
        import email_marketing as em
        return jsonify(em.campanhas())
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/api/email/campanhas", methods=["POST"])
def api_email_campanha_criar():
    """
    Campanha = lista + template + contas remetentes. É o 'escolher a lista e a
    mensagem'.
    """
    try:
        import email_marketing as em
        d = _email_json()
        return jsonify(em.campanha_criar(
            d.get("nome", ""), d.get("list_id"), d.get("template_id"),
            d.get("contas"), d.get("templates_rodizio")))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/campanhas/<int:cid>", methods=["DELETE"])
def api_email_campanha_excluir(cid):
    try:
        import email_marketing as em
        return jsonify(em.campanha_excluir(cid))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/disparo/iniciar", methods=["POST"])
def api_email_disparo_iniciar():
    try:
        import email_marketing as em
        d = _email_json()
        return jsonify(em.disparo_iniciar(d.get("campaign_id"),
                                          d.get("min_delay", 30),
                                          d.get("max_delay", 90)))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/disparo/parar", methods=["POST"])
def api_email_disparo_parar():
    try:
        import email_marketing as em
        return jsonify(em.disparo_parar())
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})


@app.route("/api/email/disparo/status")
def api_email_disparo_status():
    try:
        import email_marketing as em
        return jsonify({**em.disparo_status(), "log": em.disparo_log(40)})
    except Exception as e:
        return jsonify({"erro": str(e)})


# ── CONNECTION MANAGER ENDPOINTS ──────────────────────────────────────────────
@app.route("/api/connect/whatsapp/status")
def api_connect_wa_status():
    try:
        import connection_manager as cm
        return jsonify(cm.wa_status_contas())
    except Exception as e:
        return jsonify({"erro": str(e)})

@app.route("/api/connect/whatsapp/abrir-perfil", methods=["POST"])
def api_connect_wa_abrir_perfil():
    try:
        import connection_manager as cm
        d = request.get_json(silent=True) or {}
        return jsonify(cm.wa_abrir_perfil((d or {}).get("conta", "")))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})

@app.route("/api/connect/whatsapp/conectar", methods=["POST"])
def api_connect_wa_conectar():
    """Põe UMA conta de WhatsApp para parear (gera o QR dela)."""
    try:
        import connection_manager as cm
        d = request.json if request.is_json else {}
        return jsonify(cm.wa_conectar((d or {}).get("conta", "")))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})

@app.route("/api/connect/whatsapp/qr")
def api_connect_wa_qr():
    try:
        import connection_manager as cm
        return jsonify({"qr": cm.wa_qr_code(request.args.get("conta", "")),
                        "conta": request.args.get("conta", "")})
    except Exception as e:
        return jsonify({"erro": str(e)})

@app.route("/api/connect/whatsapp/desconectar", methods=["POST"])
def api_connect_wa_desconectar():
    """Desconecta uma conta e esquece a sessão dela."""
    try:
        import connection_manager as cm
        d = request.json if request.is_json else {}
        return jsonify(cm.wa_desconectar((d or {}).get("conta", "")))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})

@app.route("/api/connect/instagram/status")
def api_connect_ig_status():
    try:
        import connection_manager as cm
        return jsonify(cm.ig_status_contas())
    except Exception as e:
        return jsonify({"erro": str(e)})

@app.route("/api/connect/instagram/abrir-perfil", methods=["POST"])
def api_connect_ig_abrir_perfil():
    recusa = _so_desta_maquina()        # a janela abre na tela DESTE computador
    if recusa:
        return recusa
    try:
        import connection_manager as cm
        d = request.get_json(silent=True) or {}
        r = cm.ig_abrir_perfil((d or {}).get("conta", ""))
        r.pop("http", None)
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})

def _estado_janelas_ig(painel=False):
    """
    Contas do Instagram + janelas manuais abertas, para Aquecimento e Direct. `painel`:
    a lista do Direct está na tela (libera a espiada das dormindo).
    """
    import connection_manager as cm
    estado = cm.ig_aquecimento_status(painel=painel)
    # "perfil aberto" = qualquer janela da conta, inclusive no painel: o
    # Relogar trocaria a sessão da pasta com o painel usando a cópia antiga
    abertas = estado.get("contas_abertas")
    if abertas is None:
        abertas = estado.get("perfis_abertos") or []
    contas = cm.ig_status_contas(None if estado.get("erro") else abertas)
    return {"contas": contas if isinstance(contas, list) else [],
            "active": estado.get("active", []),
            "online": not bool(estado.get("erro")),
            "prospeccao_rodando": bool(estado.get("prospeccao_rodando")),
            "contas_prospectando": estado.get("contas_prospectando") or [],
            "falhas": estado.get("falhas") or {},
            "erro": estado.get("erro")}


def _pedido_janela_ig(destino, embutida=False):
    """POST comum: abre ou fecha a janela manual de uma conta (só nesta máquina)."""
    recusa = _so_desta_maquina()
    if recusa:
        return recusa
    import connection_manager as cm
    body = request.get_json(silent=True) or {}
    ativo = body.get("ativo")
    if not isinstance(ativo, bool):
        return jsonify({"ok": False, "erro": "ativo precisa ser true ou false."}), 400
    filtro = body.get("so_nao_lidas")
    resultado = cm.ig_janela_manual(body.get("conta", ""), ativo, destino, embutida=embutida,
                                    so_nao_lidas=filtro if isinstance(filtro, bool) else None,
                                    fechar_apos_min=body.get("fechar_apos_min") if destino == "reels" else None)
    codigo = resultado.pop("http", None) or (202 if resultado.get("ok") else 409)
    return jsonify(resultado), codigo


@app.route("/api/aquecimento/instagram", methods=["GET", "POST"])
def api_aquecimento_instagram():
    try:
        if request.method == "GET":
            return jsonify(_estado_janelas_ig())
        return _pedido_janela_ig("reels")
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 502


@app.route("/api/aquecimento/instagram/todas", methods=["POST"])
def api_aquecimento_todas():
    """
    Ativa (ou desativa) o aquecimento de TODAS as contas com sessão de uma vez.
    `fechar_apos_min` opcional: cada janela fecha sozinha depois disso. Quem escolhe as
    janelas e espaça as aberturas é o bot; aqui vai a lista das contas com sessão.
    Também é o que o agendamento do aquecimento chama.
    """
    recusa = _so_desta_maquina("O aquecimento")
    if recusa:
        return recusa
    body = request.get_json(silent=True) or {}
    ativo = body.get("ativo")
    if not isinstance(ativo, bool):
        return jsonify({"ok": False, "erro": "ativo precisa ser true ou false."}), 400
    estado = _estado_janelas_ig()
    if not estado.get("online"):
        # `bot_fora`: o agendamento tenta de novo no minuto seguinte
        return jsonify({"ok": False, "bot_fora": True,
                        "erro": estado.get("erro") or "bot de Instagram fora do ar"}), 502
    corpo = {"ativo": ativo, "fechar_apos_min": body.get("fechar_apos_min"),
             "contas": [str(c.get("identifier") or "") for c in estado.get("contas") or []
                        if c.get("sessao_ativa")]}
    try:
        r = requests.post(f"{config.DM_BOT_URL}/api/aquecimento/todas", json=corpo, timeout=60)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"ok": False, "bot_fora": True, "erro": f"bot de Instagram inacessível ({e})"}), 502


@app.route("/api/chat/instagram-direct", methods=["GET", "POST"])
def api_chat_instagram_direct():
    """Direct do Instagram no Chat ao vivo: escolhe a conta, abre a caixa dela."""
    try:
        if request.method == "GET":
            d = _estado_janelas_ig(painel=True)
            campos = ("identifier", "display_name", "sessao_ativa", "perfil_aberto")
            d["contas"] = [{k: c.get(k) for k in campos} for c in d["contas"]]
            return jsonify(d)
        return _pedido_janela_ig("direct", embutida=True)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 502


# Uma conexão reaproveitada para o bot: o teclado manda muitos pedidos curtos.
# Uma por thread do Flask: requests.Session não é segura entre threads.
_SESSOES_BOT_DM = threading.local()


def _sessao_bot_dm():
    sess = getattr(_SESSOES_BOT_DM, "s", None)
    if sess is None:
        sess = _SESSOES_BOT_DM.s = requests.Session()
    return sess


def _so_desta_maquina(o_que="O Direct no painel"):
    """
    A tela e o teclado de uma conta logada do Instagram — e o disparo da prospecção —
    não podem ficar abertos para a rede: o painel escuta em 0.0.0.0 e não tem login.
    """
    if not _servidor_local.desta_maquina(request.remote_addr):
        return jsonify({"ok": False, "erro": f"{o_que} só funciona neste computador "
                                             "(http://localhost:5020)."}), 403
    return None


@app.route("/api/chat/instagram-direct/<conta>/tela")
def api_chat_instagram_direct_tela(conta):
    """Repassa a imagem ao vivo (MJPEG) da conta aberta no bot."""
    recusa = _so_desta_maquina()
    if recusa:
        return recusa
    from urllib.parse import quote
    conta = config.ig_conta_normalizada(conta)
    if not conta:
        return jsonify({"ok": False, "erro": "conta inválida"}), 400
    try:
        r = requests.get(f"{config.DM_BOT_URL}/api/janela-manual/{quote(conta)}/tela",
                         stream=True, timeout=(3, 60))
    except Exception as e:
        return jsonify({"ok": False, "erro": f"Bot de Instagram indisponível: {e}"}), 502
    if r.status_code != 200:
        r.close()
        return jsonify({"ok": False, "erro": "esta conta não está aberta no painel"}), r.status_code

    def repassar():
        try:
            for pedaco in r.iter_content(chunk_size=None):
                yield pedaco
        except Exception:
            pass
        finally:
            r.close()

    return Response(repassar(), mimetype=r.headers.get("Content-Type"),
                    headers={"Cache-Control": "no-store"})


@app.route("/api/chat/instagram-direct/<conta>/midia")
@app.route("/api/chat/instagram-direct/<conta>/midia/<mid>")
def api_chat_instagram_direct_midia(conta, mid=None):
    """
    Áudio/vídeo do Direct tocado pelo painel (a janela da conta não tem os codecs). Sem
    `mid`: o último play. Com `mid`: o arquivo (o id que o bot deu, nunca uma URL).
    """
    recusa = _so_desta_maquina()
    if recusa:
        return recusa
    from urllib.parse import quote
    conta = config.ig_conta_normalizada(conta)
    import re as _re
    if not conta or (mid is not None and not _re.fullmatch(r"[0-9a-f]{16}", str(mid))):
        return jsonify({"ok": False, "erro": "pedido inválido"}), 400
    caminho = f"/api/janela-manual/{quote(conta)}/midia" + (f"/{mid}" if mid else "")
    try:
        r = _sessao_bot_dm().get(f"{config.DM_BOT_URL}{caminho}", timeout=40, stream=mid is not None)
    except Exception as e:
        return jsonify({"ok": False, "erro": f"Bot de Instagram indisponível: {e}"}), 502
    if mid is None or r.status_code != 200:
        try:
            return jsonify(r.json()), r.status_code
        except Exception:
            return jsonify({"ok": False, "erro": f"bot respondeu HTTP {r.status_code}"}), 502
    tipo = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not (tipo.startswith("audio/") or tipo.startswith("video/")):
        tipo = "audio/mp4"

    def repassar():
        try:
            for pedaco in r.iter_content(chunk_size=65536):
                yield pedaco
        finally:
            r.close()
    return Response(repassar(), mimetype=tipo,
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.route("/api/chat/instagram-direct/<conta>/entrada", methods=["POST"])
def api_chat_instagram_direct_entrada(conta):
    """Cliques e teclas feitos sobre a imagem, na ordem, para a conta aberta."""
    recusa = _so_desta_maquina()
    if recusa:
        return recusa
    from urllib.parse import quote
    conta = config.ig_conta_normalizada(conta)
    if not conta:
        return jsonify({"ok": False, "erro": "conta inválida"}), 400
    try:
        r = _sessao_bot_dm().post(f"{config.DM_BOT_URL}/api/janela-manual/{quote(conta)}/entrada",
                          json={"eventos": (request.get_json(silent=True) or {}).get("eventos")},
                          timeout=5)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"ok": False, "erro": f"Bot de Instagram indisponível: {e}"}), 502

@app.route("/api/connect/instagram/bot")
def api_connect_ig_bot():
    try:
        import connection_manager as cm
        return jsonify(cm.ig_status_bot())
    except Exception as e:
        return jsonify({"erro": str(e)})

@app.route("/api/connect/instagram/auth/<username>", methods=["POST"])
def api_connect_ig_auth(username):
    """Abre a janela de login do Instagram para UMA conta."""
    try:
        import connection_manager as cm
        d = request.json if request.is_json else {}
        return jsonify(cm.ig_autenticar(username,
                                        trocar_conta=bool((d or {}).get("trocar_conta"))))
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)})

@app.route("/api/connect/email/status")
def api_connect_email_status():
    try:
        import connection_manager as cm
        return jsonify(cm.emailregar_status_completo())
    except Exception as e:
        return jsonify({"erro": str(e)})

@app.route("/api/connect/email/auth", methods=["POST"])
def api_connect_email_auth():
    try:
        import connection_manager as cm
        d = request.json or {}
        return jsonify(cm.email_autenticar(d.get("email", "")))
    except Exception as e:
        return jsonify({"erro": str(e)})


def _ligar_esteira():
    """Sobe o laço da esteira junto do dashboard."""
    try:
        ddb.init_dashboard()
        if (ddb.get_config("esteira_auto") or "0") != "1":
            print("[esteira] automática DESLIGADA (app_config.esteira_auto=1 liga)")
            return
        import esteira_wa as ew
        ew.iniciar_laco()
        print("[esteira] automática LIGADA")
    except Exception as e:
        print(f"[esteira] não deu para ligar: {e}")


def _ligar_daemon():
    """Sobe o daemon de automação junto com o painel."""
    try:
        import scheduler
        if scheduler._is_running():
            print("[daemon] já estava rodando")
            return

        # DETACHED_PROCESS, e não CREATE_NO_WINDOW: o painel já roda solto do console (é
        # assim que o subir.py o inicia), e um neto que apenas esconde a janela continua
        # preso ao pai — foi assim que a primeira versão do daemon morreu 14 segundos
        # depois de nascer.
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

        # stdout no vazio: o scheduler já tem um FileHandler próprio apontando para
        # `out/logs/scheduler.log`.
        subprocess.Popen(
            [sys.executable, "scheduler.py"], cwd=str(ROOT),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        print("[daemon] iniciado")
    except Exception as e:
        print(f"[daemon] não deu para iniciar: {e}")


def _ligar_instagram_oficial():
    """Sobe a puxada do relé de webhooks do Instagram (API oficial)."""
    try:
        import instagram_api
        instagram_api.iniciar_laco()
        faltam = [k for k, v in instagram_api.configurado().items() if not v]
        print("[instagram] API oficial: " + ("relé configurado" if not faltam
              else "aguardando no .env: " + ", ".join(faltam)))
    except Exception as e:
        print(f"[instagram] não deu para ligar a API oficial: {e}")


if __name__ == "__main__":
    _ligar_esteira()
    _ligar_daemon()
    _ligar_instagram_oficial()
    import os
    porta = int(os.environ.get("PORT", "5020"))
    ddb.init_dashboard()
    print("=" * 60)
    print("🖥️  Dashboard LeadForge")
    print(f"📡  http://localhost:{porta}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=porta, debug=False, threaded=True,
            request_handler=_servidor_local.RequestHandlerSemAtraso)
