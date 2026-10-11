"""coleta_profunda.py — O BOT coleta TUDO sobre a clínica, antes de qualquer IA."""

import asyncio
import json
import re
import sys
from urllib.parse import quote_plus, urlparse

import config  # noqa (UTF-8)
import db
import pagespeed
import web_enrich

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

DIRETORIOS = {
    "doctoralia": "Doctoralia", "boaconsulta": "BoaConsulta", "guiaodonto": "Guia Odonto",
    "telelistas": "Telelistas", "apontador": "Apontador", "solutudo": "Solutudo",
    "encontraodonto": "Encontra Odonto", "clinicorp": "Clinicorp", "ondeir": "Onde Ir",
    "hotfrog": "Hotfrog", "cylex": "Cylex", "econodata": "Econodata", "cnpj": "consulta CNPJ",
}
REDES = {"instagram.com": "Instagram", "facebook.com": "Facebook", "linkedin.com": "LinkedIn",
         "youtube.com": "YouTube", "tiktok.com": "TikTok", "twitter.com": "X/Twitter"}

# DDD -> UF, usado para não confundir a clínica com homônimas de outro estado
DDD_UF = {
    "11": "SP", "12": "SP", "13": "SP", "14": "SP", "15": "SP", "16": "SP", "17": "SP",
    "18": "SP", "19": "SP", "21": "RJ", "22": "RJ", "24": "RJ", "27": "ES", "28": "ES",
    "31": "MG", "32": "MG", "33": "MG", "34": "MG", "35": "MG", "37": "MG", "38": "MG",
    "41": "PR", "42": "PR", "43": "PR", "44": "PR", "45": "PR", "46": "PR",
    "47": "SC", "48": "SC", "49": "SC", "51": "RS", "53": "RS", "54": "RS", "55": "RS",
    "61": "DF", "62": "GO", "64": "GO", "63": "TO", "65": "MT", "66": "MT", "67": "MS",
    "68": "AC", "69": "RO", "71": "BA", "73": "BA", "74": "BA", "75": "BA", "77": "BA",
    "79": "SE", "81": "PE", "87": "PE", "82": "AL", "83": "PB", "84": "RN", "85": "CE",
    "88": "CE", "86": "PI", "89": "PI", "91": "PA", "93": "PA", "94": "PA", "92": "AM",
    "97": "AM", "95": "RR", "96": "AP", "98": "MA", "99": "MA",
}


# ── 1. Busca no Google pelo nome da clínica ──────────────────────────────────

