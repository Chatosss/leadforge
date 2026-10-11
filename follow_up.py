"""follow_up.py — Engine de follow-up automatico do LeadForge."""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import config
import db

log = logging.getLogger("leadforge.follow_up")

# Tentativas antes de desistir de um item da fila.
MAX_TENTATIVAS = 3
BACKOFF_MINUTOS = {1: 10, 2: 60, 3: 360}

# Item reivindicado (`sending`) parado mais que isto veio de um processo que
# morreu no meio do envio. Volta para `pending`.
CLAIM_TIMEOUT_MIN = 15

STATUS_FILA = ("pending", "sending", "sent", "failed", "skipped", "cancelled")
CANAIS = ("whatsapp", "email", "instagram")

_pronto = False


# ══════════════════════════════════════════════════════════════════════════════
# Schema
# ══════════════════════════════════════════════════════════════════════════════

def init(forcar: bool = False):
    """Cria/migra as tabelas de follow-up. Só faz trabalho na primeira chamada."""
    global _pronto
    if _pronto and not forcar:
        return

    with db.conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS follow_up_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canal TEXT NOT NULL,
                delay_days INTEGER NOT NULL,
                template_id INTEGER,
                ativo INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_fu_rules_canal ON follow_up_rules(canal);

            CREATE TABLE IF NOT EXISTS follow_up_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                canal TEXT NOT NULL,
                rule_id INTEGER REFERENCES follow_up_rules(id),
                scheduled_for TEXT NOT NULL,
                status TEXT DEFAULT 'pending'
                    CHECK(status IN ('pending','sending','sent','failed','skipped','cancelled')),
                sent_at TEXT,
                tentativas INTEGER DEFAULT 0,
                ultimo_erro TEXT,
                updated_at TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
        """)
        _migrar_queue(con)
        con.executescript("""
            CREATE INDEX IF NOT EXISTS idx_fu_queue_status ON follow_up_queue(status);
            CREATE INDEX IF NOT EXISTS idx_fu_queue_scheduled ON follow_up_queue(scheduled_for);
            CREATE INDEX IF NOT EXISTS idx_fu_queue_lead ON follow_up_queue(lead_id);
        """)
    _pronto = True


def _migrar_queue(con):
    """
    Amplia a fila antiga (status só `pending`/`sent`/`cancelled`, sem contador de
    tentativas) para o schema atual.
    """
    linha = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='follow_up_queue'"
    ).fetchone()
    if not linha or "'skipped'" in (linha["sql"] or ""):
        return

    log.info("[follow-up] migrando follow_up_queue para o schema com falhas e tentativas")
    con.executescript("""
        CREATE TABLE follow_up_queue_novo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            canal TEXT NOT NULL,
            rule_id INTEGER REFERENCES follow_up_rules(id),
            scheduled_for TEXT NOT NULL,
            status TEXT DEFAULT 'pending'
                CHECK(status IN ('pending','sending','sent','failed','skipped','cancelled')),
            sent_at TEXT,
            tentativas INTEGER DEFAULT 0,
            ultimo_erro TEXT,
            updated_at TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        INSERT INTO follow_up_queue_novo
            (id, lead_id, canal, rule_id, scheduled_for, status, sent_at, created_at)
        SELECT id, lead_id, canal, rule_id, scheduled_for, status, sent_at, created_at
        FROM follow_up_queue;
        DROP TABLE follow_up_queue;
        ALTER TABLE follow_up_queue_novo RENAME TO follow_up_queue;
    """)


# ══════════════════════════════════════════════════════════════════════════════
# Agendamento
# ══════════════════════════════════════════════════════════════════════════════

def agendar_follow_ups(lead_id: int, canal: str,
                       primeiro_contato: Optional[datetime] = None) -> int:
    """
    Agenda follow-ups para um lead num canal, baseado nas regras ativas. Chamar depois
    do primeiro contato. Devolve quantos foram agendados.
    """
    init()
    base = primeiro_contato or datetime.now()

    with db.conn() as con:
        con.execute(
            "UPDATE follow_up_queue SET status='cancelled', "
            "updated_at=datetime('now','localtime') "
            "WHERE lead_id=? AND canal=? AND status IN ('pending','sending')",
            (lead_id, canal)
        )
        regras = con.execute(
            "SELECT * FROM follow_up_rules WHERE canal=? AND ativo=1 ORDER BY delay_days",
            (canal,)
        ).fetchall()
        for regra in regras:
            quando = base + timedelta(days=regra["delay_days"])
            con.execute(
                "INSERT INTO follow_up_queue (lead_id, canal, rule_id, scheduled_for) "
                "VALUES (?,?,?,?)",
                (lead_id, canal, regra["id"],
                 quando.isoformat(sep=" ", timespec="seconds"))
            )

    if not regras:
        log.warning("[follow-up] lead %s: nenhuma regra ativa no canal %s, "
                    "nada agendado", lead_id, canal)
    return len(regras)


def cancelar_follow_ups(lead_id: int, canal: Optional[str] = None) -> int:
    """
    Cancela follow-ups pendentes quando o lead responde. Se canal for None, cancela
    todos os canais. Devolve quantos cancelou.
    """
    init()
    with db.conn() as con:
        if canal:
            cur = con.execute(
                "UPDATE follow_up_queue SET status='cancelled', "
                "updated_at=datetime('now','localtime') "
                "WHERE lead_id=? AND canal=? AND status IN ('pending','sending')",
                (lead_id, canal)
            )
        else:
            cur = con.execute(
                "UPDATE follow_up_queue SET status='cancelled', "
                "updated_at=datetime('now','localtime') "
                "WHERE lead_id=? AND status IN ('pending','sending')",
                (lead_id,)
            )
        return cur.rowcount


# ══════════════════════════════════════════════════════════════════════════════
# Fila
# ══════════════════════════════════════════════════════════════════════════════

def _agora() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


_SELECT_VENCIDO = (
    "SELECT fq.*, l.nome, l.telefone, l.email, l.instagram, l.nicho "
    "FROM follow_up_queue fq "
    "JOIN leads l ON l.id = fq.lead_id "
    "WHERE fq.status='pending' AND fq.scheduled_for <= ? "
    "ORDER BY fq.scheduled_for LIMIT 1"
)


def proximo_pendente() -> Optional[Dict]:
    """Espia o proximo follow-up vencido, sem reivindicar. None se não há."""
    init()
    with db.conn() as con:
        _soltar_travados(con)
        row = con.execute(_SELECT_VENCIDO, (_agora(),)).fetchone()
    return dict(row) if row else None


def _soltar_travados(con):
    """Devolve para a fila o que ficou preso em `sending` por processo morto."""
    limite = (datetime.now() - timedelta(minutes=CLAIM_TIMEOUT_MIN)) \
        .isoformat(sep=" ", timespec="seconds")
    cur = con.execute(
        "UPDATE follow_up_queue SET status='pending' "
        "WHERE status='sending' AND COALESCE(updated_at, created_at) < ?",
        (limite,)
    )
    if cur.rowcount:
        log.warning("[follow-up] %s item(ns) presos em 'sending' devolvidos à fila",
                    cur.rowcount)


def _reivindicar() -> Optional[Dict]:
    """Pega o proximo vencido e marca `sending` na mesma transação."""
    init()
    with db.conn() as con:
        _soltar_travados(con)
        row = con.execute(_SELECT_VENCIDO, (_agora(),)).fetchone()
        if not row:
            return None
        cur = con.execute(
            "UPDATE follow_up_queue SET status='sending', "
            "updated_at=datetime('now','localtime') "
            "WHERE id=? AND status='pending'",
            (row["id"],)
        )
        if cur.rowcount != 1:
            return None      # outro processo pegou primeiro
    return dict(row)


def _finalizar(item_id: int, status: str, erro: str = ""):
    with db.conn() as con:
        con.execute(
            "UPDATE follow_up_queue SET status=?, ultimo_erro=?, "
            "sent_at=CASE WHEN ?='sent' THEN datetime('now','localtime') ELSE sent_at END, "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (status, (erro or "")[:500] or None, status, item_id)
        )


# Conta remetente aberta no painel (Chat ao vivo › Instagram Direct): não é
# falha, é espera.
ADIAR_OCUPADA_MIN = 15


def _reagendar(item_id: int, tentativas: int, minutos: int, erro: str):
    """Volta o item para `pending` daqui a `minutos`, com `tentativas` gravadas."""
    quando = (datetime.now() + timedelta(minutes=minutos)).isoformat(sep=" ", timespec="seconds")
    with db.conn() as con:
        con.execute(
            "UPDATE follow_up_queue SET status='pending', tentativas=?, "
            "scheduled_for=?, ultimo_erro=?, updated_at=datetime('now','localtime') "
            "WHERE id=?",
            (tentativas, quando, (erro or "")[:500], item_id)
        )


def _adiar_sem_contar(item_id: int, tentativas: int, erro: str, ate: str = None) -> str:
    """
    Espera sem gastar tentativa. `ate` ('AAAA-MM-DD HH:MM:SS'): até quando vale esperar
    (o limite do dia libera à meia-noite); sem ele, 15 min.
    """
    minutos = ADIAR_OCUPADA_MIN
    if ate:
        try:
            falta = (datetime.strptime(str(ate)[:19], "%Y-%m-%d %H:%M:%S") - datetime.now()).total_seconds()
            minutos = max(ADIAR_OCUPADA_MIN, int(falta // 60) + 1)
        except ValueError:
            pass
    _reagendar(item_id, tentativas, minutos, erro)
    log.info("[follow-up] item %s espera %smin (não conta tentativa): %s",
             item_id, minutos, erro)
    return "adiado"


def _adiar(item_id: int, tentativas: int, erro: str) -> str:
    """
    Reagenda o item para o futuro e conta a tentativa. Estourado o teto, marca `failed`
    e sai da fila.
    """
    proxima = tentativas + 1
    if proxima > MAX_TENTATIVAS:
        _finalizar(item_id, "failed", erro)
        log.error("[follow-up] item %s desistido após %s tentativas: %s",
                  item_id, tentativas, erro)
        return "falhou"

    minutos = BACKOFF_MINUTOS.get(proxima, 360)
    _reagendar(item_id, proxima, minutos, erro)
    log.warning("[follow-up] item %s adiado %smin (tentativa %s/%s): %s",
                item_id, minutos, proxima, MAX_TENTATIVAS, erro)
    return "adiado"


# ══════════════════════════════════════════════════════════════════════════════
# Envio
# ══════════════════════════════════════════════════════════════════════════════

def processar_proximo() -> Dict:
    """Processa o proximo follow-up vencido e devolve o que aconteceu."""
    item = _reivindicar()
    if not item:
        return {"acao": "vazio", "mensagem": "nenhum follow-up vencido"}

    item_id = item["id"]
    lead_id = item["lead_id"]
    canal = (item["canal"] or "").strip().lower()
    tentativas = item.get("tentativas") or 0

    try:
        respondeu = _ja_respondeu(lead_id)
        if respondeu is None:
            acao = _adiar(item_id, tentativas,
                          "não foi possível verificar se o lead já respondeu")
            return {"acao": acao, "item_id": item_id, "lead_id": lead_id,
                    "canal": canal, "erro": "verificação de resposta indisponível"}
        if respondeu:
            cancelados = cancelar_follow_ups(lead_id)
            log.info("[follow-up] lead %s já respondeu: %s follow-up(s) cancelado(s)",
                     lead_id, cancelados)
            return {"acao": "cancelado", "item_id": item_id, "lead_id": lead_id,
                    "canal": canal, "mensagem": "lead já respondeu",
                    "cancelados": cancelados}

        lead = db.get_lead(lead_id)
        if not lead:
            _finalizar(item_id, "skipped", "lead não encontrado")
            return {"acao": "pulado", "item_id": item_id, "lead_id": lead_id,
                    "canal": canal, "erro": "lead não encontrado"}

        r = _despachar(canal, item, lead)

    except Exception as e:
        # Erro NÃO tratado: loga com stack e devolve o item para a fila com
        # backoff. Silêncio aqui foi o defeito original.
        log.exception("[follow-up] erro inesperado no item %s (lead %s, canal %s)",
                      item_id, lead_id, canal)
        acao = _adiar(item_id, tentativas, f"{type(e).__name__}: {e}")
        return {"acao": acao, "item_id": item_id, "lead_id": lead_id,
                "canal": canal, "erro": f"{type(e).__name__}: {e}"}

    if r.get("ok"):
        _finalizar(item_id, "sent")
        log.info("[follow-up] enviado: lead %s (%s) %s",
                 lead_id, canal, r.get("detalhe", ""))
        return {"acao": "enviado", "item_id": item_id, "lead_id": lead_id,
                "canal": canal, "detalhe": r.get("detalhe", ""),
                "resposta": r.get("resposta")}

    erro = r.get("erro") or "falha desconhecida"
    if r.get("ocupada"):
        acao = _adiar_sem_contar(item_id, tentativas, erro, r.get("adiar_ate"))
        return {"acao": acao, "item_id": item_id, "lead_id": lead_id,
                "canal": canal, "erro": erro}
    if r.get("permanente"):
        _finalizar(item_id, "skipped", erro)
        log.warning("[follow-up] pulado definitivamente: lead %s (%s): %s",
                    lead_id, canal, erro)
        return {"acao": "pulado", "item_id": item_id, "lead_id": lead_id,
                "canal": canal, "erro": erro}

    acao = _adiar(item_id, tentativas, erro)
    return {"acao": acao, "item_id": item_id, "lead_id": lead_id,
            "canal": canal, "erro": erro}


def enviar_proximo() -> Optional[str]:
    """
    Compatibilidade: devolve a descrição do envio, ou None se nada saiu. Quem precisa
    saber o motivo usa `processar_proximo()`.
    """
    r = processar_proximo()
    if r.get("acao") == "enviado":
        return f"lead {r['lead_id']} ({r['canal']})"
    return None


def _despachar(canal: str, item: Dict, lead: Dict) -> Dict:
    """
    Roteia para o sender do canal. Devolve {ok, erro, permanente, detalhe}. `permanente`
    diz se repetir adianta.
    """
    etapa = _etapa(item)
    tpl, erro = _carregar_template(item.get("rule_id"), canal)
    if erro:
        return {"ok": False, "erro": erro, "permanente": True}

    if canal == "whatsapp":
        return _enviar_whatsapp(item, lead, tpl, etapa)
    if canal == "email":
        return _enviar_email(item, lead, tpl, etapa)
    if canal == "instagram":
        return _enviar_instagram(item, lead, tpl, etapa)
    return {"ok": False, "erro": f"canal desconhecido: {canal}", "permanente": True}


def _etapa(item: Dict) -> str:
    """Nome da etapa gravada no toque: `follow_up_d3`, `follow_up_d7`..."""
    dias = None
    rule_id = item.get("rule_id")
    if rule_id:
        with db.conn() as con:
            row = con.execute("SELECT delay_days FROM follow_up_rules WHERE id=?",
                              (rule_id,)).fetchone()
        if row:
            dias = row["delay_days"]
    return f"follow_up_d{dias}" if dias is not None else "follow_up"


def _enviar_whatsapp(item: Dict, lead: Dict, tpl: Dict, etapa: str) -> Dict:
    from senders import whatsapp_sender

    if not ((lead.get("telefone") or "").strip() or (lead.get("whatsapp") or "").strip()):
        return {"ok": False, "erro": "lead sem telefone", "permanente": True}

    repetido = whatsapp_sender.repeticao(lead["id"], etapa)
    if repetido:
        return {"ok": False, "erro": repetido, "permanente": True}

    texto = _renderizar(lead, tpl["corpo"], canal="whatsapp")
    if not texto.strip():
        return {"ok": False, "erro": f"template {tpl['id']} rendeu texto vazio",
                "permanente": True}
    # personalização por lead (liga/desliga no cartão do canal); na falha, o texto acima
    import personalizar
    texto = personalizar.aplicar(lead, "whatsapp", texto, etapa=etapa)

    r = whatsapp_sender.enviar(lead["id"], texto, etapa=etapa)
    return _resultado(r, f"WhatsApp para {r.get('numero') or lead.get('telefone')}")


def _enviar_email(item: Dict, lead: Dict, tpl: Dict, etapa: str) -> Dict:
    from senders import email_sender

    if not (lead.get("email") or "").strip():
        return {"ok": False, "erro": "lead sem e-mail", "permanente": True}

    assunto = _renderizar(lead, tpl.get("assunto") or "", canal="email")
    corpo = _renderizar(lead, tpl["corpo"], canal="email")
    if not assunto.strip():
        # E-mail frio sem assunto é filtrado antes de chegar na caixa de entrada.
        return {"ok": False,
                "erro": f"template {tpl['id']} ({tpl.get('nome')}) está sem assunto",
                "permanente": True}
    if not corpo.strip():
        return {"ok": False, "erro": f"template {tpl['id']} rendeu corpo vazio",
                "permanente": True}
    # personalização por lead (liga/desliga no cartão do canal); na falha, o corpo acima
    import personalizar
    corpo = personalizar.aplicar(lead, "email", corpo, etapa=etapa)

    r = email_sender.enviar(lead["id"], assunto, corpo, etapa=etapa)
    return _resultado(r, f"e-mail para {r.get('para') or lead.get('email')}")


def _enviar_instagram(item: Dict, lead: Dict, tpl: Dict, etapa: str) -> Dict:
    from senders import instagram_sender

    if not (lead.get("instagram") or "").strip():
        return {"ok": False, "erro": "lead sem perfil de Instagram", "permanente": True}

    texto = _renderizar(lead, tpl["corpo"], canal="instagram")
    if not texto.strip():
        return {"ok": False, "erro": f"template {tpl['id']} rendeu texto vazio",
                "permanente": True}
    # sem conta livre agora: espera ANTES de pedir o texto à IA (cada adiamento
    # gastava uma chamada de personalização)
    ocupado = instagram_sender.sem_conta_livre()
    if ocupado:
        return {"ok": False, "erro": ocupado, "permanente": False, "ocupada": True}
    # personalização por lead (liga/desliga no cartão do canal); na falha, o texto acima
    import personalizar
    texto = personalizar.aplicar(lead, "instagram", texto, etapa=etapa)

    nicho = (lead.get("nicho") or "").strip() or "clinicas"
    r = instagram_sender.enviar(lead["id"], texto, etapa=etapa, nicho=nicho)
    return _resultado(r, f"DM para @{(lead.get('instagram') or '').lstrip('@')}")


def _resultado(r: Dict, detalhe: str) -> Dict:
    """Traduz a resposta do sender para {ok, erro, permanente}."""
    if r.get("ok"):
        return {"ok": True, "detalhe": detalhe, "resposta": r}

    erro = r.get("erro") or "falha desconhecida"
    if r.get("ocupada"):
        return {"ok": False, "erro": erro, "permanente": False, "ocupada": True,
                "adiar_ate": r.get("adiar_ate")}
    if r.get("bloqueado"):
        try:
            import modo
            ao_vivo = bool(modo.ao_vivo())
        except Exception:
            ao_vivo = False
        return {"ok": False, "erro": erro, "permanente": ao_vivo}

    # Bot fora do ar, rede, limite diário de conta: tudo isso passa. Transitório.
    return {"ok": False, "erro": erro, "permanente": False}


# ══════════════════════════════════════════════════════════════════════════════
# Templates
# ══════════════════════════════════════════════════════════════════════════════

def _carregar_template(rule_id, canal: str) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Devolve (template, erro). O texto vem de `msg_templates`, cujas colunas reais são
    `assunto` e `corpo` — a versão anterior lia `texto`/`body`, que não existem, e
    `sqlite3.Row` levanta IndexError para coluna ausente.
    """
    if not rule_id:
        return None, "item da fila sem regra associada"

    with db.conn() as con:
        regra = con.execute("SELECT * FROM follow_up_rules WHERE id=?",
                            (rule_id,)).fetchone()
        if not regra:
            return None, f"regra {rule_id} não existe mais"
        if not regra["template_id"]:
            return None, f"regra {rule_id} não tem template associado"
        tpl = con.execute("SELECT * FROM msg_templates WHERE id=?",
                          (regra["template_id"],)).fetchone()

    if not tpl:
        return None, (f"regra {rule_id} aponta para o template "
                      f"{regra['template_id']}, que não existe em msg_templates")

    tpl = dict(tpl)
    if (tpl.get("canal") or "").lower() != canal:
        return None, (f"template {tpl['id']} é do canal {tpl.get('canal')}, "
                      f"mas a regra {rule_id} é de {canal}")
    if not (tpl.get("corpo") or "").strip():
        return None, f"template {tpl['id']} ({tpl.get('nome')}) está sem corpo"
    return tpl, None


