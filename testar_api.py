"""Testa todas as rotas API novas do dashboard."""
import json
import urllib.request

BASE = "http://localhost:5020"
ok = 0
fail = 0

def testar(nome, metodo, rota, body=None):
    global ok, fail
    url = BASE + rota
    try:
        dados = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=dados, method=metodo,
                                     headers={"Content-Type": "application/json"} if dados else {})
        r = urllib.request.urlopen(req, timeout=10)
        resp = json.loads(r.read().decode())
        # Verifica se tem erro
        if isinstance(resp, dict) and resp.get("erro"):
            print(f"  FAIL  {nome}: {resp['erro']}")
            fail += 1
        else:
            # Conta itens se for lista
            if isinstance(resp, list):
                print(f"  OK    {nome}: {len(resp)} itens")
            elif isinstance(resp, dict):
                keys = list(resp.keys())[:5]
                print(f"  OK    {nome}: {keys}")
            ok += 1
    except Exception as e:
        print(f"  FAIL  {nome}: {e}")
        fail += 1

print("=" * 60)
print("TESTE DAS ROTAS API")
print("=" * 60)

# Rotas existentes (devem continuar funcionando)
testar("GET /api/bootstrap", "GET", "/api/bootstrap")
testar("GET /api/leads", "GET", "/api/leads")
testar("GET /api/kanban", "GET", "/api/kanban")
testar("GET /api/templates", "GET", "/api/templates")
testar("GET /api/whatsapp/status", "GET", "/api/whatsapp/status")

# Rotas novas (Hube 02)
testar("GET /api/scheduler/status", "GET", "/api/scheduler/status")
testar("GET /api/follow-up/rules", "GET", "/api/follow-up/rules")
testar("GET /api/follow-up/pendentes", "GET", "/api/follow-up/pendentes")
testar("GET /api/accounts", "GET", "/api/accounts")
testar("GET /api/backup/listar", "GET", "/api/backup/listar")
testar("GET /api/enrichment/status", "GET", "/api/enrichment/status")
testar("GET /api/funnel", "GET", "/api/funnel")
testar("GET /api/weekly-report", "GET", "/api/weekly-report")
testar("GET /api/leads?limit=10", "GET", "/api/leads?limit=10")

# POSTs (criação)
testar("POST /api/follow-up/rules", "POST", "/api/follow-up/rules",
       {"canal": "whatsapp", "delay_days": 2})
testar("POST /api/accounts", "POST", "/api/accounts",
       {"channel": "whatsapp", "identifier": "5582999990000", "display_name": "Teste", "daily_limit": 30})

print()
print(f"RESULTADO: {ok} OK, {fail} FAIL")
