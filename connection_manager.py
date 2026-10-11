"""
connection_manager.py — Gerencia conexões reais dos canais WhatsApp, Instagram e E-mail.
"""
import json, os, subprocess, sys, urllib.error, urllib.request
from pathlib import Path
from typing import Dict, List, Optional

import config

RAIZ = Path(__file__).resolve().parent

# ══════════════════════════════════════════════════════════════════════════════
# 📱 WHATSAPP
# ══════════════════════════════════════════════════════════════════════════════

def _wa_api(caminho: str, dados=None, timeout=5):
    """Chama a API do bot WhatsApp."""
    try:
        url = f"{config.WHATSAPP_BOT_URL}{caminho}"
        body = json.dumps(dados).encode() if dados else None
        req = urllib.request.Request(url, data=body, method='POST' if dados else 'GET',
                                     headers={'Content-Type': 'application/json'} if body else {})
        r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            resposta = json.loads(e.read())
            if isinstance(resposta, dict):
                resposta.setdefault("ok", False)
                resposta.setdefault("erro", f"Bot WhatsApp respondeu HTTP {e.code}")
                return resposta
        except Exception:
            pass
        if e.code == 404:
            return {"ok": False,
                    "erro": "A rota não existe na versão do bot WhatsApp em execução. Reinicie o serviço WhatsApp para atualizar."}
        return {"ok": False, "erro": f"Bot WhatsApp respondeu HTTP {e.code}"}
    except Exception:
        return {"ok": False, "pronto": False, "erro": "Bot offline"}


def wa_status(conta: str = "") -> Dict:
    """Status real de UMA conta do bot WhatsApp (vazio = conta padrao)."""
    s = _wa_api("/api/status" + (f"?conta={conta}" if conta else ""))
    return {
        "online": s.get("pronto", False) or s.get("iniciado", False),
        "conectado": s.get("pronto", False),
        "numero": s.get("numero"),
        "conta": s.get("conta"),
        "tem_qr": s.get("temQr", False),
        "qr": None,
        "erro": s.get("erro"),
        "resposta_modo": s.get("resposta", {}).get("modo", "off"),
        "contas": s.get("contas") or [],
    }


def wa_contas_bot() -> List[Dict]:
    """Todas as contas que o bot conhece, com o estado de cada uma."""
    r = _wa_api("/api/contas")
    return r.get("contas") or []


def wa_qr_code(conta: str = "") -> Optional[str]:
    """QR code em base64 (data URI) de UMA conta, se houver."""
    s = _wa_api("/api/status" + (f"?conta={conta}" if conta else ""))
    if s.get("temQr"):
        qr = _wa_api("/api/qr" + (f"?conta={conta}" if conta else ""))
        return qr.get("qr")
    return None


def wa_conectar(conta: str = "") -> Dict:
    """Poe UMA conta para conectar (gera o QR se ela nao estiver pareada)."""
    numero = "".join(ch for ch in str(conta or "") if ch.isdigit())
    if numero:
        return _wa_api("/api/contas", {"conta": numero})
    return _wa_api("/api/iniciar", {})


def wa_abrir_perfil(conta: str) -> Dict:
    """Abre a sessão LocalAuth da conta em uma janela Chromium visível."""
    numero = "".join(ch for ch in str(conta or "") if ch.isdigit())
    if not 8 <= len(numero) <= 15:
        return {"ok": False, "erro": "informe o telefone completo da conta com DDI"}
    return _wa_api(f"/api/contas/{numero}/abrir-perfil",
                   {"conta": numero}, timeout=30)


def wa_desconectar(conta: str) -> Dict:
    """Desconecta e esquece a sessao de uma conta."""
    numero = "".join(ch for ch in str(conta or "") if ch.isdigit())
    if not numero:
        return {"ok": False, "erro": "informe o numero da conta"}
    try:
        from senders import whatsapp_sender
        return whatsapp_sender.remover_conta(numero)
    except Exception as e:
        return {"ok": False, "erro": str(e)}


def _com_janela(contas, canal):
    """Troca o `sent_today` guardado pelo consumo REAL das ultimas 24h."""
    try:
        import limites
        por_id = {e["id"]: e for e in limites.estado(canal)}
    except Exception:
        return [dict(c) for c in contas]
    saida = []
    for c in contas:
        d = dict(c)
        e = por_id.get(d["id"])
        if e:
            d["sent_today"] = e["usados"]        # nome antigo, valor da janela
            d["usados_24h"] = e["usados"]
            d["daily_limit"] = e["limite"]
            d["restam"] = e["restam"]
            d["libera_em"] = e["libera_em"]
            d["cheia"] = e["cheia"]
            d["ultimo_envio"] = e["ultimo_envio"]
        saida.append(d)
    return saida


