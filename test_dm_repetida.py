# -*- coding: utf-8 -*-
"""test_dm_repetida.py — quem já recebeu DM não recebe a abordagem de novo."""

import ast
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "senders" / "dm_bot"))
_TMP = Path(tempfile.mkdtemp(prefix="lf_dm_repetida_"))
os.environ["LEADFORGE_DB"] = str(_TMP / "banco.db")

import config          # noqa: E402
import historico_ig    # noqa: E402

falhas = []


def checar(ok, texto):
    print(f"  {'ok  ' if ok else 'FALHOU'} {texto}")
    if not ok:
        falhas.append(texto)


def _json(caminho: Path, dados):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")


def _envio(data):
    return {"modelo": "x", "data": data, "respondeu": False, "converteu": False}


# ════════════════════════════════════════════════════════════════════════════
#  A. O HISTÓRICO
# ════════════════════════════════════════════════════════════════════════════

def teste_caso_real():
    print()
    print("[A0] o caso real: o perfil de março é reconhecido")
    antigo = Path(config.BASE_DIR).parent / "Prospecção" / "dminstagram"
    if not antigo.is_dir():
        print("  (pulado: o dminstagram antigo não existe nesta máquina)")
        return
    # o perfil do caso real vem do.env (LF_TESTE_DM_ANTIGA=usuario): não fica no
    # código
    caso = os.environ.get("LF_TESTE_DM_ANTIGA", "")
    if not caso:
        print("  (pulado: LF_TESTE_DM_ANTIGA não definido)")
        return
    historico_ig._esquecer()
    r = historico_ig.ja_abordado(caso)
    checar(r is not None, f"está no histórico: {r}")
    if r:
        checar(r["data"].startswith("2026-03-13"),
               f"e a data é a PRIMEIRA DM, de março: {r['data']}")
    checar(historico_ig.ja_abordado("nike") is None,
           "e quem nunca recebeu DM continua livre (@nike)")


def montar_pastas():
    """Um projeto e um bot antigo "vizinho", com o formato real de cada um."""
    proj, viz = _TMP / "proj", _TMP / "viz"
    _json(proj / "senders/dm_bot/perfis/Agencias/rastreio_dms.json",
          {"agencia1": _envio("2026-09-20 10:00:00")})
    (proj / "senders/dm_bot/perfis/Agencias/processados.txt").write_text(
        "agencia1|2026-09-20 10:00:00\nsorejeitado|2026-09-01 08:00:00\n",
        encoding="utf-8")
    _json(proj / "senders/dm_bot/perfis/clinicas/rastreio_dms.json",
          {"clinica1": _envio("2026-08-02 09:00:00")})
    _json(proj / "data/ig_perfis/Quebrado/rastreio_dms.json", {})
    (proj / "data/ig_perfis/Quebrado/rastreio_dms.json").write_text(
        "{isto nao e json", encoding="utf-8")
    # o bot antigo: perfis/ na raiz, e o executável em dist/
    _json(viz / "perfis/Gestores/rastreio_dms.json",
          {"SouNatalia": _envio("2026-03-13 15:52:15"),
           "agencia1": _envio("2026-02-01 08:00:00")})
    _json(viz / "dist/Prospec/_internal/perfis/Gestores/rastreio_dms.json",
          {"doexecutavel": _envio("2026-02-18 14:00:00")})
    config.BASE_DIR = proj
    config.IG_HISTORICO_RAIZES = [viz, _TMP / "nao_existe"]
    historico_ig.REVER_S = 0
    historico_ig._esquecer()
    return proj, viz


