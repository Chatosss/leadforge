"""web_enrich.py — Apuração de dados visitando o SITE da clínica."""

import re
import ssl
import urllib.request
from typing import Dict, List
from urllib.parse import urljoin, urlparse

import db
import whatsapp_check

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
_TIMEOUT = 10
_PAGINAS = ["", "contato", "contato/", "fale-conosco", "fale-conosco/", "contact", "sobre"]

_RE_TEL = re.compile(r"(?:\+?55\s*)?\(?\d{2}\)?[\s.\-]?9?\s?\d{4}[\s.\-]?\d{4}")
_RE_TELLINK = re.compile(r'href=["\']tel:([+\d\s().\-]+)["\']', re.I)
_RE_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_RE_WA = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=|whatsapp://send\?phone=)(\+?\d{10,15})")
_RE_IG = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)")
_IG_IGNORAR = {"p", "reel", "reels", "explore", "accounts", "stories", "tv", ""}

# DDDs brasileiros válidos (evita capturar máscaras/exemplos como 00, 01, 99...)
_DDDS = {11,12,13,14,15,16,17,18,19,21,22,24,27,28,31,32,33,34,35,37,38,
         41,42,43,44,45,46,47,48,49,51,53,54,55,61,62,63,64,65,66,67,68,69,
         71,73,74,75,77,79,81,82,83,84,85,86,87,88,89,91,92,93,94,95,96,97,98,99}


def _telefone_valido(formatado: str) -> bool:
    """Rejeita placeholders/máscaras: DDD inexistente, dígitos repetidos, sequência."""
    n = whatsapp_check.normalizar(formatado)
    if not n:
        return False
    if int(n["ddd"]) not in _DDDS:
        return False
    sub = n["numero"]
    if len(set(sub)) <= 2:               # 999999999, 0000..., 12121212
        return False
    if re.search(r"(\d)\1{4,}", sub):    # 5+ dígitos iguais seguidos
        return False
    if sub in ("123456789", "12345678", "987654321"):
        return False
    return True


def _fetch(url: str) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "pt-BR"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx) as r:
        raw = r.read(600_000)  # limita a 600KB
    try:
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return raw.decode("latin-1", errors="ignore")


def apurar_site(url: str) -> Dict:
    """
    Extrai contatos do site. Retorna {ok, telefones, emails, whatsapps, instagram,
    paginas}.
    """
    if not url:
        return {"ok": False, "erro": "sem site"}
    if not url.startswith("http"):
        url = "http://" + url
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    telefones, tel_links, emails, whatsapps, instagrams = set(), set(), set(), set(), set()
    lidas = 0
    for p in _PAGINAS:
        alvo = url if p == "" else urljoin(base + "/", p)
        try:
            html = _fetch(alvo)
        except Exception:
            continue
        lidas += 1
        # WhatsApp (mais confiável — número explícito no link)
        for m in _RE_WA.findall(html):
            n = whatsapp_check.normalizar(m)
            if n and _telefone_valido(n["formatado"]):
                whatsapps.add(n["formatado"])
        # telefones em links tel: (clique-para-ligar reais — prioridade)
        for m in _RE_TELLINK.findall(html):
            n = whatsapp_check.normalizar(m)
            if n and _telefone_valido(n["formatado"]):
                tel_links.add(n["formatado"])
        # telefones em texto livre (validados contra placeholders)
        for m in _RE_TEL.findall(html):
            n = whatsapp_check.normalizar(m)
            if n and _telefone_valido(n["formatado"]):
                telefones.add(n["formatado"])
        # emails (ignora imagens/assets)
        for m in _RE_EMAIL.findall(html):
            if not re.search(r"\.(png|jpg|jpeg|gif|svg|webp)$", m, re.I):
                emails.add(m.lower())
        # instagram
        for m in _RE_IG.findall(html):
            if m.lower() not in _IG_IGNORAR:
                instagrams.add(m)
        if lidas >= 3 and (tel_links or whatsapps):
            break  # já achou o suficiente

    if lidas == 0:
        return {"ok": False, "erro": "site inacessível"}
    # telefones de links tel: vêm primeiro (mais confiáveis), sem duplicar
    tels = list(dict.fromkeys([*sorted(tel_links), *sorted(telefones)]))
    return {
        "ok": True,
        "telefones": tels,
        "telefones_confiaveis": sorted(set(tel_links) | set(whatsapps)),  # de links tel:/wa.me
        "emails": sorted(emails),
        "whatsapps": sorted(whatsapps),
        "instagram": sorted(instagrams)[:1],
        "paginas_lidas": lidas,
    }


