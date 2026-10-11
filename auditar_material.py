"""auditar_material.py — Gera o PDF de vários leads e procura defeito no texto."""

import argparse
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
OUT = RAIZ / "out"

try:
    from pypdf import PdfReader
except ImportError:                                     # pragma: no cover
    from PyPDF2 import PdfReader                        # type: ignore


def _texto_do_pdf(caminho: Path) -> str:
    try:
        bruto = " ".join((p.extract_text() or "") for p in PdfReader(str(caminho)).pages)
    except Exception as e:
        return f"__ERRO_LEITURA__ {e}"
    return re.sub(r"\s+", " ", bruto).strip()


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn").lower()


def _digitos(s: str) -> str:
    return re.sub(r"\D", "", s or "")


# ── Checagens ─────────────────────────────────────────────────────────────────
# Cada função recebe (texto, lead, ctx) e devolve lista de problemas (str).
# ctx traz dados já apurados: cidade do lead, referência escolhida, etc.

def c_travessao(texto, lead, ctx):
    """Decisão do usuário: proibido travessão longo. Há sanitizador; isto confere."""
    achados = set()
    for m in re.finditer(r"[—–]", texto):
        antes = texto[m.start() - 1] if m.start() else " "
        depois = texto[m.end()] if m.end() < len(texto) else " "
        if antes.isdigit() and depois.isdigit():
            continue                       # 397–597: intervalo, permitido
        achados.add(m.group(0))
    return [f"travessão longo proibido no texto: {' '.join(sorted(achados))}"] if achados else []


def c_cidade_errada(texto, lead, ctx):
    """PDF de clínica de Manaus dizia 'em Arapiraca' (e vice-versa)."""
    import cidade as cid
    minha = (ctx.get("cidade") or "")
    erros = []
    for nome in {c["nome"] for c in cid.CIDADES.values()}:
        if nome == minha:
            continue
        if re.search(rf"\b{re.escape(nome)}\b", texto):
            # o nome da própria clínica pode conter a cidade ("Odonto Manaus")
            if _sem_acento(nome) in _sem_acento(lead.get("nome") or ""):
                continue
            # a referência pode carregar a cidade no nome dela
            if _sem_acento(nome) in _sem_acento(ctx.get("ref_nome") or ""):
                continue
            erros.append(f"cita '{nome}' mas o lead é de {minha or '(sem cidade)'}")
    return erros


def c_referencia_de_outra_cidade(texto, lead, ctx):
    """'uma referência da sua região' apontando para clínica de outro estado."""
    if not ctx.get("ref_id"):
        return []
    if (ctx.get("ref_cidade") or "") != (ctx.get("cidade") or ""):
        return [f"referência {ctx['ref_id']} é de {ctx.get('ref_cidade')} "
                f"e o lead é de {ctx.get('cidade')}"]
    return []


def c_referencia_atras(texto, lead, ctx):
    """Defeito 1: referência ATRÁS do lead e o texto dizendo que está à frente."""
    if not ctx.get("ref_id"):
        return []
    minha, dela = lead.get("posicao"), ctx.get("ref_posicao")
    if minha is None or dela is None or dela <= minha:
        return []

    ref_curto = _sem_acento((ctx.get("ref_nome") or "").split(" - ")[0].split(" | ")[0])[:22]
    erros = []
    for frase in re.split(r"(?<=[.!?])\s+", texto):
        alvo = _sem_acento(frase)
        fala_da_ref = (ref_curto and ref_curto in alvo) or "referencia" in alvo
        if not fala_da_ref:
            continue
        if re.search(r"\b(a frente|na frente|acima de voce|ultrapass)", alvo):
            erros.append(f"referência está em {dela}º, atrás do lead ({minha}º), "
                         f"mas a frase diz que está à frente: '{frase[:80]}'")
    return erros


_FRASES_DE_PERDA = (
    r"que n[ãa]o entra no caixa",
    r"deixa de (?:entrar|receber)",
    r"deixa de faturar",
)