def teste_historico(proj, viz):
    print()
    print("[A1] outro nicho, outra versão do bot, o executável antigo")
    r = historico_ig.ja_abordado("clinica1")
    checar(r is not None and "clinicas" in r["origem"],
           f"DM do nicho clinicas vale para qualquer nicho: {r}")
    r = historico_ig.ja_abordado("sounatalia")
    checar(r is not None and r["data"] == "2026-03-13 15:52:15",
           f"DM do bot antigo (pasta vizinha) vale: {r}")
    checar(historico_ig.ja_abordado("doexecutavel") is not None,
           "e a do executável do bot antigo também")

    print()
    print("[A2] a PRIMEIRA vez é a que fica")
    r = historico_ig.ja_abordado("agencia1")
    checar(r is not None and r["data"] == "2026-02-01 08:00:00",
           f"recebeu em fevereiro (antigo) e em setembro (atual): {r}")

    print()
    print("[A3] do jeito que o @ chegar")
    for forma in ("@SOUNATALIA", " sounatalia/ ",
                  "https://www.instagram.com/sounatalia/?igsh=abc",
                  "instagram.com/SouNatalia"):
        checar(historico_ig.ja_abordado(forma) is not None, f"{forma!r}")

    print()
    print("[A4] rejeitado NÃO é abordado")
    # processados.txt mistura envio com rejeição (bio bloqueada, sem botão,
    # erro de tela). Conferidos na tela: quem estava só lá não tinha conversa.
    checar(historico_ig.ja_abordado("sorejeitado") is None,
           "quem está só no processados.txt continua livre")

    print()
    print("[A5] um arquivo quebrado não derruba o resto")
    checar(historico_ig.ja_abordado("clinica1") is not None,
           "com um rastreio ilegível na pasta, os outros continuam valendo")

    print()
    print("[A6] o registro de envios do banco também conta")
    with sqlite3.connect(os.environ["LEADFORGE_DB"]) as con:
        con.execute("CREATE TABLE IF NOT EXISTS envios_conta (id INTEGER PRIMARY "
                    "KEY AUTOINCREMENT, account_id INTEGER NOT NULL, canal TEXT "
                    "NOT NULL, destino TEXT DEFAULT '', lead_id INTEGER, sent_at "
                    "TEXT NOT NULL)")
        con.execute("INSERT INTO envios_conta(account_id,canal,destino,sent_at) "
                    "VALUES(1,'instagram','SoNoBanco','2026-09-10 11:00:00')")
        con.execute("INSERT INTO envios_conta(account_id,canal,destino,sent_at) "
                    "VALUES(1,'whatsapp','5582999999999','2026-09-10 11:00:00')")
    r = historico_ig.ja_abordado("sonobanco")
    checar(r is not None and r["data"] == "2026-09-10 11:00:00",
           f"DM registrada só no banco: {r}")
    checar(historico_ig.ja_abordado("5582999999999") is None,
           "e envio de WhatsApp não vira bloqueio de Instagram")

    print()
    print("[A7] o que o bot acaba de enviar vale NA HORA")
    historico_ig.REVER_S = 3600      # sem releitura: só o anotar() pode responder
    historico_ig.ja_abordado("qualquer")
    checar(historico_ig.ja_abordado("acabeidemandar") is None, "antes: livre")
    historico_ig.anotar("@AcabeiDeMandar", "2026-09-24 10:00:00", "perfis/Agencias")
    checar(historico_ig.ja_abordado("acabeidemandar") is not None,
           "depois do anotar(): bloqueado, sem esperar reler o arquivo")

    print()
    print("[A8] nicho novo que aparece com o bot ligado entra na releitura")
    historico_ig.REVER_S = 0
    _json(proj / "senders/dm_bot/perfis/NichoNovo/rastreio_dms.json",
          {"donichonovo": _envio("2026-09-24 11:00:00")})
    checar(historico_ig.ja_abordado("donichonovo") is not None,
           "o arquivo criado agora já é lido")

    print()
    print("[A9] SOMENTE LEITURA — o dminstagram está sob a REGRA ZERO")
    antes = {p: (p.stat().st_mtime_ns, p.read_bytes())
             for p in list(proj.rglob("*")) + list(viz.rglob("*")) if p.is_file()}
    for _ in range(3):
        historico_ig.resumo()
        historico_ig.ja_abordado("sounatalia")
    depois = {p: (p.stat().st_mtime_ns, p.read_bytes())
              for p in list(proj.rglob("*")) + list(viz.rglob("*")) if p.is_file()}
    checar(antes == depois, "nenhum arquivo lido foi criado, alterado ou tocado")
    fonte = (RAIZ / "historico_ig.py").read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    escreve = []
    for n in ast.walk(arvore):
        if isinstance(n, ast.Attribute) and n.attr in (
                "write_text", "write_bytes", "unlink", "rename", "replace",
                "mkdir", "rmdir", "touch", "remove"):
            escreve.append(n.attr)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open":
            escreve.append("open")
    checar(not escreve, f"e o código não tem chamada de escrita: {escreve or 'nenhuma'}")


