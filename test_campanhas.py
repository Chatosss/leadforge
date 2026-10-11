"""
test_campanhas.py — Campanha é a sequência inteira, e o lead não troca de campanha.
"""
import sys

import config
import dashboard_db as ddb
import db
import esteira_wa as ew

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

LEAD = 633
ABERTURA_A = "Abertura da campanha A para {nome_curto}"
REFORCO_A = "Reforço 1 da campanha A"
ABERTURA_B = "Abertura da campanha B para {nome_curto}"


def pares_de_nomes() -> dict:
    """Pares de palavras consecutivas do nome de cada lead -> nome do operador."""
    # O nome da NOSSA empresa não é vazamento: "Presença Digital" abre todas as
    # campanhas de propósito.
    nossos = set()
    for campo in ("EMPRESA_NOME", "ATENDENTE_NOME", "EMPRESA_SITE"):
        palavras = [p for p in str(getattr(config, campo, "")).split() if len(p) >= 4]
        for a, b in zip(palavras, palavras[1:]):
            nossos.add(f"{a} {b}".lower())

    pares = {}
    for l in db.all_leads():
        palavras = [p for p in (l.get("nome") or "").split() if len(p) >= 4]
        for a, b in zip(palavras, palavras[1:]):
            par = f"{a} {b}".lower()
            if par in nossos:
                continue
            pares[par] = l.get("nome")
    return pares


def nomes_de_clinica_vazados() -> list:
    """
    Mensagens de campanha que trazem o nome de alguma clínica da base escrito no texto,
    em vez de `{nome_curto}`.
    """
    pares = pares_de_nomes()
    achados = []
    for c in ddb.listar_campanhas("whatsapp"):
        for m in c["mensagens"]:
            corpo = (m.get("corpo") or "").lower()
            for par, dono in pares.items():
                if par in corpo:
                    achados.append(f"{c['nome']}/{m.get('rotulo_passo')}: "
                                   f"'{par}' (de {dono})")
    return achados


def sent_do_lead(lead_id: int) -> int:
    with db.conn() as con:
        return con.execute("SELECT COUNT(*) FROM touches WHERE lead_id=? "
                           "AND status='sent'", (lead_id,)).fetchone()[0]


