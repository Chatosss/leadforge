"""janela_remota.py — uma página do Chromium mostrada e operada de dentro do painel."""
import asyncio
import base64
import os
import sys
import threading
import time

VIEWPORT_INICIAL = {"width": 1280, "height": 800}   # o painel ajusta à área dele
TELA_MAX = {"width": 1920, "height": 1200}
# Fora da tela, mas visível para o Windows: headless derruba a sessão do
# Instagram. -32000 é a posição que o próprio Windows dá a janela minimizada.
MARCA_FORA_DA_TELA = "--window-position=-32000,-32000"
ARGS_FORA_DA_TELA = [MARCA_FORA_DA_TELA]
QUALIDADE_JPEG = 70
KEEPALIVE_S = 3            # repete o quadro: a conexão não cai e o fim é notado logo

TECLAS = {
    "Enter": ("Enter", 13, "\r"), "Backspace": ("Backspace", 8, None),
    "Tab": ("Tab", 9, None), "Escape": ("Escape", 27, None),
    "Delete": ("Delete", 46, None), "Home": ("Home", 36, None), "End": ("End", 35, None),
    "PageUp": ("PageUp", 33, None), "PageDown": ("PageDown", 34, None),
    "ArrowLeft": ("ArrowLeft", 37, None), "ArrowUp": ("ArrowUp", 38, None),
    "ArrowRight": ("ArrowRight", 39, None), "ArrowDown": ("ArrowDown", 40, None),
}
# Ctrl+<letra> que fazem sentido num campo de texto. Copiar/colar não: a área
# de transferência é a do computador do operador, e o painel cola como texto.
ATALHOS = {"a": "selectAll", "z": "undo", "y": "redo"}

_BORDA = b"--quadro\r\nContent-Type: image/jpeg\r\n\r\n"
# JPEG de 1x1 px: o keepalive escreve ALGO mesmo antes do primeiro quadro. Sem
# escrita, a conexão fechada pelo navegador nunca é notada, o espectador fica
# contado para sempre e a página nunca sai da conversa.
_JPEG_VAZIO = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP////////////////////////////////////////"
    "//////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=")
QUADROS_POR_SEGUNDO = 15     # com animação (GIF, vídeo), o Chromium mandaria 30+