def c_valor_zerado(texto, lead, ctx):
    """Defeito 18: clínica em 1º lugar com o documento inteiro em 'R$ 0'."""
    erros = []
    tem_perda = ctx.get("perda_apurada")

    frases = [m for p in _FRASES_DE_PERDA for m in re.finditer(p, texto, re.I)]
    if tem_perda is False and frases:
        erros.append("lead sem perda apurada, mas o material fala em dinheiro "
                     f"que deixa de entrar ({len(frases)}x)")

    if tem_perda:
        for m in frases:
            # só o valor que a frase qualifica, que vem ANTES dela ("soma R$ 9.600 que não
            # entra no caixa").
            antes = re.findall(r"R\$\s*([\d.,]+)", texto[max(0, m.start() - 80):m.start()])
            if antes and re.fullmatch(r"0([.,]00)?", antes[-1]):
                erros.append(f"perda apurada, mas o valor citado é 'R$ {antes[-1]}'")
    return erros


def c_markdown(texto, lead, ctx):
    """Defeito 4: markdown vazando no corpo."""
    erros = []
    if re.search(r"\*\*\w", texto):
        erros.append("negrito markdown (**) no texto")
    if re.search(r"(^|\s)#{1,4}\s", texto):
        erros.append("cabeçalho markdown (#) no texto")
    return erros


def c_nega_personalizacao(texto, lead, ctx):
    """Defeito 20: 'este material não é um modelo pronto'."""
    alvo = _sem_acento(texto)
    frases = ["nao e um modelo pronto", "nao se trata de um modelo",
              "nao e generico", "nao e um documento generico"]
    return [f"nega personalização: '{f}'" for f in frases if f in alvo]


def c_automacao_fora_do_dominio(texto, lead, ctx):
    """
    Regra de produto: automação de WhatsApp/Instagram só existe no nível 3. Era o que o
    material mais errava.
    """
    import config
    dominio = getattr(config, "NIVEL_COM_AUTOMACAO", 3)
    marcas = [(m.start(), int(m.group(1)))
              for m in re.finditer(r"N[íi]vel\s+([123])", texto)]
    erros = []
    for m in re.finditer(r"automa[çc][ãa]o de (?:whatsapp|instagram)", texto, re.I):
        anteriores = [n for pos, n in marcas if pos < m.start()]
        nivel_da_fatia = anteriores[-1] if anteriores else None
        if nivel_da_fatia is None:
            # fora da tabela de planos: só vale se o plano recomendado for o 3
            if ctx.get("nivel") not in (None, dominio):
                erros.append(f"automação citada fora da tabela de planos, com "
                             f"plano recomendado nível {ctx.get('nivel')}")
        elif nivel_da_fatia != dominio:
            erros.append(f"automação citada sob o Nível {nivel_da_fatia}, "
                         f"e ela só existe no Nível {dominio}")
    return erros


def c_telefone_da_clinica(texto, lead, ctx):
    """Defeito 3: assinar com o telefone da própria clínica destinatária."""
    tel = _digitos(lead.get("telefone"))[-8:]
    if len(tel) < 8:
        return []
    if tel in _digitos(texto):
        return [f"o telefone da própria clínica (...{tel}) aparece no material"]
    return []


def c_regiao_e_endereco(texto, lead, ctx):
    """Defeito 19: 'quem procura dentista em 46 Rua São Francisco'."""
    erros = []
    # o serviço entra em minúscula ("quem procura dentista em X").
    for m in re.finditer(r"quem procura\s+[a-zà-ÿ][^.]{0,70}? em ([^.,;]{2,45})", texto):
        alvo = m.group(1).strip()
        if re.search(r"\d", alvo):
            erros.append(f"região com número: 'em {alvo}'")
        elif re.search(r"\b(rua|av\.?|avenida|travessa|pra[çc]a|sala|andar|"
                       r"ed[íi]f|shopping|bloco|loja)\b", alvo, re.I):
            erros.append(f"logradouro no lugar da região: 'em {alvo}'")
    return erros


def c_placeholder(texto, lead, ctx):
    """Campo de template que não foi substituído."""
    erros = []
    for pad in (r"\{\w+\}", r"\bTODO\b", r"_{3,}", r"\bXXX+\b", r"\bNone\b", r"\bnan\b"):
        m = re.search(pad, texto)
        if m:
            erros.append(f"placeholder não substituído: '{m.group(0)}'")
    return erros