def wa_status_contas() -> List[Dict]:
    """Status de cada conta WhatsApp cadastrada no LeadForge."""
    import db
    with db.conn() as con:
        contas = con.execute(
            "SELECT id, identifier, display_name, daily_limit, sent_today, auto_prospect FROM accounts WHERE channel='whatsapp'"
        ).fetchall()
    contas = _com_janela(contas, "whatsapp")
    
    # O bot mantem um cliente POR CONTA. Cada uma tem a sua sessao e o seu
    # Chromium: uma sessao do WhatsApp Web pertence a um numero so.
    do_bot = wa_contas_bot()
    bot_online = bool(do_bot) or wa_status().get("online", False)

    def _fim(n):
        n = "".join(ch for ch in str(n or "") if ch.isdigit())
        return n[-8:] if len(n) >= 8 else n

    resultado = []
    for c in contas:
        c = dict(c)
        alvo = _fim(c["identifier"])
        # Casa pelo id da conta no bot OU pelo numero que o QR pareou. Os dois
        # porque o operador cadastra o numero que ele acha que e, e quem diz o
        # numero de verdade e o WhatsApp, depois do pareamento.
        no_bot = next((b for b in do_bot
                       if _fim(b.get("conta")) == alvo or _fim(b.get("numero")) == alvo), None)
        c["conectado"] = bool(no_bot and no_bot.get("pronto"))
        c["tem_qr"] = bool(no_bot and no_bot.get("temQr"))
        c["perfil_aberto"] = bool(no_bot and no_bot.get("perfilAberto"))
        c["cadastrada_no_bot"] = no_bot is not None
        c["bot_online"] = bot_online
        # O erro do bot PRECISA chegar a tela.
        c["erro_bot"] = (no_bot or {}).get("erro") or ""
        # `iniciado` distingue "o bot esta tentando" de "o bot nem comecou".
        # Conta sem sessao salva NAO sobe sozinha: ela espera o pedido de
        # pareamento. Sem isso a tela nao tem como saber que falta um clique.
        c["iniciado_no_bot"] = bool(no_bot and no_bot.get("iniciado"))
        c["status_conexao"] = ("Perfil aberto no navegador" if c["perfil_aberto"]
                               else "Conectada" if c["conectado"]
                               else "Aguardando QR" if c["tem_qr"]
                               else "Falhou ao conectar" if c["erro_bot"]
                               else "Cadastrada, conectando" if c["cadastrada_no_bot"]
                               else "Sem sessao - precisa parear")
        resultado.append(c)
    return resultado


# ══════════════════════════════════════════════════════════════════════════════
# 📸 INSTAGRAM
# ══════════════════════════════════════════════════════════════════════════════

def _ig_sessoes_dir() -> Path:
    """
    Convencao de sessao do Hube 02 (uma pasta por conta, com playwright_state.json).
    """
    base = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / "ProspeccaoHub" / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _ig_perfil_bot(conta: str = "") -> Path:
    """O perfil de Chromium que o bot de DM REALMENTE abre para esta conta."""
    perfil = config.ig_perfil(conta)
    if conta:
        return perfil
    # Sem conta: perfil unico legado, com a mesma reserva que o bot faz.
    antigo = RAIZ / "senders" / "dm_bot" / "session_data"
    if not perfil.exists() and antigo.exists():
        return antigo
    return perfil


def ig_perfil_sessao(conta: str = "") -> Dict:
    """Estado real da sessao de UMA conta do Instagram."""
    perfil = _ig_perfil_bot(conta)
    conta_norm = config.ig_conta_normalizada(conta)
    comando = ("python -X utf8 senders/dm_bot_login.py "
               + conta_norm).strip()
    try:
        from senders import dm_bot_login
        estado = dm_bot_login.sessao_no_disco(str(perfil))
    except Exception as e:
        return {"conta": conta_norm or None, "perfil": str(perfil), "ativa": False,
                "motivo": f"nao consegui verificar: {e}", "comando_login": comando}
    return {
        "conta": conta_norm or None,
        "perfil": str(perfil),
        "ativa": bool(estado.get("ativa")),
        "motivo": estado.get("motivo", ""),
        "comando_login": comando,
    }


def ig_status_bot() -> Dict:
    """O bot de DM (porta 5022) esta no ar?"""
    base = config.DM_BOT_URL.rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/estado", timeout=3) as r:
            estado = json.loads(r.read().decode("utf-8"))
        return {"online": True, "url": base,
                "rodando": bool(estado.get("rodando")),
                "perfis_abertos": estado.get("perfis_abertos") or []}
    except urllib.error.HTTPError:
        return {"online": True, "url": base}      # servidor antigo respondeu
    except Exception as e:
        return {"online": False, "url": base,
                "erro": f"Bot de DM offline em {base}: {e}"}


