# -*- coding: utf-8 -*-
"""test_teto_diario.py — o teto por conta é do DIA e zera à meia-noite."""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))
os.environ["LEADFORGE_DB"] = str(Path(tempfile.gettempdir()) / "lf_teto_dia_pessoal.db")

import db          # noqa: E402
import limites     # noqa: E402

falhas = []


def checar(ok, texto):
    print(f"  {'ok  ' if ok else 'FALHOU'} {texto}")
    if not ok:
        falhas.append(texto)


CONTA = None


def _envio(conta_id, quando: datetime):
    """Grava um envio com a hora exata — é assim que se simula o passado."""
    with db.conn() as con:
        con.execute("INSERT INTO envios_conta(account_id,canal,destino,sent_at) "
                    "VALUES(?,?,?,?)",
                    (conta_id, "whatsapp", "82900000000",
                     quando.strftime("%Y-%m-%d %H:%M:%S")))


def preparar():
    print()
    print("[0] uma conta de WhatsApp com teto 10")
    global CONTA
    db.init_db()
    limites.init()
    # A tabela `accounts` e do `account_manager`, nao do `limites`: o registro
    # de envios e uma coisa, o cadastro de contas e outra.
    import account_manager
    account_manager.init()
    with db.conn() as con:
        con.execute("DELETE FROM envios_conta")
        # O banco e reaproveitado entre execucoes: sem isto a segunda rodada
        # esbarra no UNIQUE(channel, identifier).
        con.execute("DELETE FROM accounts WHERE identifier='5582900000000'")
        cur = con.execute(
            "INSERT INTO accounts(channel,identifier,display_name,daily_limit,status) "
            "VALUES('whatsapp','5582900000000','Teste',10,'active')")
        CONTA = cur.lastrowid
    checar(CONTA is not None, "conta criada")


def teste_o_corte_e_meia_noite():
    print()
    print("[1] o corte é a meia-noite de hoje, no horário local")
    corte = limites._corte()
    hoje = datetime.now().strftime("%Y-%m-%d")
    checar(corte == hoje + " 00:00:00", f"corte = {corte}")
    # Em UTC a virada aconteceria às 21h de Sao Paulo, cortando o teto no meio da
    # tarde. O operador opera aqui, e a meia-noite tem de ser a dele.
    checar(corte.endswith("00:00:00"), "e e mesmo meia-noite, nao um horario qualquer")


def teste_conta_so_o_que_saiu_hoje():
    print()
    print("[2] conta o que saiu hoje, e ignora ontem")
    agora = datetime.now()
    # Tres de hoje, de manha cedo (mas depois da meia-noite).
    for i in range(3):
        _envio(CONTA, agora.replace(hour=1, minute=10 + i))
    # Dois de ONTEM, a noite — dentro das ultimas 24h, fora do dia de hoje.
    # Na janela deslizante estes DOIS contariam; agora nao contam.
    ontem = agora - timedelta(days=1)
    for i in range(2):
        _envio(CONTA, ontem.replace(hour=23, minute=30 + i))

    v = limites.pode_enviar(CONTA)
    checar(v["usados"] == 3, f"usados = {v['usados']} (esperado 3, nao 5)")
    checar(v["restam"] == 7, f"restam = {v['restam']}")
    checar(v["ok"] is True, "e ainda pode enviar")


def teste_cheia_libera_a_meia_noite():
    print()
    print("[3] cheia: a vaga volta à meia-noite, e todas juntas")
    agora = datetime.now()
    for i in range(7):                      # 3 + 7 = 10 = teto
        _envio(CONTA, agora.replace(hour=2, minute=i))

    v = limites.pode_enviar(CONTA)
    checar(v["ok"] is False, f"a conta fecha no teto ({v['usados']}/{v['limite']})")
    checar("hoje" in (v["motivo"] or ""),
           f"e o motivo fala de HOJE, nao de 24h: {v['motivo']!r}")
    # Na janela deslizante isto era "24h depois da mensagem mais antiga" — um
    # horario diferente para cada conta, dificil de prever. Agora e um so.
    amanha = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
              + timedelta(days=1))
    checar(v["libera_em"] == amanha.strftime("%Y-%m-%d %H:%M:%S"),
           f"libera à meia-noite: {v['libera_em']}")