def num(v, minimo, maximo, padrao=0.0):
    """Número limitado a [minimo, maximo]; lixo vira `padrao`."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return padrao
    return padrao if n != n else max(minimo, min(maximo, n))


class TelaRemota:
    def __init__(self):
        self.cond = threading.Condition()
        self.jpg = None
        self.seq = 0
        self.w = None              # área que o quadro mostra, em px CSS
        self.h = None
        self.botoes = 0
        self.espectadores = 0
        self._sem_desde = time.monotonic()
        self.transmitindo = False
        self.encerrada = False
        self._cdp = None

    # ── transmissão ─────────────────────────────────────────────────────────
    async def ligar(self, context, page):
        """Abre a sessão do DevTools. A transmissão só começa com espectador."""
        self._cdp = await context.new_cdp_session(page)
        self._cdp.on("Page.screencastFrame", self._quadro)

    def _quadro(self, ev):
        meta = ev.get("metadata") or {}
        with self.cond:
            self.jpg = base64.b64decode(ev["data"])
            self.seq += 1
            # o clique (0..1) é convertido pela área que o quadro mostra, não
            # pelo viewport pedido: barra de rolagem e afins comem pixels
            self.w = meta.get("deviceWidth") or self.w
            self.h = meta.get("deviceHeight") or self.h
            self.cond.notify_all()
        cdp = self._cdp
        # O próximo quadro só vem depois da confirmação: segurá-la limita a taxa.
        espera = max(0.0, 1.0 / QUADROS_POR_SEGUNDO - (time.monotonic() - getattr(self, "_ultimo", 0.0)))
        self._ultimo = time.monotonic() + espera

        async def confirmar():
            try:
                if espera:
                    await asyncio.sleep(espera)
                await cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
            except Exception:
                pass
        asyncio.ensure_future(confirmar())

    async def acompanhar(self):
        """Liga/desliga a transmissão conforme há alguém olhando. Chamar no laço."""
        if self._cdp is None:
            return
        if self.espectadores > 0 and not self.transmitindo:
            await self._cdp.send("Page.startScreencast", {
                "format": "jpeg", "quality": QUALIDADE_JPEG,
                "maxWidth": TELA_MAX["width"], "maxHeight": TELA_MAX["height"]})
            self.transmitindo = True
        elif self.espectadores == 0 and self.transmitindo:
            await self._cdp.send("Page.stopScreencast")
            self.transmitindo = False
            with self.cond:
                self.jpg = None    # quem chegar depois não vê um quadro velho

    def soltar(self):
        """A página foi fechada (conta dormindo): a próxima `ligar` usa a nova."""
        self._cdp = None
        self.transmitindo = False
        with self.cond:
            self.jpg = None

    def sem_espectador_ha(self):
        """Segundos desde que o último espectador saiu (0 se há alguém)."""
        return 0.0 if self.espectadores else time.monotonic() - self._sem_desde

    def encerrar(self):
        with self.cond:
            self.encerrada = True
            self.cond.notify_all()

    def quadros(self):
        """
        Gerador MJPEG. Cada quadro já sai seguido da borda do próximo: o navegador só
        mostra uma imagem quando vê a borda seguinte, e sem isso a tela ficava sempre um
        quadro atrás (até o keepalive).
        """
        with self.cond:
            self.espectadores += 1
        try:
            yield _BORDA
            visto = -1
            while True:
                with self.cond:
                    if self.seq == visto and not self.encerrada:
                        self.cond.wait(timeout=KEEPALIVE_S)
                    if self.encerrada:
                        return
                    jpg, visto = self.jpg, self.seq
                yield (jpg or _JPEG_VAZIO) + b"\r\n" + _BORDA
        finally:
            with self.cond:
                self.espectadores -= 1
                if not self.espectadores:
                    self._sem_desde = time.monotonic()

    # ── entrada ─────────────────────────────────────────────────────────────
    async def aplicar(self, page, ev):
        """Aplica um evento do contrato. Devolve False se o `t` não é daqui."""
        t = ev.get("t")
        cdp = self._cdp
        w = self.w or VIEWPORT_INICIAL["width"]
        h = self.h or VIEWPORT_INICIAL["height"]
        if t in ("mouse", "roda"):
            x = num(ev.get("x"), 0, 1) * w
            y = num(ev.get("y"), 0, 1) * h
            if t == "roda":
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseWheel", "x": x, "y": y,
                    "deltaX": num(ev.get("dx"), -2000, 2000),
                    "deltaY": num(ev.get("dy"), -2000, 2000)})
                return True
            acao = {"down": "mousePressed", "up": "mouseReleased",
                    "move": "mouseMoved"}.get(ev.get("a"))
            if not acao:
                return True
            botao = ev.get("b") if ev.get("b") in ("left", "right") else "left"
            if acao == "mousePressed":
                self.botoes = 1 if botao == "left" else 2
            elif acao == "mouseReleased":
                self.botoes = 0
            await cdp.send("Input.dispatchMouseEvent", {
                "type": acao, "x": x, "y": y,
                "button": "none" if acao == "mouseMoved" and not self.botoes else botao,
                "buttons": self.botoes,
                "clickCount": int(num(ev.get("n"), 0, 3, 1)) if acao != "mouseMoved" else 0})
            return True
        if t == "texto":
            s = str(ev.get("s") or "")[:2000]
            if s:
                await cdp.send("Input.insertText", {"text": s})
            return True
        if t == "tecla":
            k = str(ev.get("k") or "")
            mod = int(num(ev.get("mod"), 0, 15))
            if k in TECLAS:
                code, vk, texto = TECLAS[k]
                base = {"key": k, "code": code, "windowsVirtualKeyCode": vk, "modifiers": mod}
                down = dict(base, type="keyDown")
                if texto and not (mod & 6):
                    down.update(text=texto, unmodifiedText=texto)
                await cdp.send("Input.dispatchKeyEvent", down)
                await cdp.send("Input.dispatchKeyEvent", dict(base, type="keyUp"))
            elif len(k) == 1 and k.lower() in ATALHOS and (mod & 6):
                base = {"key": k.lower(), "code": "Key" + k.upper(),
                        "windowsVirtualKeyCode": ord(k.upper()), "modifiers": mod}
                await cdp.send("Input.dispatchKeyEvent",
                               dict(base, type="keyDown", commands=[ATALHOS[k.lower()]]))
                await cdp.send("Input.dispatchKeyEvent", dict(base, type="keyUp"))
            return True
        if t == "tamanho":
            await page.set_viewport_size({
                "width": int(num(ev.get("w"), 480, TELA_MAX["width"], VIEWPORT_INICIAL["width"])),
                "height": int(num(ev.get("h"), 360, TELA_MAX["height"], VIEWPORT_INICIAL["height"]))})
            return True
        return False


# ── Fora da barra de tarefas (Windows) ────────────────────────────────────── A
# janela fora da tela ainda aparecia na barra (e no Alt+Tab) como um Chromium a
# mais por conta aberta.
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_APPWINDOW = 0x00040000
_WS_EX_NOACTIVATE = 0x08000000
_GW_OWNER = 4


def esconder_da_barra(pids):
    """
    Tira da barra de tarefas as janelas destes processos. Devolve (janelas achadas,
    janelas mudadas): só dá por resolvido quem achou.
    """
    if sys.platform != "win32" or not pids:
        return 0, 0
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetWindowLongW.restype = ctypes.c_long
    janelas = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visitar(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, _GW_OWNER):
            janelas.append(hwnd)
        return True

    user32.EnumWindows(visitar, 0)
    mudadas = 0
    for h in janelas:
        ex = user32.GetWindowLongW(h, _GWL_EXSTYLE)
        novo = (ex | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE) & ~_WS_EX_APPWINDOW
        if novo != ex:
            # a barra só relê o estilo quando a janela reaparece
            user32.ShowWindow(h, 0)                  # SW_HIDE
            user32.SetWindowLongW(h, _GWL_EXSTYLE, novo)
            user32.ShowWindow(h, 4)                  # SW_SHOWNOACTIVATE
            mudadas += 1
    return len(janelas), mudadas
