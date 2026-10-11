import asyncio
import random
import os
import logging
import re
import unicodedata
import threading
import json
import hashlib
import time
import base64
from datetime import datetime
from urllib.parse import urlparse, urljoin
from playwright.async_api import async_playwright
from flask import Flask, request, jsonify

# === CONFIGURAÇÃO DE LOGS ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("log_prospeccao.txt", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Silencia logs excessivos do Flask
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# === DIRETÓRIOS ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PERFIS_DIR = os.path.join(BASE_DIR, 'perfis')

# Perfil do navegador FORA do OneDrive.
_APPDATA = os.environ.get("LOCALAPPDATA")
SESSION_DIR = os.environ.get("IG_SESSION_DIR") or os.path.join(
    _APPDATA or os.path.expanduser("~"), "clinica_prospector", "ig_session")
_ANTIGO = os.path.join(BASE_DIR, 'session_data')

# A reserva para o perfil antigo era SILENCIOSA, e foi assim que o bot passou a
# usar um perfil sem `sessionid` enquanto o perfil bom estava ao lado: subindo
# num ambiente sem LOCALAPPDATA, o caminho padrao vira
# C:\Users\<voce>\clinica_prospector\ig_session, que nao existe, e a reserva
# assumia sem dizer nada.
if not _APPDATA and not os.environ.get("IG_SESSION_DIR"):
    logger.warning("LOCALAPPDATA ausente no ambiente: perfil do Instagram "
                   "resolvido para %s. Se nao for o perfil logado, suba o bot "
                   "com IG_SESSION_DIR apontando para o certo.", SESSION_DIR)
if not os.path.exists(SESSION_DIR) and os.path.exists(_ANTIGO):
    logger.warning("perfil %s nao existe; caindo no perfil antigo %s. "
                   "Se a sessao falhar, o login provavelmente esta no outro.",
                   SESSION_DIR, _ANTIGO)
    SESSION_DIR = _ANTIGO
logger.info("Perfil do Instagram (padrao): %s", SESSION_DIR)

# ── Perfil POR CONTA ─────────────────────────────────────────────────────────
# Cada conta do Instagram tem o seu proprio perfil de Chromium, senao logar na
# segunda conta derruba a sessao da primeira: perfil de navegador guarda UMA
# sessao.
import sys as _sys
_RAIZ = os.path.dirname(os.path.dirname(BASE_DIR))
if _RAIZ not in _sys.path:
    _sys.path.insert(0, _RAIZ)
try:
    import config as _config
except Exception as _e:
    _config = None
    logger.warning("config.py nao pode ser importado (%s); o perfil por conta "
                   "fica indisponivel e o bot usa so o perfil padrao", _e)

try:
    import limites as _limites
except Exception as _e2:
    _limites = None
    logger.warning("limites.py nao pode ser importado (%s); o teto volta a ser "
                   "o contador em memoria desta execucao", _e2)

# Quem JA recebeu DM, em qualquer nicho, conta ou versao do bot. Sem ele, so o
# processados.txt do nicho em uso segura a repeticao -- e foi assim que 85
# perfis receberam a abordagem duas vezes. Ver historico_ig.py.
try:
    import historico_ig as _historico
except Exception as _e3:
    _historico = None
    logger.error("historico_ig.py nao pode ser importado (%s): so o "
                 "processados.txt do nicho em uso vai evitar DM repetida", _e3)

# A mensagem de cada perfil na prospeccao por seguidores: variaveis, variacoes
# {a|b}, blocos {se...: } e, com a personalizacao do Instagram ligada, a IA do
# 9router. Sem o modulo, a mensagem sai como sempre saiu ({nome} e mais nada).
try:
    import personalizar as _personalizar
except Exception as _e4:
    _personalizar = None
    logger.warning("personalizar.py nao pode ser importado (%s): a mensagem da "
                   "prospeccao sai so com {nome}", _e4)

# A checagem "ja existe conversa?" na tela. Sem try de proposito: sem ela o bot
# NAO pode enviar, e falhar na subida e melhor do que mandar DM repetida.
_AQUI = os.path.dirname(os.path.abspath(__file__))
if _AQUI not in _sys.path:
    _sys.path.insert(0, _AQUI)
import conversa_ig as _conversa
import janela_remota
import navegador_unico
import reels_leve

# Guarda de Host (DNS rebinding) e TCP sem atraso, comuns com o painel. Sem
# try de propósito: sem a guarda, a tela e o teclado das contas ficariam
# alcançáveis por um site aberto no navegador — melhor não subir.
import servidor_local as _servidor_local

CONTA_ATUAL = ""          # conta em uso AGORA no rodizio ("" = perfil padrao)
CONTAS_FILA = []          # contas que ainda faltam no rodizio deste disparo
JANELAS = {}               # conta -> estado da janela manual dela (a ÚNICA fonte)
JANELAS_LOCK = threading.Lock()
CONTAS_DO_BOT = set()      # contas que a prospecção/lote em andamento está usando
# nomes da v022, quando só existia o aquecimento
AQUECIMENTO_ATIVO, AQUECIMENTO_LOCK = JANELAS, JANELAS_LOCK


class _PerfisAbertos:
    """
    Contas cuja PASTA de perfil está aberta por uma janela externa (Reels, abrir perfil)
    — derivado de JANELAS. A janela do painel não entra: ela roda no navegador único com
    uma cópia dos cookies e não prende a pasta. Quem o envio deve evitar é
    `_contas_ocupadas` (esta mais a conta sendo vista).
    """
    def _contas(self):
        return [c for c, e in list(JANELAS.items()) if not e.get("embutida")]

    def __contains__(self, conta):
        e = JANELAS.get(conta)
        return bool(e) and not e.get("embutida")

    def __iter__(self):
        return iter(self._contas())

    def __len__(self):
        return len(self._contas())


PERFIS_ABERTOS = _PerfisAbertos()


def contas_do_rodizio():
    """Todas as contas de Instagram que podem enviar agora, com mais vaga primeiro."""
    if _limites is None or _config is None:
        return []
    saida = []
    for e in _limites.estado("instagram"):
        if e.get("status") != "active" or e.get("restam", 0) <= 0:
            continue
        nome = str(e.get("conta") or "").lstrip("@").lower()
        if not nome:
            continue
        try:
            perfil = str(_config.ig_perfil(nome))
        except Exception:
            continue
        if not os.path.exists(perfil):
            logger.warning("@%s nao tem sessao salva; fora do rodizio", nome)
            continue
        saida.append({"conta": nome, "restam": e["restam"], "limite": e["limite"]})
    saida.sort(key=lambda x: -x["restam"])
    return saida


def teto_estourado(conta=None):
    """A conta remetente ja bateu o teto das ultimas 24h?"""
    conta = conta or CONTA_ATUAL
    if _limites is not None and conta:
        try:
            c = _limites.conta_por_identificador("instagram", conta)
            if c:
                v = _limites.pode_enviar(c["id"])
                if not v["ok"]:
                    logger.warning("[TETO] @%s em %s/%s nas ultimas 24h%s",
                                   conta, v["usados"], v["limite"],
                                   f"; libera 1 em {v['libera_em']}" if v.get("libera_em") else "")
                    return True
                return False
            logger.warning("[TETO] @%s nao esta cadastrada em Contas; "
                           "usando o limite fixo de %s desta execucao",
                           conta, LIMITE_DIARIO)
        except Exception as e:
            logger.warning("[TETO] falha ao consultar o teto (%s); "
                           "usando o contador desta execucao", e)
    return estatisticas['hoje'] >= LIMITE_DIARIO


def perfil_atual(conta=None):
    """Perfil de Chromium de uma conta."""
    conta = conta or CONTA_ATUAL
    if _config is not None and conta:
        return str(_config.ig_perfil(conta))
    return SESSION_DIR

# Rodar HEADLESS derruba a sessão: o Instagram detecta o navegador automatizado
# e invalida o login, deixando no perfil só os cookies anônimos.
HEADLESS = (os.environ.get("IG_HEADLESS", "").strip().lower() in ("1", "true", "sim"))

# === VARIÁVEIS GLOBAIS ===
LIMITE_DIARIO = 50
BOT_RODANDO = False
STOP_BOT = False
NICHO_ATUAL = "Padrao"
MENSAGEM_ATUAL = ""
MODELO_ATUAL = ""
estatisticas = {"hoje": 0, "total": 0, "rejeitados": 0}

# === LOTE ALVO (clinica_prospector) === Lista de alvos específicos [{username,
# mensagem}] com MENSAGEM ÚNICA por lead.
LOTE_ALVOS = []

# ==========================================
# ====== 1. API FLASK (ouve o LeadForge) ===
# ==========================================
app = Flask(__name__)


# Janela manual: a sessão autenticada da conta aberta para o OPERADOR usar.
DESTINOS_MANUAIS = {
    "inicio": "https://www.instagram.com/",
    "reels": "https://www.instagram.com/reels/",
    "direct": "https://www.instagram.com/direct/inbox/",
}
NOME_DESTINO = {"inicio": "o Instagram", "reels": "o Reels", "direct": "o Direct"}


def _destino_da_url(url):
    """Onde a janela está AGORA: o operador navega por conta própria."""
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    if host != "instagram.com" and not host.endswith(".instagram.com"):
        return "outro"
    if u.path.startswith("/direct/"):
        return "direct"
    if u.path.startswith(("/reels/", "/reel/")):
        return "reels"
    return "inicio" if u.path in ("", "/") else "outro"


# ── Janela no painel (v024): o Direct operado de dentro do LeadForge ─────── O
# Instagram não deixa ser posto em <iframe> (X-Frame-Options); o Chromium da
# conta roda fora da tela e `janela_remota` mostra a página no painel e devolve
# a ela os cliques e as teclas.
ESTACIONAR_APOS_S = 30
NAVEGACAO_MS = 10000         # goto/voltar/recarregar: não segura o laço mais que isto

# O painel não mostra barra de endereço. Fora do Instagram, uma página falsa de
# "entre de novo" pegaria a senha digitada pelo painel, e o Chromium do
# Playwright não se atualiza sozinho — então link de fora não abre aqui.
def _host_permitido(url):
    host = (urlparse(url or "").hostname or "").lower()
    return host == "instagram.com" or host.endswith(".instagram.com")


def _fora_do_instagram(url):
    """
    Página que não pode ficar na tela do painel. `blob:`/`data:` contam: uma página de
    fora pode se trocar por uma dessas e escapar de quem só olha http.
    """
    u = str(url or "")
    return bool(u) and u != "about:blank" and not _host_permitido(u)


# Filtro "só não lidas": o Instagram no navegador não tem (o app tem).
JS_PAGINA = r"""(ligado) => {
  if (!window.__lfOuvindo) {
    window.__lfOuvindo = true;
    document.addEventListener('mousedown', () => { window.__lfClique = Date.now(); }, true);
  }
  // o aviso "Ativar notificações" que o Instagram abre a cada visita
  let popup = false;
  for (const d of document.querySelectorAll('[role=dialog]')) {
    if (!/Ativar notifica|Turn on Notifications/i.test(d.innerText || '')) continue;
    const b = [...d.querySelectorAll('button,[role=button]')]
      .find(x => /^(Agora não|Not Now)$/i.test((x.innerText || '').trim()));
    if (b) { b.click(); popup = true; }
  }
  if (!location.pathname.startsWith('/direct/')) return {popup};
  const nav = document.querySelector('main [role=navigation]');
  if (!nav) return {popup};
  const folhas = r => [...r.querySelectorAll('span')].filter(s => !s.children.length && s.textContent.trim());
  const todas = [...nav.querySelectorAll('[role=button]')]
    .filter(r => r.querySelector('img') && folhas(r).some(s => s.textContent.trim() === '·'));
  const linhas = todas.filter(r => !(r.parentElement && r.parentElement.closest('[role=button]')
                                       && todas.includes(r.parentElement.closest('[role=button]'))));
  const novaDe = new Map(linhas.map(r => {
    const fs = folhas(r);
    return [r, fs.some(s => parseInt(getComputedStyle(s).fontWeight, 10) >= 600)
      || !!r.querySelector('[aria-label*="não lida" i],[aria-label*="unread" i]')];
  }));
  const novas = [...novaDe.values()].filter(Boolean).length;
  const pausa = Date.now() - (window.__lfClique || 0) < 1500;
  let mostradas = 0;
  if (!pausa) for (const r of linhas) {
    if (novaDe.get(r) && ligado) r.dataset.lfVista = '1';
    if (!ligado) delete r.dataset.lfVista;
    const esconder = ligado && !novaDe.get(r) && !r.dataset.lfVista;
    if (esconder) { r.style.display = 'none'; r.dataset.lfOculto = '1'; }
    else { mostradas++; if (r.dataset.lfOculto) { r.style.display = ''; delete r.dataset.lfOculto; } }
  }
  // o total certo é o número no ícone do Direct do próprio Instagram: a lista
  // só desenha as conversas que cabem na tela (e há as abas Geral/Pedidos)
  let marcaIg = null;
  for (const a of document.querySelectorAll('a[href^="/direct/"]')) {
    if (nav.contains(a)) continue;
    const n = parseInt(((a.innerText || '').match(/\d+/) || [])[0], 10);
    if (!isNaN(n)) { marcaIg = n; break; }
  }
  let aviso = document.getElementById('lf-aviso-filtro');
  if (ligado && linhas.length) {
    if (!aviso || !aviso.isConnected) {
      aviso = document.createElement('div');
      aviso.id = 'lf-aviso-filtro';
      aviso.style.cssText = 'padding:10px 16px;margin:4px 8px;border-radius:8px;font:13px/1.4 system-ui,sans-serif;'
        + 'color:#1b4332;background:#e7f8ee;border:1px solid #b7e4c7';
      linhas[0].parentElement.insertBefore(aviso, linhas[0]);
    }
    const resto = marcaIg && marcaIg > novas
      ? ` O Instagram marca ${marcaIg}: role a lista para baixo, ou veja General e Pedidos.` : '';
    aviso.textContent = novas
      ? `Filtro do LeadForge ligado: só as não lidas.${resto}`
      : (resto ? `Nenhuma não lida nesta parte da lista.${resto}`
               : 'Nenhuma conversa não lida. (Filtro "Só não lidas" do LeadForge ligado — desmarque para ver todas.)');
  } else if (aviso) {
    aviso.remove();
  }
  return {popup, contagem: {conversas: linhas.length, nao_lidas: novas, marca_instagram: marcaIg}};
}"""