def apurar_lead(lead_id: int) -> Dict:
    """Apura o site do lead, corrige contatos e confirma WhatsApp. Registra nota."""
    import dashboard_db as ddb
    lead = db.get_lead(lead_id)
    if not lead:
        return {"ok": False, "erro": "lead não encontrado"}
    site = lead.get("website", "")
    if not site:
        return {"ok": False, "erro": "lead sem site para apurar"}

    res = apurar_site(site)
    if not res.get("ok"):
        return res

    # telefone a aplicar: SÓ os confiáveis (de links tel:/wa.me). Texto-livre não
    # sobrescreve — vira só sugestão em nota (evita trocar por número errado).
    confiaveis = res.get("telefones_confiaveis", [])
    melhor_tel = next((t for t in confiaveis if (whatsapp_check.normalizar(t) or {}).get("is_mobile")),
                      confiaveis[0] if confiaveis else "")
    # para checagem de WhatsApp, usa o confiável; senão o melhor candidato de texto
    tel_para_wa = melhor_tel or (res["telefones"][0] if res["telefones"] else "")
    wa = whatsapp_check.verificar_whatsapp(tel_para_wa) if tel_para_wa else {"tem": None}

    # monta atualização (preenche vazios; corrige telefone SÓ com fonte confiável)
    campos = {}
    mudancas = []
    tel_atual = (lead.get("telefone") or "").strip()
    if melhor_tel and whatsapp_check.normalizar(melhor_tel):
        if not tel_atual:
            campos["telefone"] = melhor_tel; mudancas.append(f"telefone: {melhor_tel}")
        elif whatsapp_check.normalizar(tel_atual) and \
                whatsapp_check.normalizar(tel_atual)["e164"] != whatsapp_check.normalizar(melhor_tel)["e164"]:
            campos["telefone"] = melhor_tel
            mudancas.append(f"telefone corrigido: {tel_atual} → {melhor_tel} (link do site)")
    elif res["telefones"] and not tel_atual:
        # sem confiável, mas o lead não tinha telefone: registra sugestões
        mudancas.append("telefones no site (verificar): " + ", ".join(res["telefones"][:3]))
    if res["emails"] and not (lead.get("email") or "").strip():
        campos["email"] = res["emails"][0]; mudancas.append(f"e-mail: {res['emails'][0]}")
    if res["instagram"] and not (lead.get("instagram") or "").strip():
        campos["instagram"] = "@" + res["instagram"][0]; mudancas.append(f"instagram: @{res['instagram'][0]}")

    if campos:
        ddb.atualizar_lead(lead_id, campos)
    # grava whatsapp
    with db.conn() as con:
        con.execute("UPDATE leads SET whatsapp=?, tem_whatsapp=?, site_apurado=1 WHERE id=?",
                    (wa.get("link") or "", 1 if wa.get("tem") else (0 if wa.get("tem") is False else None), lead_id))

    resumo = "Apuração pelo site: " + ("; ".join(mudancas) if mudancas else "nada novo a corrigir")
    if wa.get("tem") is not None:
        resumo += f" · WhatsApp {'SIM' if wa['tem'] else 'não'} ({wa['tipo']})"
    ddb.add_nota(lead_id, resumo)

    return {"ok": True, "achados": res, "mudancas": mudancas, "whatsapp": wa,
            "melhor_telefone": melhor_tel}


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) > 1:
        print(json.dumps(apurar_site(sys.argv[1]), ensure_ascii=False, indent=2))
    else:
        print("uso: python web_enrich.py https://site-da-clinica.com.br")
