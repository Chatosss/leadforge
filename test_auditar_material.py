"""test_auditar_material.py — Controle negativo do auditor de material."""

import sys

import auditar_material as am

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

LEAD = {"id": 1, "nome": "Clinica Teste Odonto", "telefone": "(82) 99123-4567",
        "posicao": 20, "nicho": "odontologia"}
CTX = {"cidade": "Arapiraca", "perda_apurada": True, "nivel": 2,
       "ref_id": 9, "ref_nome": "Ref X", "ref_cidade": "Manaus", "ref_posicao": 40}

CASOS = [
    ("travessao", "Isso — aqui é travessao de pontuacao.", True),
    ("travessao", "Faixa de R$ 397–597/mes apenas.", False),
    ("cidade", "Quem procura dentista em Manaus encontra.", True),
    ("cidade", "Quem procura dentista em Arapiraca encontra.", False),
    ("referencia_cidade", "texto qualquer", True),
    # a afirmação tem de ser SOBRE a referência para valer como defeito
    ("referencia_posicao", "A Ref X aparece à frente da sua clinica no Maps.", True),
    ("referencia_posicao", "Comparamos com a referencia, que esta na frente.", True),
    ("referencia_posicao", "A Ref X tem mais avaliacoes que a sua.", False),
    # o lead em 1º lugar: quem esta na frente e ele, e isso e elogio.
    # esta frase real reprovava 4 materiais corretos antes do ajuste.
    ("referencia_posicao",
     "A clinica ja ganhou a parte dificil, que e aparecer na frente.", False),
    ("valor_zerado", "soma R$ 0 que nao entra no caixa", True),
    ("valor_zerado", "soma R$ 9.600 que nao entra no caixa. R$ 0 Comecar agora", False),
    ("markdown", "Veja **isto** com atencao.", True),
    ("markdown", "Veja isto com atencao.", False),
    ("nega_personalizacao", "Este material nao e um modelo pronto, viu.", True),
    ("automacao", "Nível 2 · Conversão inclui automação de WhatsApp para voce.", True),
    ("automacao", "Nível 3 · Domínio inclui automação de WhatsApp e Instagram.", False),
    ("telefone", "Fale comigo no (82) 99123-4567 agora.", True),
    ("telefone", "Fale comigo no (82) 98888-1111 agora.", False),
    ("regiao", "quem procura dentista em 46 Rua Sao Francisco hoje.", True),
    ("regiao", "quem procura dentista em Arapiraca hoje.", False),
    ("placeholder", "Ola {nome}, tudo bem?", True),
    ("placeholder", "Ola doutora, tudo bem?", False),
    ("promessa", "Garantimos o ROI do investimento.", True),
    ("promessa", "Mostramos o que a clinica deixa de receber.", False),
]

# nota preenchida + contagem 0 = contagem NAO capturada (nota nao existe sem
# avaliacao). O material nao pode afirmar escassez em cima disso.
LEAD_SEM_CONTAGEM = dict(LEAD, avaliacao_num=3.6, total_avaliacoes_num=0)
LEAD_COM_CONTAGEM = dict(LEAD, avaliacao_num=4.8, total_avaliacoes_num=212)

CASOS_CONTAGEM = [
    ("afirma_sem_medir", LEAD_SEM_CONTAGEM,
     "Poucas avaliacoes visiveis: o paciente compara.", True),
    ("afirma_sem_medir", LEAD_SEM_CONTAGEM,
     "Nao foi possivel ler o numero de avaliacoes no perfil.", False),
    ("afirma_sem_medir", LEAD_COM_CONTAGEM,
     "Poucas avaliacoes visiveis: o paciente compara.", False),
]


def _teste_texto_cortado():
    """
    A checagem compara o que `montar()` gerou com o que o PDF imprimiu, então o controle
    precisa de um par HTML/texto, não de uma frase solta.
    """
    fn = dict(am.CHECAGENS)["texto_cortado"]
    html = ("<p>Volume de avaliacao e o que decide entre dois perfis parecidos "
            "quando o paciente compara.</p>")
    inteiro = ("Volume de avaliacao e o que decide entre dois perfis parecidos "
               "quando o paciente compara. Proximos passos")
    cortado = "Volume de avaliacao e o que decid Proximos passos"
    casos = [("PDF imprimiu inteiro", inteiro, False),
             ("PDF cortou no meio", cortado, True)]
    falhas = 0
    for rotulo, pdf_txt, deve_pegar in casos:
        obtido = bool(fn(pdf_txt, LEAD, {"html": html}))
        ok = obtido == deve_pegar
        falhas += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FALHOU'} texto_cortado        "
              f"deve {'reprova' if deve_pegar else 'aprova':<8} -> "
              f"{'reprovou' if obtido else 'aprovou'}  ({rotulo})")
    return falhas, len(casos)


def main():
    fn = dict(am.CHECAGENS)
    falhas = 0
    for nome, texto, deve_pegar in CASOS:
        obtido = bool(fn[nome](texto, LEAD, CTX))
        ok = obtido == deve_pegar
        falhas += 0 if ok else 1
        selo = "ok  " if ok else "FALHOU"
        esperado = "reprova" if deve_pegar else "aprova"
        print(f"  {selo} {nome:<20} deve {esperado:<8} -> "
              f"{'reprovou' if obtido else 'aprovou'}")

    for nome, lead, texto, deve_pegar in CASOS_CONTAGEM:
        obtido = bool(fn[nome](texto, lead, CTX))
        ok = obtido == deve_pegar
        falhas += 0 if ok else 1
        selo = "ok  " if ok else "FALHOU"
        esperado = "reprova" if deve_pegar else "aprova"
        print(f"  {selo} {nome:<20} deve {esperado:<8} -> "
              f"{'reprovou' if obtido else 'aprovou'}")

    f_corte, n_corte = _teste_texto_cortado()
    falhas += f_corte

    total = len(CASOS) + len(CASOS_CONTAGEM) + n_corte
    print()
    print(f"RESULTADO: {total - falhas}/{total} passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
