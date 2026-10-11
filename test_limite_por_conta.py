# -*- coding: utf-8 -*-
"""
test_limite_por_conta.py — O limite diário de cada conta é o ÚNICO limite do WhatsApp,
então ele precisa segurar sozinho.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))
os.environ["LEADFORGE_DB"] = str(Path(tempfile.gettempdir()) / "lf_limite_conta_pessoal.db")

import db          # noqa: E402
import limites     # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

falhas = []
NUMERO = "5582900000001"
PAUSADA = "5582900000002"


def checar(ok, texto):
    print(f"  {'ok  ' if ok else 'FALHOU'} {texto}")
    if not ok:
        falhas.append(texto)


def preparar():
    db.init_db()
    limites.init()
    import account_manager
    account_manager.init()
    with db.conn() as con:
        con.execute("DELETE FROM envios_conta")
        con.execute("DELETE FROM accounts WHERE identifier IN (?,?)", (NUMERO, PAUSADA))
        a = con.execute("INSERT INTO accounts(channel,identifier,display_name,daily_limit,status) "
                        "VALUES('whatsapp',?,?,3,'active')", (NUMERO, "teste")).lastrowid
        b = con.execute("INSERT INTO accounts(channel,identifier,display_name,daily_limit,status) "
                        "VALUES('whatsapp',?,?,5,'paused')", (PAUSADA, "pausada")).lastrowid
    return a, b


def main():
    conta, pausada = preparar()

    print("[1] reserva na entrega ocupa a vaga")
    limites.reservar(conta, "whatsapp", "82999990001", lead_id=9001)
    limites.reservar(conta, "whatsapp", "82999990002", lead_id=9002)
    checar(limites.pode_enviar(conta)["restam"] == 1, "2 na fila de um limite 3 -> resta 1")

    print("[2] confirmação não conta duas vezes")
    limites.confirmar_fila("whatsapp", 9001, NUMERO, "82999990001")
    with db.conn() as con:
        linhas = con.execute("SELECT situacao FROM envios_conta WHERE lead_id=9001").fetchall()
    checar([r[0] for r in linhas] == ["enviado"], f"lead 9001 vira 1 linha 'enviado' ({[r[0] for r in linhas]})")
    checar(limites.pode_enviar(conta)["restam"] == 1, "continua restando 1")

    print("[3] falha do bot devolve a vaga")
    limites.devolver_fila("whatsapp", 9002)
    checar(limites.pode_enviar(conta)["restam"] == 2, "a vaga do lead 9002 voltou")

    print("[4] reserva esquecida expira")
    velha = (datetime.now() - timedelta(hours=limites.FILA_VALE_HORAS + 1)).strftime("%Y-%m-%d %H:%M:%S")
    if velha[:10] == datetime.now().strftime("%Y-%m-%d"):
        with db.conn() as con:
            con.execute("INSERT INTO envios_conta(account_id,canal,destino,lead_id,sent_at,situacao) "
                        "VALUES(?,?,?,?,?,'fila')", (conta, "whatsapp", "x", 9003, velha))
        checar(limites.pode_enviar(conta)["restam"] == 2,
               f"reserva de {limites.FILA_VALE_HORAS + 1}h atrás não conta")
    else:
        print("  (pulado: antes das 3h da manhã a reserva velha cairia em ontem)")

    print("[4b] confirmação de lote antigo, sem reserva, ainda registra")
    limites.confirmar_fila("whatsapp", 9004, NUMERO, "82999990004")
    checar(limites.pode_enviar(conta)["restam"] == 1, "envio sem reserva conta pelo remetente")

    print("[5] conta pausada fora das vagas do canal")
    checar(limites.vagas_hoje("whatsapp") == 1,
           f"vagas do canal = só a ativa ({limites.vagas_hoje('whatsapp')})")

    print("[6] limite fora da faixa")
    for valor in ("0", "201", "abc", "-3"):
        try:
            limites.validar_limite(valor)
            checar(False, f"{valor!r} deveria ser recusado")
        except ValueError:
            checar(True, f"{valor!r} recusado")
    checar(limites.validar_limite("200") == 200, "200 aceito")

    print("[7] envio avulso sem conta cadastrada não sai")
    from senders import whatsapp_sender as ws
    originais = (ws.identidade.pode_enviar, ws.modo.permite, ws._post, ws._registrar, ws.db.get_lead)
    postou = []
    try:
        ws.db.get_lead = lambda i: {"id": i, "telefone": "82999990009", "status": "qualified"}
        ws.identidade.pode_enviar = lambda *a, **k: {"ok": True, "valor": "5582999990009"}
        ws.modo.permite = lambda *a, **k: {"ok": True}
        ws._registrar = lambda *a, **k: None
        ws._post = lambda rota, corpo: postou.append(rota) or {"ok": True}
        r = ws.enviar(9009, "oi", conta="5511999999999")
        checar(not r["ok"] and not postou, f"número fora de Contas recusado ({r.get('erro', '')[:50]!r})")
        checar(not r.get("ocupada"), "número fora de Contas é falha (conta tentativa), não espera sem fim")
        r = ws.enviar(9009, "oi", conta=PAUSADA)
        checar(not r["ok"] and not postou, f"conta pausada recusada ({r.get('erro', '')[:40]!r})")
        for i in range(3):
            limites.registrar(conta, "whatsapp", destino=f"x{i}")
        r = ws.enviar(9009, "oi", conta=NUMERO)
        checar(r.get("ocupada") and r.get("adiar_ate") and not postou,
               f"limite do dia: espera até {r.get('adiar_ate')} (follow-up não descarta)")
        import follow_up
        from datetime import datetime as _dt
        reag = []
        _orig = follow_up._reagendar
        follow_up._reagendar = lambda item, tent, minutos, erro: reag.append(minutos)
        try:
            follow_up._adiar_sem_contar(1, 0, "teto", r.get("adiar_ate"))
        finally:
            follow_up._reagendar = _orig
        falta = (_dt.strptime(r["adiar_ate"], "%Y-%m-%d %H:%M:%S") - _dt.now()).total_seconds() / 60
        checar(reag and abs(reag[0] - falta) < 2, f"o follow-up espera {reag[0] if reag else '?'} min (até a meia-noite)")
    finally:
        (ws.identidade.pode_enviar, ws.modo.permite, ws._post, ws._registrar, ws.db.get_lead) = originais

    with db.conn() as con:
        con.execute("DELETE FROM envios_conta")
        con.execute("DELETE FROM accounts WHERE identifier IN (?,?)", (NUMERO, PAUSADA))

    print()
    print("RESULTADO:", "tudo passou" if not falhas else f"{len(falhas)} falha(s)")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
