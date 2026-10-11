# -*- coding: utf-8 -*-
"""ia_roteador.py — a IA das mensagens personalizadas, pelo 9router desta máquina."""

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("ia_roteador")

RAIZ = Path(__file__).resolve().parent


def _carregar_env():
    """As IA_MSG_* moram no .env do projeto, junto das outras chaves."""
    env = RAIZ / ".env"
    if not env.exists():
        return
    for linha in env.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        k, v = linha.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_carregar_env()

URL = os.environ.get("IA_MSG_URL", "http://127.0.0.1:20128/v1").rstrip("/")
CHAVE = os.environ.get("IA_MSG_KEY", "")
MODELOS = [m.strip() for m in os.environ.get(
    "IA_MSG_MODELOS",
    "cf/@cf/meta/llama-3.3-70b-instruct-fp8-fast,"
    "cf/@cf/mistralai/mistral-small-3.1-24b-instruct,Combotest").split(",") if m.strip()]
PRAZO_MODELO = int(os.environ.get("IA_MSG_PRAZO_MODELO", "25"))
PRAZO_TOTAL = int(os.environ.get("IA_MSG_PRAZO_TOTAL", "50"))

# O comando que o operador usa para abrir o 9router. `--host 127.0.0.1`: só esta
# máquina alcança o roteador.
COMANDO = os.environ.get("NOVEROUTER_COMANDO",
                         "9router --no-browser --skip-update --host 127.0.0.1")

_u = urlparse(URL)
HOST = _u.hostname or "127.0.0.1"
PORTA = _u.port or 20128
LOCAL = HOST in ("127.0.0.1", "localhost", "::1")

# Castigo por tipo de erro, em segundos. O que diz "volte já" (sobrecarga)
# castiga pouco; o que diz "nunca mais" (modelo aposentado) castiga muito.
CASTIGO = {
    "credito": 30 * 60,        # 402
    "capacidade": 5 * 60,      # 429
    "sobrecarga": 3 * 60,      # 5xx
    "inexistente": 6 * 3600,   # 404, 410
    "recusado": 6 * 3600,      # 401, 403 (chave ou modelo sem permissão)
    "demora": 5 * 60,          # timeout
    "vazio": 10 * 60,          # modelo que só "pensa" e não responde
    "outro": 10 * 60,
}

_TRAVA = threading.Lock()
_PAUSA: dict = {}              # modelo -> até quando fica de fora (epoch)
_ULTIMA_PARTIDA = [0.0]        # quando este processo mandou abrir o 9router
PARTIDA_INTERVALO = 120        # não abre de novo antes disso


# ══════════════════════════════════════════════════════════════════════════════
#  O 9ROUTER ESTÁ ABERTO?
# ══════════════════════════════════════════════════════════════════════════════

def configurado() -> bool:
    return bool(CHAVE and MODELOS)


