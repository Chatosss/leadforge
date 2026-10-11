"""test_rodizio_primeiro_contato.py — A abertura não pode falar como follow-up."""
import sys
from pathlib import Path

import dashboard_db as ddb

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROTA = Path(__file__).resolve().parent / "dashboard" / "app.py"

# Frases que denunciam texto de follow-up numa mensagem de abertura.
FRASES_DE_FOLLOWUP = [
    "mensagem anterior",
    "última mensagem",
    "não vou insistir",
    "encerro por aqui",
    "reforçar",
]


def main():
    falhas = 0

    # 1) o rodízio da rota é de campanhas, não de templates soltos
    fonte = ROTA.read_text(encoding="utf-8")
    trecho = fonte.split("def api_entrar_contato_enviar")[1][:3000]
    por_campanha = "campanhas_para_rodizio()" in trecho
    sem_lista_crua = "ddb.listar_templates(\"whatsapp\")" not in trecho
    ok = por_campanha and sem_lista_crua
    falhas += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FALHOU'} a rota sorteia CAMPANHA "
          f"(campanhas_para_rodizio={por_campanha}, "
          f"sem listar_templates cru={sem_lista_crua})")

    # 2) a abertura de cada campanha do rodízio não fala como follow-up
    rodizio = ddb.campanhas_para_rodizio()
    print(f"  ---  no rodízio hoje: {len(rodizio)} campanha(s) "
          f"{[c['nome'] for c in rodizio]}")

    ok = len(rodizio) >= 1
    falhas += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FALHOU'} existe pelo menos 1 campanha em rodízio")

    for c in rodizio:
        abertura = next((m for m in c["mensagens"] if (m.get("ordem") or 0) == 0), None)
        corpo = ((abertura or {}).get("corpo") or "").lower()
        achadas = [f for f in FRASES_DE_FOLLOWUP if f in corpo]
        bom = bool(corpo) and not achadas
        falhas += 0 if bom else 1
        print(f"  {'ok  ' if bom else 'FALHOU'} abertura de {c['nome']!r}: "
              + ("não fala de mensagem anterior" if bom
                 else (f"fala como follow-up -> {achadas}" if corpo else "VAZIA")))

    # 3) controle negativo: os reforços REALMENTE têm essas frases. Se este
    #    passar a falhar, as frases mudaram e a busca acima virou enfeite
    reforcos = [m for c in ddb.listar_campanhas("whatsapp") for m in c["mensagens"]
                if (m.get("ordem") or 0) >= 1]
    com_frase = [m for m in reforcos
                 if any(f in (m.get("corpo") or "").lower() for f in FRASES_DE_FOLLOWUP)]
    ok = bool(com_frase)
    falhas += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FALHOU'} controle negativo: {len(com_frase)} de "
          f"{len(reforcos)} reforços falam de mensagem anterior "
          f"(prova que a busca encontra o problema)")

    print()
    total = 3 + len(rodizio)
    print(f"RESULTADO: {total - falhas}/{total} passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
