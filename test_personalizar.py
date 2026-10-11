# -*- coding: utf-8 -*-
"""
test_personalizar.py — cada mensagem sob medida para o lead, e a IA nunca manda o que
não devia.
"""

import ast
import json
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))
_TMP = Path(tempfile.mkdtemp(prefix="lf_perso_"))
os.environ["LEADFORGE_DB"] = str(_TMP / "banco.db")

import db               # noqa: E402
import dashboard_db     # noqa: E402
import ia_roteador      # noqa: E402  (carrega o .env)
import personalizar     # noqa: E402

CHAVE_REAL = os.environ.get("IA_MSG_KEY", "")

falhas = []


def checar(ok, texto):
    print(f"  {'ok  ' if ok else 'FALHOU'} {texto}")
    if not ok:
        falhas.append(texto)


# ════════════════════════════════════════════════════════════════════════════
#  O 9ROUTER FALSO
# ════════════════════════════════════════════════════════════════════════════

PEDIDOS = []                  # modelos pedidos, na ordem
COMPORTAMENTO = {}            # modelo -> o que responder
RESPOSTA_OK = ("Boa tarde! Aqui é o Leonardo. Vi a nota 4,1 da Clínica Sorriso no Google, "
               "com 23 avaliações. Posso te mandar uma análise?")


class _Falso(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, codigo, dados):
        corpo = json.dumps(dados).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        try:
            self.wfile.write(corpo)
        except OSError:
            pass            # o cliente desistiu (prazo estourado): é o esperado

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        pedido = json.loads(self.rfile.read(n) or b"{}")
        modelo = pedido.get("model", "")
        PEDIDOS.append(modelo)
        c = COMPORTAMENTO.get(modelo, "ok")
        if c.startswith("http"):
            self._json(int(c[4:]), {"error": {"message": f"falso {c}"}})
        elif c == "lento":
            time.sleep(2.5)
            self._json(200, {"choices": [{"message": {"content": RESPOSTA_OK},
                                          "finish_reason": "stop"}]})
        elif c == "cortado":
            self._json(200, {"choices": [{"message": {"content": "Boa tarde! Aqui é o"},
                                          "finish_reason": "length"}]})
        elif c == "vazio":
            self._json(200, {"choices": [{"message": {"content": ""},
                                          "finish_reason": "stop"}]})
        elif c == "sse":
            partes = [RESPOSTA_OK[:30], RESPOSTA_OK[30:]]
            corpo = "".join("data: " + json.dumps({"choices": [{"delta": {"content": p}}]}) + "\n\n"
                            for p in partes) + "data: [DONE]\n\n"
            dados = corpo.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(dados)))
            self.end_headers()
            self.wfile.write(dados)
        elif c == "inventa":
            self._json(200, {"choices": [{"message": {
                "content": "Boa tarde! Vi que 87% dos seus pacientes vêm do Google. "
                           "Posso te mandar uma análise gratuita?"}, "finish_reason": "stop"}]})
        else:
            self._json(200, {"choices": [{"message": {"content": RESPOSTA_OK},
                                          "finish_reason": "stop"}]})


def _subir_falso():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Falso)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    porta = srv.server_address[1]
    ia_roteador.URL = f"http://127.0.0.1:{porta}/v1"
    ia_roteador.HOST, ia_roteador.PORTA, ia_roteador.LOCAL = "127.0.0.1", porta, True
    ia_roteador.CHAVE = "chave-de-teste"
    ia_roteador.PRAZO_MODELO, ia_roteador.PRAZO_TOTAL = 1, 12
    return srv


def _modelos(*nomes):
    ia_roteador.MODELOS = list(nomes)
    ia_roteador._esquecer()
    PEDIDOS.clear()


# ════════════════════════════════════════════════════════════════════════════
#  DADOS
# ════════════════════════════════════════════════════════════════════════════

