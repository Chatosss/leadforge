"""ingest.py — Lê empresas do banco de origem (maps_data.db) e carrega em `leads`."""

import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Optional

import config
import db


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_nota(texto) -> Optional[float]:
    """'4,3' -> 4.3; '4.3' -> 4.3; '' / 'Sem avaliações' -> None."""
    if texto is None:
        return None
    s = str(texto).strip()
    if not s:
        return None
    # pega o primeiro número tipo 4,3 ou 4.3
    m = re.search(r"(\d+[.,]?\d*)", s)
    if not m:
        return None
    val = m.group(1).replace(",", ".")
    try:
        nota = float(val)
    except ValueError:
        return None
    return nota if 0 <= nota <= 5 else None


def parse_total(texto) -> int:
    """
    '1.234' -> 1234; '(1.234)' -> 1234; '123' -> 123; '' -> 0. E a forma abreviada do
    Maps: '1,2 mil' -> 1200; '2 mil' -> 2000; '1.2K' -> 1200.
    """
    if texto is None:
        return 0
    s = str(texto).strip().lower()

    # abreviação: o separador decimal aqui é vírgula (pt-BR), e o ponto é milhar
    m = re.search(r"([\d.,]+)\s*(mil|k|m)\b", s)
    if m:
        num = m.group(1)
        # '1.234,5' -> 1234.5; '1,2' -> 1.2; '1.2' (formato K) -> 1.2
        if "," in num:
            num = num.replace(".", "").replace(",", ".")
        elif num.count(".") == 1 and len(num.split(".")[1]) <= 2:
            pass                      # '1.2K': o ponto é decimal
        else:
            num = num.replace(".", "")
        try:
            valor = float(num)
        except ValueError:
            return 0
        return int(round(valor * (1_000_000 if m.group(2) == "m" else 1_000)))

    digits = re.sub(r"[^\d]", "", s)  # remove pontos, parênteses, espaços
    return int(digits) if digits else 0


def _tem_valor(campo) -> int:
    return 1 if (campo and str(campo).strip()) else 0


def detectar_nicho(categoria: str) -> str:
    cat = _sem_acentos(str(categoria or "").lower())
    for nicho, palavras in config.NICHO_KEYWORDS.items():
        for p in palavras:
            if _sem_acentos(p.lower()) in cat:
                return nicho
    return config.NICHO_PADRAO


def _sem_acentos(txt: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", txt)
                   if unicodedata.category(c) != "Mn")


# ── Origem (somente leitura) ──────────────────────────────────────────────────

def _open_ro(path: Path) -> sqlite3.Connection:
    """Abre o banco de origem em modo SOMENTE LEITURA (Regra Zero)."""
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _tem_coluna(con: sqlite3.Connection, tabela: str, coluna: str) -> bool:
    cols = con.execute(f"PRAGMA table_info({tabela})").fetchall()
    return any(c["name"] == coluna for c in cols)


# ── Ingest ────────────────────────────────────────────────────────────────────

def ingest(source_db: Optional[Path] = None) -> int:
    """
    Carrega as empresas do banco de origem em `leads`. Retorna quantos leads foram
    inseridos/atualizados.
    """
    source_db = Path(source_db) if source_db else config.MAPS_DB_ORIGEM
    if not source_db.exists():
        print(f"⚠ Banco de origem não encontrado: {source_db}")
        return 0

    db.init_db()
    con = _open_ro(source_db)
    try:
        tem_posicao = _tem_coluna(con, "businesses", "posicao")
        order = "posicao ASC, id ASC" if tem_posicao else "id ASC"
        rows = con.execute(f"SELECT * FROM businesses ORDER BY {order}").fetchall()
    finally:
        con.close()

    # fallback de posição: rank por ordem de coleta dentro da categoria
    rank_por_nicho: dict = {}
    total = 0
    for r in rows:
        row = dict(r)
        nicho = detectar_nicho(row.get("categoria", ""))

        if tem_posicao and row.get("posicao") not in (None, "", 0):
            posicao = int(row["posicao"])
        else:
            rank_por_nicho[nicho] = rank_por_nicho.get(nicho, 0) + 1
            posicao = rank_por_nicho[nicho]

        lead = {
            "maps_url":             row.get("maps_url"),
            "nome":                 row.get("nome", ""),
            "categoria":            row.get("categoria", ""),
            "nicho":                nicho,
            "avaliacao_num":        parse_nota(row.get("avaliacao")),
            "total_avaliacoes_num": parse_total(row.get("total_avaliacoes")),
            "tem_site":             _tem_valor(row.get("website")),
            "tem_instagram":        _tem_valor(row.get("instagram")),
            "instagram":            (row.get("instagram") or "").strip(),
            "telefone":             (row.get("telefone") or "").strip(),
            "website":              (row.get("website") or "").strip(),
            "endereco":             (row.get("endereco") or "").strip(),
            "posicao":              posicao,
        }
        db.upsert_lead(lead)
        total += 1

    print(f"✅ Ingest concluído: {total} leads carregados de {source_db.name} "
          f"(posição {'real' if tem_posicao else 'derivada'}).")
    return total


if __name__ == "__main__":
    import sys
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    ingest(src)
    print("Funil:", db.funnel_counts())
