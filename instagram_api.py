"""instagram_api.py — o Instagram no chat ao vivo pela API OFICIAL da Meta (v023)."""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import chat_service
import db

logger = logging.getLogger("instagram_api")

RAIZ = Path(__file__).resolve().parent
GRAPH = "https://graph.instagram.com"
LIMITE_BYTES = 1000              # texto por mensagem, regra da Meta
JANELA = timedelta(hours=24)     # janela padrão de resposta
RENOVAR_APOS = timedelta(days=7)  # token dura 60 dias; renova com folga
CAMPOS_WEBHOOK = ("messages", "message_echoes")


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURAÇÃO
# ══════════════════════════════════════════════════════════════════════════════

def _ler_env():
    """O .env é relido a cada consulta: mudar a chave não exige reiniciar."""
    vals = {}
    try:
        for linha in (RAIZ / ".env").read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            k, v = linha.split("=", 1)
            vals[k.strip()] = v.strip()
    except OSError:
        pass
    return vals


def cfg(nome, padrao=""):
    return os.environ.get(nome) or _ler_env().get(nome) or padrao


def versao():
    v = cfg("IG_API_VERSAO", "v26.0")
    return v if re.fullmatch(r"v\d{1,3}\.\d", v) else "v26.0"


def _agora():
    return datetime.now(timezone.utc).astimezone()


def _iso(d):
    return d.isoformat(timespec="seconds") if d else None


def _data(texto):
    try:
        d = datetime.fromisoformat(str(texto))
        return d if d.tzinfo else d.astimezone()
    except (TypeError, ValueError):
        return None


def _limpo(texto):
    """Mensagem de erro sem token nem segredo (exceção de rede traz a URL)."""
    t = re.sub(r"(access_token|client_secret)=[^&\s'\"]+", r"\1=***", str(texto))
    return t[:300]