def _buscar_web(termo: str, ddd_ref: str = "") -> dict:
    """Pesquisa o nome da clínica na web e classifica o que aparece."""
    import urllib.parse
    import urllib.request

    achados = {"ok": False, "termo": termo, "fonte": "", "resultados": [],
               "tem_site_oficial": False, "redes": [], "diretorios": [],
               "total_resultados": 0, "erro": ""}

    fontes = [
        ("DuckDuckGo", f"https://html.duckduckgo.com/html/?q={quote_plus(termo)}",
         r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'),
        ("Bing", f"https://www.bing.com/search?q={quote_plus(termo)}&setlang=pt-BR",
         r'<h2><a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>'),
    ]
    html, usada = "", ""
    for nome, url, _rx in fontes:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _UA, "Accept-Language": "pt-BR,pt;q=0.9"})
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read().decode("utf-8", errors="ignore")
            if len(html) > 2000:
                usada = nome
                rx = _rx
                break
        except Exception as e:
            achados["erro"] = f"{nome}: {e}"
    if not usada:
        return achados

    links = []
    for m in re.finditer(rx, html, re.I | re.S):
        href, titulo = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        # DuckDuckGo embrulha a URL real em /l/?uddg=
        if "uddg=" in href:
            href = urllib.parse.unquote(href.split("uddg=")[-1].split("&")[0])
        if href.startswith("http"):
            links.append({"url": href, "titulo": re.sub(r"\s+", " ", titulo).strip()[:90]})
    achados["fonte"] = usada

    vistos, lista = set(), []
    for r in links:
        dom = urlparse(r["url"]).netloc.replace("www.", "").lower()
        if dom in vistos:
            continue
        vistos.add(dom)
        tipo = "outro"
        for k, nome in REDES.items():
            if k in dom:
                tipo = f"rede:{nome}"
                if nome not in achados["redes"]:
                    achados["redes"].append(nome)
        for k, nome in DIRETORIOS.items():
            if k in dom:
                tipo = f"diretorio:{nome}"
                if nome not in achados["diretorios"]:
                    achados["diretorios"].append(nome)
        lista.append({"dominio": dom, "titulo": r["titulo"], "tipo": tipo})

    # marca quais resultados realmente falam DESTA clínica.
    # CUIDADO com homônimas de outra cidade/estado: se o resultado traz um DDD
    # diferente do DDD da clínica, é OUTRA empresa com nome parecido.
    _palavras = {w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ]{4,}", termo)}
    for r in lista:
        alvo = f'{r["titulo"]} {r["dominio"]}'.lower()
        nome_bate = sum(1 for p in _palavras if p in alvo) >= 2
        conflito = False
        if ddd_ref:
            ddds = set(re.findall(r"\((\d{2})\)", r["titulo"]))
            if ddds and ddd_ref not in ddds:
                conflito = True          # telefone de outro estado
        r["e_desta_clinica"] = bool(nome_bate and not conflito)
        if conflito:
            r["alerta"] = f"DDD diferente do da clínica ({ddd_ref}), provavelmente homônima"
    achados["resultados"] = lista[:12]
    achados["total_resultados"] = len(lista)

    # ── extrai achados úteis dos resultados (dados que o Maps não tem) ──
    texto_res = " ".join(f'{x["dominio"]} {x["titulo"]}' for x in lista) + " " + html[:120000]
    # CNPJ
    m = re.search(r"\b(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})\b", texto_res)
    if m:
        achados["cnpj_encontrado"] = m.group(1)
    # @ do Instagram
    for r in lista:
        if "instagram.com" in r["dominio"]:
            mm = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", html)
            if mm and mm.group(1).lower() not in ("p", "reel", "explore", "accounts"):
                achados["instagram_encontrado"] = "@" + mm.group(1)
            break
    # cidade/UF: só aceita se aparecer no TÍTULO de um resultado que também cita a
    # clínica, e se a mesma cidade se repetir. Evita pegar a cidade de outra empresa
    # que apareceu na mesma página de busca.
    from collections import Counter
    palavras_nome = {w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ]{4,}", termo)}
    rx_cidade = (r"\b([A-ZÁÂÃÉÊÍÓÔÕÚÇ][a-zà-ÿ]+(?:\s[A-ZÁÂÃÉÊÍÓÔÕÚÇ]?[a-zà-ÿ]+)?)"
                 r"\s*[/-]\s*(AL|SP|RJ|MG|BA|PE|CE|PR|RS|SC|GO|PA|MA|PB|RN|PI|SE|AM|MT|MS|DF|ES|RO|AC|AP|RR|TO)\b")
    candidatas = []
    for r in lista:
        t = r["titulo"]
        # o título precisa citar a clínica para a cidade dele valer
        if not any(p in t.lower() for p in palavras_nome):
            continue
        mc = re.search(rx_cidade, t)
        if mc:
            candidatas.append(f"{mc.group(1)}/{mc.group(2)}")
    if candidatas:
        cidade, vezes = Counter(candidatas).most_common(1)[0]
        # só entra no dossiê se repetir em 2+ resultados. Dado não confiável é
        # REMOVIDO, não sinalizado: se ele existir, a IA vai acabar usando.
        if vezes >= 2:
            achados["cidade_uf_encontrada"] = cidade
            achados["cidade_confirmada"] = True
    achados["tem_site_oficial"] = any(r["tipo"] == "outro" and
                                      not re.search(r"cadastro|cnpj|invest|quero|guia|lista",
                                                    r["dominio"]) for r in lista)
    achados["ok"] = True
    return achados


# ── 2. Conteúdo do site oficial ──────────────────────────────────────────────

def _ler_site(url: str) -> dict:
    """Extrai título, descrição e sinais de conteúdo do site oficial."""
    out = {"ok": False, "url": url}
    if not url:
        out["erro"] = "sem site"
        return out
    try:
        html = web_enrich._fetch(url if url.startswith("http") else "http://" + url)
    except Exception as e:
        out["erro"] = f"site inacessível: {e}"
        return out

    def tag(rx):
        m = re.search(rx, html, re.I | re.S)
        return re.sub(r"\s+", " ", m.group(1)).strip()[:200] if m else ""

    texto = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.I | re.S)
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = re.sub(r"\s+", " ", texto)

    servicos = []
    for termo in ["implante", "ortodontia", "aparelho", "clareamento", "prótese", "protese",
                  "canal", "endodontia", "periodontia", "harmonização", "botox", "lentes",
                  "facetas", "limpeza", "extração", "siso", "invisalign", "urgência",
                  "odontopediatria", "cirurgia"]:
        if re.search(rf"\b{termo}", texto, re.I):
            servicos.append(termo)

    out.update({
        "ok": True,
        "titulo": tag(r"<title[^>]*>(.*?)</title>"),
        "descricao": tag(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']'),
        "servicos_citados": servicos[:10],
        "tem_agendamento_online": bool(re.search(r"agendar|agendamento|marcar consulta|"
                                                 r"schedule|whatsapp", texto, re.I)),
        "tem_whatsapp_no_site": bool(re.search(r"wa\.me|api\.whatsapp", html, re.I)),
        "tamanho_conteudo": len(texto),
        "conteudo_raso": len(texto) < 1200,
    })
    return out


