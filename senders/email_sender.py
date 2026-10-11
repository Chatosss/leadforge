"""
email_sender.py — Envio REAL de e-mail, com trava de identidade e rodízio de contas.
"""

import os
import re
import smtplib
import sys
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional

import config
import db
import identidade
import modo


# ──.env (mesmo carregador do ia_hook, sem dependência externa) ──────────────
def _carregar_env():
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for linha in env.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        k, v = linha.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_carregar_env()


def _lista(valor: str) -> List[str]:
    return [x.strip().lower() for x in re.split(r"[,;\s]+", valor or "") if x.strip()]


CONTAS_FIXAS = _lista(os.environ.get("EMAIL_CONTAS", ""))
EXCLUIDAS = set(_lista(os.environ.get("EMAIL_EXCLUIR", "")))
NOME_REMETENTE = os.environ.get("EMAIL_NOME", config.EMPRESA_NOME).strip()
LIMITE_DIA_CONTA = int(os.environ.get("EMAIL_LIMITE_DIA_CONTA", "30") or 30)
MODO = os.environ.get("EMAIL_MODO", "auto").strip().lower()

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USUARIO = os.environ.get("SMTP_USUARIO", "").strip()
SMTP_SENHA = os.environ.get("SMTP_SENHA", "").strip()

_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")


# ── Contabilidade de envios por conta ────────────────────────────────────────

