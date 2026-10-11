"""
channel_backends.py — Suporte completo de backend para os canais Instagram e E-mail.
Conecta os dados de nichos, templates, listas, contas e bots ao LeadForge.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import config
import db

RAIZ = Path(__file__).resolve().parent

# Caminhos para bots externos (somente leitura de estrutura / escrita apenas nas pastas permitidas)
IG_DIR = RAIZ.parent / "Prospecção" / "dminstagram"
MAIL_DIR = RAIZ.parent / "Prospecção" / "mailmarketing"
SESSIONS_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ProspeccaoHub" / "sessions"


# ══════════════════════════════════════════════════════════════════════════════
# 📸 INSTAGRAM BACKEND
# ══════════════════════════════════════════════════════════════════════════════

def ig_perfis_dir() -> Path:
    """A pasta de nichos que o BOT realmente usa."""
    pdir = RAIZ / "senders" / "dm_bot" / "perfis"
    pdir.mkdir(parents=True, exist_ok=True)
    return pdir


def ig_perfis_dir_origem() -> Optional[Path]:
    """A pasta do bot original. SOMENTE LEITURA — serve para importar nicho."""
    origem = IG_DIR / "perfis"
    return origem if origem.exists() else None


# Nomes de arquivo do bot. Estavam divergentes no painel, e por isso a tela de
# Fontes aparecia vazia com o arquivo cheio: ele procurava `fontes.txt` e
# `lista_negra.txt`, que não existem em lugar nenhum.
IG_ARQUIVOS = {
    "fontes": "fontes_alvo.txt",
    "fontes_usadas": "fontes_usadas.txt",
    "palavras_chave": "palavras_chave.txt",
    "palavras_contexto": "palavras_contexto.txt",
    "lista_negra": "palavras_bloqueadas.txt",
}


def ig_listar_nichos() -> List[Dict]:
    """Lista os nichos do bot, com estatísticas de DM e rastreio."""
    pdir = ig_perfis_dir()
    nichos = []
    for d in sorted(pdir.iterdir()):
        if not d.is_dir():
            continue
        processados = d / "processados.txt"
        rastreio = d / "rastreio_dms.json"
        templates = d / "templates_msg.json"

        count = 0
        if processados.exists():
            count = sum(1 for line in processados.read_text(
                encoding="utf-8", errors="ignore").splitlines() if line.strip())

        tracking = {}
        if rastreio.exists():
            try:
                tracking = json.loads(rastreio.read_text(encoding="utf-8"))
            except Exception:
                pass

        tpl_count = tpl_ativos = 0
        if templates.exists():
            try:
                tpls = json.loads(templates.read_text(encoding="utf-8"))
                tpl_count = len(tpls)
                tpl_ativos = sum(1 for t in tpls if t.get("ativo", True))
            except Exception:
                pass

        # Pendentes = o que esta na fila MENOS o que ja foi varrido.
        fontes = d / IG_ARQUIVOS["fontes"]
        usadas_f = d / IG_ARQUIVOS["fontes_usadas"]
        n_fontes = 0
        if fontes.exists():
            usadas_set = set()
            if usadas_f.exists():
                usadas_set = {l.strip() for l in usadas_f.read_text(
                    encoding="utf-8", errors="ignore").splitlines() if l.strip()}
            n_fontes = len({l.strip() for l in fontes.read_text(
                encoding="utf-8", errors="ignore").splitlines()
                if l.strip() and l.strip() not in usadas_set})

        replied = sum(1 for v in tracking.values() if v.get("respondeu"))
        converted = sum(1 for v in tracking.values() if v.get("converteu"))
        nichos.append({
            "nicho": d.name,
            "dms_enviadas": count,
            "responderam": replied,
            "convertidos": converted,
            "taxa_resposta": round((replied / count * 100), 1) if count else 0,
            "templates_total": tpl_count,
            "templates_ativos": tpl_ativos,
            "fontes_pendentes": n_fontes,
        })
    return nichos


def ig_nichos_importaveis() -> List[str]:
    """Nichos que existem no bot original e ainda não foram copiados para cá."""
    origem = ig_perfis_dir_origem()
    if not origem:
        return []
    daqui = {d.name for d in ig_perfis_dir().iterdir() if d.is_dir()}
    return sorted(d.name for d in origem.iterdir() if d.is_dir() and d.name not in daqui)


def ig_importar_nicho(nome: str) -> Dict:
    """Copia um nicho do bot original para a pasta do bot desta cópia."""
    origem = ig_perfis_dir_origem()
    if not origem:
        return {"ok": False, "erro": "pasta do bot original não encontrada"}
    de = origem / nome
    if not de.is_dir():
        return {"ok": False, "erro": f"nicho {nome} não existe na origem"}
    para = ig_perfis_dir() / nome
    if para.exists():
        return {"ok": False, "erro": f"{nome} já existe aqui"}

    para.mkdir(parents=True, exist_ok=True)
    copiados = []
    for arq in de.iterdir():
        if arq.is_file() and arq.suffix in (".txt", ".json") and ".bak" not in arq.name:
            shutil.copy2(arq, para / arq.name)
            copiados.append(arq.name)
    return {"ok": True, "nicho": nome, "arquivos": copiados}


def ig_criar_nicho(nome: str) -> Dict:
    """Cria a pasta de um nicho novo, com os arquivos que o bot espera."""
    nome = nome.strip().replace("/", "_").replace("\\", "_")
    if not nome:
        return {"ok": False, "erro": "Nome inválido"}
    pdir = ig_perfis_dir() / nome
    if pdir.exists():
        return {"ok": False, "erro": "Nicho já existe"}
    pdir.mkdir(parents=True, exist_ok=True)
    for arquivo in IG_ARQUIVOS.values():
        (pdir / arquivo).write_text("", encoding="utf-8")
    (pdir / "processados.txt").write_text("", encoding="utf-8")
    (pdir / "rastreio_dms.json").write_text("{}", encoding="utf-8")
    (pdir / "templates_msg.json").write_text(
        json.dumps([{"id": 1, "nome": "Modelo 1", "texto": "Oi {nome}, tudo bem?",
                     "ativo": True}], indent=2, ensure_ascii=False),
        encoding="utf-8")
    return {"ok": True, "nicho": nome}


def _ig_tpl_normalizar(t: Dict, i: int) -> Dict:
    """Põe o template no formato que a tela entende."""
    ativo = t.get("ativo", True)
    if isinstance(ativo, str):
        ativo = ativo.strip().lower() in ("true", "1", "sim", "yes")
    return {
        "id": t.get("id") or (i + 1),
        "nome": t.get("nome") or f"Modelo {i + 1}",
        "texto": t.get("mensagem") or t.get("texto") or t.get("body") or "",
        "ativo": bool(ativo),
    }


def _ig_tpl_para_bot(t: Dict, i: int) -> Dict:
    """Devolve ao formato do bot, senão ele para de ler o que a tela salvou."""
    return {
        "id": t.get("id") or (i + 1),
        "nome": t.get("nome") or f"Modelo {i + 1}",
        "mensagem": t.get("texto") or t.get("mensagem") or "",
        "ativo": str(bool(t.get("ativo", True))),
    }


def ig_carregar_nicho_dados(nicho: str) -> Dict:
    """Keywords, fontes e templates de um nicho, com os nomes de arquivo do bot."""
    pdir = ig_perfis_dir() / nicho
    if not pdir.exists():
        return {"ok": False, "erro": "Nicho não encontrado"}

    def _ler(arquivo: str) -> str:
        f = pdir / arquivo
        return f.read_text(encoding="utf-8", errors="ignore") if f.exists() else ""

    templates = []
    tpl_file = pdir / "templates_msg.json"
    if tpl_file.exists():
        try:
            brutos = json.loads(tpl_file.read_text(encoding="utf-8"))
            templates = [_ig_tpl_normalizar(t, i) for i, t in enumerate(brutos)]
        except Exception:
            pass
    if not templates:
        # o bot original guardava a mensagem única em `mensagem.txt`; sem isto o
        # nicho importado aparecia sem template nenhum
        msg = _ler("mensagem.txt").strip()
        if msg:
            templates = [{"id": 1, "nome": _ler("nome_modelo.txt").strip() or "Modelo 1",
                          "texto": msg, "ativo": True}]

    return {
        "ok": True,
        "nicho": nicho,
        "palavras_chave": _ler(IG_ARQUIVOS["palavras_chave"]),
        "palavras_contexto": _ler(IG_ARQUIVOS["palavras_contexto"]),
        "lista_negra": _ler(IG_ARQUIVOS["lista_negra"]),
        "fontes": _ler(IG_ARQUIVOS["fontes"]),
        "fontes_usadas": _ler(IG_ARQUIVOS["fontes_usadas"]),
        "templates": templates,
    }


def ig_salvar_nicho_dados(nicho: str, dados: Dict) -> Dict:
    """Salva keywords, fontes ou templates de um nicho — sempre na pasta do bot."""
    pdir = ig_perfis_dir() / nicho
    if not pdir.exists():
        return {"ok": False, "erro": "Nicho não encontrado"}

    for chave, arquivo in IG_ARQUIVOS.items():
        if chave in dados and chave != "fontes_usadas":
            (pdir / arquivo).write_text(dados[chave], encoding="utf-8")

    if "templates" in dados:
        saida = [_ig_tpl_para_bot(t, i) for i, t in enumerate(dados["templates"])]
        (pdir / "templates_msg.json").write_text(
            json.dumps(saida, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"ok": True}


def ig_bot_status() -> Dict:
    """O servidor do bot de DM (porta 5022) está respondendo?"""
    import requests
    base = config.DM_BOT_URL.rstrip("/")
    try:
        requests.get(base, timeout=3)
        return {"online": True, "url": base}
    except requests.exceptions.HTTPError:
        return {"online": True, "url": base}
    except Exception as e:
        return {"online": False, "url": base,
                "mensagem": f"Bot de DM offline em {base}: {e}"}


def ig_iniciar_autenticacao(username: str) -> Dict:
    """Inicia processo de autenticação manual do Instagram para salvar sessão."""
    username = username.strip().lstrip("@")
    if not username:
        return {"ok": False, "erro": "Username inválido"}

    auth_script = RAIZ.parent / "Hube 02" / "auth_launcher.py"
    if not auth_script.exists():
        return {"ok": False, "erro": "Script de autenticação não encontrado"}

    try:
        subprocess.Popen([sys.executable, str(auth_script), username],
                         cwd=str(auth_script.parent), stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"ok": True, "mensagem": f"Navegador aberto para autenticar @{username}"}
    except Exception as e:
        return {"ok": False, "erro": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# 📧 E-MAIL (GMAIL & LISTAS & CAMPANHAS) BACKEND
# ══════════════════════════════════════════════════════════════════════════════

def email_pcz_db():
    pcz = config.MAIL_BOT_DB
    if not pcz.exists():
        pcz = Path.home() / ".pcz" / "pcz.db"
    return pcz


def email_stats() -> Dict:
    """Estatísticas completas de e-mail."""
    with db.conn() as con:
        contas_gmail = con.execute("SELECT COUNT(*) FROM accounts WHERE channel='gmail' AND status='active'").fetchone()[0]
        tpls = con.execute("SELECT COUNT(*) FROM msg_templates WHERE canal='email'").fetchone()[0]
        
    pcz = email_pcz_db()
    prospects_total = 0
    prospects_pendentes = 0
    prospects_enviados = 0
    prospects_replied = 0

    if pcz.exists():
        try:
            import sqlite3
            c = sqlite3.connect(str(pcz))
            prospects_total = c.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]
            prospects_pendentes = c.execute("SELECT COUNT(*) FROM prospects WHERE status='pending'").fetchone()[0]
            prospects_enviados = c.execute("SELECT COUNT(*) FROM prospects WHERE status='sent'").fetchone()[0]
            prospects_replied = c.execute("SELECT COUNT(*) FROM prospects WHERE status='replied'").fetchone()[0]
            c.close()
        except Exception:
            pass

    return {
        "contas_ativas": contas_gmail,
        "templates": tpls,
        "total_prospects": prospects_total,
        "pendentes": prospects_pendentes,
        "enviados": prospects_enviados,
        "respostas": prospects_replied,
        "taxa_resposta": round(prospects_replied / prospects_enviados * 100, 1) if prospects_enviados else 0,
    }


def email_listar_listas() -> List[Dict]:
    """Lista listas de e-mails do pcz.db ou do leadforge."""
    pcz = email_pcz_db()
    if not pcz.exists():
        return []
    try:
        import sqlite3
        c = sqlite3.connect(str(pcz))
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT l.id, l.name, COUNT(p.id) as total,
                   SUM(CASE WHEN p.status='pending' THEN 1 ELSE 0 END) as pendentes,
                   SUM(CASE WHEN p.status='sent' THEN 1 ELSE 0 END) as enviados
            FROM lists l
            LEFT JOIN prospects p ON p.list_id = l.id
            GROUP BY l.id, l.name
            ORDER BY l.id DESC
        """).fetchall()
        listas = [dict(r) for r in rows]
        c.close()
        return listas
    except Exception:
        return []


