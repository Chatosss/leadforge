"""abordagem.py — A copy de prospecção, por canal e por toque da cadência."""

import re
from typing import Dict, Optional

import config
import db
import diagnostico as diag_mod

# ── Formato por canal ────────────────────────────────────────────────────────
# O que muda entre canais é o formato e o grau de informalidade, não a estratégia.
FORMATOS = {
    "whatsapp": (
        "CANAL: WhatsApp, primeiro contato de um número desconhecido.\n"
        "FORMATO: no máximo 3 linhas curtas, separadas por quebra de linha. "
        "Menos de 45 palavras no total. ZERO emoji. Nenhum link."),
    "instagram": (
        "CANAL: DM de Instagram.\n"
        "FORMATO: no máximo 3 linhas, tom de rede social, um pouco mais leve que "
        "o WhatsApp. Menos de 45 palavras. No máximo 1 emoji, e só se couber "
        "naturalmente. Nenhum link."),
    "email": (
        "CANAL: e-mail frio.\n"
        "FORMATO: primeira linha exatamente 'Assunto: ...' com no máximo 6 palavras, "
        "sem hype, de preferência com o dado concreto. Corpo com no máximo 90 "
        "palavras, em 3 parágrafos curtos. Sem saudação genérica do tipo 'espero "
        "que esteja bem'. Sem assinatura com telefone ou endereço.\n"
        "O assunto tem que ser DIFERENTE do assunto dos e-mails anteriores desta "
        "sequência: assunto repetido faz o segundo e-mail parecer reenvio do "
        "primeiro, e ele é apagado sem abrir."),
}

# ── Papel de cada toque ──────────────────────────────────────────────────────
PAPEIS = {
    # Não passa pela IA: `gerar()` devolve o texto fixo para esta etapa.
    "dia0_ping": (
        "PAPEL DESTE TOQUE: só confirmar que é a clínica certa. NÃO é abordagem.\n"
        "Uma linha, saudação pelo horário e a pergunta de identificação.\n"
        "É PROIBIDO: dizer seu nome, citar posição, nota, avaliações, valor, "
        "número de pacientes, oferecer qualquer coisa ou explicar por que está "
        "escrevendo. Tudo isso é a mensagem seguinte."),
    "dia0": (
        "PAPEL DESTE TOQUE: dizer o que você encontrou e PEDIR PERMISSÃO para "
        "mandar o material. NÃO é marcar reunião.\n"
        "A pessoa acabou de confirmar que é a clínica, então ela está esperando "
        "você dizer a que veio. Diga, nesta ordem:\n"
        "1. Que você deu uma olhada em como a clínica aparece e ENCONTROU ALGUNS "
        "PONTOS ERRADOS. Fale de erros achados, não de oportunidade.\n"
        "2. DOIS exemplos concretos e conferíveis, tirados do contexto: a posição "
        "na busca e a nota do Google. Use os números reais dos campos "
        "'posicao_google' e 'nota_google'. Se um dos dois não estiver no "
        "contexto, dê só o que existe, e NÃO invente o outro.\n"
        "3. Que isso está atrapalhando a clínica hoje, custando paciente que "
        "procura e acaba em outro lugar. Diga que atrapalha, sem quantificar.\n"
        "4. Que você montou uma ANÁLISE GRATUITA da clínica, que ela já está "
        "pronta, e pergunte se pode mandar. A pergunta final é essa e só essa.\n"
        "Opcional (permissão social): pode situar de onde veio o contato ('vi sua "
        "clínica pesquisando X aqui em Y'). NÃO cite o nome da sua empresa.\n"
        "É PROIBIDO neste toque: qualquer valor em R$, número de pacientes "
        "perdidos, preço, percentual de perda, e QUALQUER pedido de reunião, "
        "call, conversa de 10 minutos ou horário. O número e a reunião vêm "
        "DEPOIS, quando o material já tiver sido enviado. Pedir reunião aqui "
        "troca um sim fácil (receber um PDF) por um sim caro (dar tempo).\n"
        "EXEMPLO DE ESTILO (imite o formato, NUNCA copie os números ou a cidade: "
        "use sempre os dados reais do contexto):\n"
        "  - 'Oi, aqui é o João. Dei uma olhada em como a clínica aparece no "
        "Google e encontrei alguns pontos errados. Vocês aparecem em 12º quando "
        "alguém procura dentista em Tal, e a nota está em 4,1. Isso está fazendo "
        "a clínica perder paciente para quem aparece antes. Montei uma análise "
        "gratuita disso e já está pronta aqui, posso te mandar?'"),
    "dia2": (
        "PAPEL DESTE TOQUE: mostrar o tamanho do problema, agora com contexto.\n"
        "A pessoa já recebeu a primeira mensagem. Traga o número: quantos pacientes "
        "por mês e quanto em R$ a posição atual custa. Diga em poucas palavras DE "
        "ONDE vem a conta, usando só 'como_o_numero_foi_calculado': número sem "
        "origem soa chute, número com origem soa autoridade, e é a origem que "
        "compra a reunião.\n"
        "Feche com o MENOR passo possível: 10 minutos para mostrar o cálculo. Se "
        "couber natural, dê a saída numa frase curta ('se não fizer sentido, é só "
        "dizer'). Facilitar o não aumenta a chance do sim.\n"
        "Não repita a observação da primeira mensagem como se fosse novidade."),
    "dia4": (
        "PAPEL DESTE TOQUE: prova por comparação.\n"
        "Mostre o que uma clínica de referência da MESMA região faz diferente, "
        "usando só os números dela que estão no contexto. O ponto NÃO é elogiar a "
        "concorrente: é mostrar que a diferença é de PRESENÇA DIGITAL, não de "
        "qualidade do trabalho clínico. Esse enquadramento tira o dono da defensiva, "
        "porque ninguém aceita ouvir que atende mal, mas todo mundo aceita que "
        "ainda não mexeu no perfil.\n"
        "A comparação tem que ser no eixo de 'em_que_ela_e_melhor'. Se a "
        "referência aparece DEPOIS da clínica na busca, o argumento é o que ela "
        "MOSTRA no perfil (avaliações, nota, site, Instagram), nunca a posição.\n"
        "ASSUNTO: no máximo 6 palavras, com o dado concreto, sem hype. Bons "
        "exemplos de ângulo quando ela aparece antes: 'quem aparece antes da sua "
        "clínica', 'por que o vizinho aparece primeiro'. Quando ela aparece "
        "depois: 'a diferença não é o atendimento', 'o perfil que mostra mais'.\n"
        "Feche pedindo 10 minutos."),
    "dia7_followup": (
        "PAPEL DESTE TOQUE: último ângulo, com saída fácil.\n"
        "A pessoa JÁ RECEBEU três mensagens com a posição no Google, o valor "
        "perdido por mês e a comparação com a concorrente. É PROIBIDO repetir "
        "qualquer um desses três: repetir o argumento é o que faz a sequência "
        "virar perseguição.\n"
        "O corte novo é o TEMPO: a diferença não fica parada, ela se acumula "
        "enquanto nada muda. Fale do acumulado, não da perda mensal de novo.\n"
        "Feche com uma pergunta orientada ao 'não', que devolve o controle para a "
        "pessoa: algo como 'faz sentido eu parar de te escrever sobre isso?'. Quem "
        "pode dizer não sem custo responde mais do que quem só pode dizer sim. "
        "Deixe explícito que está tudo bem se não for prioridade agora."),
    "dia12_ultima": (
        "PAPEL DESTE TOQUE: encerrar a sequência, educadamente. NÃO é vender.\n"
        "Duas linhas: você para por aqui e deixa uma porta concreta e sem custo "
        "aberta, do tipo 'se um dia quiser ver a conta, fica guardada aqui'.\n"
        "É PROIBIDO: citar valor perdido, número de pacientes, posição no Google, "
        "concorrente, ou qualquer argumento de venda. Quem encerra e reabre o "
        "pitch no mesmo parágrafo não está encerrando.\n"
        "Sem ironia, sem cobrança, sem 'última chance'. Tom de quem respeita o "
        "silêncio da outra pessoa."),
}

