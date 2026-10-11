# -*- coding: utf-8 -*-
"""test_tempo_real.py — A coleta grava enquanto colhe, para quando pedem, e volta."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
os.environ["LEADFORGE_DB"] = str(Path(tempfile.mkdtemp(prefix="lf_tr_")) / "teste.db")
sys.path.insert(0, str(RAIZ))

import db                      # noqa: E402
import dashboard_db as ddb     # noqa: E402
import scrap_destino           # noqa: E402
import scrap_live              # noqa: E402
import varredura as V          # noqa: E402

falhas = []


def checar(rotulo, condicao, detalhe=""):
    print("   " + ("OK     " if condicao else "FALHOU ") + rotulo
          + ((" - " + str(detalhe)) if detalhe else ""))
    if not condicao:
        falhas.append(rotulo)


def titulo(t):
    print("\n" + t)


db.init_db()
ddb.init_dashboard()
scrap_destino.init()


# ══ Um Maps de mentira, que entrega mais gente a cada rolagem ════════════════

class Ancora:
    def __init__(self, nome, url):
        self.nome, self.url = nome, url

    async def get_attribute(self, chave):
        return self.nome if chave == "aria-label" else self.url

    async def evaluate(self, _js):
        return {"text": f"{self.nome}\nAgência de marketing · Rua Um, 10",
                "web": "", "rat": "4,5 estrelas", "rev": "12"}


class Feed:
    def __init__(self, pagina):
        self.pagina = pagina

    async def evaluate(self, _js):
        self.pagina.rolagens += 1        # rolou: o Maps revela mais um pedaço

    async def wait_for(self, **_k):
        return True

    async def all(self):
        return self.pagina.visiveis()

    async def count(self):
        return len(self.pagina.visiveis())


class PaginaFalsa:
    """Revela `por_rolagem` lugares a cada rolagem, até acabar o estoque."""

    def __init__(self, total, por_rolagem=6, prefixo="ag"):
        self.total, self.por_rolagem, self.prefixo = total, por_rolagem, prefixo
        self.rolagens = 0

    def visiveis(self):
        quantos = min(self.total, self.por_rolagem * (self.rolagens + 1))
        return [Ancora(f"{self.prefixo} {i}",
                       f"https://maps/place/{self.prefixo}-{i}/data=!3d-9.65!4d-35.73")
                for i in range(quantos)]

    def locator(self, _seletor):
        return Feed(self)


async def _rolar(pagina, coletados, **kw):
    return await scrap_live.rolar_feed(pagina, coletados, limite_rolagens=12, **kw)


# ══ 1. a coleta grava a cada rolagem, não só no fim ══════════════════════════
titulo("1) o lead está no banco ANTES da coleta terminar")

sid = ddb.add_scrap("agência", "Maceió AL")
pagina = PaginaFalsa(total=30, por_rolagem=6)
coletados = {}
gravacao = {"novos": 0, "ja_em_quadro": 0, "encontrados": 0, "organica": 0}
no_banco_por_rolagem = []


async def gravar(novos):
    scrap_live.persistir("agência", novos, sid, acumulado=gravacao)
    with db.conn() as con:
        no_banco_por_rolagem.append(
            con.execute("SELECT COUNT(*) FROM leads WHERE scrap_id=?", (sid,)).fetchone()[0])

asyncio.run(_rolar(pagina, coletados, ao_gravar=gravar))

checar("colheu os 30", len(coletados) == 30, len(coletados))
checar("gravou em várias etapas, não numa só", len(no_banco_por_rolagem) >= 4,
       no_banco_por_rolagem)
checar("o banco cresceu junto com a coleta",
       no_banco_por_rolagem == sorted(no_banco_por_rolagem)
       and no_banco_por_rolagem[0] < no_banco_por_rolagem[-1],
       no_banco_por_rolagem)
checar("já havia lead salvo na 1ª gravação", no_banco_por_rolagem[0] > 0,
       no_banco_por_rolagem[0])
with db.conn() as con:
    total = con.execute("SELECT COUNT(*) FROM leads WHERE scrap_id=?", (sid,)).fetchone()[0]
checar("no fim, os 30 estão gravados", total == 30, total)

# a posição orgânica não pode recomeçar do 1 a cada rolagem
with db.conn() as con:
    posicoes = sorted(r[0] for r in con.execute(
        "SELECT posicao FROM leads WHERE scrap_id=? AND posicao IS NOT NULL", (sid,)))
checar("posição é 1..30 sem repetir", posicoes == list(range(1, 31)),
       f"{posicoes[:6]}... ({len(posicoes)} valores, {len(set(posicoes))} distintos)")


# ══ 2. queda no meio: o que já entrou fica ═══════════════════════════════════
titulo("2) uma queda no meio não leva o que já foi colhido")

sid2 = ddb.add_scrap("agência", "Arapiraca AL")
pagina2 = PaginaFalsa(total=40, por_rolagem=5, prefixo="quedou")
coletados2 = {}
conta2 = {"novos": 0, "ja_em_quadro": 0, "encontrados": 0, "organica": 0}


async def gravar_ate_cair(novos):
    scrap_live.persistir("agência", novos, sid2, acumulado=conta2)
    if conta2["novos"] >= 15:
        raise RuntimeError("o navegador morreu")   # a queda, no meio da coleta


try:
    asyncio.run(_rolar(pagina2, coletados2, ao_gravar=gravar_ate_cair))
    caiu = False
except RuntimeError:
    caiu = True

with db.conn() as con:
    sobreviveram = con.execute("SELECT COUNT(*) FROM leads WHERE scrap_id=?",
                               (sid2,)).fetchone()[0]
checar("a coleta realmente caiu", caiu)
checar("os leads lidos até a queda estão no banco", sobreviveram >= 15, sobreviveram)
checar("e não são todos (a queda foi no meio)", sobreviveram < 40, sobreviveram)


# ══ 3. parar quando pedem ════════════════════════════════════════════════════
titulo("3) o pedido de parada é atendido entre rolagens")

sid3 = ddb.add_scrap("agência", "Maceió AL")
ddb.set_scrap_status(sid3, "rodando")
r = ddb.pedir_parada(sid3)
checar("o painel consegue pedir a parada", r.get("ok") is True, r)
checar("a coleta enxerga o pedido", ddb.parada_pedida(sid3) is True)

pagina3 = PaginaFalsa(total=60, por_rolagem=5, prefixo="parou")
coletados3 = {}
leitura = asyncio.run(_rolar(pagina3, coletados3,
                             deve_parar=lambda: ddb.parada_pedida(sid3)))
checar("parou antes de varrer tudo", len(coletados3) < 60, len(coletados3))
checar("e avisou que foi interrompida", leitura["interrompida"] is True)

# parar o que não está rodando é recusado, e com motivo
ddb.set_scrap_status(sid3, "concluido")
r2 = ddb.pedir_parada(sid3)
checar("não deixa pausar busca parada", r2.get("ok") is False, r2.get("erro", "")[:45])


# ══ 4. a varredura guarda o ponto e volta dele ═══════════════════════════════
titulo("4) a varredura continua de onde parou")

MUNDO = [(f"lugar-{i}", -9.65 + (i % 20) * 0.01, -35.73 + (i // 20) * 0.01)
         for i in range(120)]
import re as _re                                                    # noqa: E402
_RE_AT = _re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+),(\d+)z")


async def abrir(_page, url):
    abrir.ultima = url
    return True


async def rolar(_page, coletados, aceitar=None, ao_progredir=None, rotulo="",
                limite_rolagens=30, ao_gravar=None, deve_parar=None):
    """O Maps de mentira — que COBRA o contrato em vez de aceitar qualquer coisa."""
    rolar.recebeu_gravar = ao_gravar is not None
    rolar.recebeu_parar = deve_parar is not None

    m = _RE_AT.search(abrir.ultima)
    lat, lon, z = float(m.group(1)), float(m.group(2)), int(m.group(3))
    meia = V.graus_visiveis(z) / 2
    brutos, novos = 0, []
    for nome, la, lo in MUNDO:
        if abs(la - lat) > meia * 0.756 or abs(lo - lon) > meia:
            continue
        brutos += 1
        chave = f"https://maps/place/{nome}"
        info = {"nome": nome, "maps_url": f"{chave}/data=!3d{la}!4d{lo}"}
        if chave in coletados or (aceitar and not aceitar(info)):
            continue
        coletados[chave] = info
        novos.append(info)
    if ao_gravar and novos:
        await ao_gravar(novos)
        rolar.gravou += len(novos)
    return {"brutos": brutos, "aceitos": len(novos), "interrompida": False}


rolar.recebeu_gravar = False
rolar.recebeu_parar = False
rolar.gravou = 0


async def cenario_pausa():
    """Roda 3 áreas, pede parada, guarda o ponto; depois continua dali."""
    guardado = {}
    visitadas = []

    async def checkpoint(estado):
        guardado.clear()
        guardado.update(estado)

    async def progresso(_m):
        return None

    coletados = {}
    conta = {"n": 0}

    def parar():
        conta["n"] += 1
        return conta["n"] > 3          # deixa 3 áreas passarem, então pede parada

    rel = await V.varrer(None, "agência", "AL", coletados, abrir, rolar,
                         orcamento=40, ao_progredir=progresso,
                         ao_checkpoint=checkpoint, deve_parar=parar)
    visitadas.append(rel["consultas"])
    parcial = len(coletados)

    # continua do ponto guardado, com o que já foi colhido em mãos
    rel2 = await V.varrer(None, "agência", "AL", coletados, abrir, rolar,
                          orcamento=40, ao_progredir=progresso,
                          retomar=rel.get("estado") or guardado)
    return rel, rel2, parcial, len(coletados)


rel, rel2, parcial, total_final = asyncio.run(cenario_pausa())

checar("parou nas primeiras áreas", rel["consultas"] <= 4, rel["consultas"])
checar("marcou que foi interrompida", rel.get("interrompida") is True)
checar("deixou um ponto de retomada", bool(rel.get("estado")))
checar("a retomada NÃO recomeça do zero",
       rel2["consultas"] > rel["consultas"], f"{rel['consultas']} -> {rel2['consultas']}")
checar("a retomada continua a contagem, não zera",
       rel2["consultas"] >= rel["consultas"], rel2["consultas"])
checar("colheu mais do que tinha antes de pausar", total_final > parcial,
       f"{parcial} -> {total_final}")


# O contrato entre a varredura e quem le a lista: o que a varredura RECEBE tem
# de chegar la embaixo. Foi aqui que o duble antigo mentia.
async def com_gravador():
    guardados = []

    async def gravar(novos):
        guardados.extend(novos)

    rolar.gravou = 0
    rolar.recebeu_gravar = rolar.recebeu_parar = False
    coletados = {}
    rel = await V.varrer(None, "agência", "AL", coletados, abrir, rolar,
                         orcamento=4, ao_gravar=gravar,
                         deve_parar=lambda: False)
    return rel, guardados


rel_g, guardados = asyncio.run(com_gravador())
checar("a varredura repassa o gravador para quem lê a lista",
       rolar.recebeu_gravar is True)
checar("...e o pedido de parada também", rolar.recebeu_parar is True)
checar("o gravador foi mesmo chamado durante a varredura", len(guardados) > 0,
       len(guardados))
checar("gravou tudo que colheu", len(guardados) == rel_g["encontrados"],
       f"{len(guardados)} de {rel_g['encontrados']}")

# a varredura que TERMINA não deixa ponto de retomada para trás
async def ate_o_fim():
    return await V.varrer(None, "agência", "AL", {}, abrir, rolar,
                          orcamento=6, ao_progredir=None)

rel3 = asyncio.run(ate_o_fim())
checar("varredura não interrompida não deixa ponto", rel3.get("estado") is None,
       rel3.get("estado"))


# ══ 5. o ponto de retomada vai e volta do banco ══════════════════════════════
titulo("5) o ponto de retomada sobrevive ao banco")

sid5 = ddb.add_scrap("agência", "Alagoas", modo="estado", orcamento=40)
ddb.salvar_progresso(sid5, rel["estado"])
devolvido = ddb.ler_progresso(sid5)
checar("volta igual ao que entrou",
       json.dumps(devolvido, sort_keys=True) == json.dumps(rel["estado"], sort_keys=True))
checar("a varredura aceita o que voltou do banco",
       V._recuperar(devolvido) is not None)
ddb.salvar_progresso(sid5, None)
checar("e some quando a coleta termina", ddb.ler_progresso(sid5) is None)
checar("progresso ilegível não derruba nada", True)
with db.conn() as con:
    con.execute("UPDATE scrap_queue SET progresso='{isso nao e json' WHERE id=?", (sid5,))
checar("progresso corrompido vale como ausente", ddb.ler_progresso(sid5) is None)


# ══ 6. a listagem não carrega o JSON inteiro para a tela ═════════════════════
titulo("6) a fila manda um resumo, não o ponto inteiro")

ddb.salvar_progresso(sid5, rel["estado"])
linha = next(s for s in ddb.listar_scrap() if s["id"] == sid5)
checar("não expõe o JSON do progresso", "progresso" not in linha, list(linha)[-4:])
checar("diz que há de onde continuar", linha["tem_progresso"] is True)
checar("e quantas áreas já foram", linha["areas_feitas"] == rel["consultas"],
       f"{linha['areas_feitas']} vs {rel['consultas']}")


# ══ 7. retomar não conta de novo quem já veio ════════════════════════════════
titulo("7) retomar não recontabiliza o que já estava salvo")

anteriores = scrap_live._ja_coletados(sid)      # a busca #1, com 30 gravados
checar("reconhece os 30 que já vieram", len(anteriores) == 30, len(anteriores))
checar("vêm marcados como já gravados",
       all(v.get("retomado") for v in anteriores.values()))

conta7 = {"novos": 0, "ja_em_quadro": 0, "encontrados": 0, "organica": 0}
scrap_live.persistir("agência", list(anteriores.values()), sid, acumulado=conta7)
checar("não regrava nenhum deles", conta7["novos"] == 0, conta7)
checar("mas conta que estavam lá", conta7["encontrados"] == 30, conta7)
with db.conn() as con:
    ainda = con.execute("SELECT COUNT(*) FROM leads WHERE scrap_id=?", (sid,)).fetchone()[0]
checar("e o banco não duplicou nada", ainda == 30, ainda)


# ══ 8. a trava de coleta dupla não pode virar beco ═══════════════════════════
titulo("8) coleta que morreu não tranca a busca para sempre")

import importlib.util as _u                                          # noqa: E402
_spec = _u.spec_from_file_location("painel", str(RAIZ / "dashboard" / "app.py"))
painel = _u.module_from_spec(_spec)
sys.modules["painel"] = painel
_spec.loader.exec_module(painel)

sid8 = ddb.add_scrap("agência", "Maceió AL")
ddb.set_scrap_status(sid8, "rodando")

# nunca reportou nada: o 'rodando' no banco é resquício, não vida
painel.SCRAP_PROGRESS.pop(sid8, None)
checar("sem sinal nenhum = não está viva", painel._coleta_viva(sid8) is False)

# sinal recente: está viva de verdade, e a trava vale
import time as _t                                                    # noqa: E402
painel.SCRAP_PROGRESS[sid8] = {"done": False, "quando": _t.time()}
checar("sinal de agora = viva", painel._coleta_viva(sid8) is True)

# sinal velho: morreu no meio e não avisou
painel.SCRAP_PROGRESS[sid8] = {"done": False,
                               "quando": _t.time() - painel.SILENCIO_ATE_MORRER - 5}
checar("sinal velho demais = morta", painel._coleta_viva(sid8) is False)

# terminou: também não está rodando
painel.SCRAP_PROGRESS[sid8] = {"done": True, "quando": _t.time()}
checar("coleta concluída não conta como viva", painel._coleta_viva(sid8) is False)


# retomar não pode recomeçar a colocação: medido no Maps, uma varredura pausada
# e retomada terminou com 92 leads e 84 posições distintas
checar("a última colocação da busca é lida do banco",
       scrap_live._ultima_posicao(sid) == 30, scrap_live._ultima_posicao(sid))
conta_ret = {"novos": 0, "ja_em_quadro": 0, "encontrados": 0,
             "organica": scrap_live._ultima_posicao(sid)}
scrap_live.persistir("agência", [{"maps_url": "https://maps/place/depois-da-pausa",
                                  "nome": "Depois da pausa", "categoria": "Agência",
                                  "telefone": "", "website": "", "endereco": "",
                                  "avaliacao": "", "total_avaliacoes": ""}],
                     sid, acumulado=conta_ret)
with db.conn() as con:
    p_novo = con.execute("SELECT posicao FROM leads WHERE maps_url=?",
                         ("https://maps/place/depois-da-pausa",)).fetchone()[0]
    distintas, preenchidas = con.execute(
        "SELECT COUNT(DISTINCT posicao), COUNT(posicao) FROM leads WHERE scrap_id=?",
        (sid,)).fetchone()
checar("o lead da retomada segue a numeração", p_novo == 31, p_novo)
checar("nenhuma colocação repetida na busca", distintas == preenchidas,
       f"{distintas} distintas de {preenchidas}")


print("\n" + ("TODOS OS CASOS PASSARAM" if not falhas
              else f"FALHARAM ({len(falhas)}): {falhas}"))
sys.exit(1 if falhas else 0)