def email_importar_lista_texto(nome: str, texto_linhas: str) -> Dict:
    """Importa lista de e-mails colados em formato texto."""
    pcz = email_pcz_db()
    if not pcz.exists():
        return {"ok": False, "erro": "Banco de e-mail pcz.db não encontrado"}

    linhas = [l.strip() for l in texto_linhas.splitlines() if l.strip()]
    if not linhas:
        return {"ok": False, "erro": "Nenhum e-mail fornecido"}

    try:
        import sqlite3
        c = sqlite3.connect(str(pcz))
        cur = c.execute("INSERT INTO lists (name) VALUES (?)", (nome,))
        list_id = cur.lastrowid

        inseridos = 0
        for l in linhas:
            partes = [p.strip() for p in l.split(",")]
            email = partes[0] if len(partes) > 0 else ""
            empresa = partes[1] if len(partes) > 1 else ""
            dominio = partes[2] if len(partes) > 2 else (email.split("@")[-1] if "@" in email else "")

            if "@" in email:
                c.execute(
                    "INSERT INTO prospects (list_id, email, company, domain, status) VALUES (?,?,?,?, 'pending')",
                    (list_id, email, empresa, dominio)
                )
                inseridos += 1

        c.commit()
        c.close()
        return {"ok": True, "list_id": list_id, "inseridos": inseridos}
    except Exception as e:
        return {"ok": False, "erro": str(e)}


