"""email_marketing.py — Backend da área de E-mail do LeadForge."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import config

RAIZ = Path(__file__).resolve().parent
LAUNCHER = RAIZ.parent / "Hube 02" / "email_launcher.py"
PID_FILE = RAIZ / "data" / "email_disparo.pid"


# ══════════════════════════════════════════════════════════════════════════════
# Banco
# ══════════════════════════════════════════════════════════════════════════════

def caminho_db() -> Path:
    """O pcz.db. `config.MAIL_BOT_DB` é a fonte; o fallback cobre instalação nova."""
    p = config.MAIL_BOT_DB
    if p.exists():
        return p
    return Path.home() / ".pcz" / "pcz.db"


def _conn():
    p = caminho_db()
    if not p.exists():
        raise FileNotFoundError(f"banco do mail bot não encontrado em {p}")
    con = sqlite3.connect(str(p), timeout=10)
    con.row_factory = sqlite3.Row
    return con


def disponivel() -> Dict:
    """O painel precisa saber se a máquina de e-mail existe antes de desenhar."""
    p = caminho_db()
    return {"ok": p.exists(), "banco": str(p),
            "launcher": str(LAUNCHER), "launcher_ok": LAUNCHER.exists()}


# ══════════════════════════════════════════════════════════════════════════════
# Listas
# ══════════════════════════════════════════════════════════════════════════════

def listas() -> List[Dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT l.id, l.name, l.created_at,
                   COUNT(p.id) AS total,
                   SUM(CASE WHEN p.status='pending' THEN 1 ELSE 0 END) AS pendentes,
                   SUM(CASE WHEN p.status='sent'    THEN 1 ELSE 0 END) AS enviados,
                   SUM(CASE WHEN p.status='replied' THEN 1 ELSE 0 END) AS respostas,
                   SUM(CASE WHEN p.status='error'   THEN 1 ELSE 0 END) AS erros
            FROM lists l LEFT JOIN prospects p ON p.list_id = l.id
            GROUP BY l.id, l.name, l.created_at
            ORDER BY l.id DESC
        """).fetchall()
    return [dict(r) for r in rows]


def lista_importar(nome: str, texto: str) -> Dict:
    """
    Importa e-mails colados. Uma linha por prospect: email@empresa.com, Empresa,
    dominio.com Só o e-mail é obrigatório; o domínio sai do próprio endereço quando
    falta.
    """
    nome = (nome or "").strip()
    if not nome:
        return {"ok": False, "erro": "dê um nome à lista"}
    linhas = [l.strip() for l in (texto or "").splitlines() if l.strip()]
    if not linhas:
        return {"ok": False, "erro": "nenhum e-mail informado"}

    with _conn() as con:
        cur = con.execute("INSERT INTO lists (name) VALUES (?)", (nome,))
        list_id = cur.lastrowid
        inseridos, ignorados = 0, 0
        for l in linhas:
            partes = [p.strip() for p in l.split(",")]
            email = partes[0] if partes else ""
            if "@" not in email:
                ignorados += 1
                continue
            empresa = partes[1] if len(partes) > 1 else ""
            dominio = partes[2] if len(partes) > 2 else email.split("@")[-1]
            con.execute(
                "INSERT INTO prospects (list_id, email, company, domain, status) "
                "VALUES (?,?,?,?, 'pending')", (list_id, email, empresa, dominio))
            inseridos += 1
        con.commit()
    return {"ok": True, "list_id": list_id, "inseridos": inseridos,
            "ignorados": ignorados}


def lista_excluir(list_id: int) -> Dict:
    """Apaga a lista e os prospects dela."""
    with _conn() as con:
        usada = con.execute("SELECT name FROM campaigns WHERE list_id=?",
                            (list_id,)).fetchall()
        if usada:
            nomes = ", ".join(r["name"] for r in usada)
            return {"ok": False, "erro": f"lista usada pela(s) campanha(s): {nomes}"}
        n = con.execute("DELETE FROM prospects WHERE list_id=?", (list_id,)).rowcount
        con.execute("DELETE FROM lists WHERE id=?", (list_id,))
        con.commit()
    return {"ok": True, "prospects_removidos": n}


# ══════════════════════════════════════════════════════════════════════════════
# Templates
# ══════════════════════════════════════════════════════════════════════════════

VARIAVEIS = ["{{empresa}}", "{{email}}", "{{dominio}}"]


