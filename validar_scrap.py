"""ATENÇÃO: ESTE SCRIPT APAGA A BASE DE PROSPECÇÃO INTEIRA."""
import sys, time, requests, db
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass

BASE = "http://localhost:5020"

if "--apagar-tudo" not in sys.argv:
    print(__doc__)
    print("Nada foi apagado. Rode com --apagar-tudo se é isso mesmo que você quer.")
    raise SystemExit(1)

with db.conn() as con:
    n = con.execute("SELECT COUNT(*) FROM leads WHERE origem='scrap-live'").fetchone()[0]
print(f"Isto vai APAGAR {n} leads reais e tudo que depende deles (CASCADE).")
if input("Digite APAGAR para confirmar: ").strip().upper() != "APAGAR":
    raise SystemExit("cancelado.")

# a trava do banco é deliberada, então o escape também precisa ser
with db.exclusao_liberada():
    with db.conn() as con:
        con.execute("DELETE FROM leads WHERE origem='scrap-live'")
        con.execute("DELETE FROM scrap_queue")

sid = requests.post(f"{BASE}/api/scrap", json={
    "termo": "clínica odontológica", "localizacao": "Copacabana Rio de Janeiro"}).json()["id"]
requests.post(f"{BASE}/api/scrap/{sid}/run", json={})
print(f"Scrap sid {sid} iniciado. Aguardando conclusão...", flush=True)

for i in range(40):
    time.sleep(3)
    p = requests.get(f"{BASE}/api/scrap/live?sid={sid}").json()
    print(f"  [{i*3:>3}s] {p.get('count',0)} coletadas | {p.get('msg','')[:55]}", flush=True)
    if p.get("done"):
        break

# valida no banco
leads = [l for l in db.all_leads() if l.get("origem") == "scrap-live"]
print("\n" + "=" * 70, flush=True)
print(f"VALIDAÇÃO — {len(leads)} clínicas gravadas", flush=True)
def pct(cond): return f"{sum(1 for l in leads if cond(l))}/{len(leads)}"
if leads:
    print(f"  com telefone:      {pct(lambda l: l.get('telefone'))}", flush=True)
    print(f"  com website:       {pct(lambda l: l.get('tem_site'))}", flush=True)
    print(f"  com nota:          {pct(lambda l: l.get('avaliacao_num') is not None)}", flush=True)
    print(f"  com nº avaliações: {pct(lambda l: (l.get('total_avaliacoes_num') or 0) > 0)}", flush=True)
    print(f"  com categoria:     {pct(lambda l: l.get('categoria'))}", flush=True)
    print(f"  com score:         {pct(lambda l: l.get('score') is not None)}", flush=True)
    print("\nAMOSTRA (5 primeiras):", flush=True)
    for l in leads[:5]:
        print(f"  • {l['nome'][:32]:<32} score={l.get('score')} nota={l.get('avaliacao_num')} "
              f"aval={l.get('total_avaliacoes_num')} tel={l.get('telefone') or '-'} "
              f"site={'sim' if l.get('tem_site') else '-'} cat={(l.get('categoria') or '-')[:22]}", flush=True)
else:
    print("  ⚠ Nenhuma clínica gravada (Maps pode ter bloqueado).", flush=True)
