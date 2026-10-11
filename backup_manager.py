# -*- coding: utf-8 -*-
"""backup_manager.py — Backup e migração dos DADOS do LeadForge."""

import json
import os
import shutil
import socket
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import config

RAIZ = Path(__file__).resolve().parent
BACKUP_DIR = RAIZ / "data" / "backups"
EXPORT_DIR = RAIZ / "data" / "exports"
SLOTS = 30
FORMATO = 2          # versão do formato do pacote


# INVENTÁRIO — a única lista que decide o que é "dado do usuário" Cada item vira
# uma pasta dentro do zip.

def _pcz() -> Path:
    return Path.home() / ".pcz"


def _ig_sessions() -> Path:
    return Path(getattr(config, "IG_SESSOES_DIR", RAIZ / "data" / "ig_sessions"))


def _ig_legado() -> Path:
    return Path(getattr(config, "IG_PERFIL_LEGADO", RAIZ / "data" / "ig_session"))


def _wa_sessao() -> Path:
    """Sessão do whatsapp-web.js (LocalAuth). Fica fora do OneDrive de propósito."""
    base = _ig_sessions().parent
    for cand in (base / "wa-sessao", RAIZ / "senders" / "whatsapp_bot" / ".wwebjs_auth"):
        if cand.exists():
            return cand
    return base / "wa-sessao"


def inventario() -> List[Dict]:
    """O que o backup carrega, em ordem de importância."""
    return [
        # ── bancos ────────────────────────────────────────────────────────────
        {"chave": "leadforge_db", "destino": "bancos",
         "origem": Path(config.LEADFORGE_DB), "tipo": "arquivo", "pesado": False,
         "rotulo": "Banco principal (leads, funil, conversas, campanhas)"},
        {"chave": "pcz_db", "destino": "bancos",
         "origem": _pcz() / "pcz.db", "tipo": "arquivo", "pesado": False,
         "rotulo": "Banco do e-mail (listas, templates, campanhas, contas)"},

        # ── credenciais e sessões ─────────────────────────────────────────────
        {"chave": "gmail_tokens", "destino": "credenciais/tokens",
         "origem": _pcz() / "tokens", "tipo": "pasta", "pesado": False,
         "rotulo": "Contas de Gmail autorizadas (tokens OAuth)"},
        {"chave": "gmail_credentials", "destino": "credenciais",
         "origem": _pcz() / "credentials.json", "tipo": "arquivo", "pesado": False,
         "rotulo": "Credencial do app Google"},
        {"chave": "env", "destino": "credenciais",
         "origem": RAIZ / ".env", "tipo": "arquivo", "pesado": False,
         "rotulo": "Chaves do .env (IA, limites)"},
        {"chave": "wa_sessao", "destino": "sessoes/whatsapp",
         "origem": _wa_sessao(), "tipo": "pasta", "pesado": True,
         "rotulo": "Sessões do WhatsApp (evita reescanear o QR)"},
        {"chave": "ig_sessoes", "destino": "sessoes/instagram",
         "origem": _ig_sessions(), "tipo": "pasta", "pesado": True,
         "rotulo": "Sessões do Instagram (evita relogar as contas)"},
        {"chave": "ig_legado", "destino": "sessoes/instagram_legado",
         "origem": _ig_legado(), "tipo": "pasta", "pesado": True,
         "rotulo": "Sessão antiga do Instagram"},

        # ── configuração de trabalho ──────────────────────────────────────────
        {"chave": "ig_nichos", "destino": "nichos",
         "origem": RAIZ / "senders" / "dm_bot" / "perfis", "tipo": "pasta", "pesado": False,
         "rotulo": "Nichos do Instagram (palavras-chave, fontes, modelos, processados)"},
        {"chave": "ig_perfis_dados", "destino": "nichos_dados",
         "origem": RAIZ / "data" / "ig_perfis", "tipo": "pasta", "pesado": False,
         "rotulo": "Dados dos perfis de Instagram"},

        # ── produção ──────────────────────────────────────────────────────────
        {"chave": "anexos", "destino": "anexos",
         "origem": Path(config.DATA_DIR) / "attachments", "tipo": "pasta", "pesado": False,
         "rotulo": "Anexos dos leads"},
        # as propostas caem soltas em `out/`, não numa subpasta; `logs` fica de
        # fora aqui porque é um item próprio logo abaixo
        {"chave": "materiais", "destino": "materiais",
         "origem": RAIZ / "out", "tipo": "pasta", "pesado": False,
         "ignorar_sub": {"logs"},
         "rotulo": "Materiais, propostas e PDFs gerados"},
        {"chave": "pop_cache", "destino": "cache",
         "origem": Path(config.DATA_DIR) / "populacao_cache.json", "tipo": "arquivo",
         "pesado": False, "rotulo": "Cache do IBGE"},

        # ── logs ──────────────────────────────────────────────────────────────
        {"chave": "logs", "destino": "logs",
         "origem": RAIZ / "out" / "logs", "tipo": "pasta", "pesado": False,
         "rotulo": "Logs de execução"},
        {"chave": "log_prospeccao", "destino": "logs",
         "origem": RAIZ / "log_prospeccao.txt", "tipo": "arquivo", "pesado": False,
         "rotulo": "Log da prospecção de Instagram"},
    ]


