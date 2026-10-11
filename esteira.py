"""esteira.py — Esteira COMPLETA de um lead, ponta a ponta."""

import subprocess
import sys
from pathlib import Path

import config
import db
import scorer
import diagnostico as diag_mod
import proposta as prop_mod
import web_enrich
import pagespeed
import documento_pdf
import dashboard_db as ddb

ROOT = Path(__file__).resolve().parent


def rodar_esteira(lead_id: int, com_pagespeed: bool = False, com_pdf: bool = True,
                  recruzar: bool = False) -> dict:
    lead = db.get_lead(lead_id)
    if not lead:
        return {"ok": False, "erro": "lead não encontrado"}

    passos = []

    # 1. QUALIFICAR
    if lead.get("score") is None:
        s, b = scorer.score_lead(lead)
        db.set_score(lead_id, s, b)
        passos.append("qualificado")

    # 2. APURAR (site -> contatos + WhatsApp)
    if lead.get("website"):
        try:
            ap = web_enrich.apurar_lead(lead_id)
            if ap.get("ok"):
                passos.append("site apurado")
        except Exception:
            pass

    # 3. IDENTIDADE (cruzamento) — depois de apurar, para o laudo já ver os
    # contatos que a apuração trouxe. Roda por lead, sempre que a esteira roda.
    identidade_resumo = None
    liberado = {}
    try:
        import identidade
        laudo = identidade.garantir(lead_id, forcar=recruzar)
        if laudo.get("ok"):
            identidade_resumo = laudo.get("resumo")
            liberado = laudo.get("liberado") or {}
            canais_ok = [c for c, v in liberado.items() if v]
            passos.append("identidade: " + (", ".join(canais_ok) if canais_ok
                                            else "nenhum canal liberado"))
    except Exception as e:
        passos.append(f"identidade falhou: {e}")

    # 4. PageSpeed (opcional; lento/limitado)
    if com_pagespeed and lead.get("website"):
        try:
            r = pagespeed.pagespeed_lead(lead_id)
            if r.get("ok"):
                passos.append(f"pagespeed {r['score']}")
        except Exception:
            pass

    lead = db.get_lead(lead_id)  # recarrega dados apurados

    # 4. REFERÊNCIAS na cidade + 5. DIAGNÓSTICO
    concorrente = diag_mod.montar_concorrente(lead)
    d = diag_mod.gerar_diagnostico(lead)
    passos.append("diagnóstico")

    # 6. PROPOSTA
    p = prop_mod.gerar_proposta(lead)
    passos.append("proposta")

    # 7. MATERIAL (PDF)
    pdf_ok = False
    if com_pdf:
        try:
            r = subprocess.run([sys.executable, "proposta_designs.py", "--final", str(lead_id)],
                               cwd=str(ROOT), capture_output=True, text=True, timeout=120)
            pdf = config.OUT_DIR / f"proposta_{documento_pdf._slug(lead['nome'])}.pdf"
            if r.returncode == 0 and pdf.exists():
                ddb.add_anexo(lead_id, pdf.name, str(pdf))
                pdf_ok = True
                passos.append("material PDF")
        except Exception:
            pass

    # 8. move para "Material pronto"
    ddb.set_kanban_opcional(lead_id, "material")

    return {
        "ok": True,
        "lead_id": lead_id,
        "nome": lead["nome"],
        "passos": passos,
        "referencia": concorrente["nome"] if concorrente else None,
        "roi": d.get("roi"),
        "nivel": p.get("nivel_nome"),
        "pdf": pdf_ok,
        "identidade": identidade_resumo,
        "canais_liberados": liberado,
    }


def rodar_lote(ids: list, com_pagespeed: bool = False, recruzar: bool = False) -> dict:
    resultados = [rodar_esteira(i, com_pagespeed=com_pagespeed, com_pdf=True,
                                recruzar=recruzar) for i in ids]
    ok = sum(1 for r in resultados if r.get("ok"))
    return {"ok": True, "total": len(ids), "concluidos": ok, "resultados": resultados}


if __name__ == "__main__":
    import json
    if len(sys.argv) > 1:
        print(json.dumps(rodar_esteira(int(sys.argv[1]), com_pagespeed="--ps" in sys.argv,
                                       recruzar="--recruzar" in sys.argv),
                         ensure_ascii=False, indent=2))
    else:
        print("uso: python esteira.py <lead_id> [--ps] [--recruzar]")
