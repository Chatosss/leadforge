"""run_pilot.py — Piloto ponta a ponta (dry-run) do LeadForge."""

import argparse

import config
import db
import seed_amostra
import scorer
import diagnostico as diag_mod
import proposta as prop_mod
import bridge
import crm


def _linha(c=" ", n=74):
    return c * n


def tabela_resumo(top_n: int):
    leads = db.leads_by_score(limit=top_n)
    print("\n" + _linha("═"))
    print("TABELA-RESUMO DO PILOTO (ordenado por oportunidade)")
    print(_linha("═"))
    print(f"{'#':>2}  {'score':>5}  {'nicho':<12} {'ROI/mês':>10}  {'nível':<11} "
          f"{'canais':<10} clínica")
    print(_linha("─"))
    for i, l in enumerate(leads, 1):
        diag = db.get_diagnostico(l["id"]) or {}
        prop = db.get_proposta(l["id"]) or {}
        roi = diag.get("roi_reais", 0)
        nivel = prop.get("nivel_nome", "-")
        canais = []
        if bridge.ig_username(l):
            canais.append("IG")
        if l.get("telefone"):
            canais.append("WA")
        em, _ = bridge.email_lead(l)
        if em:
            canais.append("mail")
        nome = l["nome"][:30]
        print(f"{i:>2}  {l['score']:>5}  {l['nicho']:<12} "
              f"R$ {diag_mod._br(roi):>7}  {nivel:<11} {'+'.join(canais):<10} {nome}")
    print(_linha("─"))


def main(top_n: int = 25):
    print(_linha("█"))
    print("  PILOTO — MÁQUINA DE PROSPECÇÃO PARA CLÍNICAS")
    print(_linha("█"))

    print("\n[1/6] LOCALIZAR — gerando amostra e carregando leads...")
    seed_amostra.seed()

    print("\n[2/6] QUALIFICAR — pontuando leads...")
    scorer.run()

    print(f"\n[3/6] DIAGNOSTICAR — gerando diagnósticos (top {top_n})...")
    diag_mod.run(top_n)

    print(f"\n[4/6] PROPOR — montando propostas (top {top_n})...")
    prop_mod.run(top_n)

    print(f"\n[5/6] PROSPECTAR — agendando cadência multicanal (DRY-RUN)...")
    resumo = bridge.run(enviar=False, top_n=top_n, verbose=False)
    print(f"      Cadência agendada. IG prontos: {resumo.get('ig')} | "
          f"E-mails prontos: {resumo.get('email')} | "
          f"WhatsApp: contrato pronto (Roadmap).")

    print("\n[6/6] ACOMPANHAR — funil:")
    crm.imprimir_funil()

    tabela_resumo(top_n)

    # Amostra de 1 diagnóstico completo + próximos toques
    top = db.leads_by_score(limit=1)
    if top:
        d = db.get_diagnostico(top[0]["id"])
        print(f"\n📄 Exemplo de diagnóstico — {top[0]['nome']}:")
        print(_linha("─"))
        print(d["texto"])
        print(_linha("─"))

    print(f"\n✅ Piloto concluído. CSV de leads: {config.CSV_SAIDA}")
    print("   Nada foi enviado (dry-run). Para disparar: python bridge.py --enviar")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()
    main(top_n=args.top)
