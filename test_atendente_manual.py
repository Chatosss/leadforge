"""test_atendente_manual.py — O bot manda template e nada mais."""
import sys

import config
import conversa_ia
import db
import esteira_wa

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Não existe lead com este número. O teste confere isso antes de escrever.
FAKE = "5511900000001"

LEAD_TESTE = 633        # 'teste-rota': nunca aponta para clínica real

# mensagens que ANTES tinham resposta automática, uma por ramificação de
# `responder()`. Todas têm que sair caladas agora.
CASOS = [
    ("oi",                          "saudação"),
    ("é sim, pode mandar",          "confirmação do ping (era a abordagem)"),
    ("vc é robô?",                  "pergunta se é automatizado"),
    ("quero falar com uma pessoa",  "pediu humano"),
    ("me manda o preço",            "pergunta comercial (era o atendente)"),
]


def _limpar():
    with db.conn() as con:
        con.execute("DELETE FROM conversas WHERE numero=?", (FAKE,))
        con.execute("DELETE FROM conversa_estado WHERE numero=?", (FAKE,))
        con.execute("DELETE FROM mensagens WHERE externo_id=?", (FAKE,))


def main():
    conversa_ia.init()
    falhas = 0

    if conversa_ia.lead_por_numero(FAKE):
        print(f"  ABORTADO: {FAKE} casou com um lead real. Escolha outro número.")
        return 1

    try:
        # 1) a chave
        ok = config.ATENDENTE_AUTOMATICO is False
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} config.ATENDENTE_AUTOMATICO = "
              f"{config.ATENDENTE_AUTOMATICO} (esperado False)")

        # 2) e 3) nada sai, tudo fica registrado
        for msg, rotulo in CASOS:
            _limpar()
            r = conversa_ia.responder(FAKE, msg)
            calado = not r.get("responder") and not r.get("texto")
            registrado = any(m["autor"] == "lead" and m["texto"] == msg
                             for m in conversa_ia.historico(FAKE, 10))
            sem_resposta_gravada = not any(
                m["autor"] == "ia" for m in conversa_ia.historico(FAKE, 10))
            bom = calado and registrado and sem_resposta_gravada
            falhas += 0 if bom else 1
            detalhe = []
            if not calado:
                detalhe.append(f"responderia: {(r.get('texto') or '')[:40]!r}")
            if not registrado:
                detalhe.append("mensagem do lead NÃO foi registrada")
            if not sem_resposta_gravada:
                detalhe.append("gravou resposta da IA")
            print(f"  {'ok  ' if bom else 'FALHOU'} {rotulo:38} "
                  f"{'calado e registrado' if bom else '; '.join(detalhe)}")

        # 4) opt-out continua valendo, sem resposta
        _limpar()
        r = conversa_ia.responder(FAKE, "não quero mais receber, pare")
        st = conversa_ia.estado(FAKE)
        bom = (not r.get("responder")) and st.get("pausado") == 1 \
            and st.get("estagio") == "encerrado"
        falhas += 0 if bom else 1
        print(f"  {'ok  ' if bom else 'FALHOU'} opt-out: pausado="
              f"{st.get('pausado')} estagio={st.get('estagio')} "
              f"responder={r.get('responder')} (esperado 1/encerrado/False)")

        # 5) quem respondeu não recebe mais toque
        respondeu = esteira_wa.respondeu(LEAD_TESTE)
        print(f"  {'ok  ' if respondeu else 'FALHOU'} lead {LEAD_TESTE} já "
              f"respondeu -> esteira.respondeu = {respondeu} (esperado True)")
        falhas += 0 if respondeu else 1

        # e a trava não pode barrar quem NUNCA respondeu, senão para a esteira
        with db.conn() as con:
            virgem = con.execute(
                "SELECT id FROM leads WHERE id NOT IN "
                "(SELECT DISTINCT COALESCE(lead_id,-1) FROM conversas) "
                "AND telefone <> '' LIMIT 1").fetchone()
        if virgem:
            lid = virgem["id"]
            bloqueado = esteira_wa.respondeu(lid)
            falhas += 0 if not bloqueado else 1
            print(f"  {'ok  ' if not bloqueado else 'FALHOU'} lead {lid} nunca "
                  f"respondeu -> esteira.respondeu = {bloqueado} (esperado False)")
    finally:
        _limpar()

    print()
    total = 1 + len(CASOS) + 1 + 2
    print(f"RESULTADO: {total - falhas}/{total} passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