def _renderizar(lead: Dict, texto: str, canal: str) -> str:
    """Troca as variáveis do template pelos dados do lead."""
    texto = texto or ""
    try:
        import esteira_wa
        texto = esteira_wa.renderizar(lead, texto, limpar=(canal != "email"))
    except Exception as e:
        log.warning("[follow-up] renderização completa indisponível (%s), "
                    "usando substituição mínima", e)
        nome = lead.get("nome") or ""
        texto = texto.replace("{nome}", nome)
        texto = texto.replace("{nome_curto}", nome.split()[0] if nome else "")
        texto = texto.replace("{cidade}", (lead.get("cidade") or "").split(",")[0])

    empresa = lead.get("nome") or ""
    return texto.replace("{{empresa}}", empresa).replace("{{nome}}", empresa)


# ══════════════════════════════════════════════════════════════════════════════
# Resposta do lead
# ══════════════════════════════════════════════════════════════════════════════

def _ja_respondeu(lead_id: int) -> Optional[bool]:
    """O lead já respondeu? True/False, ou None quando não deu para verificar."""
    # Kanban primeiro: é a coluna que o reply_detector alimenta nos três canais,
    # e é uma consulta só.
    try:
        with db.conn() as con:
            row = con.execute("SELECT kanban FROM leads WHERE id=?",
                              (lead_id,)).fetchone()
        if row and (row["kanban"] or "").strip().lower() in (
                "respondeu", "conversa", "fechado", "ignorado"):
            return True
    except Exception:
        log.exception("[follow-up] falha ao ler kanban do lead %s", lead_id)
        return None

    try:
        import bridge
        return bool(bridge.ja_respondeu(lead_id))
    except Exception:
        log.exception("[follow-up] falha ao verificar resposta do lead %s", lead_id)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Regras e relatórios