# Áudio e vídeo do Direct: o Chromium do Playwright NÃO tem os codecs comerciais
# (AAC/H.264) — canPlayType de mp4a.40.2 = "" no Chromium 141, "probably" no
# Chrome 154.
JS_GANCHO_MIDIA = r"""(() => {
  if (window.__lfGancho) return; window.__lfGancho = true;
  window.__lfMidia = [];
  const tocar = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    try {
      const fonte = this.querySelector && this.querySelector('source');
      const url = this.currentSrc || this.src || (fonte && fonte.src) || '';
      window.__lfMidia.push({url, tipo: this.tagName.toLowerCase(), t: Date.now()});
      if (window.__lfMidia.length > 20) window.__lfMidia.shift();
    } catch (e) {}
    return tocar.apply(this, arguments);
  };
})();"""
JS_MIDIA_DRENAR = "() => { const m = window.__lfMidia || []; window.__lfMidia = []; return m; }"


# Servidores de arquivo da Meta usados pelo Direct. A voz vem de cdn.fbsbx.com;
# foto e vídeo, de *.cdninstagram.com e *.fbcdn.net.
_HOSTS_MIDIA = (".cdninstagram.com", ".fbcdn.net", ".fbsbx.com")


MIDIA_MAX_BYTES = 50 * 1024 * 1024


def _host_de_midia(url):
    host = (urlparse(url or "").hostname or "").lower()
    return str(url or "").startswith("https://") and host.endswith(_HOSTS_MIDIA)


def _registrar_midias(estado, eventos):
    """Guarda os endereços de mídia tocados e avisa o painel do último."""
    for ev in eventos or []:
        url, tipo = str(ev.get("url") or ""), ("video" if ev.get("tipo") == "video" else "audio")
        seq = estado.get("midia_seq", 0) + 1
        estado["midia_seq"] = seq
        if _host_de_midia(url):
            midias = estado.setdefault("midias", {})
            mid = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
            midias[mid] = {"url": url, "tipo": tipo}
            while len(midias) > 60:
                midias.pop(next(iter(midias)))
            ultimo = estado.get("midia_evento") or {}
            if ultimo.get("id") == mid and time.monotonic() - estado.get("midia_evento_em", 0) < 3:
                estado["midia_seq"] = seq - 1          # o mesmo play visto duas vezes
                continue
            estado["midia_evento"] = {"seq": seq, "id": mid, "tipo": tipo}
            estado["midia_evento_em"] = time.monotonic()
        elif url:
            # blob:/mediasource: o Instagram montou a mídia na página e não há
            # endereço para o painel tocar
            estado["midia_evento"] = {"seq": seq, "id": None, "tipo": tipo,
                                      "erro": "esta mídia só toca no app ou no site do Instagram"}


async def _seguro(coro):
    try:
        await coro
    except Exception:
        pass


def _avisar_uma_vez(estado, chave, *args):
    """Aviso repetido a cada 0,3 s inundaria o log que o painel mostra: 1 por minuto."""
    agora = time.monotonic()
    ultimos = estado.setdefault("avisos_em", {})
    if agora - ultimos.get(chave, -1e9) > 60:
        ultimos[chave] = agora
        logger.warning(*args)


def _navegando(estado):
    t = estado.get("navegacao")
    return bool(t and not t.done())


def _navegar(estado, coro):
    """Navega em paralelo ao laço: a fila de cliques e o Fechar não esperam a página."""
    anterior = estado.get("navegacao")
    if anterior and not anterior.done():
        anterior.cancel()
    estado["navegacao"] = asyncio.ensure_future(_seguro(coro))


async def _evento_do_canal(page, estado, ev):
    """O que não é mouse/teclado: navegação e filtro (são do Instagram)."""
    t = ev.get("t")
    if t == "nav":
        a = ev.get("a")
        if a == "voltar":
            _navegar(estado, page.go_back(wait_until="domcontentloaded", timeout=NAVEGACAO_MS))
        elif a == "avancar":
            _navegar(estado, page.go_forward(wait_until="domcontentloaded", timeout=NAVEGACAO_MS))
        elif a == "recarregar":
            _navegar(estado, page.reload(wait_until="domcontentloaded", timeout=NAVEGACAO_MS))
        elif a == "inbox":
            _navegar(estado, page.goto(DESTINOS_MANUAIS["direct"], wait_until="domcontentloaded",
                                       timeout=NAVEGACAO_MS))
    elif t == "filtro":
        estado["so_nao_lidas"] = bool(ev.get("v"))
        estado["pagina_em"] = 0                     # reaplica já


# Contas no painel sem ninguém olhando DORMEM (página fechada; o contexto com os
# cookies fica e acordar leva ~1 s).
DORMIR_APOS_S = int(os.environ.get("LF_DORMIR_S", 180))
ESPIAR_A_CADA_S = int(os.environ.get("LF_ESPIAR_S", 600))
MAX_ACORDADAS = int(os.environ.get("LF_MAX_ACORDADAS", 3))
# Memória: abaixo disto, a conta parada há mais tempo dorme antes de abrir outra
MEMORIA_MINIMA_MB = int(os.environ.get("LF_MEMORIA_MINIMA_MB", 1500))
PAINEL_ABERTO_S = 90          # a lista do Direct consultada há menos disto = painel aberto
AVISO_VALE_S = 30             # aviso na lista some depois disto (o estado volta a aparecer)
TENTAR_ACORDAR_S = 15         # conta que não abriu tenta de novo depois disto
_PAINEL = {"lista_em": 0.0}   # última consulta da lista do Direct (monotonic)
_ESPIADA = {"trava": None}    # asyncio.Lock do loop do navegador único

# Iframes que o Instagram usa (login, verificação, pixel da Meta): podem abrir
# DENTRO da página. A página principal só navega em instagram.com.
_HOSTS_SUBQUADRO = (".instagram.com", ".facebook.com", ".fbcdn.net", ".cdninstagram.com",
                    ".fbsbx.com", ".google.com", ".gstatic.com", ".recaptcha.net")

# Link com target=_blank abre na MESMA aba: assim passa pelo bloqueio de
# `_bloquear_fora` desde o primeiro pedido (aba nova carregava o site de fora
# antes de `aba_nova` conseguir fechá-la).
JS_MESMA_ABA = r"""
document.addEventListener('click', e => {
  const a = e.target && e.target.closest ? e.target.closest('a[target]') : null;
  if (a && a.target && a.target !== '_self') a.target = '_self';
}, true);
"""


def _avisar(estado, texto):
    """Aviso na lista do painel, com prazo (AVISO_VALE_S)."""
    estado["aviso"], estado["aviso_em"] = texto, time.monotonic()


def _aviso_vigente(estado):
    if estado.get("aviso") and time.monotonic() - estado.get("aviso_em", 0) < AVISO_VALE_S:
        return estado["aviso"]
    return None


def _aviso_bloqueio(estado, url):
    _avisar(estado, "link externo bloqueado: "
                    f"{urlparse(url).hostname or (url or '').split(':', 1)[0]}")


def _pouca_memoria():
    """Fora do lock: o psutil leva milissegundos e o lock trava a tela de todas."""
    try:
        import psutil
        return psutil.virtual_memory().available / 1048576 < MEMORIA_MINIMA_MB
    except Exception:
        return False


def _host_de_subquadro(url):
    host = (urlparse(url or "").hostname or "").lower()
    return host.endswith(_HOSTS_SUBQUADRO) or host in ("instagram.com", "facebook.com")


async def _bloquear_fora(ctx, pg, estado):
    """
    Navegação para fora do Instagram é barrada ANTES de carregar (o laço só percebia
    depois, com a página de fora já rodando). Só pedidos de documento passam por aqui —
    o resto da página não paga nada.
    """
    cdp = await ctx.new_cdp_session(pg)
    raiz = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]

    def pausado(ev):
        url = (ev.get("request") or {}).get("url", "")
        principal = ev.get("frameId") == raiz
        ok = _host_permitido(url) if principal else (_host_de_subquadro(url) or url.startswith("about:"))
        if ok:
            asyncio.ensure_future(_seguro(cdp.send("Fetch.continueRequest", {"requestId": ev["requestId"]})))
        else:
            if principal:
                _aviso_bloqueio(estado, url)
            asyncio.ensure_future(_seguro(cdp.send("Fetch.failRequest", {"requestId": ev["requestId"],
                                                                         "errorReason": "BlockedByClient"})))
    cdp.on("Fetch.requestPaused", pausado)
    await cdp.send("Fetch.enable", {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]})