def preparar():
    import scrap_destino
    db.init_db()
    dashboard_db.init_dashboard()
    scrap_destino.init()          # a coluna leads.scrap_id nasce aqui
    with db.conn() as con:
        sid = con.execute("INSERT INTO scrap_queue(termo, localizacao) VALUES(?,?)",
                          ("dentista", "arapiraca alagoas")).lastrowid
        lid = con.execute(
            "INSERT INTO leads(maps_url, nome, categoria, nicho, avaliacao_num, "
            "total_avaliacoes_num, website, posicao, uf, scrap_id, telefone) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("https://maps/1", "Clínica Sorriso | Odontologia Integrada", "Dentista",
             "odontologia", 4.1, 23, "https://www.sorrisoteste.com.br/contato", 7, "AL",
             sid, "82999990000")).lastrowid
        con.execute("INSERT INTO diagnosticos(lead_id, problemas, concorrente_nome, "
                    "concorrente_nota, texto) VALUES(?,?,?,?,?)",
                    (lid, json.dumps(["Site lento no celular", "Poucas avaliações"]),
                     "Clínica Rival", 4.8, "Texto do diagnóstico."))
        sem = con.execute("INSERT INTO leads(maps_url, nome) VALUES(?,?)",
                          ("https://maps/2", "Studio Ana")).lastrowid
        outros = [con.execute("INSERT INTO leads(maps_url, nome) VALUES(?,?)",
                              (f"https://maps/v{i}", f"Lead {i}")).lastrowid for i in range(12)]
    return dict(db.get_lead(lid)), dict(db.get_lead(sem)), [dict(db.get_lead(i)) for i in outros]


# ════════════════════════════════════════════════════════════════════════════
#  A. FUNÇÕES DO TEMPLATE
# ════════════════════════════════════════════════════════════════════════════