# ══════════════════════════════════════════════════════════════════════════════

_SELECT_REGRAS = (
    "SELECT r.*, t.nome AS template_nome, t.canal AS template_canal, "
    "t.assunto AS template_assunto "
    "FROM follow_up_rules r LEFT JOIN msg_templates t ON t.id = r.template_id "
)


def listar_regras(canal: Optional[str] = None) -> List[Dict]:
    """Lista regras de follow-up, com o nome do template e o que está quebrado."""
    init()
    with db.conn() as con:
        if canal:
            rows = con.execute(
                _SELECT_REGRAS + "WHERE r.canal=? ORDER BY r.delay_days", (canal,)
            ).fetchall()
        else:
            rows = con.execute(
                _SELECT_REGRAS + "ORDER BY r.canal, r.delay_days"
            ).fetchall()

    regras = []
    for r in rows:
        d = dict(r)
        d["problema"] = _problema_da_regra(d)
        regras.append(d)
    return regras


def _problema_da_regra(d: Dict) -> Optional[str]:
    if not d.get("template_id"):
        return "sem template associado"
    if not d.get("template_nome"):
        return f"template {d['template_id']} não existe"
    if (d.get("template_canal") or "").lower() != (d.get("canal") or "").lower():
        return f"template é do canal {d.get('template_canal')}"
    if (d.get("canal") or "") == "email" and not (d.get("template_assunto") or "").strip():
        return "template de e-mail sem assunto"
    return None