async def _janela_painel(conta, estado):
    """A conta aberta no painel, como página do navegador único."""
    nav = navegador_unico.NAVEGADOR
    tela = estado["tela"]
    perfil = perfil_atual(conta)
    if _ESPIADA["trava"] is None:
        _ESPIADA["trava"] = asyncio.Lock()
    ctx = None

    async def contexto(esperar=True):
        """
        O contexto da conta, recriado se o navegador único caiu. Pasta em uso pela
        prospecção: espera (até 15 min) só quando alguém quer a conta — a espiada não
        espera, para não segurar a vez das outras.
        """
        nonlocal ctx
        vivo = ctx is not None and ctx.browser is not None and ctx.browser.is_connected()
        if vivo:
            return ctx
        espera = 0
        while True:
            try:
                novo = await nav.novo_contexto(conta, perfil, janela_remota.VIEWPORT_INICIAL)
                break
            except PermissionError as e:
                if not esperar or estado["stop"].is_set() or espera > 15 * 60:
                    raise
                _avisar(estado, f"aguardando a prospecção liberar @{conta}")
                logger.info("@%s: %s", conta, e)
                await asyncio.sleep(10)
                espera += 10
        estado.pop("aviso", None)
        await novo.add_init_script(JS_GANCHO_MIDIA)
        await novo.add_init_script(JS_MESMA_ABA)

        async def aba_nova(nova):
            # Reserva do JS_MESMA_ABA (window.open, por exemplo): se for do
            # Instagram, abre na aba principal; de fora, não abre.
            try:
                # a página que o próprio painel está abrindo também dispara
                # este evento: espera ela ser registrada antes de decidir
                while estado.get("criando_pagina"):
                    await asyncio.sleep(0.05)
                if nova is estado.get("pagina") or nova is estado.get("espiando"):
                    return
                await nova.wait_for_load_state("commit")
                url = nova.url
                await nova.close()
                pg = estado.get("pagina")
                if pg and not pg.is_closed() and _host_permitido(url):
                    _navegar(estado, pg.goto(url, wait_until="domcontentloaded", timeout=NAVEGACAO_MS))
                elif url and url != "about:blank":
                    _aviso_bloqueio(estado, url)
            except Exception:
                pass
        novo.on("page", lambda nova: asyncio.ensure_future(aba_nova(nova)))
        # Só o play() conta (gancho JS_GANCHO_MIDIA): a voz passa por ele com o endereço
        # do CDN. Ouvir os pedidos de rede também pegaria o que o Instagram só PRÉ-
        # CARREGA ao abrir uma conversa.
        ctx = novo
        estado["contexto"] = novo
        return novo

    async def nova_pagina(chave, esperar=True):
        estado["criando_pagina"] = True
        try:
            pg = await nav.nova_pagina(await contexto(esperar))
            estado[chave] = pg
        finally:
            estado["criando_pagina"] = False
        try:
            await _bloquear_fora(ctx, pg, estado)
        except Exception as e:
            # sem o bloqueio a página ainda é recusada fora do Instagram pelo
            # laço (depois de carregar); fica registrado
            logger.warning("@%s: bloqueio de links externos não ligou (%s)", conta, e)
        return pg

    async def abrir_pagina():
        """Página do Direct ligada à tela. Na falha, nada fica aberto."""
        pg = await nova_pagina("pagina")
        try:
            try:
                await pg.goto(DESTINOS_MANUAIS["direct"], wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.warning("@%s: não carreguei o Direct (%s)", conta, e)
            await tela.ligar(ctx, pg)
        except BaseException:
            estado.pop("pagina", None)
            await _seguro(pg.close())
            raise
        estado["dormindo"], estado["pagina_em"] = False, 0
        return pg

    async def acordar():
        """
        Abre a página. Falhou (navegador caindo, rede): a conta continua dormindo e
        tenta de novo em TENTAR_ACORDAR_S. Sem sessão: encerra.
        """
        pouca = _pouca_memoria()
        with JANELAS_LOCK:
            _limitar_acordadas(conta, pouca)
            estado["acordando"] = True          # as outras já a contam como acordada
        try:
            pg = await abrir_pagina()
        except navegador_unico.SemSessao:
            raise
        except Exception as e:
            logger.warning("@%s não abriu (%s); tento de novo em %d s", conta, e, TENTAR_ACORDAR_S)
            _avisar(estado, "não abriu agora; tentando de novo")
            estado["tentar_acordar_em"] = time.monotonic() + TENTAR_ACORDAR_S
            estado["dormindo"] = True
            return None
        finally:
            estado.pop("acordando", None)
        estado.pop("acordar", None)
        estado.pop("dormir_ja", None)
        return pg

    async def espiar():
        """
        Abre só a caixa de entrada, lê a contagem e fecha (uma conta por vez). É
        cancelada se alguém abrir a conta no meio.
        """
        async with _ESPIADA["trava"]:
            if estado["stop"].is_set() or not estado.get("dormindo") or tela.espectadores:
                return
            pg = None
            try:
                pg = await nova_pagina("espiando", esperar=False)
                await pg.goto(DESTINOS_MANUAIS["direct"], wait_until="domcontentloaded", timeout=30000)
                await pg.wait_for_selector("main [role=navigation]", timeout=20000)
                await asyncio.sleep(2)              # a lista termina de chegar
                await _ler_pagina(conta, pg, estado)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.info("@%s: espiada sem sucesso (%s)", conta, e)
            finally:
                estado.pop("espiando", None)
                if pg is not None:
                    await _seguro(pg.close())

    def dormir_agora(motivo):
        logger.info("@%s foi dormir (%s)", conta, motivo)
        tela.soltar()
        estado["dormindo"] = True
        estado.pop("dormir_ja", None)

    espiada = None
    fila = asyncio.Queue()
    estado["entrada"] = (asyncio.get_running_loop(), fila)
    estado["contou_em"] = time.monotonic()
    page = None
    try:
        page = await acordar()
        logger.info("Conta @%s aberta no painel (navegador único)%s", conta,
                    "" if page else " — dormindo até abrir")
        while not estado["stop"].is_set():
            # ── dormindo: acorda quando alguém olha (ou o painel pede)
            if estado.get("dormindo"):
                quer = tela.espectadores > 0 or estado.get("acordar")
                if quer and time.monotonic() >= estado.get("tentar_acordar_em", 0):
                    if espiada is not None and not espiada.done():
                        espiada.cancel()
                        await asyncio.gather(espiada, return_exceptions=True)
                    pg = await acordar()
                    if pg is not None:
                        page = pg
                        logger.info("@%s acordou", conta)
                    continue
                while not fila.empty():
                    fila.get_nowait()           # clique em conta dormindo não vale depois
                painel_aberto = time.monotonic() - _PAINEL["lista_em"] < PAINEL_ABERTO_S
                if (painel_aberto and (espiada is None or espiada.done())
                        and time.monotonic() - estado.get("contou_em", 0) > ESPIAR_A_CADA_S):
                    estado["contou_em"] = time.monotonic()
                    espiada = asyncio.ensure_future(espiar())
                await asyncio.sleep(0.5)
                continue
            # página fechou sem ninguém pedir (o navegador único caiu): dorme e
            # é recriada quando alguém abrir
            if page is None or page.is_closed():
                logger.warning("@%s: a página caiu; volta quando alguém abrir", conta)
                dormir_agora("página caiu")
                # cada conta espia num momento diferente depois de uma queda
                estado["contou_em"] = time.monotonic() + random.uniform(0, 60)
                continue
            sem = tela.sem_espectador_ha()
            pedido_dormir = estado.pop("dormir_ja", False) and tela.espectadores == 0
            if pedido_dormir or (sem > DORMIR_APOS_S and tela.espectadores == 0):
                dormir_agora("para abrir espaço" if pedido_dormir else f"{DORMIR_APOS_S // 60} min sem uso")
                await _seguro(page.close())
                estado["contou_em"] = time.monotonic()
                continue

            with JANELAS_LOCK:
                pedido_ir = estado.pop("ir_para", None)
                estado["destino"] = _destino_da_url(page.url)
            if pedido_ir and estado["destino"] != pedido_ir:
                _navegar(estado, page.goto(DESTINOS_MANUAIS[pedido_ir],
                                           wait_until="domcontentloaded", timeout=NAVEGACAO_MS))
            # reserva do bloqueio antes de carregar: blob:/data: não passam por ele
            if _fora_do_instagram(page.url) and not _navegando(estado):
                _aviso_bloqueio(estado, page.url)
                _navegar(estado, page.goto(DESTINOS_MANUAIS["direct"],
                                           wait_until="domcontentloaded", timeout=NAVEGACAO_MS))
            try:
                await tela.acompanhar()
            except Exception as e:
                _avisar_uma_vez(estado, "transmissao", "@%s: transmissão (%s)", conta, e)
            if (sem > ESTACIONAR_APOS_S and not _navegando(estado)
                    and urlparse(page.url).path.startswith("/direct/t/")):
                logger.info("@%s: ninguém olhando; conversa fechada para não marcar como vista", conta)
                _navegar(estado, page.goto(DESTINOS_MANUAIS["direct"],
                                           wait_until="domcontentloaded", timeout=NAVEGACAO_MS))
            await _processar_entradas(conta, page, estado, tela, fila)
            # com alguém olhando, o filtro de não lidas acompanha a lista; sem
            # ninguém, só a contagem importa (a lista do painel lê a cada 5 s)
            intervalo = 1.5 if tela.espectadores else 10
            if time.monotonic() - estado.get("pagina_em", 0) > intervalo and not _navegando(estado):
                await _ler_pagina(conta, page, estado)
    finally:
        if espiada is not None and not espiada.done():
            espiada.cancel()
        estado.pop("entrada", None)
        estado.pop("contexto", None)
        tela.encerrar()
        if ctx is not None:
            try:
                await asyncio.wait_for(ctx.close(), timeout=15)
            except Exception as e:
                logger.warning("@%s: o contexto do painel não fechou (%s)", conta, e)


def _limitar_acordadas(conta_nova, pouca_memoria=False):
    """
    (sob JANELAS_LOCK) No máximo MAX_ACORDADAS contas acordadas no painel; ao acordar
    mais uma, dorme a parada há mais tempo (sem ninguém olhando). Com a memória livre
    baixa, dorme uma a mais. Conta no meio de acordar já conta como acordada (senão duas
    acordando juntas passavam do máximo).
    """
    acordadas = [(e["tela"].sem_espectador_ha(), c) for c, e in JANELAS.items()
                 if c != conta_nova and e.get("embutida") and e.get("tela")
                 and (not e.get("dormindo") or e.get("acordando")) and not e.get("dormir_ja")]
    limite = MAX_ACORDADAS - 1
    if pouca_memoria:
        limite = max(0, limite - 1)
    paradas = sorted([x for x in acordadas
                      if JANELAS[x[1]]["tela"].espectadores == 0 and not JANELAS[x[1]].get("acordando")],
                     reverse=True)
    while len(acordadas) > limite and paradas:
        _, c = paradas.pop(0)
        JANELAS[c]["dormir_ja"] = True
        acordadas = [x for x in acordadas if x[1] != c]
        logger.info("@%s vai dormir para @%s acordar (máximo %d acordadas)", c, conta_nova, MAX_ACORDADAS)


async def _processar_entradas(conta, page, estado, tela, fila):
    """Cliques e teclas da fila, na ordem; depois de um clique, colhe o play()."""
    lote = []
    try:
        ev = await asyncio.wait_for(fila.get(), timeout=0.3)
        lote = [ev]
        while not fila.empty():
            lote.append(fila.get_nowait())
        for ev in lote:
            if (ev.get("t") == "tecla" and ev.get("k") == "Enter"
                    and estado.pop("segurar_enter", False)):
                _avisar(estado, "parte do texto não chegou: confira a mensagem e aperte Enter de novo")
                continue
            if ev.get("t") == "texto":
                estado.pop("aviso", None)
            if not await tela.aplicar(page, ev):
                await _evento_do_canal(page, estado, ev)
            if ev.get("t") == "mouse" and ev.get("a") == "up":
                agora_ms = time.time() * 1000
                estado["colher_midia_ate"] = time.monotonic() + 4
                estado["clique_ms"] = agora_ms
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        if any(x.get("t") == "texto" for x in lote):
            estado["segurar_enter"] = True
        _avisar_uma_vez(estado, "entrada", "@%s: entrada do painel falhou (%s)", conta, e)
    if time.monotonic() < estado.get("colher_midia_ate", 0):
        try:
            eventos = await asyncio.wait_for(page.evaluate(JS_MIDIA_DRENAR), timeout=1)
            # só o play() feito DEPOIS do clique: o que já tocava sozinho não vale
            desde = estado.get("clique_ms", 0) - 300
            _registrar_midias(estado, [e for e in eventos or [] if (e.get("t") or 0) >= desde])
        except Exception:
            pass


async def _ler_pagina(conta, page, estado):
    """Filtro de não lidas, contagem e o aviso de notificações (com prazo)."""
    estado["pagina_em"] = time.monotonic()
    try:
        r = await asyncio.wait_for(page.evaluate(JS_PAGINA, bool(estado.get("so_nao_lidas"))), timeout=2)
        if (r or {}).get("contagem") is not None:
            estado["contagem"] = r["contagem"]
            estado["contagem_em"] = datetime.now().astimezone().isoformat(timespec="seconds")
        if (r or {}).get("popup"):
            logger.info("@%s: aviso de notificações fechado (Agora não)", conta)
    except Exception:
        pass


async def _janela_manual(conta, estado):
    """
    Janela EXTERNA da conta (Reels do aquecimento, "abrir navegador" em Perfis): o
    perfil persistente numa janela visível, até o operador fechá-la ou o painel pedir. A
    do Chat ao vivo é `_janela_painel` (navegador único).
    """
    perfil = perfil_atual(conta)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=perfil, headless=False,
            args=["--disable-blink-features=AutomationControlled"])
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(DESTINOS_MANUAIS[estado["destino"]], wait_until="domcontentloaded",
                                timeout=30000)
            except Exception as e:
                # A janela fica: o operador tenta de novo nela mesma.
                logger.warning("@%s: não carreguei %s (%s)", conta, NOME_DESTINO[estado["destino"]], e)
            logger.info("Janela manual de @%s aberta (%s)", conta, estado["destino"])
            while not estado["stop"].is_set() and not page.is_closed():
                if estado.get("fechar_em") and time.monotonic() > estado["fechar_em"]:
                    logger.info("@%s: janela fechada no horário marcado", conta)
                    break
                with JANELAS_LOCK:
                    pedido = estado.pop("ir_para", None)
                    estado["destino"] = _destino_da_url(page.url)
                if pedido:
                    # Já está lá: só traz para frente, sem recarregar a conversa
                    # que o operador pode estar digitando.
                    if estado["destino"] != pedido and pedido == "reels":
                        # este Chromium não toca o áudio AAC do Instagram: o
                        # Reels vai leve (sem áudio, menor qualidade) como no
                        # navegador do aquecimento
                        if not estado.get("reels_leve_ligado"):
                            estado["reels_leve_ligado"] = True
                            await _seguro(reels_leve.ligar(context, page, estado.setdefault("reels_leve", {})))
                        _navegar(estado, _ir_ao_destino(page, pedido))
                    elif estado["destino"] != pedido:
                        _navegar(estado, _ir_ao_destino(page, pedido))
                    await _seguro(page.bring_to_front())
                await asyncio.sleep(0.4)
                if time.monotonic() - estado.get("pagina_em", 0) > 1.5 and not _navegando(estado):
                    await _ler_pagina(conta, page, estado)
        finally:
            try:
                await asyncio.wait_for(context.close(), timeout=15)
            except Exception as e:
                logger.error("@%s: o navegador não fechou em 15s (%s); encerrando o processo", conta, e)
                _matar_chromium(perfil)


# ── Aquecimento: Reels leve, todas as contas num navegador só ──────────────
# Janela pequena e visível, que passa para o próximo Reel quando o vídeo acaba,
# vídeo na menor qualidade e sem áudio (reels_leve.py), uma conta abrindo por
# vez: ativar todas juntas abriria 19 páginas de uma vez e travaria o computador
# (abrem de 3 em 3).
AQUECER_VIEWPORT = {"width": 420, "height": 760}
AQUECER_MAX_MIN = 12 * 60          # "fechar depois de" aceita até 12 h
AQUECER_ABRINDO_JUNTAS = 3         # 19 contas: ~6 min uma por vez, ~2 min de 3 em 3
AQUECER_PRAZO_ABRIR_S = 120        # uma página que trave não segura a fila
_AQ = {"abrindo": None, "n": 0, "fechando": None}


def _posicao_aquecimento():
    """Janelas em cascata, para nenhuma ficar escondida atrás da outra."""
    i = _AQ["n"] % 10
    _AQ["n"] += 1
    return (40 + i * 36, 20 + i * 28)