def teste_funcoes(lead, sem, outros):
    print("\n[A1] variáveis do lead")
    tarde = datetime(2026, 9, 25, 15, 0)
    v = personalizar.variaveis(lead, tarde)
    esperado = {"nota": "4,1", "avaliacoes": "23", "posicao": "7",
                "site": "sorrisoteste.com.br", "regiao": "Arapiraca Alagoas", "uf": "AL",
                "concorrente": "Clínica Rival", "nota_concorrente": "4,8",
                "problema": "Site lento no celular", "saudacao": "Boa tarde"}
    for k, esp in esperado.items():
        checar(v.get(k) == esp, f"{{{k}}} = {v.get(k)!r} (esperado {esp!r})")
    checar("|" not in v["nome_curto"], f"{{nome_curto}} sem o sufixo: {v['nome_curto']!r}")
    checar(personalizar.variaveis(lead, datetime(2026, 9, 25, 8))["saudacao"] == "Bom dia"
           and personalizar.variaveis(lead, datetime(2026, 9, 25, 20))["saudacao"] == "Boa noite",
           "{saudacao} segue a hora do envio")

    print("\n[A2] blocos {se ...} e {sem ...}")
    # o espaço DEPOIS dos dois-pontos não conta (é para ler); o que vai no texto
    # fica fora das chaves
    tpl = "Oi! {se nota: Vi a nota {nota} no Google.} {sem site: Vocês não têm site?}"
    checar(personalizar.renderizar(lead, tpl) == "Oi! Vi a nota 4,1 no Google.",
           f"com nota e com site: {personalizar.renderizar(lead, tpl)!r}")
    checar(personalizar.renderizar(sem, tpl) == "Oi! Vocês não têm site?",
           f"sem nota e sem site: {personalizar.renderizar(sem, tpl)!r}")
    checar("  " not in personalizar.renderizar(sem, "Olá {nota} , tudo bem?"),
           "variável vazia não deixa espaço duplo nem ' ,'")

    print("\n[A3] variações {a|b}: fixas por lead, diferentes entre leads")
    tpl = "{Oi|Olá|Bom dia}, {nome_curto}! {Tudo bem?|Como vai?}"
    a1, a2 = personalizar.renderizar(lead, tpl), personalizar.renderizar(lead, tpl)
    checar(a1 == a2, f"o mesmo lead recebe sempre a mesma: {a1!r}")
    distintas = {personalizar.renderizar(o, tpl) for o in outros}
    checar(len(distintas) >= 2, f"leads diferentes recebem opções diferentes ({len(distintas)} versões em 12)")
    checar(personalizar.renderizar(lead, "{Vi {nome_curto}|Achei {nome_curto}} no Maps").endswith("no Maps")
           and "{" not in personalizar.renderizar(lead, "{Vi {nome_curto}|Achei {nome_curto}} no Maps"),
           "variação com variável dentro funciona")
    nome_barra = dict(lead, nome="Natália | Tráfego Pago", id=999)
    r = personalizar.renderizar(nome_barra, "{Oi|Olá} {nome}")
    checar(r.endswith("Natália | Tráfego Pago"), f"nome com '|' não quebra a variação: {r!r}")

    print("\n[A4] um renderizador só")
    import esteira_wa
    tpl = "{saudacao}, {nome_curto}! Nota {nota}, região {regiao}."
    checar(esteira_wa.renderizar(lead, tpl) == personalizar.renderizar(lead, tpl),
           "esteira_wa.renderizar delega para personalizar.renderizar")

    print("\n[A5] perfil do Instagram (prospecção por seguidores)")
    tpl = "{Oi|Olá}{se nome: , {nome}}! Vi o seu perfil {usuario}."
    r = personalizar.renderizar_perfil({"usuario": "ana.mkt", "nome": "Ana"}, tpl)
    checar(r.startswith(("Oi, Ana!", "Olá, Ana!")) and r.endswith("@ana.mkt."), f"com nome: {r!r}")
    r = personalizar.renderizar_perfil({"usuario": "ana.mkt", "nome": ""}, tpl)
    checar(r.startswith(("Oi!", "Olá!")), f"sem nome, sem vírgula sobrando: {r!r}")
    try:
        "{Oi|Olá} {nome}".format(nome="Ana")
        quebrava = False
    except (KeyError, ValueError, IndexError):
        quebrava = True
    checar(quebrava, "PROVA: o .format(nome=) antigo do bot quebrava com variação — "
                     "o perfil ia para REJECTED")


# ════════════════════════════════════════════════════════════════════════════
#  B. CHECAGENS
# ════════════════════════════════════════════════════════════════════════════

def teste_checagens(lead):
    print("\n[B] checagens que reprovam a IA")
    base = ("Boa tarde! Aqui é o Leonardo. Vi que vocês têm nota 4,1 no Google. "
            "Posso te mandar uma análise gratuita?")
    ctx = personalizar._contexto_lead(lead)
    ok = personalizar.checar(RESPOSTA_OK, base, ctx, "whatsapp")
    checar(ok == [], f"texto bom passa: {ok}")
    casos = {
        "número inventado": "Boa tarde! Aqui é o Leonardo. Vi que 3 de cada 10 clientes somem. Posso te mandar uma análise?",
        "link inventado": "Boa tarde! Aqui é o Leonardo. Veja em www.meusite.com.br a análise. Posso te mandar?",
        "oferta inventada": "Boa tarde! Aqui é o Leonardo. Vi a nota 4,1 no Google. Posso te dar um desconto?",
        "cortado": "Boa tarde! Aqui é o Leonardo. Vi a nota 4,1 da Clínica Sorriso no Google e queria",
        "placeholder": "Boa tarde! Aqui é o Leonardo. Vi a nota 4,1 da [Nome da Clínica]. Posso te mandar?",
        "markdown": "Boa tarde! Aqui é o **Leonardo**. Vi a nota 4,1 no Google. Posso te mandar uma análise?",
        "curto": "Oi!",
        "longo": "Boa tarde! " + "Vi a nota 4,1 da Clínica Sorriso no Google. " * 12 + "Posso?",
    }
    for rotulo, texto in casos.items():
        m = personalizar.checar(texto, base, ctx, "whatsapp")
        checar(bool(m), f"{rotulo}: reprovado ({'; '.join(m)[:90]})")
    gratis = personalizar.checar(
        "Boa tarde! Aqui é o Leonardo. Vi a nota 4,1 no Google. Posso te mandar a análise gratuita?",
        base, ctx, "whatsapp")
    checar(not any("oferta" in x for x in gratis),
           "'gratuita' que JÁ estava na base do dono não conta como inventada")
    desc = personalizar.checar(
        "Boa tarde! Aqui é o Leonardo. Vi a nota 4,1 no Google. Análise gratuita e com desconto?",
        base, ctx, "whatsapp")
    checar(any("desconto" in x for x in desc),
           "mas 'gratuita' na base NÃO libera a IA a inventar 'desconto'")
    checar(personalizar.checar("Boa tarde! Vi o site sorrisoteste.com.br e a nota 4.1. Posso te mandar uma análise?",
                               base, ctx, "whatsapp") == [],
           "domínio que está nos dados e '4.1' para '4,1' passam")


