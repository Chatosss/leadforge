"""test_parse_avaliacoes.py — Leitura do nº de avaliações do card do Maps."""

import re
import sys

import ingest

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# (entrada, esperado)
CASOS_PARSE = [
    ("114", 114),
    ("1.361", 1361),
    ("(114)", 114),
    ("114 avaliações", 114),
    ("12", 12),
    ("", 0),
    (None, 0),
    # abreviações: o que quebrava
    ("1,2 mil", 1200),
    ("(1,2 mil)", 1200),
    ("2 mil", 2000),
    ("10 mil", 10000),
    ("1,5 mil", 1500),
    ("1.2K", 1200),
    ("1,2 M", 1200000),
]

# o mesmo padrão usado em scrap_live._JS_CARD, para não divergirem em silêncio
RE_CARD = re.compile(r"\(([\d.,]+\s*(?:mil|k|m)?)\)", re.I)

CASOS_CARD = [
    ("4,8 (114)", "114"),
    ("4,9 (1,2 mil)", "1,2 mil"),
    ("5,0 (1.361)", "1.361"),
    ("4,7 (2 mil)", "2 mil"),
    ("4,8 (1.2K)", "1.2K"),
]


def main():
    falhas = 0

    print("parse_total:")
    for entrada, esperado in CASOS_PARSE:
        obtido = ingest.parse_total(entrada)
        ok = obtido == esperado
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} {str(entrada)!r:16} -> {obtido:<9} "
              f"esperado {esperado}")

    print()
    print("extração do card (mesmo padrão do _JS_CARD):")
    for texto, esperado in CASOS_CARD:
        m = RE_CARD.search(texto)
        obtido = m.group(1) if m else None
        ok = obtido == esperado
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} {texto!r:18} -> {obtido!r:12} "
              f"esperado {esperado!r}")

    total = len(CASOS_PARSE) + len(CASOS_CARD)
    print()
    print(f"RESULTADO: {total - falhas}/{total} passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
