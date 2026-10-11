"""scrap_live.py — Coleta AO VIVO no Google Maps com transmissão em tempo real."""

import asyncio
import base64
import re
import sys
from urllib.parse import quote_plus

import requests
from playwright.async_api import async_playwright

import config  # noqa (reconfigura UTF-8)
import db
import dashboard_db as ddb
import ingest
import scorer
import web_enrich
import whatsapp_check

DASH = "http://localhost:5020"

# JS que lê o card inteiro do resultado (texto + site + aria-label da nota)
_JS_CARD = """el=>{
  let card=el;
  for(let i=0;i<8;i++){let c=card.parentElement;if(!c)break;card=c;
    if(card.querySelectorAll('a[href*=\"/place/\"]').length===1 && (card.innerText||'').length>25)break;}
  let web='';
  let wl=card.querySelector('a[data-value=\"Website\"]')||card.querySelector('a[aria-label*=\"site\"]');
  if(!wl){let as=[...card.querySelectorAll('a[href^=\"http\"]')].filter(x=>!/google\\.|\\/maps\\//.test(x.href));if(as.length)wl=as[0];}
  if(wl)web=wl.href;
  let img=card.querySelector('span[role=\"img\"][aria-label]');
  let rat= img?(img.getAttribute('aria-label')||''):'';
  // nº de avaliações: número entre parênteses ao lado da estrela (evita o DDD do telefone)
  let rev='';
  // aceita a forma abreviada "(1,2 mil)": com [\\d.] apenas, o parêntese do topo
  // da lista nunca casava e a clínica com MAIS avaliações ficava com zero
  if(img){let par=img.parentElement; for(let i=0;i<3&&par;i++){let mm=(par.textContent||'').match(/\\(([\\d.,]+\\s*(?:mil|k|m)?)\\)/i);if(mm){rev=mm[1];break;}par=par.parentElement;}}
  return {text:card.innerText||'', web:web, rat:rat, rev:rev};
}"""


async def extrair_card(a, base_url, pos):
    """
    Extrai TODOS os campos do card do Maps: nome, nota, avaliações, telefone, site,
    categoria e endereço.
    """
    info = {"nome": "", "maps_url": base_url, "avaliacao": "", "total_avaliacoes": "",
            "telefone": "", "website": "", "categoria": "", "endereco": "", "posicao": pos,
            "patrocinado": 0}
    try:
        info["nome"] = (await a.get_attribute("aria-label")) or ""
    except Exception:
        pass
    try:
        data = await a.evaluate(_JS_CARD)
    except Exception:
        data = {}
    text = (data or {}).get("text", "")
    rat = (data or {}).get("rat", "")
    web = (data or {}).get("web", "")
    rev = (data or {}).get("rev", "")

    # anúncio? (aparece em 1º artificialmente — não conta como posição orgânica)
    if re.search(r"patrocinad|an[úu]ncio|sponsored", text, re.I):
        info["patrocinado"] = 1

    # nota
    m = re.search(r"(\d[,.]\d)", rat) or re.search(r"(\d[,.]\d)\s*\(", text)
    if m:
        info["avaliacao"] = m.group(1).replace(".", ",")
    # nº de avaliações: elemento ao lado da estrela > aria-label > parênteses após a nota
    if rev:
        info["total_avaliacoes"] = rev
    else:
        mv = (re.search(r"([\d.,]+\s*(?:mil|k|m)?)\s*(?:avalia|coment|review)", rat, re.I)
              or re.search(r"\d[,.]\d\s*\(\s*([\d.,]+\s*(?:mil|k|m)?)\s*\)", text, re.I))
        if mv:
            info["total_avaliacoes"] = mv.group(1)
    # website
    if web:
        info["website"] = web.split("?")[0]
    # telefone (validado contra placeholders/DDD)
    for cand in web_enrich._RE_TEL.findall(text):
        if web_enrich._telefone_valido(cand):
            info["telefone"] = whatsapp_check.normalizar(cand)["formatado"]
            break
    # categoria + endereço (linha "Categoria · Endereço", ignorando horários)
    for l in [x.strip() for x in text.split("\n") if x.strip()][1:]:
        if re.match(r"^(Aberto|Fechado|Fecha|Abre|Temporariamente|24)", l, re.I):
            continue
        if "·" in l or "⋅" in l:
            parts = re.split(r"[·⋅]", l)
            info["categoria"] = parts[0].strip()
            if len(parts) > 1 and re.search(r"\d|R\.|Av\.|Rua|Alameda|Trav", parts[-1]):
                info["endereco"] = parts[-1].strip()
            break
    return info


def _post(sid, shot, businesses, done=False, msg=""):
    try:
        requests.post(f"{DASH}/api/scrap/progress", timeout=5, json={
            "sid": sid, "screenshot": shot, "businesses": businesses,
            "count": len(businesses), "done": done, "msg": msg})
    except Exception:
        pass