# ════════════════════════════════════════════════════════════════════════════
#  C. A CORRENTE DE MODELOS, CONTRA O 9ROUTER FALSO
# ════════════════════════════════════════════════════════════════════════════

def teste_corrente(lead):
    base = personalizar.renderizar(lead, "Boa tarde! Aqui é o Leonardo. {se nota: Vi que vocês "
                                         "têm nota {nota} no Google.} Posso te mandar uma análise?")
    personalizar.salvar("whatsapp", True, "cite a nota")

    print("\n[C1] resposta boa: personaliza, guarda e reaproveita")
    _modelos("bom")
    r = personalizar.personalizar(lead, "whatsapp", base, etapa="dia0")
    checar(r["personalizado"] and r["texto"] == RESPOSTA_OK, f"personalizada ({r['modelo']})")
    n = len(PEDIDOS)
    r2 = personalizar.personalizar(lead, "whatsapp", base, etapa="dia0")
    checar(r2["texto"] == RESPOSTA_OK and len(PEDIDOS) == n,
           "a segunda vez (o envio depois da prévia) sai do cache, sem chamar a IA")
    personalizar.salvar("whatsapp", True, "outra instrução")
    personalizar.personalizar(lead, "whatsapp", base, etapa="dia0")
    checar(len(PEDIDOS) == n + 1, "mudou a instrução: gera de novo")

    print("\n[C2] erro do provedor: castigo e próximo modelo")
    COMPORTAMENTO.update({"sem_credito": "http402", "cheio": "http429", "aposentado": "http410"})
    _modelos("sem_credito", "cheio", "aposentado", "bom")
    r = personalizar.personalizar(lead, "whatsapp", base, etapa="c2", usar_cache=False)
    checar(r["personalizado"] and r["modelo"] == "bom",
           f"passou pelos três quebrados e usou o 'bom': {[t['resultado'] for t in r['tentativas']]}")
    PEDIDOS.clear()
    r = personalizar.personalizar(lead, "whatsapp", base, etapa="c2b", usar_cache=False)
    checar(PEDIDOS == ["bom"], f"na próxima, os castigados nem são chamados: {PEDIDOS}")
    checar(all(p > 0 for p in ia_roteador.estado()["de_castigo"].values())
           and len(ia_roteador.estado()["de_castigo"]) == 3, "e o estado mostra os três de castigo")

    print("\n[C3] cortado, vazio, fluxo SSE e demora")
    COMPORTAMENTO.update({"corta": "cortado", "oco": "vazio", "fluxo": "sse", "devagar": "lento"})
    _modelos("corta", "oco", "fluxo")
    r = personalizar.personalizar(lead, "whatsapp", base, etapa="c3", usar_cache=False)
    res = [t["resultado"] for t in r["tentativas"]]
    checar(res[:2] == ["cortado", "vazio"], f"cortado e vazio contam como falha: {res}")
    checar(r["personalizado"] and r["texto"] == RESPOSTA_OK, "a resposta em fluxo (SSE) é lida inteira")
    _modelos("devagar", "bom")
    t0 = time.time()
    r = personalizar.personalizar(lead, "whatsapp", base, etapa="c3b", usar_cache=False)
    checar(r["personalizado"] and r["modelo"] == "bom" and time.time() - t0 < 5,
           f"o lento estoura o prazo por modelo e o próximo responde ({time.time()-t0:.1f}s)")

    print("\n[C4] tudo falha, ou a IA inventa: sai o TEMPLATE, e fica registrado")
    COMPORTAMENTO.update({"mentiroso": "inventa"})
    _modelos("sem_credito2", "mentiroso")
    COMPORTAMENTO["sem_credito2"] = "http402"
    r = personalizar.personalizar(lead, "whatsapp", base, etapa="c4")
    checar(not r["personalizado"] and r["texto"] == base, "reprovada: o texto é o template")
    checar("reprovada" in r["motivo"] and r.get("reprovada"),
           f"e diz por quê: {r['motivo'][:100]}")
    _modelos("mentiroso")
    personalizar.personalizar(lead, "whatsapp", base, etapa="c4")
    checar(PEDIDOS == ["mentiroso"], "reprovada NÃO vai para o cache (tenta de novo no próximo)")
    ult = personalizar.ultimas("whatsapp", 3)
    checar(ult and ult[0]["resultado"] == "template" and "reprovada" in ult[0]["motivo"],
           f"o registro de decisões mostra: {ult[0]['motivo'][:70] if ult else '-'}")
    checar(personalizar.aplicar(lead, "whatsapp", base, "c4") == base,
           "aplicar() devolve o template, sem levantar erro")

    print("\n[C5] canal desligado e botão de teste")
    personalizar.salvar("whatsapp", False)
    _modelos("bom")
    r = personalizar.personalizar(lead, "whatsapp", base, etapa="c5")
    checar(not r["personalizado"] and PEDIDOS == [] and r["texto"] == base,
           "desligado: nem chama a IA")
    antes = len(personalizar.ultimas("whatsapp", 500))
    r = personalizar.personalizar(lead, "whatsapp", base, etapa="c5", usar_cache=False, forcar=True)
    checar(r["personalizado"], "o botão Testar roda mesmo desligado")
    checar(len(personalizar.ultimas("whatsapp", 500)) == antes,
           "e o teste NÃO entra no registro de decisões")

    print("\n[C6] 9router fechado")
    personalizar.salvar("whatsapp", True)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); porta_morta = s.getsockname()[1]; s.close()
    url_ok, porta_ok = ia_roteador.URL, ia_roteador.PORTA
    ia_roteador.URL, ia_roteador.PORTA = f"http://127.0.0.1:{porta_morta}/v1", porta_morta
    chamadas = []
    original = ia_roteador.garantir
    ia_roteador.garantir = lambda esperar_s=25.0: (chamadas.append(1) or
                                                   {"no_ar": False, "acao": "falhou", "detalhe": "teste"})
    try:
        _modelos("a", "b", "c")
        r = personalizar.personalizar(lead, "whatsapp", base, etapa="c6", usar_cache=False)
        checar(not r["personalizado"] and r["texto"] == base and "fora do ar" in r["motivo"],
               f"sai o template: {r['motivo']}")
        checar(len(chamadas) == 1, f"tentou abrir o 9router UMA vez, e não por modelo ({len(chamadas)})")
    finally:
        ia_roteador.garantir = original
        ia_roteador.URL, ia_roteador.PORTA = url_ok, porta_ok

    print("\n[C7] perfil do Instagram com a IA")
    personalizar.salvar("instagram", True)
    _modelos("bom")
    perfil = {"usuario": "ana.mkt", "nome": "Ana", "bio": "Tráfego pago para clínicas. Nota 4,1 com 23 avaliações",
              "nicho": "Agencias"}
    tpl = "Boa tarde{se nome: , {nome}}! Aqui é o Leonardo. Vi que vocês têm nota 4,1 no Google. Posso te mandar uma análise?"
    r = personalizar.texto_do_perfil(tpl, perfil)
    checar(r["personalizado"], f"o perfil é personalizado: {r['motivo'] or r['modelo']}")
    pedido = personalizar._pedido("instagram", r["base"], personalizar._contexto_perfil(perfil), "")
    checar("Tráfego pago para clínicas" in pedido[1]["content"], "a bio do perfil vai para a IA")


