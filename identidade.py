"""
identidade.py — Cruzamento de dados: WhatsApp, e-mail e Instagram são da MESMA clínica?
"""

import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from typing import Dict, List, Optional
from urllib.parse import urlparse

import config  # noqa: F401  (reconfigura o console para UTF-8)
import db
import web_enrich
import whatsapp_check

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
_TIMEOUT = 12

CANAIS = ("whatsapp", "email", "instagram")

# Domínios que nunca são o site oficial de uma clínica.
_NAO_E_SITE = {
    "instagram.com", "facebook.com", "linkedin.com", "youtube.com", "tiktok.com",
    "twitter.com", "x.com", "wa.me", "api.whatsapp.com", "google.com", "goo.gl",
    "duckduckgo.com", "bing.com",
    "maps.google.com", "doctoralia.com.br", "boaconsulta.com", "telelistas.net",
    "apontador.com.br", "solutudo.com.br", "hotfrog.com.br", "cylex.com.br",
    "econodata.com.br", "guiamais.com.br", "ondeir.com.br", "yelp.com",
    "consultaremedios.com.br", "encontraodonto.com.br", "clinicorp.com",
    "booking.com", "tripadvisor.com", "wikipedia.org", "jusbrasil.com.br",
    "cnpj.biz", "casadosdados.com.br", "empresascnpj.com", "reclameaqui.com.br",
    "dentmap.com.br", "agendarconsulta.com", "clinicasdobrasil.com.br",
    "guiaodontologico.com.br", "achouodonto.com.br", "linkedin.com",
}

# Provedores públicos: um e-mail @gmail nunca prova posse pelo domínio.
_EMAIL_PUBLICO = {
    "gmail.com", "hotmail.com", "outlook.com", "outlook.com.br", "yahoo.com",
    "yahoo.com.br", "bol.com.br", "uol.com.br", "terra.com.br", "live.com",
    "icloud.com", "globo.com", "ig.com.br", "zipmail.com.br", "msn.com",
}

# DDD -> UF. Telefone de outro estado indica homônima, não a mesma clínica.
DDD_UF = {
    "11": "SP", "12": "SP", "13": "SP", "14": "SP", "15": "SP", "16": "SP",
    "17": "SP", "18": "SP", "19": "SP", "21": "RJ", "22": "RJ", "24": "RJ",
    "27": "ES", "28": "ES", "31": "MG", "32": "MG", "33": "MG", "34": "MG",
    "35": "MG", "37": "MG", "38": "MG", "41": "PR", "42": "PR", "43": "PR",
    "44": "PR", "45": "PR", "46": "PR", "47": "SC", "48": "SC", "49": "SC",
    "51": "RS", "53": "RS", "54": "RS", "55": "RS", "61": "DF", "62": "GO",
    "63": "TO", "64": "GO", "65": "MT", "66": "MT", "67": "MS", "68": "AC",
    "69": "RO", "71": "BA", "73": "BA", "74": "BA", "75": "BA", "77": "BA",
    "79": "SE", "81": "PE", "82": "AL", "83": "PB", "84": "RN", "85": "CE",
    "86": "PI", "87": "PE", "88": "CE", "89": "PI", "91": "PA", "92": "AM",
    "93": "PA", "94": "PA", "95": "RR", "96": "AP", "97": "AM", "98": "MA",
    "99": "MA",
}


# SINAIS — o vocabulário de evidências peso, descrição, é_prova_de_posse Peso >=
# PESO_PROVA_FORTE numa evidência de posse é conclusivo sozinho: significa que o
# contato está publicado num canal que a própria clínica controla.
PESO_PROVA_FORTE = 55

SINAIS = {
    # ── Provas de posse: ligam o contato à clínica por um caminho verificável ──
    # Confirmação humana: você conferiu o contato por fora (ligou, pegou no balcão,
    # a clínica te passou).
    "confirmado_manualmente": (100, "confirmado manualmente por você", True),
    "e_o_telefone_do_maps":   (60, "é o telefone que a clínica cadastrou no Google", True),
    "ig_declarado_no_maps":   (60, "a clínica declarou este perfil como site no Google", True),
    "wa_declarado_no_maps":   (60, "a clínica declarou este WhatsApp como site no Google", True),
    "publicado_no_site":      (55, "publicado no site oficial da clínica", True),
    "email_do_dominio":       (60, "e-mail usa o domínio do site oficial", True),
    "wa_link_no_site":        (60, "link de WhatsApp publicado no site oficial", True),
    "ig_link_no_site":        (60, "perfil de Instagram linkado no site oficial", True),
    "ig_tem_tel_da_clinica":  (55, "o perfil exibe o telefone da clínica", True),
    "site_tem_endereco":      (25, "o site publica o endereço do Google", False),
    "ig_do_maps":             (30, "perfil já registrado no cadastro do lead", False),

    # ── Coerência: apoiam, mas nunca provam sozinhas ──
    "nome_bate":              (12, "nome bate com o da clínica", False),
    "cidade_bate":            (10, "cidade confere", False),
    "ddd_bate":               (12, "DDD compatível com a cidade da clínica", False),
    "aparece_na_busca":       (8,  "aparece na busca pelo nome da clínica", False),
    "celular_valido":         (6,  "celular brasileiro válido", False),
    "wa_registrado":          (25, "número confirmado no WhatsApp", False),
}

CONFLITOS = {
    "ddd_de_outro_estado":    (-70, "telefone de outro estado: provável homônima"),
    "wa_nao_registrado":      (-60, "número não tem conta de WhatsApp"),
    "fixo_nao_verificado":    (-45, "telefone fixo e o bot não pôde confirmar a conta"),
    "email_publico_sem_prova": (-35, "e-mail de provedor público sem prova de posse"),
    # -45 e não -30: precisa passar de PESO_CONFLITO_BLOQUEIA_PROVA. Um e-mail
    # em domínio de terceiro publicado numa página é da plataforma que hospeda,
    # não da clínica. Era assim que `press@google.com` saía como confirmado.
    "dominio_estranho":       (-45, "e-mail em domínio de terceiro, não da clínica"),
    "nome_nao_bate":          (-20, "nada no contato lembra o nome da clínica"),
    "site_nao_confirmado":    (-25, "veio de um site que não foi confirmado como oficial"),
}

