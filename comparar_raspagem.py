"""comparar_raspagem.py — O que mudou entre a base antiga e a recém-raspada."""

import json
import sys
from pathlib import Path

import db

RAIZ = Path(__file__).resolve().parent


def _n(v):
    return v if v is not None else 0


def carregar_antes(caminho: str) -> dict:
    dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
    return {int(k): v for k, v in dados.items()}


def agora() -> dict:
    with db.conn() as con:
        return {r["id"]: dict(r) for r in con.execute(
            "SELECT id,nome,posicao,avaliacao_num,total_avaliacoes_num,tem_site,"
            "telefone,score FROM leads WHERE origem='scrap-live'")}


def comparar(antes: dict, depois: dict):
    melhoras, pioras, neutras = [], [], []

    for lid, a in antes.items():
        d = depois.get(lid)
        if d is None:
            pioras.append((lid, a["nome"], "sumiu da base"))
            continue

        av_a, av_d = _n(a["total_avaliacoes_num"]), _n(d["total_avaliacoes_num"])
        if av_a == 0 and av_d > 0:
            melhoras.append((lid, a["nome"], f"contagem de avaliações: — → {av_d}"))
        elif av_a > 0 and av_d == 0:
            pioras.append((lid, a["nome"], f"contagem de avaliações: {av_a} → —"))
        elif av_a != av_d:
            neutras.append((lid, a["nome"], f"avaliações: {av_a} → {av_d}"))

        # telefone é o canal do piloto: perder um é perder o lead
        ta, td = (a["telefone"] or "").strip(), (d["telefone"] or "").strip()
        if ta and not td:
            pioras.append((lid, a["nome"], f"telefone sumiu ({ta})"))
        elif not ta and td:
            melhoras.append((lid, a["nome"], f"telefone apareceu ({td})"))
        elif ta != td:
            neutras.append((lid, a["nome"], f"telefone: {ta} → {td}"))

        na, nd = a["avaliacao_num"], d["avaliacao_num"]
        if na is not None and nd is None:
            pioras.append((lid, a["nome"], f"nota sumiu ({na})"))
        elif na is None and nd is not None:
            melhoras.append((lid, a["nome"], f"nota apareceu ({nd})"))
        elif na is not None and nd is not None and abs(na - nd) >= 0.1:
            neutras.append((lid, a["nome"], f"nota: {na} → {nd}"))

        pa, pd = a["posicao"], d["posicao"]
        if pa != pd:
            neutras.append((lid, a["nome"], f"posição: {pa} → {pd}"))

    novos = [(lid, d["nome"]) for lid, d in depois.items() if lid not in antes]
    return melhoras, pioras, neutras, novos


def main():
    caminho = sys.argv[1] if len(sys.argv) > 1 else "out/logs/antes_reraspagem.json"
    antes = carregar_antes(caminho)
    depois = agora()
    melhoras, pioras, neutras, novos = comparar(antes, depois)

    def bloco(titulo, itens, limite=40):
        print(f"\n{titulo} ({len(itens)})")
        print("-" * 74)
        for item in itens[:limite]:
            if len(item) == 2:
                print(f"  {item[0]:>4}  {item[1][:60]}")
            else:
                print(f"  {item[0]:>4}  {item[1][:34]:36} {item[2]}")
        if len(itens) > limite:
            print(f"  ... e mais {len(itens) - limite}")

    print("=" * 74)
    print(f"  ANTES: {len(antes)} leads   DEPOIS: {len(depois)} leads")
    print("=" * 74)
    bloco("MELHOROU", melhoras)
    bloco("PIOROU  (precisa de decisão)", pioras)
    bloco("MUDOU (nem melhor nem pior)", neutras)
    bloco("NOVOS na base", novos)

    com_a = sum(1 for v in antes.values() if _n(v["total_avaliacoes_num"]) > 0)
    com_d = sum(1 for v in depois.values() if _n(v["total_avaliacoes_num"]) > 0)
    print("\n" + "=" * 74)
    print(f"  contagem de avaliações capturada: {com_a}/{len(antes)} → "
          f"{com_d}/{len(depois)}")
    print(f"  pioras que exigem decisão: {len(pioras)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