def validar() -> Dict:
    """Confere se as regras conseguem virar mensagem."""
    regras = listar_regras()
    quebradas = [{"regra": r["id"], "canal": r["canal"],
                  "delay_days": r["delay_days"], "problema": r["problema"]}
                 for r in regras if r["problema"]]
    return {"ok": not quebradas, "regras": len(regras), "quebradas": quebradas}


def criar_regra(canal: str, delay_days: int, template_id: Optional[int] = None) -> int:
    """Cria uma regra de follow-up, recusando o que não vira mensagem."""
    init()
    canal = (canal or "").strip().lower()
    if canal not in CANAIS:
        raise ValueError(f"canal inválido: {canal}")
    try:
        delay_days = int(delay_days)
    except (TypeError, ValueError):
        raise ValueError(f"delay_days inválido: {delay_days}")
    if delay_days < 0:
        raise ValueError("delay_days não pode ser negativo")

    if template_id:
        with db.conn() as con:
            tpl = con.execute("SELECT canal FROM msg_templates WHERE id=?",
                              (template_id,)).fetchone()
        if not tpl:
            raise ValueError(f"template {template_id} não existe")
        if (tpl["canal"] or "").lower() != canal:
            raise ValueError(f"template {template_id} é do canal {tpl['canal']}, "
                             f"não {canal}")

    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO follow_up_rules (canal, delay_days, template_id) VALUES (?,?,?)",
            (canal, delay_days, template_id)
        )
        return cur.lastrowid