# Faixas do veredito.
LIMIAR_CONFIRMADO = 85
LIMIAR_PROVAVEL = 60
LIMIAR_INCERTO = 30

# Conflito a partir deste peso derruba o veredito para `conflito`.
PESO_CONFLITO_GRAVE = -50
# Conflito a partir deste peso já impede a prova de posse de sustentar sozinha o
# candidato. Serve para o caso do telefone fixo: o número é da clínica (prova de
# posse legítima), mas isso não quer dizer que tenha conta de WhatsApp.
PESO_CONFLITO_BLOQUEIA_PROVA = -40


# ══════════════════════════════════════════════════════════════════════════════
#  Utilidades
# ══════════════════════════════════════════════════════════════════════════════

def _sem_acento(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t or "")
                   if unicodedata.category(c) != "Mn")


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _sem_acento(t or "").lower())


_PALAVRAS_VAZIAS = {
    "clinica", "consultorio", "centro", "odontologia", "odontologica", "odonto",
    "dr", "dra", "doutor", "doutora", "estetica", "saude", "instituto", "espaco",
    "studio", "sorriso", "dental", "medica", "medico", "e", "de", "da", "do",
    "dos", "das", "the", "ltda", "me", "eireli",
    # locais genéricos de e-mail: não distinguem uma clínica de outra
    "contato", "atendimento", "comercial", "faleconosco", "recepcao", "adm",
    "financeiro", "secretaria", "agendamento", "info", "email", "mail",
}


def _tokens(nome: str) -> set:
    """Palavras distintivas do nome (descarta genéricas como 'clínica', 'odonto')."""
    brutas = re.findall(r"[a-z0-9]{3,}", _sem_acento(nome or "").lower())
    fortes = {p for p in brutas if p not in _PALAVRAS_VAZIAS}
    return fortes or set(brutas)


def _dominio(url: str) -> str:
    if not url:
        return ""
    if not url.startswith("http"):
        url = "http://" + url
    return urlparse(url).netloc.replace("www.", "").lower()


# Páginas que a clínica pode declarar como "site" no Google, mas cujo CONTEÚDO
# não é dela: o que está publicado ali é da plataforma.
_CONTEUDO_NAO_E_DA_CLINICA = {
    "instagram.com", "facebook.com", "m.facebook.com", "fb.com",
    "api.whatsapp.com", "wa.me", "twitter.com", "x.com", "tiktok.com",
    "youtube.com", "youtu.be", "t.me", "linkedin.com",
}


def _conteudo_confiavel(dom: str) -> bool:
    d = (dom or "").lower().replace("www.", "")
    return not any(d == b or d.endswith("." + b) for b in _CONTEUDO_NAO_E_DA_CLINICA)


def _e_site_possivel(dom: str) -> bool:
    if not dom or "." not in dom:
        return False
    return not any(dom == b or dom.endswith("." + b) for b in _NAO_E_SITE)


def _cidade_do_endereco(endereco: str) -> str:
    """
    Extrai a cidade de um endereço do Maps. Formato típico: 'Rua X, 123 - Bairro, Cidade
    - UF, CEP'.
    """
    if not endereco:
        return ""
    m = re.search(r",\s*([^,\-]+?)\s*-\s*[A-Z]{2}\b", endereco)
    if m:
        return m.group(1).strip()
    # 'Rua X, 123' não tem cidade: dois campos, o segundo numérico
    partes = [p.strip() for p in endereco.split(",") if p.strip()]
    if len(partes) >= 2 and not re.match(r"^n?°?\s*\d", partes[-1], re.I):
        return partes[-1]
    return ""


def _cidade_da_operacao() -> str:
    """Cidade base da operação (config.EMPRESA_CIDADE), sem a UF."""
    return (getattr(config, "EMPRESA_CIDADE", "") or "").split(",")[0].strip()


# Regras de @ do Instagram: até 30 caracteres, letras/números/ponto/underscore,
# sem começar ou terminar em ponto. Filtra o lixo que o regex genérico captura do
# HTML do próprio Instagram (rsrc.php, static, etc.).
_RE_HANDLE = re.compile(r"^[a-z0-9_](?:[a-z0-9_.]{0,28}[a-z0-9_])?$")
_HANDLE_PROIBIDO = {
    # caminhos internos do Instagram
    "p", "reel", "reels", "explore", "accounts", "stories", "tv", "direct",
    "about", "legal", "privacy", "terms", "developer", "developers", "api",
    "static", "rsrc", "graphql", "web", "ajax", "emails", "session", "oauth",
    # marcas: aparecem em barra de compartilhamento e rodapé de site, nunca são
    # o perfil da clínica. Sem isto, um site com botão de WhatsApp no rodapé
    # gerava o candidato "@whatsapp" e a DM iria para a conta oficial da Meta.
    "whatsapp", "facebook", "instagram", "tiktok", "youtube", "twitter", "x",
    "linkedin", "telegram", "google", "maps", "wa", "share", "home", "index",
    "site", "contato", "login", "signup", "help", "support",
}
_RE_ARQUIVO = re.compile(r"\.(php|js|css|json|html?|png|jpe?g|svg|gif|webp|ico|map)$", re.I)


def _handle_valido(u: str) -> bool:
    u = (u or "").strip().lower()
    if not u or not _RE_HANDLE.match(u) or ".." in u:
        return False
    if u in _HANDLE_PROIBIDO or _RE_ARQUIVO.search(u):
        return False
    return True


