"""ia_hook.py — Refino/geração de texto por IA (compatível com o padrão OpenAI)."""

import os

import requests

import config  # noqa: F401  (carrega o .env)

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek/deepseek-chat-v3-0324")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "40"))

IA_ATIVA = bool(LLM_API_KEY)

# Crédito é outra pergunta. `IA_ATIVA` só diz que existe chave, e com isso o
# painel do operar.py imprimia "IA (copy) PRONTO" durante os cinco dias em que a
# conta esteve em 402 e toda a copy saía no texto determinístico.
_CREDITO_CACHE = {"em": 0.0, "ok": None, "detalhe": ""}
_CREDITO_TTL = 300          # segundos; o saldo não muda de minuto a minuto


def credito(forcar: bool = False) -> dict:
    """{ok, detalhe}. Consulta o saldo no provedor, com cache curto."""
    import time
    if not IA_ATIVA:
        return {"ok": None, "detalhe": "sem chave"}
    agora = time.time()
    if not forcar and _CREDITO_CACHE["ok"] is not None and \
            agora - _CREDITO_CACHE["em"] < _CREDITO_TTL:
        return {"ok": _CREDITO_CACHE["ok"], "detalhe": _CREDITO_CACHE["detalhe"]}
    try:
        r = requests.get(f"{LLM_BASE_URL}/credits",
                         headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                         timeout=10)
        if r.status_code == 404:            # provedor sem esse endpoint
            return {"ok": None, "detalhe": "provedor não informa saldo"}
        r.raise_for_status()
        d = (r.json() or {}).get("data") or {}
        total = float(d.get("total_credits") or 0)
        usado = float(d.get("total_usage") or 0)
        saldo = total - usado
        ok = saldo > 0
        detalhe = f"saldo US$ {saldo:.2f}" if ok else "sem crédito (402)"
    except Exception as exc:
        return {"ok": None, "detalhe": f"não deu para checar: {exc}"}
    _CREDITO_CACHE.update({"em": agora, "ok": ok, "detalhe": detalhe})
    return {"ok": ok, "detalhe": detalhe}


def pronta() -> bool:
    """A IA responde agora? Chave presente E crédito que não esteja zerado."""
    return IA_ATIVA and credito()["ok"] is not False

_SYS_REFINO = (
    "Você reescreve textos de prospecção B2B para clínicas de saúde no Brasil. "
    "Tom consultivo, humano, direto, em português do Brasil — sem parecer spam nem "
    "exagero de vendas. MANTENHA todos os números, nomes e fatos do texto original "
    "(posição, R$, nota, avaliações). Não invente dados. Responda só com o texto final."
)


def _chamar_llm(messages, temperatura=0.7, max_tokens=900):
    """Chamada crua ao endpoint /chat/completions (padrão OpenAI/OpenRouter)."""
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
        # OpenRouter usa estes p/ ranking (opcionais):
        "HTTP-Referer": "https://clinica-prospector.local",
        "X-Title": "Clinica Prospector",
    }
    body = {"model": LLM_MODEL, "messages": messages,
            "temperature": temperatura, "max_tokens": max_tokens}
    r = requests.post(f"{LLM_BASE_URL}/chat/completions", headers=headers,
                      json=body, timeout=LLM_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


# ── API pública ───────────────────────────────────────────────────────────────

def refinar_com_ia(texto: str, contexto: dict | None = None,
                   instrucao: str | None = None) -> str:
    """
    Recebe um texto determinístico e devolve uma versão refinada pela IA. Sem chave, ou
    em qualquer erro, devolve o texto original (fail-safe).
    """
    if not IA_ATIVA or not texto:
        return texto
    try:
        user = (f"Instrução: {instrucao or 'Refine o tom, mantendo todos os fatos e números.'}\n"
                f"Contexto do lead (não invente além disto): {contexto or {}}\n\n"
                f"Texto base:\n{texto}")
        out = _chamar_llm([{"role": "system", "content": _SYS_REFINO},
                           {"role": "user", "content": user}])
        return out or texto
    except Exception as exc:
        print(f"[ia_hook] IA indisponível ({exc}); usando texto determinístico.")
        return texto


def gerar_conteudo(instrucao: str, contexto: dict | None = None,
                   sistema: str | None = None, max_tokens: int = 700) -> str:
    """Gera conteúdo do zero (post, e-mail, legenda, abordagem). Retorna "" em erro."""
    if not IA_ATIVA:
        return ""
    try:
        sistema = sistema or (
            "Você é copywriter de marketing para clínicas de saúde no Brasil. "
            "Escreve em português do Brasil, claro e persuasivo, sem clichês nem spam.")
        user = f"{instrucao}\n\nDados para personalizar: {contexto or {}}"
        return _chamar_llm([{"role": "system", "content": sistema},
                            {"role": "user", "content": user}], max_tokens=max_tokens)
    except Exception as exc:
        print(f"[ia_hook] IA indisponível ({exc}).")
        return ""


def status() -> dict:
    return {"ativa": IA_ATIVA, "base_url": LLM_BASE_URL, "modelo": LLM_MODEL,
            "chave": (LLM_API_KEY[:8] + "…") if LLM_API_KEY else "(sem chave)"}


if __name__ == "__main__":
    print("Config:", status())
    if IA_ATIVA:
        exemplo = ("Olá! Sua clínica aparece na 65ª posição no Google Maps. "
                   "Isso faz perder cerca de 12 pacientes por mês.")
        print("\n[Determinístico]:", exemplo)
        print("\n[Refinado pela IA]:", refinar_com_ia(
            exemplo, {"nome": "Clínica Corpus Face", "nicho": "odontologia"},
            "Reescreva como uma abordagem curta e simpática de WhatsApp."))