# ════════════════════════════════════════════════════════════════════════════
#  B. A TELA — com páginas montadas no formato medido do Instagram
# ════════════════════════════════════════════════════════════════════════════

def _janela(usuario, miolo, extra_janela="", ident="alvo"):
    """A janelinha de chat como medida (só a estrutura)."""
    return f"""
    <div id="jan-{ident}" data-pagelet="IGDChatTabsRootContentOffMsys">
      <div><a href="/{usuario}/" aria-label="Open the profile page of {usuario}">
        <h2>Nome de {usuario}</h2></a></div>
      <div aria-label="Nome de {usuario}">
        <div data-pagelet="IGDMessagesList">
          <h2>Nome de {usuario}</h2><span>{usuario} · Instagram</span>
          <a href="/{usuario}/" role="link">Ver perfil</a>
          {miolo}
        </div>
        {extra_janela}
        <div role="textbox" contenteditable="true" data-caixa="{ident}"></div>
        <div role="button" data-botao="{ident}">Enviar</div>
      </div>
    </div>"""


def _pagina(*janelas, perfil="alvo"):
    # A página do perfil fica POR BAIXO da janelinha, com link para o próprio
    # perfil. Esse link não pode ser emprestado para a conversa de outra pessoa.
    return f"""<html><body>
      <main><header><a href="/{perfil}/">{perfil}</a></header>
        <div role="button" id="botao-perfil">Enviar mensagem</div></main>
      {''.join(janelas)}
      <script>window.cliques = [];
        document.addEventListener('click', e => window.cliques.push(
          e.target.getAttribute('data-botao') || e.target.id || '?'));</script>
    </body></html>"""


def _tela_cheia(usuario, miolo, cartao=True):
    """A conversa em tela cheia (/direct/t/...), como a extensão abre por ig.me."""
    card = (f'<h2>Nome de {usuario}</h2><span>{usuario} · Instagram</span>'
            f'<a href="/{usuario}/" role="link">Ver perfil</a>') if cartao else ""
    return f"""<html><body>
      <nav><a href="/minha_conta/">eu</a><a href="/explore/">explorar</a></nav>
      <main><div>
        <div role="navigation"><a href="/direct/t/1/">outra conversa</a></div>
        <div id="painel">
          <div><a href="/{usuario}/" aria-label="Open the profile page of {usuario}">Nome</a></div>
          <div>
            <div data-pagelet="IGDMessagesList">{card}{miolo}</div>
            <div role="textbox" contenteditable="true" data-caixa="{usuario}"></div>
          </div>
        </div>
      </div></main>
    </body></html>"""


_MSG = """<span dir="auto">09:16</span>
  <div role="article" aria-roledescription="message"><span>Olá, bom dia!</span></div>"""


