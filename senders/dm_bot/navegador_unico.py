"""navegador_unico.py — um Chromium só para todas as contas abertas no painel."""
import asyncio
import base64
import ctypes
import ctypes.wintypes as _w
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

import janela_remota

logger = logging.getLogger(__name__)

# Marca na linha de comando: é por ela que o navegador único (e o que sobrar
# dele depois de um reinício) é reconhecido.
MARCA = "--lf-navegador-unico"
# Separação de sites desligada: o Instagram abre vários processos por página
# para conteúdo de outros domínios; aqui só abre instagram.com (links de fora
# são bloqueados), e desligar economizou ~20% (926 → 760 MB em 3 contas).
ISOLAR_SITES = False

# O Chromium só lê o ÚLTIMO --disable-features: o nosso apagava a lista que o
# Playwright passa (MediaRouter, Translate, PaintHolding...). Vai a lista dele
# (chromiumSwitches.js do Playwright instalado) mais a nossa, num switch só.
_RECURSOS_DO_PLAYWRIGHT = ["AcceptCHFrame", "AvoidUnnecessaryBeforeUnloadCheckSync",
                           "DestroyProfileOnBrowserClose", "DialMediaRouteProvider",
                           "GlobalMediaControls", "HttpsUpgrades", "LensOverlay", "MediaRouter",
                           "PaintHolding", "ThirdPartyStoragePartitioning", "Translate",
                           "AutoDeElevate", "RenderDocument"]
_RECURSOS_DESLIGADOS = _RECURSOS_DO_PLAYWRIGHT + ([] if ISOLAR_SITES
                                                  else ["IsolateOrigins", "site-per-process"])

ARGS_BASE = (["--mute-audio", "--disable-blink-features=AutomationControlled",
              "--disable-features=" + ",".join(_RECURSOS_DESLIGADOS)]
             + ([] if ISOLAR_SITES else ["--disable-site-isolation-trials"]))
ARGS = [MARCA, janela_remota.MARCA_FORA_DA_TELA] + ARGS_BASE


# Navegador do AQUECIMENTO (Reels): o mesmo Chromium e as mesmas flags, mas
# com as janelas NA TELA (o Reels passa sozinho; o operador pode mexer) e na barra.
# Um processo para todas as contas: 19 contas eram 19 Chromiums (~300 MB cada).
MARCA_AQUECIMENTO = "--lf-aquecimento"
# Modo "máquina fraca" do Chromium: com 5 contas no Reels leve, 413 → 250-296 MB
# por conta, e o vídeo tocando igual.
ARGS_AQUECIMENTO = [MARCA_AQUECIMENTO, "--enable-low-end-device-mode"] + ARGS_BASE


class SemSessao(Exception):
    """A pasta da conta não tem sessão do Instagram (deslogada): não adianta esperar."""


# ── Cookies da pasta de perfil ──────────────────────────────────────────────
class _Blob(ctypes.Structure):
    _fields_ = [("n", _w.DWORD), ("p", ctypes.POINTER(ctypes.c_char))]


def _dpapi(dados: bytes) -> bytes:
    """Desfaz a proteção do Windows (só funciona para o usuário que protegeu)."""
    entrada = _Blob(len(dados), ctypes.cast(ctypes.create_string_buffer(dados, len(dados)),
                                            ctypes.POINTER(ctypes.c_char)))
    saida = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(entrada), None, None, None,
                                                    None, 0, ctypes.byref(saida)):
        raise OSError("o Windows recusou abrir a chave do perfil")
    try:
        return ctypes.string_at(saida.p, saida.n)
    finally:
        ctypes.windll.kernel32.LocalFree(saida.p)


_SAMESITE = {0: "None", 1: "Lax", 2: "Strict"}