def ig_abrir_perfil(username: str = "") -> Dict:
    """Pede ao bot de Instagram para abrir a sessão persistente em janela visível."""
    return ig_janela_manual(username, True, "inicio")


def ig_aquecimento_status(painel: bool = False) -> Dict:
    """
    Janelas manuais abertas (Reels, Direct, início), com o destino de cada uma.
    `painel`: o pedido vem da lista do Direct na tela (o bot espia só com ela).
    """
    base = config.DM_BOT_URL.rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/janela-manual/status"
                                    + ("?painel=1" if painel else ""), timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"active": [], "erro": f"Bot de Instagram indisponível: {e}"}


def ig_janela_manual(username: str, ativo: bool, destino: str, embutida: bool = False,
                     so_nao_lidas=None, fechar_apos_min=None) -> Dict:
    """Abre, leva para outro destino ou fecha a janela autenticada de uma conta."""
    conta = config.ig_conta_normalizada(username)
    if not conta:
        return {"ok": False, "erro": "informe uma conta Instagram válida"}
    base = config.DM_BOT_URL.rstrip("/")
    corpo = {"conta": conta, "ativo": bool(ativo), "destino": destino, "embutida": bool(embutida)}
    if isinstance(so_nao_lidas, bool):
        corpo["so_nao_lidas"] = so_nao_lidas
    if fechar_apos_min:
        corpo["fechar_apos_min"] = fechar_apos_min
    dados = json.dumps(corpo).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/janela-manual", data=dados, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
            d["http"] = r.status
            return d
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "http": 502,
                    "erro": "O bot de Instagram no ar é de uma versão anterior. "
                            "Reinicie com: python -X utf8 subir.py --reiniciar"}
        try:
            d = json.loads(e.read().decode("utf-8"))
        except Exception:
            d = {"ok": False, "erro": f"Bot de Instagram respondeu HTTP {e.code}"}
        d["http"] = e.code
        return d
    except Exception as e:
        return {"ok": False, "http": 502, "erro": f"Bot de Instagram indisponível: {e}"}


def ig_aquecimento(username: str, ativo: bool) -> Dict:
    """Abre ou encerra a sessão visível de Reels para uso manual."""
    return ig_janela_manual(username, ativo, "reels")


def ig_status_contas(perfis_abertos=None) -> List[Dict]:
    """Status de cada conta Instagram: tem sessão? está autenticada?"""
    import db
    with db.conn() as con:
        contas = con.execute(
            "SELECT id, identifier, display_name, daily_limit, sent_today, auto_prospect FROM accounts WHERE channel='instagram'"
        ).fetchall()
    contas = _com_janela(contas, "instagram")
    
    if perfis_abertos is None:
        estado_bot = ig_status_bot()
        bot_online = estado_bot.get("online", False)
        perfis_abertos = set(estado_bot.get("perfis_abertos") or [])
    else:
        bot_online = True
        perfis_abertos = set(perfis_abertos)
    legado_unico = ig_perfil_sessao()          # perfil de antes da separacao

    resultado = []
    for c in contas:
        c = dict(c)
        sessao = ig_perfil_sessao(c["identifier"])
        # Cada conta tem o SEU perfil de navegador. Perfil guarda uma sessao so:
        # sem essa separacao, logar na segunda conta derruba a primeira.
        c["sessao_ativa"] = sessao["ativa"]
        c["perfil"] = sessao["perfil"]
        c["motivo"] = sessao["motivo"]
        c["bot_online"] = bot_online
        c["perfil_aberto"] = config.ig_conta_normalizada(c["identifier"]) in perfis_abertos
        c["comando_login"] = sessao["comando_login"]
        c["status_conexao"] = ("Conectada" if sessao["ativa"]
                               else "Sem sessao - precisa logar")
        if not sessao["ativa"] and legado_unico["ativa"]:
            c["observacao"] = ("existe uma sessao no perfil unico antigo "
                               f"({legado_unico['perfil']}); logue por conta para separar")
        resultado.append(c)
    return resultado