def main():
    falhas = 0
    original = (db.get_lead(LEAD) or {}).get("campanha_id")
    a_id = b_id = temp = None

    try:
        a_id = ddb.criar_campanha("ZZ teste A")
        b_id = ddb.criar_campanha("ZZ teste B")

        # 1) só entra no rodízio quem tem abertura escrita
        nomes = [c["nome"] for c in ddb.campanhas_para_rodizio()]
        ok = "ZZ teste A" not in nomes and "ZZ teste B" not in nomes
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} campanha ativa e SEM abertura fica "
              f"fora do rodízio")

        ddb.criar_template("whatsapp", "Abertura", "", ABERTURA_A,
                           campanha_id=a_id, ordem=0)
        ddb.criar_template("whatsapp", "Reforço 1", "", REFORCO_A,
                           campanha_id=a_id, ordem=1)
        ddb.criar_template("whatsapp", "Abertura", "", ABERTURA_B,
                           campanha_id=b_id, ordem=0)

        nomes = [c["nome"] for c in ddb.campanhas_para_rodizio()]
        ok = "ZZ teste A" in nomes and "ZZ teste B" in nomes
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} com abertura escrita, as duas "
              f"entram no rodízio")

        # 5) a etapa sai da posição
        m0 = ddb.template_da_campanha(a_id, 0)
        m1 = ddb.template_da_campanha(a_id, 1)
        ok = (m0["etapa"] == config.ESTEIRA_ETAPAS[0]
              and m1["etapa"] == config.ESTEIRA_ETAPAS[1])
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} etapa derivada da ordem: "
              f"0->{m0['etapa']}, 1->{m1['etapa']}")

        # 2) desativar tira do rodízio
        ddb.atualizar_campanha(b_id, {"ativa": False})
        nomes = [c["nome"] for c in ddb.campanhas_para_rodizio()]
        ok = "ZZ teste B" not in nomes and "ZZ teste A" in nomes
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} desativada sai do rodízio, a outra fica")

        # 3) o texto vem da campanha DO LEAD
        lead = dict(db.get_lead(LEAD))
        lead["campanha_id"] = a_id
        t0 = ew.montar_mensagem(lead, config.ESTEIRA_ETAPAS[0])
        t1 = ew.montar_mensagem(lead, config.ESTEIRA_ETAPAS[1])
        ok = "campanha A" in t0 and "campanha A" in t1
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} lead na A recebe textos da A: "
              f"{t0[:34]!r} / {t1[:28]!r}")

        # 4) posição sem mensagem devolve vazio, não pega de outra campanha
        t2 = ew.montar_mensagem(lead, config.ESTEIRA_ETAPAS[2])
        ok = t2 == ""
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} posição vazia da campanha -> "
              f"{t2[:40]!r} (esperado vazio)")

        # 2b) quem está numa campanha DESATIVADA continua recebendo os textos dela
        lead["campanha_id"] = b_id
        tb = ew.montar_mensagem(lead, config.ESTEIRA_ETAPAS[0])
        ok = "campanha B" in tb
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} campanha desativada ainda serve "
              f"quem já está dentro: {tb[:34]!r}")

        # 6) campanha com lead dentro não se exclui
        ddb.definir_campanha_do_lead(LEAD, a_id)
        r = ddb.excluir_campanha(a_id)
        ok = not r.get("ok") and "lead" in (r.get("erro") or "")
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} excluir campanha com lead dentro: "
              f"{r.get('erro') or 'DEIXOU EXCLUIR'}")

        # solto o lead e aí sim exclui
        ddb.definir_campanha_do_lead(LEAD, None)
        r = ddb.excluir_campanha(a_id)
        ok = r.get("ok")
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} sem lead dentro, exclui")
        if ok:
            a_id = None

        sobrou = ddb.template_da_campanha(m0["campanha_id"], 0) if a_id else None
        ok = sobrou is None
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} as mensagens vão junto com a campanha")

        # Os rascunhos das campanhas foram escritos usando a clínica de teste como
        # exemplo ("a análise da Sorriso Real").
        vazados = nomes_de_clinica_vazados()
        ok = not vazados
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} nenhuma campanha tem nome de "
              f"clínica escrito no texto"
              + ("" if ok else f" -> {vazados[:3]}"))

        # Em 17/08 cinco envios de um lead só saíram todos com a mesma campanha: o
        # rodízio era a posição do lead no lote (`campanhas[i % total]`), e num lote de
        # um o índice é sempre 0.
        falsas = [{"id": 1}, {"id": 2}, {"id": 3}]
        ultima, escolhidas = None, []
        for _ in range(7):
            c = ddb.proxima_campanha(falsas, ultima)
            ultima = c["id"]
            escolhidas.append(c["id"])
        ok = escolhidas == [1, 2, 3, 1, 2, 3, 1]
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} 7 envios de um lead cada -> "
              f"campanhas {escolhidas} (esperado a sequência em loop)")

        # campanha que saiu do rodízio não trava o giro
        c = ddb.proxima_campanha([{"id": 1}, {"id": 3}], 1)
        ok = c["id"] == 3
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} com a 2 fora do ar, depois da 1 "
              f"vem a {c['id']} (esperado 3)")
        c = ddb.proxima_campanha([{"id": 1}, {"id": 3}], 99)
        ok = c["id"] == 1
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} ponteiro apontando para campanha "
              f"que não existe mais -> volta para a {c['id']} (esperado 1)")

        # O relógio da esteira é a tabela `touches`, então adiantar um lead é registrar
        # que os toques anteriores estão vencidos.
        temp = ddb.inserir_manual({"nome": "ZZ Passo Teste",
                                   "telefone": "(82) 99661-9912"})
        novos_antes = ew.enviados_novos_hoje()

        ew.definir_passo(temp, 2)          # próxima = a 3ª mensagem
        st = ew.estado(temp)
        ok = st["passo"] == 2 and ew.ordem_da_etapa(st["proxima"]) == 2
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} passo movido à mão para 2 -> "
              f"passo={st['passo']}, próxima={st['proxima']}")

        ok = (sent_do_lead(temp) == 0
              and ew.enviados_novos_hoje() == novos_antes)
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} nada virou envio: "
              f"{sent_do_lead(temp)} toque(s) 'sent', teto do dia intacto")

        ew.definir_passo(temp, 0)          # voltar por cima do que foi manual
        ok = ew.estado(temp)["passo"] == 0
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} voltar por cima de passo manual: "
              f"passo={ew.estado(temp)['passo']}")

        # agora um toque REAL, e voltar por cima dele tem que ser recusado
        with db.conn() as con:
            con.execute("INSERT INTO touches(lead_id,canal,etapa,status,sent_at) "
                        "VALUES(?,?,?,'sent',datetime('now','localtime'))",
                        (temp, "whatsapp", config.ESTEIRA_ETAPAS[0]))
        try:
            ew.definir_passo(temp, 0)
            recusou, motivo = False, "DEIXOU VOLTAR"
        except ValueError as e:
            recusou, motivo = True, str(e)
        falhas += 0 if recusou else 1
        print(f"  {'ok  ' if recusou else 'FALHOU'} voltar por cima de toque "
              f"que saiu de verdade: {motivo}")

        # `template_da_etapa` não filtrava por campanha, e como as mensagens das
        # campanhas também guardam a etapa, o lead "sem campanha" continuava recebendo,
        # calado, os textos da campanha de menor id.
        solto = dict(db.get_lead(LEAD))
        solto["campanha_id"] = None
        sobrando = [e for e in config.ESTEIRA_ETAPAS
                    if ew.montar_mensagem(solto, e)]
        ok = not sobrando
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} lead sem campanha não tem texto "
              f"em nenhuma posição"
              + ("" if ok else f" -> ainda sai em {sobrando}"))

        ok = not ew.tem_texto(solto, config.ESTEIRA_ETAPAS[0])
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} tem_texto() avisa a tela para não "
              f"oferecer o botão de envio")

        # controle negativo: com o nome vazado de propósito, a trava TEM que acusar.
        alvo = next(iter(pares_de_nomes()))
        ddb.criar_template("whatsapp", "Reforço 1", "",
                           f"Como já fizemos a análise da {alvo}, achei que "
                           f"valia a pena compartilhar.",
                           campanha_id=b_id, ordem=1)
        pegou = nomes_de_clinica_vazados()
        ok = bool(pegou)
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} controle negativo: nome vazado de "
              f"propósito foi acusado" + (f" -> {pegou[0]}" if pegou else ""))
    finally:
        if temp:
            try:
                ddb.excluir_lead(temp)
            except Exception as e:
                print(f"  (não deu para remover o lead temporário {temp}: {e})")
        for cid in (a_id, b_id):
            if cid:
                with db.conn() as con:
                    con.execute("DELETE FROM msg_templates WHERE campanha_id=?", (cid,))
                    con.execute("DELETE FROM campanhas WHERE id=?", (cid,))
        with db.conn() as con:
            con.execute("UPDATE leads SET campanha_id=? WHERE id=?", (original, LEAD))

    print()
    total = 22
    print(f"RESULTADO: {total - falhas}/{total} passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