def _fetch(url: str) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Accept-Language": "pt-BR,pt;q=0.9"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx) as r:
        return r.read(600_000).decode("utf-8", errors="ignore")


def _digitos(t: str) -> str:
    return re.sub(r"\D", "", t or "")


def _mesmo_telefone(a: str, b: str) -> bool:
    """Compara pelos 8 dígitos finais: imune a 55, DDD e ao 9 extra."""
    da, dbb = _digitos(a)[-8:], _digitos(b)[-8:]
    return len(da) == 8 and da == dbb


# ══════════════════════════════════════════════════════════════════════════════
#  Etapa 1: descoberta na web
# ══════════════════════════════════════════════════════════════════════════════

def _buscar(termo: str) -> List[Dict]:
    """Pesquisa na web e devolve [{url, dominio, titulo}]. Reusa o motor do dossiê."""
    try:
        import coleta_profunda
        achados = coleta_profunda._buscar_web(termo)
    except Exception:
        return []
    saida = []
    for r in achados.get("resultados", []):
        saida.append({"dominio": r.get("dominio", ""), "titulo": r.get("titulo", ""),
                      "tipo": r.get("tipo", ""), "url": "https://" + r.get("dominio", "")})
    return saida


def _site_contem_endereco(url: str, endereco: str) -> bool:
    """
    Confere se o site publica o endereço do Google Maps. Serve de prova quando a clínica
    não tem telefone no cadastro. Compara logradouro e CEP, ignorando acentuação e
    pontuação, porque cada site escreve o endereço de um jeito.
    """
    if not url or not endereco:
        return False
    logradouro = re.split(r"[-,]", endereco)[0].strip()
    cep = re.search(r"\d{5}-?\d{3}", endereco)
    agulhas = []
    if len(_norm(logradouro)) >= 10:
        agulhas.append(_norm(logradouro))
    if cep:
        agulhas.append(_digitos(cep.group(0)))
    if not agulhas:
        return False
    try:
        bruto = _fetch(url if url.startswith("http") else "http://" + url)
    except Exception:
        return False
    texto = _norm(re.sub(r"<[^>]+>", " ", bruto))
    digitos_pagina = _digitos(bruto)
    return any(a in texto or a in digitos_pagina for a in agulhas)


def _perfil_instagram(username: str) -> Dict:
    """
    Lê o que o Instagram expõe publicamente do perfil (og:title / og:description). O
    Instagram bloqueia boa parte do acesso automatizado, então isto é best-effort:
    falhou, devolve vazio e o perfil simplesmente não ganha o sinal.
    """
    user = (username or "").strip().lstrip("@")
    if not user:
        return {}
    try:
        html = _fetch(f"https://www.instagram.com/{user}/")
    except Exception as e:
        return {"erro": str(e)}
    def _meta(prop):
        m = re.search(rf'<meta[^>]+property=["\']{prop}["\'][^>]+content=["\']([^"\']+)',
                      html, re.I)
        return m.group(1) if m else ""
    desc = _meta("og:description")
    titulo = _meta("og:title")
    if not desc and not titulo:
        return {"bloqueado": True}
    return {"titulo": titulo, "descricao": desc,
            "telefones": re.findall(r"(?:\+?55\s*)?\(?\d{2}\)?[\s.\-]?9?\s?\d{4}[\s.\-]?\d{4}",
                                    f"{titulo} {desc}")}


# ══════════════════════════════════════════════════════════════════════════════
#  Etapa 2: montar o dossiê de fontes
# ══════════════════════════════════════════════════════════════════════════════