def ig_autenticar(username: str = "", trocar_conta: bool = False) -> Dict:
    """Abre a janela de login do Instagram no perfil que o BOT usa."""
    script = RAIZ / "senders" / "dm_bot_login.py"
    if not script.exists():
        return {"ok": False, "erro": f"{script} nao encontrado"}

    conta = config.ig_conta_normalizada(username)
    if username and not conta:
        return {"ok": False, "erro": f"conta invalida: {username!r}"}

    if trocar_conta:
        perfil = _ig_perfil_bot(conta)
        if perfil.exists():
            from datetime import datetime
            destino = perfil.with_name(
                perfil.name + "_anterior_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
            try:
                # renomeia em vez de apagar: se o login novo falhar, o perfil
                # anterior ainda esta la para ser restaurado na mao
                perfil.rename(destino)
            except Exception as e:
                return {"ok": False, "erro": f"nao consegui guardar o perfil atual: {e}. "
                                             f"Feche o bot de DM (porta 5022) e tente de novo."}

    # Roda SEM janela de console. O que o operador precisa ver é a janela do
    # Chromium, que o script abre de qualquer jeito; o console só despejava
    # texto no desktop. A saída vai para arquivo, e o painel a mostra.
    log = RAIZ / "data" / "ig_login.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        saida = open(log, "w", encoding="utf-8")
        subprocess.Popen(
            [sys.executable, "-X", "utf8", str(script)]
            + ([conta] if conta else []),
            cwd=str(RAIZ), stdout=saida, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    return {"ok": True,
            "mensagem": "Abri a janela de login do Instagram. Faca o login "
                        "(e o 2FA, se pedir) e a sessao e salva sozinha.",
            "conta": conta or None,
            "perfil": str(_ig_perfil_bot(conta)),
            "comando_manual": ("python -X utf8 senders/dm_bot_login.py "
                               + conta).strip()}


# ══════════════════════════════════════════════════════════════════════════════
# 📧 E-MAIL (GMAIL)
# ══════════════════════════════════════════════════════════════════════════════

def _gmail_tokens_dir() -> Path:
    d = Path.home() / ".pcz" / "tokens"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _gmail_creds_file() -> Path:
    return Path.home() / ".pcz" / "credentials.json"


def email_status_geral() -> Dict:
    """Status geral do Gmail: tem credentials.json? quantas contas autenticadas?"""
    creds_existe = _gmail_creds_file().exists()
    tokens_dir = _gmail_tokens_dir()
    tokens = list(tokens_dir.glob("*.json")) if tokens_dir.exists() else []
    
    return {
        "credentials_json": creds_existe,
        "credentials_path": str(_gmail_creds_file()),
        "tokens_dir": str(tokens_dir),
        "contas_autenticadas": len(tokens),
        "tokens": [t.stem for t in tokens],
    }


def email_status_contas() -> List[Dict]:
    """Status de cada conta Gmail: está autenticada? tem token válido?"""
    import db
    with db.conn() as con:
        contas = con.execute(
            "SELECT id, identifier, display_name, daily_limit, sent_today, auto_prospect FROM accounts WHERE channel='gmail'"
        ).fetchall()
    
    tokens_dir = _gmail_tokens_dir()
    
    resultado = []
    for c in contas:
        c = dict(c)
        email = c["identifier"]
        token_path = tokens_dir / f"{email}.json"
        tem_token = token_path.exists()
        
        token_valido = False
        if tem_token:
            try:
                from google.oauth2.credentials import Credentials
                creds = Credentials.from_authorized_user_file(str(token_path))
                token_valido = creds.valid or (creds.expired and creds.refresh_token is not None)
            except Exception:
                token_valido = False
        
        c["tem_token"] = tem_token
        c["token_valido"] = token_valido
        c["status_conexao"] = "✅ Conectado" if token_valido else ("⚠️ Token expirado" if tem_token else "❌ Sem OAuth")
        resultado.append(c)
    return resultado


def email_autenticar(email: str) -> Dict:
    """Inicia fluxo OAuth2 para uma conta Gmail."""
    if not _gmail_creds_file().exists():
        return {
            "ok": False,
            "erro": "credentials.json não encontrado",
            "instrucoes": [
                "1. Acesse https://console.cloud.google.com/apis/credentials",
                "2. Crie um OAuth 2.0 Client ID (Desktop Application)",
                "3. Baixe o JSON e salve como: " + str(_gmail_creds_file()),
                "4. Ative a Gmail API em https://console.cloud.google.com/apis/library/gmail.googleapis.com",
            ]
        }
    
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        
        SCOPES = [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.settings.basic",
            "https://www.googleapis.com/auth/gmail.modify",
        ]
        
        tokens_dir = _gmail_tokens_dir()
        token_path = tokens_dir / f"{email}.json"
        
        flow = InstalledAppFlow.from_client_secrets_file(str(_gmail_creds_file()), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
        token_path.write_text(creds.to_json())
        
        return {"ok": True, "mensagem": f"Conta {email} autenticada com sucesso!"}
    except Exception as e:
        return {"ok": False, "erro": str(e)}


def emailregar_status_completo() -> Dict:
    """Status completo para exibição no dashboard."""
    geral = email_status_geral()
    contas = email_status_contas()
    
    return {
        "geral": geral,
        "contas": contas,
    }