# ── Regras duras da abordagem (somam às regras de VOZ do copy_ai) ────────────
REGRAS = f"""
IDENTIDADE: você é {config.ATENDENTE_NOME}, uma pessoa, e é QUEM ESCREVE a
mensagem. O campo "quem_assina_a_mensagem" é o SEU nome, nunca o nome de quem
recebe. É PROIBIDO chamar o destinatário por esse nome ou cumprimentá-lo com ele.
Quem recebe é o estabelecimento do campo "clinica".
Apresente-se no começo OU assine no fim, nunca os dois. NÃO cite o nome da sua
empresa no dia 0: citar empresa logo de cara liga o filtro de vendedor de quem lê.

SOBRE O TEMPO: você NÃO acompanha o estabelecimento ao longo do tempo. É PROIBIDO escrever
"desde que comecei a acompanhar", "nos últimos meses", "venho observando" ou
qualquer coisa que sugira monitoramento. Os dados são de uma leitura só. Projeção
para frente ("cada mês que segue assim") é permitida; observação passada, não.

SOBRE O CÁLCULO: é PROIBIDO inventar o método por trás do número. Se for explicar
de onde ele vem, use somente o que está em "como_o_numero_foi_calculado". Se esse
campo não existir, diga que mostra a conta na conversa, sem descrever regra
nenhuma. Frases como "cada posição abaixo da quinta custa um paciente" são
inventadas e derrubam a credibilidade do número inteiro.

SOBRE A CONCORRENTE: só cite número dela (nota, avaliações, posição) se o campo
correspondente estiver preenchido no contexto. Confira
"esta_a_frente_da_clinica" antes de dizer que ela aparece antes ou depois (use "estabelecimento" no texto):
afirmar a posição errada destrói a credibilidade da mensagem inteira.
O campo "em_que_ela_e_melhor" diz qual é a ÚNICA vantagem comprovada dela, e a
comparação tem que ser feita NESSE eixo. Se ele não for "aparece ANTES", então a
referência aparece DEPOIS do estabelecimento, e é PROIBIDO escrever que ela aparece
antes, na frente, acima ou em posição melhor: compare pelo que o eixo permite
(volume de avaliações, nota, site e Instagram no perfil).

É PROIBIDO ESCREVER (denuncia mensagem em massa e/ou é gatilho de spam):
"sem compromisso", "passando aqui", "rapidinho", "tudo bem?", "espero que esteja
bem", "venho oferecer", "gostaria de apresentar", "aproveite", "última chance",
"oportunidade única", "dei uma olhada na sua presença digital".

ASSINATURA: termine com o seu primeiro nome sozinho, em uma linha própria, e nada
mais. Nada de "Sou o X", "Meu nome é X", "X aqui" no meio ou no fim do texto. Se
você se apresentou na primeira linha, não assine de novo no fim.

MAIS REGRAS:
- NÃO comece a mensagem com o nome do estabelecimento. Abrir com o nome completo é a
  assinatura visual de mala direta, e é a primeira coisa que quem lê reconhece.
  Prefira "seu estabelecimento" ou comece direto pelo fato. Use o nome no máximo uma vez
  na mensagem inteira, e só se ficar natural.
- Uma pergunta por mensagem. Duas perguntas fazem a pessoa não responder nenhuma.
- Nada de lista entre parênteses: isso lê como relatório, não como mensagem.
- Não diga o que você "fez" (analisei, montei, preparei). Diga o que você VIU.
- Nunca prometa retorno, lucro, payback ou primeira posição no Google.
"""


# ── Fatos da clínica ─────────────────────────────────────────────────────────

def _cidade(lead: Dict) -> str:
    import identidade
    return (identidade._cidade_do_endereco(lead.get("endereco") or "")
            or identidade._cidade_da_operacao())