def coletar(lead: Dict, usar_busca: bool = True, max_sites: int = 4) -> Dict:
    """
    Reúne tudo que existe sobre a clínica, marcando de ONDE cada dado veio. Não pontua
    nada aqui: só coleta.
    """
    nome = lead.get("nome") or ""
    tel_maps = (lead.get("telefone") or "").strip()
    endereco = lead.get("endereco") or ""
    cidade = _cidade_do_endereco(endereco)
    cidade_origem = "endereço" if cidade else ""
    if not cidade:
        cidade = _cidade_da_operacao()
        cidade_origem = "cidade da operação (suposição)" if cidade else ""
    ddd_maps = (whatsapp_check.normalizar(tel_maps) or {}).get("ddd", "")

    ancora = {
        "nome": nome, "telefone": tel_maps, "endereco": endereco, "cidade": cidade,
        "cidade_origem": cidade_origem,
        "ddd": ddd_maps, "uf": DDD_UF.get(ddd_maps, ""),
        "site_declarado": (lead.get("website") or "").strip(),
        "instagram_cadastrado": (lead.get("instagram") or "").strip(),
    }

    dossie = {"ancora": ancora, "sites": [], "busca": [], "instagram": {}, "avisos": []}

    # O campo "site" do Google costuma trazer, em vez de um site, o perfil do
    # Instagram, um link de WhatsApp ou um Linktree.
    declarado = ancora["site_declarado"]
    if declarado:
        m_ig = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", declarado, re.I)
        if m_ig and _handle_valido(m_ig.group(1)):
            dossie["ig_declarado_no_maps"] = m_ig.group(1)
        m_wa = re.search(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=)(\+?\d{10,15})",
                         declarado, re.I)
        if m_wa:
            dossie["wa_declarado_no_maps"] = m_wa.group(1)

    # ── 2.1 Candidatos a site ────────────────────────────────────────────────
    candidatos = []
    if ancora["site_declarado"]:
        candidatos.append({"dominio": _dominio(ancora["site_declarado"]),
                           "url": ancora["site_declarado"], "origem": "maps"})

    if usar_busca:
        termo = f"{nome} {cidade}".strip() if cidade else nome
        resultados = _buscar(termo)
        dossie["busca"] = resultados
        for r in resultados:
            dom = r.get("dominio", "")
            if not _e_site_possivel(dom):
                continue
            if any(c["dominio"] == dom for c in candidatos):
                continue
            candidatos.append({"dominio": dom, "url": r.get("url") or ("https://" + dom),
                               "origem": "busca", "titulo": r.get("titulo", "")})
        # o Instagram que apareceu na busca também interessa
        for r in resultados:
            if "instagram.com" in (r.get("dominio") or ""):
                dossie.setdefault("ig_na_busca", []).append(r.get("titulo", ""))

    # ── 2.2 Visita cada candidato e lê os contatos publicados ────────────────
    for cand in candidatos[:max_sites]:
        if not _conteudo_confiavel(cand["dominio"]):
            # o @ já foi extraído da URL acima; raspar a página só traria os
            # links da própria plataforma disfarçados de contato da clínica
            dossie["sites"].append({**cand, "ok": False, "oficial": False,
                                    "erro": "perfil de rede social: conteúdo não é da clínica"})
            continue
        achado = web_enrich.apurar_site(cand["url"])
        registro = {**cand, "ok": bool(achado.get("ok")), "erro": achado.get("erro", "")}
        if achado.get("ok"):
            registro.update({
                "telefones": achado.get("telefones", []),
                "telefones_confiaveis": achado.get("telefones_confiaveis", []),
                "emails": achado.get("emails", []),
                "whatsapps": achado.get("whatsapps", []),
                "instagram": achado.get("instagram", []),
            })
            # este site é MESMO da clínica?
            tem_tel = any(_mesmo_telefone(t, tel_maps)
                          for t in achado.get("telefones", []) + achado.get("whatsapps", [])) \
                      if tel_maps else False
            # o endereço só é consultado quando o telefone não resolveu, para não
            # baixar a página de novo à toa
            tem_end = False
            if not tem_tel and cand["origem"] != "maps":
                tem_end = _site_contem_endereco(cand["url"], endereco)
            registro["tem_telefone_da_ancora"] = tem_tel
            registro["tem_endereco_da_ancora"] = tem_end
            # Diretório republica o telefone da clínica, e isso promovia o diretório a "site
            # oficial", fazendo o e-mail genérico DELE passar como sendo da clínica.
            registro["e_diretorio"] = not _e_site_possivel(cand["dominio"])
            # domínios colam as palavras ("institutoadonisborn"), então a busca
            # tem que ser por substring, não por token exato
            dom_norm = _norm(cand["dominio"])
            registro["nome_no_dominio"] = any(
                len(t) >= 4 and t in dom_norm for t in _tokens(nome))
            # Oficial em dois caminhos: da clínica no domínio.
            registro["oficial"] = (not registro["e_diretorio"]) and (
                cand["origem"] == "maps"
                or ((tem_tel or tem_end) and registro["nome_no_dominio"]))
        dossie["sites"].append(registro)

    # ── 2.3 Instagram: perfil público ────────────────────────────────────────
    users = []
    ig_cad = ancora["instagram_cadastrado"]
    if ig_cad:
        users.append(re.sub(r"^@", "", ig_cad.split("instagram.com/")[-1].strip("/@ ")))
    for s in dossie["sites"]:
        for u in s.get("instagram", []) or []:
            if u and u not in users:
                users.append(u)
    for u in users[:3]:
        dossie["instagram"][u] = _perfil_instagram(u)

    return dossie


# ══════════════════════════════════════════════════════════════════════════════
#  Etapa 3: ranquear candidatos por canal
# ══════════════════════════════════════════════════════════════════════════════

class _Cand:
    """Um contato candidato, com as evidências que o sustentam."""

    def __init__(self, canal: str, valor: str, rotulo: str = ""):
        self.canal = canal
        self.valor = valor
        self.rotulo = rotulo or valor
        self.evidencias: List[Dict] = []
        self.conflitos: List[Dict] = []

    def sinal(self, chave: str, detalhe: str = ""):
        if chave not in SINAIS or any(e["sinal"] == chave for e in self.evidencias):
            return
        peso, texto, posse = SINAIS[chave]
        self.evidencias.append({"sinal": chave, "peso": peso, "texto": texto,
                                "posse": posse, "detalhe": detalhe})

    def conflito(self, chave: str, detalhe: str = ""):
        if chave not in CONFLITOS or any(c["sinal"] == chave for c in self.conflitos):
            return
        peso, texto = CONFLITOS[chave]
        self.conflitos.append({"sinal": chave, "peso": peso, "texto": texto,
                               "detalhe": detalhe})

    @property
    def tem_posse(self) -> bool:
        return any(e["posse"] for e in self.evidencias)

    @property
    def prova_forte(self) -> Optional[Dict]:
        """
        A evidência que, sozinha, encerra a discussão: o contato está publicado num
        canal que a própria clínica controla (o cadastro do Google ou o site que ela
        declarou lá). Semelhança de nome nunca chega aqui.
        """
        fortes = [e for e in self.evidencias
                  if e["posse"] and e["peso"] >= PESO_PROVA_FORTE]
        return max(fortes, key=lambda e: e["peso"]) if fortes else None

    @property
    def _conflito_grave(self) -> bool:
        return any(c["peso"] <= PESO_CONFLITO_GRAVE for c in self.conflitos)

    @property
    def _prova_anulada(self) -> bool:
        """Há conflito pesado o bastante para a prova de posse não bastar."""
        return any(c["peso"] <= PESO_CONFLITO_BLOQUEIA_PROVA for c in self.conflitos)

    @property
    def confianca(self) -> int:
        bruto = sum(e["peso"] for e in self.evidencias) + sum(c["peso"] for c in self.conflitos)
        # Havendo prova de posse, ela É a confiança: um conflito leve (o @ não
        # lembrar o nome da clínica) não pode fazer a nota desmentir o veredito.
        if self.prova_forte and not self._prova_anulada:
            bruto = max(bruto, LIMIAR_CONFIRMADO)
        return max(0, min(100, bruto))

    @property
    def veredito(self) -> str:
        # Conflito grave derruba o candidato por mais evidência que ele tenha:
        # telefone de outro estado é homônima, e conta inexistente é conta
        # inexistente. Nenhum acúmulo de pontos compensa isso.
        if self._conflito_grave:
            return "conflito"
        # Prova forte vence conflito leve, e deve mesmo: se a clínica declarou o perfil
        # no próprio cadastro do Google, o handle não lembrar o nome dela é irrelevante.
        if self.prova_forte and not self._prova_anulada:
            return "confirmado"
        c = self.confianca
        if c >= LIMIAR_CONFIRMADO and self.tem_posse and not self.conflitos:
            return "confirmado"
        if c >= LIMIAR_PROVAVEL:
            return "provavel"
        if c >= LIMIAR_INCERTO:
            return "incerto"
        return "descartado"

    def dict(self) -> Dict:
        forte = self.prova_forte
        return {"canal": self.canal, "valor": self.valor, "rotulo": self.rotulo,
                "confianca": self.confianca, "veredito": self.veredito,
                "tem_prova_de_posse": self.tem_posse,
                "prova_forte": forte["texto"] if forte else "",
                "evidencias": self.evidencias, "conflitos": self.conflitos}


