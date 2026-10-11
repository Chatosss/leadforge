# -*- coding: utf-8 -*-
"""personalizar.py — cada mensagem de envio, sob medida para o lead."""

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

import config
import db

logger = logging.getLogger("personalizar")

CANAIS = ("whatsapp", "instagram", "email")

# ── As variáveis, com a explicação que a tela mostra ─────────────────────────
VARIAVEIS = [
    ("nome", "nome do lead, como está no Google Maps"),
    ("nome_curto", "o nome sem os sufixos (\"Clínica Sorriso\")"),
    ("artigo", "\"da\" ou \"do\", concordando com o nome"),
    ("artigo_em", "\"na\" ou \"no\", concordando com o nome"),
    ("cidade", "cidade do lead (hoje vazia na base: o Maps não a trouxe)"),
    ("regiao", "a região da busca que achou o lead (\"São Paulo\", \"Alagoas\")"),
    ("uf", "estado (sigla)"),
    ("categoria", "categoria no Google (\"Dentista\")"),
    ("nicho", "nicho interno (odontologia, estética...)"),
    ("servico", "o serviço que você oferece para esse nicho"),
    ("nota", "nota no Google (\"4,1\")"),
    ("avaliacoes", "quantas avaliações tem no Google"),
    ("posicao", "posição na busca do Maps"),
    ("site", "domínio do site (vazio se não tem)"),
    ("concorrente", "o concorrente apontado no diagnóstico"),
    ("nota_concorrente", "a nota desse concorrente"),
    ("problema", "o problema principal do diagnóstico"),
    ("corpo", "o texto inteiro do diagnóstico"),
    ("saudacao", "Bom dia, Boa tarde ou Boa noite, pela hora do envio"),
    ("atendente", "o nome do atendente (configuração)"),
]
# Na prospecção por seguidores do Instagram o "lead" é um perfil, não um
# cadastro do Maps: estas são as que existem ali.
VARIAVEIS_PERFIL = [
    ("nome", "primeiro nome do perfil (o mesmo {nome} de antes)"),
    ("nome_perfil", "o nome inteiro que aparece no perfil"),
    ("usuario", "o @ do perfil"),
    ("nicho", "o nicho da prospecção"),
    ("saudacao", "Bom dia, Boa tarde ou Boa noite, pela hora do envio"),
]

# Teto de tamanho por canal: mensagem de WhatsApp/Direct que vira redação não é
# lida, e a IA tende a escrever mais do que a base.
LIMITE = {"whatsapp": 1000, "instagram": 1000, "email": 3500}


# ══════════════════════════════════════════════════════════════════════════════
#  1. VARIÁVEIS
# ══════════════════════════════════════════════════════════════════════════════

def _saudacao(agora: Optional[datetime] = None) -> str:
    h = (agora or datetime.now()).hour
    return "Bom dia" if 5 <= h < 12 else ("Boa tarde" if 12 <= h < 18 else "Boa noite")


def _fmt_nota(v) -> str:
    try:
        return f"{float(v):.1f}".replace(".", ",")
    except (TypeError, ValueError):
        return ""


def _fmt_int(v) -> str:
    try:
        return str(int(float(v)))
    except (TypeError, ValueError):
        return ""