def _servico(lead: Dict) -> str:
    """Como o paciente procura: 'dentista', 'clínica de estética'."""
    cat = (lead.get("categoria") or "").strip().lower()
    return cat or {"odontologia": "dentista", "estetica": "estética",
                   "fisioterapia": "fisioterapeuta",
                   "veterinaria": "veterinário"}.get(lead.get("nicho"), "estabelecimento")


_GENERICAS = {"clinica", "odontologia", "odonto", "dentista", "consultorio",
              "centro", "estetica", "saude", "instituto", "studio", "dental"}


def _norm_pt(p: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", p.lower())
                   if unicodedata.category(c) != "Mn")


def _gancho_sugerido(lead: Dict) -> str:
    """Qual observação abre o dia 0."""
    pos = lead.get("posicao")
    nota = lead.get("avaliacao_num")
    if pos and pos > 3:
        return "posicao no Google"
    if lead.get("avaliacoes_sem_resposta"):      # quando o dado passar a existir
        return "avaliações recentes sem resposta"
    if nota is not None and nota < config.NOTA_ALVO \
            and (lead.get("total_avaliacoes_num") or 0) >= 10:
        return "nota abaixo de 4,5"
    if not lead.get("tem_site"):
        return "perfil do Google sem site"
    if not lead.get("tem_instagram"):
        return "perfil sem Instagram ligado"
    return "presença geral no Google"


def _contexto(lead: Dict, etapa: str) -> Dict:
    """
    Contexto para a IA. Campo vazio vai marcado, nunca omitido: omitir convida o modelo
    a preencher a lacuna sozinho.
    """
    import conteudo
    ctx = conteudo._contexto(lead)
    ctx["cidade_da_clinica"] = _cidade(lead) or conteudo._NAO_INFORMADO
    ctx["como_o_paciente_procura"] = _servico(lead)
    ctx["quem_assina_a_mensagem"] = config.ATENDENTE_NOME
    ctx["gancho_sugerido"] = _gancho_sugerido(lead)

    # Números da concorrente de referência.
    conc = diag_mod.montar_concorrente(lead)
    if conc:
        pos_l, pos_c = lead.get("posicao"), conc.get("posicao")
        ctx["concorrente_referencia"] = {
            "nome": conc.get("nome") or conteudo._NAO_INFORMADO,
            "nota": conc.get("nota") if conc.get("nota") is not None
                    else conteudo._NAO_INFORMADO,
            "total_avaliacoes": conc.get("avaliacoes") or conteudo._NAO_INFORMADO,
            "posicao_google": pos_c or conteudo._NAO_INFORMADO,
            "esta_a_frente_da_clinica": (
                "sim" if (pos_c and pos_l and pos_c < pos_l)
                else "não, ela aparece DEPOIS da clínica destinatária"
                if (pos_c and pos_l) else conteudo._NAO_INFORMADO),
            # o eixo diz qual é a ÚNICA vantagem comprovada dela. Sem isso o
            # modelo assumia vantagem de posição mesmo com o campo acima dizendo
            # o contrário, e o e-mail do dia 4 se contradizia sozinho
            "em_que_ela_e_melhor": {
                "posicao": "aparece ANTES da clínica na busca",
                "reputacao": ("tem mais avaliações ou nota melhor, mas aparece "
                              "DEPOIS: é PROIBIDO dizer que ela aparece antes"),
                "presenca": ("mostra site ou Instagram no perfil do Google e a "
                             "clínica não, mas aparece DEPOIS: é PROIBIDO dizer "
                             "que ela aparece antes"),
            }.get(conc.get("eixo"), conteudo._NAO_INFORMADO),
        }
    else:
        ctx["concorrente_referencia"] = conteudo._NAO_INFORMADO

    # O telefone do prospect não serve para escrever a copy e já vazou: numa
    # geração o modelo assinou o WhatsApp do lead 582 com o número da PRÓPRIA
    # estabelecimento destinatário. Fora do contexto, não tem como sair no texto.
    ctx.pop("telefone_da_clinica_destinataria", None)
    # A CONTA, não só o resultado. Mandando apenas "perde R$ 6.400" o modelo
    # inventou a metodologia ("cada posição abaixo da 5ª reduz 1 paciente a cada 30
    # dias"), que não existe.
    try:
        import estimativa
        est = estimativa.estimar(lead)
        ctx["como_o_numero_foi_calculado"] = est.get("memoria_calculo") or est.get("memoria")
    except Exception:
        ctx["como_o_numero_foi_calculado"] = conteudo._NAO_INFORMADO

    # no dia 0 o número de perda não pode nem estar no contexto: se estiver,
    # o modelo acha um jeito de usá-lo
    if etapa == "dia0":
        for interno in ("perda_mes_reais", "pacientes_perdidos_mes",
                        "concorrente_referencia", "como_o_numero_foi_calculado"):
            ctx.pop(interno, None)
        return ctx

    # Estabelecimento no topo não perde por posição, e a conta por CTR devolve zero.
    if not diag_mod.perda_apurada(lead)[3]:
        for interno in ("perda_mes_reais", "pacientes_perdidos_mes",
                        "como_o_numero_foi_calculado"):
            ctx.pop(interno, None)
        ctx["perda_por_posicao"] = (
            "NÃO HÁ. Este estabelecimento já aparece entre as primeiras da busca, então "
            "não existe perda por posição a citar. É PROIBIDO citar valor em R$, "
            "número de pacientes perdidos ou posição ruim. O que dá para tratar "
            "é o que falta no perfil dela.")
    return ctx


# ── Versões determinísticas (valem sem IA e servem de referência) ────────────

def _pergunta_fecho(cfg: Optional[Dict]) -> str:
    """A pergunta de fechamento do dia 0, conforme a variante em teste."""
    if (cfg or {}).get("pergunta") == "simnao":
        return "Isso já está no radar de vocês?"
    return "Como vocês estão lidando com isso hoje?"


