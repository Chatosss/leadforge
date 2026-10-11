"""test_upsert_preserva.py — Re-raspagem não pode apagar dado que já existia."""

import sys

import db

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

URL = "http://teste.local/upsert-preserva"

COMPLETA = {"maps_url": URL, "nome": "Clinica X", "categoria": "Clínica odontológica",
            "nicho": "odontologia", "avaliacao_num": 4.8, "total_avaliacoes_num": 114,
            "tem_site": 1, "tem_instagram": 0, "instagram": "",
            "telefone": "(82) 99999-0000", "website": "http://x.com",
            "endereco": "Rua A", "posicao": 5}

POBRE = dict(COMPLETA, avaliacao_num=None, total_avaliacoes_num=0, tem_site=0,
             telefone="", website="", endereco="", posicao=7)

MELHOR = dict(COMPLETA, avaliacao_num=4.9, total_avaliacoes_num=130,
              telefone="(82) 98888-1111", website="http://y.com",
              endereco="Rua B", posicao=3)


def _estado():
    with db.conn() as con:
        r = con.execute(
            "SELECT nome,avaliacao_num,total_avaliacoes_num,telefone,website,"
            "tem_site,endereco,posicao FROM leads WHERE maps_url=?", (URL,)).fetchone()
    return dict(r) if r else {}


def _limpar():
    with db.exclusao_liberada():
        with db.conn() as con:
            con.execute("DELETE FROM leads WHERE maps_url=?", (URL,))


def ck(rotulo, ok):
    print(f"  {'ok  ' if ok else 'FALHOU'} {rotulo}")
    return 0 if ok else 1


def main():
    falhas = 0
    _limpar()
    try:
        db.upsert_lead(COMPLETA)
        e = _estado()
        falhas += ck("leitura completa grava tudo",
                     e.get("total_avaliacoes_num") == 114 and e.get("website") == "http://x.com")

        db.upsert_lead(POBRE)
        e = _estado()
        falhas += ck("leitura pobre PRESERVA o nº de avaliações",
                     e.get("total_avaliacoes_num") == 114)
        falhas += ck("leitura pobre PRESERVA a nota", e.get("avaliacao_num") == 4.8)
        falhas += ck("leitura pobre PRESERVA o telefone",
                     e.get("telefone") == "(82) 99999-0000")
        falhas += ck("leitura pobre PRESERVA o site", e.get("website") == "http://x.com")
        falhas += ck("leitura pobre PRESERVA o endereço", e.get("endereco") == "Rua A")
        falhas += ck("tem_site continua 1, derivado do site preservado",
                     e.get("tem_site") == 1)
        falhas += ck("posição ATUALIZA (é o que muda a cada busca)",
                     e.get("posicao") == 7)

        db.upsert_lead(MELHOR)
        e = _estado()
        falhas += ck("leitura com valor novo SOBRESCREVE avaliações",
                     e.get("total_avaliacoes_num") == 130)
        falhas += ck("leitura com valor novo SOBRESCREVE telefone",
                     e.get("telefone") == "(82) 98888-1111")
    finally:
        _limpar()

    total = 11
    print()
    print(f"RESULTADO: {total - falhas}/{total} passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
