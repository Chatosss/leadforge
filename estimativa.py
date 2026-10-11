"""estimativa.py — Estimativa DEFENSÁVEL de pacientes e receita perdidos."""

import json
import re
import unicodedata
import urllib.parse
import urllib.request
from typing import Dict, Optional

import config

# ── CTR por posição no bloco local do Google (fração dos cliques) ─────────────
# Curva pública de estudos de SEO local: a 1ª posição leva a maior fatia e a
# cauda cai rápido. Abaixo da 10ª a visibilidade é praticamente residual.
CTR_POSICAO = {
    1: 0.246, 2: 0.135, 3: 0.092, 4: 0.062, 5: 0.045,
    6: 0.034, 7: 0.026, 8: 0.020, 9: 0.016, 10: 0.013,
}
CTR_CAUDA_BASE = 0.013     # a partir da 10ª
CTR_CAUDA_DECAIMENTO = 0.88  # cada posição adiante vale 12% menos
CTR_MINIMO = 0.0005        # nunca zero: sempre resta um resíduo


def ctr_da_posicao(pos: Optional[int]) -> float:
    """Fração de cliques que a posição costuma receber."""
    if not pos or pos < 1:
        return CTR_MINIMO
    if pos in CTR_POSICAO:
        return CTR_POSICAO[pos]
    ctr = CTR_CAUDA_BASE * (CTR_CAUDA_DECAIMENTO ** (pos - 10))
    return max(CTR_MINIMO, ctr)


# ── População do município (IBGE, com cache) ─────────────────────────────────

_CACHE_POP = config.DATA_DIR / "populacao_cache.json"


def _norm(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", s or "")
                if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z ]", "", s.lower()).strip()


