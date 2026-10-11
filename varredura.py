# -*- coding: utf-8 -*-
"""varredura.py — Colher um ESTADO inteiro, e não a primeira página dele."""

import math
import re
import unicodedata
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

# Retângulo que envolve cada estado: (lat_min, lat_max, lon_min, lon_max).
UFS: Dict[str, Dict] = {
    "AC": {"nome": "Acre",                "cx": (-11.15, -7.11, -73.99, -66.62)},
    "AL": {"nome": "Alagoas",             "cx": (-10.50, -8.81, -38.24, -35.15)},
    "AP": {"nome": "Amapá",               "cx": (-1.24,   4.44, -54.88, -49.87)},
    "AM": {"nome": "Amazonas",            "cx": (-9.82,   2.25, -73.80, -56.10)},
    "BA": {"nome": "Bahia",               "cx": (-18.35, -8.53, -46.62, -37.34)},
    "CE": {"nome": "Ceará",               "cx": (-7.86,  -2.78, -41.42, -37.25)},
    "DF": {"nome": "Distrito Federal",    "cx": (-16.05, -15.50, -48.29, -47.31)},
    "ES": {"nome": "Espírito Santo",      "cx": (-21.30, -17.89, -41.88, -39.66)},
    "GO": {"nome": "Goiás",               "cx": (-19.50, -12.39, -53.25, -45.90)},
    "MA": {"nome": "Maranhão",            "cx": (-10.26, -1.04, -48.75, -41.79)},
    "MT": {"nome": "Mato Grosso",         "cx": (-18.04, -7.35, -61.63, -50.22)},
    "MS": {"nome": "Mato Grosso do Sul",  "cx": (-24.07, -17.16, -58.17, -50.92)},
    "MG": {"nome": "Minas Gerais",        "cx": (-22.92, -14.23, -51.05, -39.86)},
    "PA": {"nome": "Pará",                "cx": (-9.84,   2.59, -58.90, -46.06)},
    "PB": {"nome": "Paraíba",             "cx": (-8.30,  -6.02, -38.77, -34.79)},
    "PR": {"nome": "Paraná",              "cx": (-26.72, -22.51, -54.62, -48.02)},
    "PE": {"nome": "Pernambuco",          "cx": (-9.48,  -7.29, -41.36, -34.80)},
    "PI": {"nome": "Piauí",               "cx": (-10.93, -2.74, -45.99, -40.37)},
    "RJ": {"nome": "Rio de Janeiro",      "cx": (-23.37, -20.76, -44.89, -40.96)},
    "RN": {"nome": "Rio Grande do Norte", "cx": (-6.98,  -4.83, -38.58, -34.97)},
    "RS": {"nome": "Rio Grande do Sul",   "cx": (-33.75, -27.08, -57.65, -49.69)},
    "RO": {"nome": "Rondônia",            "cx": (-13.69, -7.97, -66.81, -59.77)},
    "RR": {"nome": "Roraima",             "cx": (-1.58,   5.27, -64.82, -58.89)},
    "SC": {"nome": "Santa Catarina",      "cx": (-29.35, -25.96, -53.84, -48.35)},
    "SP": {"nome": "São Paulo",           "cx": (-25.31, -19.78, -53.11, -44.16)},
    "SE": {"nome": "Sergipe",             "cx": (-11.57, -9.51, -38.25, -36.39)},
    "TO": {"nome": "Tocantins",           "cx": (-13.47, -5.17, -50.74, -45.70)},
}

# Quando vale dividir uma área em quatro.
CHEIA = 25

# ...e o outro sinal: o Google truncou a lista.
CORTE_DO_GOOGLE = 55

# Teto de células na primeira rodada.
CELULAS_INICIAIS = 12

# Margem de tolerância no recorte, em graus (~17 km). O retângulo do estado é
# aproximado e a fronteira não é reta: sem folga, empresa de cidade grudada na
# divisa cairia fora por um erro do retângulo, não por estar em outro estado.
FOLGA_GRAUS = 0.15

