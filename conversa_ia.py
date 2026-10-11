"""conversa_ia.py — Atendente de IA que conversa no WhatsApp até a reunião."""

import json
import re
from datetime import datetime
from typing import Dict, List, Optional

import config
import db
import ia_hook

MAX_HISTORICO = 14           # mensagens enviadas ao modelo
LIMITE_SEM_HUMANO = 12       # depois disso, chama você
LIMITE_CHEIRO_IA = 6         # acima disso, pede uma reescrita (folgado: é conversa)


# ── Banco ────────────────────────────────────────────────────────────────────

def init():
    with db.conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS conversas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                numero TEXT NOT NULL,
                lead_id INTEGER,
                autor TEXT NOT NULL,          -- lead | ia | humano
                texto TEXT NOT NULL,
                criado_em TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_conv_num ON conversas(numero);

            CREATE TABLE IF NOT EXISTS conversa_estado (
                numero TEXT PRIMARY KEY,
                lead_id INTEGER,
                estagio TEXT DEFAULT 'novo',  -- novo|conversando|interessado|reuniao|humano|encerrado
                pausado INTEGER DEFAULT 0,    -- 1 = IA não responde mais
                atualizado_em TEXT DEFAULT (datetime('now','localtime'))
            );
        """)


def _so_digitos(n: str) -> str:
    return re.sub(r"\D", "", n or "")


def lead_por_numero(numero: str) -> Optional[Dict]:
    """Acha o lead dono deste número (compara só os dígitos finais)."""
    alvo = _so_digitos(numero)[-8:]
    if len(alvo) < 8:
        return None
    with db.conn() as con:
        linhas = con.execute(
            "SELECT * FROM leads WHERE telefone <> '' OR whatsapp <> ''").fetchall()
    for r in linhas:
        l = dict(r)
        for campo in ("telefone", "whatsapp"):
            if alvo and alvo in _so_digitos(l.get(campo) or ""):
                return l
    return None


def historico(numero: str, limite: int = MAX_HISTORICO) -> List[Dict]:
    with db.conn() as con:
        rows = con.execute(
            "SELECT autor, texto, criado_em FROM conversas WHERE numero=? "
            "ORDER BY id DESC LIMIT ?", (numero, limite)).fetchall()
    return [dict(r) for r in reversed(rows)]


def registrar(numero: str, autor: str, texto: str, lead_id=None):
    init()
    with db.conn() as con:
        con.execute("INSERT INTO conversas(numero,lead_id,autor,texto) VALUES(?,?,?,?)",
                    (numero, lead_id, autor, texto))


def _chave(numero: str) -> str:
    """A chave sob a qual a conversa deste telefone já está gravada."""
    n = _so_digitos(numero)
    if len(n) < 8:
        return n
    with db.conn() as con:
        for (existente,) in con.execute("SELECT numero FROM conversa_estado"):
            if _so_digitos(existente)[-8:] == n[-8:]:
                return existente
    return n


def estado(numero: str) -> Dict:
    init()
    numero = _chave(numero)
    with db.conn() as con:
        r = con.execute("SELECT * FROM conversa_estado WHERE numero=?", (numero,)).fetchone()
        if not r:
            con.execute("INSERT INTO conversa_estado(numero) VALUES(?)", (numero,))
            return {"numero": numero, "lead_id": None, "estagio": "novo", "pausado": 0}
    return dict(r)


def set_estado(numero: str, **campos):
    """Grava o estado da conversa, criando a linha se ela ainda não existir."""
    init()
    numero = _chave(numero)
    permitido = {"lead_id", "estagio", "pausado"}
    sets = [f"{k}=?" for k in campos if k in permitido]
    vals = [v for k, v in campos.items() if k in permitido]
    if not sets:
        return
    vals.append(numero)
    with db.conn() as con:
        con.execute("INSERT OR IGNORE INTO conversa_estado(numero) VALUES(?)", (numero,))
        con.execute(f"UPDATE conversa_estado SET {','.join(sets)}, "
                    f"atualizado_em=datetime('now','localtime') WHERE numero=?", vals)


# ── Regras que não dependem da IA ────────────────────────────────────────────

_PARAR = re.compile(r"\b(n[ãa]o (quero|tenho interesse)|para de (mandar|enviar)|sai(r)? da lista|"
                    r"descadastr|me tira|n[ãa]o me mand|bloquear|spam|denunci)", re.I)
_HUMANO = re.compile(r"\b(falar com (uma pessoa|humano|atendente|respons[áa]vel)|"
                     r"quero falar com algu[ée]m)", re.I)

# Pergunta DIRETA sobre ser automatizado.
_E_ROBO = re.compile(
    r"(voc[êe]\s+[ée]\s+(um[a]?\s+)?(ia|rob[ôo]|bot|m[áa]quina|intelig[êe]ncia)"
    r"|[ée]\s+(um[a]?\s+)?(rob[ôo]|bot|ia)\b"
    r"|isso\s+[ée]\s+(autom[áa]tic|um\s+bot|uma\s+ia)"
    r"|mensagem\s+autom[áa]tica"
    r"|atendimento\s+autom[áa]tic"
    r"|(t[ôo]|estou)\s+falando\s+com\s+(um[a]?\s+)?(rob[ôo]|bot|ia|m[áa]quina))", re.I)
_REUNIAO = re.compile(r"\b(pode ligar|me liga|vamos conversar|marcar|agendar|reuni[ãa]o|"
                      r"que horas|amanh[ãa]|hoje [àa]s|dispon[íi]vel|topo|bora)\b", re.I)

# Mensagem que é SÓ cumprimento, sem conteúdo.
_SAUDACAO = re.compile(
    r"^(oi+|ol[áa]+|e a[íi]|eae|opa|hey|hello|al[ôo]|"
    r"bom dia|boa tarde|boa noite|"
    r"tudo bem|tudo bom|td bem|beleza|como vai|"
    r"(oi+|ol[áa]+|opa|bom dia|boa tarde|boa noite)[\s,]+(tudo bem|tudo bom|td bem|beleza)"
    r")$", re.I)


# Qualquer dado do diagnóstico aparecendo no texto.
_TEM_DADO = re.compile(
    r"(r\$\s*[\d.,]+"
    r"|\d+\s*[ºoª]?\s*(posi[çc][ãa]o|lugar)"
    r"|\bem\s+\d+[ºoª]"
    r"|nota\s+\d"
    r"|\d+\s*avalia[çc]"
    r"|\d+\s*(pacientes?|consultas?))", re.I)


# Pedido direto para ver o que foi encontrado.
_NAO_E_A_CLINICA = re.compile(
    r"(n[ãa]o\s+[ée]\s+(aqui|n[ãa]o|essa|esse|dessa|desse)"
    r"|n[ãa]o\s+(sou|somos|[ée])\s+d[ao]"
    r"|engano|n[úu]mero\s+errado|se\s+enganou|pessoa\s+errada"
    r"|aqui\s+n[ãa]o\s+[ée]|nada\s+a\s+ver)", re.I)

# Resposta ao ping confirmando. Deliberadamente larga: qualquer coisa que não
# seja recusa, pedido de parada ou "número errado" conta como abertura, porque
# "sim", "opa", "pois não", "sou eu" e "em que posso ajudar" são todos convite.
_CONFIRMA_PING = re.compile(
    r"(\bsim\b|\bsou\b|\bsomos\b|\b[ée]\b|isso|pois\s+n[ãa]o|opa|ol[áa]|oi\b"
    r"|em\s+que\s+(posso|podemos)|pode\s+falar|diga|fala|quem\s+[ée]|qual|"
    r"\bboa\b|\bbom\b)", re.I)

# Autorização para mandar o material. É a deixa do terceiro toque.
_AUTORIZA_MATERIAL = re.compile(
    r"(pode (mandar|enviar|passar|sim)|manda(r)?\s*(a[ií]|sim|pra mim|por favor)?\b"
    r"|envia(r)?\b|\bsim\b|claro|por favor|quero ver|pode ser|beleza|blz"
    r"|t[áa] bom|ok\b|okay|uhum|aceito|gostaria)", re.I)

# Recusa explícita ao material. Sem isto, "não precisa" cairia no padrão acima
# pelo "precisa" e a pessoa receberia o PDF que acabou de recusar.
_RECUSA_MATERIAL = re.compile(
    r"(n[ãa]o (precisa|quero|obrigad|manda|envia|preciso)|sem interesse|"
    r"agora n[ãa]o|deixa (pra|para) (depois|l[áa])|dispenso)", re.I)

_QUER_VER = re.compile(
    r"(me\s+mostr|pode\s+mostrar|quero\s+ver|manda\s+(a[ií]|isso|o\s+que)"
    r"|o\s+que\s+(voc[êe]|vc)\s+(viu|achou|encontrou)"
    r"|que\s+(problema|pontos?)|qual\s+(o\s+)?problema"
    r"|como\s+assim\s+perde|o\s+que\s+t[áa]\s+errado)", re.I)


def _fato_principal(lead: Optional[Dict]) -> str:
    """A observação mais forte e CONFERÍVEL sobre a clínica, em uma frase."""
    if not lead:
        return ""
    pos = lead.get("posicao")
    nota = lead.get("avaliacao_num")
    cat = (lead.get("categoria") or "clínica").lower()
    if pos and pos > 3:
        return (f"vocês aparecem em {pos}º quando alguém procura {cat} na cidade, "
                f"e os três primeiros ficam com quase todos os cliques")
    if nota is not None and nota < config.NOTA_ALVO:
        return (f"a nota de vocês no Google está em {nota:.1f}, e a partir de 4,5 "
                f"o paciente para de comparar e liga direto")
    if not lead.get("tem_site"):
        return ("o perfil de vocês no Google não tem site ligado, então quem chega "
                "ali não vê serviço nem consegue agendar")
    if not lead.get("tem_instagram"):
        return "o perfil de vocês no Google não tem o Instagram ligado"
    return ""


def _so_saudacao(msg: str) -> bool:
    limpo = re.sub(r"[^\wÀ-ÿ\s]", " ", msg or "", flags=re.U)
    limpo = re.sub(r"\s+", " ", limpo).strip()
    return bool(limpo) and bool(_SAUDACAO.match(limpo))


# ── Prompt ───────────────────────────────────────────────────────────────────

def _sistema(lead: Optional[Dict], diag: Optional[Dict], prop: Optional[Dict]) -> str:
    fcfg = config.FAIXAS_PRECO
    tabela = " · ".join(f"Nível {n} {f['nome']}: R$ {f['min']} a R$ {f['max']}/mês"
                        for n, f in fcfg.items())
    if lead:
        ctx = (f"CLÍNICA: {lead['nome']} · {lead.get('categoria','')} · "
               f"posição {lead.get('posicao','?')}º no Google · "
               f"nota {lead.get('avaliacao_num') or 'não informada'}")
        if diag:
            ctx += f"\nDIAGNÓSTICO JÁ FEITO: deixa de receber cerca de R$ {diag.get('roi_reais',0):,}/mês".replace(",", ".")
            try:
                probs = json.loads(diag.get("problemas") or "[]")
                if probs:
                    ctx += "\nPONTOS ENCONTRADOS: " + "; ".join(probs[:3])
            except Exception:
                pass
        if prop:
            ctx += f"\nPLANO INDICADO: Nível {prop.get('nivel')} ({prop.get('nivel_nome')})"
    else:
        ctx = "NÚMERO DESCONHECIDO: não temos diagnóstico desta clínica ainda."

    return f"""Você é {config.ATENDENTE_NOME}, da {config.EMPRESA_NOME}, empresa que cuida da
