"""versionar.py — Cria uma snapshot versionada do projeto."""

import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
DATA = RAIZ / "data"
VERSOES = DATA / "versoes"
BANCO = DATA / "leadforge.db"
CHANGELOG = RAIZ / "CHANGELOG.md"


def _slug(texto: str) -> str:
    """Gera um slug curto a partir da descricao."""
    texto = texto.lower().strip()
    texto = re.sub(r"[^\w\s-]", "", texto)
    texto = re.sub(r"[\s_]+", "-", texto)
    return texto[:40].rstrip("-") or "snapshot"


def _proximo_numero() -> int:
    """Numero da proxima versao, baseado nas existentes."""
    if not VERSOES.exists():
        return 1
    existentes = []
    for f in VERSOES.iterdir():
        m = re.match(r"leadforge-v(\d+)-", f.name)
        if m:
            existentes.append(int(m.group(1)))
    return max(existentes, default=0) + 1


def _ler_changelog() -> str:
    if CHANGELOG.exists():
        return CHANGELOG.read_text(encoding="utf-8")
    return ""


def versionar(descricao: str = ""):
    """Cria uma nova versao do banco."""
    if not BANCO.exists():
        print("ERRO: data/leadforge.db nao encontrado. Rode a migracao primeiro.")
        return None

    VERSOES.mkdir(parents=True, exist_ok=True)

    num = _proximo_numero()
    agora = datetime.now()
    timestamp = agora.strftime("%Y%m%d-%H%M%S")
    slug = _slug(descricao) if descricao else "snapshot"
    nome = f"leadforge-v{num:03d}-{timestamp}-{slug}.db"
    destino = VERSOES / nome

    shutil.copy2(BANCO, destino)

    # Atualiza CHANGELOG
    entry = (
        f"## v{num:03d} — {agora.strftime('%d/%m/%Y %H:%M')}\n\n"
        f"- **Arquivo:** `data/versoes/{nome}`\n"
    )
    if descricao:
        entry += f"- **Descricao:** {descricao}\n"
    entry += f"- **Banco:** {BANCO.name}\n\n"

    changelog = _ler_changelog()
    if changelog.startswith("# "):
        # Insere depois do cabecalho
        linhas = changelog.split("\n", 2)
        if len(linhas) >= 3:
            header = linhas[0] + "\n" + linhas[1] + "\n"
            resto = linhas[2]
            changelog = header + "\n" + entry + "\n" + resto
        else:
            changelog = linhas[0] + "\n\n" + entry + changelog
    else:
        changelog = f"# LeadForge — Historico de Versoes\n\n{entry}\n{changelog}"

    CHANGELOG.write_text(changelog, encoding="utf-8")

    print(f"Versao v{num:03d} criada: data/versoes/{nome}")
    if descricao:
        print(f"  Descricao: {descricao}")
    print(f"  Tamanho: {destino.stat().st_size / 1024:.1f} KB")

    return destino


if __name__ == "__main__":
    desc = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    versionar(desc)
