"""
test_janela_manual.py — janela manual do Instagram (Reels, Direct, início) e o Direct
dentro do painel.
"""
import asyncio
import os
import re
import json
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(RAIZ, "senders", "dm_bot"))
sys.path.insert(0, RAIZ)

import Servidor_N8N as bot  # noqa: E402
import janela_remota  # noqa: E402
import logging  # noqa: E402

# O teste não escreve no log da operação (o painel mostra esse arquivo).
for _h in list(bot.logger.handlers) + list(logging.getLogger().handlers):
    if isinstance(_h, logging.FileHandler):
        _h.close()
        (bot.logger if _h in bot.logger.handlers else logging.getLogger()).removeHandler(_h)

falhas = []


def confere(cond, msg):
    print(("  ok  " if cond else "  FALHOU  ") + msg)
    if not cond:
        falhas.append(msg)


class _ThreadFalsa:
    """Guarda o alvo em vez de abrir o Chromium ou disparar o bot."""
    criadas = []

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self.target, self.args = target, args
        _ThreadFalsa.criadas.append(self)

    def start(self):
        pass


bot.threading.Thread = _ThreadFalsa


class _FuturoFalso:
    def add_done_callback(self, f):
        pass


def _agendar_falso(coro):
    """A janela do painel roda no navegador único: aqui não abre navegador."""
    _ThreadFalsa.criadas.append(coro)
    coro.close()
    return _FuturoFalso()


bot.navegador_unico.NAVEGADOR.agendar = _agendar_falso
bot.navegador_unico.AQUECIMENTO.agendar = _agendar_falso
# Perfil que existe no disco, sem depender de qual conta está logada aqui.
bot.perfil_atual = lambda conta=None: RAIZ
cliente = bot.app.test_client()


def limpar():
    bot.JANELAS.clear()          # PERFIS_ABERTOS é derivado dela
    bot.CONTAS_DO_BOT.clear()
    bot.BOT_RODANDO = False
    _ThreadFalsa.criadas.clear()


def abrir(conta, destino="direct", embutida=False):
    return cliente.post("/api/janela-manual", json={"conta": conta, "ativo": True,
                                                    "destino": destino, "embutida": embutida})


LOTE = {"nicho": "teste_leadforge", "alvos": [{"username": "perfil_que_nao_existe", "mensagem": "oi"}]}

print("[1] destino fora da lista")
limpar()
r = abrir("contateste", "https://evil.example/")
confere(r.status_code == 400, "URL no lugar do rótulo devolve 400")
confere(not _ThreadFalsa.criadas, "nenhuma janela foi aberta")

print("[2] trava por conta")
r = abrir("contateste")
confere(r.status_code == 202 and r.get_json()["ok"], "abre com 202")
confere("contateste" in bot.PERFIS_ABERTOS, "perfil reservado")
confere(bot.JANELAS["contateste"]["destino"] == "direct", "destino Direct registrado")
r = cliente.post("/api/enviar_lote", json=dict(LOTE, conta="contateste"))
confere(r.status_code == 409 and r.get_json().get("ocupada") is True,
        "lote pela conta aberta é recusado com `ocupada`")
confere(not bot.BOT_RODANDO, "e o bot não ficou marcado como rodando")
r = cliente.post("/api/enviar_lote", json=dict(LOTE, conta="outraconta"))
confere(r.status_code == 200, "lote por OUTRA conta segue (o follow-up não para)")
confere(bot.BOT_RODANDO and bot.CONTAS_DO_BOT == {"outraconta"},
        "reserva feita no mesmo passo da conferência")
bot.BOT_RODANDO = False
bot.CONTAS_DO_BOT.clear()
st = cliente.get("/api/janela-manual/status").get_json()
confere(st["active"][0]["destino"] == "direct", "status mostra o destino")

print("[3] a mesma conta de novo")
r = cliente.post("/api/aquecimento", json={"conta": "contateste", "ativo": True})
confere(r.status_code == 200, "reaproveita a janela (200)")
confere(bot.JANELAS["contateste"].get("ir_para") == "reels", "pede para ir ao Reels")
confere(sum(1 for t in _ThreadFalsa.criadas if t.target is bot._rodar_janela_manual) == 1,
        "não abriu segunda janela")