def templates() -> List[Dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT t.id, t.name, t.subject, t.body, t.created_at, "
            "(SELECT COUNT(*) FROM campaigns c WHERE c.template_id = t.id) AS em_campanhas "
            "FROM templates t ORDER BY t.id"
        ).fetchall()
    return [dict(r) for r in rows]


def template(tid: int) -> Optional[Dict]:
    with _conn() as con:
        r = con.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def template_criar(nome: str, assunto: str, corpo: str) -> Dict:
    erro = _validar_template(nome, assunto, corpo)
    if erro:
        return {"ok": False, "erro": erro}
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO templates (name, subject, body) VALUES (?,?,?)",
            (nome.strip(), assunto.strip(), corpo.strip()))
        con.commit()
        return {"ok": True, "id": cur.lastrowid}


def template_editar(tid: int, nome: str, assunto: str, corpo: str) -> Dict:
    erro = _validar_template(nome, assunto, corpo)
    if erro:
        return {"ok": False, "erro": erro}
    with _conn() as con:
        cur = con.execute(
            "UPDATE templates SET name=?, subject=?, body=? WHERE id=?",
            (nome.strip(), assunto.strip(), corpo.strip(), tid))
        con.commit()
        if not cur.rowcount:
            return {"ok": False, "erro": f"template {tid} não existe"}
    return {"ok": True, "id": tid}


def template_excluir(tid: int) -> Dict:
    """Recusa template em uso: campanha sem texto não dispara e não avisa."""
    with _conn() as con:
        usos = con.execute("SELECT name FROM campaigns WHERE template_id=?",
                           (tid,)).fetchall()
        if usos:
            nomes = ", ".join(r["name"] for r in usos)
            return {"ok": False, "erro": f"template usado pela(s) campanha(s): {nomes}"}
        con.execute("DELETE FROM campaign_templates WHERE template_id=?", (tid,))
        cur = con.execute("DELETE FROM templates WHERE id=?", (tid,))
        con.commit()
        if not cur.rowcount:
            return {"ok": False, "erro": f"template {tid} não existe"}
    return {"ok": True}


def _validar_template(nome: str, assunto: str, corpo: str) -> Optional[str]:
    if not (nome or "").strip():
        return "dê um nome ao template"
    if not (assunto or "").strip():
        # e-mail frio sem assunto é filtrado antes de chegar na caixa de entrada
        return "o assunto é obrigatório"
    if not (corpo or "").strip():
        return "o corpo é obrigatório"
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Contas remetentes
# ══════════════════════════════════════════════════════════════════════════════

def _gmail():
    """O `gmail_manager` da cópia do mail bot. None se as libs do Google faltarem."""
    caminho = config.BASE_DIR / "senders" / "mail_bot" / "core"
    if str(caminho) not in sys.path:
        sys.path.insert(0, str(caminho))
    try:
        import gmail_manager
        return gmail_manager
    except Exception:
        return None


def contas() -> List[Dict]:
    """Contas Gmail do mail bot, com teto diário, uso de hoje e estado do OAuth."""
    gm = _gmail()
    with _conn() as con:
        rows = con.execute(
            "SELECT id, email, sender_name, forwarding_to, daily_limit, sent_today, "
            "last_reset, filters_set, active FROM accounts ORDER BY id").fetchall()
    saida = []
    for r in rows:
        d = dict(r)
        d["restam"] = max(0, (d["daily_limit"] or 0) - (d["sent_today"] or 0))
        d["autenticada"] = bool(gm.is_authenticated(d["email"])) if gm else None
        if d["autenticada"] is None:
            d["oauth_txt"] = "libs do Gmail ausentes"
        elif d["autenticada"]:
            d["oauth_txt"] = "autenticada"
        else:
            d["oauth_txt"] = "sem OAuth"
        saida.append(d)
    return saida


