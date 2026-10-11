"""copy_ai.py — Regras de copy para a IA não escrever "texto genérico de IA"."""

import re

import ia_hook

# ── Clichês que denunciam IA (peso: quanto pesa no "cheiro") ─────────────────
BANIDAS = {
    # aberturas e conectivos batidos
    "sabia que": 3, "que tal": 3, "imagine só": 3, "vamos juntos": 3,
    "não perca mais tempo": 4, "o que você acha": 2, "fica a dica": 3,
    "espero que esteja bem": 2, "venho por meio desta": 3,
    # vocabulário de marketing vazio
    "alavancar": 3, "potencializar": 3, "revolucionar": 3, "transformar sua": 2,
    "elevar o patamar": 4, "de forma estratégica": 2, "soluções personalizadas": 3,
    "excelência": 2, "diferencial competitivo": 3, "resultados extraordinários": 4,
    "no mundo digital": 3, "na era digital": 4, "cenário atual": 3,
    "parceria de sucesso": 3, "juntos podemos": 3, "todo o cuidado que você merece": 4,
    "conte com a gente": 2,
    # estruturas de IA
    "primeiramente": 2, "em suma": 3, "vale ressaltar": 3, "é importante destacar": 3,
    "não é mesmo": 3, "afinal": 1, "portanto": 1,
    # promessas vagas
    "muito mais": 2, "diversos benefícios": 3, "inúmeras vantagens": 3,
    # vícios de abordagem fria: denunciam mensagem em massa ou são gatilho de
    # spam. "sem compromisso" é o pior deles, porque planta na cabeça de quem lê
    # justamente a ideia de compromisso que tenta afastar.
    "sem compromisso": 5, "passando aqui": 5, "rapidinho": 4,
    "espero que esteja bem": 4, "venho oferecer": 4, "gostaria de apresentar": 4,
    "oportunidade única": 5, "última chance": 5, "aproveite": 3,
    "dei uma olhada na sua presença digital": 4, "não vou tomar seu tempo": 3,
    "só um minutinho": 4, "posso te fazer uma pergunta": 3,
    # diminutivo de vendedor: encolhe o pedido no papel e soa condescendente
    "minutinho": 3, "minutinhos": 3,
    # formalidade de e-mail corporativo, que num contato frio soa a circular
    "espero que este e-mail te encontre": 5, "prezado": 4, "prezada": 4,
    "segue em anexo": 2, "conforme conversamos": 2, "desde já agradeço": 3,
    "fico no aguardo": 3, "sem mais": 3, "estou à disposição": 3,
    "reforço meu contato": 3, "reitero": 3, "cabe destacar": 3,
    # abertura de vendedor
    "quero te apresentar": 4, "posso te ajudar": 3, "presta um serviço": 3,
    "tudo bem por aí": 4, "meu nome é": 2, "trabalho com": 2,
    # exagero de entusiasmo e gíria de vendedor
    "fantástico": 3, "incrível": 3, "top": 2, "fechou": 1,
}
# NÃO adicionado de propósito, apesar de sugerido: "como vai".