print("[4] prospecção rodando")
bot.BOT_RODANDO = True
bot.CONTAS_DO_BOT.update({"emuso"})
r = abrir("emuso")
confere(r.status_code == 409, "a conta que está prospectando não abre janela")
confere("emuso" not in bot.PERFIS_ABERTOS, "perfil não reservado")
r = abrir("livre")
confere(r.status_code == 202, "outra conta abre mesmo com a prospecção rodando")
bot.BOT_RODANDO = False
bot.CONTAS_DO_BOT.clear()

print("[5] fechar")
estado = bot.JANELAS["contateste"]
r = cliente.post("/api/janela-manual", json={"conta": "contateste", "ativo": False,
                                              "destino": "direct"})
confere(r.status_code == 202 and estado["stop"].is_set(), "sinal de parada enviado")


async def _navegador_fechou(conta, estado):
    return None
_janela_real = bot._janela_manual
bot._janela_manual = _navegador_fechou
bot._rodar_janela_manual("contateste", estado)
bot._janela_manual = _janela_real
confere("contateste" not in bot.PERFIS_ABERTOS, "perfil liberado")
confere("contateste" not in bot.JANELAS, "janela fora da lista")

print("[6] rotas antigas")
limpar()
r = cliente.post("/api/perfil/abrir", json={"conta": "contateste"})
confere(r.status_code == 202, "abrir perfil usa a janela manual")
confere(bot.JANELAS["contateste"]["destino"] == "inicio", "destino início")
r = cliente.post("/api/aquecimento", json={"conta": "contateste", "ativo": "sim"})
confere(r.status_code == 400, "ativo não booleano devolve 400")

print("[7] leitura da URL")
confere(bot._destino_da_url("https://www.instagram.com/direct/t/123/") == "direct", "/direct/t/ é Direct")
confere(bot._destino_da_url("https://www.instagram.com/reels/abc/") == "reels", "/reels/ é Reels")
confere(bot._destino_da_url("https://www.instagram.com/reel/abc/") == "reels", "/reel/ (um reel só) é Reels")
confere(bot._destino_da_url("https://www.instagram.com/") == "inicio", "raiz é início")
confere(bot._destino_da_url("https://www.instagram.com/fulano/") == "outro", "perfil é outro")
confere(bot._destino_da_url("https://evil.example/direct/") == "outro", "/direct/ de outro site é outro")
confere(not bot._host_permitido("https://l.facebook.com/x") and bot._host_permitido("https://l.instagram.com/?u=x"),
        "só instagram.com abre no painel")

print("[8] modos e pedidos numa janela aberta")
limpar()
r = abrir("contateste", embutida=True)
confere(r.status_code == 202 and bot.JANELAS["contateste"]["embutida"], "abre no painel")
confere(isinstance(bot.JANELAS["contateste"].get("tela"), janela_remota.TelaRemota), "com tela para transmitir")
r = cliente.post("/api/aquecimento", json={"conta": "contateste", "ativo": True})
confere(r.status_code == 409, "Reels externo recusado numa conta aberta no painel")
confere("ir_para" not in bot.JANELAS["contateste"], "e a janela não foi mexida")
limpar()
abrir("contateste")
r = cliente.post("/api/perfil/abrir", json={"conta": "contateste"})
confere(r.status_code == 200, "abrir perfil de janela aberta responde 200")
confere(bot.JANELAS["contateste"].get("ir_para") == "direct",
        "não leva ao início: fica no Direct e só vem para frente")
bot.JANELAS["contateste"]["stop"].set()
r = abrir("contateste")
confere(r.status_code == 409, "janela fechando recusa pedido novo")
r = cliente.post("/api/janela-manual", json={"conta": "x", "ativo": True, "destino": ["direct"]})
confere(r.status_code == 400, "destino que não é texto devolve 400 (não 500)")

print("[9] só nesta máquina e teto")
limpar()
abrir("contateste", embutida=True)
fora = {"REMOTE_ADDR": "192.168.3.50"}
r = cliente.get("/api/janela-manual/contateste/tela", environ_base=fora)
confere(r.status_code == 403, "tela recusada para outro computador da rede")
r = cliente.post("/api/janela-manual/contateste/entrada", json={"eventos": []}, environ_base=fora)
confere(r.status_code == 403, "teclado recusado para outro computador da rede")
for rota, corpo in (("/api/janela-manual", {"conta": "y", "ativo": True, "destino": "direct"}),
                    ("/api/aquecimento", {"conta": "y", "ativo": True}),
                    ("/api/perfil/abrir", {"conta": "y"}),
                    ("/api/iniciar", {"nicho": "x", "mensagem_ia": "oi"}),
                    ("/api/enviar_lote", LOTE),
                    ("/api/parar", {})):
    r = cliente.post(rota, json=corpo, environ_base=fora)
    confere(r.status_code == 403, f"{rota} recusado para outro computador da rede")