# ════════════════════════════════════════════════════════════════════════════
#  D. O 9ROUTER ABRE SOZINHO — SEM ABRIR PROCESSO DE VERDADE
# ════════════════════════════════════════════════════════════════════════════

def teste_garantir():
    print("\n[D] garantir(): confere a porta e abre se estiver fechado")
    abertos = []
    estado_porta = {"no_ar": False}
    orig_no_ar, orig_iniciar = ia_roteador.no_ar, ia_roteador.iniciar
    ia_roteador.no_ar = lambda timeout=1.0: estado_porta["no_ar"]

    def iniciar_falso():
        abertos.append(1)
        estado_porta["no_ar"] = True          # "subiu"
        return True
    ia_roteador.iniciar = iniciar_falso
    try:
        ia_roteador._esquecer()
        g = ia_roteador.garantir(esperar_s=3)
        checar(g["acao"] == "iniciado" and g["no_ar"] and abertos == [1], f"fechado → abriu: {g}")
        g = ia_roteador.garantir(esperar_s=3)
        checar(g["acao"] == "ja_estava" and abertos == [1], "aberto → não abre de novo")
        estado_porta["no_ar"] = False
        ia_roteador.iniciar = lambda: (abertos.append(1) or True)   # abre e NÃO sobe
        g = ia_roteador.garantir(esperar_s=1)
        checar(len(abertos) == 1 and g["acao"] == "recente",
               f"logo depois de mandar abrir, não empilha outra janela: {g['acao']}")
    finally:
        ia_roteador.no_ar, ia_roteador.iniciar = orig_no_ar, orig_iniciar
        ia_roteador._esquecer()
    fonte = ast.unparse(next(n for n in ast.walk(ast.parse((RAIZ / "ia_roteador.py").read_text(encoding="utf-8")))
                             if isinstance(n, ast.FunctionDef) and n.name == "iniciar"))
    checar("CREATE_NEW_CONSOLE" in fonte and "16777216" in fonte,
           "abre em janela PRÓPRIA e pedindo para sair do Job do LeadForge "
           "(CREATE_BREAKAWAY_FROM_JOB = 0x01000000)")
    checar("title 9router" in fonte, "com o título '9router', como o dono abre")


