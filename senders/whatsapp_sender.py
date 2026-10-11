"""whatsapp_sender.py — Envio REAL de WhatsApp pelo bot próprio (porta 5021)."""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

import config
import db
import identidade
import limites
import modo

BASE = config.WHATSAPP_BOT_URL.rstrip("/")
_TIMEOUT = 20


def _post(caminho: str, corpo: Dict) -> Dict:
    dados = json.dumps(corpo).encode("utf-8")
    req = urllib.request.Request(f"{BASE}{caminho}", data=dados, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "erro": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "erro": f"bot de WhatsApp inacessível em {BASE}: {e}"}


def _get(caminho: str, params: Optional[Dict] = None) -> Dict:
    """GET no bot. Lê o corpo TAMBÉM quando a resposta é de erro."""
    url = f"{BASE}{caminho}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # o servidor RESPONDEU; a resposta é que não foi 2xx
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"erro": f"HTTP {e.code}"}
    except Exception as e:
        # `offline` marca a diferenca que o texto do erro nao consegue carregar: aqui o
        # servidor NAO respondeu.
        return {"erro": f"bot de WhatsApp inacessível em {BASE}: {e}",
                "offline": True}


# ── Estado ───────────────────────────────────────────────────────────────────

def status(conta: str = "") -> Dict:
    """
    {no_ar, pronto, numero, erro, contas}. no_ar = o servidor Node respondeu pronto =
    esta conta de WhatsApp está conectada (QR já escaneado)
    """
    s = _get("/api/status", {"conta": conta} if conta else None)
    if "pronto" not in s:
        # Sem `pronto` há DOIS casos, e tratá-los igual custou caro: o servidor
        # fora do ar, e o servidor respondendo uma recusa com motivo — que é
        # informação, não silêncio.
        erro = s.get("erro") or ""
        respondeu = not s.get("offline")

        # "mais de uma conectada" NÃO é falta de conexão: é excesso dela.
        varias = s.get("contas") or []
        if respondeu and not conta and varias:
            return {"no_ar": True, "pronto": True, "ambiguo": True,
                    "numero": None, "conta": None, "erro": "",
                    "contas": varias}

        return {"no_ar": respondeu, "pronto": False, "numero": None,
                "erro": erro or "sem resposta",
                "contas": varias}
    return {"no_ar": True, "pronto": bool(s.get("pronto")),
            "numero": s.get("numero"), "conta": s.get("conta"),
            "erro": s.get("erro") or "", "contas": s.get("contas") or []}


def contas() -> Dict:
    """Todas as contas do bot e o estado de cada uma."""
    r = _get("/api/contas")
    if "contas" not in r:
        return {"no_ar": False, "contas": [], "erro": r.get("erro", "sem resposta")}
    return {"no_ar": True, "contas": r.get("contas") or [], "erro": ""}


def cadastrar_conta(numero: str) -> Dict:
    """Cadastra um número no bot e inicia o pareamento (gera o QR dele)."""
    return _post("/api/contas", {"conta": numero})


