"""avaliacoes_enrich.py — Lê o nº de avaliações na PÁGINA DO LUGAR."""

import argparse
import asyncio
import re
import sys
from typing import Optional
from urllib.parse import quote_plus

import db
import ingest

PAUSA_ENTRE_LEADS = 2.0     # segundos; é a conta do Google, não a nossa
TIMEOUT_PAGINA = 40000
ESPERA_ELEMENTO = 12000

_RE_ARIA = re.compile(r"^\s*([\d.,]+\s*(?:mil|k|m)?)\s+avalia", re.I)
# cabeçalho "4,8 (20)" — o mesmo número, quando o aria-label não aparece
_RE_CABECALHO = re.compile(r"\d[,.]\d\s*[^\d(]{0,8}\(\s*([\d.,]+\s*(?:mil|k|m)?)\s*\)", re.I)
_RE_TEXTO = re.compile(r"([\d.,]+\s*(?:mil|k)?)\s*(?:avalia[çc][õo]es|coment[áa]rios)", re.I)


async def _ler_da_pagina(page) -> list:
    """Todos os números que a página oferece, por três caminhos independentes."""
    valores = []
    try:
        await page.wait_for_selector("[aria-label*='avalia' i]", timeout=ESPERA_ELEMENTO)
    except Exception:
        pass

    try:
        arias = await page.evaluate(
            """() => [...document.querySelectorAll('[aria-label]')]
                    .map(x => x.getAttribute('aria-label'))
                    .filter(t => t && /avalia/i.test(t))""")
    except Exception:
        arias = []
    for a in arias or []:
        m = _RE_ARIA.match(a)
        if m:
            valores.append(ingest.parse_total(m.group(1)))
            break

    try:
        txt = await page.evaluate("() => (document.body.innerText||'').slice(0,3000)")
    except Exception:
        txt = ""
    for rx in (_RE_CABECALHO, _RE_TEXTO):
        m = rx.search(txt or "")
        if m:
            valores.append(ingest.parse_total(m.group(1)))
    return [v for v in valores if v and v > 0]


def _urls(lead) -> list:
    """As vias, da mais direta para a mais indireta."""
    url = (lead.get("maps_url") or "").strip()
    vias = []
    if url:
        vias.append(("pagina do lugar", url))
        vias.append(("aba de avaliacoes", url + "!9m1!1b1"))
    nome = (lead.get("nome") or "")[:60]
    if nome:
        cidade = (lead.get("cidade") or "").split(",")[0]
        vias.append(("busca pelo nome",
                     "https://www.google.com/maps/search/"
                     + quote_plus(f"{nome} {cidade}".strip())))
    return vias


async def _ler_contagem(page, lead) -> Optional[int]:
    """O nº de avaliações, ou None quando não dá para ler com confiança."""
    for _rotulo, u in _urls(lead):
        try:
            await page.goto(u, wait_until="domcontentloaded", timeout=TIMEOUT_PAGINA)
        except Exception:
            continue
        valores = await _ler_da_pagina(page)
        if not valores:
            continue
        if len(set(valores)) > 1:
            continue                      # caminhos discordaram: não arrisca
        return valores[0]
    return None


async def _rodar(leads, verbose=True):
    from playwright.async_api import async_playwright

    achados, falhas = {}, []
    async with async_playwright() as p:
        b = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--lang=pt-BR,pt",
                  "--no-sandbox"])
        ctx = await b.new_context(
            locale="pt-BR", viewport={"width": 1100, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"))
        page = await ctx.new_page()
        for i, l in enumerate(leads, 1):
            n = await _ler_contagem(page, l)
            if n:
                achados[l["id"]] = n
                if verbose:
                    antes = l.get("total_avaliacoes_num") or 0
                    seta = "=" if antes == n else "->"
                    print(f"  [{i}/{len(leads)}] {l['id']:>4} {l['nome'][:38]:40} "
                          f"{antes or '—'} {seta} {n}", flush=True)
            else:
                falhas.append(l["id"])
                if verbose:
                    print(f"  [{i}/{len(leads)}] {l['id']:>4} {l['nome'][:38]:40} "
                          f"não deu para ler", flush=True)
            await page.wait_for_timeout(int(PAUSA_ENTRE_LEADS * 1000))
        await b.close()
    return achados, falhas


def gravar(achados: dict) -> int:
    """Grava só o que foi lido. Zero e None não encostam no banco."""
    n = 0
    with db.conn() as con:
        for lid, valor in achados.items():
            if not valor or valor <= 0:
                continue
            con.execute("UPDATE leads SET total_avaliacoes_num=?, "
                        "updated_at=datetime('now','localtime') WHERE id=?", (valor, lid))
            n += 1
    return n


def escolher(args) -> list:
    with db.conn() as con:
        if args.leads:
            ids = [int(i) for i in args.leads.split(",") if i.strip().isdigit()]
            q = ("SELECT id,nome,maps_url,cidade,total_avaliacoes_num FROM leads "
                 f"WHERE id IN ({','.join('?' * len(ids))})")
            rows = con.execute(q, ids).fetchall()
        elif args.todos:
            rows = con.execute(
                "SELECT id,nome,maps_url,cidade,total_avaliacoes_num FROM leads "
                "WHERE origem='scrap-live' ORDER BY posicao").fetchall()
        else:
            rows = con.execute(
                "SELECT id,nome,maps_url,cidade,total_avaliacoes_num FROM leads "
                "WHERE origem='scrap-live' AND COALESCE(total_avaliacoes_num,0)=0 "
                "ORDER BY posicao").fetchall()
    return [dict(r) for r in rows if (r["maps_url"] or "").startswith("http")]


def main():
    ap = argparse.ArgumentParser(description="Nº de avaliações pela página do lugar")
    ap.add_argument("--faltantes", action="store_true", help="só quem está sem (padrão)")
    ap.add_argument("--todos", action="store_true", help="reconfere a base inteira")
    ap.add_argument("--leads", default="", help="ids separados por vírgula")
    ap.add_argument("--seco", action="store_true", help="lê e mostra, não grava")
    args = ap.parse_args()

    leads = escolher(args)
    if not leads:
        print("nada a fazer.")
        return 0
    print(f"lendo a página do lugar de {len(leads)} lead(s)...\n")
    achados, falhas = asyncio.run(_rodar(leads))

    print()
    print(f"lidos com sucesso : {len(achados)}")
    print(f"sem leitura       : {len(falhas)}" + (f"  {falhas[:12]}" if falhas else ""))
    if args.seco:
        print("\n--seco: nada foi gravado.")
        return 0
    print(f"gravados          : {gravar(achados)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
