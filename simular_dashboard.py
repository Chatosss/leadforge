"""simular_dashboard.py — Simulação ponta a ponta do dashboard via HTTP."""

import io
import sys
import time

import requests

for _s in (sys.stdout, sys.stderr):  # console UTF-8 no Windows
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE = "http://localhost:5020"
OK, FAIL = "✅", "❌"
resultados = []


def check(nome, cond, extra=""):
    resultados.append((cond, nome))
    print(f"{OK if cond else FAIL} {nome}" + (f"  ({extra})" if extra else ""))
    return cond


def main():
    print("=" * 64)
    print("SIMULAÇÃO COMPLETA DO DASHBOARD")
    print("=" * 64)

    # 1. Bootstrap
    b = requests.get(f"{BASE}/api/bootstrap").json()
    check("Bootstrap (colunas kanban + ferramentas)",
          len(b["kanban_cols"]) == 9 and "email" in b["ferramentas"],
          f"{len(b['kanban_cols'])} colunas")

    # 2. Leads carregam
    leads = requests.get(f"{BASE}/api/leads").json()
    check("Listar leads", len(leads) >= 26, f"{len(leads)} leads")
    top = leads[0]
    lid = top["id"]
    print(f"   → lead de teste: {top['nome']} (id {lid})")

    # 3. Mover pelo funil (Kanban)
    fluxo = ["material", "contato_iniciado", "conversa", "reuniao", "fechado"]
    ok_mov = True
    for col in fluxo:
        r = requests.post(f"{BASE}/api/leads/{lid}/kanban", json={"kanban": col}).json()
        ok_mov = ok_mov and r.get("ok")
    det = requests.get(f"{BASE}/api/leads/{lid}").json()
    check("Mover lead por 5 estágios do Kanban", ok_mov and det["kanban"] == "fechado",
          f"terminou em {det['kanban']}")

    # 4. Nota
    requests.post(f"{BASE}/api/leads/{lid}/notes", json={"texto": "Ligar terça 14h."})
    det = requests.get(f"{BASE}/api/leads/{lid}").json()
    check("Adicionar nota", len(det.get("notas", [])) >= 1)

    # 5. Editar contatos + CNPJ
    requests.patch(f"{BASE}/api/leads/{lid}", json={"email": "contato@teste.com", "cnpj": "33000167000101"})
    det = requests.get(f"{BASE}/api/leads/{lid}").json()
    check("Editar contatos (e-mail/CNPJ)", det.get("email") == "contato@teste.com")

    # 6. Inserção manual
    r = requests.post(f"{BASE}/api/leads", json={
        "nome": "Clínica Teste Manual", "categoria": "Dentista",
        "telefone": "(21) 90000-0000", "instagram": "@testemanual"}).json()
    check("Inserir lead manual", r.get("ok") and r.get("id"), f"id {r.get('id')}")
    manual_id = r.get("id")

    # 7. Templates
    r = requests.post(f"{BASE}/api/templates", json={
        "canal": "instagram", "nome": "Teste sim", "corpo": "Oi {nome}!"}).json()
    tpls = requests.get(f"{BASE}/api/templates?canal=instagram").json()
    check("Criar + listar template", r.get("ok") and len(tpls) >= 1, f"{len(tpls)} templates IG")

    # 8. Scrap
    r = requests.post(f"{BASE}/api/scrap", json={
        "termo": "clínica de estética", "localizacao": "Ipanema, Rio de Janeiro"}).json()
    fila = requests.get(f"{BASE}/api/scrap").json()
    check("Adicionar busca ao scrap", r.get("ok") and len(fila) >= 1, f"{len(fila)} na fila")

    # 9. Prospecção — preview (instagram)
    ig_leads = [l["id"] for l in leads if l.get("tem_instagram")][:3]
    r = requests.post(f"{BASE}/api/prospect", json={
        "canal": "instagram", "lead_ids": ig_leads, "enviar": False}).json()
    check("Prospecção — preview Instagram", r.get("ok") and len(r.get("previews", [])) == len(ig_leads),
          f"{len(r.get('previews', []))} mensagens geradas")
    if r.get("previews"):
        print(f"   → ex: {r['previews'][0]['corpo'][:70]}...")

    # 10. Gerar material (PDF) — roda subprocess Playwright
    print("   ⏳ gerando material (diagnóstico+proposta+PDF)...")
    r = requests.post(f"{BASE}/api/leads/{manual_id}/material", json={}, timeout=180).json()
    det = requests.get(f"{BASE}/api/leads/{manual_id}").json()
    check("Gerar material (PDF anexado + move p/ 'material')",
          r.get("ok") and det["kanban"] == "material" and len(det.get("anexos", [])) >= 1,
          f"pdf={r.get('pdf')}")

    # 11. Enriquecimento governo (precisa internet; falha graciosa é OK)
    r = requests.post(f"{BASE}/api/leads/{lid}/enrich", json={"cnpj": "33000167000101"}).json()
    check("Enriquecimento governo (ok OU falha graciosa sem quebrar)",
          "ok" in r, f"ok={r.get('ok')} fonte={r.get('fonte', r.get('erro',''))[:40]}")

    # 12. Export CSV / JSON
    csv_r = requests.get(f"{BASE}/api/export?fmt=csv")
    json_r = requests.get(f"{BASE}/api/export?fmt=json")
    check("Exportar CSV + JSON", csv_r.ok and "nome" in csv_r.text and json_r.ok,
          f"csv {len(csv_r.text)} bytes")

    # 13. Import CSV
    csv_data = "nome,categoria,telefone,email\nClínica Importada,Estética,(21) 5555-0000,x@y.com\n"
    files = {"file": ("import.csv", io.StringIO(csv_data), "text/csv")}
    r = requests.post(f"{BASE}/api/import", files=files).json()
    check("Importar CSV", r.get("ok") and r.get("importados") >= 1, f"{r.get('importados')} importados")

    # 14. Stats refletem mudanças
    s = requests.get(f"{BASE}/api/stats").json()
    check("Stats atualizados (fechados >=1, material >=1)",
          s["por_coluna"].get("fechado", 0) >= 1 and s["por_coluna"].get("material", 0) >= 1,
          f"total={s['total']} fechados={s['por_coluna'].get('fechado')}")

    # ── Resumo ──
    print("\n" + "=" * 64)
    passou = sum(1 for c, _ in resultados if c)
    total = len(resultados)
    print(f"RESULTADO: {passou}/{total} verificações passaram")
    if passou == total:
        print("🎉 TUDO OK — processo completo funcionando ponta a ponta.")
    else:
        print("⚠ Falhas:")
        for c, n in resultados:
            if not c:
                print(f"   {FAIL} {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