presença digital de clínicas de saúde (Google Meu Negócio, reputação, WhatsApp e
Instagram). Está conversando por WhatsApp com o dono ou a recepção de uma clínica.

SUA IDENTIDADE (use sempre estes dados, nunca invente outros):
- Seu nome: {config.ATENDENTE_NOME}
- Empresa: {config.EMPRESA_NOME}
- Base: {config.EMPRESA_CIDADE}
É TERMINANTEMENTE PROIBIDO escrever placeholders como [seu nome], [nome da empresa]
ou [x]. Se um dado não estiver aqui, escreva a frase sem ele.

SEU ÚNICO OBJETIVO: marcar uma conversa de 10 minutos (chamada ou vídeo).
Você NÃO fecha contrato, NÃO cobra, NÃO envia link de pagamento.

CONTEXTO DESTA CLÍNICA:
{ctx}

TABELA DE PREÇOS (só cite se perguntarem):
{tabela}

COMO CONDUZIR A CONVERSA (nesta ordem, sem pular etapa):
1. RECONHECER. Antes de argumentar, mostre que entendeu o que a pessoa trouxe.
   Se veio objeção ou dúvida, nomeie ("faz sentido", "imagino a correria"). O erro
   mais comum é pular direto para oferecer horário: quem não se sentiu ouvido não
   marca nada.