r = cliente.post("/api/janela-manual/contateste/entrada", json={"eventos": []})
confere(r.status_code == 409, "teclado antes de a janela subir devolve 409")
for i in range(9):
    abrir(f"extra{i}", embutida=True)
r = abrir("decima_primeira", embutida=True)
confere(r.status_code == 202 and len(bot.JANELAS) == 11, "11 contas abertas ao mesmo tempo (sem teto)")
limpar()

print("[10] tradução de cliques e teclas")


class _CdpFalso:
    def __init__(self):
        self.enviados = []

    async def send(self, metodo, params=None):
        self.enviados.append((metodo, params or {}))


async def _traduzir(eventos):
    tela = janela_remota.TelaRemota()
    tela._cdp = _CdpFalso()
    estado = {}
    for ev in eventos:
        if not await tela.aplicar(None, ev):
            await bot._evento_do_canal(None, estado, ev)
    return tela._cdp.enviados, estado


env, est = asyncio.run(_traduzir([
    {"t": "mouse", "a": "down", "x": 0.5, "y": 0.25, "b": "left", "n": 1},
    {"t": "mouse", "a": "up", "x": 0.5, "y": 0.25, "b": "left", "n": 1},
    {"t": "texto", "s": "Olá, tudo bem? 😊"},
    {"t": "tecla", "k": "Enter", "mod": 0},
    {"t": "tecla", "k": "Enter", "mod": 8},
    {"t": "tecla", "k": "F12", "mod": 0},
    {"t": "tecla", "k": "a", "mod": 2},
    {"t": "mouse", "a": "down", "x": 9, "y": -3},
    {"t": "filtro", "v": True},
]))
confere(env[0] == ("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 640.0, "y": 200.0,
        "button": "left", "buttons": 1, "clickCount": 1}), "clique no meio vira (640, 200) na página")
confere(env[2] == ("Input.insertText", {"text": "Olá, tudo bem? 😊"}), "texto com acento e emoji vai inteiro")
confere(env[3][1].get("text") == chr(13) and env[3][1]["type"] == "keyDown", "Enter envia (keyDown com retorno de carro)")
confere(env[5][1]["modifiers"] == 8, "Shift+Enter leva o Shift (quebra de linha)")
confere(not any(p.get("key") == "F12" for _, p in env), "tecla fora da lista é ignorada (F12)")
confere(any(p.get("commands") == ["selectAll"] for _, p in env), "Ctrl+A vira selecionar tudo")
confere(env[-1][1]["x"] == 1280.0 and env[-1][1]["y"] == 0.0, "coordenada fora da tela é limitada")
confere(est.get("so_nao_lidas") is True, "filtro de não lidas liga (evento do canal)")

print("[11] grupo e canal do WhatsApp")
import chat_service  # noqa: E402
confere(not chat_service.whatsapp_peer_valido("120363420222454900"), "canal/grupo (120363…, 18 dígitos) fora")
confere(not chat_service.whatsapp_peer_valido("5511999999999160000000"), "grupo no formato antigo fora")
confere(not chat_service.whatsapp_peer_valido("0"), "número curto fora")
confere(chat_service.whatsapp_peer_valido("558299990001"), "telefone fica")
confere(chat_service.whatsapp_peer_valido("123456789012345"), "LID de 15 dígitos fica (é pessoa)")
confere(chat_service.registrar("whatsapp", "conta", "120363420222454900", "oi") is None,
        "canal não é gravado")

print("[12] mesmas teclas no painel e no bot")
js = open(os.path.join(RAIZ, "dashboard", "static", "app.js"), encoding="utf-8").read()
m = re.search(r"const IGD_TECLAS=new Set\(\[(.*?)\]\);", js, re.S)
teclas_js = set(re.findall(r"'([^']+)'", m.group(1))) if m else set()
m = re.search(r"const IGD_ATALHOS=\[(.*?)\];", js, re.S)
atalhos_js = set(re.findall(r"'([^']+)'", m.group(1))) if m else set()
confere(teclas_js == set(janela_remota.TECLAS), f"teclas iguais ({len(teclas_js)})")
confere(atalhos_js == set(janela_remota.ATALHOS), "atalhos Ctrl iguais")

