"""operar.py — Painel único de operação da prospecção."""

import argparse
import sys

import config
import db
import modo

LEAD_TESTE = 633   # 'teste-rota', apontado para os contatos de teste do dono


def _linha(c="─", n=72):
    return c * n


# ── Diagnóstico ───────────────────────────────────────────────────────────────

def estado() -> dict:
    from senders import whatsapp_sender, instagram_sender, email_sender
    e = {"modo": modo.atual()}
    try:
        e["whatsapp"] = whatsapp_sender.status()
    except Exception as ex:
        e["whatsapp"] = {"pronto": False, "erro": str(ex)}
    try:
        e["instagram"] = instagram_sender.status()
    except Exception as ex:
        e["instagram"] = {"no_ar": False, "erro": str(ex)}
    try:
        e["email"] = email_sender.status()
    except Exception as ex:
        e["email"] = {"pronto": False, "erro": str(ex)}
    try:
        import ia_hook
        e["ia"] = ia_hook.pronta()
        e["ia_detalhe"] = ia_hook.credito()["detalhe"]
    except Exception:
        e["ia"] = False
        e["ia_detalhe"] = "falhou ao checar"
    with db.conn() as con:
        e["leads_reais"] = con.execute(
            "SELECT COUNT(*) FROM leads WHERE origem='scrap-live'").fetchone()[0]
        e["enviados_hoje"] = con.execute(
            "SELECT COUNT(*) FROM touches WHERE status='sent' "
            "AND date(sent_at)=date('now','localtime')").fetchone()[0]
    return e


def imprimir_estado():
    e = estado()
    print(_linha("═"))
    print("  PAINEL DE OPERAÇÃO")
    print(_linha("═"))
    print()
    print(modo.resumo())
    print()
    wa, ig, em = e["whatsapp"], e["instagram"], e["email"]
    def selo(ok):
        return "PRONTO" if ok else "FORA"
    print("canais:")
    print(f"  whatsapp   {selo(wa.get('pronto')):<7} "
          f"{wa.get('numero') or wa.get('erro','')}")
    print(f"  instagram  {selo(ig.get('no_ar')):<7} {ig.get('erro','')}")
    print(f"  e-mail     {selo(em.get('pronto')):<7} "
          f"{len(em.get('contas') or [])} conta(s), "
          f"{em.get('capacidade_hoje', 0)} de capacidade hoje")
    detalhe = e.get("ia_detalhe", "")
    print(f"  IA (copy)  {selo(e['ia']):<7} "
          f"{detalhe if e['ia'] else (detalhe + ': a copy sai no texto fixo')}")
    print()
    print(f"leads reais na base: {e['leads_reais']}")
    print(f"toques enviados hoje: {e['enviados_hoje']}")
    print(_linha())


# ── Ações ─────────────────────────────────────────────────────────────────────

def rodar_teste(etapa: str = "", canais=("whatsapp",)):
    """Dispara para o lead de teste, que aponta para os contatos do operador."""
    if modo.ao_vivo():
        print("O modo está AO VIVO. Volte para teste antes:")
        print("   python modo.py --teste")
        return 1
    import bridge
    etapas = {c: etapa for c in canais} if etapa else None
    print(f"teste: lead {LEAD_TESTE}, canais {', '.join(canais)}"
          f"{', etapa ' + etapa if etapa else ''}\n")
    bridge.run(enviar=True, lead_ids=[LEAD_TESTE], canais=tuple(canais), etapas=etapas)
    return 0


def rodar_ao_vivo(ids, etapa: str = "", canais=("whatsapp",)):
    if not ids:
        print("--leads é obrigatório. Escolher lead por score é decisão sua.")
        return 1
    if not modo.ao_vivo():
        print("O modo está em TESTE, nada sairia para clínica real.")
        print("Para liberar:  python modo.py --ao-vivo")
        return 1
    import bridge
    nomes = [(db.get_lead(i) or {}).get("nome", f"id {i}") for i in ids]
    print("ENVIO REAL para:")
    for i, n in zip(ids, nomes):
        print(f"   {i}  {n}")
    if input("\nDigite ENVIAR para confirmar: ").strip().upper() != "ENVIAR":
        print("cancelado.")
        return 1
    etapas = {c: etapa for c in canais} if etapa else None
    bridge.run(enviar=True, lead_ids=ids, canais=tuple(canais), etapas=etapas)
    return 0


def gerar_pdf(lead_id: int):
    lead = db.get_lead(lead_id)
    if not lead:
        print(f"lead {lead_id} não encontrado")
        return 1
    import proposta_designs
    print(f"gerando material de {lead['nome']}...")
    caminho = proposta_designs.gerar_final(lead_id)
    if not caminho:
        print("falhou")
        return 1
    try:
        import dashboard_db as ddb
        ddb.add_anexo(lead_id, caminho.name, str(caminho))
    except Exception:
        pass
    print(f"pronto: {caminho}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Painel de operação da prospecção")
    ap.add_argument("--checar", action="store_true", help="só o diagnóstico")
    ap.add_argument("--teste", action="store_true", help="envia aos contatos de teste")
    ap.add_argument("--ao-vivo", dest="ao_vivo", action="store_true",
                    help="envia de verdade (exige --leads e modo ao vivo)")
    ap.add_argument("--leads", default="", help="ids separados por vírgula")
    ap.add_argument("--etapa", default="", help="dia0_ping, dia0, dia2, dia4...")
    ap.add_argument("--canais", default="whatsapp", help="whatsapp,instagram,email")
    ap.add_argument("--pdf", type=int, default=0, help="gera o material do lead")
    a = ap.parse_args()

    canais = tuple(c.strip() for c in a.canais.split(",") if c.strip())
    ids = [int(i) for i in a.leads.split(",") if i.strip().isdigit()]

    if a.pdf:
        return gerar_pdf(a.pdf)
    if a.teste:
        imprimir_estado()
        return rodar_teste(a.etapa, canais)
    if a.ao_vivo:
        imprimir_estado()
        return rodar_ao_vivo(ids, a.etapa, canais)
    imprimir_estado()
    if not a.checar:
        print()
        print("nada foi enviado. Para agir:")
        print("   python operar.py --teste --etapa dia0_ping")
        print("   python operar.py --ao-vivo --leads 582 --etapa dia0_ping")
        print("   python operar.py --pdf 582")
    return 0


if __name__ == "__main__":
    sys.exit(main())
