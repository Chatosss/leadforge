"""agendamentos.py — Agenda o INÍCIO dos envios de cada canal."""

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional

import config
import db

log = logging.getLogger("leadforge.agendamentos")

CANAIS = ("whatsapp", "email", "instagram", "aquecimento")
DIAS = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]   # 0 = segunda

_DASH = f"http://localhost:{getattr(config, 'SCHEDULER_PORT', 5020)}"

_pronto = False


def init(forcar: bool = False):
    global _pronto
    if _pronto and not forcar:
        return
    with db.conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS agendamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canal TEXT NOT NULL,
                hora TEXT NOT NULL,                  -- 'HH:MM'
                dias TEXT NOT NULL DEFAULT '0,1,2,3,4',
                ativo INTEGER NOT NULL DEFAULT 1,
                config_json TEXT DEFAULT '{}',
                ultimo_disparo TEXT,
                ultimo_resultado TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_agenda_canal ON agendamentos(canal);
        """)
    _pronto = True


# ══════════════════════════════════════════════════════════════════════════════
# CRUD
# ══════════════════════════════════════════════════════════════════════════════

def _valido(hora: str, dias: str, canal: str) -> Optional[str]:
    if canal not in CANAIS:
        return f"canal inválido: {canal}"
    try:
        h, m = (hora or "").split(":")
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError
    except Exception:
        return f"hora inválida: {hora!r} (use HH:MM)"
    nums = [d.strip() for d in (dias or "").split(",") if d.strip() != ""]
    if not nums:
        return "escolha ao menos um dia da semana"
    for d in nums:
        if not d.isdigit() or not (0 <= int(d) <= 6):
            return f"dia inválido: {d}"
    return None


def listar(canal: Optional[str] = None) -> List[Dict]:
    init()
    with db.conn() as con:
        if canal:
            rows = con.execute("SELECT * FROM agendamentos WHERE canal=? ORDER BY hora",
                               (canal,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM agendamentos ORDER BY canal, hora").fetchall()
    saida = []
    for r in rows:
        d = dict(r)
        try:
            d["config"] = json.loads(d.get("config_json") or "{}")
        except Exception:
            d["config"] = {}
        d["dias_lista"] = [DIAS[int(x)] for x in d["dias"].split(",") if x.strip().isdigit()]
        d["resumo"] = f"{', '.join(d['dias_lista'])} às {d['hora']}"
        if d["canal"] == "whatsapp":
            d["quadro_nome"] = _nome_do_quadro(d["config"].get("quadro_id"))
        if d["canal"] == "aquecimento":
            acao = "fechar" if d["config"].get("acao") == "desativar" else "abrir"
            mins = d["config"].get("fechar_apos_min")
            d["resumo"] += f" · {acao} todas" + (f" por {mins} min" if acao == "abrir" and mins else "")
        saida.append(d)
    return saida


def _nome_do_quadro(qid) -> str:
    """Nome do quadro para mostrar na lista — o id sozinho não diz nada ao operador."""
    if not qid:
        return "quadro aberto na tela"
    try:
        import dashboard_db as ddb
        for q in ddb.quadros():
            if q["id"] == int(qid):
                return q["nome"]
    except Exception:
        pass
    return f"quadro #{qid} (apagado)"


def criar(canal: str, hora: str, dias: str = "0,1,2,3,4",
          config_extra: Optional[Dict] = None) -> Dict:
    init()
    erro = _valido(hora, dias, canal)
    if erro:
        return {"ok": False, "erro": erro}
    if canal == "aquecimento":
        cfg = config_extra or {}
        if cfg.get("acao") not in ("ativar", "desativar"):
            return {"ok": False, "erro": "ação do aquecimento: ativar ou desativar"}
        mins = cfg.get("fechar_apos_min")
        if mins not in (None, ""):
            if isinstance(mins, bool) or not str(mins).isdigit() or not 1 <= int(mins) <= 720:
                return {"ok": False, "erro": "fechar depois de: 1 a 720 minutos"}
            cfg["fechar_apos_min"] = int(mins)
        config_extra = {k: v for k, v in cfg.items() if k in ("acao", "fechar_apos_min") and v not in (None, "")}
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO agendamentos (canal, hora, dias, config_json) VALUES (?,?,?,?)",
            (canal, hora, dias, json.dumps(config_extra or {}, ensure_ascii=False)))
    return {"ok": True, "id": cur.lastrowid}


def editar(aid: int, **campos) -> Dict:
    init()
    permitidos = {"hora", "dias", "ativo", "config_json"}
    if "config" in campos:
        campos["config_json"] = json.dumps(campos.pop("config"), ensure_ascii=False)
    campos = {k: v for k, v in campos.items() if k in permitidos and v is not None}
    if not campos:
        return {"ok": False, "erro": "nada para alterar"}
    if "hora" in campos or "dias" in campos:
        atual = next((a for a in listar() if a["id"] == aid), None)
        if not atual:
            return {"ok": False, "erro": f"agendamento {aid} não existe"}
        erro = _valido(campos.get("hora", atual["hora"]),
                       campos.get("dias", atual["dias"]), atual["canal"])
        if erro:
            return {"ok": False, "erro": erro}
    sets = ", ".join(f"{k}=?" for k in campos)
    with db.conn() as con:
        cur = con.execute(f"UPDATE agendamentos SET {sets} WHERE id=?",
                          list(campos.values()) + [aid])
        if not cur.rowcount:
            return {"ok": False, "erro": f"agendamento {aid} não existe"}
    return {"ok": True}


def excluir(aid: int) -> Dict:
    init()
    with db.conn() as con:
        cur = con.execute("DELETE FROM agendamentos WHERE id=?", (aid,))
        if not cur.rowcount:
            return {"ok": False, "erro": f"agendamento {aid} não existe"}
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# Execução
# ══════════════════════════════════════════════════════════════════════════════

def _post(url: str, corpo: Dict, timeout: int = 30) -> Dict:
    dados = json.dumps(corpo).encode("utf-8")
    req = urllib.request.Request(url, data=dados, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "erro": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "erro": f"{url} inacessível: {e}"}


def disparar(ag: Dict) -> Dict:
    """Aperta o botão do canal. Devolve o que a rota respondeu."""
    canal = ag["canal"]
    cfg = ag.get("config") or {}

    if canal == "whatsapp":
        # O quadro vai explícito. Sem ele o disparo usaria o quadro ABERTO NA
        # TELA na hora em que o agendamento roda — de madrugada, sem ninguém
        # olhando, e mudando conforme o que o operador deixou aberto na véspera.
        corpo = {}
        if cfg.get("limite"):
            corpo["limite"] = cfg["limite"]
        if cfg.get("quadro_id"):
            corpo["quadro_id"] = cfg["quadro_id"]
        return _post(f"{_DASH}/api/kanban/entrar-contato/enviar", corpo)

    if canal == "email":
        if not cfg.get("campaign_id"):
            return {"ok": False, "erro": "agendamento de e-mail sem campanha escolhida"}
        return _post(f"{_DASH}/api/email/disparo/iniciar", {
            "campaign_id": cfg["campaign_id"],
            "min_delay": cfg.get("min_delay", 30),
            "max_delay": cfg.get("max_delay", 90)})

    if canal == "instagram":
        if not cfg.get("nicho"):
            return {"ok": False, "erro": "agendamento de Instagram sem nicho escolhido"}
        return _post(f"{_DASH}/api/ig/prospectar", {
            "nicho": cfg["nicho"], "conta": cfg.get("conta", ""),
            "mensagem": cfg.get("mensagem", "")})

    if canal == "aquecimento":
        corpo = {"ativo": cfg.get("acao") != "desativar"}
        if corpo["ativo"] and cfg.get("fechar_apos_min"):
            corpo["fechar_apos_min"] = cfg["fechar_apos_min"]
        # abrir 19 contas uma por vez leva alguns minutos, mas o painel só pede
        return _post(f"{_DASH}/api/aquecimento/instagram/todas", corpo, timeout=120)

    return {"ok": False, "erro": f"canal desconhecido: {canal}"}


def vencidos(agora: Optional[datetime] = None) -> List[Dict]:
    """
    Quais agendamentos deveriam ter disparado até agora e ainda não dispararam hoje.
    """
    agora = agora or datetime.now()
    hoje = agora.date().isoformat()
    dia_semana = str(agora.weekday())
    pendentes = []
    for ag in listar():
        if not ag["ativo"]:
            continue
        if dia_semana not in [d.strip() for d in ag["dias"].split(",")]:
            continue
        if ag["hora"] > agora.strftime("%H:%M"):
            continue
        if (ag.get("ultimo_disparo") or "")[:10] == hoje:
            continue
        pendentes.append(ag)
    return pendentes


AQUECIMENTO_TOLERANCIA_MIN = 60    # depois disso o aquecimento do dia não abre mais


def _atraso_min(ag: Dict, agora: datetime) -> float:
    h, m = (int(x) for x in ag["hora"].split(":"))
    return (agora - agora.replace(hour=h, minute=m, second=0, microsecond=0)).total_seconds() / 60


def rodar_pendentes(agora: Optional[datetime] = None) -> List[Dict]:
    """Dispara o que estiver vencido. Chamado pelo scheduler de minuto em minuto."""
    init()
    agora = agora or datetime.now()
    resultados = []
    # o aquecimento vai por último: no mesmo horário, a prospecção pega as
    # contas antes (aberta no aquecimento, a conta fica fora da prospecção)
    for ag in sorted(vencidos(agora), key=lambda a: a["canal"] == "aquecimento"):
        if ag["canal"] == "aquecimento":
            atraso = _atraso_min(ag, agora)
            if atraso > AQUECIMENTO_TOLERANCIA_MIN:
                # o painel estava fora no horário: abrir 19 janelas às 22h
                # porque o daemon subiu tarde não é o que foi agendado
                r = {"ok": False, "erro": f"pulado: o horário passou há {int(atraso)} min"}
            else:
                r = disparar(ag)
                if r.get("bot_fora") or "inacessível" in str(r.get("erro") or ""):
                    log.warning("[agenda] aquecimento %s: bot fora do ar; tenta de novo no "
                                "próximo minuto (até %s min do horário)", ag["resumo"],
                                AQUECIMENTO_TOLERANCIA_MIN)
                    continue        # não marca: o próximo minuto tenta de novo
        else:
            r = disparar(ag)
        ok = bool(r.get("ok"))
        resumo = (r.get("mensagem") or r.get("erro") or "sem resposta")[:300]
        # marca o disparo mesmo quando falha: sem isso, um agendamento com erro
        # permanente (campanha apagada, bot fora) seria retentado a cada minuto
        # até o fim do dia
        with db.conn() as con:
            con.execute(
                "UPDATE agendamentos SET ultimo_disparo=datetime('now','localtime'), "
                "ultimo_resultado=? WHERE id=?",
                (("ok: " if ok else "falhou: ") + resumo, ag["id"]))
        (log.info if ok else log.error)(
            "[agenda] %s %s -> %s", ag["canal"], ag["resumo"], resumo)
        resultados.append({"id": ag["id"], "canal": ag["canal"], "ok": ok,
                           "resposta": resumo})
    return resultados


def proximo(canal: Optional[str] = None) -> Optional[Dict]:
    """O próximo agendamento a rodar, para a tela mostrar sem fazer conta."""
    agora = datetime.now()
    melhor = None
    for ag in listar(canal):
        if not ag["ativo"]:
            continue
        dias = sorted(int(d) for d in ag["dias"].split(",") if d.strip().isdigit())
        for adiante in range(8):
            alvo = (agora.weekday() + adiante) % 7
            if alvo not in dias:
                continue
            if adiante == 0 and ag["hora"] <= agora.strftime("%H:%M"):
                continue
            chave = (adiante, ag["hora"])
            if melhor is None or chave < melhor[0]:
                melhor = (chave, ag)
            break
    if not melhor:
        return None
    (adiante, hora), ag = melhor
    quando = "hoje" if adiante == 0 else ("amanhã" if adiante == 1
                                          else DIAS[(agora.weekday() + adiante) % 7])
    return {**ag, "proximo_em": f"{quando} às {hora}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    print(json.dumps({"agendamentos": listar(), "vencidos": vencidos(),
                      "proximo": proximo()}, indent=2, ensure_ascii=False, default=str))