print("[13] follow-up com a conta aberta")
import follow_up  # noqa: E402
r = follow_up._resultado({"ok": False, "ocupada": True, "erro": "@x aberta"}, "DM")
confere(r.get("ocupada") is True and r.get("permanente") is False, "vira espera, não falha permanente")
confere(follow_up.ADIAR_OCUPADA_MIN > 0, "com prazo curto de nova tentativa")

print("[14] transmissão só com espectador")
tela = janela_remota.TelaRemota()
g = tela.quadros()
confere(tela.espectadores == 0, "ninguém olhando antes de abrir o fluxo")
primeiro = next(g)
confere(tela.espectadores == 1 and primeiro.startswith(b"--quadro"), "abrir o fluxo conta 1 espectador")
with tela.cond:
    tela.jpg, tela.seq = b"JPEG", 1
    tela.cond.notify_all()
q = next(g)
confere(q.startswith(b"JPEG") and q.endswith(janela_remota._BORDA), "quadro sai seguido da borda do próximo")
g.close()
confere(tela.espectadores == 0 and tela.sem_espectador_ha() >= 0, "fechar o fluxo zera os espectadores")
_keep = janela_remota.KEEPALIVE_S
janela_remota.KEEPALIVE_S = 0.05
g = janela_remota.TelaRemota().quadros()
next(g)
confere(next(g).startswith(janela_remota._JPEG_VAZIO),
        "sem quadro, o sinal de vida ainda escreve (conexão fechada é notada)")
g.close()
janela_remota.KEEPALIVE_S = _keep

print("[15] guarda de Host")
import servidor_local  # noqa: E402
for h, esperado in (("localhost:5020", True), ("127.0.0.1:5022", True), ("[::1]:5020", True),
                    ("192.168.3.12:5020", True), ("evil.example:5020", False), ("", False)):
    confere(servidor_local.host_permitido(h) is esperado, f"Host {h or '(vazio)'} → {'aceito' if esperado else 'recusado'}")
confere(not servidor_local.host_permitido("[::1]lixo:5020"), "Host [::1] com lixo depois → recusado")
r = cliente.get("/api/janela-manual/status", headers={"Host": "rebind.evil.example:5022"})
confere(r.status_code == 403, "bot recusa pedido com Host de outro site")
r = cliente.get("/api/janela-manual/status", headers={"Sec-Fetch-Site": "cross-site"})
confere(r.status_code == 403, "bot recusa /api disparado por outro site (img/form)")
r = cliente.get("/api/janela-manual/status", headers={"Origin": "https://evil.example"})
confere(r.status_code == 403, "bot recusa /api com Origin de outro site")
r = cliente.get("/api/janela-manual/status", headers={"Host": "localhost:5022", "Sec-Fetch-Site": "same-origin",
                                                       "Origin": "http://localhost:5022"})
confere(r.status_code == 200, "pedido do próprio painel passa")
r = cliente.get("/api/janela-manual/status", headers={"Host": "192.168.3.12:5022",
                                                       "Origin": "http://203.0.113.5"})
confere(r.status_code == 403, "Origin de outro IP (página de outro computador) é recusado")
r = cliente.get("/api/janela-manual/status", headers={"Origin": "null"})
confere(r.status_code == 403, "Origin null (iframe com sandbox) é recusado")
limpar()

print("[16] mídia do chat")
confere(chat_service.mime_limpo("audio/ogg; codecs=opus") == "audio/ogg", "codecs saem do tipo")
confere("image/svg+xml" not in chat_service.MIME_NA_TELA and "text/html" not in chat_service.MIME_NA_TELA,
        "SVG e HTML nunca aparecem na tela (só baixam)")
d = chat_service._descrever_midia({"id": 7, "text": "[áudio]", "provider_id": None})
confere(d and d["tipo"] == "audio" and d["url"] == "/api/chat/mensagens/7/midia",
        "rótulo [áudio] gravado sem id vira player (o arquivo é buscado pelo horário)")
d = chat_service._descrever_midia({"id": 8, "text": "[documento] contrato.pdf"})
confere(d and d["tipo"] == "documento" and d["nome"] == "contrato.pdf", "documento leva o nome do arquivo")
confere(chat_service._descrever_midia({"id": 9, "text": "oi"}) is None, "texto comum não tem mídia")
d = chat_service._descrever_midia({"id": 10, "text": "[imagem] olha", "media_file": "x.jpg",
                                   "media_mime": "image/jpeg"})
confere(d and d["tipo"] == "imagem" and d["baixada"], "imagem já baixada")

