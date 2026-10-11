# -*- coding: utf-8 -*-
"""limites.py — Teto por conta, POR DIA, zerando à meia-noite."""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import config
import db

# O teto é por DIA, não por janela de horas.
JANELA_HORAS = 24

# Reserva de vaga de mensagem entregue ao bot e ainda não confirmada. Um lote de
# 40 a 90 s por envio leva ~1h; 2h cobre com folga sem prender a vaga no dia.
FILA_VALE_HORAS = 2

# Teto de sanidade do limite diário de UMA conta. Acima disso é dedo
# escorregado.
LIMITE_MAXIMO_CONTA = 200
SITUACOES_CONTA = ("active", "paused", "inactive", "banned")


def validar_limite(valor) -> int:
    """O limite diário de uma conta, ou ValueError com o motivo para a tela."""
    try:
        n = int(str(valor).strip())
    except Exception:
        raise ValueError("o limite precisa ser um número inteiro")
    if not 1 <= n <= LIMITE_MAXIMO_CONTA:
        raise ValueError(f"limite fora da faixa (1 a {LIMITE_MAXIMO_CONTA} por dia)")
    return n


def _pcz() -> Path:
    return Path(config.MAIL_BOT_DB)


def init():
    """Cria a tabela do registro (idempotente)."""
    try:
        import account_manager
        account_manager.init()
    except Exception:
        # Um defeito ao preparar o cadastro não pode impedir o registro de
        # envios de existir — sem ele, o teto para de ser contado e o envio
        # deixa de ter freio.
        pass
    with db.conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS envios_conta (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                canal      TEXT    NOT NULL,
                destino    TEXT    DEFAULT '',
                lead_id    INTEGER,
                sent_at    TEXT    NOT NULL
            );
            -- a consulta quente é "desta conta, desde tal hora"
            CREATE INDEX IF NOT EXISTS ix_envios_conta_janela
                ON envios_conta(account_id, sent_at);
        """)
        # `situacao`: 'enviado' (confirmado) ou 'fila' (entregue ao bot, ainda
        # saindo). Sem a fila, a rodada seguinte da esteira — 5 min depois, com
        # o lote anterior ainda no meio — enxergava vagas que já estavam gastas.
        colunas = {r[1] for r in con.execute("PRAGMA table_info(envios_conta)")}
        if "situacao" not in colunas:
            con.execute("ALTER TABLE envios_conta ADD COLUMN situacao TEXT DEFAULT 'enviado'")


# ── Contas ────────────────────────────────────────────────────────────────────

def conta(account_id: int) -> Optional[Dict]:
    with db.conn() as con:
        r = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return dict(r) if r else None


def conta_por_identificador(canal: str, identificador: str) -> Optional[Dict]:
    """
    Acha a conta pelo que o canal chama de identificador: o número no WhatsApp, o @ no
    Instagram, o endereço no e-mail. É por aqui que o remetente devolvido pelo bot vira
    uma conta com teto.
    """
    if not identificador:
        return None
    alvo = str(identificador).strip().lstrip("@").lower()
    with db.conn() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM accounts WHERE channel=?", (canal,))]
    for r in rows:
        ident = str(r["identifier"] or "").strip().lstrip("@").lower()
        if ident == alvo:
            return r
    # WhatsApp escreve o número de várias formas (com/sem 55, com/sem 9)
    if canal == "whatsapp":
        so_digitos = "".join(c for c in alvo if c.isdigit())
        for r in rows:
            ident = "".join(c for c in str(r["identifier"] or "") if c.isdigit())
            if ident and (ident == so_digitos
                          or ident.endswith(so_digitos[-8:])
                          or so_digitos.endswith(ident[-8:])):
                return r
    return None


# ── A janela ──────────────────────────────────────────────────────────────────

def _corte() -> str:
    """A meia-noite de hoje. Tudo enviado depois disto conta para o teto."""
    return datetime.now().replace(hour=0, minute=0, second=0,
                                  microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _proxima_meia_noite() -> datetime:
    """Quando as vagas voltam. Sempre a mesma resposta para todas as contas."""
    return (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1))


def _envios_na_janela(c: Dict) -> List[str]:
    """
    As horas das mensagens que esta conta mandou HOJE, da mais antiga para a mais nova.
    """
    corte = _corte()
    if c["channel"] == "gmail":
        caminho = _pcz()
        if not caminho.exists():
            return []
        try:
            con = sqlite3.connect(str(caminho), timeout=5)
            try:
                ident = str(c["identifier"] or "").strip().lower()
                linha = con.execute(
                    "SELECT id FROM accounts WHERE lower(email)=?", (ident,)).fetchone()
                if not linha:
                    return []
                return [r[0] for r in con.execute(
                    "SELECT sent_at FROM send_log WHERE account_id=? AND status='sent' "
                    "AND sent_at > ? ORDER BY sent_at", (linha[0], corte))]
            finally:
                con.close()
        except sqlite3.Error:
            return []

    # a reserva da fila vale por FILA_VALE_HORAS: se o bot caiu no meio do lote
    # e a confirmação nunca veio, a vaga volta em vez de travar o dia inteiro
    with db.conn() as con:
        return [r[0] for r in con.execute(
            "SELECT sent_at FROM envios_conta WHERE account_id=? AND sent_at > ? "
            "AND (COALESCE(situacao,'enviado')='enviado' "
            "     OR sent_at > datetime('now','localtime',?)) "
            "ORDER BY sent_at", (c["id"], corte, f"-{FILA_VALE_HORAS} hours"))]


def _limite_da_conta(c: Dict) -> int:
    """O teto em vigor para esta conta."""
    if c["channel"] == "gmail":
        caminho = _pcz()
        if caminho.exists():
            try:
                con = sqlite3.connect(str(caminho), timeout=5)
                try:
                    r = con.execute("SELECT daily_limit FROM accounts WHERE lower(email)=?",
                                    (str(c["identifier"] or "").strip().lower(),)).fetchone()
                    if r:
                        return int(r[0] or 0)
                finally:
                    con.close()
            except sqlite3.Error:
                pass
    return int(c["daily_limit"] or 0)


def usados_hoje(account_id: int) -> int:
    c = conta(account_id)
    return len(_envios_na_janela(c)) if c else 0


def pode_enviar(account_id: int) -> Dict:
    """Esta conta pode mandar mais uma AGORA?"""
    c = conta(account_id)
    if not c:
        return {"ok": False, "motivo": "conta não encontrada", "usados": 0,
                "limite": 0, "restam": 0, "libera_em": None}
    if c["status"] != "active":
        return {"ok": False, "motivo": f"conta {c['status']}", "usados": 0,
                "limite": c["daily_limit"], "restam": 0, "libera_em": None}

    limite = _limite_da_conta(c)
    envios = _envios_na_janela(c)
    usados = len(envios)
    restam = max(0, limite - usados)

    base = {"usados": usados, "limite": limite, "restam": restam,
            "conta": c["identifier"], "canal": c["channel"]}

    if limite <= 0:
        return {**base, "ok": False, "motivo": "teto diário zerado", "libera_em": None}
    if usados < limite:
        return {**base, "ok": True, "motivo": "", "libera_em": None}

    # CHEIA: todas as vagas voltam juntas, à meia-noite.
    return {**base, "ok": False,
            "motivo": f"teto de {limite} para hoje atingido",
            "libera_em": _proxima_meia_noite().strftime("%Y-%m-%d %H:%M:%S")}


# O nome antigo continua valendo. Renomear sem ponte quebraria quem chamasse
# `usados_24h` fora do que eu enxerguei — e as duas instalações compartilham
# esta base.
usados_24h = usados_hoje


def registrar(account_id: int, canal: str, destino: str = "",
              lead_id: int = None, quando: str = None):
    """Marca uma mensagem enviada. Chamar SÓ depois da confirmação de envio."""
    if canal == "gmail":
        return
    init()
    with db.conn() as con:
        con.execute(
            "INSERT INTO envios_conta(account_id, canal, destino, lead_id, sent_at) "
            "VALUES(?,?,?,?,COALESCE(?, datetime('now','localtime')))",
            (int(account_id), canal, str(destino or "")[:120], lead_id, quando))


def reservar(account_id: int, canal: str, destino: str = "", lead_id: int = None):
    """Mensagem entregue ao bot e ainda na fila: já ocupa a vaga da conta."""
    init()
    with db.conn() as con:
        con.execute(
            "INSERT INTO envios_conta(account_id, canal, destino, lead_id, sent_at, situacao) "
            "VALUES(?,?,?,?,datetime('now','localtime'),'fila')",
            (int(account_id), canal, str(destino or "")[:120], lead_id))


def confirmar_fila(canal: str, lead_id: int, identificador: str = "",
                   destino: str = "") -> bool:
    """
    O bot confirmou: a reserva deste lead vira envio. Sem reserva (lote antigo, de antes
    da fila), registra pelo remetente, como antes.
    """
    init()
    with db.conn() as con:
        cur = con.execute(
            "UPDATE envios_conta SET situacao='enviado', sent_at=datetime('now','localtime') "
            "WHERE id=(SELECT id FROM envios_conta WHERE canal=? AND lead_id=? "
            "          AND situacao='fila' ORDER BY id LIMIT 1)", (canal, lead_id))
        if cur.rowcount:
            return True
    return registrar_por_identificador(canal, identificador, destino, lead_id)


def devolver_fila(canal: str, lead_id: int):
    """O bot falhou nesta mensagem: a vaga reservada volta para a conta."""
    init()
    with db.conn() as con:
        con.execute(
            "DELETE FROM envios_conta WHERE id=(SELECT id FROM envios_conta WHERE canal=? "
            "AND lead_id=? AND situacao='fila' ORDER BY id LIMIT 1)", (canal, lead_id))


def vagas_hoje(canal: str) -> int:
    """Quantas mensagens ainda cabem hoje, somando as contas ATIVAS do canal."""
    return sum(e["restam"] for e in estado(canal) if e.get("status") == "active")


def registrar_por_identificador(canal: str, identificador: str, destino: str = "",
                                lead_id: int = None) -> bool:
    """Mesma coisa, quando só se sabe o número/@ que o bot usou."""
    c = conta_por_identificador(canal, identificador)
    if not c:
        return False
    registrar(c["id"], canal, destino, lead_id)
    return True


# ── Visão para a tela ─────────────────────────────────────────────────────────

def estado(canal: str = None) -> List[Dict]:
    """Uma linha por conta, com o consumo da janela — é o que o painel desenha."""
    init()
    with db.conn() as con:
        if canal:
            rows = con.execute("SELECT * FROM accounts WHERE channel=? ORDER BY identifier",
                               (canal,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM accounts ORDER BY channel, identifier").fetchall()

    saida = []
    for r in rows:
        c = dict(r)
        envios = _envios_na_janela(c)
        limite = _limite_da_conta(c)
        usados = len(envios)
        # Cheia: volta à meia-noite, e a data vai junto — "00:00" sozinho não
        # diz se é hoje ou daqui a uma semana para quem olha às 23h50.
        libera = (_proxima_meia_noite().strftime("%d/%m %H:%M")
                  if usados >= limite > 0 else None)
        ultimo = envios[-1] if envios else None
        saida.append({
            "id": c["id"], "canal": c["channel"], "conta": c["identifier"],
            "nome": c["display_name"], "status": c["status"],
            "limite": limite, "usados": usados, "restam": max(0, limite - usados),
            "cheia": usados >= limite > 0,
            "libera_em": libera,
            "ultimo_envio": ultimo,
            "pct": min(100, round(usados / limite * 100)) if limite else 0,
        })
    return saida


# ── E-mail: fazer o disparador de fora obedecer à janela ─────────────────────

def sincronizar_email() -> Dict:
    """Espelha o consumo da janela em `pcz.accounts.sent_today`."""
    caminho = _pcz()
    if not caminho.exists():
        return {"ok": False, "erro": "pcz.db não encontrado"}

    hoje = datetime.now().strftime("%Y-%m-%d")
    corte = _corte()
    mudou = []
    try:
        con = sqlite3.connect(str(caminho), timeout=10)
        con.row_factory = sqlite3.Row
        try:
            for r in con.execute("SELECT id, email, daily_limit, sent_today FROM accounts"):
                usados = con.execute(
                    "SELECT COUNT(*) FROM send_log WHERE account_id=? AND status='sent' "
                    "AND sent_at > ?", (r["id"], corte)).fetchone()[0]
                if usados != r["sent_today"]:
                    mudou.append({"conta": r["email"], "de": r["sent_today"], "para": usados})
                con.execute("UPDATE accounts SET sent_today=?, last_reset=? WHERE id=?",
                            (usados, hoje, r["id"]))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error as e:
        return {"ok": False, "erro": str(e)}
    return {"ok": True, "ajustadas": len(mudou), "mudancas": mudou[:10]}


# ── Retrofit: aproveitar o histórico que já existe ───────────────────────────

def importar_historico() -> Dict:
    """Preenche `envios_conta` com o que já está em `touches`, uma vez só."""
    init()
    relato = {}
    with db.conn() as con:
        ja = con.execute("SELECT COUNT(*) FROM envios_conta").fetchone()[0]
        if ja:
            return {"ok": True, "pulado": True, "motivo": f"já há {ja} registro(s)"}

        corte = _corte()
        for canal in ("whatsapp", "instagram"):
            contas_do_canal = [dict(r) for r in con.execute(
                "SELECT * FROM accounts WHERE channel=? AND status='active'", (canal,))]
            if len(contas_do_canal) != 1:
                relato[canal] = f"{len(contas_do_canal)} contas: não dá para atribuir"
                continue
            alvo = contas_do_canal[0]
            linhas = con.execute(
                "SELECT lead_id, COALESCE(sent_at, created_at) FROM touches "
                "WHERE canal=? AND status='sent' AND COALESCE(sent_at, created_at) > ?",
                (canal, corte)).fetchall()
            for lead_id, quando in linhas:
                con.execute(
                    "INSERT INTO envios_conta(account_id,canal,destino,lead_id,sent_at) "
                    "VALUES(?,?,?,?,?)", (alvo["id"], canal, "", lead_id, quando))
            relato[canal] = f"{len(linhas)} envio(s) atribuídos a {alvo['identifier']}"
    return {"ok": True, "relato": relato}


if __name__ == "__main__":
    init()
    print(f"Teto do DIA · zera à meia-noite · corte: {_corte()}  ·  agora: {datetime.now():%d/%m %H:%M}")
    print(f"Corte:  {_corte()}\n")
    for e in estado():
        marca = "CHEIA" if e["cheia"] else "ok   "
        libera = f"  libera 1 em {e['libera_em']}" if e["libera_em"] else ""
        print(f"  [{marca}] {e['canal']:<10} {str(e['conta'])[:32]:<34} "
              f"{e['usados']:>3}/{e['limite']:<4}{libera}")
