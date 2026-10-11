"""test_esteira_wa.py — O cronômetro da esteira de WhatsApp."""
import sys
from datetime import datetime, timedelta

import config
import db
import esteira_wa as ew

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

LEAD = 633
FMT = "%Y-%m-%d %H:%M:%S"
_criados = []


def _limpar():
    if not _criados:
        return
    with db.conn() as con:
        con.executemany("DELETE FROM touches WHERE id=?", [(i,) for i in _criados])
    _criados.clear()


def _enviar(etapa, quando):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO touches(lead_id,canal,etapa,status,sent_at) "
            "VALUES(?,'whatsapp',?,'sent',?)", (LEAD, etapa, quando.strftime(FMT)))
        _criados.append(cur.lastrowid)


def _guardar_reais():
    """Tira de cena os toques que já existiam, para não contaminar o cenário."""
    with db.conn() as con:
        ids = [r[0] for r in con.execute(
            "SELECT id FROM touches WHERE lead_id=? AND canal='whatsapp'", (LEAD,))]
        con.execute("UPDATE touches SET canal='__guardado' WHERE lead_id=? "
                    "AND canal='whatsapp'", (LEAD,))
    return ids


def _devolver_reais(ids):
    if not ids:
        return
    with db.conn() as con:
        con.executemany("UPDATE touches SET canal='whatsapp' WHERE id=?",
                        [(i,) for i in ids])


def ck(rotulo, ok, detalhe=""):
    print(f"  {'ok  ' if ok else 'FALHOU'} {rotulo}" + (f"  ({detalhe})" if not ok and detalhe else ""))
    return 0 if ok else 1


def main():
    agora = datetime(2026, 9, 1, 10, 0, 0)
    falhas = 0
    guardados = _guardar_reais()
    try:
        # 1) nunca contatado: o toque 1 pode sair agora
        st = ew.estado(LEAD, agora)
        falhas += ck("sem toque: próxima é a abertura e está pronta",
                     st["proxima"] == "wa1_abertura" and st["pronto"] and st["passo"] == 0,
                     f"{st['proxima']} pronto={st['pronto']}")

        # 2) abertura saiu hoje: o reforço só vence no dia 2
        _enviar("wa1_abertura", agora)
        st = ew.estado(LEAD, agora)
        falhas += ck("abertura enviada: próxima é o reforço, ainda NÃO pronta",
                     st["proxima"] == "wa2_reforco" and not st["pronto"] and st["passo"] == 1,
                     f"{st['proxima']} pronto={st['pronto']}")
        esperado = agora + timedelta(days=2)
        falhas += ck("reforço vence no dia 2 contado do 1º toque",
                     st["vence_em"] == esperado, f"{st['vence_em']} != {esperado}")

        # 3) dois dias depois: vencido
        st = ew.estado(LEAD, agora + timedelta(days=2, minutes=1))
        falhas += ck("no dia 2 o reforço está pronto", st["pronto"])

        # 4) trava do mesmo dia: mensagem recente empurra o vencimento
        _enviar("wa2_reforco", agora + timedelta(days=2))
        st = ew.estado(LEAD, agora + timedelta(days=2, hours=1))
        falhas += ck("acabou de receber: a próxima NÃO sai no mesmo dia",
                     not st["pronto"], f"vence_em={st['vence_em']}")

        # 5) reentrada: sumiu por semanas, volta e retoma da etapa certa
        st = ew.estado(LEAD, agora + timedelta(days=40))
        falhas += ck("reentrada tardia: retoma da isca, não da abertura",
                     st["proxima"] == "wa3_isca" and st["passo"] == 2, str(st["proxima"]))
        falhas += ck("reentrada tardia: já está pronta para sair", st["pronto"])

        # 6) esteira concluída para de vez
        _enviar("wa3_isca", agora + timedelta(days=5))
        _enviar("wa4_encerramento", agora + timedelta(days=9))
        st = ew.estado(LEAD, agora + timedelta(days=30))
        falhas += ck("depois do 4º toque a esteira encerra",
                     st["concluida"] and st["proxima"] is None and not st["pronto"])

        # 7) etapa legada conta como o toque 1
        _limpar()
        _enviar("dia0_ping", agora)
        st = ew.estado(LEAD, agora + timedelta(days=3))
        falhas += ck("`dia0_ping` antigo conta como abertura",
                     st["passo"] == 1 and st["proxima"] == "wa2_reforco",
                     f"passo={st['passo']} proxima={st['proxima']}")
    finally:
        _limpar()
        _devolver_reais(guardados)

    print()
    print(f"RESULTADO: {9 - falhas}/9 passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