# ── A voz (system prompt mestre) ─────────────────────────────────────────────
VOZ = """Você escreve copy de prospecção B2B para clínicas de saúde no Brasil.
Escreve como um consultor experiente que já analisou o negócio do cliente — não como
um robô de marketing.

COMO ESCREVER:
1. Comece pelo FATO, não pela saudação. A primeira linha precisa conter um dado concreto
   do cliente (posição no Google, nota, valor em R$). Nada de "Sabia que...", "Que tal...".
2. Ritmo: alterne frases curtas e longas. Uma frase de 3 palavras depois de uma longa
   prende a leitura. Nunca 3 frases seguidas do mesmo tamanho.
3. Concreto > abstrato. "12 pacientes por mês" em vez de "muitos pacientes".
   "R$ 9.600" em vez de "um valor considerável".
4. Uma ideia por parágrafo. Parágrafos de 1 a 3 linhas.
5. Zero hype. Sem superlativo, sem exclamação (no máximo uma no texto inteiro),
   sem emoji a não ser que seja pedido.
6. Antecipe a objeção em vez de ignorá-la. Se o dado for ruim, diga com naturalidade.
7. CTA de baixo atrito: peça algo pequeno (responder, ver, 10 minutos) — nunca "compre agora".
8. Escreva na segunda pessoa (você/sua clínica). Sem "nós somos especialistas em...".
9. NUNCA INVENTE NADA. Use apenas os números e fatos que estão nos dados fornecidos.
   É PROIBIDO: criar estatísticas ("8 em cada 10", "96% dos pacientes"), inventar cidade
   ou hashtag de localidade, prometer preço/desconto/consulta grátis/cortesia, inventar
   nome de pessoa, serviço ou prazo. Se o dado não foi dado, escreva sem ele.
   Nunca use placeholders como [Nome].
10. Não mude a CAUSA do problema. Se a perda é por não ser encontrado no Google, não
   escreva que é por falta ou desistência de paciente.
11. Não REINTERPRETE um número. "37 avaliações" não vira "37 pacientes atendidos" nem
   "37 clientes satisfeitos". Use cada número exatamente com o significado do campo.
12. QUEM É QUEM: telefone, endereço e Instagram nos dados são DA CLÍNICA QUE VAI RECEBER
   a mensagem, são dados dela, não seus. Nunca os use como assinatura ou contato seu,
   e nunca assine a mensagem com dados de contato (não temos esses dados).
13. PONTUAÇÃO: é PROIBIDO usar travessão longo (— ou –). Use ponto final, vírgula,
   dois-pontos ou parênteses. Prefira quebrar em duas frases.
14. LOOP ABERTO: quando a mensagem terminar com pergunta, ela fica em aberto de
   propósito. Não responda a própria pergunta nem entregue a solução na mesma
   mensagem. O que fica incompleto é o que traz a resposta.
15. UMA IDEIA, UMA MENSAGEM: se a mensagem tem dois pedidos ou dois assuntos,
   corte um. Mensagem com foco único é respondida; mensagem com dois pedidos é
   ignorada.
16. PERGUNTA CALIBRADA: na ausência de instrução em contrário, prefira perguntas
   que começam com "como" ou "o que" a perguntas de sim/não. "Como vocês fazem
   isso hoje?" abre conversa. Mas esta é uma PREFERÊNCIA, não uma proibição: se
   a instrução do toque pedir pergunta de sim/não, obedeça a ela. Uma pergunta
   fechada e fácil ("isso já está no radar de vocês?") também é boa abertura.
   Esta regra vale menos que a instrução específica da mensagem.

PROIBIDO ESCREVER (soa a robô): "sabia que", "que tal", "não perca mais tempo",
"alavancar", "potencializar", "revolucionar", "na era digital", "no mundo digital",
"soluções personalizadas", "excelência", "diferencial competitivo", "vale ressaltar",
"é importante destacar", "em suma", "primeiramente", "juntos podemos",
"todo o cuidado que você merece".

SOBRE OS DADOS QUE VOCÊ RECEBE:
Qualquer campo com "NÃO INFORMADO" significa que esse dado NÃO EXISTE para você.
É proibido preencher, supor ou aproximar esse campo. Simplesmente escreva o texto
sem mencioná-lo. Exemplos do que isso proíbe: se "endereco_cidade" está NÃO INFORMADO,
não cite nenhuma cidade nem hashtag de cidade; se "telefone" está NÃO INFORMADO, não
escreva telefone nenhum (nem inventado, nem no formato (XX) XXXX); se
"horario_funcionamento" está NÃO INFORMADO, não diga que atende sábado, nem até que
horas; se "precos_e_promocoes" está NÃO INFORMADO, não prometa desconto, consulta
grátis ou cortesia.

Responda SOMENTE com o texto final, sem comentários, sem títulos como "Versão 1"."""


# ── Padrões de ALUCINAÇÃO (inventar fato) ───────────────────────────────────
_ALUC = [
    # estatística inventada
    (re.compile(r"\b\d{1,3}\s*%\s*(d[oa]s|de cada|dos pacientes)", re.I), 6, "estatística inventada (%)"),
    (re.compile(r"\b\d+\s+em\s+cada\s+\d+", re.I), 6, "estatística inventada (X em cada Y)"),
    # oferta comercial que a clínica não autorizou
    (re.compile(r"\b(gr[áa]tis|gratuit[ao]|sem cobran[çc]a|desconto|promo[çc][ãa]o|"
                r"cortesia|isen[çc][ãa]o|primeira consulta gr)", re.I), 8, "oferta/preço inventado"),
    # horário de funcionamento / dia de atendimento inventado
    (re.compile(r"\b(aberto|atende|atendemos|funcionamos|plant[ãa]o)\b[^.\n]{0,40}"
                r"(至|até|das|aos|sábado|domingo|\d{1,2}h)", re.I), 6, "horário/dia inventado"),
    # placeholder de qualquer tipo: [Nome], (XX) XXXX, xxx
    (re.compile(r"\[[^\]]{2,20}\]"), 8, "placeholder [ ]"),
    (re.compile(r"\(?\s*[Xx]{2}\s*\)?[\s\-]*[Xx]{4,}"), 8, "placeholder de telefone (XX)"),
    (re.compile(r"\b(seu nome|nome da clínica|inserir aqui|link na bio da)\b", re.I), 5, "placeholder textual"),
]