async def _janela_aquecimento(conta, estado):
    """Reels da conta numa janela do navegador do aquecimento."""
    nav = navegador_unico.AQUECIMENTO
    perfil = perfil_atual(conta)
    if _AQ["abrindo"] is None:
        _AQ["abrindo"] = asyncio.Semaphore(AQUECER_ABRINDO_JUNTAS)
    contagem = estado.setdefault("reels_leve", {})
    ctx = None
    try:
        espera = 0
        while True:
            try:
                ctx = await nav.novo_contexto(conta, perfil, AQUECER_VIEWPORT)
                break
            except PermissionError as e:
                if estado["stop"].is_set() or espera > 15 * 60:
                    raise
                _avisar(estado, f"aguardando a prospecção liberar @{conta}")
                logger.info("@%s: %s", conta, e)
                await asyncio.sleep(10)
                espera += 10
        estado.pop("aviso", None)
        await ctx.add_init_script(reels_leve.JS_MUDO)
        await ctx.add_init_script(JS_MESMA_ABA)

        async def proteger(pg):
            """Leve (menor qualidade, sem áudio) e sem sair do Instagram."""
            await _seguro(reels_leve.ligar(ctx, pg, contagem))
            try:
                await _bloquear_fora(ctx, pg, estado)
            except Exception as e:
                logger.warning("@%s: bloqueio de links externos não ligou (%s)", conta, e)

        async def abrir():
            pg = await nav.nova_pagina(ctx, posicao=_posicao_aquecimento())
            estado["pagina"] = pg
            await proteger(pg)
            # aba aberta pelo operador (Ctrl+clique) também fica leve e presa ao Instagram
            ctx.on("page", lambda nova: asyncio.ensure_future(_seguro(proteger(nova))))
            try:
                await _ir_ao_destino(pg, estado["destino"])
            except Exception as e:
                # a janela fica: o operador tenta de novo nela mesma
                logger.warning("@%s: não carreguei %s (%s)", conta, NOME_DESTINO[estado["destino"]], e)
            await asyncio.sleep(2)              # a próxima da fila espera esta assentar
            return pg

        # Fila de abertura com prazo: desiste se a conta for fechada (ou passar
        # da hora de fechar) enquanto espera, e uma página que trave não segura
        # a vez das outras para sempre.
        if not await _vez_de_abrir(estado):
            return
        try:
            page = await asyncio.wait_for(abrir(), AQUECER_PRAZO_ABRIR_S)
        except asyncio.TimeoutError:
            page = estado.get("pagina")
            logger.warning("@%s: a abertura passou de %d s; segue como está", conta, AQUECER_PRAZO_ABRIR_S)
            if page is None:
                raise
        finally:
            _AQ["abrindo"].release()
        estado["aberto_em"] = time.monotonic()
        if estado["destino"] == "reels":
            estado["conferir_reels_em"] = time.monotonic() + 20
        logger.info("Aquecimento de @%s aberto (Reels na menor qualidade, sem som)", conta)
        while not estado["stop"].is_set() and not page.is_closed():
            if estado.get("fechar_em") and time.monotonic() > estado["fechar_em"]:
                logger.info("@%s: aquecimento fechado no horário marcado", conta)
                break
            with JANELAS_LOCK:
                pedido = estado.pop("ir_para", None)
                estado["destino"] = _destino_da_url(page.url)
            if pedido:
                if pedido != estado["destino"]:
                    _navegar(estado, _ir_ao_destino(page, pedido))
                    estado["aberto_em"] = time.monotonic()
                await _seguro(page.bring_to_front())
            # reserva do bloqueio: blob:/data: não passam por ele
            if _fora_do_instagram(page.url) and not _navegando(estado):
                _aviso_bloqueio(estado, page.url)
                _navegar(estado, _ir_ao_destino(page, "reels"))
            # ficou na caixa do Direct (o caminho de entrada) porque a página
            # demorou com todas as contas abrindo juntas: mais uma tentativa
            if estado.get("conferir_reels_em") and time.monotonic() > estado["conferir_reels_em"]:
                estado.pop("conferir_reels_em", None)
                if estado["destino"] == "direct" and not _navegando(estado):
                    logger.info("@%s: ficou na caixa do Direct; levando ao Reels de novo", conta)
                    _navegar(estado, _ir_ao_destino(page, "reels"))
            await asyncio.sleep(0.5)
            # vídeo acabou (ou deu erro, ou travou): passa para o próximo Reel, com uma
            # pausa de pessoa
            if (estado["destino"] == "reels" and not _navegando(estado)
                    and time.monotonic() >= estado.get("ver_video_em", 0)):
                estado["ver_video_em"] = time.monotonic() + 2
                passou = await reels_leve.passar_se_acabou(
                    page, estado.setdefault("rolagem", {}),
                    lambda: (estado["stop"].is_set() or page.is_closed() or _navegando(estado)
                             or _destino_da_url(page.url) != "reels"))
                if passou is True:
                    estado["reels_vistos"] = estado.get("reels_vistos", 0) + 1
                elif passou == "travou":
                    _avisar(estado, "o Reel não passou sozinho (algum aviso aberto na janela?)")
            # o aviso de notificações aparece logo ao abrir: lê a página a cada
            # 3 s no primeiro minuto e a cada 30 s depois
            intervalo = 3 if time.monotonic() - estado.get("aberto_em", 0) < 60 else 30
            if time.monotonic() - estado.get("pagina_em", 0) > intervalo and not _navegando(estado):
                await _ler_pagina(conta, page, estado)
    finally:
        if ctx is not None:
            try:
                await asyncio.wait_for(ctx.close(), timeout=15)
            except Exception as e:
                logger.warning("@%s: o aquecimento não fechou (%s)", conta, e)
        _AQ["fechando"] = asyncio.ensure_future(_seguro(nav.fechar_se_vazio()))


async def _vez_de_abrir(estado):
    """
    Espera a vez na fila de abertura (AQUECER_ABRINDO_JUNTAS por vez). False: a conta
    foi fechada, ou passou da hora de fechar, antes da vez.
    """
    while True:
        if estado["stop"].is_set() or (estado.get("fechar_em") and time.monotonic() > estado["fechar_em"]):
            return False
        try:
            await asyncio.wait_for(_AQ["abrindo"].acquire(), timeout=1)
            return True
        except asyncio.TimeoutError:
            continue


def _ir_ao_destino(page, destino):
    """A navegação para um destino; o Reels entra pelo menu (reels_leve)."""
    if destino == "reels":
        return reels_leve.ir_ao_reels(page, DESTINOS_MANUAIS["direct"], DESTINOS_MANUAIS["reels"])
    return page.goto(DESTINOS_MANUAIS[destino], wait_until="domcontentloaded", timeout=NAVEGACAO_MS)


def _matar_chromium(perfil, so_fora_da_tela=False):
    """Encerra Chromium preso neste perfil (pelo --user-data-dir exato)."""
    try:
        import psutil
    except Exception:
        return 0
    alvo = os.path.normcase(os.path.abspath(perfil)) if perfil else None
    mortos = 0
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info["name"] or "").lower() not in ("chrome.exe", "chromium.exe", "chrome"):
                continue
            cmd = proc.info["cmdline"] or []
            dirs = [c.split("=", 1)[1].strip('"') for c in cmd if c.startswith("--user-data-dir=")]
            if not dirs:
                continue
            d = os.path.normcase(os.path.abspath(dirs[0]))
            if alvo is not None and d != alvo:
                continue
            if alvo is None and not (_config is not None and
                                     d.startswith(os.path.normcase(str(_config.IG_SESSOES_DIR)))):
                continue
            if so_fora_da_tela and janela_remota.MARCA_FORA_DA_TELA not in cmd:
                continue
            proc.kill()
            mortos += 1
        except Exception:
            continue
    return mortos


def _rodar_janela_manual(conta, estado):
    try:
        asyncio.run(_janela_manual(conta, estado))
    except Exception as e:
        logger.error("Janela manual de @%s encerrou com erro: %s", conta, e)
    finally:
        estado["stop"].set()
        if estado.get("tela"):
            estado["tela"].encerrar()
        with JANELAS_LOCK:
            if JANELAS.get(conta) is estado:
                JANELAS.pop(conta, None)


FALHAS_PAINEL = {}          # conta -> (motivo, instante): a janela some, o motivo fica


def _iniciar_no_navegador_unico(conta, estado, nav=None, tarefa=None):
    """A janela do painel (ou do aquecimento) é uma tarefa no loop do navegador."""
    def fim(fut):
        try:
            erro = fut.exception()
        except BaseException as e:          # cancelada: CancelledError não é Exception
            erro = e
        if erro and not estado["stop"].is_set():
            # fechar a conta enquanto ela esperava a prospecção não é falha
            logger.error("Janela de @%s no painel encerrou com erro: %s", conta, erro)
            FALHAS_PAINEL[conta] = (str(erro)[:200] or type(erro).__name__, time.time())
        estado["stop"].set()
        if estado.get("tela"):
            estado["tela"].encerrar()
        with JANELAS_LOCK:
            if JANELAS.get(conta) is estado:
                JANELAS.pop(conta, None)
    nav = nav or navegador_unico.NAVEGADOR
    nav.agendar((tarefa or _janela_painel)(conta, estado)).add_done_callback(fim)


def _contas_vistas_no_painel():
    """(sob JANELAS_LOCK) Contas que o operador está OLHANDO no painel agora."""
    return {c for c, e in JANELAS.items()
            if e.get("embutida") and e.get("tela") and e["tela"].espectadores > 0}


def _contas_ocupadas():
    """
    (sob JANELAS_LOCK) Contas que a prospecção, o lote e o follow-up NÃO usam agora:
    pasta presa numa janela externa, ou conta que o operador está OLHANDO no painel.
    """
    return set(PERFIS_ABERTOS) | _contas_vistas_no_painel()


def _contas_do_bot():
    """
    Contas que a prospecção ou o lote estão usando agora (chamar sob JANELAS_LOCK).
    """
    return set(CONTAS_DO_BOT) if BOT_RODANDO else set()


def _fechar_em(minutos):
    """`fechar_apos_min` do pedido em instante (monotonic), ou None."""
    if isinstance(minutos, bool):
        return None
    try:
        m = int(minutos)
    except (TypeError, ValueError, OverflowError):
        return None
    return time.monotonic() + m * 60 if 1 <= m <= AQUECER_MAX_MIN else None


