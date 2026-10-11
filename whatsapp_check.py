"""whatsapp_check.py — Normaliza telefones BR e confirma WhatsApp."""

import json
import os
import re
import urllib.request
from typing import Dict, Optional

# Por padrão consulta o bot próprio (senders/whatsapp_bot, porta 5021). Se ele
# estiver desligado ou sem conta conectada, a verificação cai na heurística.
_PADRAO = "http://localhost:5021/api/verificar"
WHATSAPP_CHECK_URL = os.environ.get("WHATSAPP_CHECK_URL", _PADRAO)


def normalizar(numero: str) -> Optional[Dict]:
    """Extrai DDD + número de um telefone BR. Retorna None se inválido."""
    d = re.sub(r"\D", "", numero or "")
    if d.startswith("55") and len(d) >= 12:
        d = d[2:]
    if len(d) < 10 or len(d) > 11:
        return None
    ddd = d[:2]
    resto = d[2:]
    is_mobile = len(resto) == 9 and resto.startswith("9")
    e164 = "55" + ddd + resto
    return {"ddd": ddd, "numero": resto, "is_mobile": is_mobile,
            "e164": e164, "formatado": f"({ddd}) {resto[:-4]}-{resto[-4:]}"}


def wa_link(numero: str) -> Optional[str]:
    n = normalizar(numero)
    return f"https://wa.me/{n['e164']}" if n else None


def verificar_whatsapp(numero: str) -> Dict:
    """
    Retorna {tem, tipo, link, motivo}. tem: True | False | None(indefinido) tipo:
    'verificado' (via bot) | 'provavel' | 'improvavel' | 'invalido'
    """
    n = normalizar(numero)
    if not n:
        return {"tem": None, "tipo": "invalido", "link": None,
                "motivo": "número inválido para WhatsApp"}
    link = f"https://wa.me/{n['e164']}"

    # verificação REAL (se um bot estiver plugado)
    if WHATSAPP_CHECK_URL:
        try:
            url = f"{WHATSAPP_CHECK_URL}?numero={n['e164']}"
            req = urllib.request.Request(url, headers={"User-Agent": "clinica_prospector"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8"))
            bruto = data.get("tem", data.get("exists", data.get("registered")))
            # `tem: null` = o bot respondeu mas não tem conta conectada. Isso NÃO
            # é "não tem WhatsApp": é ausência de resposta, e cai na heurística.
            if bruto is not None:
                return {"tem": bool(bruto), "tipo": "verificado", "link": link,
                        "motivo": "confirmado pelo bot de WhatsApp"}
        except Exception:
            pass  # cai na heurística

    if n["is_mobile"]:
        return {"tem": True, "tipo": "provavel", "link": link,
                "motivo": "celular válido — quase sempre tem WhatsApp (não verificado ao vivo)"}
    return {"tem": False, "tipo": "improvavel", "link": link,
            "motivo": "número fixo — normalmente não tem WhatsApp"}


if __name__ == "__main__":
    for t in ["(51) 9 9527-7648", "(21) 3555-2121", "5521999998888", "123"]:
        print(t, "->", verificar_whatsapp(t))