def no_ar(timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((HOST, PORTA), timeout=timeout):
            return True
    except OSError:
        return False


def iniciar() -> bool:
    """Abre o 9router numa janela PRÓPRIA, como o operador abre."""
    if not LOCAL:
        logger.warning("9router em %s não é desta máquina: não dá para abrir", URL)
        return False
    if sys.platform == "win32":
        base = subprocess.CREATE_NEW_CONSOLE
        breakaway = 0x01000000        # CREATE_BREAKAWAY_FROM_JOB
        args = ["cmd.exe", "/k", f"title 9router && {COMANDO}"]
        for flags in (base | breakaway, base):
            try:
                subprocess.Popen(args, cwd=str(Path.home()), creationflags=flags,
                                 close_fds=True)
                return True
            except OSError as e:
                logger.info("abrir o 9router com flags %#x falhou (%s)", flags, e)
        return False
    try:
        subprocess.Popen(COMANDO.split(), cwd=str(Path.home()),
                         start_new_session=True, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except OSError as e:
        logger.warning("não consegui abrir o 9router: %s", e)
        return False


def garantir(esperar_s: float = 25.0) -> dict:
    """{no_ar, acao, detalhe}. Confere a porta e, fechado, abre e espera subir."""
    if no_ar():
        return {"no_ar": True, "acao": "ja_estava", "detalhe": f"porta {PORTA}"}
    if not LOCAL:
        return {"no_ar": False, "acao": "remoto", "detalhe": f"{URL} não responde"}
    with _TRAVA:
        agora = time.time()
        recente = agora - _ULTIMA_PARTIDA[0] < PARTIDA_INTERVALO
        if not recente:
            _ULTIMA_PARTIDA[0] = agora
    if recente:
        # outra chamada acabou de mandar abrir: só espera
        fim = time.time() + esperar_s
        while time.time() < fim:
            if no_ar():
                return {"no_ar": True, "acao": "iniciado", "detalhe": "subiu"}
            time.sleep(0.5)
        return {"no_ar": False, "acao": "recente",
                "detalhe": "já mandei abrir há pouco e ele não subiu"}
    if not iniciar():
        return {"no_ar": False, "acao": "falhou",
                "detalhe": f"não consegui executar: {COMANDO}"}
    logger.info("9router estava fechado; mandei abrir (%s)", COMANDO)
    fim = time.time() + esperar_s
    while time.time() < fim:
        if no_ar():
            return {"no_ar": True, "acao": "iniciado",
                    "detalhe": f"subiu em {esperar_s - (fim - time.time()):.1f}s"}
        time.sleep(0.5)
    return {"no_ar": False, "acao": "falhou",
            "detalhe": f"mandei abrir, mas a porta {PORTA} não abriu em {esperar_s:.0f}s"}


# ══════════════════════════════════════════════════════════════════════════════
#  PEDIR TEXTO
# ══════════════════════════════════════════════════════════════════════════════

def _em_pausa(modelo: str) -> float:
    with _TRAVA:
        return max(0.0, _PAUSA.get(modelo, 0.0) - time.time())


def _castigar(modelo: str, tipo: str):
    with _TRAVA:
        _PAUSA[modelo] = time.time() + CASTIGO.get(tipo, CASTIGO["outro"])


def _tipo_do_http(codigo: int) -> str:
    if codigo == 402:
        return "credito"
    if codigo == 429:
        return "capacidade"
    if codigo in (404, 410):
        return "inexistente"
    if codigo in (401, 403):
        return "recusado"
    if codigo >= 500:
        return "sobrecarga"
    return "outro"


def _ler_resposta(bruto: str) -> tuple:
    """(texto, finish_reason) de uma resposta em JSON OU em fluxo (SSE)."""
    try:
        d = json.loads(bruto)
        c = (d.get("choices") or [{}])[0]
        return ((c.get("message") or {}).get("content") or ""), c.get("finish_reason")
    except (ValueError, AttributeError, IndexError):
        pass
    partes, fim = [], None
    for linha in bruto.splitlines():
        linha = linha.strip()
        if linha.startswith("data:"):
            linha = linha[5:].strip()
        if not linha or linha == "[DONE]":
            continue
        try:
            ev = json.loads(linha)
        except ValueError:
            continue
        for c in ev.get("choices") or []:
            partes.append(((c.get("delta") or {}).get("content"))
                          or ((c.get("message") or {}).get("content")) or "")
            fim = c.get("finish_reason") or fim
    return "".join(partes), fim


def _recusou_conexao(exc: Exception) -> bool:
    razao = getattr(exc, "reason", exc)
    return isinstance(razao, ConnectionRefusedError) or "10061" in str(razao) \
        or "refused" in str(razao).lower()


def gerar(mensagens: list, max_tokens: int = 700, temperatura: float = 0.7) -> dict:
    """{ok, texto, modelo, motivo, tentativas}. Nunca levanta exceção."""
    tentativas = []
    if not configurado():
        return {"ok": False, "texto": "", "modelo": "", "tentativas": tentativas,
                "motivo": "IA_MSG_KEY/IA_MSG_MODELOS ausentes no .env"}
    inicio = time.time()
    ja_tentou_abrir = False
    # O 9router inteiro fechado NÃO é culpa de modelo nenhum.
    if LOCAL and not no_ar():
        ja_tentou_abrir = True
        t0 = time.time()
        g = garantir()
        tentativas.append({"modelo": "(9router)", "resultado": g["acao"],
                           "segundos": round(time.time() - t0, 1)})
        if not g["no_ar"]:
            return {"ok": False, "texto": "", "modelo": "", "tentativas": tentativas,
                    "motivo": f"9router fora do ar ({g['detalhe']})"}
    fila = list(MODELOS)
    i = 0
    while i < len(fila):
        modelo = fila[i]
        restante = PRAZO_TOTAL - (time.time() - inicio)
        if restante < 3:
            tentativas.append({"modelo": modelo, "resultado": "sem tempo", "segundos": 0})
            break
        pausa = _em_pausa(modelo)
        if pausa > 0:
            tentativas.append({"modelo": modelo, "resultado": f"de castigo ({pausa:.0f}s)",
                               "segundos": 0})
            i += 1
            continue
        corpo = {"model": modelo, "messages": mensagens, "temperature": temperatura,
                 "max_tokens": max_tokens, "stream": False}
        req = urllib.request.Request(
            f"{URL}/chat/completions", data=json.dumps(corpo).encode("utf-8"),
            headers={"Authorization": f"Bearer {CHAVE}",
                     "Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=min(PRAZO_MODELO, restante)) as r:
                bruto = r.read().decode("utf-8", "replace")
            texto, fim = _ler_resposta(bruto)
            texto = (texto or "").strip()
            gasto = round(time.time() - t0, 1)
            if fim == "length":
                tentativas.append({"modelo": modelo, "resultado": "cortado", "segundos": gasto})
                i += 1
                continue
            if not texto:
                _castigar(modelo, "vazio")
                tentativas.append({"modelo": modelo, "resultado": "vazio", "segundos": gasto})
                i += 1
                continue
            tentativas.append({"modelo": modelo, "resultado": "ok", "segundos": gasto})
            return {"ok": True, "texto": texto, "modelo": modelo, "motivo": "",
                    "tentativas": tentativas}
        except urllib.error.HTTPError as e:
            tipo = _tipo_do_http(e.code)
            _castigar(modelo, tipo)
            tentativas.append({"modelo": modelo, "resultado": f"HTTP {e.code} ({tipo})",
                               "segundos": round(time.time() - t0, 1)})
            i += 1
        except urllib.error.URLError as e:
            if _recusou_conexao(e):
                # o roteador inteiro está fechado: todos os modelos passam por ele
                if not ja_tentou_abrir:
                    ja_tentou_abrir = True
                    g = garantir()
                    tentativas.append({"modelo": "(9router)", "resultado": g["acao"],
                                       "segundos": round(time.time() - t0, 1)})
                    if g["no_ar"]:
                        continue            # tenta o MESMO modelo de novo
                return {"ok": False, "texto": "", "modelo": "", "tentativas": tentativas,
                        "motivo": "9router fora do ar"}
            _castigar(modelo, "demora" if "timed out" in str(e).lower() else "outro")
            tentativas.append({"modelo": modelo, "resultado": f"erro de rede ({e.reason})",
                               "segundos": round(time.time() - t0, 1)})
            i += 1
        except (TimeoutError, socket.timeout):
            _castigar(modelo, "demora")
            tentativas.append({"modelo": modelo, "resultado": "demorou demais",
                               "segundos": round(time.time() - t0, 1)})
            i += 1
        except Exception as e:                      # nunca derrubar quem chamou
            _castigar(modelo, "outro")
            tentativas.append({"modelo": modelo, "resultado": f"erro ({type(e).__name__})",
                               "segundos": round(time.time() - t0, 1)})
            i += 1
    resumo = "; ".join(f"{t['modelo']}: {t['resultado']}" for t in tentativas) or "nenhum modelo"
    return {"ok": False, "texto": "", "modelo": "", "tentativas": tentativas,
            "motivo": f"nenhum modelo respondeu ({resumo})"}


def estado() -> dict:
    """Para a tela: o roteador está aberto, e quem está de castigo."""
    with _TRAVA:
        agora = time.time()
        pausas = {m: int(ate - agora) for m, ate in _PAUSA.items() if ate > agora}
    return {"url": URL, "no_ar": no_ar(), "configurado": configurado(),
            "modelos": list(MODELOS), "de_castigo": pausas, "local": LOCAL}


def _esquecer():
    """Zera castigos e a marca de partida. Para os testes."""
    with _TRAVA:
        _PAUSA.clear()
        _ULTIMA_PARTIDA[0] = 0.0