def teste_o_bot_desligado_na_virada_volta_zerado():
    print()
    print("[4] O CASO DO PEDIDO: bot desligado na virada volta zerado")
    # Este e o caso que o operador levantou: "caso o bot inicie no dia seguinte e
    # veja que ja devia ter sido resetado, ele ja reseta automaticamente".
    with db.conn() as con:
        con.execute("DELETE FROM envios_conta")
    ontem = datetime.now() - timedelta(days=1)
    for i in range(10):                     # ontem a conta bateu o teto
        _envio(CONTA, ontem.replace(hour=20, minute=i))

    v = limites.pode_enviar(CONTA)
    checar(v["usados"] == 0, f"hoje comeca em zero ({v['usados']})")
    checar(v["restam"] == 10, f"com o teto inteiro disponivel ({v['restam']})")
    checar(v["ok"] is True, "e a conta pode enviar assim que o bot sobe")

    # E o que prova que NAO voltamos ao contador guardado: as funcoes que DECIDEM o
    # teto nao leem campo guardado nenhum — contam o registro.
    import ast
    arvore = ast.parse((RAIZ / "limites.py").read_text(encoding="utf-8"))
    corpos = {n.name: ast.unparse(n) for n in ast.walk(arvore)
              if isinstance(n, ast.FunctionDef)}
    for nome in ("pode_enviar", "_envios_na_janela", "_corte"):
        checar("sent_today" not in corpos.get(nome, ""),
               f"`{nome}` nao le contador guardado")
    checar("_corte()" in corpos.get("_envios_na_janela", ""),
           "e a contagem sai do corte da meia-noite")


def teste_teto_reduzido_no_meio_do_dia():
    print()
    print("[5] baixar o teto abaixo do que já saiu não quebra a conta")
    # O operador pode reduzir o limite depois de a conta já ter enviado. O
    # `restam` nao pode virar negativo e a tela nao pode mostrar lixo.
    with db.conn() as con:
        con.execute("DELETE FROM envios_conta")
        con.execute("UPDATE accounts SET daily_limit=3 WHERE id=?", (CONTA,))
    agora = datetime.now()
    for i in range(5):
        _envio(CONTA, agora.replace(hour=3, minute=i))

    v = limites.pode_enviar(CONTA)
    checar(v["ok"] is False, "fechada, como tem de ser")
    checar(v["restam"] == 0, f"restam nao fica negativo: {v['restam']}")
    checar(v["libera_em"] is not None, "e ainda diz quando volta")
    with db.conn() as con:
        con.execute("UPDATE accounts SET daily_limit=10 WHERE id=?", (CONTA,))


def teste_cliente_novo_nao_leva_500():
    print()
    print("[7] cliente novo, sem a tabela de contas, nao recebe erro")
    # O DEFEITO QUE ISTO PEGA: `estado()` consulta `accounts`, e um cliente que
    # ainda nao cadastrou conta nenhuma nao tem essa tabela.
    with db.conn() as con:
        con.execute("DROP TABLE IF EXISTS accounts")
    try:
        linhas_ = limites.estado()
        checar(linhas_ == [], f"sem a tabela, devolve lista vazia: {linhas_}")
    except Exception as e:
        checar(False, f"estourou em vez de devolver vazio: {e}")
    try:
        v = limites.pode_enviar(1)
        checar(v["ok"] is False,
               "e perguntar por uma conta que nao existe nao estoura")
    except Exception as e:
        checar(False, f"pode_enviar estourou: {e}")
    # E a tabela volta a existir sozinha, que e o conserto de verdade.
    with db.conn() as con:
        existe = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
        ).fetchone()
    checar(existe is not None, "e a tabela e recriada por `limites.init()`")


def teste_a_tela_recebe_o_mesmo():
    print()
    print("[6] a tela (`estado`) concorda com o portão (`pode_enviar`)")
    # Duas contas diferentes da mesma coisa foi a doenca deste projeto. Aqui as
    # duas leem `_corte()`, entao tem de bater.
    with db.conn() as con:
        con.execute("DELETE FROM envios_conta")
    agora = datetime.now()
    for i in range(4):
        _envio(CONTA, agora.replace(hour=4, minute=i))
    ontem = agora - timedelta(days=1)
    _envio(CONTA, ontem.replace(hour=22, minute=0))

    v = limites.pode_enviar(CONTA)
    linha = next((e for e in limites.estado("whatsapp") if e["id"] == CONTA), None)
    checar(linha is not None, "a conta aparece na tela")
    if linha:
        checar(linha["usados"] == v["usados"] == 4,
               f"o mesmo numero nos dois: tela={linha['usados']} portao={v['usados']}")
        checar(linha["restam"] == v["restam"], "e o mesmo restante")


if __name__ == "__main__":
    print("=" * 70)
    print("O TETO POR CONTA — do dia, zerando a meia-noite")
    print("=" * 70)
    preparar()
    teste_o_corte_e_meia_noite()
    teste_conta_so_o_que_saiu_hoje()
    teste_cheia_libera_a_meia_noite()
    teste_o_bot_desligado_na_virada_volta_zerado()
    teste_teto_reduzido_no_meio_do_dia()
    teste_a_tela_recebe_o_mesmo()
    teste_cliente_novo_nao_leva_500()

    print()
    print("=" * 70)
    if falhas:
        print(f"RESULTADO: {len(falhas)} falha(s)")
        for f in falhas:
            print("  •", f)
        sys.exit(1)
    print("RESULTADO: tudo passou")