# siglas/nomes de cidade que a IA costuma inventar em hashtag
_CIDADES = {"saopaulo", "riodejaneiro", "belohorizonte", "brasilia", "curitiba", "portoalegre",
            "salvador", "recife", "fortaleza", "goiania", "campinas", "bh", "sp", "rj", "poa",
            "bsb", "cwb", "ssa", "for", "rec"}


def degenerado(texto: str, contexto: dict | None = None) -> list:
    """A geração desandou e o texto não é uma mensagem."""
    t = texto or ""
    achados = []
    if not t.strip():
        return achados

    # Chave interna do contexto aparecendo no texto do cliente. Só as chaves em
    # snake_case contam: "clinica", "site" e "instagram" também são chaves, mas
    # são palavras normais e apareciam em toda mensagem legítima.
    vazadas = [k for k in (contexto or {}) if "_" in k and k in t]
    if vazadas:
        achados.append("vazou chave do contexto: " + ", ".join(sorted(vazadas)[:3]))

    # sintaxe de estrutura de dados no meio da mensagem
    if re.search(r"['\"]\s*:\s*['\"\[{]", t) or re.search(r"\}\s*[,，]\s*['\"]", t):
        achados.append("sintaxe de JSON no texto")

    # pontuação de largura dupla / árabe: sinal clássico de geração desandando
    if re.search(r"[，、；：？！،؛]", t):
        achados.append("pontuação fora do português")

    # repetição literal de trecho longo
    trechos = {}
    limpo = re.sub(r"\s+", " ", t)
    for i in range(0, max(0, len(limpo) - 40), 10):
        pedaco = limpo[i:i + 40]
        trechos[pedaco] = trechos.get(pedaco, 0) + 1
    if trechos and max(trechos.values()) >= 3:
        achados.append("trecho repetido três vezes ou mais")

    return achados


def alucinacoes(texto: str, contexto: dict | None = None) -> tuple[int, list]:
    """Detecta fatos provavelmente inventados. Retorna."""
    pontos, achados = 0, []
    t = texto or ""
    for rx, peso, rotulo in _ALUC:
        if rx.search(t):
            pontos += peso
            achados.append(rotulo)
    # hashtag/menção de cidade que não está no contexto (inclui siglas: #bh, #sp)
    ctx = " ".join(str(v).lower() for v in (contexto or {}).values() if v).replace(" ", "")
    for tag in re.findall(r"#(\w{2,})", t):
        low = tag.lower()
        if low in _CIDADES and low not in ctx:
            pontos += 6
            achados.append(f"cidade inventada (#{tag})")
    return pontos, achados


def limpar_travessao(texto: str) -> str:
    """
    Remove travessões longos (— –) do texto final, trocando por pontuação normal. " —
    Palavra" -> ". Palavra" · " — palavra" -> ", palavra"
    """
    if not texto:
        return texto
    t = texto.replace("—", "—").replace("–", "–")
    # travessão cercado de espaços
    t = re.sub(r"\s+[—–]\s+([A-ZÁÂÃÀÉÊÍÓÔÕÚÇ])", r". \1", t)
    t = re.sub(r"\s+[—–]\s+", ", ", t)
    # travessão grudado ou sobrando
    t = re.sub(r"[—–]", "-", t)
    # arruma pontuação duplicada gerada pela troca
    t = re.sub(r",\s*,", ",", t)
    t = re.sub(r"\.\s*\.", ".", t)
    t = re.sub(r",\s*\.", ".", t)
    return t


# Cada termo banido vira regex com fronteira de palavra.
_BANIDAS_RX = {
    termo: re.compile(r"(?<![0-9a-zà-ÿ])" + re.escape(termo) + r"(?![0-9a-zà-ÿ])", re.I)
    for termo in BANIDAS
}


