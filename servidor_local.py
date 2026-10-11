"""servidor_local.py — duas proteções comuns ao painel (5020) e ao bot de DM (5022)."""
import ipaddress
import socket

from werkzeug.serving import WSGIRequestHandler

_NOMES_LOCAIS = {"localhost", socket.gethostname().lower()}


def host_permitido(host: str) -> bool:
    """`Host` aceito: IP literal, localhost ou o nome desta máquina."""
    h = (host or "").strip().lower()
    if not h:
        return False
    if h.startswith("["):                     # [::1]:5020
        fim = h.find("]")
        resto = h[fim + 1:] if fim > 0 else "x"
        if resto and not (resto.startswith(":") and resto[1:].isdigit()):
            return False                      # [::1]lixo
        h = h[1:fim]
    elif h.count(":") == 1:                   # nome:porta ou ipv4:porta
        h = h.split(":", 1)[0]
    h = h.rstrip(".")
    if h in _NOMES_LOCAIS or h.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def pedido_de_outro_site(headers, host="") -> bool:
    """O navegador marcou o pedido como vindo de outro site?"""
    if (headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
        return True
    origem = headers.get("Origin")
    if origem is None or origem == "":
        return False
    if origem == "null":
        return True
    from urllib.parse import urlparse
    return urlparse(origem).netloc.lower() != (host or "").lower()


def instalar_guarda_host(app):
    """
    Recusa (403) pedido com `Host` de outro site e, nas rotas /api, pedido que outro
    site disparou pelo navegador. Abrir a página do painel por um link de outro site
    continua valendo (não é /api).
    """
    from flask import request

    @app.before_request
    def _guarda_host():
        if not host_permitido(request.host):
            return ("Host não permitido: este serviço só atende por localhost ou IP.",
                    403, {"Content-Type": "text/plain; charset=utf-8"})
        if request.path.startswith("/api/") and pedido_de_outro_site(request.headers, request.host):
            return ("Pedido de outro site recusado.",
                    403, {"Content-Type": "text/plain; charset=utf-8"})
    return _guarda_host


def desta_maquina(remote_addr) -> bool:
    """O pedido saiu deste computador? (tela, teclado e disparo das contas)"""
    return remote_addr in ("127.0.0.1", "::1")


class RequestHandlerSemAtraso(WSGIRequestHandler):
    """Liga TCP_NODELAY em cada conexão aceita."""

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