# ── 3. Dossiê completo ───────────────────────────────────────────────────────

def coletar(lead_id: int, com_google: bool = True, com_site: bool = True) -> dict:
    lead = db.get_lead(lead_id)
    if not lead:
        return {"ok": False, "erro": "lead não encontrado"}

    dossie = {"ok": True, "lead_id": lead_id, "coletado_em": None}

    # 1. Google Maps (do banco)
    dossie["google_maps"] = {
        "nome": lead["nome"],
        "categoria": lead.get("categoria"),
        "posicao_organica": lead.get("posicao"),
        "nota": lead.get("avaliacao_num"),
        "total_avaliacoes": lead.get("total_avaliacoes_num"),
        "telefone": lead.get("telefone"),
        "endereco": lead.get("endereco"),
        "site_vinculado": bool(lead.get("tem_site")),
        "instagram_vinculado": bool(lead.get("tem_instagram")),
        "patrocinado": bool(lead.get("patrocinado")),
    }

    # 2. Busca na web pelo nome da clínica.
    # Usa o DDD do telefone para saber o estado e evitar confundir com homônimas
    # de outra região (o endereço do Maps costuma trazer só a rua).
    if com_google:
        import whatsapp_check
        n = whatsapp_check.normalizar(lead.get("telefone") or "")
        ddd = n["ddd"] if n else ""
        uf = DDD_UF.get(ddd, "")
        termo = f'{lead["nome"]} {(lead.get("endereco") or "").split(",")[0]} {uf}'.strip()
        dossie["busca_google"] = _buscar_web(termo, ddd_ref=ddd)
        dossie["busca_google"]["uf_da_clinica"] = uf or "desconhecida"
    else:
        dossie["busca_google"] = {"ok": False, "erro": "não coletado"}

    # 3. Site oficial
    if com_site and lead.get("website"):
        dossie["site"] = _ler_site(lead["website"])
    else:
        dossie["site"] = {"ok": False, "erro": "clínica sem site conhecido"}

    # 4. PageSpeed (se já coletado)
    ps = pagespeed.dados_para_pdf(lead)
    dossie["pagespeed"] = {"score": ps.get("score"), "metrics": ps.get("metrics")} if ps else \
                          {"score": None, "erro": "não coletado"}

    # 5. Concorrência (referência da região)
    lideres = db.leaders_by_nicho(lead.get("nicho", ""), lead_id, 1)
    if lideres:
        c = lideres[0]
        dossie["referencia_regiao"] = {
            "nome": c["nome"], "posicao": c.get("posicao"), "nota": c.get("avaliacao_num"),
            "avaliacoes": c.get("total_avaliacoes_num"),
            "site_vinculado": bool(c.get("tem_site")),
            "instagram_vinculado": bool(c.get("tem_instagram")),
        }
    else:
        dossie["referencia_regiao"] = None

    from datetime import datetime
    dossie["coletado_em"] = datetime.now().isoformat(timespec="seconds")

    # guarda no banco para reuso
    try:
        with db.conn() as con:
            con.execute("UPDATE leads SET dossie_json=? WHERE id=?",
                        (json.dumps(dossie, ensure_ascii=False), lead_id))
    except Exception:
        pass
    return dossie


def resumo_legivel(d: dict) -> str:
    """Versão curta do dossiê, para log/checagem."""
    g, b, s = d.get("google_maps", {}), d.get("busca_google", {}), d.get("site", {})
    L = [f"MAPS: {g.get('nome')} · {g.get('posicao_organica')}º · nota {g.get('nota')} · "
         f"{g.get('total_avaliacoes')} avaliações · site {g.get('site_vinculado')}"]
    if b.get("ok"):
        L.append(f"GOOGLE: {b.get('total_resultados')} domínios · redes {b.get('redes')} · "
                 f"diretórios {b.get('diretorios')}")
    else:
        L.append(f"GOOGLE: {b.get('erro')}")
    if s.get("ok"):
        L.append(f"SITE: '{(s.get('titulo') or '')[:60]}' · serviços {s.get('servicos_citados')} · "
                 f"agendamento {s.get('tem_agendamento_online')} · raso {s.get('conteudo_raso')}")
    else:
        L.append(f"SITE: {s.get('erro')}")
    ps = d.get("pagespeed", {})
    L.append(f"PAGESPEED: {ps.get('score') if ps.get('score') is not None else ps.get('erro')}")
    return "\n".join(L)


if __name__ == "__main__":
    lid = int(sys.argv[1]) if len(sys.argv) > 1 else 547
    d = coletar(lid)
    print(resumo_legivel(d))
    print("\n--- dossiê completo ---")
    print(json.dumps(d, ensure_ascii=False, indent=2)[:2500])