_RE_CALIBRADA = re.compile(r"\b(como|o que|de que forma|qual)\b[^?]*\?", re.I)


def variante_aplicada(texto: str, cfg: Optional[Dict]) -> bool:
    """A variante REALMENTE saiu no texto?"""
    cfg = cfg or {}
    t = texto or ""
    if cfg.get("pergunta") == "simnao":
        return not bool(_RE_CALIBRADA.search(t))
    if cfg.get("pergunta") == "calibrada":
        return bool(_RE_CALIBRADA.search(t))
    if cfg.get("cta_saida") is True:
        return bool(re.search(r"(n[ãa]o fizer sentido|s[óo] (me )?dizer|sem problema)", t, re.I))
    if cfg.get("cta_saida") is False:
        return not re.search(r"n[ãa]o fizer sentido", t, re.I)
    return True


def saudacao_por_horario(agora=None) -> str:
    """Bom dia, boa tarde ou boa noite pelo relógio do envio."""
    from datetime import datetime
    h = (agora or datetime.now()).hour
    if 5 <= h < 12:
        return "bom dia"
    if 12 <= h < 18:
        return "boa tarde"
    return "boa noite"


def _nome_curto(lead: Dict) -> str:
    """O nome da clínica como uma pessoa falaria ao telefone."""
    nome = (lead.get("nome") or "").strip()
    nome = re.split(r"\s+[|\-–—]\s+", nome)[0].strip()
    palavras = nome.split()
    if len(palavras) > 5:
        nome = " ".join(palavras[:5])
    return nome


# Substantivo masculino abrindo o nome da clínica.
_MASCULINOS = ("instituto", "consultorio", "consultório", "centro", "studio",
               "espaco", "espaço", "grupo", "laboratorio", "laboratório",
               "dr", "dr.", "doutor")


def artigo_do_nome(nome: str) -> str:
    """"do" ou "da" para este nome de clínica."""
    palavras = (nome or "").split()
    if not palavras:
        return "da"
    return "do" if palavras[0].lower().strip(".,") in _MASCULINOS else "da"


def _det_dia0_ping(lead: Dict) -> str:
    """O toque de contato: confirma que é a clínica certa e não vende nada."""
    nome = _nome_curto(lead)
    if not nome:
        return f"Opa, {saudacao_por_horario()}, é do estabelecimento?"
    return f"Opa, {saudacao_por_horario()}, é {artigo_do_nome(nome)} {nome}?"


def _det_dia0(lead: Dict, canal: str, cfg: Optional[Dict] = None) -> str:
    """
    A abordagem: os erros encontrados e o pedido de permissão para mandar o material.
    Sem valor em R$ e sem pedido de reunião, que vêm depois do envio.
    """
    nome = config.ATENDENTE_NOME
    pos = lead.get("posicao")
    nota = lead.get("avaliacao_num")
    servico = _servico(lead)
    cidade = _cidade(lead)
    onde = f" em {cidade}" if cidade else ""

    # Achado é o que está ERRADO, não o que foi medido.
    bem_posicionada = bool(pos) and pos <= config.POSICAO_ALVO
    achados = []
    if pos and not bem_posicionada:
        achados.append(f"vocês aparecem em {pos}º quando alguém procura "
                       f"{servico}{onde}")
    if nota is not None and nota < config.NOTA_ALVO:
        achados.append(f"a nota está em {nota:.1f}".replace(".", ","))
    if not lead.get("tem_site"):
        achados.append("o perfil no Google está sem site")
    if not lead.get("tem_instagram"):
        achados.append("o Instagram não está ligado ao perfil")
    if not achados:
        achados.append("o perfil de vocês no Google está incompleto")
    achados = achados[:2]          # duas observações; a terceira vira parágrafo

    # O fecho segue a mesma bifurcação do documento: quem já aparece na frente
    # não está perdendo paciente por posição, e dizer que está é contrariável
    # pelo próprio destinatário em dez segundos.
    if bem_posicionada:
        consequencia = ("Vocês já aparecem bem na busca, então isso não é "
                        "posição: é o que o perfil deixa de mostrar para quem "
                        "chega nele")
    else:
            consequencia = ("Isso está fazendo o estabelecimento perder paciente para quem "
                        "aparece antes")

    return (f"Oi, aqui é o {nome}.\n"
            f"Dei uma olhada em como o estabelecimento aparece no Google e encontrei "
            f"alguns pontos: {', e '.join(achados)}.\n"
            f"{consequencia}. Montei uma análise gratuita disso e já está "
            f"pronta aqui, posso te mandar?")


def _det_dia2(lead: Dict) -> str:
    perdidos, roi, _ = diag_mod.estimar_roi(lead)
    return (f"Fiz a conta do que essa posição custa: cerca de {perdidos} pacientes "
            f"por mês que procuram seu tipo de serviço e acabam em outro estabelecimento. "
            f"No ticket médio da sua área, algo perto de "
            f"R$ {diag_mod._br(roi)} por mês.\n"
            f"O número sai de dados públicos de busca da sua cidade, te mostro a "
            f"conta em 10 minutos. Se não fizer sentido, é só me dizer.")