# TOKEN GUARDADO CIFRADO (DPAPI do Windows) O token lê e envia DM pela conta por
# 60 dias.

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    def _dpapi(dados: bytes, cifrar: bool) -> bytes:
        buf = ctypes.create_string_buffer(dados, len(dados))
        entrada = _Blob(len(dados), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        saida = _Blob()
        funcao = _crypt32.CryptProtectData if cifrar else _crypt32.CryptUnprotectData
        # 0x01 = CRYPTPROTECT_UI_FORBIDDEN: nunca abrir janela pedindo nada
        if not funcao(ctypes.byref(entrada), None, None, None, None, 0x01, ctypes.byref(saida)):
            raise OSError("DPAPI recusou a operação")
        try:
            return ctypes.string_at(saida.pbData, saida.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(saida.pbData, ctypes.c_void_p))


def _guardar(token: str) -> str:
    if os.name == "nt":
        return "dpapi:" + base64.b64encode(_dpapi(token.encode("utf-8"), True)).decode("ascii")
    logger.warning("sem DPAPI neste sistema: o token do Instagram fica legível no banco")
    return "plain:" + token


def _abrir(valor: str) -> str:
    if valor.startswith("dpapi:"):
        return _dpapi(base64.b64decode(valor[6:]), False).decode("utf-8")
    if valor.startswith("plain:"):
        return valor[6:]
    raise ValueError("token guardado em formato desconhecido")


# ══════════════════════════════════════════════════════════════════════════════
#  BANCO
# ══════════════════════════════════════════════════════════════════════════════

_INIT = threading.Lock()
_PRONTO = False


def init():
    global _PRONTO
    if _PRONTO:
        return
    with _INIT:
        if _PRONTO:
            return
        with db.conn() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS ig_api_contas (
                ig_id TEXT PRIMARY KEY,           -- user_id da conta profissional (= entry.id do webhook)
                username TEXT NOT NULL,
                nome TEXT NOT NULL DEFAULT '',
                tipo_conta TEXT NOT NULL DEFAULT '',
                token TEXT NOT NULL,              -- cifrado (ver _guardar)
                token_renovado_em TEXT NOT NULL,
                token_expira_em TEXT,             -- NULL = desconhecida até a 1ª renovação
                inscrito_em TEXT,                 -- subscribed_apps confirmado
                ultimo_erro TEXT NOT NULL DEFAULT '',
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ig_api_contas_user ON ig_api_contas(username);
            """)
        _PRONTO = True


def _linhas():
    init()
    with db.conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM ig_api_contas ORDER BY username").fetchall()]


def _linha(ig_id=None, username=None):
    init()
    with db.conn() as con:
        if ig_id is not None:
            r = con.execute("SELECT * FROM ig_api_contas WHERE ig_id=?", (str(ig_id),)).fetchone()
        else:
            r = con.execute("SELECT * FROM ig_api_contas WHERE username=?",
                            (str(username or "").lower().lstrip("@"),)).fetchone()
    return dict(r) if r else None


def _anotar(ig_id, **campos):
    campos["atualizado_em"] = _iso(_agora())
    sets = ", ".join(f"{k}=?" for k in campos)
    with db.conn() as con:
        con.execute(f"UPDATE ig_api_contas SET {sets} WHERE ig_id=?",
                    (*campos.values(), str(ig_id)))


def _token(linha):
    try:
        return _abrir(linha["token"])
    except Exception as e:
        raise ErroAPI(f"não consegui abrir o token guardado de @{linha['username']} "
                      f"({type(e).__name__}); cole um token novo") from None


# ══════════════════════════════════════════════════════════════════════════════
#  CHAMADAS À GRAPH API
# ══════════════════════════════════════════════════════════════════════════════

class ErroAPI(Exception):
    def __init__(self, mensagem, codigo=None, sub=None, http=None):
        super().__init__(mensagem)
        self.codigo, self.sub, self.http = codigo, sub, http

    @property
    def token_ruim(self):
        return self.codigo == 190


def _amigavel(codigo, sub, texto_meta):
    if codigo == 190:
        return ("O token desta conta venceu ou foi revogado. Gere outro no Painel de "
                "Apps da Meta e cole de novo em 'Instagram pela API oficial'.")
    if codigo == 10 and sub == 2534022:
        return ("Passou de 24h desde a última mensagem do lead: a Meta não aceita "
                "resposta pela API. Responda pelo app do Instagram.")
    if codigo == 551:
        return "Este perfil não pode receber mensagens agora (bloqueou a conta ou saiu do Instagram)."
    if codigo in (4, 17, 32, 613):
        return "A Meta limitou as chamadas desta conta por alguns minutos. Tente de novo em instantes."
    if codigo in (3, 10, 200):
        return (f"A Meta recusou por permissão: {texto_meta}. Confira se o app tem "
                "instagram_business_manage_messages e se esta conta autorizou o app.")
    return f"A Meta recusou: {texto_meta} (código {codigo})"


def _chamar(metodo, caminho, token, params=None, corpo=None, com_versao=True, prazo=20):
    """Uma chamada à graph.instagram.com. Erro vira ErroAPI com texto para gente."""
    base = f"{GRAPH}/{versao()}" if com_versao else GRAPH
    url = f"{base}/{caminho.lstrip('/')}"
    params = dict(params or {})
    params["access_token"] = token          # como na documentação de cada ponto
    try:
        r = requests.request(metodo, url, params=params, json=corpo, timeout=prazo)
    except requests.RequestException as e:
        # `from None`: a exceção original carrega a URL com o token
        raise ErroAPI(f"sem resposta da Meta ({type(e).__name__})") from None
    try:
        dados = r.json()
    except ValueError:
        raise ErroAPI(f"a Meta respondeu algo que não é JSON (HTTP {r.status_code})",
                      http=r.status_code) from None
    erro = dados.get("error") if isinstance(dados, dict) else None
    if r.status_code >= 400 or erro:
        erro = erro if isinstance(erro, dict) else {}
        codigo, sub = erro.get("code"), erro.get("error_subcode")
        texto = _limpo(erro.get("message") or erro.get("error_user_msg") or f"HTTP {r.status_code}")
        raise ErroAPI(_amigavel(codigo, sub, texto), codigo, sub, r.status_code)
    return dados


# ══════════════════════════════════════════════════════════════════════════════
#  CONTAS (token colado pelo operador)
# ══════════════════════════════════════════════════════════════════════════════

def _resumo(linha):
    """O que a tela pode ver de uma conta. O token NUNCA sai daqui."""
    expira = _data(linha.get("token_expira_em"))
    return {
        "ig_id": linha["ig_id"], "username": linha["username"], "nome": linha["nome"],
        "tipo_conta": linha["tipo_conta"],
        "token_renovado_em": linha["token_renovado_em"],
        "token_expira_em": linha.get("token_expira_em"),
        "dias_restantes": (expira - _agora()).days if expira else None,
        "inscrito": bool(linha.get("inscrito_em")),
        "ultimo_erro": linha.get("ultimo_erro") or "",
    }


def contas():
    return [_resumo(l) for l in _linhas()]


def contas_ativas():
    """username -> ig_id das contas com token que não está marcado como ruim."""
    return {l["username"]: l["ig_id"] for l in _linhas()
            if not (l.get("ultimo_erro") or "").startswith("token:")}


def conectar(token_colado):
    """Confere o token na Meta, guarda cifrado e inscreve a conta no webhook."""
    token = str(token_colado or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._\-|]{20,2048}", token):
        raise ValueError("Isso não parece um token de acesso do Instagram. Copie o "
                         "token inteiro gerado no Painel de Apps da Meta.")
    eu = _chamar("GET", "me", token, {"fields": "user_id,username,account_type,name"})
    if isinstance(eu.get("data"), list) and eu["data"]:
        eu = eu["data"][0]                    # formato do exemplo da documentação
    ig_id = str(eu.get("user_id") or "").strip()
    username = str(eu.get("username") or "").strip().lower()
    if not ig_id.isdigit() or not username:
        raise ErroAPI("A Meta não devolveu o ID da conta profissional. O token é da "
                      "'API do Instagram com Login do Instagram'?")
    agora = _iso(_agora())
    init()
    with db.conn() as con:
        con.execute("""INSERT INTO ig_api_contas
            (ig_id,username,nome,tipo_conta,token,token_renovado_em,token_expira_em,
             inscrito_em,ultimo_erro,criado_em,atualizado_em)
            VALUES (?,?,?,?,?,?,NULL,NULL,'',?,?)
            ON CONFLICT(ig_id) DO UPDATE SET
              username=excluded.username, nome=excluded.nome, tipo_conta=excluded.tipo_conta,
              token=excluded.token, token_renovado_em=excluded.token_renovado_em,
              token_expira_em=NULL, ultimo_erro='', atualizado_em=excluded.atualizado_em""",
            (ig_id, username, str(eu.get("name") or "")[:200],
             str(eu.get("account_type") or "")[:40], _guardar(token), agora, agora, agora))
    # Token com mais de 24h já pode ser renovado: a renovação devolve a validade
    # exata. Token recém-gerado recusa — fica o original, renovado em 7 dias.
    try:
        renovar(ig_id, forcar=True)
    except ErroAPI:
        pass
    inscrever(ig_id)
    return _resumo(_linha(ig_id))


def remover(ig_id):
    init()
    with db.conn() as con:
        return con.execute("DELETE FROM ig_api_contas WHERE ig_id=?", (str(ig_id),)).rowcount


def inscrever(ig_id):
    """Liga as notificações de mensagem desta conta para o app (subscribed_apps)."""
    linha = _linha(ig_id)
    if not linha:
        return False
    tok = _token(linha)
    erro = ""
    for campos in (",".join(CAMPOS_WEBHOOK), "messages"):
        try:
            r = _chamar("POST", f"{ig_id}/subscribed_apps", tok, {"subscribed_fields": campos})
            if r.get("success"):
                _anotar(ig_id, inscrito_em=_iso(_agora()), ultimo_erro="")
                return True
            erro = "a Meta não confirmou a inscrição"
        except ErroAPI as e:
            erro = str(e)
            if e.token_ruim:
                break
    _anotar(ig_id, inscrito_em=None, ultimo_erro=f"inscrição: {erro}"[:300])
    return False


def renovar(ig_id, forcar=False):
    """Renova o token por mais 60 dias (a Meta exige token com 24h ou mais)."""
    linha = _linha(ig_id)
    if not linha:
        return False
    ultima = _data(linha["token_renovado_em"])
    if not forcar and ultima and _agora() - ultima < RENOVAR_APOS:
        return False
    try:
        r = _chamar("GET", "refresh_access_token", _token(linha),
                    {"grant_type": "ig_refresh_token"}, com_versao=False)
    except ErroAPI as e:
        if e.token_ruim:
            _anotar(ig_id, ultimo_erro=f"token: {e}"[:300])
        raise
    novo = str(r.get("access_token") or "")
    if not novo:
        raise ErroAPI("a Meta não devolveu o token renovado")
    expira = _agora() + timedelta(seconds=int(r.get("expires_in") or 0))
    _anotar(ig_id, token=_guardar(novo), token_renovado_em=_iso(_agora()),
            token_expira_em=_iso(expira) if r.get("expires_in") else None, ultimo_erro="")
    return True


def renovar_todos():
    feitos = 0
    for l in _linhas():
        try:
            feitos += 1 if renovar(l["ig_id"]) else 0
        except ErroAPI as e:
            logger.warning("renovação do token de @%s falhou: %s", l["username"], e)
    return feitos


# ══════════════════════════════════════════════════════════════════════════════
#  ENVIO
# ══════════════════════════════════════════════════════════════════════════════

def janela(conversa):
    """(aberta, fecha_em ISO) a partir da última mensagem RECEBIDA da conversa."""
    ultima = _data(conversa.get("last_inbound_at"))
    if not ultima:
        return False, None
    fecha = ultima + JANELA
    return _agora() < fecha, _iso(fecha)


def enviar_texto(conta, igsid, texto):
    """Envia pela conta da conversa. Devolve o message_id da Meta."""
    linha = _linha(username=conta)
    if not linha:
        raise ErroAPI(f"@{conta} não está conectada à API oficial: cole o token dela "
                      "em 'Instagram pela API oficial'.")
    if len(texto.encode("utf-8")) > LIMITE_BYTES:
        raise ValueError(f"O Instagram aceita até {LIMITE_BYTES} bytes por mensagem "
                         f"(esta tem {len(texto.encode('utf-8'))}). Divida em duas.")
    try:
        r = _chamar("POST", f"{linha['ig_id']}/messages", _token(linha),
                    corpo={"recipient": {"id": str(igsid)}, "message": {"text": texto}})
    except ErroAPI as e:
        if e.token_ruim:
            _anotar(linha["ig_id"], ultimo_erro=f"token: {e}"[:300])
        raise
    return r.get("message_id")


# ══════════════════════════════════════════════════════════════════════════════
#  RECEBIMENTO (corpo do webhook, já com a assinatura conferida)
# ══════════════════════════════════════════════════════════════════════════════

_ROTULOS = {
    "image": "imagem", "video": "vídeo", "audio": "áudio", "file": "arquivo",
    "share": "post compartilhado", "ig_post": "post compartilhado",
    "ig_reel": "reel compartilhado", "reel": "reel compartilhado",
    "story_mention": "mencionou você no story", "story": "story", "ig_story": "story",
    "media": "mídia", "like_heart": "❤️", "sticker": "figurinha",
}


def _texto(msg):
    partes = []
    if ((msg.get("reply_to") or {}).get("story")):
        partes.append("[respondeu ao seu story]")
    for a in msg.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        tipo = str(a.get("type") or "")
        url = str(((a.get("payload") or {}).get("url")) or "")
        partes.append(f"[{_ROTULOS.get(tipo, tipo or 'anexo')}]" + (f" {url}" if url else ""))
    if msg.get("is_unsupported"):
        partes.append("[conteúdo que a API não mostra — veja no app do Instagram]")
    texto = str(msg.get("text") or "").strip()
    if texto:
        partes.append(texto)
    return "\n".join(partes).strip()


def _nome_do_contato(linha, igsid):
    """@username do contato pela API de Perfil (só vale depois que ele escreveu)."""
    try:
        p = _chamar("GET", str(igsid), _token(linha), {"fields": "name,username"})
    except ErroAPI as e:
        logger.info("perfil do contato %s indisponível: %s", igsid, e)
        return ""
    u = str(p.get("username") or "").strip()
    return f"@{u}" if u else str(p.get("name") or "").strip()


def _epoch(valor):
    try:
        n = float(valor)
        return n / 1000.0 if n > 1e11 else n
    except (TypeError, ValueError):
        d = _data(str(valor or "").replace("Z", "+00:00"))
        if d is None and valor:
            try:
                d = datetime.strptime(str(valor), "%Y-%m-%dT%H:%M:%S%z")
            except ValueError:
                d = None
        return d.timestamp() if d else None


def _historico_anterior(linha, igsid, antes_de, limite=6):
    """
    As mensagens ANTERIORES à primeira que chegou por webhook — em geral a abordagem da
    prospecção que o lead está respondendo. Sem isso a conversa abre com "quanto custa?"
    e o operador não sabe do que se fala.
    """
    tok = _token(linha)
    conv = _chamar("GET", "me/conversations", tok,
                   {"platform": "instagram", "user_id": str(igsid)})
    lista = conv.get("data") or []
    if not lista:
        return 0
    msgs = _chamar("GET", str(lista[0].get("id")), tok, {"fields": "messages"})
    ids = [m.get("id") for m in ((msgs.get("messages") or {}).get("data") or []) if m.get("id")]
    gravadas = 0
    for mid in ids[:limite]:
        time.sleep(0.6)                      # a API de conversas aceita 2 chamadas/s
        try:
            m = _chamar("GET", str(mid), tok, {"fields": "id,created_time,from,to,message"})
        except ErroAPI:
            continue
        quando = _epoch(m.get("created_time"))
        if quando is None or antes_de is None or quando >= antes_de:
            continue
        de = str((m.get("from") or {}).get("id") or "")
        saiu = de == str(linha["ig_id"])
        texto = str(m.get("message") or "").strip() or "[mídia]"
        r = chat_service.registrar("instagram", linha["username"], igsid, texto,
                                   provider_id=m.get("id") or mid,
                                   direction="out" if saiu else "in",
                                   delivery_state="sent" if saiu else "received",
                                   quando=quando, conta_nao_lida=False)
        gravadas += 1 if r and r["nova"] else 0
    return gravadas


def _evento(linha, ev):
    ig_id, conta = str(linha["ig_id"]), linha["username"]
    edicao = ev.get("message_edit")
    if isinstance(edicao, dict) and edicao.get("mid") and edicao.get("text"):
        return chat_service.trocar_texto("instagram", conta, edicao["mid"],
                                         f"{edicao['text']} (editada)")
    msg = ev.get("message")
    if not isinstance(msg, dict) or msg.get("is_self"):
        return 0
    eco = bool(msg.get("is_echo"))
    peer = str(((ev.get("recipient") if eco else ev.get("sender")) or {}).get("id") or "")
    if not peer or peer == ig_id:
        return 0
    mid = msg.get("mid")
    if msg.get("is_deleted"):
        return chat_service.trocar_texto("instagram", conta, mid, "🗑 Mensagem apagada pelo contato.")
    texto = _texto(msg)
    if not texto:
        return 0
    nome = ""
    if not eco and not chat_service.nome_da_conversa("instagram", conta, peer):
        nome = _nome_do_contato(linha, peer)
    r = chat_service.registrar("instagram", conta, peer, texto, display_name=nome,
                               provider_id=mid, direction="out" if eco else "in",
                               delivery_state="sent" if eco else "received",
                               quando=ev.get("timestamp"))
    if r and r["primeira_entrada"]:
        try:
            _historico_anterior(linha, peer, _epoch(ev.get("timestamp")))
        except ErroAPI as e:
            logger.info("histórico anterior de %s em @%s indisponível: %s", peer, conta, e)
    return 1 if r and r["nova"] else 0


def processar(corpo: bytes) -> int:
    """Grava as mensagens de UM corpo de webhook. Devolve quantas eram novas."""
    dados = json.loads(corpo.decode("utf-8"))
    if not isinstance(dados, dict) or dados.get("object") != "instagram":
        return 0
    por_id = {l["ig_id"]: l for l in _linhas()}
    novas = 0
    for entrada in dados.get("entry") or []:
        linha = por_id.get(str((entrada or {}).get("id") or ""))
        if not linha:
            logger.warning("webhook de conta não conectada (%s): ignorado", (entrada or {}).get("id"))
            continue
        for ev in entrada.get("messaging") or []:
            if isinstance(ev, dict):
                novas += _evento(linha, ev)
    return novas


def assinatura_ok(corpo: bytes, assinatura: str, segredo: str) -> bool:
    esperado = "sha256=" + hmac.new(segredo.encode("utf-8"), corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, str(assinatura or ""))


# ══════════════════════════════════════════════════════════════════════════════
#  RELÉ: puxar a fila e confirmar
# ══════════════════════════════════════════════════════════════════════════════

ESTADO = {"ultimo_ok": None, "ultimo_erro": "", "eventos": 0, "mensagens": 0,
          "descartados": 0, "restantes": 0}
_FALHAS = {}           # id do evento -> tentativas que deram erro ao gravar
_PUXANDO = threading.Lock()


def configurado():
    return {"app_secret": bool(cfg("IG_API_APP_SECRET")),
            "rele_url": bool(cfg("IG_RELE_URL")),
            "rele_token": bool(cfg("IG_RELE_TOKEN"))}


def _rele(acao, extra):
    url = cfg("IG_RELE_URL")
    if not url.startswith("https://"):
        raise ErroAPI("IG_RELE_URL precisa começar com https://")
    try:
        r = requests.post(url, params={"acao": acao},
                          json={"token": cfg("IG_RELE_TOKEN"), **extra}, timeout=20)
    except requests.RequestException as e:
        raise ErroAPI(f"relé fora do ar ({type(e).__name__})") from None
    try:
        dados = r.json()
    except ValueError:
        raise ErroAPI(f"o relé respondeu algo que não é JSON (HTTP {r.status_code})") from None
    if r.status_code == 403:
        raise ErroAPI("o relé recusou o IG_RELE_TOKEN deste painel")
    if not r.ok or not dados.get("ok"):
        raise ErroAPI(f"o relé respondeu HTTP {r.status_code}: {dados.get('erro', '')}"[:200])
    return dados


def puxar():
    """Uma rodada: puxa até 50 notificações, grava e confirma as resolvidas."""
    if not all(configurado().values()):
        return {"ok": False, "configurado": False}
    if not _PUXANDO.acquire(blocking=False):
        return {"ok": True, "ocupado": True}
    try:
        segredo = cfg("IG_API_APP_SECRET")
        dados = _rele("puxar", {"max": 50})
        resolvidos, novas = [], 0
        for ev in dados.get("eventos") or []:
            ev_id = str(ev.get("id") or "")
            try:
                corpo = base64.b64decode(str(ev.get("corpo_b64") or ""), validate=True)
            except ValueError:
                corpo = b""
            if not corpo or not assinatura_ok(corpo, ev.get("assinatura"), segredo):
                # o relé já confere; chegar aqui é relé com segredo diferente ou
                # fila adulterada. Não grava, e tira da fila para não travar.
                logger.error("notificação %s com assinatura inválida: descartada", ev_id)
                ESTADO["descartados"] += 1
                resolvidos.append(ev_id)
                continue
            try:
                novas += processar(corpo)
                resolvidos.append(ev_id)
                _FALHAS.pop(ev_id, None)
            except Exception as e:
                _FALHAS[ev_id] = _FALHAS.get(ev_id, 0) + 1
                logger.error("notificação %s não gravou (%s): %s", ev_id,
                             _FALHAS[ev_id], _limpo(e))
                if _FALHAS[ev_id] >= 5:      # não pode travar a fila para sempre
                    ESTADO["descartados"] += 1
                    resolvidos.append(ev_id)
                    _FALHAS.pop(ev_id, None)
        if resolvidos:
            _rele("confirmar", {"ids": resolvidos})
        ESTADO.update(ultimo_ok=_iso(_agora()), ultimo_erro="",
                      eventos=ESTADO["eventos"] + len(dados.get("eventos") or []),
                      mensagens=ESTADO["mensagens"] + novas,
                      restantes=int(dados.get("restantes") or 0))
        return {"ok": True, "eventos": len(dados.get("eventos") or []), "novas": novas,
                "restantes": ESTADO["restantes"]}
    except ErroAPI as e:
        ESTADO["ultimo_erro"] = str(e)
        raise
    finally:
        _PUXANDO.release()


def _laco():
    espera, ultima_renovacao = 5, 0.0
    while True:
        try:
            if _linhas() and time.time() - ultima_renovacao > 6 * 3600:
                ultima_renovacao = time.time()
                renovar_todos()
            r = puxar()
            if r.get("configurado") is False:
                espera = 30
            else:
                espera = 1 if r.get("restantes") else 4
        except Exception as e:
            ESTADO["ultimo_erro"] = _limpo(e)
            espera = min(60, max(5, espera * 2))
        time.sleep(espera)


_LACO = None


def iniciar_laco():
    """Sobe a puxada do relé junto com o painel (sem relé configurado, só espera)."""
    global _LACO
    if _LACO and _LACO.is_alive():
        return
    _LACO = threading.Thread(target=_laco, name="instagram-rele", daemon=True)
    _LACO.start()


def status():
    url = cfg("IG_RELE_URL")
    return {"configurado": configurado(), "versao": versao(),
            "rele_url": url.split("?")[0] if url else "",
            "laco_ativo": bool(_LACO and _LACO.is_alive()),
            "estado": dict(ESTADO), "contas": contas()}
