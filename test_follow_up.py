"""
test_follow_up.py — O follow-up envia de verdade, pelo caminho com trava, e falha alto
quando não consegue.
"""
import sys
from datetime import datetime, timedelta

import dashboard_db as ddb
import db
import follow_up
import modo
from senders import email_sender, instagram_sender, whatsapp_sender

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

falhas = 0
CRIADOS = {"leads": [], "regras": [], "templates": []}


def checar(condicao, descricao, detalhe=""):
    global falhas
    if condicao:
        print(f"  OK   {descricao}")
    else:
        falhas += 1
        print(f"  FALHA {descricao}" + (f"\n        {detalhe}" if detalhe else ""))


# ── Fixtures descartáveis ────────────────────────────────────────────────────

def criar_lead(nome="ZZ FollowUp Teste"):
    lid = ddb.inserir_manual({"nome": nome, "telefone": "(82) 90000-0000"})
    with db.conn() as con:
        con.execute("UPDATE leads SET email=?, instagram=?, kanban=?, cidade=? "
                    "WHERE id=?",
                    (f"zz.followup.{lid}@exemplo.invalido", f"zz_followup_{lid}",
                     "contato_iniciado", "Maceió, AL", lid))
    CRIADOS["leads"].append(lid)
    return lid


def criar_template(canal, assunto, corpo):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO msg_templates (canal, nome, assunto, corpo) VALUES (?,?,?,?)",
            (canal, "ZZ Teste follow-up", assunto, corpo))
        tid = cur.lastrowid
    CRIADOS["templates"].append(tid)
    return tid


def criar_regra(canal, dias, template_id):
    rid = follow_up.criar_regra(canal, dias, template_id)
    CRIADOS["regras"].append(rid)
    return rid


def regra_crua(canal, dias, template_id):
    """Insere direto no banco, pulando a validação — para simular o legado."""
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO follow_up_rules (canal, delay_days, template_id) VALUES (?,?,?)",
            (canal, dias, template_id))
        rid = cur.lastrowid
    CRIADOS["regras"].append(rid)
    return rid


def resetar_fila():
    """Isola o cenário: cancela o que estiver vencido e desliga todas as regras."""
    with db.conn() as con:
        con.execute("UPDATE follow_up_queue SET status='cancelled' "
                    "WHERE status IN ('pending','sending')")
        con.execute("UPDATE follow_up_rules SET ativo=0")


def vencer(lead_id):
    """Puxa a fila do lead para o passado, para o item estar vencido agora."""
    passado = (datetime.now() - timedelta(minutes=1)).isoformat(sep=" ", timespec="seconds")
    with db.conn() as con:
        con.execute("UPDATE follow_up_queue SET scheduled_for=? "
                    "WHERE lead_id=? AND status='pending'", (passado, lead_id))