def _det_dia4(lead: Dict) -> Dict:
    conc = diag_mod.montar_concorrente(lead)
    pos = lead.get("posicao")
    if conc and conc.get("a_frente"):
        corpo = (f"Um concorrente da sua região aparece na frente do seu na busca e "
                 f"tem nota {conc.get('nota') or 'melhor'}. A diferença ali não é "
                 f"de trabalho clínico, é de como o perfil está montado.\n\n"
                 f"É o tipo de coisa que se ajusta sem mexer no atendimento.\n\n"
                 f"Consegue 10 minutos esta semana para eu te mostrar?")
    elif conc:
        # ela aparece DEPOIS: o argumento é o que o perfil dela mostra, não a
        # posição. Dizer "aparece na frente" aqui seria mentira conferível em
        # dez segundos, e derruba a mensagem inteira
        prova = (f"{conc['avaliacoes']} avaliações" if conc.get("avaliacoes")
                 else "o perfil completo")
        corpo = (f"Um concorrente da sua região aparece depois do seu na busca e "
                 f"mesmo assim mostra {prova} para quem abre o perfil. Quem "
                 f"compara dois perfis decide pelo que consegue ver.\n\n"
                 f"A diferença ali não é de trabalho clínico, é de como o perfil "
                 f"está montado, e se ajusta sem mexer no atendimento.\n\n"
                 f"Consegue 10 minutos esta semana para eu te mostrar?")
    else:
        corpo = (f"Quem está nas três primeiras posições da sua região tem o perfil "
                 f"completo: site no lugar, avaliações respondidas, fotos atuais.\n\n"
                 f"Nada disso mexe no seu atendimento, só em como você aparece.\n\n"
                 f"Consegue 10 minutos esta semana?")
    assunto = (f"{pos}º lugar na busca da sua região" if pos
               else "como seu estabelecimento aparece na busca")
    return {"assunto": assunto, "texto": corpo}


def _det_dia7(lead: Dict) -> Dict:
    _, roi, _ = diag_mod.estimar_roi(lead)
    # fecho orientado ao "não": quem pode recusar sem custo responde mais
    return {"assunto": "última coisa e paro de escrever",
            "texto": (f"Um ponto que costuma passar batido: essa diferença não fica "
                      f"parada. Cada mês igual soma perto de R$ {diag_mod._br(roi)}, "
                      f"e o mês seguinte começa do mesmo lugar.\n\n"
                      f"Se não for prioridade agora, faz sentido eu parar de te "
                      f"escrever sobre isso? Sem problema nenhum, é só me dizer.")}


def _det_dia12(lead: Dict) -> str:
    return ("Paro por aqui para não incomodar. Se um dia quiser ver a conta, "
            "ela fica guardada aqui comigo.\nBom trabalho.")


def _determinista(lead: Dict, canal: str, etapa: str,
                  cfg: Optional[Dict] = None) -> Dict:
    if etapa == "dia0_ping":
        return {"texto": _det_dia0_ping(lead), "assunto": ""}
    if etapa == "dia0":
        return {"texto": _det_dia0(lead, canal, cfg), "assunto": ""}
    if etapa == "dia2":
        return {"texto": _det_dia2(lead), "assunto": ""}
    if etapa == "dia4":
        return _det_dia4(lead)
    if etapa == "dia7_followup":
        return _det_dia7(lead)
    if etapa == "dia12_ultima":
        return {"texto": _det_dia12(lead), "assunto": ""}
    return {"texto": _det_dia0(lead, canal), "assunto": ""}


# ── Geração ──────────────────────────────────────────────────────────────────

# Telefone brasileiro em qualquer forma escrita. Exige a forma de telefone para
# não confundir com os números que a copy usa de verdade: "R$ 9.600", "1.200
# buscas", "10,8%".
_RE_TELEFONE = re.compile(
    r"(?:\+?55\s*)?(?:\(\d{2}\)|\b\d{2})\s*9?\s*\d{4}[-.\s]?\d{4}\b"
    r"|\b\d{4,5}[-.]\d{4}\b")

# "aparece antes / na frente / acima / primeiro que" ditos da concorrente
_RE_CONC_NA_FRENTE = re.compile(
    r"\b(aparece|est[áa]|fica|vem|surge|ranqueia|posiciona[- ]?se)\b[^.\n]{0,40}"
    r"\b(antes|na frente|à frente|a frente|acima|primeiro|melhor posicionad)",
    re.I)


# Pedido de tempo da outra pessoa, em qualquer forma.
_RE_PEDE_REUNIAO = re.compile(
    r"\b\d{1,2}\s*min\b|\b\d{1,2}\s*minut|reuni[ãa]o|\bcall\b|te ligar|posso (te )?ligar|"
    r"liga[çc][ãa]o|bater um papo|conversa r[áa]pida|papo r[áa]pido|"
    r"(agendar|marcar)\s+(uma?\s+)?(conversa|call|reuni|hor[áa]rio|papo)|"
    r"que horas (voc[êe]|vc|fica)|tem (uns?|um)\s*\d+\s*min", re.I)


# O pedido de permissão para enviar: "posso mandar?", "posso te enviar?",
# "quer que eu mande?". Exige a forma de PERGUNTA, porque afirmar que vai
# mandar não é pedir permissão, e permissão é o ponto inteiro deste toque.
_RE_PEDE_MATERIAL = re.compile(
    r"(posso|quer que eu|gostaria que eu|te)\s+"
    r"(te\s+)?(mand|envi|passa)[a-zç]*\b[^.!?\n]*\?"
    r"|\b(mando|envio|passo)\s+(pra|para)\s+(voc[êe]|vc|ti)\b[^.!?\n]*\?", re.I)


# URL explicita, encurtador, ou dominio solto tipo "clinica.com.br/promo".
# `wa.me` e `bit.ly` entram porque sao os que mais aparecem em copy pronta.
_RE_LINK = re.compile(
    r"https?://|www\.\w|\bwa\.me\b|\bbit\.ly\b|\btinyurl\b|"
    r"\b[\w-]+\.(?:com\.br|com|net|br|io|site|link|me)\b(?:/\S*)?",
    re.I)


