"""test_envio_fila.py — Verificação lead a lead, e quem falha vai para "Problema"."""
import sys
import time

import dashboard_db as ddb
import db
import envio_fila

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


def criar_lead(nome):
    lid = ddb.inserir_manual({"nome": nome, "telefone": "(82) 90000-0000"})
    ddb.set_kanban(lid, "entrar_contato")
    return lid


def kanban(lid):
    return (db.get_lead(lid) or {}).get("kanban")


def notas(lid):
    return " | ".join(n["texto"] for n in ddb.notas_do_lead(lid))


def esperar_fim(limite=30):
    for _ in range(limite * 10):
        if not envio_fila.estado()["rodando"]:
            return True
        time.sleep(0.1)
    return False


def main():
    falhas = 0
    criados = []
    # intervalo mínimo: o teste não pode esperar 30s entre leads
    from senders import whatsapp_sender
    import esteira_wa as ew
    envio_original = whatsapp_sender.enviar_lote
    intervalo_original = envio_fila._intervalo
    envio_fila._intervalo = lambda: 0.05
    # O teste não pode depender da hora em que roda nem do quanto já saiu hoje:
    # teto estourado ou fora da janela param a rodada antes do que se quer medir.
    janela_original = ew.dentro_da_janela
    ew.dentro_da_janela = lambda agora=None: True

    try:
        a = criar_lead("ZZ Fila Bom 1"); criados.append(a)
        b = criar_lead("ZZ Fila Ruim");  criados.append(b)
        c = criar_lead("ZZ Fila Bom 2"); criados.append(c)

        # ── 1, 2 e 3) o do meio é recusado na verificação ────────────────────
        chamadas = []

        def espiao(itens, etapa="dia0"):
            lid = itens[0]["lead_id"]
            chamadas.append(lid)
            if lid == b:
                return {"ok": False, "enviados": 0,
                        "bloqueados": [{"lead_id": lid,
                                        "motivo": "nenhum whatsapp encontrado "
                                                  "para esta clínica"}]}
            return {"ok": True, "enviados": 1, "bloqueados": []}

        whatsapp_sender.enviar_lote = espiao
        envio_fila.disparar([a, b, c])
        esperar_fim()
        est = envio_fila.estado()

        ok = chamadas == [a, b, c]
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} um envio POR LEAD, na ordem: "
              f"{len(chamadas)} chamada(s) (esperado 3, uma por lead)")

        ok = kanban(b) == envio_fila.COLUNA_PROBLEMA
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} recusado foi para "
              f"'{kanban(b)}' (esperado '{envio_fila.COLUNA_PROBLEMA}')")

        ok = kanban(a) == envio_fila.COLUNA_FEITO and kanban(c) == envio_fila.COLUNA_FEITO
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} a fila SEGUIU: os outros dois "
              f"foram para '{kanban(a)}' e '{kanban(c)}'")

        ok = "nenhum whatsapp" in notas(b).lower()
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} o motivo ficou escrito na nota: "
              f"{notas(b)[:60]!r}")

        ok = est["enviados"] == 2 and est["problemas"] == 1
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} placar: {est['enviados']} "
              f"enviado(s), {est['problemas']} problema(s) (esperado 2 e 1)")

        # ── 4) bot fora não manda ninguém para "Problema" ────────────────────
        d = criar_lead("ZZ Fila Infra 1"); criados.append(d)
        e = criar_lead("ZZ Fila Infra 2"); criados.append(e)

        def espiao_bot_fora(itens, etapa="dia0"):
            return {"ok": False, "bloqueados": [],
                    "erro": "nada foi enviado: o WhatsApp ainda não está conectado"}

        whatsapp_sender.enviar_lote = espiao_bot_fora
        envio_fila.disparar([d, e])
        esperar_fim()
        est = envio_fila.estado()

        ok = kanban(d) == "entrar_contato" and kanban(e) == "entrar_contato"
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} bot fora: os dois ficaram em "
              f"'{kanban(d)}'/'{kanban(e)}' (esperado seguir em entrar_contato)")

        ok = est["problemas"] == 0 and "conectado" in est["motivo_parada"]
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} nenhum foi punido e a rodada "
              f"parou: problemas={est['problemas']}, motivo={est['motivo_parada'][:40]!r}")

        # ── 5) duas rodadas ao mesmo tempo ──────────────────────────────────
        def espiao_lento(itens, etapa="dia0"):
            time.sleep(0.4)
            return {"ok": True, "enviados": 1, "bloqueados": []}

        whatsapp_sender.enviar_lote = espiao_lento
        envio_fila.disparar([d, e])
        r = envio_fila.disparar([d])
        ok = not r.get("ok") and "andamento" in (r.get("erro") or "")
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} segunda rodada recusada: "
              f"{r.get('erro')!r}")
        esperar_fim()

        # ── 6) trava de repetição: a mesma mensagem não sai duas vezes ─────── Em 21/08
        # quatro agências receberam a MESMA abertura duas e três vezes, com 17 e 19
        # minutos de intervalo, porque o botão foi clicado de novo enquanto os leads
        # ainda estavam na coluna.
        f = criar_lead("ZZ Fila Repete"); criados.append(f)
        with db.conn() as con:
            con.execute("INSERT INTO touches(lead_id,canal,etapa,status,sent_at) "
                        "VALUES(?,?,?,'sent',datetime('now','localtime'))",
                        (f, "whatsapp", "dia0_ping"))

        motivo = whatsapp_sender.repeticao(f, "dia0_ping")
        ok = bool(motivo) and "já recebeu" in motivo
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} mesma etapa recusada: {str(motivo)[:60]!r}")

        # etapa legada e a nova são a MESMA mensagem: dia0_ping == wa1_abertura
        ok = bool(whatsapp_sender.repeticao(f, "wa1_abertura"))
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} etapa legada conta como a mesma "
              f"(dia0_ping == wa1_abertura)")

        # outra etapa, mas dentro das horas mínimas: também não sai
        motivo = whatsapp_sender.repeticao(f, "wa3_isca")
        ok = bool(motivo) and "mínimo" in motivo
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} outra etapa colada recusada: "
              f"{str(motivo)[:60]!r}")

        # o `enviar_lote` de verdade marca o bloqueio como REPETIÇÃO (o bot é
        # dado como pronto para o teste não depender da conexão: a trava de
        # repetição barra antes de qualquer POST)
        status_original = whatsapp_sender.status
        # `conta` entrou na assinatura quando o status passou a
        # distinguir "nenhuma conectada" de "varias conectadas"
        whatsapp_sender.status = lambda conta="": {"no_ar": True, "pronto": True,
                                          "numero": "0", "erro": ""}
        # e com uma conta com vaga: sem isto, num dia em que as contas reais já
        # bateram o limite, o lote volta antes da trava de repetição
        remetentes_original = whatsapp_sender._remetentes_disponiveis
        whatsapp_sender._remetentes_disponiveis = lambda conta="": [
            {"conta": "5500000000000", "restam": 10, "id_no_bot": "5500000000000"}]
        try:
            r = envio_original([{"lead_id": f, "mensagem": "oi",
                                 "etapa": "dia0_ping"}], etapa="dia0_ping")
        finally:
            whatsapp_sender.status = status_original
            whatsapp_sender._remetentes_disponiveis = remetentes_original
        bl = (r.get("bloqueados") or [{}])[0]
        ok = bl.get("repeticao") is True and not r.get("ok")
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} enviar_lote marca o bloqueio como "
              f"repetição: {bl.get('repeticao')} ({str(bl.get('motivo'))[:40]!r})")

        # e o worker tira esse lead da fila sem chamá-lo de problema
        chamadas.clear()

        def espiao_com_trava(itens, etapa="dia0"):
            lid = itens[0]["lead_id"]
            motivo = whatsapp_sender.repeticao(lid, itens[0].get("etapa") or etapa)
            if motivo:
                return {"ok": False, "bloqueados": [
                    {"lead_id": lid, "motivo": motivo, "repeticao": True}]}
            return {"ok": True, "enviados": 1, "bloqueados": []}

        whatsapp_sender.enviar_lote = espiao_com_trava
        envio_fila.disparar([f])
        esperar_fim()
        est = envio_fila.estado()
        ok = (kanban(f) == envio_fila.COLUNA_FEITO and est["problemas"] == 0
              and est.get("repetidos") == 1)
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} quem já recebeu vai para "
              f"'{kanban(f)}' como repetido, não como problema "
              f"(repetidos={est.get('repetidos')}, problemas={est['problemas']})")
    finally:
        whatsapp_sender.enviar_lote = envio_original
        envio_fila._intervalo = intervalo_original
        ew.dentro_da_janela = janela_original
        for lid in criados:
            try:
                ddb.excluir_lead(lid, forcar=True)
            except Exception as ex:
                print(f"  (não deu para limpar o lead {lid}: {ex})")

    print()
    total = 13
    print(f"RESULTADO: {total - falhas}/{total} passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