def teste_tela():
    from playwright.async_api import async_playwright
    import conversa_ig as cv

    async def rodar():
        async with async_playwright() as p:
            nav = await p.chromium.launch(headless=True)
            page = await nav.new_page()

            async def veredito(html, u="alvo", **kw):
                await page.set_content(html)
                kw.setdefault("prazo", 4)
                kw.setdefault("estavel", 1.5)
                return await cv.conversa_com(page, u, **kw)

            print()
            print("[B0] PROVA DO DEFEITO: a checagem antiga dava 'Chat limpo' "
                  "numa conversa COM mensagem")
            await page.set_content(_pagina(_janela("alvo", _MSG)))
            txt = " ".join(await page.locator(
                'div[role="grid"], div[role="listbox"]').all_inner_texts())
            checar(txt == "", "a janelinha não tem grid nem listbox: o texto "
                              "lido vinha vazio, e vazio virava 'pode enviar'")

            print()
            print("[B1] conversa com mensagem → existe")
            v, m = await veredito(_pagina(_janela("alvo", _MSG)))
            checar(v == "existe", f"{v} ({m})")

            print()
            print("[B2] só o horário, sem o papel 'article' → existe")
            # se o Instagram renomear o papel da mensagem, o separador de
            # horário ainda denuncia a conversa
            v, m = await veredito(_pagina(_janela(
                "alvo", '<span dir="auto">13 de mar de 2026 15:52</span><p>Opa</p>')))
            checar(v == "existe", f"{v} ({m})")

            print()
            print("[B3] conversa vazia, da pessoa certa → vazia (e só depois de firmar)")
            t0 = time.monotonic()
            v, m = await veredito(_pagina(_janela("alvo", "")))
            dt = time.monotonic() - t0
            checar(v == "vazia", f"{v} ({m})")
            checar(dt >= 1.5, f"não decidiu na primeira leitura ({dt:.1f}s)")

            print()
            print("[B4] FALHA FECHADA: sem lista de mensagens → incerto, nunca vazia")
            v, m = await veredito(_pagina(
                '<div role="grid"></div><div role="textbox" contenteditable="true"></div>'))
            checar(v == "incerto", f"tela desconhecida: {v} ({m})")

            print()
            print("[B5] a conversa na tela é de OUTRA pessoa")
            # a página do perfil do alvo está por baixo, com link para ele — e
            # mesmo assim a janelinha de outra pessoa não vira a conversa dele
            v, m = await veredito(_pagina(_janela("outrapessoa", "", ident="o")))
            checar(v == "incerto", f"vazia, mas não é a dela: {v} ({m})")
            v, m = await veredito(_pagina(_janela("outrapessoa", _MSG, ident="o")))
            checar(v == "incerto", f"com mensagem, mas não é a dela: {v} ({m})")
            caixa = await cv.caixa_da_conversa(page, "alvo")
            checar(caixa is None, "e não há caixa de texto do alvo para digitar")

            print()
            print("[B6] ainda carregando → não libera")
            v, m = await veredito(_pagina(_janela(
                "alvo", "", extra_janela='<div role="progressbar"></div>')))
            checar(v == "incerto", f"{v} ({m})")

            print()
            print("[B7] a mensagem chega DEPOIS do cartão → existe")
            # o cartão do perfil desenha antes das mensagens; decidir na
            # primeira leitura repetiria o erro antigo por outro caminho
            tarde = _pagina(_janela("alvo", "")) .replace(
                "</body>", """<script>setTimeout(() => {
                  const l = document.querySelector('[data-pagelet="IGDMessagesList"]');
                  const a = document.createElement('div');
                  a.setAttribute('role', 'article'); a.textContent = 'Opa';
                  l.appendChild(a); }, 1200);</script></body>""")
            v, m = await veredito(tarde, estavel=3)
            checar(v == "existe", f"{v} ({m})")

            print()
            print("[B8] duas janelas abertas: digita e clica na da pessoa CERTA")
            await page.set_content(_pagina(_janela("outrapessoa", _MSG, ident="o"),
                                           _janela("alvo", "", ident="alvo")))
            v, m = await cv.conversa_com(page, "alvo", prazo=4, estavel=1.5)
            checar(v == "vazia", f"a do alvo está vazia: {v} ({m})")
            caixa = await cv.caixa_da_conversa(page, "alvo")
            qual = await caixa.get_attribute("data-caixa") if caixa else None
            checar(qual == "alvo", f"a caixa devolvida é a do alvo: {qual}")
            clicou = await cv.clicar_enviar(page, "alvo")
            cliques = await page.evaluate("window.cliques")
            checar(clicou and cliques == ["alvo"],
                   f"'Enviar' da conversa do alvo, nunca o 'Enviar mensagem' "
                   f"do perfil: {cliques}")

            print()
            print("[B9] parada pedida no meio da leitura")
            v, m = await veredito(_pagina(_janela("alvo", "")), parar=lambda: True)
            checar(v == "incerto", f"{v} ({m})")

            print()
            print("[B10] tela cheia (o caminho da extensão)")
            v, m = await veredito(_tela_cheia("alvo", ""))
            checar(v == "vazia", f"vazia, com o cartão: {v} ({m})")
            v, m = await veredito(_tela_cheia("alvo", _MSG))
            checar(v == "existe", f"com mensagem: {v} ({m})")
            v, m = await veredito(_tela_cheia("alvo", _MSG, cartao=False))
            checar(v == "existe", f"conversa longa, sem o cartão desenhado — o "
                                  f"cabeçalho identifica: {v} ({m})")
            v, m = await veredito(_tela_cheia("alvo", "", cartao=False))
            checar(v == "incerto", f"sem mensagem e sem cartão ainda: não decide: {v}")

            print()
            print("[B11] a página ANTERIOR ainda na tela (aba reaproveitada)")
            # A extensão usa a mesma aba de um alvo para o outro.
            v, m = await veredito(_tela_cheia("anterior", _MSG))
            checar(v == "incerto", f"conversa de @anterior, pergunta sobre @alvo: {v} ({m})")
            caixa = await cv.caixa_da_conversa(page, "alvo")
            checar(caixa is None, "e a caixa de @anterior nunca é devolvida para @alvo")

            await nav.close()

    asyncio.run(rodar())