def violacoes(texto: str, etapa: str, lead: Optional[Dict] = None) -> list:
    """Confere as proibições que NÃO podem depender de o modelo obedecer."""
    t = (texto or "").lower()
    achados = []

    # Link em mensagem fria e um dos maiores gatilhos de bloqueio do WhatsApp: o
    # numero e denunciado por quem nunca pediu contato, e o Meta derruba.
    if etapa in ("dia0_ping", "dia0", "dia2"):
        if _RE_LINK.search(texto or ""):
            achados.append("link no primeiro contato (gatilho de bloqueio)")

    # Telefone nunca tem o que fazer na copy de prospecção.
    if _RE_TELEFONE.search(texto or ""):
        achados.append("telefone no texto")

    # Afirmar que a referência aparece antes quando ela aparece DEPOIS é mentira que
    # o destinatário confere em dez segundos, e derruba a mensagem inteira.
    if lead is not None:
        conc = diag_mod.montar_concorrente(lead) or {}
        if conc and not conc.get("a_frente"):
            # A frase precisa falar DELA. "quem aparece antes" é afirmação genérica sobre
            # quem ranqueia acima, verdadeira para qualquer lead fora do topo, e barrá-la
            # derrubava até o texto fixo de referência.
            bruto = (conc.get("nome") or "").split(" - ")[0]
            palavras = re.findall(r"[A-Za-zÀ-ÿ]{3,}", bruto)[:2]
            # comparação no texto normalizado: "Real Clínica" escrito sem acento
            # não casava, e a trava passava a deixar a alegação errada sair
            curto = _norm_pt(" ".join(palavras)) if len(palavras) == 2 else ""
            for frase in re.split(r"[.!?\n]+", texto or ""):
                if not _RE_CONC_NA_FRENTE.search(frase):
                    continue
                fala_dela = (curto and curto in _norm_pt(frase)) or \
                    re.search(r"\b(a concorrente|essa cl[íi]nica|ela)\b", frase, re.I)
                if not fala_dela:
                    continue
                achados.append("diz que a concorrente aparece antes, "
                               "e ela aparece depois")
                break
    tem_dinheiro = bool(re.search(r"r\$\s*[\d.,]+", t))
    tem_pacientes = bool(re.search(r"\d+\s*(pacientes?|consultas?)", t))
    tem_posicao = bool(re.search(r"\b\d+\s*[ºoa]?\s*(posi[çc][ãa]o|lugar)|"
                                 r"\bna\s+\d+[ºa]\b", t))
    tem_reuniao = bool(re.search(r"\b10\s*minutos|reuni[ãa]o|call\b", t))

    if etapa == "dia0":
        # a abordagem apresenta os erros e pede permissão para mandar o material.
        if tem_dinheiro:
            achados.append("valor em R$ antes do material")
        if tem_pacientes:
            achados.append("número de pacientes antes do material")
        if _RE_PEDE_REUNIAO.search(t):
            achados.append("pedido de reunião antes do material")
    # Valor em R$ para quem não tem perda apurada é número inventado: a conta por
    # CTR deu zero e o que sobrava vinha do piso da fórmula heurística.
    if lead is not None and tem_dinheiro and etapa not in ("dia0", "dia12_ultima"):
        if not diag_mod.perda_apurada(lead)[3]:
            achados.append("valor em R$ sem perda apurada (estabelecimento já está no topo)")

    if etapa == "dia12_ultima":
        # despedida que reabre o pitch não é despedida
        if tem_dinheiro:
            achados.append("valor em R$ na despedida")
        if tem_pacientes:
            achados.append("número de pacientes na despedida")
        # "posição" sem número na frente também é pitch: exigir o dígito deixava
        # passar "se quiser ajustar essa posição no Google"
        if tem_posicao or re.search(r"posi[çc][ãa]o|posicionamento|ranking", t):
            achados.append("argumento de posição na despedida")
        if tem_reuniao:
            achados.append("chamada para ação na despedida")
    return achados


def violacoes_finas(texto: str, etapa: str, lead: Dict) -> list:
    """Regras que estragam a mensagem sem quebrar o produto."""
    achados = []
    t = texto or ""
    nome = (lead.get("nome") or "").strip()

    # A pergunta do dia 0 não pode obrigar a pessoa a fazer conta antes de
    # responder.
    if etapa == "dia0" and not _RE_PEDE_MATERIAL.search(t):
        achados.append("não pede permissão para mandar o material")

    if etapa == "dia0":
        for pergunta in re.findall(r"[^.!?\n]*\?", t):
            if re.search(r"\b(quanto|quantos|quantas)\b", pergunta, re.I) or \
               re.search(r"\b(afeta|impacta|custa|prejudica)\b", pergunta, re.I):
                achados.append("pergunta que obriga a pessoa a estimar o impacto")
                break
            # empurrar o problema de volta ("como vocês pretendem resolver isso?")
            # transfere para o destinatário um trabalho que é seu
            if re.search(r"\b(resolver|melhorar|corrigir|mudar|ajustar)\b[^?]*\?",
                         pergunta, re.I):
                achados.append("pergunta que pede ao destinatário a solução")
                break

    # abrir com o nome completo é a assinatura visual de mala direta.
    if len(nome) > 3 and etapa != "dia0_ping":
        linhas = [l for l in t.strip().split("\n") if l.strip()]
        if linhas and linhas[0].lower().startswith("assunto:"):
            linhas = linhas[1:]
        if linhas and nome.lower() in linhas[0].lower():
            achados.append("abre com o nome completo do estabelecimento")

    # o dia 7 tem que trazer ângulo novo; repetir a concorrente é repetir o dia 4.
    if etapa == "dia7_followup":
        conc = diag_mod.montar_concorrente(lead) or {}
        # o par inicial do nome é a forma curta que a IA sempre escreve:
        # "Studio Riso Odontologia e Saúde - Dentista em Arapiraca" vira
        # "Studio Riso" no texto
        bruto = (conc.get("nome") or "").split(" - ")[0]
        palavras = re.findall(r"[A-Za-zÀ-ÿ]{3,}", bruto)[:2]
        if len(palavras) == 2 and " ".join(palavras).lower() in t.lower():
            achados.append("repete a concorrente, que já foi o argumento do dia 4")

        # repetir a perda MENSAL é repetir o dia 2. O acumulado é o ângulo novo,
        # então o mesmo valor com "por mês" ao lado é o que não pode.
        try:
            _, roi, _ = diag_mod.estimar_roi(lead)
            mensal = diag_mod._br(roi)
            if re.search(re.escape(mensal) + r"[^.\n]{0,20}(por m[êe]s|mensa)", t, re.I):
                achados.append("repete a perda mensal, que já foi o argumento do dia 2")
        except Exception:
            pass

    return achados