def c_nome_repetido(texto, lead, ctx):
    """Defeito 9: nome do remetente/clínica repetido a ponto de soar automático."""
    curto = (lead.get("nome") or "").split(" - ")[0].split(" | ")[0].strip()
    if len(curto) < 6:
        return []
    n = len(re.findall(re.escape(curto), texto, re.I))
    return [f"nome da clínica repetido {n}x no material"] if n > 12 else []


def c_promessa_de_retorno(texto, lead, ctx):
    """Decisão do usuário: a copy não promete retorno, ROI nem payback."""
    alvo = _sem_acento(texto)
    erros = []
    for termo, rotulo in (("roi", "ROI"), ("payback", "payback"),
                          ("retorno garantido", "retorno garantido"),
                          ("garantimos", "garantia de resultado")):
        if re.search(rf"\b{re.escape(termo)}\b", alvo):
            erros.append(f"promessa proibida: {rotulo}")
    return erros


def c_afirma_sem_medir(texto, lead, ctx):
    """Afirmar escassez de avaliações quando a contagem não foi capturada."""
    tem_nota = lead.get("avaliacao_num") is not None
    contagem = lead.get("total_avaliacoes_num") or 0
    if not tem_nota or contagem:
        return []
    alvo = _sem_acento(texto)
    frases = ["poucas avaliacoes visiveis", "quase nao tem",
              "poucas avaliacoes", "nao tem avaliacoes"]
    achadas = [f for f in frases if f in alvo]
    return [f"afirma escassez de avaliações ('{f}') com a contagem não capturada"
            for f in achadas]


def _frases_do_html(html: str):
    """Os blocos de texto que o gerador MANDOU imprimir, sem as tags."""
    import html as _html
    # o <head> não é impresso: o <title> ("Proposta, Clínica X") aparecia como
    # texto gerado e não renderizado, que é exatamente o que esta checagem caça
    corpo = re.split(r"<body\b[^>]*>", html, maxsplit=1)
    html = corpo[1] if len(corpo) > 1 else html
    limpo = re.sub(r"<(script|style)\b.*?</\1>", " ", html,
                   flags=re.S | re.I)
    # cada tag vira fronteira: o que sobra entre elas é um bloco de texto
    for bruto in re.split(r"<[^>]+>", limpo):
        txt = re.sub(r"\s+", " ", _html.unescape(bruto)).strip()
        if len(txt) >= 40:                 # blocos curtos são rótulo, não frase
            yield txt


def c_texto_cortado(texto, lead, ctx):
    """Texto que o gerador produziu e o PDF não mostrou inteiro."""
    html = ctx.get("html")
    if not html:
        return []
    alvo = re.sub(r"\s+", " ", texto)
    erros = []
    for frase in _frases_do_html(html):
        if frase in alvo:
            continue
        # o extrator de PDF quebra ligaduras e espaços; compara sem eles antes
        # de acusar, para não transformar ruído de extração em defeito
        compacta = re.sub(r"\s+", "", frase)
        if compacta and compacta in re.sub(r"\s+", "", alvo):
            continue
        erros.append(f"gerado mas não impresso inteiro: {frase[:70]!r}")
    return erros[:4]                      # o suficiente para localizar o bloco


def c_referencia_pior(texto, lead, ctx):
    """Atribuir à referência um número em que ela é PIOR que o lead."""
    ref_nota, ref_aval = ctx.get("ref_nota"), ctx.get("ref_avaliacoes")
    nota_l = lead.get("avaliacao_num")
    aval_l = lead.get("total_avaliacoes_num") or 0
    erros = []
    if ref_nota is not None and nota_l is not None and ref_nota < nota_l:
        if re.search(rf"tem nota {re.escape(f'{ref_nota:.1f}')}", texto):
            erros.append(f"cita a nota {ref_nota} da referência, abaixo da do lead ({nota_l})")
    if ref_aval and aval_l and ref_aval < aval_l:
        if re.search(rf"\b{ref_aval}\s+avalia", texto):
            erros.append(f"cita as {ref_aval} avaliações da referência, "
                         f"abaixo das do lead ({aval_l})")
    return erros