def init():
    with db.conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS email_envios (
                conta  TEXT NOT NULL,
                dia    TEXT NOT NULL,
                total  INTEGER DEFAULT 0,
                PRIMARY KEY (conta, dia)
            );
        """)


def _usados_hoje() -> Dict[str, int]:
    init()
    hoje = date.today().isoformat()
    with db.conn() as con:
        rows = con.execute("SELECT conta, total FROM email_envios WHERE dia=?",
                           (hoje,)).fetchall()
    return {r["conta"]: r["total"] for r in rows}


def _contabilizar(conta: str):
    init()
    with db.conn() as con:
        con.execute("""
            INSERT INTO email_envios (conta, dia, total) VALUES (?,?,1)
            ON CONFLICT(conta, dia) DO UPDATE SET total = total + 1
        """, (conta, date.today().isoformat()))


# ── Gmail (reaproveita a autenticação da cópia do mail bot) ──────────────────

def _gmail():
    """Importa o gmail_manager da cópia. Devolve None se as libs não existirem."""
    caminho = config.BASE_DIR / "senders" / "mail_bot" / "core"
    if str(caminho) not in sys.path:
        sys.path.insert(0, str(caminho))
    try:
        import gmail_manager
        return gmail_manager
    except Exception:
        return None


def contas_autenticadas() -> List[str]:
    """
    Contas Gmail utilizáveis, já autenticadas, fora as excluídas. Com EMAIL_CONTAS
    preenchido, respeita a lista e não descobre nada.
    """
    gm = _gmail()
    if not gm:
        return []
    if CONTAS_FIXAS:
        candidatas = CONTAS_FIXAS
    else:
        pasta = getattr(gm, "TOKENS_DIR", None)
        if not pasta or not Path(pasta).exists():
            return []
        candidatas = sorted(p.stem.lower() for p in Path(pasta).glob("*.json"))
    return [c for c in candidatas if c not in EXCLUIDAS and gm.is_authenticated(c)]


def _proxima_conta() -> Optional[str]:
    """A conta que menos enviou hoje e ainda cabe no limite diário."""
    usados = _usados_hoje()
    livres = [(usados.get(c, 0), c) for c in contas_autenticadas()
              if usados.get(c, 0) < LIMITE_DIA_CONTA]
    if not livres:
        return None
    livres.sort()
    return livres[0][1]


def status() -> Dict:
    """O que está pronto, quanta capacidade resta hoje e o que falta configurar."""
    contas = contas_autenticadas()
    usados = _usados_hoje()
    smtp_ok = bool(SMTP_HOST and SMTP_USUARIO and SMTP_SENHA)
    capacidade = sum(max(0, LIMITE_DIA_CONTA - usados.get(c, 0)) for c in contas)

    pendencias = []
    if not contas and not smtp_ok:
        if _gmail() is None:
            pendencias.append("bibliotecas do Gmail ausentes (google-api-python-client)")
        else:
            pendencias.append("nenhuma conta Gmail autenticada fora das excluídas")
        pendencias.append("SMTP não configurado (SMTP_HOST/SMTP_USUARIO/SMTP_SENHA)")
    elif contas and capacidade == 0:
        pendencias.append(f"todas as {len(contas)} contas bateram o limite de "
                          f"{LIMITE_DIA_CONTA} envios hoje")

    return {
        "pronto": bool((contas and capacidade > 0) or smtp_ok),
        "contas": contas,
        "excluidas": sorted(EXCLUIDAS),
        "usados_hoje": {c: usados.get(c, 0) for c in contas},
        "limite_por_conta": LIMITE_DIA_CONTA,
        "capacidade_hoje": capacidade,
        "smtp": smtp_ok,
        "modo": MODO,
        "pendencias": pendencias,
    }


def _via_gmail(para: str, assunto: str, corpo: str) -> Dict:
    gm = _gmail()
    if not gm:
        return {"ok": False, "erro": "bibliotecas do Gmail não instaladas"}
    conta = _proxima_conta()
    if not conta:
        contas = contas_autenticadas()
        if not contas:
            return {"ok": False, "erro": "nenhuma conta Gmail autenticada disponível"}
        return {"ok": False, "erro": f"as {len(contas)} contas bateram o limite de "
                                     f"{LIMITE_DIA_CONTA} envios hoje"}
    res, thread = gm.send_email(conta, para, assunto, corpo, NOME_REMETENTE)
    if res == "ok":
        _contabilizar(conta)
        return {"ok": True, "via": "gmail", "conta": conta, "thread": thread}
    return {"ok": False, "erro": res, "via": "gmail", "conta": conta}


def _via_smtp(para: str, assunto: str, corpo: str) -> Dict:
    if not (SMTP_HOST and SMTP_USUARIO and SMTP_SENHA):
        return {"ok": False, "erro": "SMTP não configurado no .env"}
    msg = EmailMessage()
    msg["From"] = f"{NOME_REMETENTE} <{SMTP_USUARIO}>"
    msg["To"] = para
    msg["Subject"] = assunto
    msg.set_content(corpo)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(SMTP_USUARIO, SMTP_SENHA)
            s.send_message(msg)
        return {"ok": True, "via": "smtp", "conta": SMTP_USUARIO}
    except Exception as e:
        return {"ok": False, "erro": str(e), "via": "smtp"}


def _entregar(para: str, assunto: str, corpo: str) -> Dict:
    if MODO == "gmail":
        return _via_gmail(para, assunto, corpo)
    if MODO == "smtp":
        return _via_smtp(para, assunto, corpo)
    r = _via_gmail(para, assunto, corpo)
    if r.get("ok"):
        return r
    r2 = _via_smtp(para, assunto, corpo)
    if r2.get("ok"):
        return r2
    return {"ok": False, "erro": f"gmail: {r.get('erro')} | smtp: {r2.get('erro')}"}


# ── Envio ────────────────────────────────────────────────────────────────────

def enviar_endereco(para: str, assunto: str, corpo: str) -> Dict:
    """Envio cru para um endereço, SEM trava de identidade e sem registrar toque."""
    if not _RE_EMAIL.match((para or "").strip()):
        return {"ok": False, "erro": f"endereço inválido: {para}"}
    return _entregar(para.strip(), assunto, corpo)


def enviar(lead_id: int, assunto: str, corpo: str, etapa: str = "dia4",
           revalidar: bool = False, experimento: str = "", variante: str = "") -> Dict:
    """Envia para o e-mail confirmado do lead. Sem confirmação, não envia."""
    lead = db.get_lead(lead_id)
    if not lead:
        return {"ok": False, "erro": "lead não encontrado"}

    gate = identidade.pode_enviar(lead_id, "email", revalidar=revalidar)
    if not gate["ok"]:
        _registrar(lead_id, etapa, "skipped", gate["motivo"])
        return {"ok": False, "bloqueado": True, "erro": gate["motivo"],
                "confianca": gate.get("confianca"), "veredito": gate.get("veredito")}

    para = gate["valor"]
    if not _RE_EMAIL.match(para or ""):
        _registrar(lead_id, etapa, "skipped", f"endereço inválido: {para}")
        return {"ok": False, "erro": f"endereço inválido: {para}"}

    # trava de modo: em teste, só os destinos de teste passam daqui
    livre = modo.permite("email", para)
    if not livre["ok"]:
        _registrar(lead_id, etapa, "skipped", livre["motivo"])
        return {"ok": False, "bloqueado": True, "erro": livre["motivo"]}

    assunto = assunto.replace("{{empresa}}", lead.get("nome", ""))
    r = _entregar(para, assunto, corpo)
    if r.get("ok"):
        detalhe = (f"{para} via {r['via']} por {r.get('conta')} "
                   f"(identidade {gate['confianca']}/100)")
        _registrar(lead_id, etapa, "sent", detalhe)
        # o thread_id é o que permite detectar a resposta depois, via mail bot
        try:
            import experimentos
            experimentos.registrar_saida(lead_id, "email", etapa,
                                         "Assunto: " + assunto + "\n\n" + corpo,
                                         experimento, variante,
                                         externo_id=str(r.get("thread") or ""))
        except Exception:
            pass
        if lead.get("status") == "qualified":
            db.set_status(lead_id, "contacted")
        return {"ok": True, "para": para, **r}
    _registrar(lead_id, etapa, "failed", r.get("erro", ""))
    return {"ok": False, "para": para, **r}


def enviar_lote(itens: List[Dict], etapa: str = "dia4") -> Dict:
    """
    itens: [{lead_id, assunto, corpo}]. Envia um a um, alternando as contas do rodízio e
    respeitando config.ENVIO_DELAY_MIN/MAX entre um envio e o seguinte.
    """
    import random
    import time

    enviados, bloqueados, falhas = [], [], []
    for i, it in enumerate(itens):
        r = enviar(it["lead_id"], it.get("assunto", ""), it.get("corpo", ""), etapa=etapa,
                   experimento=it.get("experimento", ""), variante=it.get("variante", ""))
        if r.get("ok"):
            enviados.append({"lead_id": it["lead_id"], "conta": r.get("conta")})
        elif r.get("bloqueado"):
            bloqueados.append({"lead_id": it["lead_id"], "motivo": r["erro"]})
        else:
            falhas.append({"lead_id": it["lead_id"], "erro": r.get("erro")})
        if i < len(itens) - 1 and r.get("ok"):
            time.sleep(random.uniform(config.ENVIO_DELAY_MIN, config.ENVIO_DELAY_MAX))
    return {"ok": bool(enviados), "enviados": [e["lead_id"] for e in enviados],
            "por_conta": enviados, "bloqueados": bloqueados, "falhas": falhas}


def _registrar(lead_id: int, etapa: str, status: str, detalhe: str):
    try:
        alvo = None
        for t in db.touches_for(lead_id):
            if t["canal"] == "email" and t["etapa"] == etapa and t["status"] == "scheduled":
                alvo = t
                break
        if alvo:
            db.mark_touch(alvo["id"], status, detalhe)
        else:
            tid = db.schedule_touch(lead_id, "email", etapa, date.today().isoformat())
            db.mark_touch(tid, status, detalhe)
    except Exception:
        pass


if __name__ == "__main__":
    s = status()
    print("E-mail:", "PRONTO" if s["pronto"] else "NÃO configurado")
    print(f"Contas no rodízio ({len(s['contas'])}):")
    for c in s["contas"]:
        print(f"  {c:<38} {s['usados_hoje'].get(c, 0):>3}/{s['limite_por_conta']} hoje")
    print("Excluídas:", ", ".join(s["excluidas"]) or "(nenhuma)")
    print(f"Capacidade hoje: {s['capacidade_hoje']} envios")
    for p in s["pendencias"]:
        print("  falta:", p)
    if len(sys.argv) >= 4:
        print(enviar(int(sys.argv[1]), sys.argv[2], " ".join(sys.argv[3:])))