print("[17] follow-up espera antes da IA")
from senders import instagram_sender  # noqa: E402
import personalizar  # noqa: E402
_livre, _aplicar = instagram_sender.sem_conta_livre, personalizar.aplicar
chamou_ia = []
instagram_sender.sem_conta_livre = lambda: "todas as contas do Instagram estão abertas no painel"
personalizar.aplicar = lambda *a, **k: chamou_ia.append(1) or "x"
try:
    r = follow_up._enviar_instagram({}, {"id": 1, "instagram": "@lead", "nome": "Clínica"},
                                    {"id": 1, "corpo": "Oi"}, "follow_up_d3")
finally:
    instagram_sender.sem_conta_livre, personalizar.aplicar = _livre, _aplicar
confere(r.get("ocupada") is True and r.get("permanente") is False, "vira espera (ocupada)")
confere(not chamou_ia, "e a IA de personalização não foi chamada")

print("[19] áudio e vídeo do Direct")
limpar()
abrir("contateste", embutida=True)
est = bot.JANELAS["contateste"]
bot._registrar_midias(est, [{"url": "https://scontent-gru2-1.cdninstagram.com/v/audioclip.mp4?oh=x",
                             "tipo": "audio"}])
ev = est.get("midia_evento") or {}
confere(ev.get("id") and ev.get("tipo") == "audio", "play de áudio do CDN vira evento com id")
bot._registrar_midias(est, [{"url": "blob:https://www.instagram.com/123", "tipo": "audio"}])
ev2 = est.get("midia_evento") or {}
confere(ev2.get("id") is None and ev2.get("erro") and ev2["seq"] > ev["seq"], "blob: avisa que não dá para tocar")
bot._registrar_midias(est, [{"url": "https://evil.example/a.mp4", "tipo": "audio"}])
confere(not any("evil" in m["url"] for m in est.get("midias", {}).values()), "host de fora não entra na lista")
confere(not bot._host_de_midia("http://scontent.cdninstagram.com/a.mp4"), "só https")
confere(not bot._host_de_midia("https://cdninstagram.com.evil.example/a.mp4"), "host parecido é recusado")
confere(bot._host_de_midia("https://cdn.fbsbx.com/v/t59/x/audioclip-1-2.mp4?sdl=1"),
        "voz do Direct (cdn.fbsbx.com) é aceita")
r = cliente.get("/api/janela-manual/contateste/midia/naoexiste")
confere(r.status_code == 404, "id desconhecido devolve 404 (nada de URL vinda de fora)")
r = cliente.get("/api/janela-manual/contateste/midia", environ_base={"REMOTE_ADDR": "192.168.3.50"})
confere(r.status_code == 403, "evento de mídia só para esta máquina")
r = cliente.get(f"/api/janela-manual/contateste/midia/{ev['id']}", environ_base={"REMOTE_ADDR": "192.168.3.50"})
confere(r.status_code == 403, "arquivo de mídia só para esta máquina")
limpar()

print("[20] navegador único")
limpar()
abrir("contateste", embutida=True)
confere("contateste" not in bot.PERFIS_ABERTOS, "conta no painel não conta como pasta aberta")
r = cliente.post("/api/enviar_lote", json=dict(LOTE, conta="contateste"))
confere(r.status_code == 200, "lote pela conta aberta no painel segue (follow-up não espera)")
limpar()
bot.BOT_RODANDO = True
bot.CONTAS_DO_BOT.update({"prospectando"})
r = abrir("prospectando", embutida=True)
confere(r.status_code == 202, "conta prospectando abre no painel (não usa a pasta)")
r = abrir("prospectando2")
bot.CONTAS_DO_BOT.update({"prospectando2"})
limpar()
bot.BOT_RODANDO = True
bot.CONTAS_DO_BOT.update({"prospectando2"})
r = abrir("prospectando2")
confere(r.status_code == 409, "janela EXTERNA da conta prospectando continua recusada")
limpar()
import types  # noqa: E402
import psutil  # noqa: E402
for c in ("velha", "media", "nova", "quarta"):
    abrir(c, embutida=True)
bot.JANELAS["velha"]["tela"]._sem_desde -= 500       # parada há mais tempo
bot.JANELAS["media"]["tela"]._sem_desde -= 200
with bot.JANELAS_LOCK:
    bot._limitar_acordadas("quarta")
