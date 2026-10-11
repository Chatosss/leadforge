"""test_respondeu.py — Quem responde vai para a coluna "Respondeu", e só de lá."""
import sys

import conversa_ia
import dashboard_db as ddb
import db

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

LEAD = 633   # 'teste-rota': nunca aponta para clínica real

# (coluna de origem, coluna esperada depois da resposta)
CASOS = [
    ("lista", "respondeu"),
    ("material", "respondeu"),
    ("entrar_contato", "respondeu"),
    ("contato_iniciado", "respondeu"),
    # daqui para frente o operador já assumiu: a resposta não move
    ("respondeu", "respondeu"),
    ("material_enviado", "material_enviado"),
    ("conversa", "conversa"),
    ("reuniao", "reuniao"),
    ("fechado", "fechado"),
    ("negado", "negado"),
    ("ignorado", "ignorado"),
]


def main():
    conversa_ia.init()
    original = (db.get_lead(LEAD) or {}).get("kanban") or "lista"
    falhas = 0
    try:
        for origem, esperado in CASOS:
            ddb.set_kanban(LEAD, origem)
            conversa_ia._marcar_respondeu(LEAD)
            agora = (db.get_lead(LEAD) or {}).get("kanban")
            ok = agora == esperado
            falhas += 0 if ok else 1
            print(f"  {'ok  ' if ok else 'FALHOU'} {origem:18} -> {agora:18} "
                  f"(esperado {esperado})")

        # lead inexistente não pode derrubar o atendente
        try:
            conversa_ia._marcar_respondeu(99999999)
            conversa_ia._marcar_respondeu(None)
            print("  ok   lead inexistente/None não levanta exceção")
        except Exception as e:
            falhas += 1
            print(f"  FALHOU lead inexistente levantou: {e}")
    finally:
        ddb.set_kanban(LEAD, original)

    total = len(CASOS) + 1
    print()
    print(f"RESULTADO: {total - falhas}/{total} passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
