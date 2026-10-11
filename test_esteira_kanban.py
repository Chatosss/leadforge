"""Testa (sem servidor, não-destrutivo) reordenação, ações em massa e a esteira."""
import sys, db, dashboard_db as ddb, esteira
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass

OK, FAIL = "✅", "❌"
res = []
def ck(n, c, x=""): res.append(c); print(f"{OK if c else FAIL} {n}"+(f"  ({x})" if x else "")); return c

leads = sorted([l for l in db.all_leads() if l.get("score") is not None],
               key=lambda l: l["score"], reverse=True)
print(f"Base: {len(leads)} leads reais (não serão excluídos).")
ids = [l["id"] for l in leads[:3]]

# 1. Mover em massa
ddb.bulk_mover(ids, "conversa")
depois = {l["id"]: l for l in db.all_leads()}
ck("bulk_mover: 3 leads -> 'conversa'", all(depois[i]["kanban"] == "conversa" for i in ids))

# 2. Reordenar (ordem invertida)
ddb.reordenar("conversa", list(reversed(ids)))
depois = {l["id"]: l for l in db.all_leads()}
ordens = [(i, depois[i]["kanban_ordem"]) for i in reversed(ids)]
ck("reordenar: grava kanban_ordem 0,1,2 na nova ordem",
   [o for _, o in ordens] == [0, 1, 2], str(ordens))

# 3. Voltar para 'lista' (não-destrutivo)
ddb.bulk_mover(ids, "lista")
depois = {l["id"]: l for l in db.all_leads()}
ck("bulk_mover de volta para 'lista'", all(depois[i]["kanban"] == "lista" for i in ids))

# 4. Esteira completa no lead de maior score
alvo = leads[0]
print(f"\nRodando esteira em: {alvo['nome']} (id {alvo['id']}, score {alvo['score']})...")
r = esteira.rodar_esteira(alvo["id"], com_pagespeed=False)
ck("esteira: executou os passos", r.get("ok") and len(r.get("passos", [])) >= 2, " → ".join(r.get("passos", [])))
d = ddb.lead_detalhe(alvo["id"])
ck("esteira: gerou diagnóstico", bool(d.get("diagnostico")))
ck("esteira: gerou proposta", bool(d.get("proposta")))
ck("esteira: moveu para 'material'", d.get("kanban") == "material", f"kanban={d.get('kanban')}")
ck("esteira: anexou material", len(d.get("anexos", [])) >= 1,
   f"anexos={[a['filename'] for a in d.get('anexos', [])]}")
print(f"   referência da cidade usada: {r.get('referencia')}")
print(f"   ROI: R$ {r.get('roi')} · plano: {r.get('nivel')} · PDF: {r.get('pdf')}")

print("\n" + "=" * 60)
p = sum(1 for c in res if c)
print(f"RESULTADO: {p}/{len(res)} passaram")
print("🎉 Backend das novas funções OK." if p == len(res) else "⚠ ver falhas.")