def _sites_oficiais(dossie: Dict) -> List[Dict]:
    return [s for s in dossie["sites"] if s.get("ok") and s.get("oficial")]


def ranquear(dossie: Dict, verificar_wa: bool = True,
             confirmados: Optional[List[Dict]] = None) -> Dict[str, List[Dict]]:
    """Transforma o dossiê em candidatos pontuados por canal."""
    a = dossie["ancora"]
    nome_tokens = _tokens(a["nome"])

    # ── WhatsApp / telefone ──────────────────────────────────────────────────
    tels: Dict[str, _Cand] = {}

    def _tel_cand(bruto: str) -> Optional[_Cand]:
        n = whatsapp_check.normalizar(bruto)
        if not n:
            return None
        chave = n["e164"]
        if chave not in tels:
            c = _Cand("whatsapp", chave, n["formatado"])
            if n["is_mobile"]:
                c.sinal("celular_valido")
            # DDD contra a UF da clínica
            uf_tel = DDD_UF.get(n["ddd"], "")
            if a["uf"] and uf_tel:
                if uf_tel == a["uf"]:
                    c.sinal("ddd_bate", f"DDD {n['ddd']} = {uf_tel}")
                else:
                    c.conflito("ddd_de_outro_estado",
                               f"DDD {n['ddd']} ({uf_tel}) contra {a['uf']} da clínica")
            tels[chave] = c
        return tels[chave]

    if a["telefone"]:
        c = _tel_cand(a["telefone"])
        if c:
            c.sinal("e_o_telefone_do_maps")
    if dossie.get("wa_declarado_no_maps"):
        c = _tel_cand(dossie["wa_declarado_no_maps"])
        if c:
            c.sinal("wa_declarado_no_maps")
    for s in dossie["sites"]:
        if not s.get("ok"):
            continue
        oficial = s.get("oficial")
        for t in s.get("whatsapps", []) or []:
            c = _tel_cand(t)
            if c:
                if oficial:
                    c.sinal("wa_link_no_site", s["dominio"])
                else:
                    c.conflito("site_nao_confirmado", s["dominio"])
        for t in s.get("telefones_confiaveis", []) or []:
            c = _tel_cand(t)
            if c:
                if oficial:
                    c.sinal("publicado_no_site", s["dominio"])
                else:
                    c.conflito("site_nao_confirmado", s["dominio"])

    # Confirmação ao vivo no WhatsApp (bot da porta 5021, se conectado).
    if verificar_wa:
        for c in tels.values():
            r = whatsapp_check.verificar_whatsapp(c.valor)
            if r.get("tipo") == "verificado":
                if r.get("tem"):
                    c.sinal("wa_registrado")
                else:
                    c.conflito("wa_nao_registrado")
            elif r.get("tipo") == "improvavel":
                c.conflito("fixo_nao_verificado",
                           "conecte o bot em :5021 para confirmar")

    # ── E-mail ───────────────────────────────────────────────────────────────
    emails: Dict[str, _Cand] = {}
    for s in dossie["sites"]:
        if not s.get("ok"):
            continue
        oficial = s.get("oficial")
        for e in s.get("emails", []) or []:
            e = e.lower().strip()
            if e not in emails:
                emails[e] = _Cand("email", e)
            c = emails[e]
            dom_email = e.split("@")[-1]
            if oficial:
                c.sinal("publicado_no_site", s["dominio"])
                if dom_email == s["dominio"] or dom_email.endswith("." + s["dominio"]):
                    c.sinal("email_do_dominio", dom_email)
                elif dom_email in _EMAIL_PUBLICO:
                    # gmail publicado no site oficial ainda vale: o site é a prova
                    pass
                else:
                    c.conflito("dominio_estranho", dom_email)
            else:
                c.conflito("site_nao_confirmado", s["dominio"])
            if dom_email in _EMAIL_PUBLICO and not oficial:
                c.conflito("email_publico_sem_prova", dom_email)
            if nome_tokens & _tokens(e.split("@")[0] + " " + dom_email.split(".")[0]):
                c.sinal("nome_bate")

    # ── Instagram ────────────────────────────────────────────────────────────
    igs: Dict[str, _Cand] = {}

    def _ig_cand(user: str) -> Optional[_Cand]:
        u = (user or "").strip().lstrip("@").split("/")[0].split("?")[0]
        if not _handle_valido(u):
            return None
        if u not in igs:
            c = _Cand("instagram", u, "@" + u)
            # handles colam as palavras ("draingrid.pa" para "Dra Ingrid Passos"),
            # então comparar por token exato erra quase sempre: procura a palavra
            # do nome DENTRO do handle já normalizado.
            alvo = _norm(u)
            if any(len(t) >= 4 and t in alvo for t in nome_tokens):
                c.sinal("nome_bate", u)
            else:
                c.conflito("nome_nao_bate", u)
            igs[u] = c
        return igs[u]

    if dossie.get("ig_declarado_no_maps"):
        c = _ig_cand(dossie["ig_declarado_no_maps"])
        if c:
            c.sinal("ig_declarado_no_maps", "campo 'site' do Google")
    if a["instagram_cadastrado"]:
        c = _ig_cand(a["instagram_cadastrado"].split("instagram.com/")[-1].strip("/@ "))
        if c:
            c.sinal("ig_do_maps")
    for s in dossie["sites"]:
        if not s.get("ok"):
            continue
        for u in s.get("instagram", []) or []:
            c = _ig_cand(u)
            if not c:
                continue
            if s.get("oficial"):
                c.sinal("ig_link_no_site", s["dominio"])
            else:
                c.conflito("site_nao_confirmado", s["dominio"])

    # perfil público confirma pelo telefone exibido na bio
    for u, perfil in (dossie.get("instagram") or {}).items():
        c = igs.get(u)
        if not c or not perfil:
            continue
        for t in perfil.get("telefones", []) or []:
            if a["telefone"] and _mesmo_telefone(t, a["telefone"]):
                c.sinal("ig_tem_tel_da_clinica", t)
        texto = f"{perfil.get('titulo','')} {perfil.get('descricao','')}"
        if a["cidade"] and _norm(a["cidade"]) and _norm(a["cidade"]) in _norm(texto):
            c.sinal("cidade_bate", a["cidade"])

    # sinal fraco: apareceu na busca pelo nome
    for r in dossie.get("busca", []):
        dom = r.get("dominio", "")
        for c in list(emails.values()) + list(igs.values()):
            if c.canal == "email" and c.valor.split("@")[-1] == dom:
                c.sinal("aparece_na_busca", dom)
        if "instagram.com" in dom:
            for u, c in igs.items():
                if u.lower() in (r.get("titulo") or "").lower():
                    c.sinal("aparece_na_busca", r.get("titulo", ""))

    # ── Confirmações manuais ─────────────────────────────────────────────────
    baldes = {"whatsapp": tels, "email": emails, "instagram": igs}
    for m in (confirmados or []):
        canal, valor = m.get("canal"), (m.get("valor") or "").strip()
        if canal not in baldes or not valor:
            continue
        criar = {"whatsapp": _tel_cand, "email": None, "instagram": _ig_cand}[canal]
        if canal == "email":
            chave = valor.lower()
            c = emails.get(chave) or _Cand("email", chave)
            emails[chave] = c
        else:
            c = criar(valor)
        if not c:
            continue
        c.conflitos.clear()          # a conferência humana encerra a dúvida
        c.sinal("confirmado_manualmente", m.get("motivo") or "")

    def _ordenar(d: Dict[str, _Cand]) -> List[Dict]:
        # Empate em confiança é comum (dois telefones do mesmo site oficial).
        # Desempata por prova de posse e depois por quantidade de evidências,
        # para o escolhido ser o mais sustentado, não o primeiro encontrado.
        return sorted((c.dict() for c in d.values()),
                      key=lambda x: (x["confianca"], bool(x["prova_forte"]),
                                     len(x["evidencias"])), reverse=True)

    return {"whatsapp": _ordenar(tels), "email": _ordenar(emails),
            "instagram": _ordenar(igs)}