async def _shot(page) -> str:
    try:
        png = await page.screenshot(type="jpeg", quality=55)
        return "data:image/jpeg;base64," + base64.b64encode(png).decode()
    except Exception:
        return ""


async def abrir_busca(page, url: str) -> bool:
    """Abre uma busca do Maps e espera a lista aparecer. False = não veio lista."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return False
    await asyncio.sleep(2)

    # consentimento (necessário para ver resultados)
    for txt in ["Aceitar tudo", "Aceitar", "Concordo", "Accept all", "I agree"]:
        try:
            b = page.locator(f'button:has-text("{txt}")').first
            if await b.count() and await b.is_visible():
                await b.click()
                await asyncio.sleep(1.5)
                break
        except Exception:
            pass

    try:
        await page.locator('div[role="feed"]').wait_for(state="visible", timeout=12000)
    except Exception:
        return False
    return True


async def rolar_feed(page, coletados: dict, aceitar=None, ao_progredir=None,
                     rotulo: str = "", limite_rolagens: int = 30,
                     ao_gravar=None, deve_parar=None) -> dict:
    """Rola a lista até parar de aparecer gente nova, gravando em `coletados`."""
    feed = page.locator('div[role="feed"]')
    vistos, aceitos, parado = set(), 0, 0
    interrompida = False

    for i in range(limite_rolagens):
        links = await page.locator('div[role="feed"] a[href*="/maps/place/"]').all()
        novos = 0
        desta_rolagem = []
        for a in links:
            try:
                href = await a.get_attribute("href")
            except Exception:
                continue
            if not href:
                continue
            base_url = href.split("?")[0]
            if base_url in vistos:
                continue
            vistos.add(base_url)
            novos += 1
            if base_url in coletados:
                continue
            info = await extrair_card(a, base_url, len(coletados) + 1)
            if not info["nome"]:
                continue
            if aceitar and not aceitar(info):
                continue
            coletados[base_url] = info
            desta_rolagem.append(info)
            aceitos += 1

        # grava ANTES de avisar a tela: se cair entre uma coisa e outra, o
        # prejuízo é um número desatualizado no painel, não lead perdido
        if ao_gravar and desta_rolagem:
            await ao_gravar(desta_rolagem)

        if ao_progredir:
            onde = f"{rotulo} · " if rotulo else ""
            await ao_progredir(f"{onde}Coletando... {len(coletados)} encontradas "
                               f"(rolagem {i + 1})")

        if deve_parar and deve_parar():
            interrompida = True
            break

        parado = parado + 1 if novos == 0 else 0
        if parado >= 4:
            break
        try:
            await feed.evaluate("el=>{el.scrollTop += el.clientHeight*0.9}")
        except Exception:
            pass
        await asyncio.sleep(1.8)

    return {"brutos": len(vistos), "aceitos": aceitos, "interrompida": interrompida}


def _ja_coletados(sid: int) -> dict:
    """O que esta busca já trouxe em execuções anteriores."""
    try:
        with db.conn() as con:
            linhas = con.execute(
                "SELECT maps_url, nome FROM leads WHERE scrap_id=?", (int(sid),))
            return {r["maps_url"]: {"nome": r["nome"], "maps_url": r["maps_url"],
                                    "categoria": "", "telefone": "", "website": "",
                                    "endereco": "", "avaliacao": "",
                                    "total_avaliacoes": "", "retomado": True}
                    for r in linhas}
    except Exception:
        return {}


def _ultima_posicao(sid: int) -> int:
    """A maior colocação já gravada nesta busca."""
    try:
        with db.conn() as con:
            r = con.execute("SELECT COALESCE(MAX(posicao), 0) FROM leads "
                            "WHERE scrap_id=?", (int(sid),)).fetchone()
        return int(r[0] or 0)
    except Exception:
        return 0


async def coletar(query: str, sid: int, uf: str = "", orcamento: int = 30,
                  retomar: bool = False):
    """Uma coleta. Com `uf`, vira varredura do estado inteiro em vez de uma busca."""
    coletados = _ja_coletados(sid) if retomar else {}
    ja_tinha = len(coletados)
    relatorio = {}
    # os contadores atravessam as gravações parciais: `persistir` é chamado
    # muitas vezes durante uma coleta, e a posição orgânica de um lead não pode
    # recomeçar do 1 a cada rolagem
    gravacao = {"novos": 0, "ja_em_quadro": 0, "encontrados": 0,
                # a colocação continua de onde parou: um processo novo com o
                # contador zerado daria "1º colocado" a quem já é o 9º
                "organica": _ultima_posicao(sid) if retomar else 0}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--lang=pt-BR,pt",
                  "--no-sandbox"])
        ctx = await browser.new_context(
            locale="pt-BR", viewport={"width": 900, "height": 680},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"))
        page = await ctx.new_page()

        async def progresso(msg):
            _post(sid, await _shot(page), list(coletados.values()), msg=msg)

        async def gravar(novos):
            persistir(query, novos, sid, uf=uf, acumulado=gravacao)

        async def checkpoint(estado):
            ddb.salvar_progresso(sid, estado)

        def parar_pedido():
            return ddb.parada_pedida(sid)

        if uf:
            import varredura
            estado = varredura.nome_do_estado(uf)
            previsao = varredura.plano(uf, orcamento)
            ponto = ddb.ler_progresso(sid) if retomar else None
            await progresso(
                f"Retomando a varredura de {estado} de onde parou "
                f"({ja_tinha} já coletados)..." if ponto else
                f"Varredura de {estado}: até {orcamento} áreas do mapa, "
                f"cerca de {previsao.get('minutos_estimados', '?')} min.")
            relatorio = await varredura.varrer(
                page, query, uf, coletados, abrir_busca, rolar_feed,
                orcamento=orcamento, ao_progredir=progresso,
                retomar=ponto, ao_checkpoint=checkpoint, deve_parar=parar_pedido,
                ao_gravar=gravar)
        else:
            await progresso(f"Abrindo o Google Maps para: {query}")
            alvo = f"https://www.google.com/maps/search/{quote_plus(query)}"
            if not await abrir_busca(page, alvo):
                _post(sid, await _shot(page), [], done=True,
                      msg="Sem resultados (ou bloqueado pelo Google neste ambiente).")
                await browser.close()
                return {"leads": [], "interrompida": False, "relatorio": {},
                        "gravacao": gravacao}
            leitura = await rolar_feed(page, coletados, ao_progredir=progresso,
                                       ao_gravar=gravar, deve_parar=parar_pedido)
            relatorio = {"interrompida": leitura.get("interrompida")}

        interrompida = bool(relatorio.get("interrompida"))
        # A varredura devolve o ponto exato; a busca simples não tem ponto —
        # ela é uma consulta só, e retomar seria refazê-la.
        ddb.salvar_progresso(sid, relatorio.get("estado") if interrompida else None)

        _post(sid, await _shot(page), list(coletados.values()), done=True,
              msg=resumo_final(len(coletados), gravacao, relatorio, ja_tinha))
        await browser.close()

    return {"leads": list(coletados.values()), "interrompida": interrompida,
            "relatorio": relatorio, "gravacao": gravacao}


def resumo_final(encontrados: int, gravacao: dict, varredura_rel: dict = None,
                 ja_tinha: int = 0) -> str:
    """A frase que fecha a coleta."""
    rel = varredura_rel or {}
    abertura = "Pausada" if rel.get("interrompida") else "Concluído"
    partes = [f"{abertura}: {encontrados} encontradas"]
    if ja_tinha:
        partes.append(f"{encontrados - ja_tinha} nesta rodada")
    partes.append(f"{gravacao.get('novos', 0)} adicionadas ao Kanban")
    if gravacao.get("ja_em_quadro"):
        partes.append(f"{gravacao['ja_em_quadro']} já estavam em quadros (não repetidas)")
    if rel.get("consultas"):
        partes.append(f"{rel['consultas']} áreas varridas")
    if rel.get("fora_do_estado"):
        partes.append(f"{rel['fora_do_estado']} fora do estado (descartadas)")
    if rel.get("interrompida") and rel.get("restaram"):
        partes.append(f"faltam {rel['restaram']} áreas — o ▶ continua daqui")
    elif rel.get("restaram"):
        partes.append(f"restaram {rel['restaram']} áreas — repita com orçamento maior")
    return " · ".join(partes)


def persistir(query: str, businesses, sid: int = None, uf: str = "",
              acumulado: dict = None) -> dict:
    """Grava a coleta como leads."""
    ddb.init_dashboard()
    conta = acumulado if acumulado is not None else {
        "novos": 0, "ja_em_quadro": 0, "encontrados": 0, "organica": 0}

    for b in businesses:
        conta["encontrados"] = conta.get("encontrados", 0) + 1
        # veio do banco na retomada, não do Maps: já está gravado
        if b.get("retomado"):
            continue
        with db.conn() as con:
            anterior = con.execute("SELECT quadro_id FROM leads WHERE maps_url=?",
                                   (b["maps_url"],)).fetchone()
        if anterior and (anterior["quadro_id"] or 0) > 0:
            conta["ja_em_quadro"] = conta.get("ja_em_quadro", 0) + 1
            continue

        patro = 1 if b.get("patrocinado") else 0
        if patro:
            posicao = None  # anúncio não tem posição orgânica
        else:
            conta["organica"] = conta.get("organica", 0) + 1
            posicao = conta["organica"]
        categoria = b.get("categoria") or query
        website = (b.get("website") or "").strip()
        lead = {
            "maps_url": b["maps_url"], "nome": b["nome"], "categoria": categoria,
            "nicho": ingest.detectar_nicho(categoria),
            "avaliacao_num": ingest.parse_nota(b.get("avaliacao")),
            "total_avaliacoes_num": ingest.parse_total(b.get("total_avaliacoes")),
            "tem_site": 1 if website else 0, "tem_instagram": 0, "instagram": "",
            "telefone": (b.get("telefone") or "").strip(), "website": website,
            "endereco": (b.get("endereco") or "").strip(), "posicao": posicao,
        }
        lead_id = db.upsert_lead(lead)
        try:
            full = db.get_lead(lead_id)
            score, bd = scorer.score_lead(full)
            db.set_score(lead_id, score, bd)
        except Exception:
            pass
        with db.conn() as con:
            con.execute("UPDATE leads SET origem='scrap-live', status='qualified', "
                        "patrocinado=?, kanban=COALESCE(NULLIF(kanban,''),'lista') "
                        "WHERE id=?", (patro, lead_id))
            if not anterior:
                con.execute("UPDATE leads SET quadro_id=0 WHERE id=?", (lead_id,))
            # a busca que encontrou este lead fica registrada: e o que permite a
            # fila de buscas mostrar o que cada uma rendeu e despachar depois,
            # em vez de tudo virar uma pilha so
            if sid is not None:
                con.execute("UPDATE leads SET scrap_id=? WHERE id=?", (int(sid), lead_id))
            # a varredura sabe o estado por construcao: o lugar so entrou porque
            # a coordenada dele caiu dentro do retangulo. A CIDADE continua a
            # cargo do cidade.py -- o retangulo prova o estado, nao a cidade.
            if uf:
                con.execute("UPDATE leads SET uf=COALESCE(NULLIF(uf,''),?) WHERE id=?",
                            (uf.upper(), lead_id))
        conta["novos"] = conta.get("novos", 0) + 1
    return conta


def completar_avaliacoes(verbose=True) -> int:
    """Preenche o nº de avaliações de quem ficou sem, abrindo a página do lugar."""
    try:
        import avaliacoes_enrich as ae
    except Exception as e:
        if verbose:
            print(f"[avaliacoes] módulo indisponível: {e}")
        return 0
    with db.conn() as con:
        pendentes = [dict(r) for r in con.execute(
            "SELECT id,nome,maps_url,cidade,total_avaliacoes_num FROM leads "
            "WHERE origem='scrap-live' AND COALESCE(total_avaliacoes_num,0)=0 "
            "AND maps_url LIKE 'http%'")]
    if not pendentes:
        return 0
    if verbose:
        print(f"\nlendo o nº de avaliações de {len(pendentes)} lead(s)...")
    try:
        achados, _falhas = asyncio.run(ae._rodar(pendentes, verbose=verbose))
        return ae.gravar(achados)
    except Exception as e:
        if verbose:
            print(f"[avaliacoes] falhou: {e}")
        return 0


def _opcao(nome: str, padrao: str = "") -> str:
    """Lê `--nome valor` da linha de comando."""
    if nome in sys.argv:
        i = sys.argv.index(nome)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return padrao


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("uso: python scrap_live.py <sid> \"<termo> <localização>\" "
              "[--estado UF] [--orcamento N]")
        sys.exit(1)
    sid = int(sys.argv[1])
    query = sys.argv[2]
    # `--estado` troca a busca única pela varredura do estado. O termo vai sem a
    # localização junto: quem diz onde procurar é o enquadramento do mapa, e
    # repetir "Alagoas" no texto faria o Google recentralizar tudo no mesmo ponto.
    uf = (_opcao("--estado") or "").upper()
    orcamento = int(_opcao("--orcamento", "30") or 30)
    retomar = "--retomar" in sys.argv
    ddb.set_scrap_status(sid, "rodando")
    saida = asyncio.run(coletar(query, sid, uf=uf, orcamento=orcamento,
                                retomar=retomar))
    resultado = saida["leads"]
    # `pausado` e um fim legitimo, nao um erro: e o estado que o botao continuar
    # procura para saber que ha de onde retomar
    ddb.set_scrap_status(sid, "pausado" if saida["interrompida"] else "concluido",
                         len(resultado))
    print(f"Coleta {'pausada' if saida['interrompida'] else 'finalizada'}: "
          f"{len(resultado)} clínicas.")
    if "--sem-avaliacoes" not in sys.argv:
        n = completar_avaliacoes()
        print(f"Avaliações preenchidas: {n}")