def _pedido_janela_manual(conta, ativo, destino, embutida=False, so_nao_lidas=None,
                          fechar_apos_min=None):
    """Abre, leva para outro destino ou fecha a janela manual de uma conta."""
    if not conta or not isinstance(ativo, bool) or not isinstance(destino, str) \
            or destino not in DESTINOS_MANUAIS:
        return {"ok": False, "erro": "informe a conta, ativo como booleano e um destino válido"}, 400
    nome = NOME_DESTINO[destino]
    embutida = bool(embutida)

    with JANELAS_LOCK:
        estado = JANELAS.get(conta)
        if not ativo:
            if estado:
                estado["stop"].set()
                return {"ok": True, "active": False, "conta": conta,
                        "mensagem": f"Fechando a janela de @{conta}."}, 202
            return {"ok": True, "active": False, "conta": conta}, 200
        if estado:
            if estado["stop"].is_set():
                return {"ok": False, "erro": f"A janela de @{conta} está fechando; tente em instantes."}, 409
            if bool(estado.get("embutida")) != embutida:
                onde = "no Chat ao vivo" if estado.get("embutida") else "numa janela externa"
                return {"ok": False, "erro": f"@{conta} já está aberta {onde}. Feche lá primeiro."}, 409
            if destino == "inicio":
                # "Abrir o navegador" de uma janela que já existe só a traz para
                # frente: levá-la ao início apagaria a conversa em andamento.
                if estado.get("destino") in DESTINOS_MANUAIS:
                    estado["ir_para"] = estado["destino"]
                return {"ok": True, "active": True, "conta": conta, "destino": estado.get("destino"),
                        "mensagem": f"Trazendo a janela de @{conta} para frente."}, 200
            estado["ir_para"] = destino
            if destino == "reels" and _fechar_em(fechar_apos_min):
                estado["fechar_em"] = _fechar_em(fechar_apos_min)
            if embutida and estado.get("dormindo"):
                estado["acordar"] = True
            if so_nao_lidas is not None:
                estado["so_nao_lidas"], estado["pagina_em"] = bool(so_nao_lidas), 0
            mensagem = (f"Trazendo a janela de @{conta} para frente."
                        if estado.get("destino") == destino
                        else f"Levando a janela de @{conta} para {nome}.")
            return {"ok": True, "active": True, "conta": conta, "destino": destino,
                    "mensagem": mensagem}, 200

    perfil = perfil_atual(conta)
    if not perfil or not os.path.exists(perfil):
        return {"ok": False, "erro": f"Não encontrei uma sessão salva para @{conta}."}, 409

    estado = {
        "stop": threading.Event(),
        "destino": destino,
        "embutida": embutida,
        "so_nao_lidas": bool(so_nao_lidas),
        "iniciado_em": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if embutida:
        estado["tela"] = janela_remota.TelaRemota()
    # Reels fora do painel = aquecimento: navegador próprio, leve (ver acima)
    aquecimento = not embutida and destino == "reels"
    estado["modelo"] = "painel" if embutida else ("aquecimento" if aquecimento else "manual")
    if aquecimento:
        estado["aquecimento"] = True
        if _fechar_em(fechar_apos_min):
            estado["fechar_em"] = _fechar_em(fechar_apos_min)
    with JANELAS_LOCK:
        # O perfil não aceita dois navegadores: a conta que a prospecção ou o
        # lote estão usando não abre em janela externa. No painel abre: ele não
        # usa a pasta (navegador único, cópia dos cookies).
        if not embutida and conta in _contas_do_bot():
            return {"ok": False,
                    "erro": f"@{conta} está prospectando agora. Espere o disparo terminar "
                            f"para abrir {nome} dela."}, 409
        if conta in JANELAS:
            return {"ok": False, "erro": f"O perfil de @{conta} já está em uso."}, 409
        JANELAS[conta] = estado
    if embutida or aquecimento:
        try:
            if aquecimento:
                _iniciar_no_navegador_unico(conta, estado, navegador_unico.AQUECIMENTO, _janela_aquecimento)
            else:
                _iniciar_no_navegador_unico(conta, estado)
        except Exception as e:
            # sem isto a conta ficava em JANELAS para sempre ("perfil já em uso")
            with JANELAS_LOCK:
                if JANELAS.get(conta) is estado:
                    JANELAS.pop(conta, None)
            return {"ok": False, "erro": f"não deu para abrir @{conta} no painel: {e}"}, 500
    else:
        threading.Thread(target=_rodar_janela_manual, args=(conta, estado),
                         name=f"janela-manual-ig-{conta}", daemon=True).start()
    return {"ok": True, "active": True, "conta": conta, "destino": destino,
            "mensagem": f"Abrindo {nome} autenticado de @{conta}."}, 202


_NICHO_OK = re.compile(r"[\w][\w \-]{0,59}")


def _nicho_valido(nicho):
    return isinstance(nicho, str) and bool(_NICHO_OK.fullmatch(nicho.strip()))


def _conta_do_pedido(body):
    return (_config.ig_conta_normalizada(body.get("conta", ""))
            if _config is not None else "")


# Guarda de Host contra DNS rebinding (ver servidor_local.py): vale para todas
# as rotas, inclusive as da prospecção.
_servidor_local.instalar_guarda_host(app)


def _pedido_local():
    """Abrir, ver e operar uma conta logada: só para quem está nesta máquina."""
    return _servidor_local.desta_maquina(request.remote_addr)


def _recusa_remota():
    return jsonify({"ok": False, "erro": "só nesta máquina"}), 403


@app.route('/api/aquecimento/status')
@app.route('/api/janela-manual/status')
def api_janela_manual_status():
    # o painel pede com ?painel=1 quando a lista do Direct está na tela: é o
    # que libera a espiada das contas dormindo
    if request.args.get("painel") == "1" and _pedido_local():
        _PAINEL["lista_em"] = time.monotonic()
    with JANELAS_LOCK:
        active = [
            {"conta": conta, "iniciado_em": estado["iniciado_em"],
             "destino": estado.get("destino"), "embutida": bool(estado.get("embutida")),
             "so_nao_lidas": bool(estado.get("so_nao_lidas")),
             "contagem": estado.get("contagem"), "contagem_em": estado.get("contagem_em"),
             "aviso": _aviso_vigente(estado),
             "aquecimento": bool(estado.get("aquecimento")),
             "modelo": estado.get("modelo"),
             "reels_vistos": estado.get("reels_vistos"),
             # quantas versões pesadas e trilhas de áudio o Reels leve cortou
             "leve": dict(estado["reels_leve"]) if estado.get("reels_leve") else None,
             "fecha_em_min": (max(0, round((estado["fechar_em"] - time.monotonic()) / 60))
                              if estado.get("fechar_em") else None),
             "dormindo": bool(estado.get("dormindo"))}
            for conta, estado in sorted(JANELAS.items())
        ]
        em_uso = sorted(_contas_do_bot())
        vistas = sorted(_contas_vistas_no_painel())
        ocupadas = sorted(_contas_ocupadas())
        abertas = sorted(JANELAS)
    agora = time.time()
    # `em` deixa o painel avisar de novo a MESMA falha quando ela se repete
    falhas = {c: {"motivo": m, "em": t} for c, (m, t) in list(FALHAS_PAINEL.items())
              if agora - t < 180}
    return jsonify({"active": active, "prospeccao_rodando": BOT_RODANDO,
                    "contas_prospectando": em_uso, "contas_vistas": vistas,
                    "contas_ocupadas": ocupadas, "contas_abertas": abertas,
                    "falhas": falhas, "perfis_abertos": sorted(PERFIS_ABERTOS)})


@app.route('/api/janela-manual', methods=['POST'])
def api_janela_manual():
    if not _pedido_local():
        return _recusa_remota()
    body = request.get_json(silent=True) or {}
    resposta, codigo = _pedido_janela_manual(
        _conta_do_pedido(body), body.get("ativo"), body.get("destino"),
        embutida=body.get("embutida") is True,
        so_nao_lidas=body.get("so_nao_lidas") if isinstance(body.get("so_nao_lidas"), bool) else None,
        fechar_apos_min=body.get("fechar_apos_min"))
    return jsonify(resposta), codigo


@app.route('/api/aquecimento/todas', methods=['POST'])
def api_aquecimento_todas():
    """
    Abre o aquecimento das `contas` pedidas (o painel manda as que têm sessão) ou fecha
    TODAS as janelas de aquecimento. Escolhe pelo modelo da janela, não pela página
    aberta: uma janela de aquecimento em que o operador clicou num perfil continua sendo
    de aquecimento, e a de "abrir perfil" não é.
    """
    if not _pedido_local():
        return _recusa_remota()
    body = request.get_json(silent=True) or {}
    ativo = body.get("ativo")
    if not isinstance(ativo, bool):
        return jsonify({"ok": False, "erro": "ativo precisa ser true ou false"}), 400
    abertas, puladas = [], []
    if ativo:
        contas = body.get("contas")
        if not isinstance(contas, list):
            return jsonify({"ok": False, "erro": "contas precisa ser uma lista"}), 400
        for bruta in contas[:200]:
            conta = _config.ig_conta_normalizada(str(bruta)) if _config is not None else ""
            if not conta:
                continue
            with JANELAS_LOCK:
                atual = JANELAS.get(conta)
            if atual is not None:
                if atual.get("modelo") != "aquecimento":
                    puladas.append({"conta": conta, "motivo": "já aberta em outra janela"})
                continue
            resposta, _ = _pedido_janela_manual(conta, True, "reels",
                                                fechar_apos_min=body.get("fechar_apos_min"))
            if resposta.get("ok"):
                abertas.append(conta)
            else:
                puladas.append({"conta": conta, "motivo": resposta.get("erro") or "recusada"})
    else:
        with JANELAS_LOCK:
            for conta, estado in JANELAS.items():
                if estado.get("modelo") == "aquecimento" and not estado["stop"].is_set():
                    estado["stop"].set()
                    abertas.append(conta)
    verbo = "abrindo" if ativo else "fechando"
    if abertas:
        mensagem = (f"{verbo} {len(abertas)} conta(s)"
                    + (f" ({AQUECER_ABRINDO_JUNTAS} por vez)" if ativo else "")
                    + (f"; {len(puladas)} pulada(s)" if puladas else ""))
    else:
        mensagem = ("nenhuma conta para ativar" + (f" ({len(puladas)} pulada(s))" if puladas else "")
                    if ativo else "nenhum aquecimento aberto")
    return jsonify({"ok": bool(abertas) or not puladas, "mensagem": mensagem,
                    "contas": abertas, "puladas": puladas})


def _janela_embutida(conta):
    conta = _config.ig_conta_normalizada(conta) if _config is not None else ""
    with JANELAS_LOCK:
        estado = JANELAS.get(conta)
    return estado if estado and estado.get("embutida") else None


@app.route('/api/janela-manual/<conta>/tela')
def api_janela_manual_tela(conta):
    """A página da conta como MJPEG, enquanto alguém estiver olhando."""
    if not _pedido_local():
        return _recusa_remota()
    estado = _janela_embutida(conta)
    if not estado:
        return jsonify({"ok": False, "erro": "esta conta não está aberta no painel"}), 404
    return app.response_class(estado["tela"].quadros(),
                              mimetype="multipart/x-mixed-replace; boundary=quadro",
                              headers={"Cache-Control": "no-store"})


@app.route('/api/janela-manual/<conta>/entrada', methods=['POST'])
def api_janela_manual_entrada(conta):
    """Cliques e teclas do operador, na ordem em que ele fez."""
    if not _pedido_local():
        return _recusa_remota()
    estado = _janela_embutida(conta)
    entrada = estado.get("entrada") if estado else None
    if not entrada:
        return jsonify({"ok": False, "erro": "esta conta não está aberta no painel"}), 409
    eventos = (request.get_json(silent=True) or {}).get("eventos")
    if not isinstance(eventos, list):
        return jsonify({"ok": False, "erro": "eventos precisa ser uma lista"}), 400
    loop, fila = entrada
    try:
        for ev in eventos[:200]:
            if isinstance(ev, dict):
                loop.call_soon_threadsafe(fila.put_nowait, ev)
    except RuntimeError:
        # a janela fechou entre a leitura e o envio (laço já encerrado)
        return jsonify({"ok": False, "erro": "a janela desta conta acabou de fechar"}), 409
    return jsonify({"ok": True})


@app.route('/api/janela-manual/<conta>/midia')
def api_janela_manual_midia_evento(conta):
    """Último áudio/vídeo que o operador deu play na janela (o painel toca)."""
    if not _pedido_local():
        return _recusa_remota()
    estado = _janela_embutida(conta)
    if not estado:
        return jsonify({"ok": False, "erro": "esta conta não está aberta no painel"}), 404
    return jsonify({"ok": True, "midia": estado.get("midia_evento"),
                    "epoca": estado.get("iniciado_em")})


@app.route('/api/janela-manual/<conta>/midia/<mid>')
def api_janela_manual_midia_arquivo(conta, mid):
    """
    O arquivo de uma mídia que a página tocou. Só endereços que a PRÓPRIA página tocou
    (guardados por id) e só do CDN do Instagram — o painel manda o id, nunca uma URL.
    Baixa pela sessão da conta (mesmos cookies da página).
    """
    if not _pedido_local():
        return _recusa_remota()
    estado = _janela_embutida(conta)
    m = (estado or {}).get("midias", {}).get(str(mid))
    entrada, contexto = (estado or {}).get("entrada"), (estado or {}).get("contexto")
    if not m or not entrada or contexto is None:
        return jsonify({"ok": False, "erro": "mídia não encontrada"}), 404
    loop = entrada[0]

    async def baixar():
        # redirecionamento à mão: cada salto é conferido ANTES de o pedido sair
        # (seguir sozinho deixaria um salto para fora do CDN acontecer)
        url = m["url"]
        for _ in range(4):
            r = await contexto.request.get(url, timeout=30000, max_redirects=0)
            try:
                if r.status in (301, 302, 303, 307, 308):
                    destino = urljoin(url, r.headers.get("location", ""))
                    if not _host_de_midia(destino):
                        return None, None
                    url = destino
                    continue
                if not r.ok:
                    return None, None
                tamanho = int(r.headers.get("content-length") or 0)
                if tamanho > MIDIA_MAX_BYTES:
                    return None, "grande"
                corpo = await r.body()
                if len(corpo) > MIDIA_MAX_BYTES:
                    return None, "grande"
                return corpo, r.headers.get("content-type", "")
            finally:
                # sem isto o corpo ficava no Playwright até o contexto fechar —
                # e o contexto da conta agora vive o dia todo
                await _seguro(r.dispose())
        return None, None

    fut = asyncio.run_coroutine_threadsafe(baixar(), loop)
    try:
        corpo, tipo = fut.result(timeout=35)
    except Exception:
        fut.cancel()
        # sem detalhe: o erro traria a URL assinada da mídia do lead
        return jsonify({"ok": False, "erro": "não consegui baixar a mídia do Instagram"}), 502
    if tipo == "grande":
        return jsonify({"ok": False, "erro": "mídia grande demais para o painel (acima de 50 MB)"}), 413
    if not corpo:
        return jsonify({"ok": False, "erro": "o Instagram não entregou a mídia"}), 502
    tipo = tipo.split(";")[0].strip().lower()
    if not (tipo.startswith("audio/") or tipo.startswith("video/")):
        tipo = "audio/mp4" if m["tipo"] == "audio" else "video/mp4"
    return app.response_class(corpo, mimetype=tipo,
                              headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.route('/api/aquecimento', methods=['POST'])
def api_aquecimento():
    if not _pedido_local():
        return _recusa_remota()
    body = request.get_json(silent=True) or {}
    resposta, codigo = _pedido_janela_manual(_conta_do_pedido(body), body.get("ativo"), "reels")
    return jsonify(resposta), codigo


@app.route('/api/perfil/abrir', methods=['POST'])
def api_abrir_perfil():
    if not _pedido_local():
        return _recusa_remota()
    resposta, codigo = _pedido_janela_manual(
        _conta_do_pedido(request.get_json(silent=True) or {}), True, "inicio")
    return jsonify(resposta), codigo

@app.route('/api/iniciar', methods=['POST'])
def api_iniciar_bot():
    global BOT_RODANDO, NICHO_ATUAL, MENSAGEM_ATUAL, MODELO_ATUAL, STOP_BOT, CONTA_ATUAL, CONTAS_FILA

    # disparar DM pela conta é a ação mais sensível do bot: só desta máquina
    if not _pedido_local():
        return _recusa_remota()
    if BOT_RODANDO:
        return _ocupado_rodando()

    dados = request.json
    if not dados:
        return jsonify({"status": "erro", "mensagem": "Nenhum dado JSON recebido"}), 400

    # em variáveis locais: as globais são lidas pelo disparo em andamento, e só
    # mudam depois de a reserva deste pedido dar certo
    nicho = dados.get('nicho', 'Padrao')
    if not _nicho_valido(nicho):
        return jsonify({"status": "erro", "mensagem": "nicho inválido (só letras, números, "
                                                      "espaço e hífen)"}), 400
    nicho = nicho.strip()
    mensagem = dados.get('mensagem_ia', '')
    modelo = dados.get('nome_modelo', 'Gerado pelo LeadForge')

    if not mensagem:
        return jsonify({"status": "erro", "mensagem": "Faltou a mensagem_ia"}), 400

    # A CONTA define qual perfil de Chromium abrir, e faltava aqui.
    pedida_bruta = str(dados.get("conta") or "").strip().lower()
    if pedida_bruta in ("", "todas", "*"):
        fila = contas_do_rodizio()
        # conta numa janela externa ou sendo vista no painel fica fora do
        # rodízio (`_contas_ocupadas`); as outras prospectam normalmente
        with JANELAS_LOCK:
            ja_rodando = BOT_RODANDO
            ocupadas = _contas_ocupadas()
            abertas = [c["conta"] for c in fila if c["conta"] in ocupadas]
            fila = [c for c in fila if c["conta"] not in ocupadas]
            if fila and not ja_rodando:
                _reservar_contas_do_bot(c["conta"] for c in fila)
        if ja_rodando:
            return _ocupado_rodando()
        if abertas:
            logger.info("Fora do rodizio por estarem abertas no painel: %s",
                        ", ".join("@" + c for c in abertas))
        if not fila:
            return jsonify({"status": "erro",
                            "mensagem": "nenhuma conta de Instagram disponivel: "
                                        "sem sessao salva, teto das ultimas 24h batido"
                                        + (" ou aberta no painel" if abertas else "")}), 409
        NICHO_ATUAL, MENSAGEM_ATUAL, MODELO_ATUAL = nicho, mensagem, modelo
        CONTAS_FILA = fila
        CONTA_ATUAL = CONTAS_FILA[0]["conta"]
        STOP_BOT = False
        try:
            os.makedirs(os.path.join(PERFIS_DIR, NICHO_ATUAL), exist_ok=True)
        except Exception:
            _liberar_contas_do_bot()
            raise
        _iniciar_reservado(run_async_bot)
        resumo = ", ".join(f"@{c['conta']} ({c['restam']})" for c in CONTAS_FILA)
        logger.info("🚀 Prospeccao no nicho %s com %d conta(s) EM PARALELO: %s",
                    NICHO_ATUAL, len(CONTAS_FILA), resumo)
        return jsonify({"status": "sucesso",
                        "mensagem": f"Bot iniciado com {len(CONTAS_FILA)} conta(s) em paralelo",
                        "contas": CONTAS_FILA}), 200

    conta_pedida = ""
    if dados.get("conta"):
        conta_pedida = (_config.ig_conta_normalizada(dados["conta"])
                        if _config is not None else str(dados["conta"]).lstrip("@").lower())
        if not conta_pedida:
            return jsonify({"status": "erro",
                            "mensagem": f"conta invalida: {dados.get('conta')!r}"}), 400
        perfil = (str(_config.ig_perfil(conta_pedida)) if _config is not None
                  else SESSION_DIR)
        if not os.path.exists(perfil):
            return jsonify({"status": "erro",
                            "mensagem": f"@{conta_pedida} nao tem sessao salva. "
                                        f"Rode: python -X utf8 senders/dm_bot_login.py "
                                        f"{conta_pedida}",
                            "perfil": perfil}), 409
    recusa = _reservar_ou_recusar([conta_pedida] if conta_pedida else [])
    if recusa:
        return recusa
    NICHO_ATUAL, MENSAGEM_ATUAL, MODELO_ATUAL = nicho, mensagem, modelo
    CONTAS_FILA = []
    CONTA_ATUAL = conta_pedida

    STOP_BOT = False
    # Garante que a pasta do nicho existe
    try:
        os.makedirs(os.path.join(PERFIS_DIR, NICHO_ATUAL), exist_ok=True)
    except Exception:
        _liberar_contas_do_bot()
        raise

    _iniciar_reservado(run_async_bot)

    logger.info(f"🚀 Comando recebido! Iniciando prospecção no nicho: {NICHO_ATUAL}, "
                f"remetente {('@' + CONTA_ATUAL) if CONTA_ATUAL else 'perfil padrao'}")
    logger.info(f"Perfil em uso: {perfil_atual()}")
    return jsonify({"status": "sucesso", "mensagem": "Bot iniciado em segundo plano"}), 200


@app.route('/api/parar', methods=['POST'])
def api_parar():
    """Só desta máquina — ver `_pedido_local`. Aborta a operação em andamento."""
    global STOP_BOT
    if not _pedido_local():
        return _recusa_remota()
    if not BOT_RODANDO:
        return jsonify({"status": "ok", "mensagem": "nada em execução"}), 200
    STOP_BOT = True
    logger.info("🛑 Parada solicitada pelo painel. Encerrando após a etapa atual...")
    return jsonify({"status": "sucesso",
                    "mensagem": "parada solicitada; o bot encerra após a etapa atual"}), 200


@app.route('/api/estado', methods=['GET'])
def api_estado():
    """Estado e log ao vivo, para o painel mostrar o que está acontecendo."""
    return jsonify({
        "rodando": BOT_RODANDO,
        "parando": STOP_BOT,
        "perfis_abertos": sorted(PERFIS_ABERTOS),
        "nicho": NICHO_ATUAL,
        "conta": CONTA_ATUAL or None,
        "rodizio": [c["conta"] for c in CONTAS_FILA],
        "perfil": perfil_atual(),
        "modelo": MODELO_ATUAL,
        "estatisticas": estatisticas,
        "limite_diario": LIMITE_DIARIO,
        "log": _ultimas_linhas_log(60),
    }), 200


def _ultimas_linhas_log(n=60):
    """As últimas linhas do log do bot, mais novas primeiro."""
    try:
        caminho = os.path.join(os.path.dirname(BASE_DIR), '..', 'log_prospeccao.txt')
        caminho = os.path.abspath(caminho)
        if not os.path.exists(caminho):
            caminho = os.path.join(BASE_DIR, 'log_prospeccao.txt')
        with open(caminho, 'r', encoding='utf-8', errors='replace') as f:
            linhas = f.read().splitlines()
        return linhas[-n:][::-1]
    except Exception as e:
        return [f"(não consegui ler o log: {e})"]


@app.route('/api/enviar_lote', methods=['POST'])
def api_enviar_lote():
    """
    ENDPOINT ALVO (clinica_prospector). Recebe uma lista de alvos específicos, cada um
    com sua própria mensagem personalizada, e dispara UMA mensagem única por lead
    (personalização real evita bloqueio).
    """
    global BOT_RODANDO, LOTE_ALVOS, NICHO_ATUAL, STOP_BOT, CONTA_ATUAL

    if not _pedido_local():
        return _recusa_remota()
    if BOT_RODANDO:
        return _ocupado_rodando()

    dados = request.json
    if not dados:
        return jsonify({"status": "erro", "mensagem": "Nenhum dado JSON recebido"}), 400

    alvos = dados.get("alvos") if isinstance(dados, dict) else dados
    if not isinstance(alvos, list) or not alvos:
        return jsonify({"status": "erro",
                        "mensagem": "Envie 'alvos': [{username, mensagem}]"}), 400

    limpos = []
    for a in alvos:
        if not isinstance(a, dict):
            continue
        u = str(a.get("username", "")).lstrip("@").strip()
        m = str(a.get("mensagem", "")).strip()
        # segurança: a msg vai por .format() adiante; escapa chaves literais
        m = m.replace("{", "{{").replace("}", "}}")
        if u and m:
            limpos.append({"username": u, "mensagem": m})

    if not limpos:
        return jsonify({"status": "erro",
                        "mensagem": "Nenhum alvo válido (precisa username + mensagem)"}), 400

    nicho = (dados.get("nicho") if isinstance(dados, dict) else None) or "clinicas"
    if not _nicho_valido(nicho):
        return jsonify({"status": "erro", "mensagem": "nicho inválido (só letras, números, "
                                                      "espaço e hífen)"}), 400
    nicho = nicho.strip()

    # Conta REMETENTE do lote. Sem ela, vale o perfil padrao -- o que mantem
    # funcionando quem ainda nao separou as sessoes por conta. Em variável
    # local: CONTA_ATUAL é lida pelo lote em andamento e só muda depois da reserva.
    conta = ""
    if isinstance(dados, dict) and dados.get("conta"):
        conta = (_config.ig_conta_normalizada(dados["conta"])
                 if _config is not None else str(dados["conta"]).lstrip("@").lower())
        if not conta:
            return jsonify({"status": "erro",
                            "mensagem": f"conta invalida: {dados.get('conta')!r}"}), 400
        perfil = perfil_atual(conta)
        if not os.path.exists(perfil):
            # Recusar aqui poupa 20s de navegador para descobrir o obvio, e diz
            # o comando exato que resolve.
            return jsonify({"status": "erro",
                            "mensagem": f"@{conta} nao tem sessao salva. "
                                        f"Rode: python -X utf8 senders/dm_bot_login.py "
                                        f"{conta}",
                            "perfil": perfil}), 409

    # Só a conta REMETENTE precisa estar livre: com outra conta aberta no
    # painel, o lote (e o follow-up, que passa por aqui) segue normalmente.
    recusa = _reservar_ou_recusar([conta] if conta else [])
    if recusa:
        return recusa
    NICHO_ATUAL, CONTA_ATUAL = nicho, conta
    LOTE_ALVOS = limpos
    STOP_BOT = False
    try:
        os.makedirs(os.path.join(PERFIS_DIR, NICHO_ATUAL), exist_ok=True)
    except Exception:
        _liberar_contas_do_bot()
        raise
    _iniciar_reservado(run_lote)

    logger.info(f"🚀 Lote alvo recebido: {len(limpos)} clínica(s), nicho {NICHO_ATUAL}, "
                f"remetente {('@' + CONTA_ATUAL) if CONTA_ATUAL else 'perfil padrao'}")
    logger.info(f"Perfil do lote: {perfil_atual()}")
    return jsonify({"status": "sucesso",
                    "mensagem": f"Lote iniciado ({len(limpos)} alvos)",
                    "conta": CONTA_ATUAL or None,
                    "total": len(limpos)}), 200

# ==========================================
# ====== 2. FUNÇÕES AUXILIARES =============
# ==========================================

def get_caminho(arquivo):
    return os.path.join(PERFIS_DIR, NICHO_ATUAL, arquivo)

def remover_acentos(texto):
    if not texto: return ""
    return ''.join(c for c in unicodedata.normalize('NFD', str(texto)) if unicodedata.category(c) != 'Mn')

def ler_linhas(caminho):
    if not os.path.exists(caminho): return []
    with open(caminho, 'r', encoding='utf-8') as f: return [l.strip() for l in f.read().splitlines() if l.strip()]

def registrar_acao(username, status="SENT", conta=None):
    """Registra o envio no rastreio local. NAO manda nada para fora da maquina."""
    global estatisticas
    conta = conta or CONTA_ATUAL

    if status == "SENT":
        # Primeiro de tudo: a outra conta, trabalhando em paralelo, pode topar
        # com este @ daqui a segundos, antes de o arquivo ser relido.
        if _historico is not None:
            _historico.anotar(username, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                              f"perfis/{NICHO_ATUAL}")

        estatisticas['hoje'] += 1
        estatisticas['total'] += 1

        # consome uma vaga do teto de 24h da conta remetente
        if _limites is not None and conta:
            try:
                _limites.registrar_por_identificador("instagram", conta, destino=username)
            except Exception as e:
                logger.warning("[TETO] nao deu para registrar o envio (%s)", e)

        logger.info("--> SUCESSO! @%s -> @%s", conta or "perfil padrao", username)

        # Rastreio CRM local
        cam_rastreio = get_caminho('rastreio_dms.json')
        try:
            with open(cam_rastreio, 'r', encoding='utf-8') as f: r = json.load(f)
        except: r = {}

        r[username] = {"modelo": MODELO_ATUAL, "data": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "respondeu": False, "converteu": False}
        with open(cam_rastreio, 'w', encoding='utf-8') as f:
            json.dump(r, f, indent=4, ensure_ascii=False)

        # 3. Adiciona seguidores na fila
        fontes = ler_linhas(get_caminho('fontes_alvo.txt'))
        usadas = ler_linhas(get_caminho('fontes_usadas.txt'))
        if username not in fontes and username not in usadas:
            with open(get_caminho('fontes_alvo.txt'), 'a', encoding='utf-8') as f: f.write(f"{username}\n")
            logger.info(f"[ARANHA] @{username} salvo na fila de Fontes.")

    elif status == "REJECTED":
        estatisticas['rejeitados'] += 1

    # Salva no arquivo de processados para nunca mais abordar
    with open(get_caminho('processados.txt'), 'a', encoding='utf-8') as f:
        f.write(f"{username}|{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

async def delay(min_s=2, max_s=5):
    for _ in range(int(random.uniform(min_s, max_s) * 10)):
        if STOP_BOT: return
        await asyncio.sleep(0.1)

# ==========================================
# ====== 3. MOTOR PLAYWRIGHT ===============
# ==========================================

def _reservar_contas_do_bot(contas):
    """Marca o bot como rodando e as contas que ele vai usar (sob JANELAS_LOCK)."""
    global BOT_RODANDO
    BOT_RODANDO = True
    CONTAS_DO_BOT.clear()
    CONTAS_DO_BOT.update(contas)


def _reservar_ou_recusar(contas):
    """
    Confere e reserva NUM passo só, sob o mesmo lock da janela manual: conferir aqui e
    marcar BOT_RODANDO só dentro da thread deixava uma janela abrir no meio, no mesmo
    perfil. Devolve a resposta de recusa, ou None.
    """
    with JANELAS_LOCK:
        if BOT_RODANDO:
            return _ocupado_rodando()
        ocupadas = _contas_ocupadas()
        abertas = [c for c in contas if c in ocupadas]
        if abertas:
            # `ocupada`: o follow-up adia sem gastar tentativa (ver follow_up.py)
            return jsonify({"status": "erro", "ocupada": True,
                            "mensagem": f"@{abertas[0]} está aberta no painel (ou numa janela "
                                        "manual). Feche-a ou envie por outra conta."}), 409
        _reservar_contas_do_bot(contas)
    return None


def _ocupado_rodando():
    # `ocupada`: espera, não falha — o follow-up não gasta tentativa nem grava
    # toque `failed` por um lote ou uma prospecção que está rodando
    return jsonify({"status": "erro", "ocupada": True,
                    "mensagem": "Bot já está em execução"}), 400


def _iniciar_reservado(alvo):
    """Sobe a thread do bot; se nem subir, desfaz a reserva (senão fica presa)."""
    try:
        threading.Thread(target=alvo).start()
    except Exception:
        _liberar_contas_do_bot()
        raise


def _liberar_conta_do_bot(conta):
    """Conta que terminou o rodízio (teto, sem login) volta a poder abrir no painel."""
    with JANELAS_LOCK:
        CONTAS_DO_BOT.discard(conta)


def _liberar_contas_do_bot():
    global BOT_RODANDO
    with JANELAS_LOCK:
        BOT_RODANDO = False
        CONTAS_DO_BOT.clear()


def run_async_bot():
    try:
        asyncio.run(bot_main_headless())
    finally:
        _liberar_contas_do_bot()


def run_lote():
    try:
        asyncio.run(bot_lote_headless())
    finally:
        _liberar_contas_do_bot()


async def bot_lote_headless():
    """
    Percorre LOTE_ALVOS (alvos específicos + mensagem única) e envia via analisar_perfil
    — a MESMA função de envio do fluxo original, aqui dirigida por uma lista explícita
    em vez de raspar seguidores. Respeita LIMITE_DIARIO, processados.txt (dedupe) e
    STOP_BOT.
    """
    enviados = 0
    _anunciar_historico()
    try:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=perfil_atual(),
                headless=HEADLESS,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            if await checar_login(page):
                for alvo in list(LOTE_ALVOS):
                    if STOP_BOT:
                        break
                    if teto_estourado():
                        logger.warning("[LOTE] LIMITE DIÁRIO ATINGIDO!")
                        break
                    username = alvo.get("username", "")
                    mensagem = alvo.get("mensagem", "")
                    if not username or not mensagem:
                        continue
                    # msg única por lead entra como "template" (sem {nome} a substituir)
                    st = await analisar_perfil(context, username, [], [], mensagem)
                    logger.info(f"[LOTE] @{username} -> {st}")
                    if st == "STOP":
                        break
                    if st == "SENT":
                        enviados += 1
                        await delay(10, 15)
            await context.close()
    except Exception as e:
        logger.error(f"Erro Crítico no LOTE: {e}")

    logger.info(f"🏁 Lote finalizado. Enviados: {enviados}.")

async def checar_login(page):
    """Sessão viva?"""
    logger.info("Verificando sessão do Instagram...")
    if "instagram.com" not in page.url: await page.goto('https://www.instagram.com/')

    for _ in range(10): # Tenta por uns 20 segundos
        if STOP_BOT: return False
        try:
            cookies = await page.context.cookies("https://www.instagram.com")
            nomes = {c["name"] for c in cookies}
            if "sessionid" in nomes and "ds_user_id" in nomes:
                logger.info("Login detectado (cookie de sessão).")
                await asyncio.sleep(3)
                return True
        except Exception: pass
        try:
            if await page.locator('svg[aria-label="Página inicial"]').count() > 0 or await page.locator('svg[aria-label="Home"]').count() > 0:
                logger.info("Login detectado com sucesso!")
                await asyncio.sleep(3)
                return True
        except: pass
        await asyncio.sleep(2)

    logger.error("❌ Sessão Expirada ou bloqueada. Rode: python senders/dm_bot_login.py")
    return False

async def extrair_nome_limpo(page, username_alvo):
    try:
        header_text = await page.locator('header').inner_text()
        for linha in header_text.split('\n'):
            if linha.strip() == username_alvo or linha[0].isdigit(): continue
            if len(linha) > 2 and "Seguir" not in linha and "Mensagem" not in linha:
                nl = re.sub(r'[^\w\s\-\|]', '', unicodedata.normalize('NFKC', linha))
                for div in ['|', '-', '•', '/', '–']: 
                    if div in nl: nl = nl.split(div)[0]
                return nl.strip().split()[0].capitalize()
    except: return ""

# Perfis seguidos em que nao deu para ler a conversa, por conta remetente.
INCERTOS_SEGUIDOS = {}
LIMITE_INCERTOS = 5


def _ja_abordado(username):
    """Ja recebeu DM nossa por outro nicho, outra conta ou outra versao do bot?"""
    if _historico is None:
        return None
    return _historico.ja_abordado(username)


def _leitura_ok(conta):
    INCERTOS_SEGUIDOS[conta or CONTA_ATUAL or ""] = 0


def _nao_confirmado(username, conta, motivo):
    """Nao deu para confirmar que a conversa esta vazia: NAO envia."""
    chave = conta or CONTA_ATUAL or ""
    n = INCERTOS_SEGUIDOS.get(chave, 0) + 1
    INCERTOS_SEGUIDOS[chave] = n
    logger.warning("@%s: nao deu para confirmar que a conversa esta vazia (%s). "
                   "Por seguranca, NAO enviei.", username, motivo)
    if n >= LIMITE_INCERTOS:
        logger.error("%s: %d perfis seguidos sem conseguir ler a conversa. O "
                     "Instagram provavelmente mudou a tela -- a checagem em "
                     "senders/dm_bot/conversa_ig.py precisa ser revista. Parando "
                     "esta conta para nao arriscar DM repetida.",
                     f"@{chave}" if chave else "perfil padrao", n)
        return "STOP"
    return "INCERTO"


def _formatar_antigo(msg_template, nome):
    """
    Como a mensagem sempre foi montada. Continua valendo para o LOTE (leads do Maps),
    que chega pronto do painel com as chaves escapadas para este .format.
    """
    return (msg_template.format(nome=nome) if nome
            else msg_template.replace("{nome}", "").replace("Boa tarde ,", "Boa tarde,"))


def _nome_do_perfil(cabecalho, username):
    """O nome inteiro que aparece no perfil: a 1a linha do cabecalho que nao e o @."""
    for linha in (cabecalho or "").split("\n"):
        linha = linha.strip()
        if not linha or linha.lower() == (username or "").lower() or linha[0].isdigit():
            continue
        if "Seguir" in linha or "Mensagem" in linha or len(linha) < 2:
            continue
        return linha[:80]
    return ""


async def _mensagem_final(username, nome, cabecalho, msg_template, prospeccao):
    """A mensagem que vai para ESTE perfil."""
    if not prospeccao or _personalizar is None:
        return _formatar_antigo(msg_template, nome)
    perfil = {"usuario": username, "nome": nome or "",
              "nome_perfil": _nome_do_perfil(cabecalho, username),
              "bio": cabecalho or "", "nicho": NICHO_ATUAL}
    try:
        r = await asyncio.to_thread(_personalizar.texto_do_perfil, msg_template, perfil)
    except Exception as e:
        logger.warning("[IA] @%s: a personalizacao falhou (%s); saiu o template", username, e)
        try:
            return _personalizar.renderizar_perfil(perfil, msg_template)
        except Exception:
            return _formatar_antigo(msg_template, nome)
    if r.get("personalizado"):
        logger.info("[IA] @%s: mensagem personalizada (%s)", username, r.get("modelo"))
    elif _personalizar.ativa("instagram"):
        logger.info("[IA] @%s: saiu o template (%s)", username, r.get("motivo"))
    return r.get("texto") or _formatar_antigo(msg_template, nome)


def _anunciar_personalizacao():
    """No inicio da prospeccao: a IA esta ligada? E o 9router, aberto?"""
    if _personalizar is None:
        return
    try:
        if not _personalizar.ativa("instagram"):
            logger.info("[IA] personalizacao do Instagram desligada: a mensagem sai "
                        "do template (com variaveis e variacoes)")
            return
        import ia_roteador
        g = ia_roteador.garantir()
        logger.info("[IA] personalizacao do Instagram LIGADA; 9router: %s (%s)",
                    g.get("acao"), g.get("detalhe"))
    except Exception as e:
        logger.warning("[IA] nao deu para conferir a personalizacao (%s)", e)


def _anunciar_historico():
    """No inicio de cada disparo: quantos perfis ficam de fora, e por que."""
    INCERTOS_SEGUIDOS.clear()
    if _historico is None:
        logger.error("[HISTORICO] indisponivel: so o processados.txt do nicho "
                     "%s vai evitar DM repetida", NICHO_ATUAL)
        return
    try:
        r = _historico.resumo()
        logger.info("[HISTORICO] %d perfis que ja receberam DM (%d arquivos de "
                    "rastreio) ficam de fora deste disparo", r["perfis"], r["arquivos"])
    except Exception as e:
        logger.warning("[HISTORICO] nao deu para montar o resumo (%s)", e)


async def analisar_perfil(context, username, kw_ativas, neg_ativas, msg_template, conta=None,
                          prospeccao=False):
    # `prospeccao=True`: veio da varredura de seguidores, e a mensagem e montada
    # AQUI para este perfil (personalizar.texto_do_perfil). False: veio do lote
    # de leads do Maps, que ja chega montado e personalizado pelo painel.
    if STOP_BOT: return "STOP"
    if teto_estourado(conta): return "STOP"

    proc = ler_linhas(get_caminho('processados.txt'))
    if any(username == p.split('|')[0] for p in proc): return "SKIP"

    # Antes de abrir qualquer tela: o processados.txt acima e SO deste nicho.
    antes = _ja_abordado(username)
    if antes:
        logger.info("@%s ja recebeu DM em %s (%s). Ignorando.", username,
                    antes.get("data") or "data desconhecida", antes.get("origem"))
        registrar_acao(username, "REJECTED", conta)
        return "SKIP"

    page = await context.new_page()
    await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    try:
        logger.info(f"Analisando: @{username}")
        await page.goto(f'https://www.instagram.com/{username}/')
        await delay(3, 5)

        if STOP_BOT: await page.close(); return "STOP"

        cabecalho = ""
        try: cabecalho = await page.locator('header').inner_text()
        except: pass
        bio = cabecalho.lower()

        if any(bad in bio for bad in neg_ativas):
            registrar_acao(username, "REJECTED", conta); await page.close(); return "SKIP"

        nome = await extrair_nome_limpo(page, username)

        msg_btn = page.locator('div[role="button"]:has-text("Mensagem"), button:has-text("Mensagem")').first
        if await msg_btn.is_visible():
            await msg_btn.click()
            try: await page.wait_for_selector('div[role="textbox"]', timeout=8000)
            except: registrar_acao(username, "REJECTED", conta); await page.close(); return "SKIP"

            # A checagem antiga lia div[role="grid"]/[role="listbox"], que a
            # janelinha de chat de hoje nao tem: vinha texto vazio, vazio virava
            # "Chat limpo!" e a DM saia. Zero deteccoes em 1.100 envios.
            veredito, motivo = await _conversa.conversa_com(
                page, username, parar=lambda: STOP_BOT)
            if STOP_BOT:
                await page.close(); return "STOP"
            if veredito == "existe":
                _leitura_ok(conta)
                logger.warning(f"Já existe conversa com @{username} ({motivo}). Ignorando.")
                registrar_acao(username, "REJECTED", conta)
                await page.close()
                return "SKIP"
            if veredito != "vazia":
                await page.close()
                return _nao_confirmado(username, conta, motivo)
            _leitura_ok(conta)

            # O texto sai AQUI, antes de seguir: com a IA ligada ele leva alguns
            # segundos, e assim a segunda leitura da conversa continua colada na
            # digitacao.
            msg_final = await _mensagem_final(username, nome, cabecalho, msg_template,
                                              prospeccao)
            if STOP_BOT:
                await page.close(); return "STOP"

            logger.info(f"Chat limpo! Voltando para seguir @{username}...")
            await page.goto(f'https://www.instagram.com/{username}/')
            await delay(3, 5)
            if STOP_BOT:
                await page.close(); return "STOP"

            try:
                btn_seguir = page.locator('button:has-text("Seguir")').first
                if await btn_seguir.is_visible():
                    await btn_seguir.click()
                    await delay(2, 3)
            except: pass

            msg_btn_novo = page.locator('div[role="button"]:has-text("Mensagem"), button:has-text("Mensagem")').first
            if await msg_btn_novo.is_visible():
                await msg_btn_novo.click()
                await page.wait_for_selector('div[role="textbox"]', timeout=8000)

                # DE NOVO, na janela em que o texto vai ser digitado: e o
                # instante que importa, e a primeira leitura foi ha meio minuto.
                veredito, motivo = await _conversa.conversa_com(
                    page, username, parar=lambda: STOP_BOT)
                if STOP_BOT:
                    await page.close(); return "STOP"
                if veredito == "existe":
                    logger.warning(f"Já existe conversa com @{username} ({motivo}). Ignorando.")
                    registrar_acao(username, "REJECTED", conta)
                    await page.close()
                    return "SKIP"
                if veredito != "vazia":
                    await page.close()
                    return _nao_confirmado(username, conta, motivo)

                # A caixa DESTA conversa, nunca a primeira da pagina.
                box = await _conversa.caixa_da_conversa(page, username)
                if box is None:
                    await page.close()
                    return _nao_confirmado(username, conta,
                                           "a caixa de texto da conversa nao foi encontrada")

                await box.click(); await page.keyboard.insert_text(msg_final)
                await asyncio.sleep(2); await page.keyboard.press("Enter")

                # Do Enter em diante a DM pode ter saido: registra ANTES de
                # qualquer outro passo. Um erro daqui para baixo nao pode deixar
                # a pessoa fora do historico -- seria a proxima DM repetida.
                registrar_acao(username, "SENT", conta)
                await asyncio.sleep(2)
                try:
                    sobrou = await box.evaluate("e => (e.innerText || '').trim().length")
                    if sobrou and await _conversa.clicar_enviar(page, username):
                        await asyncio.sleep(2)
                    depois, _m = await _conversa.conversa_com(page, username, prazo=6, estavel=0)
                    if depois != "existe":
                        logger.warning("@%s: nao vi a mensagem na conversa depois do "
                                       "envio -- confira no Instagram", username)
                except Exception:
                    pass
                try: await page.close()
                except Exception: pass
                return "SENT"

        registrar_acao(username, "REJECTED", conta)

    except Exception as e: 
        logger.error(f"Erro ao processar @{username}: {e}")
        registrar_acao(username, "REJECTED", conta)

    if not page.is_closed(): await page.close()
    return "SKIP"

async def _abrir_lista(page, alvo, tipo_lista, nome_lista):
    """
    Abre a lista de seguidores (ou de quem o perfil segue). True se o dialogo abriu.
    """
    tentativas = [
        f'a[href="/{alvo}/{tipo_lista}/"]',          # exato, relativo
        f'a[href$="/{alvo}/{tipo_lista}/"]',         # termina com (exclui mutualOnly)
        f'a[href*="/{alvo}/{tipo_lista}/"]',         # o do original
        f'a[href*="/{tipo_lista}/"]',                # qualquer link da lista
        f'[role="link"][href*="/{tipo_lista}/"]',    # quando nao e <a>
    ]
    for sel in tentativas:
        try:
            el = page.locator(sel).first
            if not await el.count():
                continue
            if not await el.is_visible(timeout=2500):
                continue
            await el.click()
            await page.wait_for_selector('div[role="dialog"]', timeout=8000)
            return True
        except Exception:
            continue

    # ultimo recurso: clicar pelo TEXTO ("1.234 seguidores")
    rotulos = (["seguidores", "followers"] if tipo_lista == "followers"
               else ["seguindo", "following"])
    for r in rotulos:
        try:
            el = page.get_by_role("link", name=re.compile(r, re.I)).first
            if await el.count() and await el.is_visible(timeout=2500):
                await el.click()
                await page.wait_for_selector('div[role="dialog"]', timeout=8000)
                logger.info("@%s: %s aberta pelo texto '%s'", alvo, nome_lista, r)
                return True
        except Exception:
            continue

    # nada abriu: mostra o que EXISTE, para acertar o seletor na proxima
    try:
        hrefs = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean).slice(0, 30)")
        logger.warning("@%s: nao achei o link de %s. Links da pagina: %s",
                       alvo, nome_lista, hrefs)
    except Exception as e:
        logger.warning("@%s: nao achei o link de %s e nem consegui listar a pagina (%s)",
                       alvo, nome_lista, type(e).__name__)
    return False


async def _perfil_inacessivel(page):
    """A pagina do perfil e privada ou nao existe?"""
    marcas = ("conta e privada", "conta é privada", "perfil e privado",
              "perfil é privado", "this account is private",
              "esta pagina nao esta disponivel", "esta página não está disponível",
              "sorry, this page isn't available")
    try:
        corpo = (await page.locator("body").inner_text(timeout=4000)).lower()
    except Exception:
        return None
    for m in marcas:
        if m in corpo:
            return "privado" if "priv" in m else "inexistente"
    return None


async def varrer_fonte(context, page, alvo, conta=None):
    """Varre as listas de uma fonte atras de candidatos."""
    logger.info("EXPLORANDO FONTE: @%s", alvo)

    kw_ativas = ler_linhas(get_caminho('palavras_chave.txt'))
    neg_ativas = [p.lower() for p in ler_linhas(get_caminho('palavras_bloqueadas.txt'))]

    achou_alguma_lista = False

    # SEGUIDORES e QUEM O PERFIL SEGUE, como no original
    for tipo_lista in ["followers", "following"]:
        if STOP_BOT: return "STOP"

        nome_lista = "Seguidores" if tipo_lista == "followers" else "Quem ele Segue"
        logger.info("@%s abrindo %s de @%s", conta or "perfil padrao", nome_lista, alvo)

        await page.goto(f'https://www.instagram.com/{alvo}/')
        await delay(3, 5)
        if STOP_BOT: return "STOP"

        estado = await _perfil_inacessivel(page)
        if estado:
            logger.warning("@%s: perfil %s -- nao da para varrer, fonte descartada",
                           alvo, estado)
            return "INACESSIVEL"

        if not await _abrir_lista(page, alvo, tipo_lista, nome_lista):
            continue

        for termo in kw_ativas:
            if STOP_BOT: return "STOP"
            logger.info("Buscando termo: '%s' em %s de @%s...", termo, nome_lista, alvo)

            try:
                inp = page.locator(
                    'input[placeholder="Pesquisar"], '
                    'input[aria-label="Pesquisar"], '
                    'input[placeholder="Search"], '
                    'input[aria-label="Search"], '
                    'div[role="dialog"] input[type="text"]'
                ).first
                if await inp.is_visible(timeout=4000):
                    await inp.click(); await inp.fill(""); await asyncio.sleep(1)
                    await inp.press_sequentially(termo, delay=100); await asyncio.sleep(4)
                    if termo.lower() not in (await inp.input_value()).lower(): continue
                else:
                    logger.warning("Campo de pesquisa nao encontrado em %s de @%s "
                                   "-- pulando lista.", nome_lista, alvo)
                    break
            except Exception:
                continue

            achou_alguma_lista = True
            vistos = set()
            MAX_SCROLLS = 20
            tentativas_vazias = 0

            for i in range(MAX_SCROLLS):
                if STOP_BOT: return "STOP"
                els = await page.locator('span._ap3a._aaco._aacw._aacx._aad7._aade').all()
                novos_elementos = 0
                logger.info("  [Scroll %d/%d] %d elemento(s) na lista",
                            i + 1, MAX_SCROLLS, len(els))

                for el in els:
                    if STOP_BOT: return "STOP"
                    if teto_estourado(conta):
                        logger.warning("LIMITE DIARIO ATINGIDO para @%s",
                                       conta or "perfil padrao")
                        return "STOP"

                    try:
                        u = await el.inner_text()
                        if not u: continue
                    except Exception:
                        continue

                    proc_db = ler_linhas(get_caminho('processados.txt'))

                    if u not in vistos and not any(u == p.split('|')[0] for p in proc_db):
                        vistos.add(u)
                        novos_elementos += 1

                        texto_bloco = await el.evaluate("""node => {
                            let p = node;
                            while(p && p.parentElement && p.parentElement.tagName !== 'BODY') {
                                p = p.parentElement;
                                if (p.innerText && p.innerText.includes('\\n')) { return p.innerText; }
                            }
                            return node.innerText || '';
                        }""")

                        termo_limpo = remover_acentos(termo.lower())
                        bloco_limpo = remover_acentos(texto_bloco.lower())

                        if termo_limpo not in bloco_limpo:
                            estatisticas['rejeitados'] += 1
                            continue

                        # daqui: duas contas em paralelo nao abordam o mesmo @
                        if not _reservar_alvo(u):
                            continue
                        try:
                            st = await analisar_perfil(context, u, kw_ativas, neg_ativas,
                                                       MENSAGEM_ATUAL, conta,
                                                       prospeccao=True)
                        finally:
                            _liberar_alvo(u)
                        if st == "STOP": return "STOP"
                        if st == "SENT": await delay(10, 15)

                if novos_elementos == 0:
                    tentativas_vazias += 1
                    if tentativas_vazias >= 2: break
                else:
                    tentativas_vazias = 0

                try:
                    if len(els) > 0: await els[-1].scroll_into_view_if_needed()
                    div = page.locator('div[role="dialog"] > div > div:nth-child(2)').first
                    if await div.is_visible(): await div.evaluate("e => e.scrollTop += 2000")
                    await delay(3, 5)
                except Exception:
                    pass

    if not achou_alguma_lista:
        # Nenhuma das duas listas abriu com campo de busca. A fonte NAO foi
        # varrida, entao volta para a fila em vez de ser marcada como usada --
        # foi marcar fonte nao varrida como concluida que gastou 265 fontes.
        return "SEM_BUSCA"
    return "FINISHED"

# ── Coordenacao entre as contas que rodam ao mesmo tempo ────────────────────
# Todas as contas de um disparo trabalham sobre os MESMOS arquivos do nicho:
# `fontes_alvo.txt`, `processados.txt`, `rastreio_dms.json`.
FONTES_RESERVADAS = set()
ALVOS_EM_ATENDIMENTO = set()

# Fontes que ja falharam NESTE ciclo.
FONTES_FALHADAS = set()


def _reservar_alvo(username):
    """Este @ ja esta sendo atendido por outra conta agora?"""
    if username in ALVOS_EM_ATENDIMENTO:
        return False
    ALVOS_EM_ATENDIMENTO.add(username)
    return True


def _liberar_alvo(username):
    ALVOS_EM_ATENDIMENTO.discard(username)


def _reservar_fonte():
    """A proxima fonte que ninguem esta varrendo e que ainda nao falhou hoje."""
    usadas = set(ler_linhas(get_caminho('fontes_usadas.txt')))
    for f in ler_linhas(get_caminho('fontes_alvo.txt')):
        if f in usadas or f in FONTES_RESERVADAS or f in FONTES_FALHADAS:
            continue
        FONTES_RESERVADAS.add(f)
        return f
    return None


def _marcar_falha(alvo):
    """A fonte nao abriu agora. Sai da vez deste ciclo, mas continua na fila."""
    FONTES_RESERVADAS.discard(alvo)
    FONTES_FALHADAS.add(alvo)


def _concluir_fonte(alvo):
    """Marca a fonte como varrida. So ACRESCENTA em `fontes_usadas.txt`."""
    with open(get_caminho('fontes_usadas.txt'), 'a', encoding='utf-8') as f:
        f.write(alvo + chr(10))
    FONTES_RESERVADAS.discard(alvo)


def _limpar_fila():
    """Tira da fila o que ja foi varrido. So no FIM do ciclo, sem ninguem escrevendo."""
    try:
        usadas = set(ler_linhas(get_caminho('fontes_usadas.txt')))
        atuais = ler_linhas(get_caminho('fontes_alvo.txt'))
        restantes, vistas = [], set()
        for f in atuais:
            if f in usadas or f in vistas:
                continue
            vistas.add(f)
            restantes.append(f)
        if len(restantes) != len(atuais):
            with open(get_caminho('fontes_alvo.txt'), 'w', encoding='utf-8') as fh:
                fh.write(chr(10).join(restantes))
            logger.info("Fila limpa: %d -> %d fontes", len(atuais), len(restantes))
    except Exception as e:
        logger.warning("Nao deu para limpar a fila: %s", e)


def _devolver_fonte(alvo):
    """Solta a fonte SEM marcar como usada: ficou pela metade, outra conta segue."""
    FONTES_RESERVADAS.discard(alvo)


async def _rodar_uma_conta(p, conta, rotulo=""):
    """Uma conta, um navegador, do inicio ao seu proprio teto."""
    context = None
    quem = f"@{conta}" if conta else "perfil padrao"
    try:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=perfil_atual(conta),
            headless=HEADLESS,
            # viewport fixo, como no bot original. Sem ele o Playwright usa o
            # tamanho da janela, e o Instagram serve um layout diferente por
            # largura -- os seletores foram escritos para ESTA tela.
            viewport={'width': 1000, 'height': 800},
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        if not await checar_login(page):
            logger.warning("%s esta sem sessao valida; nao vai enviar", quem)
            return "sem_login"

        logger.info("%s pronta e trabalhando", quem)

        sem_busca_seguidas = 0

        while not STOP_BOT:
            if teto_estourado(conta):
                logger.info("%s bateu o teto das ultimas 24h; encerrando esta conta", quem)
                return "teto"

            alvo = _reservar_fonte()
            if alvo is None:
                logger.info("%s: nao ha fonte livre na fila", quem)
                return "sem_fontes"

            logger.info("%s EXPLORANDO FONTE: @%s", quem, alvo)
            try:
                st = await varrer_fonte(context, page, alvo, conta)
            except Exception as e:
                logger.error("%s falhou na fonte @%s: %s", quem, alvo, e)
                _devolver_fonte(alvo)
                return "erro"

            if STOP_BOT:
                _devolver_fonte(alvo)
                return "parado"
            if st == "STOP":
                # a fonte ficou pela metade: volta para a fila em vez de ser
                # descartada, e outra conta continua de onde esta parou
                _devolver_fonte(alvo)
                return "teto" if teto_estourado(conta) else "parado"

            if st == "INACESSIVEL":
                # privado ou inexistente: nunca vai dar certo, entao sai da fila
                # de vez em vez de voltar e ser repescado eternamente
                _concluir_fonte(alvo)
                sem_busca_seguidas = 0
                continue

            if st == "SEM_BUSCA":
                # a tela dos seguidores nao abriu direito: a fonte NAO foi varrida
                # e volta para a fila. Marcar como concluida aqui foi o que gastou
                # 265 fontes sem enviar nada.
                _marcar_falha(alvo)
                sem_busca_seguidas += 1
                if sem_busca_seguidas >= 5:
                    logger.error("%s: a caixa de busca dos seguidores falhou 5 vezes "
                                 "seguidas. O Instagram provavelmente mudou a tela -- "
                                 "os seletores em SELETORES_BUSCA precisam ser revistos. "
                                 "Parando esta conta para nao queimar a fila.", quem)
                    return "tela_mudou"
                await asyncio.sleep(3)
                continue
            sem_busca_seguidas = 0

            _concluir_fonte(alvo)
            logger.info("%s: fonte @%s concluida", quem, alvo)
            await asyncio.sleep(2)

        return "parado"
    except Exception as e:
        logger.error("Erro no navegador de %s: %s", quem, e)
        return "erro"
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if conta:
            _liberar_conta_do_bot(conta)


async def bot_main_headless():
    """Ciclo de prospeccao com TODAS as contas trabalhando ao mesmo tempo."""
    global CONTAS_FILA
    FONTES_RESERVADAS.clear()
    ALVOS_EM_ATENDIMENTO.clear()
    FONTES_FALHADAS.clear()
    _anunciar_historico()
    # so na prospeccao: no lote, a mensagem ja chega personalizada pelo painel.
    # Em thread, porque garantir() pode esperar o 9router subir.
    await asyncio.to_thread(_anunciar_personalizacao)

    try:
        fila = list(CONTAS_FILA) if CONTAS_FILA else [{"conta": CONTA_ATUAL, "restam": None}]
        nomes = ", ".join(f"@{c['conta']}" if c["conta"] else "perfil padrao" for c in fila)
        logger.info("Disparo em PARALELO com %d conta(s): %s", len(fila), nomes)

        async with async_playwright() as p:
            tarefas = [asyncio.create_task(_rodar_uma_conta(p, c["conta"])) for c in fila]
            motivos = await asyncio.gather(*tarefas, return_exceptions=True)

        for c, m in zip(fila, motivos):
            quem = f"@{c['conta']}" if c["conta"] else "perfil padrao"
            logger.info("%s terminou: %s", quem,
                        m if not isinstance(m, Exception) else f"erro ({m})")
    except Exception as e:
        logger.error(f"Erro Critico no Navegador: {e}")

    _limpar_fila()
    CONTAS_FILA = []
    FONTES_RESERVADAS.clear()
    ALVOS_EM_ATENDIMENTO.clear()
    FONTES_FALHADAS.clear()
    logger.info("🏁 Ciclo de prospeccao finalizado.")

if __name__ == "__main__":
    if not os.path.exists(PERFIS_DIR): os.makedirs(PERFIS_DIR)

    logger.info("=========================================")
    logger.info("🤖 Servidor de Prospecção Invisível Ativo")
    logger.info("📡 Escutando o LeadForge na porta 5022...")
    logger.info("=========================================")

    # Chromium de janela no painel que sobreviveu a um reinício fica fora da
    # tela: ninguém o vê nem o fecha, e ele segura o perfil da conta.
    try:
        _orfaos = (_matar_chromium(None, so_fora_da_tela=True) + navegador_unico.matar_orfaos()
                   + navegador_unico.matar_orfaos(navegador_unico.MARCA_AQUECIMENTO))
        if _orfaos:
            logger.info("Encerrados %d processo(s) de janela do painel que sobraram", _orfaos)
    except Exception as _e6:
        logger.warning("Nao consegui procurar janelas orfas: %s", _e6)
    app.run(host='0.0.0.0', port=5022, threaded=True,
            request_handler=_servidor_local.RequestHandlerSemAtraso)
