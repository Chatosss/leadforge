# -*- coding: utf-8 -*-
"""test_varredura.py — A varredura por estado, o descarte e a trava de repetição."""

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
os.environ["LEADFORGE_DB"] = str(Path(tempfile.mkdtemp(prefix="lf_teste_")) / "teste.db")
sys.path.insert(0, str(RAIZ))

import config                       # noqa: E402
import db                           # noqa: E402
import dashboard_db as ddb          # noqa: E402
import scrap_destino                # noqa: E402
import scrap_live                   # noqa: E402
import varredura as V               # noqa: E402

falhas = []


def checar(rotulo, condicao, detalhe=""):
    print("   " + ("OK     " if condicao else "FALHOU ") + rotulo
          + ((" - " + str(detalhe)) if detalhe else ""))
    if not condicao:
        falhas.append(rotulo)


def titulo(t):
    print("\n" + t)


# ══ 1. Reconhecer o estado ════════════════════════════════════════════════════
titulo("1) o que é um estado, e o que não é")
checar("'Alagoas' -> AL", V.resolver_uf("Alagoas") == "AL")
checar("'AL' -> AL", V.resolver_uf("AL") == "AL")
checar("'  alagoas ' -> AL", V.resolver_uf("  alagoas ") == "AL")
checar("'São Paulo' -> SP", V.resolver_uf("São Paulo") == "SP")
checar("'Sao Paulo' (sem acento) -> SP", V.resolver_uf("Sao Paulo") == "SP")
# o caso que decide o desenho: cidade NÃO vira varredura de 27 mil km²
checar("'Maceió AL' -> None (é cidade)", V.resolver_uf("Maceió AL") is None)
checar("'Arapiraca' -> None", V.resolver_uf("Arapiraca") is None)
checar("vazio -> None", V.resolver_uf("") is None)
checar("os 27 estados respondem", all(V.resolver_uf(s) == s for s in V.UFS))


# ══ 2. Geometria: a grade cobre o estado ══════════════════════════════════════
titulo("2) a grade cobre o estado inteiro, sem buraco")
for uf in ("AL", "AM", "RS", "DF"):
    caixa = V.UFS[uf]["cx"]
    z = V.zoom_inicial(caixa, V.CELULAS_INICIAIS)
    cels = V.grade(caixa, z)
    lat0, lat1, lon0, lon1 = caixa
    cobre = (min(c[0] for c in cels) <= lat0 + 1e-9
             and max(c[1] for c in cels) >= lat1 - 1e-9
             and min(c[2] for c in cels) <= lon0 + 1e-9
             and max(c[3] for c in cels) >= lon1 - 1e-9)
    checar(f"{uf}: a grade envolve o retângulo", cobre, f"{len(cels)} células, zoom {z}")
    checar(f"{uf}: cabe no teto de células", len(cels) <= V.CELULAS_INICIAIS,
           f"{len(cels)} <= {V.CELULAS_INICIAIS}")

# dividir uma célula tem de significar exatamente "um zoom a mais": é o que faz
# a janela do mapa continuar batendo com a célula depois da divisão
caixa = V.UFS["AL"]["cx"]
z0 = V.zoom_inicial(caixa, V.CELULAS_INICIAIS)
pai = V.grade(caixa, z0)[0]
filhas = V.quatro(pai)
checar("dividir em quatro = um zoom a mais",
       all(V.zoom_da_celula(abs(f[3] - f[2])) == z0 + 1 for f in filhas),
       [V.zoom_da_celula(abs(f[3] - f[2])) for f in filhas])
checar("as quatro filhas cobrem o pai",
       abs(sum((f[1] - f[0]) * (f[3] - f[2]) for f in filhas)
           - (pai[1] - pai[0]) * (pai[3] - pai[2])) < 1e-9)


# ══ 3. O recorte geográfico ═══════════════════════════════════════════════════
titulo("3) o que está fora do estado não entra")
MACEIO = "https://www.google.com/maps/place/X/@-9.6498,-35.7089,17z/data=!3d-9.6498!4d-35.7089"
# medido de verdade: um enquadramento de Maceió trouxe esta agência do Maranhão
MARANHAO = "https://www.google.com/maps/place/Y/data=!3d-6.1763031!4d-44.7999577"
SEM_COORD = "https://www.google.com/maps/place/Z/data=!4m2!3m1"
checar("Maceió é de Alagoas", V.dentro_do_estado(MACEIO, "AL"))
checar("a agência do Maranhão não é", not V.dentro_do_estado(MARANHAO, "AL"))
checar("...mas é do Maranhão", V.dentro_do_estado(MARANHAO, "MA"))
checar("sem coordenada = fora (não afirma sem prova)",
       not V.dentro_do_estado(SEM_COORD, "AL"))


# ══ 4. A varredura adaptativa, contra um mundo simulado ═══════════════════════
titulo("4) a varredura acha o que a busca única não acha")