LARGURA_PX = 900          # a mesma janela que a coleta abre; entra no cálculo do zoom
_RE_COORD = re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)")


# ── Identificar o estado a partir do que o operador digitou
# ──────────────────────

def _simples(t: str) -> str:
    t = unicodedata.normalize("NFKD", (t or "").strip().lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def resolver_uf(texto: str) -> Optional[str]:
    """'AL', 'Alagoas', 'alagoas ' -> 'AL'. Qualquer outra coisa -> None."""
    alvo = _simples(texto)
    if not alvo:
        return None
    if alvo.upper() in UFS:
        return alvo.upper()
    for sigla, dados in UFS.items():
        if alvo == _simples(dados["nome"]):
            return sigla
    return None


def nome_do_estado(uf: str) -> str:
    return UFS.get((uf or "").upper(), {}).get("nome", uf)


# ── Geometria ────────────────────────────────────────────────────────────────

def graus_visiveis(z: int) -> float:
    """Quantos graus de longitude cabem na janela, neste zoom."""
    return LARGURA_PX * 360.0 / (256.0 * (2.0 ** z))


def zoom_da_celula(largura_lon: float) -> int:
    """O zoom que mostra `largura_lon` graus de longitude."""
    z = math.log2(LARGURA_PX * 360.0 / (256.0 * max(largura_lon, 1e-9)))
    return max(4, min(15, int(round(z))))


# Quanto uma célula invade a vizinha. Sobreposição custa repetição, que a coleta
# descarta pelo `maps_url`; falta de sobreposição custa faixa do estado sem
# ninguém olhando, e isso não aparece em lugar nenhum. O erro barato é o de cima.
SOBREPOSICAO = 0.10


def _passos(z: int, lat_ref: float) -> Tuple[float, float]:
    """Passo em (latitude, longitude) de uma célula neste zoom."""
    dlon = graus_visiveis(z) * (1.0 - SOBREPOSICAO)
    # a janela é mais baixa que larga, e no Mercator um grau de latitude ocupa
    # mais pixels conforme se afasta do equador: no Rio Grande do Sul a mesma
    # janela cobre 13% menos latitude do que em Roraima
    dlat = dlon * (680.0 / LARGURA_PX) * math.cos(math.radians(lat_ref))
    return abs(dlat), abs(dlon)


def grade(caixa: Tuple[float, float, float, float], z: int) -> List[Tuple]:
    """Cobre o retângulo com células do tamanho exato que o zoom `z` enquadra."""
    lat0, lat1, lon0, lon1 = caixa
    dlat, dlon = _passos(z, (lat0 + lat1) / 2)
    nlin = max(1, int(math.ceil(abs(lat1 - lat0) / dlat)))
    ncol = max(1, int(math.ceil(abs(lon1 - lon0) / dlon)))
    return [(lat0 + i * dlat, lat0 + (i + 1) * dlat,
             lon0 + j * dlon, lon0 + (j + 1) * dlon)
            for i in range(nlin) for j in range(ncol)]


def zoom_inicial(caixa: Tuple[float, float, float, float], teto: int) -> int:
    """O zoom mais fechado que cobre o estado sem passar de `teto` células."""
    escolhido = 4
    for z in range(4, 14):
        if len(grade(caixa, z)) > teto:
            break
        escolhido = z
    return escolhido


def quatro(cel: Tuple[float, float, float, float]) -> List[Tuple]:
    """Parte uma célula em quatro. É a divisão que a saturação dispara."""
    lat0, lat1, lon0, lon1 = cel
    mlat, mlon = (lat0 + lat1) / 2, (lon0 + lon1) / 2
    return [(lat0, mlat, lon0, mlon), (lat0, mlat, mlon, lon1),
            (mlat, lat1, lon0, mlon), (mlat, lat1, mlon, lon1)]


def coordenada_do_url(maps_url: str) -> Optional[Tuple[float, float]]:
    m = _RE_COORD.search(maps_url or "")
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def dentro_do_estado(maps_url: str, uf: str) -> bool:
    """Este lugar fica no estado varrido?"""
    caixa = UFS.get((uf or "").upper(), {}).get("cx")
    if not caixa:
        return True
    par = coordenada_do_url(maps_url)
    if not par:
        return False
    lat, lon = par
    lat0, lat1, lon0, lon1 = caixa
    return (lat0 - FOLGA_GRAUS <= lat <= lat1 + FOLGA_GRAUS
            and lon0 - FOLGA_GRAUS <= lon <= lon1 + FOLGA_GRAUS)


def url_da_celula(termo: str, cel: Tuple[float, float, float, float]) -> str:
    lat0, lat1, lon0, lon1 = cel
    z = zoom_da_celula(abs(lon1 - lon0))
    return (f"https://www.google.com/maps/search/{quote_plus(termo)}"
            f"/@{(lat0 + lat1) / 2:.5f},{(lon0 + lon1) / 2:.5f},{z}z")


def plano(uf: str, orcamento: int = 30) -> Dict:
    """O que a varredura vai fazer, sem fazer — alimenta o aviso antes de começar."""
    caixa = UFS.get((uf or "").upper(), {}).get("cx")
    if not caixa:
        return {"ok": False, "erro": f"estado desconhecido: {uf}"}
    z = zoom_inicial(caixa, min(CELULAS_INICIAIS, max(1, orcamento)))
    cels = grade(caixa, z)
    return {"ok": True, "uf": uf.upper(), "estado": nome_do_estado(uf),
            "celulas_iniciais": len(cels), "orcamento": orcamento,
            "zoom_inicial": z,
            "minutos_estimados": int(round(orcamento * 45 / 60.0))}


# ── A varredura ──────────────────────────────────────────────────────────────

def _guardar(fila, cheias, consultas, fora, sem_coord, por_celula) -> Dict:
    """O estado da varredura em algo que cabe numa coluna de texto."""
    return {"fila": [[list(cel), niv] for cel, niv in fila],
            "cheias": [[list(cel), niv, rendeu] for cel, niv, rendeu in cheias],
            "consultas": consultas, "fora": fora, "sem_coord": sem_coord,
            "por_celula": por_celula}


def _recuperar(estado: Dict):
    """Desfaz o `_guardar`. Estado estragado vale como estado nenhum."""
    try:
        fila = [(tuple(cel), int(niv)) for cel, niv in estado.get("fila", [])]
        cheias = [(tuple(cel), int(niv), int(r)) for cel, niv, r in estado.get("cheias", [])]
        return (fila, cheias, int(estado.get("consultas", 0)),
                int(estado.get("fora", 0)), int(estado.get("sem_coord", 0)),
                list(estado.get("por_celula", [])))
    except Exception:
        return None


async def varrer(page, termo: str, uf: str, coletados: Dict,
                 abrir: Callable, rolar: Callable,
                 orcamento: int = 30, prof_max: int = 3,
                 ao_progredir: Optional[Callable] = None,
                 retomar: Optional[Dict] = None,
                 ao_checkpoint: Optional[Callable] = None,
                 deve_parar: Optional[Callable] = None,
                 ao_gravar: Optional[Callable] = None) -> Dict:
    """Percorre o estado célula a célula, gravando tudo em `coletados`."""
    caixa = UFS.get((uf or "").upper(), {}).get("cx")
    if not caixa:
        return {"ok": False, "erro": f"estado desconhecido: {uf}"}

    recuperado = _recuperar(retomar) if retomar else None
    if recuperado:
        fila, cheias, consultas, fora, sem_coord, por_celula = recuperado
    else:
        fila = [(c, 0) for c in
                grade(caixa, zoom_inicial(caixa, min(CELULAS_INICIAIS,
                                                     max(1, orcamento))))]
        cheias: List[Tuple] = []        # células que bateram no corte, por rendimento
        consultas = fora = sem_coord = 0
        por_celula: List[Dict] = []

    def aceitar(info) -> bool:
        """Filtro do recorte, aplicado card a card, ainda dentro da rolagem."""
        nonlocal fora, sem_coord
        url = info.get("maps_url") or ""
        if not coordenada_do_url(url):
            sem_coord += 1
            return False
        if not dentro_do_estado(url, uf):
            fora += 1
            return False
        return True

    interrompida = False
    while consultas < orcamento:
        if deve_parar and deve_parar():
            interrompida = True
            break
        if not fila:
            if not cheias:
                break                    # o estado acabou antes do orçamento
            # o nível terminou: desce, atacando primeiro o que mais rendeu — é
            # onde o corte do Google escondeu mais coisa
            cheias.sort(key=lambda x: -x[2])
            fila = [(filha, nivel + 1)
                    for cel, nivel, _rendeu in cheias for filha in quatro(cel)]
            cheias = []

        cel, nivel = fila.pop(0)
        consultas += 1
        antes = len(coletados)

        if ao_progredir:
            await ao_progredir(f"varrendo {nome_do_estado(uf)}: área {consultas} "
                               f"de até {orcamento} · {len(coletados)} encontrados")

        leitura = {}
        if await abrir(page, url_da_celula(termo, cel)):
            # `ao_gravar` e `deve_parar` DESCEM até aqui.
            leitura = await rolar(page, coletados, aceitar=aceitar,
                                  ao_progredir=ao_progredir,
                                  rotulo=f"área {consultas}/{orcamento}",
                                  ao_gravar=ao_gravar,
                                  deve_parar=deve_parar) or {}

        rendeu = len(coletados) - antes
        brutos = leitura.get("brutos", 0)
        # parou no meio da área: a varredura para junto, sem abrir a próxima
        if leitura.get("interrompida"):
            interrompida = True
        por_celula.append({"nivel": nivel, "novos": rendeu, "brutos": brutos,
                           "centro": [round((cel[0] + cel[1]) / 2, 4),
                                      round((cel[2] + cel[3]) / 2, 4)]})

        # Dois sinais independentes de que esta área ainda tem coisa escondida: rendeu
        # bem (há empresa aqui) ou o Google truncou a lista (mesmo que o recorte tenha
        # barrado quase tudo).
        if nivel < prof_max and (rendeu >= CHEIA or brutos >= CORTE_DO_GOOGLE):
            cheias.append((cel, nivel, rendeu))

        # o ponto de retomada é gravado DEPOIS da área, com ela já contada:
        # anotar antes faria a retomada repetir a área inteira
        if ao_checkpoint:
            await ao_checkpoint(_guardar(fila, cheias, consultas, fora,
                                         sem_coord, por_celula))
        if interrompida:
            break

    faltam = len(fila) + len(cheias) * 4
    return {"ok": True, "consultas": consultas, "encontrados": len(coletados),
            "fora_do_estado": fora, "sem_coordenada": sem_coord,
            "celulas": por_celula, "restaram": faltam,
            "interrompida": interrompida,
            "estado": _guardar(fila, cheias, consultas, fora, sem_coord,
                               por_celula) if (interrompida and faltam) else None}


if __name__ == "__main__":
    import sys
    alvo = sys.argv[1] if len(sys.argv) > 1 else "AL"
    uf = resolver_uf(alvo)
    print(f"{alvo!r} -> {uf}")
    if uf:
        p = plano(uf, int(sys.argv[2]) if len(sys.argv) > 2 else 30)
        print(p)
        for c in grade(UFS[uf]["cx"], p["zoom_inicial"])[:4]:
            print("   ", url_da_celula("Marketing digital", c))