2. CALIBRAR. Quando perguntar, prefira "como" ou "o que" a perguntas de sim/não.
   "Como vocês recebem paciente novo hoje?" abre a conversa; "posso te ligar?"
   fecha cedo demais, porque o silêncio já responde não.
3. Consiga uma concordância pequena antes de propor a reunião ("é mais ou menos
   isso?", "faz sentido?").
4. SÓ ENTÃO convide para os 10 minutos, com 2 opções concretas de horário (ex.:
   "amanhã antes do primeiro paciente, ou fim do dia?").
5. Se aceitarem, confirme o horário e diga em uma linha o que acontece nos 10
   minutos: você mostra a conta do que a posição de hoje custa.

COMO LIDAR COM OBJEÇÃO (reposicione, não empurre):
- "Quanto custa?": dê a faixa real da tabela, diga que o valor exato depende do
  que a clínica precisa, e puxe para os 10 minutos. Nunca solte o preço e cale:
  preço sem valor construído vira "caro".
- "Vou pensar" / "depois eu vejo": não insista. Nomeie e ofereça saída, que é o
  que costuma trazer a objeção verdadeira: "parece que agora não é o momento, ou
  ficou alguma dúvida que eu não expliquei bem?".
- "Já tenho quem cuida disso" / "tenho agência": NÃO desmereça quem trabalha com
  eles. Reposicione como segundo olhar, não substituição: você mostra a conta, e
  se estiver tudo certo eles confirmam que estão no caminho.
- "Não tenho tempo": encolha o pedido e devolva o controle do horário. São 10
  minutos, no horário que for menos ruim para a pessoa.
- "Manda por escrito" / "manda proposta": ofereça um resumo curto, mas amarre a
  conversa, porque a parte que importa é de onde sai o número.
- "É caro": reancore no custo de deixar como está. É PROIBIDO dar desconto.
- "Como conseguiu meu contato?": a verdade, curto. O contato está público no
  perfil do Google da clínica. Ofereça parar na hora se a pessoa preferir, e
  PARE AÍ. Emendar oferta na mesma mensagem anula a oferta de parar e é o que
  faz a pessoa bloquear.

REGRAS DE ESTILO:
- Mensagens curtas, de 1 a 3 linhas. É WhatsApp, não e-mail.
- Português do Brasil, tom de gente, educado e direto. Sem emoji (no máximo um).
- Uma pergunta por vez. Nunca dispare várias perguntas juntas.
- O convite é sempre o menor passo possível. Facilitar o não aumenta o sim.
- NO MÁXIMO UM DADO POR MENSAGEM. Posição, nota, número de avaliações e valor
  perdido são quatro argumentos diferentes: jogar todos de uma vez é despejo de
  relatório, não conversa. Escolha o que responde ao que a pessoa acabou de
  dizer e guarde o resto para quando ela perguntar.
- O diagnóstico é a SUA munição, não a abertura. Não traga número enquanto a
  pessoa só estiver cumprimentando ou sondando quem você é. Antes disso, converse.
- MAS se ela perguntar o que você viu, o que está errado ou pedir para mostrar,
  ENTREGUE o dado mais forte na hora, em uma frase, e só então convide para os 10
  minutos. Segurar informação de quem pediu é evasivo, irrita e queima a conversa.
  A reunião se ganha mostrando alguma coisa, não prometendo mostrar.
  Aí o dado tem que ser ESPECÍFICO e conferível, com o número que está no
  contexto: "vocês aparecem em 8º quando alguém procura dentista na cidade" serve.
  "Seu perfil não está otimizado" NÃO serve: é vago, não dá para conferir, e é
  exatamente o jargão que faz a pessoa achar que você fala como todo vendedor.
- É PROIBIDO: inventar número, prazo, desconto, resultado garantido, retorno ou
  lucro; prometer primeira posição no Google; dizer que é rápido ou automático.
- Se perguntarem quanto tempo leva, seja honesto: as primeiras mudanças no perfil
  aparecem em algumas semanas, e reputação se constrói ao longo do tempo.
- Se não souber algo, diga que confirma com a equipe na conversa.
- Nunca diga que é uma inteligência artificial a não ser que perguntem
  diretamente. Se perguntarem, responda com honestidade e ofereça passar para
  uma pessoa da equipe.
- PROIBIDO usar travessão longo (— ou –). Use ponto ou vírgula.

Responda SOMENTE com o texto da mensagem a enviar, sem aspas e sem comentários."""


# ── Núcleo ───────────────────────────────────────────────────────────────────

LEGENDA_MATERIAL = ("Aqui está. Dei uma olhada em cada ponto e deixei anotado "
                    "o que dá para ajustar primeiro.")
CONVITE_REUNIAO = ("Se fizer sentido, consigo te mostrar em 10 minutos de onde "
                   "sai cada número aí e o que mexer primeiro. Prefere de manhã "
                   "ou no fim do dia?")


def _convite_depois(numero: str, lead_id: int, segundos: int):
    """Manda o convite de reunião DEPOIS do anexo, com folga."""
    import threading, time

    def _tarefa():
        time.sleep(segundos)
        try:
            from senders import whatsapp_sender
            r = whatsapp_sender.enviar(lead_id, CONVITE_REUNIAO, etapa="convite_reuniao")
            if r.get("ok"):
                registrar(numero, "ia", CONVITE_REUNIAO, lead_id)
        except Exception as e:
            print(f"[conversa_ia] convite de reunião falhou: {e}")

    threading.Thread(target=_tarefa, daemon=True).start()


def _entregar_material(numero: str, lead_id: int) -> Dict:
    """A clínica autorizou: manda o PDF e agenda o convite para depois."""
    from senders import whatsapp_sender
    envio = whatsapp_sender.enviar_material(lead_id, legenda=LEGENDA_MATERIAL,
                                            gerar_se_faltar=False)
    if envio.get("ok"):
        set_estado(numero, estagio="material_enviado")
        registrar(numero, "ia", f"[material enviado] {LEGENDA_MATERIAL}", lead_id)
        _convite_depois(numero, lead_id, config.ENVIO_DELAY_MIN)
        return {"responder": False, "estagio": "material_enviado",
                "motivo": "material enviado; convite de reunião sai em seguida"}

    # Ainda sendo montado: responder é obrigatório. Silêncio depois de a pessoa
    # autorizar é pior do que não ter pedido, porque ela deu permissão e recebeu
    # nada de volta.
    if envio.get("faltou_material"):
        set_estado(numero, estagio="material_pendente")
        texto = "Perfeito. Estou finalizando a sua análise e te mando em instantes."
        registrar(numero, "ia", texto, lead_id)
        _material_depois(numero, lead_id)
        return {"responder": True, "texto": texto, "estagio": "material_pendente",
                "motivo": "autorizou; material sendo gerado em segundo plano"}

    # Falha de verdade: fala a verdade e entrega para uma pessoa.
    set_estado(numero, estagio="humano")
    texto = ("Deu um problema aqui para anexar o arquivo. Já estou resolvendo e "
             "te mando ainda hoje, tudo bem?")
    registrar(numero, "ia", texto, lead_id)
    return {"responder": True, "texto": texto, "estagio": "humano",
            "motivo": f"autorizou mas o material falhou: {envio.get('erro')}"}


def _material_depois(numero: str, lead_id: int):
    """Gera o PDF fora do request e manda quando ficar pronto."""
    import threading

    def _tarefa():
        try:
            from senders import whatsapp_sender
            if not whatsapp_sender.material_pronto(lead_id):
                whatsapp_sender.gerar_material(lead_id)
            envio = whatsapp_sender.enviar_material(
                lead_id, legenda=LEGENDA_MATERIAL, gerar_se_faltar=False)
            if envio.get("ok"):
                set_estado(numero, estagio="material_enviado")
                registrar(numero, "ia", f"[material enviado] {LEGENDA_MATERIAL}", lead_id)
                _convite_depois(numero, lead_id, config.ENVIO_DELAY_MIN)
            else:
                set_estado(numero, estagio="humano")
                print(f"[conversa_ia] material do lead {lead_id} falhou: {envio.get('erro')}")
        except Exception as e:
            set_estado(numero, estagio="humano")
            print(f"[conversa_ia] material do lead {lead_id} explodiu: {e}")

    threading.Thread(target=_tarefa, daemon=True).start()


def _marcar_respondeu(lead_id) -> bool:
    """Move o lead para a coluna "Respondeu"."""
    if not lead_id:
        return False
    try:
        import dashboard_db as ddb
        atual = (db.get_lead(lead_id) or {}).get("kanban") or "lista"
        if atual not in ddb.KANBAN_ANTES_DA_RESPOSTA:
            return False
        ddb.set_kanban(lead_id, "respondeu")
        return True
    except Exception as e:
        print(f"[conversa_ia] não deu para mover o lead {lead_id} para 'respondeu': {e}")
        return False


def responder(numero: str, mensagem: str) -> Dict:
    """Recebe a mensagem do lead e devolve {responder: bool, texto, motivo, estagio}."""
    init()
    numero = _so_digitos(numero)
    st = estado(numero)
    lead = lead_por_numero(numero)
    lead_id = lead["id"] if lead else st.get("lead_id")
    # O estado da conversa pode saber o lead mesmo quando o telefone não casa
    # (número novo, contato pela recepção, vínculo feito à mão). Sem carregar o
    # lead aqui, o atendente perde o contexto da clínica e responde no vazio.
    if lead is None and lead_id:
        lead = db.get_lead(lead_id)
    registrar(numero, "lead", mensagem, lead_id)

    # Mensagem SEM TEXTO não move o lead.
    if not (mensagem or "").strip():
        return {"responder": False, "estagio": st.get("estagio"),
                "motivo": "mensagem sem texto (status ou mídia sem legenda): "
                          "registrada, mas não conta como resposta"}
    # alimenta o log de métricas: é esta entrada que vira "taxa de resposta"
    try:
        import experimentos
        experimentos.registrar_entrada(lead_id, "whatsapp", mensagem, numero)
    except Exception:
        pass
    if lead_id and not st.get("lead_id"):
        set_estado(numero, lead_id=lead_id)
    # Qualquer resposta joga o lead na coluna "Respondeu", que é onde o operador
    # assume.
    _marcar_respondeu(lead_id)

    # Trava, não instrução de prompt.
    if not getattr(config, "ATENDENTE_AUTOMATICO", False):
        if _PARAR.search(mensagem or ""):
            # o pedido para parar continua sendo honrado, calado: opt-out é
            # proteção do lead e do número, não uma resposta
            set_estado(numero, estagio="encerrado", pausado=1)
            return {"responder": False, "estagio": "encerrado",
                    "motivo": "lead pediu para parar; registrado e pausado, "
                              "nada foi respondido (atendimento manual)"}
        # 'humano' é o estágio que já significa "esperando uma pessoa". Marcar aqui
        # garante que, se o atendente automático for religado um dia, ele não assume no
        # meio de uma conversa que o operador já estava tocando.
        set_estado(numero, estagio="humano")
        return {"responder": False, "estagio": "humano",
                "motivo": "atendente automático desligado "
                          "(config.ATENDENTE_AUTOMATICO): lead movido para "
                          "'Respondeu', atendimento manual daqui em diante"}

    # 1) pediu para parar: encerra de vez
    if _PARAR.search(mensagem or ""):
        set_estado(numero, estagio="encerrado", pausado=1)
        texto = ("Entendido, não mandamos mais mensagens. Obrigado pela atenção e "
                 "desculpe o incômodo.")
        registrar(numero, "ia", texto, lead_id)
        return {"responder": True, "texto": texto, "estagio": "encerrado",
                "motivo": "lead pediu para parar"}

    # 2) já pausado ou em atendimento humano: não responde
    if st.get("pausado"):
        return {"responder": False, "motivo": "conversa pausada", "estagio": st.get("estagio")}
    if st.get("estagio") == "humano":
        return {"responder": False, "motivo": "aguardando atendimento humano",
                "estagio": "humano"}

    # 3) perguntou DIRETAMENTE se é automatizado: a resposta é a verdade, e não
    # pode depender do modelo, que já mentiu ("Não, sou o Wallisson") quando isso
    # ficou por conta dele. Responde honesto e entrega para humano.
    if _E_ROBO.search(mensagem or ""):
        set_estado(numero, estagio="humano")
        texto = ("Sim, este primeiro atendimento é automatizado. Vou passar para "
                 "uma pessoa da equipe agora, tudo bem?")
        registrar(numero, "ia", texto, lead_id)
        return {"responder": True, "texto": texto, "estagio": "humano",
                "motivo": "perguntou se é automatizado, respondido com honestidade"}

    # 4) pediu humano
    if _HUMANO.search(mensagem or ""):
        set_estado(numero, estagio="humano")
        texto = ("Claro. Já vou pedir para alguém da equipe falar com você por aqui, "
                 "pode ser em instantes?")
        registrar(numero, "ia", texto, lead_id)
        return {"responder": True, "texto": texto, "estagio": "humano",
                "motivo": "lead pediu atendimento humano"}

    # O dia 0 virou dois toques. O primeiro só pergunta se é a clínica certa; a
    # abordagem é a RESPOSTA a quem confirmou, não uma segunda mensagem disparada
    # por conta própria.
    if st.get("estagio") == "ping":
        if _NAO_E_A_CLINICA.search(mensagem or ""):
            set_estado(numero, estagio="encerrado", pausado=1)
            texto = ("Desculpe o incômodo, foi engano da minha parte. "
                     "Não mando mais mensagem por aqui.")
            registrar(numero, "ia", texto, lead_id)
            return {"responder": True, "texto": texto, "estagio": "encerrado",
                    "motivo": "respondeu ao ping dizendo que não é a clínica"}

        if lead_id and _CONFIRMA_PING.search(mensagem or ""):
            try:
                import abordagem
                peca = abordagem.gerar(lead_id, "whatsapp", "dia0")
                texto = (peca.get("texto") or "").strip()
            except Exception:
                texto = ""
            if texto:
                # fica aguardando o "pode mandar": é esse sim que libera o PDF
                set_estado(numero, estagio="aguardando_ok_material")
                registrar(numero, "ia", texto, lead_id)
                try:
                    import experimentos
                    experimentos.registrar_saida(
                        lead_id, "whatsapp", "dia0", texto,
                        peca.get("experimento", ""), peca.get("variante", ""))
                except Exception:
                    pass
                return {"responder": True, "texto": texto,
                        "estagio": "aguardando_ok_material",
                        "motivo": "confirmou o ping, abordagem entregue"}
        # confirmou de um jeito que o padrão não reconheceu, ou o lead não está
        # vinculado: cai no atendente normal em vez de ficar mudo
        set_estado(numero, estagio="conversando")

    # A ordem é a regra. A análise é a moeda de troca: ela chega antes de qualquer
    # pedido de tempo, e o pedido de tempo pega carona no anexo, quando a pessoa já
    # tem o que conferir na mão.
    if st.get("estagio") == "aguardando_ok_material":
        if _RECUSA_MATERIAL.search(mensagem or ""):
            set_estado(numero, estagio="conversando")
            texto = ("Tranquilo, não mando então. Se mudar de ideia, a análise "
                     "fica guardada aqui.")
            registrar(numero, "ia", texto, lead_id)
            return {"responder": True, "texto": texto, "estagio": "conversando",
                    "motivo": "recusou o material"}

        if lead_id and _AUTORIZA_MATERIAL.search(mensagem or ""):
            return _entregar_material(numero, lead_id)

    # 4) conversa longa demais sem fechar: chama você
    hist = historico(numero)
    if len([h for h in hist if h["autor"] == "ia"]) >= LIMITE_SEM_HUMANO:
        set_estado(numero, estagio="humano")
        return {"responder": False, "motivo": "limite de mensagens da IA atingido",
                "estagio": "humano"}

    # 5) IA desligada
    if not ia_hook.IA_ATIVA:
        set_estado(numero, estagio="humano")
        return {"responder": False, "motivo": "IA não configurada", "estagio": "humano"}

    # 6) gera a resposta
    diag = db.get_diagnostico(lead_id) if lead_id else None
    prop = db.get_proposta(lead_id) if lead_id else None
    # Pediu para ver o que você encontrou: responde com o fato concreto, sem rodeio.
    if _QUER_VER.search(mensagem or ""):
        fato = _fato_principal(lead)
        if fato:
            texto = (f"Claro. O principal é que {fato}.\n"
                     f"Posso te mostrar a conta disso em 10 minutos?")
            set_estado(numero, estagio="conversando")
            registrar(numero, "ia", texto, lead_id)
            return {"responder": True, "texto": texto, "estagio": "conversando",
                    "motivo": "pediu para ver o achado, respondido com o fato real"}

    sistema = _sistema(lead, diag, prop)
    saudacao = _so_saudacao(mensagem)
    if saudacao:
        sistema += """

ATENÇÃO NESTA MENSAGEM: a pessoa só cumprimentou, não perguntou nada. Responda
como gente responde a um "oi": cumprimente de volta, diga em meia linha quem é
você, e faça UMA pergunta leve. Duas linhas no máximo.
É PROIBIDO nesta resposta: citar posição no Google, nota, número de avaliações,
valor em R$, número de pacientes, preço, ou oferecer reunião. Quem responde um
"oi" com relatório assusta e perde a conversa."""

    msgs = [{"role": "system", "content": sistema}]
    for h in hist:
        msgs.append({"role": "assistant" if h["autor"] in ("ia", "humano") else "user",
                     "content": h["texto"]})

    try:
        import copy_ai
        texto = ia_hook._chamar_llm(msgs, temperatura=0.6, max_tokens=300)
        texto = copy_ai.limpar_travessao(texto).strip().strip('"')

        # O atendente tem registro próprio (WhatsApp é mais informal que a
        # cadência), mas não pode escapar da lista de clichês. Limite folgado de
        # propósito: aqui é conversa, e ser rígido demais engessa a resposta.
        pontos, achados = copy_ai.cheiro_de_ia(texto)

        # Duas perguntas na mesma mensagem fazem a pessoa não responder nenhuma.
        # A instrução "uma pergunta por vez" está no prompt e o modelo desobedece
        # com frequência, então a checagem é aqui, no determinístico.
        if texto.count("?") > 1:
            achados = achados + ["duas perguntas na mesma mensagem"]
            pontos = max(pontos, LIMITE_CHEIRO_IA + 1)

        # Responder um "oi" com posição, nota e R$ é o que o operador do sistema chamou
        # de desumano, e com razão. A instrução acima pede para não fazer; esta checagem
        # garante.
        if saudacao and _TEM_DADO.search(texto):
            achados = achados + ["despejou dado do diagnóstico num simples cumprimento"]
            pontos = max(pontos, LIMITE_CHEIRO_IA + 1)

        if pontos > LIMITE_CHEIRO_IA:
            msgs2 = msgs + [
                {"role": "assistant", "content": texto},
                {"role": "user", "content":
                 f"Problemas na sua resposta: {', '.join(achados[:4])}. "
                 f"Reescreva dizendo a mesma coisa com palavras suas e com NO "
                 f"MÁXIMO UMA pergunta. Responda só com a mensagem."}]
            try:
                nova = ia_hook._chamar_llm(msgs2, temperatura=0.6, max_tokens=300)
                nova = copy_ai.limpar_travessao(nova).strip().strip('"')
                melhorou = (nova and nova.count("?") <= 1
                            and copy_ai.cheiro_de_ia(nova)[0] <= copy_ai.cheiro_de_ia(texto)[0])
                if melhorou:
                    texto = nova
            except Exception:
                pass          # fica com a primeira: resposta boa vale mais que nenhuma
    except Exception as e:
        set_estado(numero, estagio="humano")
        return {"responder": False, "motivo": f"falha na IA: {e}", "estagio": "humano"}

    novo_estagio = "reuniao" if _REUNIAO.search(mensagem or "") else "conversando"
    set_estado(numero, estagio=novo_estagio)
    registrar(numero, "ia", texto, lead_id)
    return {"responder": True, "texto": texto, "estagio": novo_estagio,
            "lead": (lead or {}).get("nome"), "motivo": "resposta gerada"}


def conversas_ativas() -> List[Dict]:
    """Lista as conversas para acompanhamento no dashboard."""
    init()
    with db.conn() as con:
        rows = con.execute("""
            SELECT e.numero, e.estagio, e.pausado, e.atualizado_em, l.nome AS clinica,
                   (SELECT texto FROM conversas c WHERE c.numero=e.numero
                     ORDER BY c.id DESC LIMIT 1) AS ultima,
                   (SELECT COUNT(*) FROM conversas c WHERE c.numero=e.numero) AS total
            FROM conversa_estado e
            LEFT JOIN leads l ON l.id = e.lead_id
            ORDER BY e.atualizado_em DESC
        """).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    init()
    # simulação de conversa
    num = "82999620931"
    roteiro = ["quem é?", "quanto custa?", "e quanto tempo leva pra aparecer?",
               "pode ser amanhã de manhã"]
    print("=" * 60)
    print("SIMULAÇÃO DE ATENDIMENTO")
    print("=" * 60)
    for m in roteiro:
        print(f"\n👤 lead: {m}")
        r = responder(num, m)
        print(f"🤖 ia  : {r.get('texto') or '(sem resposta: ' + r.get('motivo','') + ')'}")
        print(f"        [estágio: {r.get('estagio')}]")
