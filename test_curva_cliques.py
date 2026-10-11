"""test_curva_cliques.py — O gráfico de cliques por posição não pode propor piora."""

import re
import sys

import documento_pdf as dp
import estimativa

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

VERMELHO, VERDE = dp.COR["red"], dp.COR["green"]

# (posição do lead, alvo passado, espera marcação de meta?, cor da barra do lead)
CASOS = [
    (1, None, False, VERDE),
    (2, None, False, VERDE),
    (3, None, False, VERDE),
    (4, 3, True, VERMELHO),
    (6, 3, True, VERMELHO),
    (20, 3, True, VERMELHO),
    (71, 3, True, VERMELHO),
    # defesa extra: mesmo que alguém passe um alvo ATRÁS do lead, não marca
    (2, 3, False, VERDE),
    (1, 3, False, VERDE),
]


def _cor_da_barra_do_lead(svg: str):
    m = re.search(r'fill="(#[0-9a-fA-F]{6})"[^>]*>você<', svg)
    return m.group(1) if m else None


def main():
    falhas = 0
    print(f"{'lead':>6}  {'alvo':>5}  {'meta?':>6}  cor")
    for pos, alvo, espera_meta, espera_cor in CASOS:
        svg = dp.svg_curva_cliques(pos, alvo, estimativa.ctr_da_posicao)
        tem_meta = ">meta<" in svg
        cor = _cor_da_barra_do_lead(svg)
        ok_meta = tem_meta == espera_meta
        ok_cor = cor == espera_cor
        falhas += 0 if (ok_meta and ok_cor) else 1
        selo = "ok  " if (ok_meta and ok_cor) else "FALHOU"
        nome = {VERMELHO: "vermelho", VERDE: "verde"}.get(cor, str(cor))
        print(f"  {selo} {pos:>3}º  {str(alvo):>5}  {str(tem_meta):>6}  {nome}")
        if not ok_meta:
            print(f"        meta: esperava {espera_meta}, veio {tem_meta}")
        if not ok_cor:
            esperado = {VERMELHO: "vermelho", VERDE: "verde"}.get(espera_cor)
            print(f"        cor: esperava {esperado}, veio {nome}")

    # a meta, quando marcada, tem de estar À FRENTE do lead
    svg = dp.svg_curva_cliques(20, 3, estimativa.ctr_da_posicao)
    ok = ">meta<" in svg and ">você<" in svg
    falhas += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FALHOU'} lead em 20º mostra meta e posição própria")

    total = len(CASOS) + 1
    print()
    print(f"RESULTADO: {total - falhas}/{total} passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