def cookies_do_perfil(perfil: str):
    """
    Cookies do Instagram/Facebook de uma pasta de perfil, no formato do Playwright.
    Levanta PermissionError se a pasta está em uso (o Chromium que a usa trava o
    arquivo).
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    with open(os.path.join(perfil, "Local State"), encoding="utf-8") as f:
        estado = json.load(f)
    protegida = base64.b64decode(estado["os_crypt"]["encrypted_key"])
    if not protegida.startswith(b"DPAPI"):
        raise ValueError("chave do perfil em formato desconhecido")
    chave = AESGCM(_dpapi(protegida[5:]))
    origem = os.path.join(perfil, "Default", "Network", "Cookies")
    fd, copia = tempfile.mkstemp(prefix="lf_ck_", suffix=".db")
    os.close(fd)
    try:
        shutil.copy2(origem, copia)
        con = sqlite3.connect(copia)
        try:
            linhas = con.execute(
                "SELECT name, host_key, path, expires_utc, is_secure, is_httponly, samesite, "
                "encrypted_value FROM cookies WHERE host_key = 'instagram.com' "
                "OR host_key LIKE '%.instagram.com'").fetchall()
        finally:
            con.close()
    finally:
        try:
            os.remove(copia)
        except OSError:
            pass
    saida = []
    for nome, host, caminho, expira, seguro, http, samesite, cifrado in linhas:
        if not cifrado.startswith(b"v10"):
            continue
        try:
            valor = chave.decrypt(cifrado[3:15], cifrado[15:], None)
        except Exception:
            continue
        # Chromium 130+: 32 bytes de hash do domínio antes do valor
        if len(valor) > 32 and not valor[:32].isascii():
            valor = valor[32:]
        saida.append({
            "name": nome, "value": valor.decode("utf-8", "replace"), "domain": host,
            "path": caminho or "/", "secure": bool(seguro), "httpOnly": bool(http),
            "sameSite": _SAMESITE.get(samesite, "Lax"),
            "expires": (expira / 1e6 - 11644473600) if expira else -1,
        })
    return saida


# ── O navegador ─────────────────────────────────────────────────────────────
class NavegadorUnico:
    """Um loop asyncio numa thread, um Playwright, um Chromium."""

    def __init__(self, marca=MARCA, args=None, fora_da_tela=True, nome="navegador-unico"):
        self._marca = marca
        # a marca vai sempre: é por ela que pids() e matar_orfaos() acham o processo
        self._args = list(args if args is not None else ARGS)
        if marca not in self._args:
            self._args.insert(0, marca)
        self._abrindo_contextos = 0     # contextos a caminho (fechar_se_vazio não os vê)
        self._fora_da_tela = fora_da_tela      # janelas fora da tela e fora da barra
        self._nome = nome
        self._loop = None
        self._pronto = threading.Event()
        self._trava = threading.Lock()
        self._pw = None
        self._navegador = None
        self._abrindo = None
        self._cookies = {}            # conta -> última leitura boa (só na memória)
        self._barra_ate = 0.0         # até quando conferir a barra de tarefas a cada 2 s
        self._vigia = None
        self._pid = None

    # o loop vive enquanto o bot viver; cria-se na primeira conta aberta
    def loop(self):
        with self._trava:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._rodar, name=self._nome, daemon=True).start()
        self._pronto.wait(10)
        return self._loop

    def _rodar(self):
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._pronto.set)
        self._loop.run_forever()

    def agendar(self, coro):
        """Roda uma corrotina no loop do navegador (devolve um concurrent Future)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop())

    async def _navegador_vivo(self):
        if self._navegador is not None and self._navegador.is_connected():
            return self._navegador
        # várias contas abrindo juntas esperam a MESMA abertura
        abrindo = self._abrindo
        if abrindo is None or abrindo.done():
            abrindo = self._abrindo = asyncio.ensure_future(self._abrir())
        return await asyncio.shield(abrindo)

    async def _abrir(self):
        from playwright.async_api import async_playwright
        for tentativa in (1, 2):
            if self._pw is None:
                self._pw = await async_playwright().start()
            try:
                # sandbox ligado: o launch do Playwright desliga por padrão, e
                # sem ele uma falha no processo da página vira execução de
                # código como o usuário
                self._navegador = await self._pw.chromium.launch(
                    headless=False, args=self._args, chromium_sandbox=True)
                break
            except Exception:
                # o driver do Playwright morreu junto com o navegador: refaz
                if tentativa == 2:
                    raise
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None
        self._pid = None
        logger.info("%s aberto (%s)", "Navegador único das contas" if self._fora_da_tela
                    else "Navegador do aquecimento", self._navegador.version)
        if self._fora_da_tela and (self._vigia is None or self._vigia.done()):
            self._vigia = asyncio.ensure_future(self._vigiar_barra())
        return self._navegador

    def cookies(self, conta, perfil):
        """
        Lê da pasta. A última leitura vale SÓ se o arquivo está travado (a prospecção
        usando a conta) — pasta apagada ou conta deslogada não abre mais pela cópia em
        memória.
        """
        try:
            ck = cookies_do_perfil(perfil)
        except PermissionError as e:
            if conta in self._cookies:
                return self._cookies[conta]
            raise PermissionError(f"a pasta de @{conta} está em uso e ainda não há cópia dos "
                                  f"cookies desta sessão do bot") from e
        except Exception:
            self._cookies.pop(conta, None)
            raise
        if any(c["name"] == "sessionid" for c in ck):
            self._cookies[conta] = ck
        else:
            self._cookies.pop(conta, None)
        return ck

    async def novo_contexto(self, conta, perfil, viewport):
        """Contexto isolado da conta, já com os cookies dela."""
        self._abrindo_contextos += 1
        try:
            return await self._novo_contexto(conta, perfil, viewport)
        finally:
            self._abrindo_contextos -= 1

    async def _novo_contexto(self, conta, perfil, viewport):
        ck = await asyncio.to_thread(self.cookies, conta, perfil)
        if not any(c["name"] == "sessionid" for c in ck):
            raise SemSessao(f"@{conta} não tem sessão do Instagram salva (entre de novo em Contas)")
        nav = await self._navegador_vivo()
        # sem service worker: o do Instagram (notificações) seguia rodando com a página
        # fechada e segurava ~90 MB por conta dormindo; no painel ele não serve para
        # nada
        ctx = await nav.new_context(viewport=viewport, service_workers="block")
        await ctx.add_cookies(ck)
        return ctx

    async def fechar_se_vazio(self, espera=5):
        """
        Sem nenhuma conta aberta, o navegador sai inteiro (o do aquecimento não tem
        conta dormindo para guardar: vazio, segurava ~230 MB à toa).
        """
        await asyncio.sleep(espera)        # outra conta pode estar abrindo agora
        nav = self._navegador
        if (nav is not None and nav.is_connected() and not nav.contexts
                and not self._abrindo_contextos):
            self._navegador = None
            try:
                await nav.close()
                logger.info("Navegador do aquecimento fechado (nenhuma conta aberta)")
            except Exception as e:
                logger.debug("fechar navegador vazio: %s", e)

    async def nova_pagina(self, ctx, posicao=None):
        """
        Página nova. No navegador do painel a janela vai para fora da tela (as novas não
        herdam a posição); no do aquecimento, para `posicao` (x, y).
        """
        self._barra_ate = time.monotonic() + 20
        page = await ctx.new_page()
        if self._fora_da_tela:
            limites = {"left": -32000, "top": -32000}
        elif posicao:
            limites = {"left": int(posicao[0]), "top": int(posicao[1])}
        else:
            return page
        try:
            cdp = await ctx.new_cdp_session(page)
            alvo = await cdp.send("Browser.getWindowForTarget")
            await cdp.send("Browser.setWindowBounds", {"windowId": alvo["windowId"],
                                                       "bounds": limites})
            await cdp.detach()
        except Exception as e:
            logger.debug("posição da janela: %s", e)
        return page

    def pids(self):
        """
        O processo principal do navegador único: as janelas de topo são dele. A
        varredura da máquina só acontece uma vez por navegador.
        """
        try:
            import psutil
        except Exception:
            return set()
        if self._pid:
            try:
                if psutil.Process(self._pid).is_running():
                    return {self._pid}
            except Exception:
                pass
            self._pid = None
        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info["name"] or "").lower() in ("chrome.exe", "chromium.exe") \
                        and self._marca in proc.cmdline() \
                        and not any(c.startswith("--type=") for c in proc.cmdline()):
                    self._pid = proc.pid
                    return {proc.pid}
            except Exception:
                continue
        return set()

    async def _vigiar_barra(self):
        """
        Uma tarefa só tira da barra de tarefas as janelas de TODAS as contas: a cada 2 s
        depois de abrir página (é quando janela nasce), e a cada 60 s no resto do tempo,
        só de reserva.
        """
        while self._navegador is not None and self._navegador.is_connected():
            try:
                pids = await asyncio.to_thread(self.pids)
                _, mudadas = await asyncio.to_thread(janela_remota.esconder_da_barra, pids)
                if mudadas:
                    logger.info("%d janela(s) do painel tirada(s) da barra de tarefas", mudadas)
            except Exception as e:
                logger.debug("barra de tarefas: %s", e)
            await asyncio.sleep(2 if time.monotonic() < self._barra_ate else 60)


def matar_orfaos(marca=MARCA):
    """Navegador único (ou do aquecimento) que sobrou de um reinício do bot."""
    try:
        import psutil
    except Exception:
        return 0
    mortos = 0
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info["name"] or "").lower() in ("chrome.exe", "chromium.exe") \
                    and marca in (proc.info["cmdline"] or []):
                proc.kill()
                mortos += 1
        except Exception:
            continue
    return mortos


NAVEGADOR = NavegadorUnico()
AQUECIMENTO = NavegadorUnico(marca=MARCA_AQUECIMENTO, args=ARGS_AQUECIMENTO,
                             fora_da_tela=False, nome="navegador-aquecimento")
