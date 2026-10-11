"""whatsapp_stub.py — Canal WhatsApp (agora LIGADO ao bot real)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

try:
    import requests
except ImportError:
    requests = None

URL = getattr(config, "WHATSAPP_BOT_URL", "http://localhost:5021")


def disponivel() -> bool:
    """True se o bot está no ar E com a conta conectada."""
    if requests is None:
        return False
    try:
        r = requests.get(f"{URL}/api/status", timeout=4)
        return bool(r.json().get("pronto"))
    except Exception:
        return False


def status() -> dict:
    if requests is None:
        return {"pronto": False, "erro": "biblioteca requests não instalada"}
    try:
        return requests.get(f"{URL}/api/status", timeout=4).json()
    except Exception as e:
        return {"pronto": False, "erro": f"bot offline ({e})"}


def tem_whatsapp(numero: str) -> dict:
    """Verificação REAL pelo WhatsApp conectado."""
    if requests is None:
        return {"tem": None, "erro": "requests não instalado"}
    try:
        r = requests.get(f"{URL}/api/verificar", params={"numero": numero}, timeout=20)
        return r.json()
    except Exception as e:
        return {"tem": None, "erro": f"bot offline ({e})"}


def enviar(numero: str, mensagem: str) -> dict:
    """Envia uma mensagem. Mesma assinatura do contrato original."""
    if not numero or not mensagem:
        return {"status": "erro", "detalhe": "número e mensagem são obrigatórios"}
    if requests is None:
        return {"status": "erro", "detalhe": "requests não instalado"}
    try:
        r = requests.post(f"{URL}/api/enviar",
                          json={"numero": numero, "mensagem": mensagem}, timeout=40)
        d = r.json()
        return {"status": "sent" if d.get("ok") else "failed",
                "numero": numero, "detalhe": d.get("erro", "enviado")}
    except Exception as e:
        return {"status": "failed", "numero": numero,
                "detalhe": f"bot de WhatsApp offline ({e}). Suba: node senders/whatsapp_bot/server.js"}


def enviar_lote(alvos: list) -> dict:
    """
    alvos = [{numero, mensagem}]. O bot envia devagar (25s a 60s entre cada), para não
    queimar o número.
    """
    if requests is None:
        return {"status": "erro", "detalhe": "requests não instalado"}
    try:
        r = requests.post(f"{URL}/api/enviar_lote", json={"alvos": alvos}, timeout=30)
        return r.json()
    except Exception as e:
        return {"ok": False, "erro": f"bot offline ({e})"}


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print("Bot de WhatsApp:", URL)
    print("Status:", status())
    print("Conectado?", disponivel())