# "Assunto:" em qualquer roupagem que o modelo usa: com asterisco de markdown,
# com sublinhado, em inglês, com dois pontos ou travessão.
_RE_LINHA_ASSUNTO = re.compile(
    r"^[ \t]*(?:[*_]{1,2})?\s*(?:assunto|subject)\s*(?:[*_]{1,2})?\s*[:\-]\s*(.*)$",
    re.I | re.M)


def _limpar_formatacao(texto: str) -> str:
    """Tira a marcação que nenhum dos três canais renderiza."""
    t = texto or ""
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)
    t = re.sub(r"__(.+?)__", r"\1", t, flags=re.S)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = "\n".join(l.rstrip() for l in t.split("\n"))
    t = re.sub(r"\n{3,}", "\n\n", t)
    return _remover_assinatura_duplicada(t.strip()).strip()


def _remover_assinatura_duplicada(texto: str) -> str:
    """Nome do remetente duas vezes: na abertura e na assinatura."""
    nome = re.escape((config.ATENDENTE_NOME or "").strip())
    if not nome:
        return texto
    linhas = (texto or "").split("\n")
    cheias = [i for i, l in enumerate(linhas) if l.strip()]
    if len(cheias) < 2:
        return texto

    # o artigo é opcional de propósito: na primeira versão desta trava a regra
    # exigia "aqui é O Wallisson", o modelo escreveu "aqui é Wallisson" e a
    # assinatura duplicada passou direto para o WhatsApp
    primeira = linhas[cheias[0]]
    abriu = re.search(
        rf"\b(aqui (é|e)( o| a)?|quem fala (é|e)( o| a)?|sou( o| a)?|"
        rf"meu nome (é|e))\s+{nome}\b", primeira, re.I)
    if not abriu:
        return texto

    ultima = linhas[cheias[-1]].strip()
    if re.fullmatch(rf"{nome}[.,!]?", ultima, re.I):
        del linhas[cheias[-1]]
        return "\n".join(linhas).rstrip()
    return texto


def _separar_assunto(texto: str) -> Dict:
    """Tira a linha 'Assunto:...' do corpo do e-mail."""
    t = _limpar_formatacao(texto)
    achadas = [m.group(1).strip().strip('"').strip() for m in _RE_LINHA_ASSUNTO.finditer(t)]
    if not achadas:
        return {"assunto": "", "texto": t}
    corpo = _RE_LINHA_ASSUNTO.sub("", t)
    assunto = next((a for a in achadas if a), "")
    return {"assunto": assunto, "texto": _limpar_formatacao(corpo)}


def _ajuste_da_variante(cfg: Dict) -> str:
    """Traduz a config da variante em instrução para o modelo."""
    if not cfg:
        return ""
    partes = []
    if cfg.get("pergunta") == "simnao":
        partes.append(
            "NESTA VERSÃO, feche com uma pergunta de SIM/NÃO, curta e fácil de "
            "responder ('isso já está no radar de vocês?'). Não use pergunta "
            "aberta de 'como' ou 'o que'.")
    elif cfg.get("pergunta") == "calibrada":
        partes.append(
            "NESTA VERSÃO, feche com pergunta aberta começando por 'como' ou "
            "'o que', que obrigue a pessoa a pensar antes de responder.")
    if cfg.get("cta_saida") is False:
        partes.append("NESTA VERSÃO, NÃO ofereça saída ('se não fizer sentido, "
                      "é só dizer'). Faça só o convite.")
    elif cfg.get("cta_saida") is True:
        partes.append("NESTA VERSÃO, termine oferecendo a saída em uma frase "
                      "curta ('se não fizer sentido, é só me dizer').")
    if cfg.get("assunto") == "dado":
        partes.append("NESTA VERSÃO, o assunto do e-mail deve ser um dado "
                      "numérico seco, sem curiosidade.")
    elif cfg.get("assunto") == "curiosidade":
        partes.append("NESTA VERSÃO, o assunto do e-mail deve despertar "
                      "curiosidade, sem número.")
    return ("\n" + "\n".join(partes) + "\n") if partes else ""


