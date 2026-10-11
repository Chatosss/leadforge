"""cidade.py — De que cidade é este lead, e com que evidência."""

import math
import re
from typing import Dict, Optional

# Referências das cidades onde a operação atua ou pode atuar. Para incluir uma
# cidade nova, basta uma linha aqui: nome canônico, UF, latitude, longitude.
CIDADES = {
    "arapiraca": {"nome": "Arapiraca", "uf": "AL", "lat": -9.7520, "lon": -36.6614},
    "maceio":    {"nome": "Maceió",    "uf": "AL", "lat": -9.6498, "lon": -35.7089},
    "manaus":    {"nome": "Manaus",    "uf": "AM", "lat": -3.1190, "lon": -60.0217},
}

# Raio de aceitação. Generoso porque região metropolitana espalha (Manaus tem
# bairro a mais de 30 km do centro), e apertado o bastante para não confundir
# duas cidades da lista: Arapiraca e Maceió estão a ~130 km uma da outra.
RAIO_KM = 60.0

# O DDD prova a UF, não a cidade. Só vira palpite de cidade onde a lista tem uma
# única cidade naquele DDD; onde há mais de uma (82: Arapiraca e Maceió), o
# resultado fica sem cidade de propósito, em vez de escolher a mais provável.
DDD_UF = {"82": "AL", "92": "AM"}

_RE_COORD = re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)")


def _sem_acento(s: str) -> str:
    tabela = str.maketrans("áàâãéèêíìîóòôõúùûç", "aaaaeeeiiioooouuuc")
    return (s or "").lower().translate(tabela)


def _distancia_km(lat1, lon1, lat2, lon2) -> float:
    """Haversine. Distância em linha reta, que é o que basta aqui."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def coordenada(lead) -> Optional[tuple]:
    """A coordenada do estabelecimento, extraída do maps_url. (lat, lon) ou None."""
    m = _RE_COORD.search((lead.get("maps_url") or ""))
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def _por_coordenada(lead) -> Optional[Dict]:
    par = coordenada(lead)
    if not par:
        return None
    lat, lon = par
    melhor, menor = None, None
    for chave, c in CIDADES.items():
        d = _distancia_km(lat, lon, c["lat"], c["lon"])
        if menor is None or d < menor:
            melhor, menor = chave, d
    if melhor is None or menor > RAIO_KM:
        return None
    c = CIDADES[melhor]
    return {"cidade": c["nome"], "uf": c["uf"], "fonte": "coordenada",
            "confianca": "apurada", "detalhe": f"{menor:.1f} km do centro"}


def _por_texto(lead, campo: str, fonte: str) -> Optional[Dict]:
    alvo = _sem_acento(lead.get(campo) or "")
    if not alvo:
        return None
    for chave, c in CIDADES.items():
        if re.search(rf"\b{re.escape(chave)}\b", alvo):
            return {"cidade": c["nome"], "uf": c["uf"], "fonte": fonte,
                    "confianca": "apurada", "detalhe": f"'{chave}' no {campo}"}
    return None


def _por_ddd(lead) -> Optional[Dict]:
    bruto = re.sub(r"\D", "", (lead.get("telefone") or "") or (lead.get("whatsapp") or ""))
    if bruto.startswith("55"):
        bruto = bruto[2:]
    if len(bruto) < 10:
        return None
    uf = DDD_UF.get(bruto[:2])
    if not uf:
        return None
    candidatas = [c["nome"] for c in CIDADES.values() if c["uf"] == uf]
    # uma única cidade conhecida naquela UF é palpite razoável; duas ou mais,
    # escolher uma seria exatamente o erro que este módulo existe para corrigir
    cidade = candidatas[0] if len(candidatas) == 1 else None
    return {"cidade": cidade, "uf": uf, "fonte": "ddd", "confianca": "presumida",
            "detalhe": f"DDD {bruto[:2]}"}


def resolver(lead) -> Dict:
    """A cidade do lead pela evidência mais forte disponível."""
    if hasattr(lead, "keys"):
        lead = dict(lead)
    for tentativa in (
        lambda: _por_coordenada(lead),
        lambda: _por_texto(lead, "endereco", "endereco"),
        lambda: _por_texto(lead, "nome", "nome"),
        lambda: _por_ddd(lead),
    ):
        r = tentativa()
        if r and (r.get("cidade") or r.get("uf")):
            return r
    return {"cidade": None, "uf": None, "fonte": "nenhuma",
            "confianca": "nenhuma", "detalhe": "sem evidência de cidade"}


def pode_afirmar(lead) -> bool:
    """Este lead pode ter a cidade escrita num texto que vai para o cliente?"""
    r = resolver(lead)
    return r["confianca"] == "apurada" and bool(r["cidade"])


def rotulo(lead) -> str:
    """'Manaus, AM' quando dá para afirmar; string vazia quando não dá."""
    r = resolver(lead)
    if r["confianca"] != "apurada" or not r["cidade"]:
        return ""
    return f"{r['cidade']}, {r['uf']}" if r["uf"] else r["cidade"]


def gravar(lead_id: int, lead=None) -> Dict:
    """Grava a cidade apurada nas colunas do lead e devolve o que foi resolvido."""
    import db
    if lead is None:
        lead = db.get_lead(lead_id)
    r = resolver(lead)
    with db.conn() as con:
        con.execute("UPDATE leads SET cidade=?, uf=?, cidade_fonte=? WHERE id=?",
                    (r["cidade"] or "", r["uf"] or "", r["fonte"], lead_id))
    return r


def backfill() -> Dict[str, int]:
    """Preenche cidade/uf/cidade_fonte em toda a base. Idempotente."""
    import dashboard_db as ddb
    import db
    ddb.init_dashboard()          # garante as colunas
    contagem: Dict[str, int] = {}
    with db.conn() as con:
        linhas = con.execute("SELECT * FROM leads").fetchall()
        for l in linhas:
            r = resolver(l)
            con.execute("UPDATE leads SET cidade=?, uf=?, cidade_fonte=? WHERE id=?",
                        (r["cidade"] or "", r["uf"] or "", r["fonte"], l["id"]))
            chave = f"{r['cidade'] or '(sem cidade)'} / {r['fonte']}"
            contagem[chave] = contagem.get(chave, 0) + 1
    return contagem


if __name__ == "__main__":
    import collections
    import sys

    import db

    if "--gravar" in sys.argv:
        print("gravando cidade apurada em todos os leads...\n")
        for chave, n in sorted(backfill().items(), key=lambda x: -x[1]):
            print(f"  {n:5}  {chave}")
        raise SystemExit(0)

    contagem = collections.Counter()
    sem = []
    with db.conn() as con:
        linhas = con.execute(
            "SELECT * FROM leads WHERE origem='scrap-live' ORDER BY id").fetchall()
    for l in linhas:
        r = resolver(l)
        contagem[(r["cidade"], r["fonte"], r["confianca"])] += 1
        if r["confianca"] != "apurada":
            sem.append((l["id"], l["nome"][:40], r["detalhe"]))

    print(f"{len(linhas)} leads scrap-live\n")
    print(f"{'cidade':14} {'fonte':12} {'confianca':11} n")
    for (cid, fonte, conf), n in contagem.most_common():
        print(f"{str(cid):14} {fonte:12} {conf:11} {n}")
    if sem:
        print(f"\n{len(sem)} sem cidade apurada (não recebem cidade no material):")
        for i, nome, det in sem[:20]:
            print(f"  {i}  {nome:42} {det}")