def conta_criar(email: str, nome: str = "", limite: int = 70) -> Dict:
    """Cadastra uma conta remetente. O OAuth é um passo separado, depois."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return {"ok": False, "erro": f"e-mail inválido: {email}"}
    try:
        limite = int(limite)
    except (TypeError, ValueError):
        return {"ok": False, "erro": "limite diário precisa ser um número"}
    if limite < 1:
        return {"ok": False, "erro": "limite diário precisa ser ao menos 1"}

    with _conn() as con:
        if con.execute("SELECT 1 FROM accounts WHERE email=?", (email,)).fetchone():
            return {"ok": False, "erro": "esta conta já está cadastrada"}
        cur = con.execute(
            "INSERT INTO accounts (email, sender_name, daily_limit, sent_today, active) "
            "VALUES (?,?,?,0,1)", (email, (nome or "").strip() or email, limite))
        con.commit()
    return {"ok": True, "id": cur.lastrowid,
            "mensagem": f"{email} cadastrada. Falta autenticar para ela poder enviar."}


def conta_editar(account_id: int, limite=None, ativo=None, nome=None) -> Dict:
    """Ajusta teto diário, nome do remetente e liga/desliga a conta."""
    campos, valores = [], []
    if limite is not None:
        try:
            limite = int(limite)
        except (TypeError, ValueError):
            return {"ok": False, "erro": "limite diário precisa ser um número"}
        if limite < 1:
            return {"ok": False, "erro": "limite diário precisa ser ao menos 1"}
        campos.append("daily_limit=?")
        valores.append(limite)
    if ativo is not None:
        campos.append("active=?")
        valores.append(1 if ativo else 0)
    if nome is not None:
        campos.append("sender_name=?")
        valores.append(str(nome).strip())
    if not campos:
        return {"ok": False, "erro": "nada para alterar"}

    valores.append(account_id)
    with _conn() as con:
        cur = con.execute("UPDATE accounts SET " + ", ".join(campos) + " WHERE id=?",
                          valores)
        con.commit()
        if not cur.rowcount:
            return {"ok": False, "erro": f"conta {account_id} não existe"}
    return {"ok": True}


def conta_excluir(account_id: int) -> Dict:
    """Remove a conta do cadastro."""
    with _conn() as con:
        usos = con.execute(
            "SELECT c.name FROM campaign_accounts ca "
            "JOIN campaigns c ON c.id = ca.campaign_id WHERE ca.account_id=?",
            (account_id,)).fetchall()
        if usos:
            nomes = ", ".join(r["name"] for r in usos)
            return {"ok": False, "erro": f"conta usada pela(s) campanha(s): {nomes}"}
        cur = con.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        con.commit()
        if not cur.rowcount:
            return {"ok": False, "erro": f"conta {account_id} não existe"}
    return {"ok": True}


def conta_validar(account_id: int) -> Dict:
    """Valida a conta CONTRA A API do Gmail, não contra o disco."""
    gm = _gmail()
    if not gm:
        return {"ok": False, "erro": "bibliotecas do Gmail não instaladas "
                                     "(google-api-python-client)"}
    with _conn() as con:
        r = con.execute("SELECT email FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not r:
        return {"ok": False, "erro": f"conta {account_id} não existe"}
    email = r["email"]

    if not gm.is_authenticated(email):
        return {"ok": False, "autenticada": False, "email": email,
                "erro": "sem OAuth: clique em Autenticar para conceder o acesso"}
    try:
        perfil = gm.get_profile(email) or {}
    except Exception as e:
        return {"ok": False, "autenticada": False, "email": email, "erro": str(e)}

    endereco = perfil.get("emailAddress") or email
    return {"ok": True, "autenticada": True, "email": email,
            "conta_real": endereco,
            "mensagens": perfil.get("messagesTotal"),
            "mensagem": f"{endereco} respondeu à API do Gmail: a conta envia."}


def conta_autenticar(account_id: int) -> Dict:
    """Abre o consentimento OAuth do Google numa janela de console própria."""
    script = RAIZ / "email_login.py"
    if not script.exists():
        return {"ok": False, "erro": f"{script} não encontrado"}
    with _conn() as con:
        r = con.execute("SELECT email FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not r:
        return {"ok": False, "erro": f"conta {account_id} não existe"}

    # Sem janela de console: o que importa é o navegador que o Google abre.
    # A saída vai para arquivo, e o painel a mostra.
    log = RAIZ / "data" / "email_login.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        saida = open(log, "w", encoding="utf-8")
        subprocess.Popen(
            [sys.executable, "-X", "utf8", str(script), r["email"]],
            cwd=str(RAIZ), stdout=saida, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return {"ok": False, "erro": str(e)}
    return {"ok": True, "email": r["email"],
            "mensagem": "Abri o consentimento do Google para " + r["email"]
                        + ". Aprove no navegador e clique em Validar."}

# ══════════════════════════════════════════════════════════════════════════════
# Campanhas — é aqui que "escolher a lista e a mensagem" acontece
# ══════════════════════════════════════════════════════════════════════════════

def campanhas() -> List[Dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT c.id, c.name, c.status, c.created_at,
                   c.list_id, l.name AS lista_nome,
                   c.template_id, t.name AS template_nome, t.subject AS template_assunto,
                   (SELECT COUNT(*) FROM prospects p
                     WHERE p.list_id = c.list_id AND p.status='pending') AS pendentes,
                   (SELECT COUNT(*) FROM prospects p
                     WHERE p.list_id = c.list_id AND p.status='sent') AS enviados,
                   (SELECT COUNT(*) FROM campaign_accounts ca
                     WHERE ca.campaign_id = c.id) AS contas,
                   (SELECT COUNT(*) FROM campaign_templates ct
                     WHERE ct.campaign_id = c.id) AS templates_rodizio
            FROM campaigns c
            LEFT JOIN lists l     ON l.id = c.list_id
            LEFT JOIN templates t ON t.id = c.template_id
            ORDER BY c.id DESC
        """).fetchall()
    saida = []
    for r in rows:
        d = dict(r)
        d["problema"] = _problema_campanha(d)
        saida.append(d)
    return saida