def remover_conta(numero: str) -> Dict:
    """Desconecta e esquece a sessão de um número."""
    import urllib.request
    req = urllib.request.Request(f"{BASE}/api/contas/{numero}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "erro": str(e)}


def tem_whatsapp(numero: str, conta: str = "") -> Dict:
    """Confirma se o número tem conta. {tem: True|False|None}."""
    params = {"numero": numero}
    if conta:
        params["conta"] = conta
    return _get("/api/verificar", params)


# ── Envio ────────────────────────────────────────────────────────────────────

def enviar_numero(numero: str, mensagem: str, conta: str = "") -> Dict:
    """Envio cru, sem trava de identidade. Use só para teste do seu próprio número."""
    if not numero or not mensagem:
        return {"ok": False, "erro": "numero e mensagem são obrigatórios"}
    corpo = {"numero": numero, "mensagem": mensagem}
    if conta:
        corpo["conta"] = conta
    return _post("/api/enviar", corpo)


# Conferir a vaga e gastá-la é um passo só: sem isto, a fila do primeiro contato
# e a esteira, rodando juntas, liam a mesma vaga livre e as duas a usavam.
_TRAVA_VAGA = threading.RLock()


def _conta_da_conversa(lead_id: int) -> str:
    """Por qual número sai uma mensagem avulsa (follow-up, convite) sem conta dita."""
    try:
        with db.conn() as con:
            r = con.execute(
                "SELECT a.identifier FROM envios_conta e JOIN accounts a ON a.id=e.account_id "
                "WHERE e.canal='whatsapp' AND e.lead_id=? ORDER BY e.id DESC LIMIT 1",
                (lead_id,)).fetchone()
        if r and r[0]:
            return str(r[0])
    except Exception:
        pass
    try:
        livres = _remetentes_disponiveis()
        if livres:
            return livres[0]["conta"]
    except Exception:
        pass
    return _conta_padrao()


def _conta_padrao() -> str:
    """O número que o bot usa quando ninguém escolhe conta."""
    try:
        s = status()
        return str(s.get("conta") or s.get("numero") or "")
    except Exception:
        return ""


def enviar(lead_id: int, mensagem: str, etapa: str = "dia0",
           forcar: bool = False, revalidar: bool = False, conta: str = "") -> Dict:
    """Envia para o WhatsApp confirmado do lead."""
    lead = db.get_lead(lead_id)
    if not lead:
        return {"ok": False, "erro": "lead não encontrado"}

    gate = identidade.pode_enviar(lead_id, "whatsapp", revalidar=revalidar)
    if not gate["ok"] and not forcar:
        _registrar(lead_id, "whatsapp", etapa, "skipped", gate["motivo"])
        return {"ok": False, "bloqueado": True, "erro": gate["motivo"],
                "confianca": gate.get("confianca"), "veredito": gate.get("veredito")}

    numero = gate.get("valor") or (lead.get("telefone") or "")
    if not numero:
        _registrar(lead_id, "whatsapp", etapa, "skipped", "lead sem telefone")
        return {"ok": False, "erro": "lead sem telefone"}

    # trava de modo: em teste, só os destinos de teste passam daqui
    livre = modo.permite("whatsapp", numero)
    if not livre["ok"]:
        _registrar(lead_id, "whatsapp", etapa, "skipped", livre["motivo"])
        return {"ok": False, "bloqueado": True, "erro": livre["motivo"]}

    # Limite diário da conta remetente — o ÚNICO limite do canal.
    with _TRAVA_VAGA:
        remetente = conta or _conta_da_conversa(lead_id)
        c = limites.conta_por_identificador("whatsapp", remetente) if remetente else None
        if not c and not remetente:
            # bot fora do ar ou nenhuma conta conectada: passa sozinho. `ocupada`
            # = o follow-up espera sem gastar tentativa (no modo ao vivo,
            # `bloqueado` o descartaria) e nada é gravado: nada aconteceu.
            return {"ok": False, "ocupada": True,
                    "erro": "não deu para saber qual conta de WhatsApp enviaria "
                            "(o bot não respondeu ou nenhuma está conectada)"}
        if not c:
            # configuração: falha que conta tentativa (some depois de algumas),
            # não espera sem fim nem descarte na hora
            motivo = (f"o número {remetente} não está cadastrado em Contas: sem limite "
                      f"diário definido, nada sai por ele")
            _registrar(lead_id, "whatsapp", etapa, "skipped", motivo)
            return {"ok": False, "erro": motivo}
        veredito = limites.pode_enviar(c["id"])
        if not veredito["ok"]:
            motivo = veredito["motivo"]
            if veredito.get("libera_em"):
                # limite do dia: espera até a vaga voltar (meia-noite), uma vez
                # só — tentar a cada 15 min gravava ~40 toques por dia por lead
                return {"ok": False, "ocupada": True, "erro": f"{motivo}; libera em {veredito['libera_em']}",
                        "adiar_ate": veredito["libera_em"], "teto": veredito}
            # conta pausada ou limite zerado: falha que conta tentativa
            _registrar(lead_id, "whatsapp", etapa, "skipped", motivo)
            return {"ok": False, "erro": motivo, "teto": veredito}

        corpo = {"numero": numero, "mensagem": mensagem, "conta": remetente}
        r = _post("/api/enviar", corpo)
        if r.get("ok"):
            # o bot devolve QUEM enviou; é essa conta que consome a vaga
            if not limites.registrar_por_identificador(
                    "whatsapp", r.get("conta") or remetente, destino=numero, lead_id=lead_id):
                limites.registrar(c["id"], "whatsapp", destino=numero, lead_id=lead_id)
    if r.get("ok"):
        detalhe = f"enviado para {numero}"
        if r.get("conta"):
            detalhe += f" pela conta {r['conta']}"
        if forcar and not gate["ok"]:
            detalhe += f" (FORÇADO, trava dizia: {gate['motivo']})"
        else:
            detalhe += f" (identidade {gate.get('confianca')}/100, {gate.get('veredito')})"
        _registrar(lead_id, "whatsapp", etapa, "sent", detalhe)
        if lead.get("status") == "qualified":
            db.set_status(lead_id, "contacted")
        return {"ok": True, "numero": numero, "detalhe": detalhe}

    erro = r.get("erro", "falha desconhecida")
    _registrar(lead_id, "whatsapp", etapa, "failed", erro)
    return {"ok": False, "erro": erro, "numero": numero}


def enviar_material(lead_id: int, legenda: str = "", etapa: str = "material",
                    gerar_se_faltar: bool = True) -> Dict:
    """Manda o PDF da análise para o WhatsApp confirmado do lead."""
    lead = db.get_lead(lead_id)
    if not lead:
        return {"ok": False, "erro": "lead não encontrado"}

    gate = identidade.pode_enviar(lead_id, "whatsapp")
    if not gate["ok"]:
        _registrar(lead_id, "whatsapp", etapa, "skipped", gate["motivo"])
        return {"ok": False, "bloqueado": True, "erro": gate["motivo"]}

    livre = modo.permite("whatsapp", gate["valor"])
    if not livre["ok"]:
        _registrar(lead_id, "whatsapp", etapa, "skipped", livre["motivo"])
        return {"ok": False, "bloqueado": True, "erro": livre["motivo"]}

    caminho = material_pronto(lead_id)
    if not caminho:
        if not gerar_se_faltar:
            # quem chama de dentro do atendimento não pode esperar o Chromium
            return {"ok": False, "faltou_material": True,
                    "erro": "material ainda não está pronto"}
        caminho = gerar_material(lead_id)
    if not caminho:
        _registrar(lead_id, "whatsapp", etapa, "failed", "material não pôde ser gerado")
        return {"ok": False, "erro": "material não pôde ser gerado"}

    r = _post("/api/enviar_arquivo", {"numero": gate["valor"],
                                      "caminho": str(caminho),
                                      "legenda": legenda})
    if r.get("ok"):
        _registrar(lead_id, "whatsapp", etapa, "sent",
                   f"material {caminho.name} para {gate['valor']}")
        return {"ok": True, "arquivo": str(caminho), "numero": gate["valor"]}

    erro = r.get("erro", "falha desconhecida")
    _registrar(lead_id, "whatsapp", etapa, "failed", erro)
    return {"ok": False, "erro": erro}


def material_pronto(lead_id: int):
    """O PDF do lead SE ele existir e estiver atualizado. Nunca gera."""
    from pathlib import Path
    from datetime import datetime
    try:
        import dashboard_db as ddb
        lead = db.get_lead(lead_id) or {}
        carimbo = (lead.get("updated_at") or "")[:19]
        try:
            limite = datetime.strptime(carimbo, "%Y-%m-%d %H:%M:%S")
        except Exception:
            limite = None
        # anexos_do_lead já vem do mais novo para o mais antigo
        for a in ddb.anexos_do_lead(lead_id) or []:
            p = Path(a["path"])
            if not p.exists():
                continue
            if limite and datetime.fromtimestamp(p.stat().st_mtime) < limite:
                continue          # material mais velho que o próprio lead
            return p
    except Exception:
        pass
    return None


def gerar_material(lead_id: int):
    """Gera o PDF. LENTO: sobe um Chromium pelo Playwright, leva segundos."""
    from pathlib import Path
    try:
        import proposta_designs
        caminho = proposta_designs.gerar_final(lead_id)
        p = Path(caminho) if caminho else None
        if not (p and p.exists()):
            return None
        try:
            import dashboard_db as ddb
            ddb.add_anexo(lead_id, p.name, str(p))
        except Exception:
            pass
        return p
    except Exception as e:
        print(f"[whatsapp_sender] falha ao gerar material do lead {lead_id}: {e}")
        return None


def preparar_material(lead_id: int):
    """Garante o material em segundo plano, sem travar quem chamou."""
    import threading
    if material_pronto(lead_id):
        return
    threading.Thread(target=gerar_material, args=(lead_id,), daemon=True).start()


def _material_do_lead(lead_id: int):
    """Compatibilidade: material pronto, gerando na hora se faltar (lento)."""
    return material_pronto(lead_id) or gerar_material(lead_id)


def _canonica(etapa: str) -> str:
    try:
        import esteira_wa as ew
        return ew._etapa_canonica(etapa) or (etapa or "")
    except Exception:
        return etapa or ""


def repeticao(lead_id: int, etapa: str) -> Optional[str]:
    """Este lead já recebeu esta mensagem? Devolve o motivo, ou None se pode sair."""
    alvo = _canonica(etapa)
    limite = getattr(config, "ESTEIRA_MIN_HORAS_ENTRE_TOQUES", 20)
    with db.conn() as con:
        linhas = [dict(r) for r in con.execute(
            "SELECT etapa, status, sent_at, created_at FROM touches "
            "WHERE lead_id=? AND canal='whatsapp' AND status IN ('sent','queued')",
            (lead_id,))]
    if not linhas:
        return None

    for r in linhas:
        if alvo and _canonica(r["etapa"]) == alvo:
            quando = (r["sent_at"] or r["created_at"] or "")[:16]
            return (f"este lead já recebeu esta mensagem em {quando} "
                    f"(etapa {alvo}); envio repetido recusado")

    from datetime import datetime, timedelta
    agora = datetime.now()
    for r in linhas:
        bruto = (r["sent_at"] or r["created_at"] or "")[:19]
        try:
            quando = datetime.strptime(bruto, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if agora - quando < timedelta(hours=limite):
            faltam = timedelta(hours=limite) - (agora - quando)
            horas = round(faltam.total_seconds() / 3600, 1)
            return (f"este lead recebeu uma mensagem em {bruto[:16]}; "
                    f"faltam {horas}h para o mínimo de {limite}h entre toques")
    return None


def _remetentes_disponiveis(conta: str = "") -> List[Dict]:
    """As contas que podem enviar agora: conectadas no bot E com vaga nas 24h."""
    do_bot = contas()
    if not do_bot.get("no_ar"):
        return []
    prontas = {str(c.get("conta") or c.get("numero") or ""): c
               for c in do_bot.get("contas", []) if c.get("pronto")}

    saida = []
    for e in limites.estado("whatsapp"):
        numero = str(e["conta"] or "")
        if conta and numero != str(conta):
            continue
        # conta pausada não envia, mesmo conectada no bot
        if e.get("status") != "active" or e["restam"] <= 0:
            continue
        # casa o número cadastrado com o que o bot reporta (formatos variam)
        so_digitos = "".join(ch for ch in numero if ch.isdigit())
        if len(so_digitos) < 8:
            continue        # "" casaria com qualquer conta do bot
        casou = next((k for k in prontas
                      if "".join(ch for ch in k if ch.isdigit()).endswith(so_digitos[-8:])), None)
        if casou is None:
            continue
        saida.append({"conta": numero, "restam": e["restam"], "id_no_bot": casou})

    # quem tem mais vaga começa: reparte melhor quando o lote é menor que o total
    saida.sort(key=lambda x: -x["restam"])
    return saida


def _repartir(alvos: List[Dict], remetentes: List[Dict]) -> List:
    """Divide os alvos entre as contas, em rodízio, respeitando a vaga de cada uma."""
    if len(remetentes) == 1:
        return [(remetentes[0]["conta"], alvos)]
    baldes = {r["conta"]: [] for r in remetentes}
    vagas = {r["conta"]: r["restam"] for r in remetentes}
    ordem = [r["conta"] for r in remetentes]
    i = 0
    for a in alvos:
        # procura a próxima conta com vaga
        for _ in range(len(ordem)):
            c = ordem[i % len(ordem)]
            i += 1
            if vagas[c] > 0:
                baldes[c].append(a)
                vagas[c] -= 1
                break
    return [(c, baldes[c]) for c in ordem if baldes[c]]


def enviar_lote(itens: List[Dict], etapa: str = "dia0",
                permitir_repeticao: bool = False, conta: str = "") -> Dict:
    """Ver `_enviar_lote`: conferir a vaga, entregar e reservar é um passo só."""
    with _TRAVA_VAGA:
        return _enviar_lote(itens, etapa, permitir_repeticao, conta)


def _enviar_lote(itens: List[Dict], etapa: str = "dia0",
                 permitir_repeticao: bool = False, conta: str = "") -> Dict:
    """
    itens: [{lead_id, mensagem, etapa?}]. Aplica a trava lead a lead e delega o RITMO ao
    bot, que espera um tempo sorteado entre um envio e o seguinte.
    """
    # O cliente está pronto? Perguntar ANTES do laço, e não descobrir no POST final,
    # muda três coisas: o operador recebe o motivo em vez de um "nada foi enviado"
    # mudo, não se gasta a trava de identidade lead a lead à toa, e nenhum toque
    # `skipped` fica gravado sugerindo bloqueio que não houve.
    est = status(conta)
    if not est.get("pronto"):
        motivo = ("o servidor do bot não respondeu" if not est.get("no_ar")
                  else "o WhatsApp ainda não está conectado (conecte a conta na "
                       "aba Contas, ou espere terminar de sincronizar)")
        return {"ok": False, "erro": f"nada foi enviado: {motivo}",
                "bloqueados": [], "bot": est}

    # Quantas ainda cabem no teto de 24h desta conta.
    remetentes = _remetentes_disponiveis(conta)
    if not remetentes:
        return {"ok": False, "bloqueados": [],
                "erro": "nada foi enviado: nenhuma conta de WhatsApp ativa, conectada "
                        "e com vaga hoje (limites na aba Contas)"}
    cabem = sum(r["restam"] for r in remetentes)

    alvos, bloqueados = [], []
    for it in itens:
        lead_id = it.get("lead_id")
        etapa_item = it.get("etapa") or etapa
        # trava de repetição ANTES das outras: é a mais barata (uma consulta) e
        # a que evita o estrago mais visível, a mesma mensagem chegando duas
        # vezes na conversa do prospect
        if not permitir_repeticao:
            repetido = repeticao(lead_id, etapa_item)
            if repetido:
                bloqueados.append({"lead_id": lead_id, "motivo": repetido,
                                   "repeticao": True})
                _registrar(lead_id, "whatsapp", etapa_item, "skipped", repetido)
                continue
        gate = identidade.pode_enviar(lead_id, "whatsapp")
        if not gate["ok"]:
            bloqueados.append({"lead_id": lead_id, "motivo": gate["motivo"]})
            _registrar(lead_id, "whatsapp", etapa_item, "skipped", gate["motivo"])
            continue
        livre = modo.permite("whatsapp", gate["valor"])
        if not livre["ok"]:
            bloqueados.append({"lead_id": lead_id, "motivo": livre["motivo"]})
            _registrar(lead_id, "whatsapp", etapa_item, "skipped", livre["motivo"])
            continue
        alvos.append({"numero": gate["valor"], "mensagem": it["mensagem"],
                      "lead_id": lead_id, "etapa": etapa_item,
                      "experimento": it.get("experimento", ""),
                      "variante": it.get("variante", "")})
    # o que passou nas travas mas não cabe no teto fica para quando a vaga voltar
    if cabem is not None and len(alvos) > cabem:
        sobra = alvos[cabem:]
        alvos = alvos[:cabem]
        for a in sobra:
            motivo = ("acima das vagas de hoje das contas de WhatsApp; "
                      "fica para quando uma vaga voltar")
            bloqueados.append({"lead_id": a["lead_id"], "motivo": motivo, "teto": True})
            _registrar(a["lead_id"], "whatsapp", a["etapa"], "skipped", motivo)

    if not alvos:
        # o motivo real vem de `bloqueados`: pode ser identidade OU modo de
        # envio, e dizer "identidade" nos dois casos manda o operador procurar
        # o problema no lugar errado
        motivos = "; ".join(sorted({b["motivo"] for b in bloqueados})) or "sem alvos"
        return {"ok": False, "erro": f"nenhum lead liberado para envio: {motivos}",
                "bloqueados": bloqueados}

    # a etapa já vai em cada alvo (o bot devolve o resultado por alvo com ela).
    # O intervalo é a faixa em SEGUNDOS que o bot sorteia entre um envio e o
    # seguinte: sai daqui para o config ser a fonte única dos três canais.
    corpo_lote = {
        "intervalo": {"min": config.ENVIO_DELAY_MIN, "max": config.ENVIO_DELAY_MAX},
    }

    # Reparte os alvos entre as contas e manda UM LOTE PARA CADA. Elas correm
    # em paralelo: no bot cada conta tem a própria fila, então o espaçamento
    # que protege um número não atrasa o outro.
    fatias = _repartir(alvos, remetentes)
    r = {"ok": False}
    enviados, por_conta = [], []
    for numero_conta, parte in fatias:
        corpo = dict(corpo_lote)
        corpo["alvos"] = parte
        if numero_conta:
            corpo["conta"] = numero_conta
        resp = _post("/api/enviar_lote", corpo)
        por_conta.append({"conta": numero_conta, "alvos": len(parte),
                          "ok": bool(resp.get("ok")), "erro": resp.get("erro", "")})
        if resp.get("ok"):
            r = {"ok": True}
            enviados.extend(parte)
            # a vaga é gasta AGORA (o bot leva até ~1h para mandar o lote); a
            # confirmação em /api/whatsapp/resultado só troca 'fila' por 'enviado'
            c = limites.conta_por_identificador("whatsapp", numero_conta)
            if c:
                for a in parte:
                    limites.reservar(c["id"], "whatsapp", a["numero"], a["lead_id"])
        else:
            for a in parte:
                motivo = f"o bot recusou o lote de {numero_conta}: {resp.get('erro','sem motivo')}"
                bloqueados.append({"lead_id": a["lead_id"], "motivo": motivo})
                _registrar(a["lead_id"], "whatsapp", a["etapa"], "skipped", motivo)

    alvos = enviados
    if r.get("ok"):
        for a in alvos:
            # `queued`, não `sent`. Este endpoint do bot responde ANTES do primeiro envio
            # ("iniciado em segundo plano"), então o ok aqui só diz que a fila foi aceita.
            _registrar(a["lead_id"], "whatsapp", a["etapa"], "queued",
                       f"lote: {a['numero']}")
            _log_experimento(a["lead_id"], a["etapa"], a["mensagem"],
                             a.get("experimento", ""), a.get("variante", ""))
            if a["etapa"] == "dia0_ping":
                _marcar_ping(a["lead_id"], a["numero"])
    return {"ok": bool(r.get("ok")), "enviados": len(alvos),
            "bloqueados": bloqueados, "contas": por_conta, "resposta": r}


def _marcar_ping(lead_id: int, numero: str):
    """Deixa a conversa no estágio `ping`, à espera da confirmação."""
    try:
        import conversa_ia
        conversa_ia.init()
        conversa_ia.set_estado(numero, estagio="ping", lead_id=lead_id)
    except Exception:
        pass
    # O material era pré-montado aqui porque o ATENDENTE entregava o PDF sozinho
    # quando a clínica respondia "pode mandar": valia adiantar enquanto ela lia o
    # ping.
    if getattr(config, "ATENDENTE_AUTOMATICO", False):
        try:
            preparar_material(lead_id)
        except Exception:
            pass


def _log_experimento(lead_id, etapa, texto, experimento="", variante=""):
    """Alimenta o log de mensagens que sustenta as métricas e o teste A/B."""
    try:
        import experimentos
        experimentos.registrar_saida(lead_id, "whatsapp", etapa, texto,
                                     experimento, variante)
    except Exception:
        pass


# ── Registro no CRM ──────────────────────────────────────────────────────────

def _registrar(lead_id: int, canal: str, etapa: str, status: str, detalhe: str):
    """Marca o toque agendado, ou cria um avulso se não havia cadência."""
    try:
        alvo = None
        for t in db.touches_for(lead_id):
            if t["canal"] == canal and t["etapa"] == etapa and t["status"] == "scheduled":
                alvo = t
                break
        if alvo:
            db.mark_touch(alvo["id"], status, detalhe)
        else:
            from datetime import date
            tid = db.schedule_touch(lead_id, canal, etapa, date.today().isoformat())
            db.mark_touch(tid, status, detalhe)
    except Exception:
        pass


if __name__ == "__main__":
    import sys
    s = status()
    if not s["no_ar"]:
        print(f"Bot: fora do ar em {BASE} ({s['erro']})")
        print("Suba com: node senders/whatsapp_bot/server.js")
    elif s["pronto"]:
        print(f"Bot: conectado como {s['numero']}")
    else:
        print(f"Bot: no ar, mas sem conta conectada. Escaneie o QR em {BASE}")
    if len(sys.argv) >= 3 and sys.argv[1] == "--numero":
        print(enviar_numero(sys.argv[2], " ".join(sys.argv[3:]) or "Teste."))
    elif len(sys.argv) >= 3:
        print(enviar(int(sys.argv[1]), " ".join(sys.argv[2:])))
    else:
        print("uso: python -m senders.whatsapp_sender <lead_id> <mensagem>")
        print("     python -m senders.whatsapp_sender --numero 82999999999 <mensagem>")