# arquivos que nunca entram, mesmo dentro de uma pasta que entra
IGNORAR = {".lock", ".tmp", "SingletonLock", "SingletonCookie", "SingletonSocket",
           "LOCK", "lockfile", ".DS_Store", "Thumbs.db"}
IGNORAR_DIRS = {"node_modules", "__pycache__", ".git", "Crashpad", "ShaderCache",
                "GrShaderCache", "GraphiteDawnCache", "component_crx_cache"}


def _incluir(p: Path, raiz: Path = None, ignorar_sub: set = None) -> bool:
    if p.name in IGNORAR or p.name.endswith(".pyc"):
        return False
    if any(parte in IGNORAR_DIRS for parte in p.parts):
        return False
    if ignorar_sub and raiz:
        try:
            if p.relative_to(raiz).parts[0] in ignorar_sub:
                return False
        except (ValueError, IndexError):
            pass
    return True


def _tamanho(origem: Path, tipo: str, ignorar_sub: set = None) -> int:
    try:
        if tipo == "arquivo":
            return origem.stat().st_size if origem.is_file() else 0
        if not origem.is_dir():
            return 0
        return sum(f.stat().st_size for f in origem.rglob("*")
                   if f.is_file() and _incluir(f, origem, ignorar_sub))
    except OSError:
        return 0


def previsao(incluir_pesados: bool = False, incluir_logs: bool = True) -> Dict:
    """O que existe e quanto pesa — para a tela mostrar ANTES de gerar o pacote."""
    itens, total = [], 0
    for it in inventario():
        if it["pesado"] and not incluir_pesados:
            continue
        if it["destino"].startswith("logs") and not incluir_logs:
            continue
        origem = it["origem"]
        existe = origem.exists()
        tam = _tamanho(origem, it["tipo"], it.get("ignorar_sub")) if existe else 0
        total += tam
        itens.append({"chave": it["chave"], "rotulo": it["rotulo"],
                      "existe": existe, "pesado": it["pesado"],
                      "tamanho_mb": round(tam / 1048576, 2),
                      "caminho": str(origem)})
    return {"itens": itens, "total_mb": round(total / 1048576, 2)}


# ──────────────────────────────────────────────────────────────────────────────
#  GRAVAÇÃO
# ──────────────────────────────────────────────────────────────────────────────

def _checkpoint_wal():
    """Fecha o WAL antes de copiar o .db."""
    import sqlite3
    for caminho in (Path(config.LEADFORGE_DB), _pcz() / "pcz.db"):
        if not caminho.exists():
            continue
        try:
            con = sqlite3.connect(str(caminho), timeout=5)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
        except Exception:
            pass