# ════════════════════════════════════════════════════════════════════════════
#  E. OS PONTOS DE ENVIO, O subir.py E A CHAVE
# ════════════════════════════════════════════════════════════════════════════

def _funcoes(arq):
    arvore = ast.parse((RAIZ / arq).read_text(encoding="utf-8"))
    return {n.name: ast.unparse(n) for n in ast.walk(arvore)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def teste_ligacoes():
    print("\n[E1] todo ponto de envio passa pela personalização")
    ew = _funcoes("esteira_wa.py")
    checar("personalizar.aplicar(lead, 'whatsapp'" in ew["rodar"], "esteira (rodar)")
    checar("personalizar" not in ew["montar_mensagem"],
           "montar_mensagem continua determinístico (a IA fica no ponto de envio)")
    ef = _funcoes("envio_fila.py")
    checar("personalizar.aplicar(lead, 'whatsapp'" in ef["_trabalhar"], "fila de primeiro contato")
    fu = _funcoes("follow_up.py")
    for nome, canal in (("_enviar_whatsapp", "whatsapp"), ("_enviar_email", "email"),
                        ("_enviar_instagram", "instagram")):
        checar(f"personalizar.aplicar(lead, '{canal}'" in fu[nome], f"follow-up {canal}")
    app = _funcoes("dashboard/app.py")
    checar("personalizar.renderizar" in app["_mensagem_para"]
           and "personalizar.personalizar" in app["_mensagem_para"],
           "prospecção em lote do painel (_mensagem_para): renderizador único + IA")
    unit = next(c for c in app.values() if "ew.montar_mensagem(lead, etapa)" in c)
    checar("personalizar.personalizar(lead, 'whatsapp'" in unit
           and unit.find("personalizar.personalizar") < unit.find("if seco"),
           "botão do card: a PRÉVIA já mostra a personalizada")

    print("\n[E2] bot de DM: só a prospecção monta a mensagem ali")
    bot = _funcoes("senders/dm_bot/Servidor_N8N.py")
    checar("prospeccao=False" in bot["analisar_perfil"][:300],
           "analisar_perfil tem o parâmetro prospeccao")
    checar("prospeccao=True" in bot["varrer_fonte"], "a varredura de seguidores passa prospeccao=True")
    checar("prospeccao=True" not in bot["bot_lote_headless"],
           "o lote do Maps NÃO (já chega personalizado do painel)")
    formatos = [n for n, c in bot.items() if ".format(nome=" in c]
    checar(formatos == ["_formatar_antigo"], f"o .format(nome=) antigo ficou só para o lote: {formatos}")
    checar("asyncio.to_thread" in bot["_mensagem_final"],
           "a IA roda fora do event loop (as contas não travam juntas)")

    print("\n[E3] subir.py confere o 9router ao iniciar")
    sub = _funcoes("subir.py")
    m = sub["main"]
    checar("_estado_9router(abrir=True)" in m
           and m.find("_estado_9router(abrir=True)") < m.find("rodar_vinculado("),
           "antes de subir os serviços (inclusive no iniciar-bot.bat)")
    checar("JOB_OBJECT_LIMIT_BREAKAWAY_OK" in sub["_criar_job"],
           "o Job deixa o 9router sair (fechar o LeadForge não o derruba)")

    print("\n[E4] a chave do 9router só mora no .env")
    if not CHAVE_REAL:
        print("  (pulado: sem IA_MSG_KEY no .env)")
        return
    vazou = []
    for arq in list(RAIZ.rglob("*.py")) + list(RAIZ.rglob("*.js")) + list(RAIZ.rglob("*.html")):
        partes = set(arq.relative_to(RAIZ).parts)
        if partes & {"node_modules", "build", "dist", "build_cache"} or \
                any(p.startswith("_backup") for p in partes):
            continue
        try:
            if CHAVE_REAL in arq.read_text(encoding="utf-8", errors="ignore"):
                vazou.append(str(arq.relative_to(RAIZ)))
        except OSError:
            continue
    checar(not vazou, f"nenhum código tem a chave escrita: {vazou or 'nenhum'}")


if __name__ == "__main__":
    print("=" * 70)
    print("PERSONALIZAÇÃO POR LEAD — funções do template, IA do 9router e checagens")
    print("=" * 70)
    srv = None
    try:
        lead, sem, outros = preparar()
        teste_funcoes(lead, sem, outros)
        teste_checagens(lead)
        srv = _subir_falso()
        teste_corrente(lead)
        teste_garantir()
        teste_ligacoes()
    finally:
        if srv:
            srv.shutdown()
    print("\n" + "=" * 70)
    if falhas:
        print(f"RESULTADO: {len(falhas)} falha(s)")
        for f in falhas:
            print("  •", f)
        sys.exit(1)
    print("RESULTADO: tudo passou")