def email_iniciar_disparo(campaign_id: int = None, min_delay: int = 30, max_delay: int = 90) -> Dict:
    """Inicia disparo de e-mails via subprocesso email_launcher."""
    launcher = RAIZ.parent / "Hube 02" / "email_launcher.py"
    if not launcher.exists():
        return {"ok": False, "erro": "email_launcher.py não encontrado"}

    try:
        args = [sys.executable, str(launcher), "--min-delay", str(min_delay), "--max-delay", str(max_delay)]
        if campaign_id:
            args.extend(["--campaign", str(campaign_id)])
        subprocess.Popen(args, cwd=str(launcher.parent), stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"ok": True, "mensagem": "Disparo de e-mail iniciado"}
    except Exception as e:
        return {"ok": False, "erro": str(e)}


def email_inbox_replies(limit: int = 50) -> List[Dict]:
    """Retorna respostas de e-mail recebidas."""
    pcz = email_pcz_db()
    if not pcz.exists():
        return []
    try:
        import sqlite3
        c = sqlite3.connect(str(pcz))
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT pr.id, pr.email, pr.company, pr.domain, pr.status, sl.sent_at, sl.thread_id, a.email as conta_remetente
            FROM prospects pr
            LEFT JOIN send_log sl ON sl.prospect_id = pr.id
            LEFT JOIN accounts a ON a.id = sl.account_id
            WHERE pr.status = 'replied'
            ORDER BY sl.sent_at DESC LIMIT ?
        """, (limit,)).fetchall()
        respostas = [dict(r) for r in rows]
        c.close()
        return respostas
    except Exception:
        return []