def contar_pendentes() -> Dict[str, int]:
    """Conta follow-ups pendentes por canal."""
    init()
    with db.conn() as con:
        rows = con.execute(
            "SELECT canal, COUNT(*) as n FROM follow_up_queue "
            "WHERE status='pending' GROUP BY canal"
        ).fetchall()
    return {r["canal"]: r["n"] for r in rows}


def resumo_fila() -> Dict:
    """Fila por status e as últimas falhas, para o dashboard não ficar cego."""
    init()
    with db.conn() as con:
        por_status = {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) as n FROM follow_up_queue GROUP BY status")}
        falhas = [dict(r) for r in con.execute(
            "SELECT fq.id, fq.lead_id, fq.canal, fq.status, fq.tentativas, "
            "fq.ultimo_erro, fq.updated_at, l.nome "
            "FROM follow_up_queue fq LEFT JOIN leads l ON l.id = fq.lead_id "
            "WHERE fq.ultimo_erro IS NOT NULL "
            "ORDER BY fq.updated_at DESC LIMIT 20")]
        vencidos = con.execute(
            "SELECT COUNT(*) FROM follow_up_queue WHERE status='pending' "
            "AND scheduled_for <= ?", (_agora(),)).fetchone()[0]
    return {"por_status": por_status, "vencidos": vencidos,
            "pendentes_por_canal": contar_pendentes(),
            "falhas_recentes": falhas, "regras": validar()}


def reprocessar_falhas(canal: Optional[str] = None) -> int:
    """Devolve para a fila o que foi marcado `failed`, zerando as tentativas."""
    init()
    with db.conn() as con:
        if canal:
            cur = con.execute(
                "UPDATE follow_up_queue SET status='pending', tentativas=0, "
                "scheduled_for=?, updated_at=datetime('now','localtime') "
                "WHERE status='failed' AND canal=?", (_agora(), canal))
        else:
            cur = con.execute(
                "UPDATE follow_up_queue SET status='pending', tentativas=0, "
                "scheduled_for=?, updated_at=datetime('now','localtime') "
                "WHERE status='failed'", (_agora(),))
        return cur.rowcount


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="[%(levelname)s] %(name)s: %(message)s")
    if "--enviar" in sys.argv:
        print(json.dumps(processar_proximo(), indent=2, ensure_ascii=False, default=str))
    else:
        print(json.dumps(resumo_fila(), indent=2, ensure_ascii=False, default=str))
        print("\nuso: python -X utf8 follow_up.py [--enviar]")