# ══════════════════════════════════════════════════════════════════════════════
#  Etapa 4: veredito, persistência e trava
# ══════════════════════════════════════════════════════════════════════════════

def init():
    with db.conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS identidade (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id      INTEGER REFERENCES leads(id) ON DELETE CASCADE,
                canal        TEXT NOT NULL,     -- whatsapp | email | instagram
                valor        TEXT NOT NULL,
                confianca    INTEGER DEFAULT 0,
                veredito     TEXT DEFAULT 'incerto',
                escolhido    INTEGER DEFAULT 0, -- 1 = é o contato do canal
                evidencias   TEXT DEFAULT '[]',
                conflitos    TEXT DEFAULT '[]',
                atualizado_em TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(lead_id, canal, valor)
            );
            CREATE INDEX IF NOT EXISTS idx_ident_lead ON identidade(lead_id);

            CREATE TABLE IF NOT EXISTS identidade_laudo (
                lead_id      INTEGER PRIMARY KEY REFERENCES leads(id) ON DELETE CASCADE,
                laudo        TEXT DEFAULT '{}',
                atualizado_em TEXT DEFAULT (datetime('now','localtime'))
            );

            -- Confirmações feitas por você, à mão. Ficam FORA da tabela de
            -- candidatos porque precisam sobreviver a um recruzamento: refazer
            -- o cruzamento apaga os candidatos, e seria péssimo perder aqui um
            -- contato que você conferiu por telefone.
            CREATE TABLE IF NOT EXISTS identidade_manual (
                lead_id    INTEGER REFERENCES leads(id) ON DELETE CASCADE,
                canal      TEXT NOT NULL,
                valor      TEXT NOT NULL,
                motivo     TEXT DEFAULT '',
                criado_em  TEXT DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (lead_id, canal, valor)
            );
        """)


def confirmar_manual(lead_id: int, canal: str, valor: str, motivo: str = "") -> Dict:
    """Marca um contato como conferido por você. Passa a valer como prova de posse."""
    if canal not in CANAIS:
        return {"ok": False, "erro": f"canal desconhecido: {canal}"}
    if not (valor or "").strip():
        return {"ok": False, "erro": "valor vazio"}
    init()
    with db.conn() as con:
        con.execute("""
            INSERT INTO identidade_manual (lead_id, canal, valor, motivo)
            VALUES (?,?,?,?)
            ON CONFLICT(lead_id, canal, valor) DO UPDATE SET
                motivo=excluded.motivo, criado_em=datetime('now','localtime')
        """, (lead_id, canal, valor.strip(), motivo))
    return resolver(lead_id)          # refaz o laudo já contando a confirmação


def remover_manual(lead_id: int, canal: str, valor: str) -> Dict:
    init()
    with db.conn() as con:
        con.execute("DELETE FROM identidade_manual WHERE lead_id=? AND canal=? AND valor=?",
                    (lead_id, canal, valor.strip()))
    return resolver(lead_id)


def manuais(lead_id: int) -> List[Dict]:
    init()
    with db.conn() as con:
        rows = con.execute(
            "SELECT canal, valor, motivo, criado_em FROM identidade_manual WHERE lead_id=?",
            (lead_id,)).fetchall()
    return [dict(r) for r in rows]


def _salvar(lead_id: int, ranking: Dict, laudo: Dict):
    init()
    with db.conn() as con:
        con.execute("DELETE FROM identidade WHERE lead_id=?", (lead_id,))
        for canal, lista in ranking.items():
            escolhido = (laudo.get("escolhido") or {}).get(canal) or {}
            for c in lista:
                con.execute("""
                    INSERT INTO identidade
                        (lead_id, canal, valor, confianca, veredito, escolhido,
                         evidencias, conflitos)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (lead_id, canal, c["valor"], c["confianca"], c["veredito"],
                      1 if c["valor"] == escolhido.get("valor") else 0,
                      json.dumps(c["evidencias"], ensure_ascii=False),
                      json.dumps(c["conflitos"], ensure_ascii=False)))
        con.execute("""
            INSERT INTO identidade_laudo (lead_id, laudo) VALUES (?,?)
            ON CONFLICT(lead_id) DO UPDATE SET
                laudo=excluded.laudo, atualizado_em=datetime('now','localtime')
        """, (lead_id, json.dumps(laudo, ensure_ascii=False)))