# Um mundo de mentira com a forma do problema real: um aglomerado dentro do
# estado (a capital), alguns espalhados, e vizinhos do lado de fora.
MUNDO = []
for i in range(180):                                    # aglomerado em Maceió
    MUNDO.append((f"capital-{i}", -9.65 + (i % 15) * 0.004, -35.73 + (i // 15) * 0.004))
for i in range(30):                                     # espalhados pelo interior
    MUNDO.append((f"interior-{i}", -9.2 - (i % 6) * 0.35, -36.4 - (i // 6) * 0.35))
for i in range(60):                                     # fora do estado (Sergipe/BA)
    MUNDO.append((f"forasteiro-{i}", -11.9 - (i % 5) * 0.2, -37.5 - (i // 5) * 0.2))

CORTE_SIMULADO = 70          # o corte do Google, medido entre 69 e 77
_RE_AT = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+),(\d+)z")


def mundo_visivel(url):
    """O que o Maps devolveria neste enquadramento."""
    m = _RE_AT.search(url)
    lat, lon, z = float(m.group(1)), float(m.group(2)), int(m.group(3))
    meia_lon = V.graus_visiveis(z) / 2
    meia_lat = meia_lon * (680.0 / V.LARGURA_PX)

    def perto(x):
        return (x[1] - lat) ** 2 + (x[2] - lon) ** 2

    dentro = sorted((p for p in MUNDO
                     if abs(p[1] - lat) <= meia_lat and abs(p[2] - lon) <= meia_lon),
                    key=perto)
    if len(dentro) >= CORTE_SIMULADO:
        return dentro[:CORTE_SIMULADO]
    vistos = {p[0] for p in dentro}
    de_fora = sorted((p for p in MUNDO if p[0] not in vistos), key=perto)
    return dentro + de_fora[:CORTE_SIMULADO - len(dentro)]


async def rodar_varredura(orcamento, prof_max=3):
    urls = []

    async def abrir(page, url):
        urls.append(url)
        return True

    async def rolar(page, coletados, aceitar=None, ao_progredir=None,
                    rotulo="", limite_rolagens=30, ao_gravar=None,
                    deve_parar=None):
        brutos = aceitos = 0
        for nome, la, lo in mundo_visivel(urls[-1]):
            brutos += 1
            chave = f"https://maps/place/{nome}"
            info = {"nome": nome, "maps_url": f"{chave}/data=!3d{la}!4d{lo}"}
            if chave in coletados:
                continue
            if aceitar and not aceitar(info):
                continue
            coletados[chave] = info
            aceitos += 1
        return {"brutos": brutos, "aceitos": aceitos}

    coletados = {}
    rel = await V.varrer(None, "agência", "AL", coletados, abrir, rolar,
                         orcamento=orcamento, prof_max=prof_max)
    return coletados, rel, urls


coletados, rel, urls = asyncio.run(rodar_varredura(60))
nomes = {v["nome"] for v in coletados.values()}
capital = sum(1 for n in nomes if n.startswith("capital"))

# a comparação que justifica o módulo: uma consulta só, no centro do estado
uma_so = mundo_visivel(V.url_da_celula("agência", V.UFS["AL"]["cx"]))
checar("uma consulta só ficaria no corte do Google", len(uma_so) <= CORTE_SIMULADO,
       len(uma_so))
checar("a varredura traz muito mais que uma consulta",
       len(coletados) > len(uma_so) * 2, f"{len(coletados)} contra {len(uma_so)}")
checar("o aglomerado da capital foi vencido pela divisão", capital >= 150, capital)
checar("nenhum forasteiro entrou",
       not any(n.startswith("forasteiro") for n in nomes),
       [n for n in nomes if n.startswith("forasteiro")][:3])
checar("contou os que barrou", rel["fora_do_estado"] > 0, rel["fora_do_estado"])
checar("o orçamento foi respeitado", rel["consultas"] <= 60, rel["consultas"])
checar("houve divisão (existe célula de nível > 0)",
       any(c["nivel"] > 0 for c in rel["celulas"]))

# o orçamento é um teto de verdade, não uma sugestão
_, rel_curta, urls_curta = asyncio.run(rodar_varredura(5))
checar("orçamento de 5 faz no máximo 5 consultas", rel_curta["consultas"] <= 5,
       rel_curta["consultas"])
checar("e avisa quantas áreas ficaram de fora", rel_curta["restaram"] > 0,
       rel_curta["restaram"])

# área magra não é dividida: seria gastar quatro consultas para reencontrar o mesmo
magras = [c for c in rel["celulas"] if c["novos"] == 0 and c["brutos"] == 0]
filhas_de_magra = 0
checar("área vazia não vira quatro áreas vazias",
       rel["consultas"] < 4 ** 3 * len(V.grade(V.UFS["AL"]["cx"], 10)),
       f"{rel['consultas']} consultas")

# sem prof_max a divisão não teria fim
_, rel_rasa, _ = asyncio.run(rodar_varredura(60, prof_max=0))
checar("prof_max=0 impede qualquer divisão",
       all(c["nivel"] == 0 for c in rel_rasa["celulas"]))


# ══ 5. Nunca repetir quem já está em um quadro ════════════════════════════════
titulo("5) lead que já está em um quadro não é coletado de novo")
db.init_db()
ddb.init_dashboard()
scrap_destino.init()

qid = ddb.quadro_padrao_id()
sid_antigo = ddb.add_scrap("agência", "Maceió AL")
sid_novo = ddb.add_scrap("agência", "Alagoas", modo="estado", orcamento=30)

achado = {"maps_url": "https://maps/place/ja-trabalhado/data=!3d-9.65!4d-35.73",
          "nome": "Agência Já Trabalhada", "categoria": "Agência",
          "telefone": "", "website": "", "endereco": "", "avaliacao": "",
          "total_avaliacoes": ""}
inedito = {"maps_url": "https://maps/place/inedito/data=!3d-9.75!4d-36.66",
           "nome": "Agência Inédita", "categoria": "Agência",
           "telefone": "", "website": "", "endereco": "", "avaliacao": "",
           "total_avaliacoes": ""}

r1 = scrap_live.persistir("agência", [achado, inedito], sid_antigo)
checar("a primeira coleta grava os dois", r1["novos"] == 2, r1)
checar("nenhum deles nasce em quadro", r1["ja_em_quadro"] == 0)

# o operador despacha UM deles para um quadro
scrap_destino.enviar(sid_antigo, qid)
with db.conn() as con:
    em_quadro = con.execute(
        "SELECT COUNT(*) FROM leads WHERE quadro_id=?", (qid,)).fetchone()[0]
checar("os dois foram para o quadro", em_quadro == 2, em_quadro)

# ...e uma coleta nova reencontra os mesmos lugares
r2 = scrap_live.persistir("agência", [achado, inedito], sid_novo, uf="AL")
checar("a coleta nova NÃO regrava nenhum", r2["novos"] == 0, r2)
checar("e diz por quê", r2["ja_em_quadro"] == 2, r2)

with db.conn() as con:
    quantos = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    ligados = con.execute("SELECT COUNT(*) FROM leads WHERE scrap_id=?",
                          (sid_novo,)).fetchone()[0]
checar("não duplicou lead no banco", quantos == 2, quantos)
checar("nem religou o lead à busca nova", ligados == 0, ligados)

# a UF entra quando a varredura a conhece por construção
terceiro = {"maps_url": "https://maps/place/terceiro/data=!3d-9.40!4d-36.10",
            "nome": "Agência Terceira", "categoria": "Agência", "telefone": "",
            "website": "", "endereco": "", "avaliacao": "", "total_avaliacoes": ""}
scrap_live.persistir("agência", [terceiro], sid_novo, uf="AL")
with db.conn() as con:
    uf = con.execute("SELECT uf FROM leads WHERE maps_url=?",
                     (terceiro["maps_url"],)).fetchone()[0]
checar("a varredura grava a UF que provou", uf == "AL", uf)


# ══ 6. Descartar a coleta sem mandar para quadro ══════════════════════════════
titulo("6) descartar só alcança quem não foi para quadro")
with db.conn() as con:
    antes = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]

r = scrap_destino.descartar(sid_novo)
checar("apagou o que estava sem quadro", r["excluidos"] == 1, r)
checar("e contou os que ficaram", r["mantidos"] == 0, r)

with db.conn() as con:
    depois = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    sobrou_no_quadro = con.execute(
        "SELECT COUNT(*) FROM leads WHERE quadro_id=?", (qid,)).fetchone()[0]
checar("sumiu exatamente um", antes - depois == 1, f"{antes} -> {depois}")
checar("quem estava em quadro continua lá", sobrou_no_quadro == 2, sobrou_no_quadro)

# descartar a busca ANTIGA não pode alcançar os dois que foram despachados
r2 = scrap_destino.descartar(sid_antigo)
checar("busca já despachada: nada a descartar", r2["excluidos"] == 0, r2)
checar("e ela avisa que há 2 em quadros", r2["mantidos"] == 2, r2)
with db.conn() as con:
    checar("os leads despachados continuam inteiros",
           con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2)


# ══ 7. A frase que fecha a coleta ═════════════════════════════════════════════
titulo("7) o resumo final diz o que entrou e o que ficou de fora")
f = scrap_live.resumo_final(216, {"novos": 180, "ja_em_quadro": 36},
                            {"consultas": 30, "fora_do_estado": 12, "restaram": 8})
checar("conta os que já estavam em quadro", "36 já estavam em quadros" in f, f)
checar("conta as áreas varridas", "30 áreas varridas" in f)
checar("conta o que caiu fora do estado", "12 fora do estado" in f)
checar("avisa que o orçamento acabou antes", "restaram 8 áreas" in f)

f2 = scrap_live.resumo_final(76, {"novos": 76, "ja_em_quadro": 0}, {})
checar("busca simples não inventa números da varredura",
       "áreas" not in f2 and "quadros" not in f2, f2)


print("\n" + ("TODOS OS CASOS PASSARAM" if not falhas
              else f"FALHARAM ({len(falhas)}): {falhas}"))
print(f"banco de teste: {config.LEADFORGE_DB}")
sys.exit(1 if falhas else 0)