def item_da_fila(lead_id):
    """O item do cenário atual: o mais novo que não foi cancelado pelo reset."""
    with db.conn() as con:
        row = con.execute("SELECT * FROM follow_up_queue WHERE lead_id=? "
                          "AND status != 'cancelled' ORDER BY id DESC LIMIT 1",
                          (lead_id,)).fetchone()
        if row is None:
            row = con.execute("SELECT * FROM follow_up_queue WHERE lead_id=? "
                              "ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
    return dict(row) if row else None


def fila_do_lead(lead_id):
    with db.conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM follow_up_queue WHERE lead_id=?", (lead_id,))]


def limpar():
    with db.conn() as con:
        for rid in CRIADOS["regras"]:
            con.execute("DELETE FROM follow_up_queue WHERE rule_id=?", (rid,))
            con.execute("DELETE FROM follow_up_rules WHERE id=?", (rid,))
        for tid in CRIADOS["templates"]:
            con.execute("DELETE FROM msg_templates WHERE id=?", (tid,))
        for lid in CRIADOS["leads"]:
            con.execute("DELETE FROM follow_up_queue WHERE lead_id=?", (lid,))
        con.execute("UPDATE follow_up_rules SET ativo=1 WHERE ativo=0")
    for lid in CRIADOS["leads"]:
        try:
            ddb.excluir_lead(lid, forcar=True)
        except Exception as e:
            print(f"  (não deu para limpar o lead {lid}: {e})")


# ── Espiões ──────────────────────────────────────────────────────────────────

class Espiao:
    def __init__(self, resposta):
        self.resposta = resposta
        self.chamadas = []

    def __call__(self, *a, **kw):
        self.chamadas.append({"args": a, "kwargs": kw})
        return dict(self.resposta)


def main():
    global falhas
    follow_up.init()

    originais = {
        "email": email_sender.enviar,
        "wa": whatsapp_sender.enviar,
        "wa_cru": whatsapp_sender.enviar_numero,
        "wa_rep": whatsapp_sender.repeticao,
        "ig": instagram_sender.enviar,
        "ao_vivo": modo.ao_vivo,
    }
    whatsapp_sender.repeticao = lambda lead_id, etapa: None
    modo.ao_vivo = lambda: False

    try:
        # ── 1. E-mail: template lido pelas colunas certas e envio despachado ──
        print("\n1. E-mail: assunto e corpo vêm de msg_templates e chegam ao sender")
        lead = criar_lead()
        tpl = criar_template("email", "Chegou meu email, {{empresa}}?",
                             "Oi {nome_curto}, tudo bem?\n\nAbraço.")
        resetar_fila()
        criar_regra("email", 3, tpl)
        follow_up.agendar_follow_ups(lead, "email",
                                     primeiro_contato=datetime.now() - timedelta(days=4))
        espiao = Espiao({"ok": True, "para": "zz@exemplo.invalido"})
        email_sender.enviar = espiao

        r = follow_up.processar_proximo()
        checar(r["acao"] == "enviado", "envio de e-mail concluído",
               f"veio: {r}")
        checar(len(espiao.chamadas) == 1, "email_sender.enviar chamado uma vez")
        if espiao.chamadas:
            args = espiao.chamadas[0]["args"]
            assunto, corpo = args[1], args[2]
            nome = (db.get_lead(lead) or {}).get("nome", "")
            checar("{{empresa}}" not in assunto and nome in assunto,
                   "assunto renderizado com o nome do lead", f"assunto: {assunto!r}")
            checar("{nome_curto}" not in corpo,
                   "corpo renderizado sem variável literal", f"corpo: {corpo!r}")
            checar(espiao.chamadas[0]["kwargs"].get("etapa") == "follow_up_d3",
                   "etapa gravada com o dia da regra (evita falso positivo de repetição)",
                   f"etapa: {espiao.chamadas[0]['kwargs'].get('etapa')}")
        checar((item_da_fila(lead) or {}).get("status") == "sent",
               "item da fila marcado como enviado")

        # ── 2. WhatsApp passa pelo sender COM trava, nunca pelo cru ───────────
        print("\n2. WhatsApp sai pelo caminho com trava de identidade")
        lead2 = criar_lead("ZZ FollowUp WA")
        tpl2 = criar_template("whatsapp", "", "Oi {nome_curto}, passando para confirmar.")
        resetar_fila()
        criar_regra("whatsapp", 2, tpl2)
        follow_up.agendar_follow_ups(lead2, "whatsapp",
                                     primeiro_contato=datetime.now() - timedelta(days=3))
        espiao_wa = Espiao({"ok": True, "numero": "5582900000000"})
        espiao_cru = Espiao({"ok": True})
        whatsapp_sender.enviar = espiao_wa
        whatsapp_sender.enviar_numero = espiao_cru

        r = follow_up.processar_proximo()
        checar(r["acao"] == "enviado", "envio de WhatsApp concluído", f"veio: {r}")
        checar(len(espiao_wa.chamadas) == 1, "usou whatsapp_sender.enviar (com trava)")
        checar(len(espiao_cru.chamadas) == 0,
               "NÃO usou enviar_numero (envio cru, sem trava de identidade)")

        # ── 3. Falha transitória volta para a fila com backoff ────────────────
        print("\n3. Falha de envio não some: volta para a fila, com tentativa contada")
        lead3 = criar_lead("ZZ FollowUp Falha")
        tpl3 = criar_template("email", "Assunto {{empresa}}", "Corpo qualquer.")
        resetar_fila()
        criar_regra("email", 1, tpl3)
        follow_up.agendar_follow_ups(lead3, "email",
                                     primeiro_contato=datetime.now() - timedelta(days=2))
        email_sender.enviar = Espiao({"ok": False, "erro": "bot fora do ar"})

        r = follow_up.processar_proximo()
        item = item_da_fila(lead3)
        checar(r["acao"] == "adiado", "falha vira 'adiado', não silêncio", f"veio: {r}")
        checar(item["status"] == "pending" and item["tentativas"] == 1,
               "item volta para pending com tentativas=1", f"item: {item}")
        checar((item["ultimo_erro"] or "").find("bot fora do ar") >= 0,
               "motivo da falha fica gravado na fila", f"erro: {item['ultimo_erro']}")
        checar(item["scheduled_for"] > datetime.now().isoformat(sep=" "),
               "reagendado para o futuro (não trava a fila no ciclo seguinte)",
               f"scheduled_for: {item['scheduled_for']}")

        # ── 4. Teto de tentativas: desiste e marca falha ──────────────────────
        print("\n4. Depois do teto de tentativas, o item sai da fila como falha")
        for _ in range(follow_up.MAX_TENTATIVAS):
            vencer(lead3)
            r = follow_up.processar_proximo()
        item = item_da_fila(lead3)
        checar(r["acao"] == "falhou" and item["status"] == "failed",
               f"item marcado failed após {follow_up.MAX_TENTATIVAS} tentativas",
               f"acao={r.get('acao')} status={item['status']} tentativas={item['tentativas']}")

        n = follow_up.reprocessar_falhas("email")
        checar(n >= 1 and (item_da_fila(lead3) or {})["status"] == "pending",
               "reprocessar_falhas devolve o item para a fila")
        with db.conn() as con:
            con.execute("UPDATE follow_up_queue SET status='cancelled' WHERE lead_id=?",
                        (lead3,))

        # ── 5. Bloqueio de identidade em modo ao vivo é definitivo ────────────
        print("\n5. Bloqueio de identidade (ao vivo) não fica repetindo para sempre")
        lead4 = criar_lead("ZZ FollowUp Bloqueado")
        tpl4 = criar_template("email", "Assunto {{empresa}}", "Corpo.")
        resetar_fila()
        criar_regra("email", 1, tpl4)
        follow_up.agendar_follow_ups(lead4, "email",
                                     primeiro_contato=datetime.now() - timedelta(days=2))
        modo.ao_vivo = lambda: True
        email_sender.enviar = Espiao({"ok": False, "bloqueado": True,
                                      "erro": "e-mail não confirmado pelo cruzamento"})
        r = follow_up.processar_proximo()
        modo.ao_vivo = lambda: False
        checar(r["acao"] == "pulado" and (item_da_fila(lead4) or {})["status"] == "skipped",
               "bloqueio de identidade vira 'skipped', com motivo", f"veio: {r}")

        # ── 6. Lead que respondeu sai da régua ────────────────────────────────
        print("\n6. Quem respondeu não recebe follow-up")
        lead5 = criar_lead("ZZ FollowUp Respondeu")
        tpl5 = criar_template("email", "Assunto {{empresa}}", "Corpo.")
        resetar_fila()
        criar_regra("email", 1, tpl5)
        follow_up.agendar_follow_ups(lead5, "email",
                                     primeiro_contato=datetime.now() - timedelta(days=2))
        with db.conn() as con:
            con.execute("UPDATE leads SET kanban='respondeu' WHERE id=?", (lead5,))
        espiao5 = Espiao({"ok": True})
        email_sender.enviar = espiao5
        r = follow_up.processar_proximo()
        checar(r["acao"] == "cancelado" and not espiao5.chamadas,
               "lead que respondeu: nada é enviado e a régua é cancelada", f"veio: {r}")
        checar(all(i["status"] == "cancelled" for i in fila_do_lead(lead5)),
               "fila do lead fica cancelada", f"{fila_do_lead(lead5)}")

        # ── 7. Regra apontando para template inexistente é denunciada ─────────
        print("\n7. Regra com template inexistente falha alto, não em silêncio")
        lead6 = criar_lead("ZZ FollowUp Orfao")
        resetar_fila()
        rid_orfa = regra_crua("email", 1, 999999)
        follow_up.agendar_follow_ups(lead6, "email",
                                     primeiro_contato=datetime.now() - timedelta(days=2))
        email_sender.enviar = Espiao({"ok": True})
        r = follow_up.processar_proximo()
        checar(r["acao"] == "pulado" and "999999" in (r.get("erro") or ""),
               "template inexistente vira erro explicando qual", f"veio: {r}")

        v = follow_up.validar()
        checar(any(q["regra"] == rid_orfa for q in v["quebradas"]),
               "validar() acusa a regra quebrada antes da hora do envio",
               f"veio: {v}")

        # ── 8. criar_regra recusa o que não vira mensagem ─────────────────────
        print("\n8. criar_regra não deixa nascer regra quebrada")
        try:
            follow_up.criar_regra("email", 3, 999999)
            checar(False, "criar_regra recusa template inexistente")
        except ValueError:
            checar(True, "criar_regra recusa template inexistente")
        try:
            follow_up.criar_regra("email", 3, tpl2)  # tpl2 é de whatsapp
            checar(False, "criar_regra recusa template de outro canal")
        except ValueError:
            checar(True, "criar_regra recusa template de outro canal")

        # ── 9. Reivindicação: dois processos não pegam o mesmo item ───────────
        print("\n9. Scheduler e botão do painel não enviam o mesmo item duas vezes")
        lead7 = criar_lead("ZZ FollowUp Corrida")
        tpl7 = criar_template("email", "Assunto {{empresa}}", "Corpo.")
        resetar_fila()
        criar_regra("email", 1, tpl7)
        follow_up.agendar_follow_ups(lead7, "email",
                                     primeiro_contato=datetime.now() - timedelta(days=2))
        primeiro = follow_up._reivindicar()
        segundo = follow_up._reivindicar()
        checar(primeiro is not None and primeiro["lead_id"] == lead7,
               "primeira chamada reivindica o item")
        checar(segundo is None or segundo["id"] != primeiro["id"],
               "segunda chamada não pega o mesmo item", f"segundo: {segundo}")

    finally:
        email_sender.enviar = originais["email"]
        whatsapp_sender.enviar = originais["wa"]
        whatsapp_sender.enviar_numero = originais["wa_cru"]
        whatsapp_sender.repeticao = originais["wa_rep"]
        instagram_sender.enviar = originais["ig"]
        modo.ao_vivo = originais["ao_vivo"]
        limpar()

    print()
    print(f"RESULTADO: {'tudo passou' if not falhas else str(falhas) + ' falha(s)'}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