def _dominio(url: str) -> str:
    u = (url or "").strip().lower()
    u = re.sub(r"^[a-z]+://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.split("/")[0].split("?")[0]


_MINUSCULAS = {"de", "da", "do", "das", "dos", "e"}


def _titulo(texto: str) -> str:
    """"São paulo" → "São Paulo"; "rio de janeiro" → "Rio de Janeiro"."""
    palavras = (texto or "").split()
    return " ".join(p.lower() if i and p.lower() in _MINUSCULAS else p[:1].upper() + p[1:].lower()
                    for i, p in enumerate(palavras))


def _regiao(lead: Dict) -> str:
    """A região da BUSCA que achou o lead (scrap_queue.localizacao)."""
    sid = lead.get("scrap_id")
    if not sid:
        return ""
    try:
        with db.conn() as con:
            r = con.execute("SELECT localizacao FROM scrap_queue WHERE id=?", (sid,)).fetchone()
    except Exception:
        return ""
    return _titulo(((r[0] if r else "") or "").strip())


def variaveis(lead: Dict, agora: Optional[datetime] = None) -> Dict[str, str]:
    """Nome da variável (sem chaves) → valor para este lead. Vazio se não há dado."""
    import abordagem

    lead = dict(lead or {})
    nome_curto = abordagem._nome_curto(lead) if lead else ""
    art = abordagem.artigo_do_nome(nome_curto) if nome_curto else "da"
    diag = {}
    try:
        if lead.get("id"):
            diag = db.get_diagnostico(lead["id"]) or {}
    except Exception:
        diag = {}
    problema = ""
    try:
        probs = diag.get("problemas")
        probs = json.loads(probs) if isinstance(probs, str) else (probs or [])
        if probs:
            p0 = probs[0]
            problema = (p0.get("titulo") or p0.get("descricao") or "") \
                if isinstance(p0, dict) else str(p0)
    except (ValueError, TypeError):
        problema = ""
    servico = ""
    try:
        servico = abordagem._servico(lead) if lead else ""
    except Exception:
        servico = ""
    return {
        "nome": lead.get("nome") or "",
        "nome_curto": nome_curto,
        "artigo": art,
        "artigo_em": "no" if art == "do" else "na",
        "cidade": (lead.get("cidade") or "").split(",")[0].strip(),
        "regiao": _regiao(lead) if lead else "",
        "uf": (lead.get("uf") or "").strip(),
        "categoria": (lead.get("categoria") or "").strip(),
        "nicho": (lead.get("nicho") or "").strip(),
        "servico": servico or "",
        "nota": _fmt_nota(lead.get("avaliacao_num")),
        "avaliacoes": _fmt_int(lead.get("total_avaliacoes_num")),
        "posicao": _fmt_int(lead.get("posicao")),
        "site": _dominio(lead.get("website") or ""),
        "concorrente": (diag.get("concorrente_nome") or "").strip(),
        "nota_concorrente": _fmt_nota(diag.get("concorrente_nota")),
        "problema": problema.strip(),
        "corpo": diag.get("texto") or "",
        "saudacao": _saudacao(agora),
        "atendente": getattr(config, "ATENDENTE_NOME", "") or "",
    }


def variaveis_perfil(perfil: Dict, agora: Optional[datetime] = None) -> Dict[str, str]:
    """As variáveis de um perfil do Instagram na prospecção por seguidores."""
    perfil = dict(perfil or {})
    return {
        "nome": (perfil.get("nome") or "").strip(),
        "nome_perfil": (perfil.get("nome_perfil") or perfil.get("nome") or "").strip(),
        "usuario": "@" + str(perfil.get("usuario") or "").lstrip("@") if perfil.get("usuario") else "",
        "nicho": (perfil.get("nicho") or "").strip(),
        "saudacao": _saudacao(agora),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  2. FUNÇÕES DO TEMPLATE
# ══════════════════════════════════════════════════════════════════════════════

# {se nota:...} / {sem nota:...} — o miolo pode ter {variavel} e {a|b}.
_RX_COND = re.compile(r"\{(se|sem)\s+([a-z_]+)\s*:\s*((?:[^{}]|\{[^{}]*\})*)\}", re.I)
# {a|b|c} — as opções podem ter {variavel} dentro (nunca outra variação).
_RX_VAR = re.compile(r"\{((?:[^{}|]|\{[a-z_]+\})*(?:\|(?:[^{}|]|\{[a-z_]+\})*)+)\}")


def _condicionais(texto: str, valores: Dict[str, str]) -> str:
    def trocar(m):
        tipo, nome, miolo = m.group(1).lower(), m.group(2).lower(), m.group(3)
        tem = bool((valores.get(nome) or "").strip())
        return miolo if (tem if tipo == "se" else not tem) else ""
    return _RX_COND.sub(trocar, texto)


def _variacoes(texto: str, semente: str) -> str:
    cont = [0]

    def trocar(m):
        opcoes = m.group(1).split("|")
        n = cont[0]
        cont[0] += 1
        h = hashlib.sha1(f"{semente}|{n}|{m.group(1)}".encode("utf-8")).hexdigest()
        return opcoes[int(h, 16) % len(opcoes)]
    return _RX_VAR.sub(trocar, texto)


def _preencher(texto: str, valores: Dict[str, str]) -> str:
    for k, v in valores.items():
        texto = texto.replace("{" + k + "}", v or "")
    return texto


def _arrumar_espacos(texto: str) -> str:
    """Bloco ou variável vazia deixa espaço duplo e ',' — isso sai aqui."""
    texto = re.sub(r"[ \t]{2,}", " ", texto)
    texto = re.sub(r"[ \t]+([,.;:!?])", r"\1", texto)
    texto = re.sub(r"(?m)^[ \t]+|[ \t]+$", "", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def montar(corpo: str, valores: Dict[str, str], semente: str) -> str:
    """
    Condicionais → variações → variáveis → espaços. Nessa ordem, por quê: o bloco
    condicional decide com os dados; a variação escolhe entre opções que podem ter
    variável dentro; a variável vem por último, porque nome de empresa com "|" ("Natália
    | Tráfego Pago") quebraria a variação se entrasse antes.
    """
    texto = _condicionais(corpo or "", valores)
    texto = _variacoes(texto, semente)
    texto = _preencher(texto, valores)
    return _arrumar_espacos(texto)


def renderizar(lead: Dict, corpo: str, limpar: bool = True,
               agora: Optional[datetime] = None) -> str:
    """
    O texto do template para este lead. O ÚNICO renderizador de lead do projeto
    (`esteira_wa.renderizar` delega para cá).
    """
    import abordagem

    lead = dict(lead or {})
    texto = montar(corpo, variaveis(lead, agora), semente=str(lead.get("id") or lead.get("nome") or ""))
    if not limpar:
        return texto.strip()
    return abordagem._limpar_formatacao(texto).strip()


def renderizar_perfil(perfil: Dict, corpo: str, agora: Optional[datetime] = None) -> str:
    """O mesmo, para um perfil do Instagram (prospecção por seguidores)."""
    semente = str((perfil or {}).get("usuario") or "")
    return montar(corpo, variaveis_perfil(perfil, agora), semente=semente)


# ══════════════════════════════════════════════════════════════════════════════
# AJUSTES POR CANAL (liga/desliga + instrução do operador)
# ══════════════════════════════════════════════════════════════════════════════

def ativa(canal: str) -> bool:
    try:
        import dashboard_db as ddb
        return (ddb.get_config(f"perso_ia_{canal}") or "0") == "1"
    except Exception:
        return False


def instrucao(canal: str) -> str:
    try:
        import dashboard_db as ddb
        return ddb.get_config(f"perso_instrucao_{canal}") or ""
    except Exception:
        return ""


def salvar(canal: str, ligada: Optional[bool] = None, texto_instrucao: Optional[str] = None):
    if canal not in CANAIS:
        raise ValueError(f"canal desconhecido: {canal}")
    import dashboard_db as ddb
    if ligada is not None:
        ddb.set_config(f"perso_ia_{canal}", "1" if ligada else "0")
    if texto_instrucao is not None:
        ddb.set_config(f"perso_instrucao_{canal}", str(texto_instrucao).strip()[:1500])


def ajustes() -> Dict[str, Dict]:
    return {c: {"ativa": ativa(c), "instrucao": instrucao(c)} for c in CANAIS}


# ══════════════════════════════════════════════════════════════════════════════
#  3. IA — pedido, checagens, cache e registro
# ══════════════════════════════════════════════════════════════════════════════

_SISTEMA = """Você personaliza UMA mensagem de prospecção para UM destinatário, em português do Brasil.

Você recebe:
- MENSAGEM BASE: escrita pelo dono. É a voz dele, o objetivo e o pedido final.
- DADOS DO DESTINATÁRIO: só o que se sabe sobre ele. Campo ausente = não existe.
- INSTRUÇÃO DO DONO, quando houver.

Reescreva a mensagem base para esse destinatário:
1. Mantenha quem fala (nome, profissão), o objetivo e o pedido final da base.
2. Use 1 ou 2 dados concretos do destinatário, de forma natural.
3. NUNCA invente: nenhum número, nome, cidade, serviço, preço, prazo, desconto,
   estatística, link ou contato que não esteja na base ou nos dados. Nem elogio
   ou julgamento sobre o negócio que os dados não sustentem ("site bem
   estruturado", "ótimo trabalho"): muitas vezes a conversa é justamente sobre o
   que está ruim.
4. Tamanho parecido com o da base (no máximo um terço maior) e a MESMA divisão
   em parágrafos da base.
5. Sem markdown, sem aspas em volta, sem título, sem travessão longo (— ou –),
   no máximo um emoji. Não use colchetes nem chaves.
6. Não assine com dados de contato.
Responda SOMENTE com o texto final da mensagem."""

_ESTILO = {
    # "texto corrido" fazia a IA juntar os parágrafos do operador num bloco só
    "whatsapp": "É uma mensagem de WhatsApp: frases curtas, parágrafos como os da base.",
    "instagram": "É uma mensagem de Direct do Instagram: frases curtas, parágrafos como os da base.",
    "email": "É o corpo de um e-mail: parágrafos curtos. Não escreva o assunto.",
}

# rótulos humanos para o pedido; as chaves em snake_case são as que o
# `copy_ai.degenerado` procura no texto (vazamento do contexto)
_ROTULOS = {
    "nome": "nome", "cidade": "cidade", "uf": "estado", "categoria": "categoria no Google",
    "regiao_busca": "região onde foi encontrado (busca no Google Maps)",
    "nicho": "nicho", "nota_google": "nota no Google", "avaliacoes_google": "avaliações no Google",
    "posicao_maps": "posição na busca do Maps", "site": "site", "tem_site": "tem site",
    "concorrente": "concorrente", "nota_concorrente": "nota do concorrente",
    "problemas": "problemas encontrados", "usuario": "usuário no Instagram",
    "nome_perfil": "nome no perfil", "bio": "bio do perfil",
}


def _contexto_lead(lead: Dict) -> Dict[str, str]:
    v = variaveis(lead)
    diag = {}
    try:
        diag = db.get_diagnostico(lead.get("id")) or {} if lead.get("id") else {}
    except Exception:
        diag = {}
    probs = []
    try:
        p = diag.get("problemas")
        p = json.loads(p) if isinstance(p, str) else (p or [])
        probs = [str(x if not isinstance(x, dict) else (x.get("titulo") or "")) for x in p][:3]
    except (ValueError, TypeError):
        probs = []
    ctx = {
        "nome": v["nome_curto"] or v["nome"],
        "cidade": v["cidade"], "regiao_busca": v["regiao"], "uf": v["uf"],
        "categoria": v["categoria"],
        "nicho": v["nicho"], "nota_google": v["nota"], "avaliacoes_google": v["avaliacoes"],
        "posicao_maps": v["posicao"], "site": v["site"],
        "tem_site": "sim" if v["site"] else ("não" if lead.get("tem_site") in (0, "0", False) else ""),
        "concorrente": v["concorrente"], "nota_concorrente": v["nota_concorrente"],
        "problemas": "; ".join(x for x in probs if x),
    }
    return {k: val for k, val in ctx.items() if str(val or "").strip()}


def _contexto_perfil(perfil: Dict) -> Dict[str, str]:
    p = dict(perfil or {})
    bio = re.sub(r"\s+", " ", str(p.get("bio") or "")).strip()[:400]
    ctx = {"usuario": "@" + str(p.get("usuario") or "").lstrip("@") if p.get("usuario") else "",
           "nome": (p.get("nome") or "").strip(),
           "nome_perfil": (p.get("nome_perfil") or "").strip(),
           "nicho": (p.get("nicho") or "").strip(), "bio": bio}
    return {k: val for k, val in ctx.items() if val}


def _pedido(canal: str, base: str, contexto: Dict[str, str], instr: str) -> List[Dict]:
    dados = "\n".join(f"- {_ROTULOS.get(k, k)}: {v}" for k, v in contexto.items()) or "- (nenhum)"
    usuario = (f"{_ESTILO.get(canal, '')}\n\nMENSAGEM BASE:\n<<<\n{base}\n>>>\n\n"
               f"DADOS DO DESTINATÁRIO:\n{dados}\n\n"
               f"INSTRUÇÃO DO DONO: {instr.strip() or '(nenhuma)'}")
    return [{"role": "system", "content": _SISTEMA}, {"role": "user", "content": usuario}]


def _pos_processar(texto: str, canal: str) -> str:
    import abordagem
    import copy_ai

    t = (texto or "").strip()
    # aspas em volta da mensagem inteira
    if len(t) > 2 and t[0] in "\"'“«" and t[-1] in "\"'”»":
        t = t[1:-1].strip()
    # "Aqui está a mensagem:" / "Mensagem personalizada:" na primeira linha
    linhas = t.split("\n")
    if len(linhas) > 1 and linhas[0].rstrip().endswith(":") and len(linhas[0]) < 70 \
            and re.search(r"mensagem|texto|vers[ãa]o|aqui est", linhas[0], re.I):
        t = "\n".join(linhas[1:]).strip()
    t = copy_ai.limpar_travessao(t)
    if canal != "email":
        t = abordagem._limpar_formatacao(t)
    return t.strip()


_RX_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_RX_CONTATO = re.compile(
    r"https?://\S+|www\.\S+|[\w.+-]+@[\w-]+\.[\w.]+|(?<![\w@])@[\w.]{3,}"
    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|br|net|org|io|app|site)\b", re.I)
_TERMINA_BEM = re.compile(r"[.!?…)\]\"'»”]\s*$|[\U0001F300-\U0001FAFF☀-➿]\s*$")


def _formas_do_numero(n: str) -> set:
    return {n, n.replace(",", "."), n.replace(".", "").replace(",", "."),
            re.sub(r"\D", "", n)}


def checar(texto: str, base: str, contexto: Dict[str, str], canal: str) -> List[str]:
    """Motivos para REPROVAR o texto da IA. Lista vazia = aprovado."""
    import copy_ai

    motivos = []
    t = (texto or "").strip()
    b = (base or "").strip()
    if not t:
        return ["a IA devolveu texto vazio"]
    # tamanho
    if len(t) < max(40, int(0.4 * len(b))):
        motivos.append(f"curto demais ({len(t)} caracteres, base {len(b)})")
    if len(t) > max(int(1.6 * len(b)) + 80, 160) or len(t) > LIMITE.get(canal, 1000):
        motivos.append(f"longo demais ({len(t)} caracteres, base {len(b)})")
    # cortado no meio: a base termina com pontuação e a IA não
    if _TERMINA_BEM.search(b) and not _TERMINA_BEM.search(t):
        motivos.append("parece cortado (não termina a frase)")
    # sobra de template
    if re.search(r"\{[^{}\n]{1,40}\}|\[[^\]\n]{2,30}\]", t):
        motivos.append("sobrou placeholder ({...} ou [...])")
    if canal != "email" and re.search(r"\*\*|__|```|^\s*#", t, re.M):
        motivos.append("formatação markdown")
    # número que não está na base nem nos dados
    fonte = b + " " + " ".join(str(v) for v in contexto.values())
    permitidos = set()
    for n in _RX_NUM.findall(fonte):
        permitidos |= _formas_do_numero(n)
    novos = [n for n in _RX_NUM.findall(t) if not (_formas_do_numero(n) & permitidos)]
    if novos:
        motivos.append("número que não está nos dados: " + ", ".join(sorted(set(novos))[:4]))
    # link, e-mail ou @ que não estava em lugar nenhum
    fonte_baixa = fonte.lower()
    inventados = [c for c in _RX_CONTATO.findall(t) if c.lower().rstrip(".,!?") not in fonte_baixa]
    if inventados:
        motivos.append("link/contato inventado: " + ", ".join(inventados[:3]))
    # oferta, estatística, horário, placeholder — só conta se NÃO estava na base (o
    # operador pode oferecer "análise gratuita"; a IA não pode inventar desconto).
    b_baixo = b.lower()
    for rx, _peso, rotulo in copy_ai._ALUC:
        novos_trechos = [m.group(0) for m in rx.finditer(t) if m.group(0).lower() not in b_baixo]
        if novos_trechos:
            motivos.append(f"{rotulo}: {novos_trechos[0]}")
    motivos += copy_ai.degenerado(t, contexto)
    p_t, a_t = copy_ai.cheiro_de_ia(t)
    p_b, _ = copy_ai.cheiro_de_ia(b)
    if p_t - p_b > 4:
        motivos.append(f"cara de texto de IA (+{p_t - p_b}: {', '.join(a_t[:3])})")
    return motivos


def _init():
    with db.conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS mensagens_personalizadas (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                chave     TEXT NOT NULL UNIQUE,
                canal     TEXT NOT NULL,
                lead_id   INTEGER,
                alvo      TEXT DEFAULT '',
                etapa     TEXT DEFAULT '',
                base      TEXT NOT NULL,
                texto     TEXT NOT NULL,
                modelo    TEXT DEFAULT '',
                criado_em TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS personalizacao_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                canal     TEXT NOT NULL,
                alvo      TEXT DEFAULT '',
                resultado TEXT NOT NULL,
                motivo    TEXT DEFAULT '',
                modelo    TEXT DEFAULT '',
                criado_em TEXT DEFAULT (datetime('now','localtime'))
            );
        """)


def _chave(canal: str, alvo: str, etapa: str, base: str, instr: str) -> str:
    return hashlib.sha1(f"{canal}|{alvo}|{etapa}|{base}|{instr}".encode("utf-8")).hexdigest()


def _do_cache(chave: str) -> Optional[Dict]:
    try:
        _init()
        with db.conn() as con:
            r = con.execute("SELECT texto, modelo FROM mensagens_personalizadas WHERE chave=?",
                            (chave,)).fetchone()
        return {"texto": r["texto"], "modelo": r["modelo"]} if r else None
    except Exception:
        return None


def _guardar(chave, canal, lead_id, alvo, etapa, base, texto, modelo):
    try:
        _init()
        with db.conn() as con:
            con.execute("INSERT OR REPLACE INTO mensagens_personalizadas"
                        "(chave,canal,lead_id,alvo,etapa,base,texto,modelo) VALUES(?,?,?,?,?,?,?,?)",
                        (chave, canal, lead_id, alvo, etapa, base, texto, modelo))
    except Exception as e:
        logger.warning("[IA] não deu para guardar a mensagem personalizada: %s", e)


def _registrar(canal: str, alvo: str, resultado: str, motivo: str = "", modelo: str = ""):
    try:
        _init()
        with db.conn() as con:
            con.execute("INSERT INTO personalizacao_log(canal,alvo,resultado,motivo,modelo) "
                        "VALUES(?,?,?,?,?)", (canal, alvo[:120], resultado, motivo[:500], modelo))
            con.execute("DELETE FROM personalizacao_log WHERE id NOT IN "
                        "(SELECT id FROM personalizacao_log ORDER BY id DESC LIMIT 500)")
    except Exception:
        pass
    if resultado != "personalizada":
        logger.warning("[IA] %s %s saiu SEM personalização: %s", canal, alvo, motivo)


def ultimas(canal: str, n: int = 10) -> List[Dict]:
    try:
        _init()
        with db.conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT alvo, resultado, motivo, modelo, criado_em FROM personalizacao_log "
                "WHERE canal=? ORDER BY id DESC LIMIT ?", (canal, n))]
    except Exception:
        return []


def _personalizar(canal: str, alvo: str, lead_id, etapa: str, base: str,
                  contexto: Dict[str, str], usar_cache: bool, forcar: bool) -> Dict:
    """O miolo comum a lead e perfil. Nunca levanta exceção."""
    registrar = (lambda *a, **k: None) if forcar else _registrar
    saida = {"texto": base, "personalizado": False, "motivo": "", "modelo": "",
             "base": base, "tentativas": []}
    if not (base or "").strip():
        saida["motivo"] = "texto base vazio"
        return saida
    if not forcar and not ativa(canal):
        saida["motivo"] = "personalização desligada neste canal"
        return saida
    instr = instrucao(canal)
    chave = _chave(canal, alvo, etapa, base, instr)
    if usar_cache:
        c = _do_cache(chave)
        if c:
            saida.update({"texto": c["texto"], "personalizado": True,
                          "modelo": c["modelo"], "motivo": "reaproveitada (já aprovada)"})
            return saida
    try:
        import ia_roteador
        r = ia_roteador.gerar(_pedido(canal, base, contexto, instr),
                              max_tokens=1400 if canal == "email" else 700)
    except Exception as e:
        r = {"ok": False, "motivo": f"erro ao chamar a IA ({type(e).__name__})", "tentativas": []}
    saida["tentativas"] = r.get("tentativas") or []
    if not r.get("ok"):
        saida["motivo"] = r.get("motivo") or "a IA não respondeu"
        registrar(canal, alvo, "template", saida["motivo"])
        return saida
    # Checagem que quebra REPROVA — nunca aprova e nunca derruba quem chamou. Um
    # erro de regex aqui já escapou uma vez e virava 500 na prévia.
    try:
        texto = _pos_processar(r["texto"], canal)
        motivos = checar(texto, base, contexto, canal)
    except Exception as e:
        texto = r.get("texto") or ""
        motivos = [f"erro na checagem ({type(e).__name__}: {e})"]
    if motivos:
        saida["motivo"] = "reprovada: " + "; ".join(motivos)
        saida["reprovada"] = texto
        saida["modelo"] = r.get("modelo", "")
        registrar(canal, alvo, "template", saida["motivo"], saida["modelo"])
        return saida
    if usar_cache:
        _guardar(chave, canal, lead_id, alvo, etapa, base, texto, r.get("modelo", ""))
    saida.update({"texto": texto, "personalizado": True, "modelo": r.get("modelo", ""),
                  "motivo": ""})
    registrar(canal, alvo, "personalizada", "", saida["modelo"])
    return saida


def personalizar(lead: Dict, canal: str, texto_base: str, etapa: str = "",
                 usar_cache: bool = True, forcar: bool = False) -> Dict:
    """{texto, personalizado, motivo, modelo, base, tentativas[, reprovada]}."""
    lead = dict(lead or {})
    alvo = f"lead {lead.get('id')} ({(lead.get('nome') or '')[:40]})"
    try:
        ctx = _contexto_lead(lead)
    except Exception:
        ctx = {}
    return _personalizar(canal, alvo, lead.get("id"), etapa or "", texto_base or "",
                         ctx, usar_cache, forcar)


def aplicar(lead: Dict, canal: str, texto_base: str, etapa: str = "") -> str:
    """Só o texto final, para os pontos de envio. Nunca levanta exceção."""
    try:
        return personalizar(lead, canal, texto_base, etapa)["texto"]
    except Exception as e:
        logger.warning("[IA] personalização falhou (%s); saiu o template", e)
        return texto_base


def texto_do_perfil(template: str, perfil: Dict, forcar: bool = False,
                    usar_cache: bool = True) -> Dict:
    """A mensagem da prospecção por seguidores do Instagram, para UM perfil."""
    base = renderizar_perfil(perfil, template)
    alvo = "@" + str((perfil or {}).get("usuario") or "").lstrip("@")
    try:
        ctx = _contexto_perfil(perfil)
    except Exception:
        ctx = {}
    return _personalizar("instagram", alvo, None, "prospeccao", base, ctx,
                         usar_cache=usar_cache, forcar=forcar)
