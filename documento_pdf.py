"""documento_pdf.py, Documento comercial ELABORADO para o cliente, em PDF."""

import argparse
import asyncio
import math
import re
from datetime import date
from typing import Dict, List, Tuple

import config
import db
import diagnostico as diag_mod
import proposta as prop_mod
import pagespeed

_br = diag_mod._br


def _money(n) -> str:
    return "R$ " + _br(round(n))


def _slug(nome: str) -> str:
    s = re.sub(r"[^\w\s-]", "", nome.lower())
    return re.sub(r"\s+", "-", s).strip("-")


def esc(t) -> str:
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ══════════════════════════════════════════════════════════════════════════════
#  SIMULAÇÕES
# ══════════════════════════════════════════════════════════════════════════════

def _frac_recuperacao(mes: int, teto: float) -> float:
    """Rampa de recuperação (S-curve) até o teto do nível. Nunca 100%."""
    return teto * (1 - math.exp(-mes / config.RECUP_VELOCIDADE))


def simular(lead: Dict) -> Dict:
    """Monta todas as séries de simulação a partir do diagnóstico do lead."""
    problemas = diag_mod.detectar_problemas(lead)[:3]
    perdidos, roi, ticket = diag_mod.estimar_roi(lead)
    nivel_rec = prop_mod.recomendar_nivel(lead)
    meses = list(range(1, config.SIM_MESES + 1))

    # Perda acumulada se NADA for feito (linear: roi por mês)
    perda_acumulada = [(m, roi * m) for m in meses]

    # Retorno acumulado por nível (recuperação de pacientes * ticket)
    retorno_por_nivel = {}
    invest_por_nivel = {}
    payback_por_nivel = {}
    roi12_por_nivel = {}
    net12_por_nivel = {}
    for n in (1, 2, 3):
        teto = config.RECUP_TETO_NIVEL[n]
        preco = prop_mod._preco_representativo(n)
        ret_acc, inv_acc = [], []
        acc = 0.0
        breakeven = None
        for m in meses:
            recuperados = perdidos * _frac_recuperacao(m, teto)
            receita_mes = recuperados * ticket
            acc += receita_mes
            invest = preco * m
            ret_acc.append((m, acc))
            inv_acc.append((m, invest))
            if breakeven is None and acc >= invest:
                breakeven = m
        retorno_por_nivel[n] = ret_acc
        invest_por_nivel[n] = inv_acc
        payback_por_nivel[n] = breakeven
        invest_ano = preco * 12
        net12_por_nivel[n] = ret_acc[-1][1] - invest_ano
        roi12_por_nivel[n] = (ret_acc[-1][1] - invest_ano) / invest_ano * 100 if invest_ano else 0

    return {
        "problemas": problemas,
        "perdidos": perdidos,
        "roi": roi,
        "ticket": ticket,
        "nivel_rec": nivel_rec,
        "meses": meses,
        "perda_acumulada": perda_acumulada,
        "retorno_por_nivel": retorno_por_nivel,
        "invest_por_nivel": invest_por_nivel,
        "payback_por_nivel": payback_por_nivel,
        "roi12_por_nivel": roi12_por_nivel,
        "net12_por_nivel": net12_por_nivel,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  GRÁFICOS (SVG puro, sem libs, crisp no PDF)
# ══════════════════════════════════════════════════════════════════════════════

COR = {
    "dark": "#0f2f43", "teal": "#17607e", "green": "#2f9e6f", "green2": "#58d0a0",
    "red": "#d9534f", "grid": "#e3e9ef", "ink": "#33475b", "muted": "#8497a6",
    1: "#2f9e6f", 2: "#2563eb", 3: "#7c3aed",
}


def svg_gauge(score: float) -> str:
    """Medidor circular (donut) da 'saúde/oportunidade digital'."""
    frac = max(0, min(100, score)) / 100
    r, cx, cy = 70, 90, 90
    circ = 2 * math.pi * r
    dash = frac * circ
    if score >= 60:
        cor, rot = COR["red"], "OPORTUNIDADE ALTA"
    elif score >= 40:
        cor, rot = "#e8912b", "OPORTUNIDADE MÉDIA"
    else:
        cor, rot = COR["green"], "PRESENÇA SAUDÁVEL"
    return f"""<svg viewBox="0 0 180 180" width="150" height="150">
      <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{COR['grid']}" stroke-width="16"/>
      <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{cor}" stroke-width="16"
        stroke-linecap="round" stroke-dasharray="{dash:.1f} {circ:.1f}"
        transform="rotate(-90 {cx} {cy})"/>
      <text x="{cx}" y="{cy-4}" text-anchor="middle" font-size="40" font-weight="800"
        fill="{COR['dark']}">{score:.0f}</text>
      <text x="{cx}" y="{cy+20}" text-anchor="middle" font-size="13" fill="{COR['muted']}">de 100</text>
    </svg><div class="gauge-rot" style="color:{cor}">{rot}</div>"""


def svg_versus(nome_a: str, nome_b: str, rows: List[Tuple]) -> str:
    """
    Comparação clínica × concorrente. rows = [(label, disp_a, frac_a, disp_b, frac_b)]
    frac em [0,1] define o comprimento da barra.
    """
    W, rowh, top = 640, 46, 42
    H = top + len(rows) * rowh + 6
    numA_x = 168          # nº da clínica (direita), ANTES da barra vermelha
    barA_x0, barW = 176, 150
    barB_x0 = 350
    numB_x = barB_x0 + barW + 9   # nº do líder, DEPOIS da barra verde
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%">']
    # títulos neutros: quem pinta de verde ou vermelho agora é cada linha, e
    # rotular a clínica de vermelho no cabeçalho já pré-julgava o resultado
    parts.append(f'<text x="{barA_x0+barW/2}" y="26" text-anchor="middle" font-size="12.5" '
                 f'font-weight="700" fill="{COR["ink"]}">{esc(nome_a[:24])}</text>')
    parts.append(f'<text x="{barB_x0+barW/2}" y="26" text-anchor="middle" font-size="12.5" '
                 f'font-weight="700" fill="{COR["ink"]}">{esc(nome_b[:24])}</text>')
    for i, (label, da, fa, dbv, fb) in enumerate(rows):
        y = top + i * rowh + 12
        ty = y + 15
        # A cor era fixa por COLUNA: a clínica sempre vermelha, a referência sempre
        # verde.
        margem = 0.02
        if fa > fb + margem:
            cor_a, cor_b = COR["green"], COR["red"]
        elif fb > fa + margem:
            cor_a, cor_b = COR["red"], COR["green"]
        else:
            cor_a = cor_b = COR["ink"]
        parts.append(f'<text x="0" y="{ty}" font-size="12" fill="{COR["ink"]}">{esc(label)}</text>')
        parts.append(f'<text x="{numA_x}" y="{ty}" text-anchor="end" font-size="13" '
                     f'font-weight="800" fill="{cor_a}">{esc(da)}</text>')
        parts.append(f'<rect x="{barA_x0}" y="{y}" width="{barW}" height="20" rx="5" fill="#f1f4f7"/>')
        parts.append(f'<rect x="{barA_x0}" y="{y}" width="{max(5,barW*fa):.0f}" height="20" rx="5" '
                     f'fill="{cor_a}" opacity=".85"/>')
        parts.append(f'<rect x="{barB_x0}" y="{y}" width="{barW}" height="20" rx="5" fill="#f1f4f7"/>')
        parts.append(f'<rect x="{barB_x0}" y="{y}" width="{max(5,barW*fb):.0f}" height="20" rx="5" '
                     f'fill="{cor_b}" opacity=".9"/>')
        parts.append(f'<text x="{numB_x}" y="{ty}" font-size="13" font-weight="800" '
                     f'fill="{cor_b}">{esc(dbv)}</text>')
    parts.append('</svg>')
    return "".join(parts)


def _axes(parts, x0, y0, x1, y1, ymax, meses, y_ticks=4):
    """Desenha grade + eixos de um gráfico de linha (meses no X)."""
    for i in range(y_ticks + 1):
        y = y1 - (y1 - y0) * i / y_ticks
        val = ymax * i / y_ticks
        parts.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" stroke="{COR["grid"]}"/>')
        parts.append(f'<text x="{x0-8}" y="{y+4:.1f}" text-anchor="end" font-size="10" '
                     f'fill="{COR["muted"]}">{_br(round(val))}</text>')
    for m in meses:
        if m % 2 == 0 or m == 1:
            x = x0 + (x1 - x0) * (m - 1) / (len(meses) - 1)
            parts.append(f'<text x="{x:.1f}" y="{y1+16}" text-anchor="middle" font-size="10" '
                         f'fill="{COR["muted"]}">{m}m</text>')


def _pts(serie, x0, y0, x1, y1, ymax, meses):
    n = len(meses)
    out = []
    for (m, v) in serie:
        x = x0 + (x1 - x0) * (m - 1) / (n - 1)
        y = y1 - (y1 - y0) * (v / ymax if ymax else 0)
        out.append((x, y))
    return out


def svg_perda(perda_acumulada, meses) -> str:
    """Área vermelha: dinheiro perdido acumulado se nada for feito."""
    W, H = 560, 240
    x0, y0, x1, y1 = 54, 20, 545, 195
    ymax = perda_acumulada[-1][1] * 1.08
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%">']
    _axes(parts, x0, y0, x1, y1, ymax, meses)
    pts = _pts(perda_acumulada, x0, y0, x1, y1, ymax, meses)
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{x0},{y1} " + poly + f" {x1},{y1}"
    parts.append(f'<polygon points="{area}" fill="{COR["red"]}" opacity="0.12"/>')
    parts.append(f'<polyline points="{poly}" fill="none" stroke="{COR["red"]}" stroke-width="2.5"/>')
    # ponto final destacado
    ex, ey = pts[-1]
    parts.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4" fill="{COR["red"]}"/>')
    parts.append(f'<text x="{ex:.1f}" y="{ey-10:.1f}" text-anchor="end" font-size="12" '
                 f'font-weight="700" fill="{COR["red"]}">{_money(perda_acumulada[-1][1])}</text>')
    parts.append('</svg>')
    return "".join(parts)


def svg_payback(retorno, invest, breakeven, meses, cor) -> str:
    """Investimento acumulado × retorno acumulado, com ponto de equilíbrio."""
    W, H = 560, 250
    x0, y0, x1, y1 = 54, 20, 545, 195
    ymax = max(retorno[-1][1], invest[-1][1]) * 1.08
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%">']
    _axes(parts, x0, y0, x1, y1, ymax, meses)
    pr = _pts(retorno, x0, y0, x1, y1, ymax, meses)
    pi = _pts(invest, x0, y0, x1, y1, ymax, meses)
    poly_r = " ".join(f"{x:.1f},{y:.1f}" for x, y in pr)
    poly_i = " ".join(f"{x:.1f},{y:.1f}" for x, y in pi)
    # área de lucro entre retorno e investimento
    parts.append(f'<polyline points="{poly_i}" fill="none" stroke="{COR["muted"]}" '
                 f'stroke-width="2" stroke-dasharray="5 4"/>')
    parts.append(f'<polyline points="{poly_r}" fill="none" stroke="{cor}" stroke-width="2.8"/>')
    # ponto de equilíbrio (rótulo ancorado para não sair da área nem sobrepor o eixo)
    if breakeven:
        bx = x0 + (x1 - x0) * (breakeven - 1) / (len(meses) - 1)
        parts.append(f'<line x1="{bx:.1f}" y1="{y0+10}" x2="{bx:.1f}" y2="{y1}" stroke="{COR["green"]}" '
                     f'stroke-width="1.4" stroke-dasharray="3 3"/>')
        perto_da_borda = bx < x0 + 70
        anchor = "start" if perto_da_borda else "middle"
        tx = bx + 6 if perto_da_borda else bx
        parts.append(f'<text x="{tx:.1f}" y="{y0+4}" text-anchor="{anchor}" font-size="10.5" '
                     f'font-weight="700" fill="{COR["green"]}">equilíbrio: mês {breakeven}</text>')
    # legenda
    parts.append(f'<circle cx="{x0+6}" cy="{y1+30}" r="4" fill="{cor}"/>'
                 f'<text x="{x0+16}" y="{y1+34}" font-size="11" fill="{COR["ink"]}">Retorno acumulado</text>')
    parts.append(f'<line x1="{x0+170}" y1="{y1+30}" x2="{x0+190}" y2="{y1+30}" stroke="{COR["muted"]}" '
                 f'stroke-width="2" stroke-dasharray="5 4"/>'
                 f'<text x="{x0+196}" y="{y1+34}" font-size="11" fill="{COR["ink"]}">Investimento acumulado</text>')
    parts.append('</svg>')
    return "".join(parts)


def svg_curva_cliques(pos_atual, pos_alvo: int, ctr_de) -> str:
    """Para onde vão os cliques de quem procura: fatia por posição no Maps."""
    faixas = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    fora_da_faixa = bool(pos_atual) and pos_atual > 10

    W, H = 620, 268
    x0, y0, y1 = 44, 26, 186
    bw, gap = 44, 12
    if fora_da_faixa:
        bw, gap = 40, 11

    valores = [(p, ctr_de(p)) for p in faixas]
    if fora_da_faixa:
        valores.append((pos_atual, ctr_de(pos_atual)))
    vmax = max(v for _, v in valores) * 1.18 or 1

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%">']
    parts.append(f'<line x1="{x0}" y1="{y1}" x2="{W-16}" y2="{y1}" stroke="{COR["grid"]}"/>')

    for i, (p, ctr) in enumerate(valores):
        x = x0 + i * (bw + gap)
        h = (y1 - y0) * (ctr / vmax)
        y = y1 - h
        eu = bool(pos_atual) and p == pos_atual
        # Meta só existe se estiver À FRENTE de onde a clínica está. Com o alvo
        # fixo em `config.POSICAO_ALVO`, uma clínica em 2º recebia a 3ª posição
        # marcada como "meta": o documento propunha que ela PIORASSE.
        alvo = (pos_alvo is not None and p == pos_alvo and not eu
                and (not pos_atual or pos_alvo < pos_atual))
        if eu:
            # vermelho é cor de problema, e estar entre os primeiros não é
            # problema. A mesma barra que diz "você" dizia "ruim" para quem
            # está em 2º lugar.
            bem_colocada = pos_alvo is None or (pos_atual and pos_atual <= pos_alvo)
            cor, op = (COR["green"] if bem_colocada else COR["red"]), 1.0
        elif alvo:
            cor, op = COR["green"], 0.95
        else:
            cor, op = COR["muted"], 0.30
        parts.append(f'<rect x="{x}" y="{y:.1f}" width="{bw}" height="{max(h,3):.1f}" '
                     f'rx="5" fill="{cor}" opacity="{op}"/>')
        # % em cima da barra, só onde ajuda a ler
        if eu or alvo or p <= 3:
            rotulo_pct = f"{ctr*100:.1f}".replace(".", ",") + "%"
            parts.append(f'<text x="{x+bw/2:.0f}" y="{y-6:.0f}" text-anchor="middle" '
                         f'font-size="10.5" font-weight="700" fill="{cor if op>0.5 else COR["ink"]}">'
                         f'{rotulo_pct}</text>')
        rot = f"{p}º"
        parts.append(f'<text x="{x+bw/2:.0f}" y="{y1+16}" text-anchor="middle" font-size="11" '
                     f'font-weight="{"800" if (eu or alvo) else "400"}" '
                     f'fill="{cor if (eu or alvo) else COR["muted"]}">{esc(rot)}</text>')
        if eu:
            # acompanha a cor da barra: vermelho fixo aqui deixava o rótulo
            # "você" vermelho embaixo de uma barra verde
            parts.append(f'<text x="{x+bw/2:.0f}" y="{y1+32}" text-anchor="middle" '
                         f'font-size="10.5" font-weight="700" fill="{cor}">você</text>')
        elif alvo:
            parts.append(f'<text x="{x+bw/2:.0f}" y="{y1+32}" text-anchor="middle" '
                         f'font-size="10.5" font-weight="700" fill="{COR["green"]}">meta</text>')

    if fora_da_faixa:
        # a barra do lead fica destacada do bloco 1..10 por um respiro visual
        xg = x0 + len(faixas) * (bw + gap) - gap / 2
        parts.append(f'<line x1="{xg:.0f}" y1="{y0}" x2="{xg:.0f}" y2="{y1}" '
                     f'stroke="{COR["grid"]}" stroke-dasharray="4 4"/>')

    parts.append(f'<text x="{x0}" y="{H-10}" font-size="10.5" fill="{COR["muted"]}">'
                 f'Fatia dos cliques por posição na busca do Maps</text>')
    parts.append('</svg>')
    return "".join(parts)


def svg_custo_demora(roi_mes: int) -> str:
    """
    Quanto custa ADIAR a decisão. Barras crescentes: começar agora, em 1, 3, 6 e 12
    meses. Não fala de retorno, fala do que a clínica deixa de receber.
    """
    cenarios = [(0, "Começar\nagora"), (1, "Adiar\n1 mês"), (3, "Adiar\n3 meses"),
                (6, "Adiar\n6 meses"), (12, "Adiar\n1 ano")]
    W, H = 560, 250
    x0, y0, y1 = 40, 30, 175
    vmax = max(roi_mes * 12, 1) * 1.15
    bw, gap = 74, 24
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%">']
    parts.append(f'<line x1="{x0}" y1="{y1}" x2="{W-20}" y2="{y1}" stroke="{COR["grid"]}"/>')
    for i, (meses, rot) in enumerate(cenarios):
        perdido = roi_mes * meses
        x = x0 + 12 + i * (bw + gap)
        h = (y1 - y0) * (perdido / vmax) if vmax else 0
        y = y1 - h
        # quanto mais adia, mais vermelho
        op = 0.25 + 0.75 * (meses / 12)
        parts.append(f'<rect x="{x}" y="{y:.1f}" width="{bw}" height="{max(h,3):.1f}" rx="6" '
                     f'fill="{COR["red"]}" opacity="{op:.2f}"/>')
        rotulo = "R$ 0" if meses == 0 else _money(perdido)
        parts.append(f'<text x="{x+bw/2}" y="{y-8:.1f}" text-anchor="middle" font-size="12" '
                     f'font-weight="800" fill="{COR["red"] if meses else COR["green"]}">{rotulo}</text>')
        for j, linha in enumerate(rot.split("\n")):
            parts.append(f'<text x="{x+bw/2}" y="{y1+17+j*13}" text-anchor="middle" font-size="11" '
                         f'fill="{COR["ink"]}">{esc(linha)}</text>')
    parts.append(f'<text x="{x0}" y="{H-6}" font-size="10.5" fill="{COR["muted"]}">'
                 f'Valor que deixa de entrar no caixa enquanto a situação continua como está.</text>')
    parts.append('</svg>')
    return "".join(parts)


def svg_impacto_elementos(itens) -> str:
    """
    Impacto de cada elemento que falta. itens = [(elemento, presente: bool, efeito)].
    Mostra o que a clínica perde por não ter cada peça.
    """
    W, rowh, top = 560, 40, 8
    H = top + len(itens) * rowh + 6
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%">']
    for i, (elem, ok, efeito) in enumerate(itens):
        y = top + i * rowh
        cor = COR["green"] if ok else COR["red"]
        icone = "✓" if ok else "✕"
        parts.append(f'<circle cx="12" cy="{y+14}" r="9" fill="{cor}" opacity="0.15"/>')
        parts.append(f'<text x="12" y="{y+18}" text-anchor="middle" font-size="11" '
                     f'font-weight="800" fill="{cor}">{icone}</text>')
        parts.append(f'<text x="30" y="{y+13}" font-size="12.5" font-weight="700" '
                     f'fill="{COR["dark"]}">{esc(elem)}</text>')
        parts.append(f'<text x="30" y="{y+29}" font-size="11.5" fill="{COR["muted"]}">'
                     f'{esc(efeito[:88])}</text>')
    parts.append('</svg>')
    return "".join(parts)


def svg_niveis(net12_por_nivel: Dict, roi12_por_nivel: Dict, nivel_rec: int) -> str:
    """Barras do RETORNO LÍQUIDO estimado em 12 meses por nível (R$ que sobra)."""
    W, H = 560, 230
    x0, y0, y1 = 60, 24, 165
    vals = [net12_por_nivel[n] for n in (1, 2, 3)]
    vmax = max(vals + [10]) * 1.18
    bw, gap = 96, 70
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%">']
    parts.append(f'<line x1="{x0}" y1="{y1}" x2="{W-20}" y2="{y1}" stroke="{COR["grid"]}"/>')
    nomes = {n: config.FAIXAS_PRECO[n]["nome"] for n in (1, 2, 3)}
    for i, n in enumerate((1, 2, 3)):
        x = x0 + 20 + i * (bw + gap)
        h = (y1 - y0) * (vals[i] / vmax) if vmax else 0
        y = y1 - h
        destaque = (n == nivel_rec)
        parts.append(f'<rect x="{x}" y="{y:.1f}" width="{bw}" height="{h:.1f}" rx="8" '
                     f'fill="{COR[n]}" opacity="{1 if destaque else 0.55}"/>')
        parts.append(f'<text x="{x+bw/2}" y="{y-22:.1f}" text-anchor="middle" font-size="14" '
                     f'font-weight="800" fill="{COR[n]}">{_money(vals[i])}</text>')
        parts.append(f'<text x="{x+bw/2}" y="{y-8:.1f}" text-anchor="middle" font-size="10.5" '
                     f'fill="{COR["muted"]}">{roi12_por_nivel[n]:.0f}% ROI</text>')
        parts.append(f'<text x="{x+bw/2}" y="{y1+18}" text-anchor="middle" font-size="12" '
                     f'font-weight="{700 if destaque else 500}" fill="{COR["dark"]}">Nível {n}</text>')
        parts.append(f'<text x="{x+bw/2}" y="{y1+34}" text-anchor="middle" font-size="10.5" '
                     f'fill="{COR["muted"]}">{esc(nomes[n])}</text>')
        if destaque:
            parts.append(f'<text x="{x+bw/2}" y="{y1+50}" text-anchor="middle" font-size="10" '
                         f'font-weight="700" fill="{COR["green"]}">◆ recomendado</text>')
    parts.append('</svg>')
    return "".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
#  MONTAGEM DO HTML (multi-página, design elaborado)
# ══════════════════════════════════════════════════════════════════════════════

def _kpi(valor, rotulo, cor=COR["dark"]) -> str:
    return (f'<div class="kpi"><div class="kpi-v" style="color:{cor}">{valor}</div>'
            f'<div class="kpi-l">{rotulo}</div></div>')


def montar_html(lead: Dict) -> str:
    sim = simular(lead)
    problemas = sim["problemas"]
    perdidos, roi, ticket = sim["perdidos"], sim["roi"], sim["ticket"]
    nivel = sim["nivel_rec"]
    preco = prop_mod._preco_representativo(nivel)
    payback = sim["payback_por_nivel"][nivel]
    roi12 = sim["roi12_por_nivel"][nivel]
    hoje = date.today().strftime("%d/%m/%Y")
    nome = lead["nome"]
    from proposta import NIVEL_DESC
    from diagnostico import PROBLEMA_META
    fcfg = config.FAIXAS_PRECO

    # ── Diagnóstico (problemas) ──
    probs_html = "".join(
        f'<div class="prob"><div class="prob-num">{i}</div><div>'
        f'<h3>{esc(p["titulo"])}</h3><p>{esc(p["texto"])}</p></div></div>'
        for i, p in enumerate(problemas, 1)
    )

    # ── Comparação com concorrente ──
    lideres = db.leaders_by_nicho(lead["nicho"], lead["id"], limit=1)
    versus_block = ""
    if lideres:
        c = lideres[0]
        pa = lead.get("posicao") or 25
        pb = c.get("posicao") or 25
        na = lead.get("avaliacao_num") or 0
        nb = c.get("avaliacao_num") or 0
        va = lead.get("total_avaliacoes_num") or 0
        vb = c.get("total_avaliacoes_num") or 0
        vmax = max(va, vb, 1)
        presa = (1 if lead.get("tem_site") else 0) + (1 if lead.get("tem_instagram") else 0)
        presb = (1 if c.get("tem_site") else 0) + (1 if c.get("tem_instagram") else 0)
        rows = [
            ("Posição no Maps", f"{pa}º", max(0.05, 1 - (pa - 1) / 25),
             f"{pb}º", max(0.05, 1 - (pb - 1) / 25)),
            ("Nota Google", f"{na:.1f}" if na else "-", na / 5, f"{nb:.1f}" if nb else "-", nb / 5),
            ("Avaliações", _br(va), va / vmax, _br(vb), vb / vmax),
            ("Site/Insta no Google", f"{presa}/2", presa / 2, f"{presb}/2", presb / 2),
        ]
        versus_block = (
            '<div class="sec"><h2>Você × o líder da sua região</h2>'
            f'<p class="cap">Comparamos a {esc(nome)} com a <b>{esc(c["nome"])}</b>, '
            f'referência da mesma categoria hoje. A diferença é o espaço que dá para recuperar.</p>'
            f'{svg_versus(nome, c["nome"], rows)}</div>'
        )

    # ── Tiers ──
    marc = {1: COR[1], 2: COR[2], 3: COR[3]}
    niveis_cards = ""
    for n in (1, 2, 3):
        f = fcfg[n]
        resolve = [p["titulo"] for p in problemas if PROBLEMA_META.get(p["chave"], {}).get("nivel", 1) <= n]
        resolve_html = "".join(f"<li>{esc(t)}</li>" for t in resolve) or "<li>Base de presença</li>"
        badge = '<span class="tag">⭐ Mais escolhido</span>' if f["mais_vendido"] else ""
        rec = ' nivel--rec' if n == nivel else ""
        rec_tag = '<span class="tag tag--rec">Recomendado</span>' if n == nivel else ""
        niveis_cards += (
            f'<div class="nivel{rec}" style="--c:{marc[n]}">'
            f'<div class="nivel-h"><span class="nivel-nome">Nível {n} · {esc(f["nome"])}</span>{badge}{rec_tag}</div>'
            f'<div class="nivel-preco">R$ {_br(f["min"])}–{_br(f["max"])}<span>/mês</span></div>'
            f'<p class="nivel-desc">{esc(NIVEL_DESC[n]["resumo"])}</p>'
            f'<div class="nivel-res">Resolve: <ul>{resolve_html}</ul></div></div>'
        )

    # ── PageSpeed (prova visual da lentidão do site) ──
    ps = pagespeed.dados_para_pdf(lead)
    pagespeed_block = ""
    if ps and ps.get("score") is not None:
        sc = ps["score"]
        cor = "#16a34a" if sc >= 90 else ("#d97706" if sc >= 50 else "#dc2626")
        m = ps.get("metrics", {})
        shot = (f'<img src="{ps["screenshot"]}" style="max-width:280px;border:1px solid #e3e9ef;'
                f'border-radius:8px" alt="site">' if ps.get("screenshot") else "")
        mrows = "".join(f'<tr><td style="color:#7a8a99;padding:2px 10px 2px 0">{esc(k)}</td>'
                        f'<td style="font-weight:600">{esc(v)}</td></tr>'
                        for k, v in m.items() if v)
        pagespeed_block = (
            '<div class="sec"><h2>⚡ Velocidade do site (Google PageSpeed)</h2>'
            '<p class="cap">Medição oficial do Google. Site lento afasta paciente e derruba o '
            'ranking no próprio Google.</p>'
            '<div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap">'
            f'<div style="text-align:center"><div style="font-size:40px;font-weight:800;color:{cor}">{sc}</div>'
            f'<div style="font-size:11px;color:#7a8a99">de 100</div></div>'
            f'<table style="font-size:12.5px">{mrows}</table>{shot}</div></div>'
        )

    pacientes_payback = max(1, round(preco / ticket)) if ticket else 1
    retorno_ano = sim["retorno_por_nivel"][nivel][-1][1]

    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Diagnóstico Digital, {esc(nome)}</title>
<style>
  @page {{ size: A4; margin: 0; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; -webkit-print-color-adjust: exact;
       print-color-adjust: exact; }}
  body {{ font-family: "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: #1a2430;
         font-size: 13px; line-height: 1.5; }}
  h2 {{ font-size: 17px; color: {COR['dark']}; margin-bottom: 8px; }}
  .cap {{ color: {COR['muted']}; font-size: 12px; margin-bottom: 12px; }}
  .sec {{ margin: 22px 0; }}
  /* Capa */
  .cover {{ height: 297mm; background: linear-gradient(150deg, #0f2f43 0%, #17607e 65%, #1c7a94 100%);
           color: #fff; padding: 60px 54px; display: flex; flex-direction: column; }}
  .cover .brand {{ letter-spacing: .2em; font-size: 12px; text-transform: uppercase; opacity: .8; }}
  .cover .spacer {{ flex: 1; }}
  .cover .kick {{ font-size: 15px; opacity: .85; letter-spacing: .06em; text-transform: uppercase; }}
  .cover h1 {{ font-size: 46px; line-height: 1.08; margin: 10px 0 18px; font-weight: 800; }}
  .cover .desc {{ font-size: 16px; opacity: .9; max-width: 74%; }}
  .cover .foot {{ font-size: 13px; opacity: .8; border-top: 1px solid rgba(255,255,255,.25);
                 padding-top: 16px; }}
  .cover .pill {{ display: inline-block; background: rgba(255,255,255,.14); border-radius: 999px;
                 padding: 6px 16px; font-size: 13px; margin-top: 14px; }}
  /* Folhas de conteúdo */
  .sheet {{ break-before: page; padding: 20mm 15mm; }}
  .prob {{ display: flex; gap: 13px; padding: 12px 0; border-bottom: 1px solid #eef2f6; }}
  .prob:last-child {{ border: none; }}
  .prob-num {{ flex: none; width: 28px; height: 28px; border-radius: 50%; background: {COR['teal']};
              color: #fff; font-weight: 700; display: flex; align-items: center; justify-content: center; }}
  .prob h3 {{ font-size: 14px; color: #12384d; }}
  .prob p {{ font-size: 12.5px; color: #4a5a6a; }}
  /* Resumo executivo */
  .exec {{ display: flex; gap: 20px; align-items: center; background: #f6f9fb; border-radius: 14px;
          padding: 20px 24px; }}
  .gauge-wrap {{ text-align: center; flex: none; }}
  .gauge-rot {{ font-size: 11px; font-weight: 700; letter-spacing: .04em; margin-top: 2px; }}
  .kpis {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; flex: 1; }}
  .kpi {{ background: #fff; border: 1px solid #e6edf2; border-radius: 10px; padding: 12px 14px; }}
  .kpi-v {{ font-size: 21px; font-weight: 800; }}
  .kpi-l {{ font-size: 11px; color: {COR['muted']}; margin-top: 2px; }}
  .callout {{ background: {COR['dark']}; color: #fff; border-radius: 12px; padding: 18px 22px;
             text-align: center; margin: 16px 0; }}
  .callout .big {{ font-size: 34px; font-weight: 800; color: {COR['green2']}; }}
  .box {{ border: 1px solid #e6edf2; border-radius: 12px; padding: 16px 18px; margin: 14px 0;
         background: #fff; }}
  .box h2 {{ margin-bottom: 4px; }}
  .lead-note {{ font-size: 12.5px; color: #4a5a6a; margin: 8px 0 4px; }}
  /* Níveis */
  .niveis {{ display: grid; gap: 11px; }}
  .nivel {{ border: 1.5px solid #e3e9ef; border-radius: 12px; padding: 14px 16px; }}
  .nivel--rec {{ border-color: var(--c); background: color-mix(in srgb, var(--c) 5%, #fff); }}
  .nivel-h {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .nivel-nome {{ font-weight: 700; color: var(--c); font-size: 14px; }}
  .nivel-preco {{ font-size: 19px; font-weight: 800; color: #12384d; margin: 3px 0; }}
  .nivel-preco span {{ font-size: 12px; font-weight: 500; color: {COR['muted']}; }}
  .nivel-desc {{ font-size: 12px; color: #4a5a6a; }}
  .nivel-res {{ font-size: 11.5px; color: #55677a; margin-top: 6px; }}
  .nivel-res ul {{ margin: 3px 0 0 16px; }}
  .tag {{ font-size: 10.5px; background: #eef2f6; color: #33475b; padding: 2px 8px;
         border-radius: 999px; font-weight: 600; }}
  .tag--rec {{ background: var(--c); color: #fff; }}
  .rec {{ background: #ecf6f1; border-radius: 12px; padding: 18px 22px; margin-top: 16px; }}
  .cta {{ text-align: center; margin-top: 22px; padding-top: 18px; border-top: 2px dashed #dde5ec; }}
  .cta .btn {{ display: inline-block; background: {COR['teal']}; color: #fff; padding: 12px 30px;
              border-radius: 999px; font-weight: 700; }}
  .disc {{ font-size: 10px; color: #9aa8b4; margin-top: 20px; border-top: 1px solid #eef2f6;
          padding-top: 10px; }}
  .foot {{ text-align: center; font-size: 11px; color: {COR['muted']}; margin-top: 14px; }}
</style></head><body>

<div class="cover">
  <div class="brand">Presença Digital para Clínicas</div>
  <div class="spacer"></div>
  <div class="kick">Diagnóstico &amp; Plano de Crescimento</div>
  <h1>{esc(nome)}</h1>
  <div class="desc">Um raio-x completo de como a sua clínica aparece hoje na internet, com
    números, comparações e uma simulação do que está em jogo.</div>
  <div class="pill">Ticket médio considerado: R$ {_br(ticket)} · nicho {esc(lead.get('nicho',''))}</div>
  <div class="spacer"></div>
  <div class="foot">Preparado para {esc(nome)} · {hoje} · Captação • Confiança • Conversão</div>
</div>

<section class="sheet">
  <h2>Resumo executivo</h2>
  <div class="exec">
    <div class="gauge-wrap">{svg_gauge(lead['score'])}</div>
    <div class="kpis">
      {_kpi(perdidos, "pacientes/mês escapando", COR['red'])}
      {_kpi(_money(roi), "perda por mês", COR['red'])}
      {_kpi(_money(roi*12), "perda por ano", COR['red'])}
      {_kpi(f"mês {payback}" if payback else "-", "payback estimado", COR['green'])}
      {_kpi(f"{roi12:.0f}%", "retorno em 12 meses*", COR['green'])}
      {_kpi(fcfg[nivel]['nome'], "plano indicado", COR[nivel])}
    </div>
  </div>

  <div class="sec"><h2>🔍 O que encontramos</h2>{probs_html}</div>

  <div class="callout">
    <div style="font-size:12px;opacity:.85">Estimativa de dinheiro deixado na mesa hoje</div>
    <div class="big">{_money(roi)}/mês</div>
    <div style="font-size:12px;opacity:.7">≈ {_money(roi*12)} por ano</div>
  </div>
</section>

<section class="sheet">
  {versus_block}
  {pagespeed_block}
  <div class="sec"><h2>📉 Simulação: o custo de não fazer nada</h2>
    <p class="cap">Se a situação seguir como está, veja a perda acumulada mês a mês (12 meses):</p>
    {svg_perda(sim['perda_acumulada'], sim['meses'])}
    <p class="lead-note">Em um ano, isso soma cerca de <b>{_money(roi*12)}</b> em pacientes que
      procuraram {esc(lead['categoria'].lower())} na região e foram para outra clínica.</p>
  </div>
</section>

<section class="sheet">
  <div class="sec"><h2>📈 Simulação: investimento × retorno (Nível {nivel})</h2>
    <p class="cap">Projeção de recuperação de pacientes com o plano recomendado. O ponto de
      equilíbrio é quando o retorno acumulado passa o investido.</p>
    {svg_payback(sim['retorno_por_nivel'][nivel], sim['invest_por_nivel'][nivel], payback, sim['meses'], COR[nivel])}
    <p class="lead-note">Retorno acumulado estimado em 12 meses: <b>{_money(retorno_ano)}</b>,
      contra <b>{_money(preco*12)}</b> de investimento, payback por volta do
      <b>mês {payback if payback else '-'}</b>.</p>
  </div>
  <div class="sec"><h2>Qual nível rende mais?</h2>
    <p class="cap">Retorno líquido estimado em 12 meses (o que sobra depois de pagar o plano).
      A barra mais alta é a que mais coloca dinheiro no seu bolso:</p>
    {svg_niveis(sim['net12_por_nivel'], sim['roi12_por_nivel'], nivel)}
  </div>
</section>

<section class="sheet">
  <h2>✅ Como resolvemos, 3 caminhos</h2>
  <p class="cap">Você escolhe o ritmo. Cada nível ataca os pontos encontrados acima.</p>
  <div class="niveis">{niveis_cards}</div>

  <div class="rec"><h2 style="margin-bottom:4px">🎯 Nossa recomendação</h2>
    <p>Para o momento da <b>{esc(nome)}</b>, indicamos o <b>Nível {nivel}, {esc(fcfg[nivel]['nome'])}</b>.
      Com apenas <b>{pacientes_payback} paciente(s) recuperado(s)/mês</b> o plano já se paga; a
      simulação aponta payback por volta do <b>mês {payback if payback else '-'}</b> e retorno de
      <b>{roi12:.0f}%</b> em 12 meses.</p>
  </div>

  <div class="cta">
    <p style="margin-bottom:12px;color:#33475b">Quer ver como isso funciona na prática, sem compromisso?</p>
    <span class="btn">Falar com a nossa equipe</span>
  </div>

  <div class="disc">* Simulações e projeções são <b>estimativas</b> baseadas na posição no Google Maps,
    nota, volume de avaliações e presença digital atuais da clínica, mais o ticket médio do nicho.
    Resultados reais variam conforme execução, sazonalidade e mercado local. Não é garantia de retorno.</div>
  <div class="foot">Presença Digital para Clínicas · Captação • Confiança • Conversão · {hoje}</div>
</section>

</body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
#  EXPORT PDF (Playwright / Chromium)
# ══════════════════════════════════════════════════════════════════════════════

async def _html_para_pdf(html: str, caminho) -> None:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(html, wait_until="load")
        await page.pdf(path=str(caminho), format="A4", print_background=True,
                       margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        await browser.close()


def gerar_pdf(lead: Dict, caminho=None):
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    caminho = caminho or (config.OUT_DIR / f"proposta_{_slug(lead['nome'])}.pdf")
    html = montar_html(lead)
    # salva também o HTML (útil para pré-visualizar/ajustar)
    (config.OUT_DIR / f"proposta_{_slug(lead['nome'])}.html").write_text(html, encoding="utf-8")
    asyncio.run(_html_para_pdf(html, caminho))
    return caminho


def main(lead_id: int = None, todos: bool = False):
    db.init_db()
    leads = db.leads_by_score(limit=1000)
    if not leads:
        print("⚠ Sem leads. Rode: python run_pilot.py")
        return
    if todos:
        for l in leads:
            cam = gerar_pdf(l)
            print(f"✅ {l['nome']}: {cam.name}")
        print(f"\n{len(leads)} PDFs em: {config.OUT_DIR}")
    else:
        alvo = db.get_lead(lead_id) if lead_id else leads[0]
        cam = gerar_pdf(alvo)
        print(f"✅ Documento PDF ({alvo['nome']}): {cam}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead", type=int, default=None)
    ap.add_argument("--todos", action="store_true")
    args = ap.parse_args()
    main(lead_id=args.lead, todos=args.todos)
