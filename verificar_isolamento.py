"""verificar_isolamento.py — Prova automatizada da REGRA ZERO."""

import sqlite3
from pathlib import Path

import config
import ingest

PASTAS_PROIBIDAS = [
    Path.home() / "maps_scraper",
    Path(__file__).resolve().parent.parent / "Prospecção" / "dminstagram",
    Path(__file__).resolve().parent.parent / "Prospecção" / "mailmarketing",
]


def _dentro_de(caminho: Path, pasta: Path) -> bool:
    try:
        caminho.resolve().relative_to(pasta.resolve())
        return True
    except Exception:
        return False


def teste_origem_readonly() -> bool:
    """Abrir a origem via ingest._open_ro e tentar escrever DEVE falhar."""
    if not config.MAPS_DB_ORIGEM.exists():
        print("… origem não existe (ok — nada a ler). Pulando prova de RO.")
        return True
    con = ingest._open_ro(config.MAPS_DB_ORIGEM)
    try:
        con.execute("CREATE TABLE _hack_(x)")
        print("❌ FALHA GRAVE: consegui ESCREVER na origem (não está read-only!)")
        return False
    except sqlite3.OperationalError as e:
        ok = "readonly" in str(e).lower() or "read-only" in str(e).lower()
        print(f"✅ Origem é somente-leitura (escrita bloqueada: {e})")
        return ok
    finally:
        con.close()


def teste_caminhos_de_escrita() -> bool:
    """Todos os bancos que o projeto ESCREVE ficam fora das pastas proibidas."""
    escritos = [
        config.LEADFORGE_DB,
        config.MAIL_BOT_DB,
        config.DATA_DIR / "maps_scraper_clinicas.db",
        config.DATA_DIR / "seed_source.db",
    ]
    ok = True
    for alvo in escritos:
        dentro = any(_dentro_de(alvo, p) for p in PASTAS_PROIBIDAS)
        marca = "❌" if dentro else "✅"
        print(f"{marca} escrita em: {alvo}")
        if dentro:
            ok = False
    return ok


def teste_sem_vazamento() -> bool:
    """Nenhum arquivo com 'clinica' dentro das pastas proibidas."""
    ok = True
    for pasta in PASTAS_PROIBIDAS:
        if not pasta.exists():
            continue
        vazados = [p for p in pasta.rglob("*clinica*")]
        if vazados:
            ok = False
            for v in vazados:
                print(f"❌ vazou para produção: {v}")
    if ok:
        print("✅ Nenhum artefato do projeto dentro das pastas de produção")
    return ok


if __name__ == "__main__":
    print("═" * 60)
    print("VERIFICAÇÃO DE ISOLAMENTO (REGRA ZERO)")
    print("═" * 60)
    r1 = teste_origem_readonly()
    print()
    r2 = teste_caminhos_de_escrita()
    print()
    r3 = teste_sem_vazamento()
    print("\n" + "═" * 60)
    if r1 and r2 and r3:
        print("✅ TUDO ISOLADO — Regra Zero respeitada.")
    else:
        print("❌ ATENÇÃO: violação de isolamento detectada acima.")
        raise SystemExit(1)