def gerar(lead_id: int, canal: str = "whatsapp", etapa: str = "dia0",
          usar_ia: bool = True, assuntos_anteriores: Optional[list] = None,
          variante: Optional[Dict] = None) -> Dict:
    """
    Devolve {ok, canal, etapa, assunto, texto, fonte}. `fonte` diz se veio da IA ou do
    texto determinístico.
    """
    lead = db.get_lead(lead_id)
    if not lead:
        return {"ok": False, "erro": "lead não encontrado"}
    canal = canal if canal in FORMATOS else "whatsapp"
    etapa = etapa if etapa in PAPEIS else "dia0"

    # variante do teste A/B: sorteio estável por lead, feito em experimentos.py
    if variante is None:
        try:
            import experimentos
            variante = experimentos.variante_para(lead_id, canal, etapa)
        except Exception:
            variante = {"experimento": "", "variante": "", "config": {}}
    marca = {"experimento": variante.get("experimento", ""),
             "variante": variante.get("variante", "")}

    base = _determinista(lead, canal, etapa, variante.get("config"))
    # o ping é fixo por decisão, não por falta de IA: ver o comentário em PAPEIS
    if etapa == "dia0_ping":
        return {"ok": True, "canal": canal, "etapa": etapa,
                "fonte": "determinístico (ping não passa pela IA)", **marca, **base}
    if not usar_ia:
        return {"ok": True, "canal": canal, "etapa": etapa, "fonte": "determinístico",
                **marca, **base}

    import copy_ai
    import ia_hook
    if not ia_hook.IA_ATIVA:
        return {"ok": True, "canal": canal, "etapa": etapa,
                "fonte": "determinístico (IA desligada)", **marca, **base}

    proibidos = ""
    if canal == "email" and assuntos_anteriores:
        lista = "; ".join(f'"{a}"' for a in assuntos_anteriores if a)
        if lista:
            proibidos = (f"\nASSUNTOS JÁ USADOS nesta sequência, é PROIBIDO repetir "
                         f"qualquer um deles ou variação próxima: {lista}. "
                         f"Escolha outro ângulo para o assunto.\n")

    # A instrução da variante vem POR ÚLTIMO, e existir já é novidade: a função que
    # a monta estava escrita e nunca era chamada, então o modelo nunca soube qual
    # versão do teste devia escrever.
    ajuste = _ajuste_da_variante(variante.get("config") or {})
    instrucao = (f"{FORMATOS[canal]}\n\n{PAPEIS[etapa]}\n{REGRAS}{proibidos}{ajuste}\n"
                 f"Escreva a mensagem. Responda só com ela.")
    # A VOZ manda sempre abrir com um dado concreto do cliente. Isso está certo
    # para vender e errado para encerrar: com a VOZ crua, a despedida saía como
    # pitch completo, com posição, perda e CTA. Aqui a regra é suspensa.
    sistema = None
    if etapa == "dia12_ultima":
        sistema = copy_ai.VOZ + (
            "\n\nEXCEÇÃO PARA ESTA MENSAGEM (vale mais que a regra 1): esta é uma "
            "despedida, NÃO uma venda. É PROIBIDO abrir com dado do cliente e "
            "PROIBIDO citar posição, nota, avaliações, concorrente, número de "
            "pacientes ou valor em R$. Nada de chamada para ação. Duas linhas: "
            "você para de escrever e deixa a porta aberta.")
    r = copy_ai.escrever(instrucao, _contexto(lead, etapa), max_tokens=420,
                         sistema=sistema)
    texto = (r.get("texto") or "").strip()
    if not texto:
        return {"ok": True, "canal": canal, "etapa": etapa,
                "fonte": "determinístico (IA falhou)", **marca, **base}

    saida = (_separar_assunto(texto) if canal == "email"
             else {"assunto": "", "texto": _limpar_formatacao(texto)})
    if canal == "email" and not saida["assunto"]:
        saida["assunto"] = base.get("assunto", "")

    # regras finas: pede UMA reescrita antes de aceitar
    finas = violacoes_finas(saida["texto"], etapa, lead)
    if finas:
        correcao = ("\n\nAJUSTE: sua versão " + ", ".join(finas) +
                    ". Reescreva corrigindo, mantendo todos os números e fatos.")
        r2 = copy_ai.escrever(instrucao + correcao, _contexto(lead, etapa),
                              max_tokens=420, sistema=sistema)
        t2 = (r2.get("texto") or "").strip()
        if t2 and not violacoes(t2, etapa, lead):
            nova = (_separar_assunto(t2) if canal == "email"
                    else {"assunto": "", "texto": _limpar_formatacao(t2)})
            if canal == "email" and not nova["assunto"]:
                nova["assunto"] = saida.get("assunto") or base.get("assunto", "")
            # só troca se a reescrita realmente resolveu
            if len(violacoes_finas(nova["texto"], etapa, lead)) < len(finas):
                saida, finas = nova, violacoes_finas(nova["texto"], etapa, lead)

    # Insistiu no erro depois de avisado: cai no texto fixo. O determinístico foi
    # escrito como referência do que é certo, então é melhor que uma versão da IA
    # que já provou não obedecer, e ainda tende a inventar de quebra.
    if finas:
        return {"ok": True, "canal": canal, "etapa": etapa,
                "fonte": f"determinístico (IA insistiu: {', '.join(finas)})", **marca, **base}

    # A variante precisa estar VISÍVEL no texto. Se o modelo ignorou, o texto
    # fixo assume, porque ele aplica a variante por construção. Sem isso as duas
    # versões do experimento saem iguais e a comparação vira ruído.
    if not variante_aplicada(saida["texto"], variante.get("config")):
        return {"ok": True, "canal": canal, "etapa": etapa, **marca,
                "fonte": "determinístico (IA ignorou a variante)", **base}

    # trava final: violou proibição dura, usa o texto fixo em vez de arriscar
    quebras = violacoes(saida["texto"], etapa, lead)
    if quebras:
        return {"ok": True, "canal": canal, "etapa": etapa,
                "fonte": f"determinístico (IA violou: {', '.join(quebras)})", **marca, **base}

    return {"ok": True, "canal": canal, "etapa": etapa, "fonte": "IA", **marca,
            "qualidade": {"cheiro_ia": r.get("pontos"), "achados": r.get("achados"),
                          "regras_finas_pendentes": finas},
            **saida}


def cadencia(lead_id: int, usar_ia: bool = True) -> list:
    """A sequência inteira, na ordem de config.CADENCIA."""
    saida, assuntos = [], []
    for dia, canal, etapa in config.CADENCIA:
        m = gerar(lead_id, canal, etapa, usar_ia=usar_ia,
                  assuntos_anteriores=list(assuntos))
        if m.get("assunto"):
            assuntos.append(m["assunto"])
        saida.append({"dia": dia, **m})
    return saida


if __name__ == "__main__":
    import sys
    lid = int(sys.argv[1]) if len(sys.argv) > 1 else 633
    sem_ia = "--sem-ia" in sys.argv
    for m in cadencia(lid, usar_ia=not sem_ia):
        print("=" * 72)
        print(f"  DIA {m['dia']}  ·  {m['canal'].upper()}  ·  {m['etapa']}  "
              f"[{m.get('fonte')}]")
        print("=" * 72)
        if m.get("assunto"):
            print(f"Assunto: {m['assunto']}\n")
        print(m.get("texto") or m.get("erro"))
        print()