def cheiro_de_ia(texto: str) -> tuple[int, list]:
    """Pontua o quão 'genérico de IA' o texto está. 0 = limpo. Retorna."""
    t = (texto or "").lower()
    pontos, achados = 0, []
    for termo, peso in BANIDAS.items():
        if _BANIDAS_RX[termo].search(t):
            pontos += peso
            achados.append(termo)
    # excesso de exclamação
    exc = texto.count("!")
    if exc > 1:
        pontos += (exc - 1)
        achados.append(f"{exc} exclamações")
    # enxurrada de emoji
    emojis = len(re.findall(r"[\U0001F300-\U0001FAFF☀-➿]", texto or ""))
    if emojis > 3:
        pontos += (emojis - 3)
        achados.append(f"{emojis} emojis")
    # frases todas do mesmo tamanho (ritmo robótico)
    frases = [f.strip() for f in re.split(r"[.!?\n]+", texto or "") if len(f.strip()) > 15]
    if len(frases) >= 4:
        tam = [len(f.split()) for f in frases]
        media = sum(tam) / len(tam)
        variacao = sum(abs(x - media) for x in tam) / len(tam)
        if variacao < 3:
            pontos += 3
            achados.append("ritmo monótono (frases do mesmo tamanho)")
    return pontos, achados


def escrever(instrucao: str, contexto: dict, max_tokens: int = 700,
             tentativas: int = 2, limite: int = 4, sistema: str | None = None) -> dict:
    """
    Gera o texto com a VOZ e refaz se o 'cheiro de IA' passar do limite. Retorna {texto,
    pontos, achados, tentativas}.
    """
    if not ia_hook.IA_ATIVA:
        return {"texto": "", "pontos": 0, "achados": ["IA não configurada"], "tentativas": 0}

    melhor = {"texto": "", "pontos": 999, "achados": []}
    msg_extra = ""
    for i in range(1, tentativas + 1):
        texto = ia_hook.gerar_conteudo(instrucao + msg_extra, contexto,
                                       sistema=(sistema or VOZ), max_tokens=max_tokens)
        if not texto:
            break
        texto = limpar_travessao(texto)   # trava determinística: sem travessão longo
        # geração desandada não compete por pontuação: é descartada de vez, senão
        # ela poderia ser a "melhor" tentativa só porque as outras pontuaram pior
        desandou = degenerado(texto, contexto)
        if desandou:
            msg_extra = ("\n\nATENÇÃO: sua resposta anterior não era uma mensagem "
                         f"({', '.join(desandou)}). Responda SÓ com o texto da "
                         "mensagem, em português, sem repetir o contexto.")
            continue
        p1, a1 = cheiro_de_ia(texto)
        p2, a2 = alucinacoes(texto, contexto)   # inventar fato pesa mais
        pontos, achados = p1 + p2, a1 + a2
        if pontos < melhor["pontos"]:
            melhor = {"texto": texto, "pontos": pontos, "achados": achados}
        if pontos <= limite:
            return {**melhor, "tentativas": i}
        aviso = ""
        if a2:
            aviso = (f" Você INVENTOU dados ({', '.join(a2)}) — isso é proibido: use só os "
                     "números e fatos fornecidos, e remova qualquer oferta, estatística, "
                     "cidade ou prazo que não esteja nos dados.")
        msg_extra = ("\n\nATENÇÃO: sua versão anterior soou genérica ou inventou coisas "
                     f"({', '.join(achados[:5])}). Reescreva corrigindo isso." + aviso)
    return {**melhor, "tentativas": tentativas}


def refinar(texto_base: str, contexto: dict, instrucao: str,
            tentativas: int = 2, limite: int = 4) -> str:
    """
    Refina um texto determinístico mantendo os fatos, com o mesmo controle de qualidade.
    """
    if not ia_hook.IA_ATIVA or not texto_base:
        return texto_base
    pedido = (f"{instrucao}\n\nReescreva o texto abaixo seguindo as regras. "
              f"MANTENHA todos os números e fatos exatamente como estão.\n\n"
              f"--- TEXTO BASE ---\n{texto_base}")
    r = escrever(pedido, contexto, max_tokens=1100, tentativas=tentativas, limite=limite)
    return r["texto"] or texto_base


if __name__ == "__main__":
    ruim = ("Sabia que sua clínica pode alavancar resultados no mundo digital? "
            "Que tal transformar sua presença? Não perca mais tempo! "
            "Oferecemos soluções personalizadas com excelência!!! 🚀🔥💎✨")
    print("Texto ruim ->", cheiro_de_ia(ruim))
    bom = ("Sua clínica aparece em 65º lugar quando alguém procura dentista na sua região. "
           "Quem está fora dos três primeiros quase não recebe clique. "
           "Isso são cerca de 12 pacientes por mês indo para o concorrente ao lado.")
    print("Texto bom  ->", cheiro_de_ia(bom))