dormem = [c for c in ("velha", "media", "nova") if bot.JANELAS[c].get("dormir_ja")]
confere(dormem == ["velha"], f"4ª conta acordando: só a parada há mais tempo dorme (máximo {bot.MAX_ACORDADAS})")
bot.JANELAS["velha"]["dormir_ja"] = False
_vm = psutil.virtual_memory
psutil.virtual_memory = lambda: types.SimpleNamespace(available=500 * 1048576)
try:
    # a memória é lida FORA do lock (o psutil leva ms e o lock trava a tela de todas)
    pouca = bot._pouca_memoria()
    with bot.JANELAS_LOCK:
        bot._limitar_acordadas("quarta", pouca)
finally:
    psutil.virtual_memory = _vm
dormem = [c for c in ("velha", "media", "nova") if bot.JANELAS[c].get("dormir_ja")]
confere(pouca and dormem == ["velha", "media"], "memória baixa: dorme uma a mais")
bot.JANELAS["nova"]["tela"].espectadores = 1
for c in ("velha", "media"):
    bot.JANELAS[c]["dormir_ja"] = False
with bot.JANELAS_LOCK:
    vistas = bot._contas_vistas_no_painel()
confere(vistas == {"nova"}, "conta com o operador olhando aparece como vista")
r = cliente.post("/api/enviar_lote", json=dict(LOTE, conta="nova"))
confere(r.status_code == 409 and r.get_json().get("ocupada"), "follow-up espera a conta que o operador está olhando")
bot.JANELAS["nova"]["tela"].espectadores = 0
r = cliente.post("/api/enviar_lote", json=dict(LOTE, conta="nova"))
confere(r.status_code == 200, "parada (ninguém olhando), o follow-up segue por ela")
bot.BOT_RODANDO = False
bot.CONTAS_DO_BOT.clear()
est = bot.JANELAS["nova"]
url = "https://cdn.fbsbx.com/v/t59/x/audioclip-1-2.mp4?sdl=1"
bot._registrar_midias(est, [{"url": url, "tipo": "audio"}])
seq1 = est["midia_evento"]["seq"]
bot._registrar_midias(est, [{"url": url, "tipo": "audio"}])
confere(est["midia_evento"]["seq"] == seq1, "o mesmo play visto duas vezes em 3 s não vira dois eventos")
est["midia_evento_em"] -= 10
bot._registrar_midias(est, [{"url": url, "tipo": "audio"}])
confere(est["midia_evento"]["seq"] > seq1, "tocar de novo o mesmo áudio depois vale")
confere(bot.janela_remota.esconder_da_barra(set()) == (0, 0), "barra: sem processo, nada achado")
limpar()

print("[18] bot ocupado é espera")
limpar()
bot.BOT_RODANDO = True
r = cliente.post("/api/enviar_lote", json=dict(LOTE, conta="qualquer"))
confere(r.status_code == 400 and r.get_json().get("ocupada") is True,
        "lote com o bot rodando volta `ocupada` (o follow-up não gasta tentativa)")
limpar()

print("[21] rodada 6: aviso com prazo, nicho, conta acordando, regra de ocupada")
limpar()
for c in ("um", "dois"):
    abrir(c, embutida=True)
e = bot.JANELAS["um"]
bot._avisar(e, "link externo bloqueado: exemplo.com")
confere(bot._aviso_vigente(e) == "link externo bloqueado: exemplo.com", "aviso novo aparece")
e["aviso_em"] -= bot.AVISO_VALE_S + 1
confere(bot._aviso_vigente(e) is None, "aviso some depois do prazo (o estado da conta volta)")
r = cliente.get("/api/janela-manual/status")
confere(all(x.get("aviso") is None for x in r.get_json()["active"]) and
        not any("sono" in x for x in r.get_json()["active"]), "status sem aviso vencido e sem campo duplicado")
for nicho, ok in (("clinicas", True), ("Agencias", True), ("Clínicas Odonto", True),
                  (r"..\..\x", False), ("a/b", False), ("", False), (None, False)):
    confere(bot._nicho_valido(nicho) is ok, f"nicho {nicho!r} {'aceito' if ok else 'recusado'}")
r = cliente.post("/api/iniciar", json={"nicho": r"..\fora", "mensagem_ia": "oi"})
confere(r.status_code == 400, "prospecção com nicho que sai da pasta é recusada")
bot.JANELAS["um"].update(dormindo=True, acordando=True)
bot.JANELAS["dois"]["tela"]._sem_desde -= 500
_max = bot.MAX_ACORDADAS
bot.MAX_ACORDADAS = 2
try:
    with bot.JANELAS_LOCK:
        bot._limitar_acordadas("tres")
