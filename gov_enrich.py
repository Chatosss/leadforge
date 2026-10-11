"""gov_enrich.py — Enriquecimento de leads com dados públicos do governo (Receita)."""

import json
import re
import urllib.request
from typing import Dict, Optional

import config
import db

_TIMEOUT = 8
_UA = "clinica_prospector/1.0 (enriquecimento CNPJ)"


def _so_digitos(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _get_json(url: str) -> Optional[Dict]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Consulta por CNPJ ─────────────────────────────────────────────────────────

def consultar_cnpj(cnpj: str) -> Dict:
    """Consulta um CNPJ nas fontes públicas e normaliza o retorno."""
    cnpj = _so_digitos(cnpj)
    if len(cnpj) != 14:
        return {"ok": False, "erro": "CNPJ inválido (precisa de 14 dígitos)"}

    fontes = [
        ("BrasilAPI",   f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}", _norm_brasilapi),
        ("cnpj.ws",     f"https://publica.cnpj.ws/cnpj/{cnpj}",         _norm_cnpjws),
        ("ReceitaWS",   f"https://receitaws.com.br/v1/cnpj/{cnpj}",     _norm_receitaws),
    ]
    ultimo_erro = "sem resposta"
    for nome, url, norm in fontes:
        try:
            data = _get_json(url)
            if data:
                out = norm(data)
                out["fonte"] = nome
                out["ok"] = True
                out["cnpj"] = cnpj
                return out
        except Exception as e:
            ultimo_erro = f"{nome}: {e}"
            continue
    return {"ok": False, "erro": ultimo_erro}


def _norm_brasilapi(d: Dict) -> Dict:
    tel = d.get("ddd_telefone_1") or d.get("ddd_telefone_2") or ""
    end = ", ".join(x for x in [
        f"{d.get('descricao_tipo_de_logradouro','')} {d.get('logradouro','')}".strip(),
        str(d.get("numero", "")),
        d.get("bairro", ""), d.get("municipio", ""), d.get("uf", ""),
    ] if x)
    return {
        "razao_social": d.get("razao_social", ""),
        "nome_fantasia": d.get("nome_fantasia", ""),
        "situacao": d.get("descricao_situacao_cadastral", ""),
        "cnae": d.get("cnae_fiscal_descricao", ""),
        "telefone": tel,
        "email": (d.get("email") or "").lower(),
        "endereco": end,
    }


def _norm_cnpjws(d: Dict) -> Dict:
    est = d.get("estabelecimento", {}) or {}
    ddd = est.get("ddd1", "")
    fone = est.get("telefone1", "")
    tel = f"({ddd}) {fone}" if ddd and fone else fone
    cidade = (est.get("cidade") or {}).get("nome", "")
    end = ", ".join(x for x in [
        f"{est.get('tipo_logradouro','')} {est.get('logradouro','')}".strip(),
        str(est.get("numero", "")), est.get("bairro", ""), cidade, est.get("estado", {}).get("sigla", ""),
    ] if x)
    cnae = (est.get("atividade_principal") or {}).get("descricao", "")
    return {
        "razao_social": d.get("razao_social", ""),
        "nome_fantasia": est.get("nome_fantasia", ""),
        "situacao": est.get("situacao_cadastral", ""),
        "cnae": cnae,
        "telefone": tel,
        "email": (est.get("email") or "").lower(),
        "endereco": end,
    }


def _norm_receitaws(d: Dict) -> Dict:
    end = ", ".join(x for x in [
        d.get("logradouro", ""), str(d.get("numero", "")), d.get("bairro", ""),
        d.get("municipio", ""), d.get("uf", ""),
    ] if x)
    ativ = d.get("atividade_principal") or [{}]
    return {
        "razao_social": d.get("nome", ""),
        "nome_fantasia": d.get("fantasia", ""),
        "situacao": d.get("situacao", ""),
        "cnae": ativ[0].get("text", "") if ativ else "",
        "telefone": d.get("telefone", ""),
        "email": (d.get("email") or "").lower(),
        "endereco": end,
    }


# ── Busca por nome (best-effort, documentado) ─────────────────────────────────

def buscar_cnpj_por_nome(nome: str, uf: str = "") -> Dict:
    """
    Best-effort: NÃO há API oficial gratuita estável para nome->CNPJ. Retorna orientação
    em vez de fingir um resultado. Preencha o CNPJ manualmente (ou via base aberta da
    Receita) para então enriquecer com consultar_cnpj().
    """
    return {
        "ok": False,
        "erro": "Busca por nome não tem fonte oficial gratuita estável.",
        "orientacao": ("Informe o CNPJ da clínica (site/Instagram/Google costumam ter) "
                       "e use o enriquecimento por CNPJ. Alternativa: base aberta de CNPJ "
                       "da Receita Federal (dados abertos) para descoberta em massa."),
    }


# ── Enriquecer um lead do banco ───────────────────────────────────────────────

def enriquecer_lead(lead_id: int) -> Dict:
    """Consulta o CNPJ do lead e completa campos vazios (e-mail, telefone, razão)."""
    lead = db.get_lead(lead_id)
    if not lead:
        return {"ok": False, "erro": "lead não encontrado"}
    cnpj = lead.get("cnpj") if "cnpj" in lead else None
    if not cnpj:
        # tenta ler da tabela (coluna adicionada pelo dashboard_db)
        with db.conn() as con:
            r = con.execute("SELECT cnpj FROM leads WHERE id=?", (lead_id,)).fetchone()
        cnpj = r["cnpj"] if r and "cnpj" in r.keys() else ""
    if not cnpj:
        return {"ok": False, "erro": "lead sem CNPJ — informe o CNPJ antes de enriquecer"}

    res = consultar_cnpj(cnpj)
    if not res.get("ok"):
        return res

    # completa campos vazios do lead + guarda o JSON bruto do governo
    with db.conn() as con:
        cur = con.execute("SELECT telefone, email, razao_social FROM leads WHERE id=?",
                          (lead_id,)).fetchone()
        atual = dict(cur) if cur else {}
        novo_tel = atual.get("telefone") or res.get("telefone", "")
        novo_email = atual.get("email") or res.get("email", "")
        novo_razao = atual.get("razao_social") or res.get("razao_social", "")
        con.execute(
            "UPDATE leads SET telefone=?, email=?, razao_social=?, gov_json=?, "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (novo_tel, novo_email, novo_razao, json.dumps(res, ensure_ascii=False), lead_id))
    return res


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(json.dumps(consultar_cnpj(sys.argv[1]), ensure_ascii=False, indent=2))
    else:
        # CNPJ público de exemplo (Petrobras) — depende de internet
        print("Teste (precisa de internet):")
        print(json.dumps(consultar_cnpj("33000167000101"), ensure_ascii=False, indent=2))