# ════════════════════════════════════════════════════════════════════════════
#  C. O BOT — a ordem das travas no caminho do envio
# ════════════════════════════════════════════════════════════════════════════

def _funcoes(caminho: Path):
    fonte = caminho.read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    return fonte, {n.name: n for n in ast.walk(arvore)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def teste_bot():
    bot = RAIZ / "senders" / "dm_bot" / "Servidor_N8N.py"
    fonte, fns = _funcoes(bot)
    corpo = ast.unparse(fns["analisar_perfil"])

    print()
    print("[C1] o seletor que nunca achava nada saiu")
    checar('div[role="grid"], div[role="listbox"]' not in corpo,
           "analisar_perfil não lê mais grid/listbox")

    print()
    print("[C2] a ordem: histórico → abrir → tela → tela de novo → digitar")
    pos = lambda s: corpo.find(s)
    ordem = [pos("_ja_abordado(username)"), pos("context.new_page()"),
             pos("_conversa.conversa_com("),
             corpo.find("_conversa.conversa_com(", pos("_conversa.conversa_com(") + 1),
             pos("caixa_da_conversa("), pos("insert_text(")]
    checar(all(p >= 0 for p in ordem) and ordem == sorted(ordem),
           f"posições em ordem: {ordem}")

    print()
    print("[C3] só 'vazia' libera; o resto não digita")
    checar(corpo.count("if veredito != 'vazia':") == 2,
           "as duas leituras recusam tudo o que não for 'vazia'")
    checar(corpo.count("_nao_confirmado(") >= 3,
           "e o 'incerto' cai no caminho que NÃO envia")

    print()
    print("[C4] do Enter em diante, a pessoa já está no histórico")
    i_enter = corpo.find("press('Enter')")
    i_sent = corpo.find("registrar_acao(username, 'SENT', conta)")
    i_botao = corpo.find("clicar_enviar(")
    checar(0 <= i_enter < i_sent < i_botao,
           "registra o envio antes de conferir ou clicar em mais nada")
    checar("_historico.anotar(" in ast.unparse(fns["registrar_acao"]),
           "e registrar_acao(SENT) anota no histórico na hora")

    print()
    print("[C5] cinco leituras falhas seguidas param a conta")
    ns = {"INCERTOS_SEGUIDOS": {}, "LIMITE_INCERTOS": 5, "CONTA_ATUAL": "",
          "logger": __import__("logging").getLogger("teste")}
    for nome in ("_nao_confirmado", "_leitura_ok"):
        exec(compile(ast.Module(body=[fns[nome]], type_ignores=[]), str(bot), "exec"), ns)
    r = [ns["_nao_confirmado"]("x", "conta1", "teste") for _ in range(5)]
    checar(r == ["INCERTO"] * 4 + ["STOP"], f"{r}")
    ns["_leitura_ok"]("conta1")
    checar(ns["_nao_confirmado"]("x", "conta1", "t") == "INCERTO",
           "e uma leitura boa zera a contagem")
    checar(ns["_nao_confirmado"]("x", "conta2", "t") == "INCERTO",
           "a contagem é por conta")


if __name__ == "__main__":
    print("=" * 70)
    print("DM REPETIDA — histórico único e checagem da conversa na tela")
    print("=" * 70)
    try:
        teste_caso_real()
        proj, viz = montar_pastas()
        teste_historico(proj, viz)
        teste_tela()
        teste_bot()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    print()
    print("=" * 70)
    if falhas:
        print(f"RESULTADO: {len(falhas)} falha(s)")
        for f in falhas:
            print("  •", f)
        sys.exit(1)
    print("RESULTADO: tudo passou")