finally:
    bot.MAX_ACORDADAS = _max
confere(bot.JANELAS["dois"].get("dormir_ja") and not bot.JANELAS["um"].get("dormir_ja"),
        "conta no meio de acordar conta como acordada e não é posta para dormir")
bot.JANELAS["dois"]["dormir_ja"] = False
bot.JANELAS["dois"]["tela"].espectadores = 1
with bot.JANELAS_LOCK:
    ocupadas = bot._contas_ocupadas()
confere(ocupadas == {"dois"}, f"ocupada = só a que está sendo vista ({sorted(ocupadas)})")
r = cliente.get("/api/janela-manual/status").get_json()
confere(r.get("contas_ocupadas") == ["dois"] and sorted(r.get("contas_abertas")) == ["dois", "um"],
        "status publica a regra pronta (ocupadas e abertas)")
bot.FALHAS_PAINEL["tres"] = ("sem sessão", __import__("time").time())
f = cliente.get("/api/janela-manual/status").get_json()["falhas"].get("tres") or {}
confere(f.get("motivo") == "sem sessão" and f.get("em"), "falha vai com o instante (avisa de novo se repetir)")
bot.FALHAS_PAINEL.clear()
bot.JANELAS["dois"]["tela"].espectadores = 0
limpar()

print("[22] aquecimento leve")
limpar()
r = cliente.post("/api/janela-manual", json={"conta": "aq1", "ativo": True, "destino": "reels",
                                             "fechar_apos_min": 30})
coro = _ThreadFalsa.criadas[-1] if _ThreadFalsa.criadas else None
confere(r.status_code == 202 and getattr(coro, "__name__", "") == "_janela_aquecimento",
        "Reels fora do painel abre no navegador do aquecimento")
e = bot.JANELAS["aq1"]
confere(e.get("aquecimento") and 29 * 60 < e["fechar_em"] - __import__("time").monotonic() <= 30 * 60,
        "fechar_apos_min marca a hora de fechar")
st = [x for x in cliente.get("/api/janela-manual/status").get_json()["active"] if x["conta"] == "aq1"][0]
confere(st["aquecimento"] and st["fecha_em_min"] == 30, f"status mostra quanto falta ({st['fecha_em_min']} min)")
confere(bot._fechar_em("0") is None and bot._fechar_em("abc") is None and bot._fechar_em(99999) is None,
        "tempo fora da faixa é ignorado")
with bot.JANELAS_LOCK:
    confere("aq1" in bot._contas_ocupadas(), "conta em aquecimento fica fora da prospecção")
r = cliente.post("/api/janela-manual", json={"conta": "aq2", "ativo": True, "destino": "inicio"})
confere(getattr(_ThreadFalsa.criadas[-1], "target", None) is bot._rodar_janela_manual,
        "\"abrir perfil\" continua no perfil persistente (cookies voltam para a pasta)")
limpar()
rl = bot.reels_leve
man = ('"dash_manifest":"\u003CMPD>\u003CPeriod>'
       '\u003CAdaptationSet contentType=\\"video\\">'
       '\u003CRepresentation id=\\"a\\" bandwidth=\\"900\\" width=\\"720\\" height=\\"1280\\">x\u003C\/Representation>'
       '\u003CRepresentation id=\\"b\\" bandwidth=\\"300\\" width=\\"360\\" height=\\"640\\">y\u003C\/Representation>'
       '\u003CRepresentation id=\\"c\\" bandwidth=\\"200\\" width=\\"360\\" height=\\"640\\">z\u003C\/Representation>'
       '\u003C\/AdaptationSet>'
       '\u003CAdaptationSet contentType=\\"audio\\">\u003CRepresentation id=\\"s\\" bandwidth=\\"64\\">w\u003C\/Representation>\u003C\/AdaptationSet>'
       '\u003C\/Period>\u003C\/MPD>"')
novo, versoes, audios = rl.menor_qualidade(man)
confere('id=\\"c\\"' in novo and 'id=\\"a\\"' not in novo and 'id=\\"b\\"' not in novo,
        "fica só a versão de menor altura e menor bitrate")
confere(audios == 1 and "audio" not in novo, "a trilha de áudio sai (o Chromium não toca AAC)")
confere(rl.menor_qualidade("sem manifesto")[0] == "sem manifesto", "resposta sem manifesto passa igual")
pre = ('{"x":"Representation","extensions":{"all_video_dash_prefetch_representations":'
       '[{"representations":[{"mime_type":"audio/mp4","base_url":"a]b"}]}],"y":1}}')