def carregar(lead_id: int, max_idade_dias: Optional[int] = None) -> Optional[Dict]:
    """Devolve o último laudo salvo, sem rodar nada de novo."""
    init()
    with db.conn() as con:
        r = con.execute("SELECT * FROM identidade_laudo WHERE lead_id=?",
                        (lead_id,)).fetchone()
    if not r:
        return None
    laudo = json.loads(r["laudo"] or "{}")
    laudo["atualizado_em"] = r["atualizado_em"]
    if max_idade_dias is not None and _idade_dias(r["atualizado_em"]) > max_idade_dias:
        return None
    return laudo


def _idade_dias(carimbo: str) -> float:
    """Idade do laudo em dias. Carimbo ilegível conta como infinitamente velho."""
    from datetime import datetime
    try:
        quando = datetime.strptime((carimbo or "").strip()[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return float("inf")
    return (datetime.now() - quando).total_seconds() / 86400


def garantir(lead_id: int, forcar: bool = False, **kw) -> Dict:
    """O laudo do lead, cruzando na hora se não existir ou se estiver vencido."""
    if not forcar:
        laudo = carregar(lead_id, max_idade_dias=getattr(
            config, "IDENTIDADE_VALIDADE_DIAS", 30))
        if laudo:
            return laudo
    return resolver(lead_id, **kw)


def _resumo(laudo: Dict) -> str:
    linhas = []
    for canal in CANAIS:
        esc = (laudo.get("escolhido") or {}).get(canal)
        if not esc:
            linhas.append(f"{canal}: nenhum candidato aproveitável")
            continue
        selo = {"confirmado": "confirmado", "provavel": "provável",
                "incerto": "incerto", "conflito": "em conflito",
                "descartado": "descartado"}.get(esc["veredito"], esc["veredito"])
        trava = "liberado" if laudo["liberado"].get(canal) else "bloqueado"
        linhas.append(f"{canal}: {esc['rotulo']} ({esc['confianca']}/100, {selo}) {trava}")
    return " · ".join(linhas)


def resolver(lead_id: int, usar_busca: bool = True, verificar_wa: bool = True,
             salvar: bool = True, max_sites: Optional[int] = None) -> Dict:
    """Roda o cruzamento completo de um lead e devolve o laudo."""
    lead = db.get_lead(lead_id)
    if not lead:
        return {"ok": False, "erro": "lead não encontrado"}

    dossie = coletar(lead, usar_busca=usar_busca,
                     max_sites=max_sites or getattr(config, "IDENTIDADE_MAX_SITES", 4))
    ranking = ranquear(dossie, verificar_wa=verificar_wa,
                       confirmados=manuais(lead_id))

    escolhido, liberado = {}, {}
    for canal in CANAIS:
        lista = ranking.get(canal) or []
        bons = [c for c in lista if c["veredito"] != "descartado"]
        melhor = bons[0] if bons else None
        escolhido[canal] = melhor
        liberado[canal] = _libera(canal, melhor)

    laudo = {
        "ok": True,
        "lead_id": lead_id,
        "clinica": lead.get("nome"),
        "ancora": dossie["ancora"],
        "sites_analisados": [
            {"dominio": s["dominio"], "origem": s["origem"], "ok": s.get("ok"),
             "oficial": s.get("oficial", False),
             "tem_telefone_da_ancora": s.get("tem_telefone_da_ancora", False),
             "tem_endereco_da_ancora": s.get("tem_endereco_da_ancora", False),
             # nome da clínica no domínio, mas sem prova: é o caso que merece
             # olhada humana, porque pode ser um site antigo ou uma homônima
             "suspeito": bool(s.get("nome_no_dominio") and not s.get("oficial")),
             "erro": s.get("erro", "")}
            for s in dossie["sites"]
        ],
        "instagram_lidos": {u: {"bloqueado": p.get("bloqueado", False),
                                "erro": p.get("erro", "")}
                            for u, p in (dossie.get("instagram") or {}).items()},
        "candidatos": ranking,
        "escolhido": escolhido,
        "liberado": liberado,
    }
    laudo["resumo"] = _resumo(laudo)

    if salvar:
        _salvar(lead_id, ranking, laudo)
        try:
            import dashboard_db as ddb
            ddb.add_nota(lead_id, "Cruzamento de identidade: " + laudo["resumo"])
        except Exception:
            pass
    return laudo


def _libera(canal: str, cand: Optional[Dict]) -> bool:
    """A trava de envio."""
    if not cand:
        return False
    if cand["veredito"] == "conflito":
        return False
    if canal == "whatsapp":
        return cand["veredito"] in ("confirmado", "provavel")
    return cand["veredito"] == "confirmado" and cand["tem_prova_de_posse"]


def pode_enviar(lead_id: int, canal: str, revalidar: bool = False) -> Dict:
    """Trava usada pelos senders. Devolve {ok, valor, motivo, confianca, veredito}."""
    if canal not in CANAIS:
        return {"ok": False, "motivo": f"canal desconhecido: {canal}"}
    laudo = garantir(lead_id, forcar=revalidar)
    if not laudo.get("ok"):
        return {"ok": False, "motivo": laudo.get("erro", "sem laudo de identidade")}

    cand = (laudo.get("escolhido") or {}).get(canal)
    if not cand:
        return {"ok": False, "motivo": f"nenhum {canal} encontrado para esta clínica"}
    if not laudo["liberado"].get(canal):
        exigencia = ("prova de posse e veredito confirmado"
                     if canal != "whatsapp" else "veredito provável ou melhor")
        return {"ok": False, "valor": cand["valor"], "confianca": cand["confianca"],
                "veredito": cand["veredito"],
                "motivo": f"{canal} bloqueado: {cand['confianca']}/100, "
                          f"veredito '{cand['veredito']}' (exige {exigencia})"}
    return {"ok": True, "valor": cand["valor"], "rotulo": cand["rotulo"],
            "confianca": cand["confianca"], "veredito": cand["veredito"],
            "motivo": "liberado pelo cruzamento de identidade"}


# ══════════════════════════════════════════════════════════════════════════════
#  Laudo legível
# ══════════════════════════════════════════════════════════════════════════════

def imprimir(laudo: Dict):
    if not laudo.get("ok"):
        print("erro:", laudo.get("erro"))
        return
    a = laudo["ancora"]
    print("=" * 74)
    print(f"  CRUZAMENTO DE IDENTIDADE — {laudo['clinica']}  (lead {laudo['lead_id']})")
    print("=" * 74)
    print(f"Âncora (Google Maps): {a['telefone'] or 'sem telefone'} · "
          f"{a['cidade'] or 'cidade não identificada'}{'/' + a['uf'] if a['uf'] else ''}")
    print(f"Site declarado: {a['site_declarado'] or '(nenhum)'}")

    print("\nSITES ANALISADOS")
    if not laudo["sites_analisados"]:
        print("  (nenhum candidato a site)")
    for s in laudo["sites_analisados"]:
        if not s["ok"]:
            print(f"  ✗ {s['dominio']:<34} [{s['origem']}] {s['erro']}")
            continue
        selo = "OFICIAL" if s["oficial"] else "não confirmado"
        prova = ""
        if s["tem_telefone_da_ancora"]:
            prova = " (publica o telefone do Maps)"
        elif s.get("tem_endereco_da_ancora"):
            prova = " (publica o endereço do Maps)"
        elif s.get("suspeito"):
            prova = "  ← tem o nome da clínica no domínio, confira à mão"
        print(f"  • {s['dominio']:<34} [{s['origem']}] {selo}{prova}")

    for canal in CANAIS:
        print(f"\n{canal.upper()}")
        lista = laudo["candidatos"].get(canal) or []
        if not lista:
            print("  (nada encontrado)")
            continue
        for c in lista:
            marca = "→" if (laudo["escolhido"].get(canal) or {}).get("valor") == c["valor"] else " "
            print(f" {marca} {c['rotulo']:<32} {c['confianca']:>3}/100  {c['veredito']}")
            for e in c["evidencias"]:
                sufixo = f" [{e['detalhe']}]" if e["detalhe"] else ""
                print(f"      + {e['peso']:>3}  {e['texto']}{sufixo}")
            for x in c["conflitos"]:
                sufixo = f" [{x['detalhe']}]" if x["detalhe"] else ""
                print(f"      {x['peso']:>4}  {x['texto']}{sufixo}")

    print("\nLIBERAÇÃO DE ENVIO")
    for canal in CANAIS:
        estado = "LIBERADO" if laudo["liberado"].get(canal) else "BLOQUEADO"
        alvo = (laudo["escolhido"].get(canal) or {}).get("rotulo", "sem candidato")
        print(f"  {canal:<10} {estado:<10} {alvo}")
    print()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    como_json = "--json" in args
    sem_busca = "--sem-busca" in args
    args = [a for a in args if not a.startswith("--")]

    recruzar = "--recruzar" in sys.argv

    if "--todos" in sys.argv:
        # Auditoria da base. NÃO é passo obrigatório: o cruzamento roda sozinho
        # dentro da esteira e da ponte, lead a lead. Aqui só reaproveita laudo
        # válido, então rodar de novo é barato. Use --recruzar para refazer tudo.
        db.init_db()
        for lead in db.all_leads():
            laudo = garantir(lead["id"], forcar=recruzar, usar_busca=not sem_busca)
            print(f"[{lead['id']:>4}] {lead['nome'][:38]:<38} "
                  f"{laudo.get('resumo', laudo.get('erro'))}")
    elif args:
        laudo = garantir(int(args[0]), forcar=recruzar, usar_busca=not sem_busca)
        if como_json:
            print(json.dumps(laudo, ensure_ascii=False, indent=2))
        else:
            imprimir(laudo)
    else:
        print(__doc__)