CHECAGENS = [
    ("travessao", c_travessao),
    ("afirma_sem_medir", c_afirma_sem_medir),
    ("texto_cortado", c_texto_cortado),
    ("referencia_pior", c_referencia_pior),
    ("cidade", c_cidade_errada),
    ("referencia_cidade", c_referencia_de_outra_cidade),
    ("referencia_posicao", c_referencia_atras),
    ("valor_zerado", c_valor_zerado),
    ("markdown", c_markdown),
    ("nega_personalizacao", c_nega_personalizacao),
    ("automacao", c_automacao_fora_do_dominio),
    ("telefone", c_telefone_da_clinica),
    ("regiao", c_regiao_e_endereco),
    ("placeholder", c_placeholder),
    ("nome_repetido", c_nome_repetido),
    ("promessa", c_promessa_de_retorno),
]


# ── Contexto e execução ───────────────────────────────────────────────────────

def _contexto(lead):
    """Dados apurados fora do PDF, para comparar com o que o PDF diz."""
    import db
    import cidade as cid
    import proposta as prop_mod

    import diagnostico as dg

    ctx = {"cidade": cid.resolver(lead).get("cidade")}
    try:
        ctx["perda_apurada"] = bool(dg.perda_apurada(lead)[3])
    except Exception:
        ctx["perda_apurada"] = None
    try:
        refs = db.leaders_by_nicho(lead["nicho"], lead["id"], limit=1)
        if refs:
            r = refs[0]
            ctx.update({"ref_id": r["id"], "ref_nome": r.get("nome"),
                        "ref_cidade": r.get("cidade"), "ref_posicao": r.get("posicao"),
                        "ref_nota": r.get("avaliacao_num"),
                        "ref_avaliacoes": r.get("total_avaliacoes_num")})
    except Exception:
        pass
    try:
        p = prop_mod.montar(lead["id"]) if hasattr(prop_mod, "montar") else None
        if isinstance(p, dict):
            ctx["nivel"] = p.get("nivel")
    except Exception:
        pass
    try:
        import proposta_designs as pd_mod
        ctx["html"] = pd_mod.montar(pd_mod.ESTILO_PADRAO, lead)
    except Exception:
        ctx["html"] = ""
    if "nivel" not in ctx:
        with db.conn() as con:
            row = con.execute("SELECT nivel FROM propostas WHERE lead_id=? "
                              "ORDER BY id DESC LIMIT 1", (lead["id"],)).fetchone()
            ctx["nivel"] = row["nivel"] if row else None
    return ctx


def _pdf_do_lead(lead) -> Path | None:
    """O PDF gerado para este lead, casando pelo nome do arquivo."""
    import re as _re
    alvo = _re.sub(r"[^a-z0-9]+", "-", _sem_acento(lead.get("nome") or "")).strip("-")
    if not alvo:
        return None
    candidatos = []
    for f in OUT.glob("*.pdf"):
        nome = _re.sub(r"[^a-z0-9]+", "-", _sem_acento(f.stem)).strip("-")
        if alvo[:28] in nome:
            candidatos.append(f)
    if not candidatos:
        return None
    return max(candidatos, key=lambda f: f.stat().st_mtime)


