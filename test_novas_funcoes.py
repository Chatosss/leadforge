"""
Teste das novas funções: Maps link, apuração de site, WhatsApp, PageSpeed, scrap ao
vivo.
"""
import sys
import requests

for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass

BASE = "http://localhost:5020"
OK, FAIL = "✅", "❌"
res = []
def check(nome, cond, extra=""):
    res.append(cond); print(f"{OK if cond else FAIL} {nome}" + (f"  ({extra})" if extra else "")); return cond

print("="*64); print("TESTE DAS NOVAS FUNÇÕES"); print("="*64)

# 1. Link do Maps presente nos leads
leads = requests.get(f"{BASE}/api/leads").json()
com_maps = [l for l in leads if l.get("maps_url")]
check("Link do Google Maps disponível nos leads", len(com_maps) >= 1,
      f"{len(com_maps)}/{len(leads)} com maps_url")
print(f"   → ex: {com_maps[0]['maps_url']}")

# 2. Cria lead com SITE REAL e apura contatos
lid = requests.post(f"{BASE}/api/leads", json={
    "nome": "Teste Apuração", "categoria": "Dentista",
    "website": "https://www.sorridents.com.br"}).json()["id"]
print(f"   → lead de teste id {lid} (site real)")
ap = requests.post(f"{BASE}/api/leads/{lid}/apurar", json={}).json()
achou = ap.get("ok") and (ap["achados"].get("emails") or ap["achados"].get("instagram"))
check("Apuração pelo site extrai contatos reais", bool(achou),
      f"email={ap.get('achados',{}).get('emails')} ig={ap.get('achados',{}).get('instagram')}")

# 3. WhatsApp — confirma num celular e reprova num fixo
wa_cel = requests.post(f"{BASE}/api/leads/{lid}/whatsapp", json={"numero": "(51) 99527-7648"}).json()
wa_fix = requests.post(f"{BASE}/api/leads/{lid}/whatsapp", json={"numero": "(21) 3555-2121"}).json()
check("WhatsApp: celular=provável, fixo=improvável",
      wa_cel.get("tem") is True and wa_fix.get("tem") is False,
      f"cel={wa_cel.get('tipo')} fixo={wa_fix.get('tipo')} link={wa_cel.get('link')}")

# 4. PageSpeed (pode dar 429 sem chave — falha graciosa conta como OK de robustez)
ps = requests.post(f"{BASE}/api/leads/{lid}/pagespeed", json={}, timeout=90).json()
check("PageSpeed responde (nota OU falha graciosa)", "ok" in ps,
      f"ok={ps.get('ok')} score={ps.get('score')} {('' if ps.get('ok') else ps.get('erro',''))[:40]}")

# 5. Encanamento do scrap ao vivo (progress POST -> live GET)
requests.post(f"{BASE}/api/scrap/progress", json={
    "sid": 99999, "screenshot": "data:image/png;base64,AAAA",
    "businesses": [{"nome": "Clínica X", "maps_url": "http://maps/x", "avaliacao": "4,5", "total_avaliacoes": "80"}],
    "count": 1, "done": False, "msg": "teste"})
live = requests.get(f"{BASE}/api/scrap/live?sid=99999").json()
check("Scrap ao vivo: progresso transmitido e recuperado",
      live.get("count") == 1 and len(live.get("businesses", [])) == 1 and not live.get("done"),
      f"msg={live.get('msg')}")

# limpeza do lead de teste
requests.delete(f"{BASE}/api/leads/{lid}")

print("\n"+"="*64)
passou = sum(1 for c in res if c)
print(f"RESULTADO: {passou}/{len(res)} passaram")
print("🎉 Novas funções OK." if passou == len(res) else "⚠ Ver falhas acima.")