def _problema_campanha(d: Dict) -> Optional[str]:
    if not d.get("lista_nome"):
        return "a lista desta campanha não existe mais"
    if not d.get("template_nome"):
        return "o template desta campanha não existe mais"
    if not d.get("contas"):
        return "nenhuma conta remetente selecionada"
    if not d.get("pendentes"):
        return "não há prospects pendentes nesta lista"
    return None


def campanha_criar(nome: str, list_id: int, template_id: int,
                   contas_ids: Optional[List[int]] = None,
                   templates_rodizio: Optional[List[int]] = None) -> Dict:
    """Cria a campanha e já amarra contas e rodízio."""
    nome = (nome or "").strip()
    if not nome:
        return {"ok": False, "erro": "dê um nome à campanha"}
    with _conn() as con:
        if not con.execute("SELECT 1 FROM lists WHERE id=?", (list_id,)).fetchone():
            return {"ok": False, "erro": f"lista {list_id} não existe"}
        if not con.execute("SELECT 1 FROM templates WHERE id=?", (template_id,)).fetchone():
            return {"ok": False, "erro": f"template {template_id} não existe"}

        ids = [int(i) for i in (contas_ids or [])]
        if not ids:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM accounts WHERE active=1 ORDER BY id")]
        if not ids:
            return {"ok": False, "erro": "nenhuma conta Gmail ativa para enviar"}

        cur = con.execute(
            "INSERT INTO campaigns (name, template_id, list_id, status) "
            "VALUES (?,?,?, 'active')", (nome, template_id, list_id))
        cid = cur.lastrowid
        for a in ids:
            con.execute("INSERT OR IGNORE INTO campaign_accounts (campaign_id, account_id) "
                        "VALUES (?,?)", (cid, a))
        for t in (templates_rodizio or []):
            con.execute("INSERT OR IGNORE INTO campaign_templates (campaign_id, template_id) "
                        "VALUES (?,?)", (cid, int(t)))
        con.commit()
    return {"ok": True, "id": cid}


def campanha_excluir(cid: int) -> Dict:
    if disparo_status().get("rodando"):
        return {"ok": False, "erro": "pare o disparo antes de excluir a campanha"}
    with _conn() as con:
        con.execute("DELETE FROM campaign_accounts WHERE campaign_id=?", (cid,))
        con.execute("DELETE FROM campaign_templates WHERE campaign_id=?", (cid,))
        cur = con.execute("DELETE FROM campaigns WHERE id=?", (cid,))
        con.commit()
        if not cur.rowcount:
            return {"ok": False, "erro": f"campanha {cid} não existe"}
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# Disparo
# ══════════════════════════════════════════════════════════════════════════════

def _pid_vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def disparo_status() -> Dict:
    """O disparo está rodando? E quanto já andou?"""
    rodando, pid = False, None
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            rodando = _pid_vivo(pid)
        except Exception:
            rodando = False
    if not rodando:
        PID_FILE.unlink(missing_ok=True)
        pid = None

    try:
        with _conn() as con:
            por_status = {r["status"]: r["n"] for r in con.execute(
                "SELECT status, COUNT(*) n FROM prospects GROUP BY status")}
            enviados_hoje = con.execute(
                "SELECT COUNT(*) FROM send_log WHERE date(sent_at)=date('now','localtime')"
            ).fetchone()[0]
    except Exception as e:
        return {"rodando": rodando, "pid": pid, "erro": str(e)}

    return {"rodando": rodando, "pid": pid, "por_status": por_status,
            "enviando": por_status.get("sending", 0),
            "enviados_hoje": enviados_hoje}


