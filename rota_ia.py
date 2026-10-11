"""rota_ia.py — Roda a ESTEIRA COMPLETA com IA num lead e mostra o resultado."""

import sys
import time

import copy_ai
import conteudo
import db
import diagnostico as diag_mod
import ia_hook
import proposta as prop_mod

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

L = 78


def bloco(titulo, char="═"):
    print("\n" + char * L)
    print(f"  {titulo}")
    print(char * L)


def nota(pontos, achados=None):
    if pontos == 0:
        base = "🟢 0 (limpo)"
    elif pontos <= 4:
        base = f"🟢 {pontos} (ok)"
    elif pontos <= 10:
        base = f"🟡 {pontos} (aceitável)"
    else:
        base = f"🔴 {pontos} (genérico/inventado)"
    if achados:
        base += f" · {', '.join(achados[:4])}"
    return base


def avaliar(texto, ctx=None):
    """Nota combinada: cheiro de IA + alucinação."""
    p1, a1 = copy_ai.cheiro_de_ia(texto)
    p2, a2 = copy_ai.alucinacoes(texto, ctx)
    return p1 + p2, a1 + a2


def main(lead_id=None):
    if not ia_hook.IA_ATIVA:
        print("⚠ IA não configurada (.env)."); return
    lead = db.get_lead(lead_id) if lead_id else (db.leads_by_score(limit=1) or [None])[0]
    if not lead:
        print("⚠ Nenhum lead."); return

    print("═" * L)
    print(f"  ROTA COM IA — {lead['nome']}")
    print(f"  {lead.get('categoria','')} · score {lead.get('score')} · "
          f"posição {lead.get('posicao')}º · {ia_hook.LLM_MODEL}")
    print("═" * L)

    # ── 1. DIAGNÓSTICO: antes x depois ──
    problemas = diag_mod.detectar_problemas(lead)[:3]
    perdidos, roi, ticket = diag_mod.estimar_roi(lead)
    conc = diag_mod.montar_concorrente(lead)
    antes = diag_mod.montar_texto(lead, problemas, perdidos, roi, ticket, conc)

    bloco("1. DIAGNÓSTICO — ANTES (determinístico)")
    print(antes)
    p_antes, a_antes = avaliar(antes)
    print(f"\n  → cheiro de IA: {nota(p_antes)}")

    t0 = time.time()
    depois = copy_ai.refinar(
        antes,
        {"clinica": lead["nome"], "nicho": lead.get("nicho"),
         "posicao_google": lead.get("posicao"), "perda_mes_reais": roi,
         "pacientes_perdidos_mes": perdidos,
         "concorrente": conc["nome"] if conc else None},
        "Diagnóstico para o dono da clínica. Abra pelo dado mais forte. "
        "Mantenha os 3 problemas e os valores. Convite curto no fim.")
    bloco("1. DIAGNÓSTICO — DEPOIS (IA + regras de copy)")
    print(depois)
    p_dep, a_dep = avaliar(depois)
    print(f"\n  → cheiro de IA: {nota(p_dep)} · {time.time()-t0:.1f}s")

    # ── 2. PROPOSTA ──
    nivel = prop_mod.recomendar_nivel(lead)
    prop_antes = prop_mod.montar_texto(lead, roi, ticket, problemas, nivel)
    prop_dep = copy_ai.refinar(
        prop_antes,
        {"clinica": lead["nome"], "nivel_indicado": nivel, "perda_mes_reais": roi},
        "Proposta comercial. Mantenha os 3 níveis com preços exatos e a recomendação.")
    bloco("2. PROPOSTA — DEPOIS (IA)")
    print(prop_dep)
    p, pa = avaliar(prop_dep)
    print(f"\n  → cheiro de IA: {nota(p)}")

    # ── 3. PEÇAS DE CONTATO ──
    for tipo in ("whatsapp", "email", "dm", "followup", "post"):
        r = conteudo.gerar(lead["id"], tipo)
        q = r.get("qualidade", {})
        bloco(f"3. {tipo.upper()}", "─")
        print(r.get("texto") or r.get("erro"))
        print(f"\n  → cheiro de IA: {nota(q.get('cheiro_ia', 0))} "
              f"· tentativas: {q.get('tentativas')}")

    print("\n" + "═" * L)
    print("  ✅ Rota concluída. Custo aproximado: US$ 0,01 (todas as peças).")
    print("═" * L)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