def _escrever_pacote(zip_file: Path, incluir_pesados: bool, incluir_logs: bool,
                     origem_rotulo: str) -> Dict:
    _checkpoint_wal()
    zip_file.parent.mkdir(parents=True, exist_ok=True)

    conteudo, arquivos, bytes_ = [], 0, 0
    with zipfile.ZipFile(zip_file, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for it in inventario():
            if it["pesado"] and not incluir_pesados:
                continue
            if it["destino"].startswith("logs") and not incluir_logs:
                continue
            origem, destino = it["origem"], it["destino"]
            if not origem.exists():
                continue

            n = 0
            if it["tipo"] == "arquivo":
                if _incluir(origem):
                    zf.write(origem, f"dados/{destino}/{origem.name}")
                    n, bytes_ = 1, bytes_ + origem.stat().st_size
            else:
                for f in origem.rglob("*"):
                    if not f.is_file() or not _incluir(f, origem, it.get("ignorar_sub")):
                        continue
                    try:
                        zf.write(f, f"dados/{destino}/{f.relative_to(origem)}")
                        n += 1
                        bytes_ += f.stat().st_size
                    except (OSError, ValueError):
                        # arquivo em uso (perfil de navegador aberto) não trava o backup
                        continue
            arquivos += n
            conteudo.append({"chave": it["chave"], "destino": destino,
                             "rotulo": it["rotulo"], "arquivos": n})

        manifesto = {
            "formato": FORMATO,
            "produto": "LeadForge",
            "gerado_em": datetime.now().isoformat(timespec="seconds"),
            "origem": origem_rotulo,
            "maquina": socket.gethostname(),
            "inclui_sessoes": incluir_pesados,
            "inclui_logs": incluir_logs,
            "arquivos": arquivos,
            "bytes_originais": bytes_,
            "conteudo": conteudo,
            "resumo": _resumo_do_banco(),
        }
        zf.writestr("manifesto.json", json.dumps(manifesto, ensure_ascii=False, indent=2))
        zf.writestr("LEIA-ME.txt",
                    "Pacote de DADOS do LeadForge.\n\n"
                    "Contém apenas dados do usuário (bancos, contas, sessões, nichos,\n"
                    "materiais e logs). NÃO contém o programa.\n\n"
                    "Para restaurar numa instalação nova: aba Configurações >\n"
                    "Backup e migração > Importar pacote.\n")

    return {"zip": zip_file, "manifesto": manifesto}


def _resumo_do_banco() -> Dict:
    """Contagens que deixam o pacote conferível sem abrir o banco."""
    out = {}
    try:
        import db
        with db.conn() as con:
            for rotulo, sql in (("leads", "SELECT COUNT(*) FROM leads"),
                                ("quadros", "SELECT COUNT(*) FROM kanban_quadros"),
                                ("conversas", "SELECT COUNT(*) FROM conversas"),
                                ("templates", "SELECT COUNT(*) FROM msg_templates"),
                                ("contas", "SELECT COUNT(*) FROM accounts")):
                try:
                    out[rotulo] = con.execute(sql).fetchone()[0]
                except Exception:
                    pass
    except Exception:
        pass
    return out


# ──────────────────────────────────────────────────────────────────────────────
#  BACKUP DIÁRIO (30 slots no disco local)
# ──────────────────────────────────────────────────────────────────────────────

def _current_slot() -> int:
    return (datetime.now().timetuple().tm_yday - 1) % SLOTS + 1


def _slot_dir(slot: int) -> Path:
    return BACKUP_DIR / f"slot_{slot:02d}"


def run_backup(forced_slot: int = None, incluir_sessoes: bool = False) -> dict:
    """
    Cópia diária. Sem sessões por padrão: são 260 MB de perfil de Chromium que não mudam
    de um dia para o outro e encheriam o disco em 30 slots.
    """
    slot = forced_slot if forced_slot is not None else _current_slot()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slot_dir = _slot_dir(slot)
    if slot_dir.exists():
        shutil.rmtree(slot_dir, ignore_errors=True)
    slot_dir.mkdir(parents=True, exist_ok=True)

    r = _escrever_pacote(slot_dir / f"backup_{ts}.zip", incluir_sessoes, True,
                         f"backup automático slot {slot}")
    zip_file, man = r["zip"], r["manifesto"]
    size = zip_file.stat().st_size
    _log_backup(slot, str(zip_file), size, man["arquivos"])
    return {"ok": True, "slot": slot, "arquivo": str(zip_file),
            "tamanho_kb": round(size / 1024, 1), "files_added": man["arquivos"],
            "resumo": man["resumo"]}


def _log_backup(slot: int, path: str, size: int, files: int):
    try:
        import db
        with db.conn() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS backup_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot INTEGER, path TEXT, size_bytes INTEGER,
                    files_count INTEGER, created_at TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            con.execute("INSERT INTO backup_log (slot, path, size_bytes, files_count) "
                        "VALUES (?,?,?,?)", (slot, path, size, files))
    except Exception:
        pass


def listar_backups() -> list:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backups = []
    for slot_dir in sorted(BACKUP_DIR.iterdir()):
        if slot_dir.is_dir() and slot_dir.name.startswith("slot_"):
            for zf in sorted(slot_dir.glob("backup_*.zip"), reverse=True):
                backups.append({
                    "slot": int(slot_dir.name.split("_")[1]),
                    "arquivo": str(zf),
                    "tamanho_kb": round(zf.stat().st_size / 1024, 1),
                    "data": zf.stem.replace("backup_", ""),
                })
    return backups


# ──────────────────────────────────────────────────────────────────────────────
#  EXPORTAR / IMPORTAR  (migração para outra instalação)
# ──────────────────────────────────────────────────────────────────────────────

def exportar(incluir_sessoes: bool = True, incluir_logs: bool = True,
             destino: str = None) -> Dict:
    """Gera o pacote para levar a outra máquina."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    alvo = Path(destino) if destino else EXPORT_DIR / f"leadforge_dados_{ts}.zip"

    r = _escrever_pacote(alvo, incluir_sessoes, incluir_logs, "exportação manual")
    man, size = r["manifesto"], r["zip"].stat().st_size
    return {"ok": True, "arquivo": str(r["zip"]), "nome": r["zip"].name,
            "tamanho_mb": round(size / 1048576, 2), "arquivos": man["arquivos"],
            "inclui_sessoes": incluir_sessoes, "resumo": man["resumo"]}


def listar_exports() -> list:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    return [{"arquivo": str(z), "nome": z.name,
             "tamanho_mb": round(z.stat().st_size / 1048576, 2),
             "data": datetime.fromtimestamp(z.stat().st_mtime).strftime("%d/%m/%Y %H:%M")}
            for z in sorted(EXPORT_DIR.glob("*.zip"),
                            key=lambda p: p.stat().st_mtime, reverse=True)]


def inspecionar(zip_path: str) -> Dict:
    """Lê o manifesto SEM restaurar nada — a tela mostra o que há dentro antes."""
    src = Path(zip_path)
    if not src.exists():
        return {"ok": False, "erro": "arquivo não encontrado"}
    try:
        with zipfile.ZipFile(src, "r") as zf:
            nomes = zf.namelist()
            if "manifesto.json" in nomes:
                man = json.loads(zf.read("manifesto.json").decode("utf-8"))
                return {"ok": True, "manifesto": man, "antigo": False,
                        "tamanho_mb": round(src.stat().st_size / 1048576, 2)}
            # pacote do formato 1 (só banco +.env + anexos), ainda restaurável
            return {"ok": True, "antigo": True,
                    "manifesto": {"formato": 1, "arquivos": len(nomes),
                                  "conteudo": [], "resumo": {}},
                    "tamanho_mb": round(src.stat().st_size / 1048576, 2)}
    except zipfile.BadZipFile:
        return {"ok": False, "erro": "o arquivo não é um zip válido"}


def _bots_de_pe() -> List[str]:
    """
    Quem está com os arquivos abertos. Importar por cima de um bot rodando corrompe o
    banco e o perfil do navegador — o SQLite fica com o WAL de uma base e o .db de outra.
    """
    vivos = []
    for nome, porta in (("dashboard", 5020), ("bot do WhatsApp", 5021),
                        ("bot de DM", 5022)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", porta)) == 0:
                vivos.append(nome)
    return vivos


def importar(zip_path: str, restaurar_sessoes: bool = True,
             restaurar_logs: bool = False, forcar: bool = False) -> Dict:
    """Restaura um pacote NESTA instalação."""
    src = Path(zip_path)
    info = inspecionar(str(src))
    if not info.get("ok"):
        return info

    vivos = [v for v in _bots_de_pe() if v != "dashboard"]
    if vivos and not forcar:
        return {"ok": False, "precisa_forcar": True,
                "erro": "feche os bots antes de importar: " + ", ".join(vivos)
                        + ". Restaurar com eles rodando corrompe banco e sessão."}

    try:
        seguranca = run_backup(forced_slot=0, incluir_sessoes=False)
    except Exception as e:
        seguranca = {"ok": False, "erro": str(e)}

    # onde cada pasta do pacote vai parar nesta máquina
    destinos = {
        "bancos/leadforge.db": Path(config.LEADFORGE_DB),
        "bancos/pcz.db": _pcz() / "pcz.db",
        "credenciais/credentials.json": _pcz() / "credentials.json",
        "credenciais/.env": RAIZ / ".env",
        "credenciais/tokens": _pcz() / "tokens",
        "sessoes/whatsapp": _wa_sessao(),
        "sessoes/instagram": _ig_sessions(),
        "sessoes/instagram_legado": _ig_legado(),
        "nichos": RAIZ / "senders" / "dm_bot" / "perfis",
        "nichos_dados": RAIZ / "data" / "ig_perfis",
        "anexos": Path(config.DATA_DIR) / "attachments",
        "materiais": RAIZ / "out" / "materiais",
        "cache/populacao_cache.json": Path(config.DATA_DIR) / "populacao_cache.json",
        "logs": RAIZ / "out" / "logs",
    }

    escritos, ignorados, erros = 0, 0, []
    with zipfile.ZipFile(src, "r") as zf:
        for nome in zf.namelist():
            if nome.endswith("/") or nome in ("manifesto.json", "LEIA-ME.txt"):
                continue

            if info.get("antigo"):
                # formato 1: banco na raiz do zip, anexos em attachments/
                if nome.endswith(".db"):
                    alvo = Path(config.DATA_DIR) / Path(nome).name
                elif nome.startswith("attachments/"):
                    alvo = Path(config.DATA_DIR) / "attachments" / nome[len("attachments/"):]
                elif nome == ".env":
                    alvo = RAIZ / ".env"
                else:
                    ignorados += 1
                    continue
            else:
                if not nome.startswith("dados/"):
                    ignorados += 1
                    continue
                rel = nome[len("dados/"):]
                if rel.startswith("sessoes/") and not restaurar_sessoes:
                    ignorados += 1
                    continue
                if rel.startswith("logs/") and not restaurar_logs:
                    ignorados += 1
                    continue
                alvo = None
                for prefixo, base in sorted(destinos.items(), key=lambda x: -len(x[0])):
                    if rel == prefixo:
                        alvo = base
                        break
                    if rel.startswith(prefixo + "/"):
                        alvo = base / rel[len(prefixo) + 1:]
                        break
                if alvo is None:
                    ignorados += 1
                    continue

            try:
                alvo.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(nome) as f_in, open(alvo, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                escritos += 1
            except OSError as e:
                erros.append(f"{nome}: {e}")

    # o WAL antigo apontaria para o banco que acabou de ser substituído
    for banco in (Path(config.LEADFORGE_DB), _pcz() / "pcz.db"):
        for sufixo in ("-wal", "-shm"):
            resto = Path(str(banco) + sufixo)
            if resto.exists():
                try:
                    resto.unlink()
                except OSError:
                    pass

    return {"ok": True, "escritos": escritos, "ignorados": ignorados,
            "erros": erros[:10], "manifesto": info.get("manifesto", {}),
            "backup_seguranca": seguranca.get("arquivo") if seguranca.get("ok") else None,
            "aviso": "Reinicie o LeadForge para carregar os dados restaurados."}


def restaurar_backup(zip_path: str) -> dict:
    """Compatibilidade: restaura um backup do disco local."""
    return importar(zip_path, restaurar_sessoes=True, restaurar_logs=False, forcar=True)


if __name__ == "__main__":
    import sys
    if "--exportar" in sys.argv:
        print(json.dumps(exportar(incluir_sessoes="--sem-sessoes" not in sys.argv),
                         ensure_ascii=False, indent=2))
    elif "--previsao" in sys.argv:
        print(json.dumps(previsao(incluir_pesados=True), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(run_backup(), ensure_ascii=False, indent=2))