def gerar(lead_id: int) -> bool:
    r = subprocess.run([sys.executable, "-X", "utf8", "operar.py", "--pdf", str(lead_id)],
                       cwd=str(RAIZ), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    return "pronto:" in (r.stdout or "")


def auditar(lead_ids, gerar_antes=False):
    import db

    resultados = []
    for lid in lead_ids:
        lead = db.get_lead(lid)
        if not lead:
            resultados.append((lid, "(inexistente)", None, ["lead não encontrado"]))
            continue
        lead = dict(lead)
        if gerar_antes:
            print(f"  gerando {lid} {lead['nome'][:40]}...", flush=True)
            if not gerar(lid):
                resultados.append((lid, lead["nome"], None, ["falhou ao gerar o PDF"]))
                continue
        pdf = _pdf_do_lead(lead)
        if pdf is None:
            resultados.append((lid, lead["nome"], None, ["PDF não encontrado em out/"]))
            continue
        texto = _texto_do_pdf(pdf)
        if texto.startswith("__ERRO_LEITURA__"):
            resultados.append((lid, lead["nome"], pdf, [texto]))
            continue
        ctx = _contexto(lead)
        problemas = []
        for nome, fn in CHECAGENS:
            try:
                for p in fn(texto, lead, ctx):
                    problemas.append(f"[{nome}] {p}")
            except Exception as e:
                problemas.append(f"[{nome}] checagem falhou: {e}")
        resultados.append((lid, lead["nome"], pdf, problemas))
    return resultados


def amostra_de_casos(limite=10):
    """Leads que cobrem os casos-limite: cidade, topo x fundo, com e sem perda."""
    import db
    import diagnostico as dg
    with db.conn() as con:
        leads = [dict(r) for r in con.execute(
            "SELECT * FROM leads WHERE origem='scrap-live'")]
    vistos, escolhidos = set(), []
    for l in sorted(leads, key=lambda x: -(x.get("score") or 0)):
        try:
            tem_perda = bool(dg.perda_apurada(l)[3])
        except Exception:
            tem_perda = None
        chave = (l.get("cidade"), (l.get("posicao") or 999) <= 3, tem_perda,
                 bool(l.get("tem_site")))
        if chave in vistos:
            continue
        vistos.add(chave)
        escolhidos.append(l["id"])
        if len(escolhidos) >= limite:
            break
    return escolhidos


def auditar_copy():
    """Varre a copy do dia 0 de todos os leads atrás de contradição com o material."""
    import abordagem
    import config
    import db
    import diagnostico as dg

    with db.conn() as con:
        leads = [dict(r) for r in con.execute(
            "SELECT * FROM leads WHERE origem='scrap-live' ORDER BY id")]

    problemas = []
    for l in leads:
        pos, nota = l.get("posicao"), l.get("avaliacao_num")
        bem = bool(pos) and pos <= config.POSICAO_ALVO
        try:
            sem_perda = not dg.perda_apurada(l)[3]
        except Exception:
            sem_perda = None
        for etapa in ("dia0_ping", "dia0"):
            texto = (abordagem.gerar(l["id"], "whatsapp", etapa).get("texto") or "")
            for v in (abordagem.violacoes(texto, etapa, l)
                      + abordagem.violacoes_finas(texto, etapa, l)):
                problemas.append((l["id"], f"[{etapa}] trava: {v}"))
            if etapa != "dia0":
                continue
            perde = "perder paciente para quem aparece antes" in texto
            if bem and perde:
                problemas.append((l["id"], f"diz que perde por posição estando em {pos}º"))
            if sem_perda and perde:
                problemas.append((l["id"], "afirma perda sem perda apurada"))
            if nota is not None and nota >= config.NOTA_ALVO \
                    and "a nota está em" in texto:
                problemas.append((l["id"], f"lista a nota {nota} como ponto errado"))
    return len(leads), problemas


def main():
    ap = argparse.ArgumentParser(description="Audita o material gerado")
    ap.add_argument("--gerar", default="", help="ids separados por vírgula")
    ap.add_argument("--amostra", action="store_true",
                    help="escolhe leads que cobrem os casos-limite")
    ap.add_argument("--auditar", default="", help="ids já gerados, só auditar")
    ap.add_argument("--copy", action="store_true",
                    help="varre a copy do dia 0 de todos os leads")
    a = ap.parse_args()

    if a.copy:
        n, probs = auditar_copy()
        print("═" * 74)
        print("  AUDITORIA DA COPY (ping + abordagem)")
        print("═" * 74)
        for lid, p in probs:
            print(f"  lead {lid}: {p}")
        print("─" * 74)
        print(f"{n} leads varridos, {len(probs)} problema(s)")
        return 0 if not probs else 1

    if a.amostra:
        ids, gerar_antes = amostra_de_casos(), True
        print(f"amostra de casos-limite: {ids}\n")
    elif a.gerar:
        ids, gerar_antes = [int(i) for i in a.gerar.split(",") if i.strip()], True
    elif a.auditar:
        ids, gerar_antes = [int(i) for i in a.auditar.split(",") if i.strip()], False
    else:
        print("nada a fazer. Use --amostra, --gerar ou --auditar.")
        return 1

    resultados = auditar(ids, gerar_antes)
    print()
    print("═" * 74)
    print("  AUDITORIA DO MATERIAL")
    print("═" * 74)
    total = 0
    for lid, nome, pdf, problemas in resultados:
        total += len(problemas)
        selo = "OK  " if not problemas else "FALHA"
        print(f"{selo} {lid:>4}  {nome[:44]}")
        for p in problemas:
            print(f"        {p}")
    print("─" * 74)
    print(f"{len(resultados)} materiais auditados, {total} problema(s)")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