def _cache() -> dict:
    try:
        return json.loads(_CACHE_POP.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _salvar_cache(c: dict):
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_POP.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _get_json(url: str):
    """GET com suporte a gzip (a API do IBGE responde comprimido)."""
    import gzip
    import io
    req = urllib.request.Request(url, headers={
        "User-Agent": "clinica_prospector", "Accept-Encoding": "gzip, identity"})
    with urllib.request.urlopen(req, timeout=25) as r:
        bruto = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or bruto[:2] == b"\x1f\x8b":
            bruto = gzip.GzipFile(fileobj=io.BytesIO(bruto)).read()
    return json.loads(bruto.decode("utf-8"))


def populacao_municipio(cidade: str, uf: str = "") -> Optional[int]:
    """População estimada do município via API pública do IBGE (com cache)."""
    if not cidade:
        return None
    chave = f"{_norm(cidade)}|{uf.upper()}"
    c = _cache()
    if chave in c:
        return c[chave]

    try:
        # 1) acha o código do município
        url = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"
        if uf:
            url = f"https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios"
        municipios = _get_json(url)
        alvo = _norm(cidade)
        cod = next((m["id"] for m in municipios if _norm(m["nome"]) == alvo), None)
        if cod is None:
            cod = next((m["id"] for m in municipios if alvo in _norm(m["nome"])), None)
        if cod is None:
            c[chave] = None
            _salvar_cache(c)
            return None

        # 2) população estimada (agregado 6579 = população residente estimada)
        u2 = (f"https://servicodados.ibge.gov.br/api/v3/agregados/6579/periodos/-1/"
              f"variaveis/9324?localidades=N6[{cod}]")
        dados = _get_json(u2)
        serie = dados[0]["resultados"][0]["series"][0]["serie"]
        valores = [int(v) for v in serie.values() if str(v).isdigit()]
        if not valores:
            return None
        pop = valores[-1]
        c[chave] = pop
        _salvar_cache(c)
        return pop
    except Exception:
        return None


# ── Estimativa completa ──────────────────────────────────────────────────────

def estimar(lead: Dict, populacao: Optional[int] = None,
            buscas_mes: Optional[int] = None) -> Dict:
    """Calcula a perda estimada com memória de cálculo."""
    nicho = lead.get("nicho", config.NICHO_PADRAO)
    ticket = config.TICKET_POR_NICHO.get(nicho, config.TICKET_POR_NICHO["padrao"])
    taxa_busca = config.TAXA_BUSCA_MENSAL.get(nicho, config.TAXA_BUSCA_MENSAL["padrao"])
    conversao = config.TAXA_CONVERSAO_CLIQUE
    pos = lead.get("posicao")

    memoria = []
    fonte_volume = ""

    # 1) volume de buscas no mês
    if buscas_mes:
        fonte_volume = "informado manualmente"
        memoria.append(f"Buscas/mês informadas: {buscas_mes:,}".replace(",", "."))
    else:
        if populacao is None:
            cidade, uf = _cidade_uf_do_lead(lead)
            populacao = populacao_municipio(cidade, uf) if cidade else None
            if populacao:
                fonte_volume = f"IBGE ({cidade}/{uf}: {populacao:,} hab.)".replace(",", ".")
        if populacao:
            buscas_mes = int(populacao * taxa_busca)
            memoria.append(f"População {populacao:,} × {taxa_busca*100:.2f}% que procura "
                           f"{nicho} por mês = {buscas_mes:,} buscas".replace(",", "."))
        else:
            # sem população: usa o porte médio configurado, e avisa
            buscas_mes = config.BUSCAS_MES_PADRAO
            fonte_volume = "porte médio (cidade não identificada)"
            memoria.append(f"Cidade não identificada, usando volume padrão de "
                           f"{buscas_mes:,} buscas/mês".replace(",", "."))

    # 2) cliques que a clínica deixa de receber
    ctr_atual = ctr_da_posicao(pos)
    ctr_alvo = ctr_da_posicao(config.POSICAO_ALVO)
    ganho_ctr = max(0.0, ctr_alvo - ctr_atual)
    cliques_perdidos = buscas_mes * ganho_ctr
    memoria.append(f"CTR na {pos or '?'}ª posição: {ctr_atual*100:.2f}% · "
                   f"CTR na {config.POSICAO_ALVO}ª: {ctr_alvo*100:.2f}% · "
                   f"diferença {ganho_ctr*100:.2f}% = {cliques_perdidos:.0f} cliques/mês")

    # 3) canais ausentes reduzem a conversão de quem chega ao perfil
    penal = 1.0
    if not lead.get("tem_site"):
        penal -= config.PENALIDADE_SEM_SITE
    if not lead.get("tem_instagram"):
        penal -= config.PENALIDADE_SEM_INSTAGRAM
    penal = max(0.4, penal)
    conv_efetiva = conversao * penal
    if penal < 1.0:
        memoria.append(f"Sem site/Instagram no perfil, conversão cai de "
                       f"{conversao*100:.0f}% para {conv_efetiva*100:.1f}%")

    # 4) pacientes e dinheiro
    pacientes = cliques_perdidos * conv_efetiva
    pacientes = max(0, round(pacientes))
    valor = int(pacientes * ticket)
    memoria.append(f"{cliques_perdidos:.0f} cliques × {conv_efetiva*100:.1f}% de conversão "
                   f"= {pacientes} pacientes/mês × R$ {ticket} = R$ {valor:,}".replace(",", "."))

    return {
        "pacientes_mes": int(pacientes),
        "valor_mes": valor,
        "ticket": ticket,
        "buscas_mes": int(buscas_mes),
        "populacao": populacao,
        "ctr_atual": ctr_atual,
        "ctr_alvo": ctr_alvo,
        "posicao": pos,
        "posicao_alvo": config.POSICAO_ALVO,
        "conversao": conv_efetiva,
        "fonte_volume": fonte_volume,
        "memoria_calculo": memoria,
    }


def _cidade_uf_do_lead(lead) -> tuple:
    """Descobre cidade e UF do lead (dossiê da busca > endereço > DDD)."""
    import json as _json
    # 1) dossiê da coleta profunda (mais confiável)
    try:
        d = _json.loads(lead.get("dossie_json") or "{}")
        cid = (d.get("busca_google") or {}).get("cidade_uf_encontrada")
        if cid and "/" in cid:
            c, u = cid.split("/")
            return c.strip(), u.strip()
    except Exception:
        pass
    # 2) endereço do Maps
    end = lead.get("endereco") or ""
    m = re.search(r"([A-Za-zÀ-ÿ\s]{4,})\s*[-/]\s*([A-Z]{2})\b", end)
    if m:
        return m.group(1).strip(), m.group(2)
    # 3) UF pelo DDD (sem cidade, não dá para pegar população)
    return "", ""


if __name__ == "__main__":
    import sys
    import db
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    lid = int(sys.argv[1]) if len(sys.argv) > 1 else 544
    lead = db.get_lead(lid)
    print(f"=== {lead['nome']} (posição {lead.get('posicao')}) ===\n")
    r = estimar(lead)
    for linha in r["memoria_calculo"]:
        print("  •", linha)
    print(f"\n  RESULTADO: {r['pacientes_mes']} pacientes/mês · R$ {r['valor_mes']:,}/mês"
          .replace(",", "."))
    print(f"  Fonte do volume: {r['fonte_volume']}")
