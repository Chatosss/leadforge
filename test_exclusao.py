"""test_exclusao.py — Apagar lead real é possível, mas nunca por acidente."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "dashboard"))

import dashboard_db as ddb   # noqa: E402
import db                    # noqa: E402
from app import CONFIRMA_APAGAR, app   # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

LEAD_TESTE = 633


def criar_descartavel(nome="ZZ Descartavel"):
    lid = ddb.inserir_manual({"nome": nome, "telefone": "(82) 90000-0000"})
    with db.conn() as con:
        con.execute("UPDATE leads SET origem='scrap-live' WHERE id=?", (lid,))
    return lid


def existe(lid):
    with db.conn() as con:
        return bool(con.execute("SELECT 1 FROM leads WHERE id=?", (lid,)).fetchone())


def main():
    falhas = 0
    cli = app.test_client()
    criados = []

    try:
        lid = criar_descartavel()
        criados.append(lid)

        # 1) sem forçar, recusa — e a resposta diz que dá para forçar
        r = cli.post("/api/leads/bulk", json={"ids": [lid], "acao": "excluir"})
        d = r.get_json()
        ok = r.status_code == 409 and d.get("pode_forcar") and existe(lid)
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} sem forçar: HTTP {r.status_code}, "
              f"pode_forcar={d.get('pode_forcar')}, lead ainda existe={existe(lid)}")

        ok = d.get("confirmacao") == CONFIRMA_APAGAR
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} a resposta diz qual é a palavra: "
              f"{d.get('confirmacao')!r}")

        # 2) forçar sem a palavra
        r = cli.post("/api/leads/bulk",
                     json={"ids": [lid], "acao": "excluir", "forcar": True})
        ok = r.status_code == 400 and existe(lid)
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} forçar sem confirmação: "
              f"HTTP {r.status_code}, lead ainda existe={existe(lid)}")

        # 3) palavra errada
        r = cli.post("/api/leads/bulk", json={"ids": [lid], "acao": "excluir",
                                              "forcar": True,
                                              "confirmacao": "apaga isso ai"})
        ok = r.status_code == 400 and existe(lid)
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} confirmação errada: "
              f"HTTP {r.status_code}, lead ainda existe={existe(lid)}")

        # 4) o lead de teste, nem com a palavra certa
        r = cli.post("/api/leads/bulk", json={"ids": [LEAD_TESTE], "acao": "excluir",
                                              "forcar": True,
                                              "confirmacao": CONFIRMA_APAGAR})
        d = r.get_json()
        ok = (r.status_code == 409 and d.get("pode_forcar") is False
              and existe(LEAD_TESTE))
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} lead de teste {LEAD_TESTE}: "
              f"HTTP {r.status_code}, pode_forcar={d.get('pode_forcar')}, "
              f"existe={existe(LEAD_TESTE)}")

        # 5) com tudo certo, some de verdade
        r = cli.post("/api/leads/bulk", json={"ids": [lid], "acao": "excluir",
                                              "forcar": True,
                                              "confirmacao": CONFIRMA_APAGAR})
        d = r.get_json()
        ok = r.status_code == 200 and d.get("definitivo") and not existe(lid)
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} com a palavra certa: "
              f"HTTP {r.status_code}, apagado={not existe(lid)}, n={d.get('n')}")
        if not existe(lid):
            criados.remove(lid)

        # a trava do banco não pode ficar aberta depois
        with db.conn() as con:
            liberado = con.execute(
                "SELECT liberado FROM trava_exclusao WHERE id=1").fetchone()[0]
        ok = liberado == 0
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} trava do banco voltou a fechar "
              f"(liberado={liberado})")

        # 6) a contagem é o que sumiu, não o tamanho da lista
        a, b = criar_descartavel("ZZ Descartavel A"), criar_descartavel("ZZ Descartavel B")
        criados += [a, b]
        inexistente = 99999999
        r = cli.post("/api/leads/bulk",
                     json={"ids": [a, b, inexistente], "acao": "excluir",
                           "forcar": True, "confirmacao": CONFIRMA_APAGAR})
        d = r.get_json()
        ok = d.get("n") == 2
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} 3 ids pedidos, 1 inexistente -> "
              f"n={d.get('n')} (esperado 2, o que sumiu de verdade)")
        for x in (a, b):
            if not existe(x):
                criados.remove(x)
    finally:
        for lid in criados:
            try:
                ddb.excluir_lead(lid, forcar=True)
            except Exception as e:
                print(f"  (não deu para limpar o lead {lid}: {e})")

    print()
    total = 8
    print(f"RESULTADO: {total - falhas}/{total} passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