novo_pre, _, _ = rl.menor_qualidade(pre)
confere(json.loads(novo_pre)["extensions"] == {"all_video_dash_prefetch_representations": [], "y": 1},
        "a lista de pré-carregamento (com o áudio AAC) fica vazia e o JSON continua válido")
gt = (r'x<AdaptationSet contentType=\"video\">'
      r'<Representation id=\"a\" bandwidth=\"9\" height=\"1280\">1</Representation>'
      r'<Representation id=\"b\" bandwidth=\"1\" height=\"640\">2</Representation>'
      r'</AdaptationSet><AdaptationSet contentType=\"audio\">'
      r'<Representation codecs=\"mp4a.40.5\">3</Representation></AdaptationSet>')
novo_gt, v_gt, a_gt = rl.menor_qualidade(gt)
confere(v_gt == 1 and a_gt == 1 and "mp4a" not in novo_gt and 'id=\\"b\\"' in novo_gt,
        "manifesto com < e > escritos como \u003c/\u003e também é reduzido")

print("[23] ativar/desativar todas e agendamento")
limpar()
abrir("manual1", destino="inicio")                 # janela de "abrir perfil"
r = cliente.post("/api/aquecimento/todas", json={"ativo": True, "contas": ["aqa", "aqb", "manual1"],
                                                 "fechar_apos_min": 60}).get_json()
confere(sorted(r["contas"]) == ["aqa", "aqb"] and [p["conta"] for p in r["puladas"]] == ["manual1"],
        f"ativar todas abre as livres e não mexe na janela de perfil ({r['mensagem']})")
confere(bot.JANELAS["aqa"]["modelo"] == "aquecimento" and bot.JANELAS["manual1"]["modelo"] == "manual",
        "o modelo da janela fica gravado")
bot.JANELAS["aqa"]["destino"] = "outro"           # o operador clicou num perfil
r = cliente.post("/api/aquecimento/todas", json={"ativo": False}).get_json()
confere(sorted(r["contas"]) == ["aqa", "aqb"] and not bot.JANELAS["manual1"]["stop"].is_set(),
        "desativar todas fecha pelo modelo (mesmo fora do Reels) e deixa a janela de perfil")
confere(cliente.post("/api/aquecimento/todas", json={"ativo": "sim"}).status_code == 400,
        "ativo precisa ser booleano")
confere(bot._fechar_em(True) is None and bot._fechar_em(float("inf")) is None, "tempo inválido não quebra")
limpar()
import agendamentos as _ag  # noqa: E402
from datetime import datetime as _dt  # noqa: E402
_lista, _disp = _ag.vencidos, _ag.disparar
chamados = []
try:
    _ag.vencidos = lambda agora=None: [{"id": 1, "canal": "aquecimento", "hora": "09:00", "resumo": "aq"},
                                       {"id": 2, "canal": "instagram", "hora": "09:00", "resumo": "ig"}]
    _ag.disparar = lambda ag: chamados.append(ag["canal"]) or {"ok": False, "bot_fora": True, "erro": "fora"}
    _marcar = []
    class _Con:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=()): _marcar.append(args[-1]); return self
    _ag.init()                      # a tabela fica pronta antes de trocar o banco
    _conn = _ag.db.conn
    _ag.db.conn = lambda: _Con()
    _ag.rodar_pendentes(_dt.now().replace(hour=9, minute=20))
    confere(chamados == ["instagram", "aquecimento"], "no mesmo horário a prospecção vai antes do aquecimento")
    confere(_marcar == [2], "aquecimento com o bot fora não é marcado (tenta no próximo minuto)")
    chamados.clear(); _marcar.clear()
    _ag.rodar_pendentes(_dt.now().replace(hour=11, minute=0))
    confere(chamados == ["instagram"] and 1 in _marcar, "2 h depois do horário o aquecimento não abre (fica marcado)")
finally:
    _ag.vencidos, _ag.disparar = _lista, _disp
    _ag.db.conn = _conn
confere(not _ag.criar("aquecimento", "09:00", "0", {"acao": "explodir"})["ok"]
        and not _ag.criar("aquecimento", "09:00", "0", {"acao": "ativar", "fechar_apos_min": 9999})["ok"],
        "agendamento do aquecimento com ação ou tempo inválido é recusado")

print()
print("TUDO CERTO" if not falhas else f"{len(falhas)} FALHA(S)")
sys.exit(1 if falhas else 0)