def disparo_iniciar(campaign_id: int, min_delay: int = 30,
                    max_delay: int = 90) -> Dict:
    """Dispara a campanha em subprocesso."""
    if not LAUNCHER.exists():
        return {"ok": False, "erro": f"launcher não encontrado em {LAUNCHER}"}

    st = disparo_status()
    if st.get("rodando"):
        return {"ok": False, "erro": f"já existe um disparo rodando (pid {st['pid']})"}

    try:
        campaign_id = int(campaign_id)
        min_delay = max(1, int(min_delay))
        max_delay = max(min_delay, int(max_delay))
    except (TypeError, ValueError):
        return {"ok": False, "erro": "campanha e intervalos precisam ser números"}

    campanha = next((c for c in campanhas() if c["id"] == campaign_id), None)
    if not campanha:
        return {"ok": False, "erro": f"campanha {campaign_id} não existe"}
    if campanha["problema"]:
        return {"ok": False, "erro": campanha["problema"]}

    # Alinha o contador de cada conta com a janela de 24h ANTES de o disparador
    # subir.
    try:
        import limites
        sinc = limites.sincronizar_email()
        if not sinc.get("ok"):
            return {"ok": False, "erro": f"nao deu para conferir os tetos: {sinc.get('erro')}"}
    except Exception as e:
        return {"ok": False, "erro": f"nao deu para conferir os tetos: {e}"}

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = PID_FILE.parent / "email_disparo.log"
    try:
        saida = open(log, "a", encoding="utf-8")
        # CREATE_NO_WINDOW: o disparo roda por horas; um console preto aberto
        # esse tempo todo no desktop era o que aparecia ao clicar em disparar.
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(LAUNCHER),
             str(campaign_id), str(min_delay), str(max_delay)],
            cwd=str(LAUNCHER.parent), stdout=saida, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        PID_FILE.write_text(str(proc.pid))
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    return {"ok": True, "pid": proc.pid, "campanha": campanha["name"],
            "pendentes": campanha["pendentes"], "log": str(log),
            "mensagem": f"disparo da campanha \"{campanha['name']}\" iniciado "
                        f"({campanha['pendentes']} pendentes, "
                        f"intervalo {min_delay}-{max_delay}s)"}


def disparo_parar() -> Dict:
    """Encerra o disparo. O launcher devolve os `sending` para `pending` na saída."""
    st = disparo_status()
    if not st.get("rodando"):
        return {"ok": False, "erro": "nenhum disparo em andamento"}
    try:
        os.kill(st["pid"], signal.SIGTERM)
    except Exception:
        try:
            os.kill(st["pid"], 9)
        except Exception as e:
            return {"ok": False, "erro": str(e)}
    PID_FILE.unlink(missing_ok=True)
    return {"ok": True, "mensagem": "disparo encerrado"}


def disparo_log(linhas: int = 60) -> List[str]:
    log = PID_FILE.parent / "email_disparo.log"
    if not log.exists():
        return []
    try:
        return log.read_text(encoding="utf-8", errors="replace").splitlines()[-linhas:]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Painel
# ══════════════════════════════════════════════════════════════════════════════

def stats() -> Dict:
    """Números da aba de e-mail, todos do pcz.db."""
    base = disponivel()
    if not base["ok"]:
        return {**base, "erro": "banco do mail bot não encontrado"}
    with _conn() as con:
        por_status = {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) n FROM prospects GROUP BY status")}
        total = sum(por_status.values())
        enviados = por_status.get("sent", 0)
        respostas = por_status.get("replied", 0)
        n_listas = con.execute("SELECT COUNT(*) FROM lists").fetchone()[0]
        n_tpl = con.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
        n_camp = con.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
        enviados_hoje = con.execute(
            "SELECT COUNT(*) FROM send_log WHERE date(sent_at)=date('now','localtime')"
        ).fetchone()[0]
    cts = contas()
    return {
        **base,
        "total_prospects": total,
        "pendentes": por_status.get("pending", 0),
        "enviados": enviados,
        "respostas": respostas,
        "erros": por_status.get("error", 0),
        "taxa_resposta": round(respostas / enviados * 100, 1) if enviados else 0,
        "listas": n_listas, "templates": n_tpl, "campanhas": n_camp,
        "enviados_hoje": enviados_hoje,
        "contas_ativas": sum(1 for c in cts if c["active"]),
        "capacidade_hoje": sum(c["restam"] for c in cts if c["active"]),
        "disparo": disparo_status(),
    }


if __name__ == "__main__":
    print(json.dumps(stats(), indent=2, ensure_ascii=False, default=str))
