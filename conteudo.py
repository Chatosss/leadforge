"""conteudo.py, Conteúdo por IA para um lead (post, e-mail, WhatsApp, DM, follow-up)."""

import copy_ai
import db
import diagnostico as diag_mod
import ia_hook

TIPOS = {
    "whatsapp": (
        "Escreva UMA mensagem de WhatsApp para o dono da clínica (primeiro contato frio).\n"
        "FORMATO: no máximo 4 linhas curtas. Sem saudação formal. Sem emoji.\n"
        "Abra citando a posição no Google dela. Cite 1 problema concreto e o valor perdido "
        "por mês. Feche perguntando se pode mandar o diagnóstico (resposta sim/não)."),
    "email": (
        "Escreva um e-mail de prospecção frio.\n"
        "FORMATO: primeira linha 'Assunto: ...' (máx. 8 palavras, sem hype, cite um número). "
        "Corpo com no máximo 120 palavras, em 3 parágrafos curtos. Sem assinatura inventada, "
        "termine com a pergunta.\n"
        "Abra com o dado da posição no Google. Depois o custo em R$. Feche pedindo 10 minutos."),
    "dm": (
        "Escreva uma DM de Instagram para a clínica (primeiro contato).\n"
        "FORMATO: 3 linhas no máximo, tom de mensagem de rede social, no máximo 1 emoji.\n"
        "Cite um ponto concreto do perfil dela no Google e ofereça o diagnóstico."),
    "followup": (
        "Escreva um follow-up curto para quem recebeu a mensagem e não respondeu.\n"
        "FORMATO: 3 linhas. Sem cobrar. Traga um ângulo NOVO (o concorrente, ou a perda "
        "acumulada) em vez de repetir a primeira mensagem. Feche com saída fácil "
        "('se não for prioridade agora, sem problema')."),
    "post": (
        "Escreva um post para o Instagram DA PRÓPRIA CLÍNICA (conteúdo que ela publicaria "
        "para atrair pacientes, não é venda de serviço para ela).\n"
        "FORMATO: gancho na primeira linha, 3 a 5 linhas de corpo, chamada para agendar, "
        "3 a 4 hashtags. No máximo 2 emojis no total.\n"
        "Tema: um problema real que o paciente dela tem. Nada de autoelogio da clínica.\n"
        "RESTRIÇÕES: não cite horário de funcionamento, dia de atendimento, preço, promoção "
        "nem telefone (a menos que o telefone esteja nos dados). Nas hashtags use apenas o "
        "tema e o nome da clínica, nunca cidade/sigla de cidade. CTA genérica: 'chama no "
        "direct' ou 'agende sua avaliação'."),
}


_NAO_INFORMADO = "NÃO INFORMADO, não invente, escreva sem este dado"


def _contexto(lead) -> dict:
    """Monta o contexto COMPLETO para a IA."""
    problemas = [p["titulo"] for p in diag_mod.detectar_problemas(lead)[:3]]
    perdidos, roi, ticket = diag_mod.estimar_roi(lead)
    conc = diag_mod.montar_concorrente(lead)

    # nº de avaliações: descarta o valor se for igual ao DDD (bug de captura antiga)
    import whatsapp_check
    aval = lead.get("total_avaliacoes_num")
    tel_n = whatsapp_check.normalizar(lead.get("telefone") or "")
    if aval and tel_n and str(aval) == tel_n["ddd"]:
        aval = None

    _AUSENTE = object()  # sentinela: distingue "não passei valor" de "valor é None"

    def v(campo, valor=_AUSENTE):
        val = lead.get(campo) if valor is _AUSENTE else valor
        return val if (val not in (None, "", 0)) else _NAO_INFORMADO

    return {
        # ATENÇÃO: estes dados são DA CLÍNICA DESTINATÁRIA (o prospect), não nossos.
        "clinica": lead["nome"],
        "categoria_servico": v("categoria"),
        "nicho": v("nicho"),
        "endereco_da_clinica_destinataria": v("endereco"),
        "telefone_da_clinica_destinataria": v("telefone"),
        "instagram": v("instagram"),
        "site": v("website"),
        "posicao_google": v("posicao"),
        "nota_google": v("avaliacao_num"),
        "total_avaliacoes": v("total_avaliacoes_num", aval),
        "horario_funcionamento": _NAO_INFORMADO,   # nunca coletamos, trava explícita
        "precos_e_promocoes": _NAO_INFORMADO,      # idem
        "problemas_encontrados": problemas,
        "pacientes_perdidos_mes": perdidos,
        "perda_mes_reais": roi,
        "concorrente_referencia": conc["nome"] if conc else _NAO_INFORMADO,
    }


def _contexto_publico(lead) -> dict:
    """
    Contexto para conteúdo que a PRÓPRIA CLÍNICA publica (post). NÃO inclui dados do
    diagnóstico (posição, pacientes perdidos, R$, concorrente) - isso é informação
    interna da prospecção; a clínica nunca publicaria isso.
    """
    ctx = _contexto(lead)
    for interno in ("posicao_google", "problemas_encontrados", "pacientes_perdidos_mes",
                    "perda_mes_reais", "concorrente_referencia", "site"):
        ctx.pop(interno, None)
    ctx["publico_alvo"] = "pacientes da clínica (não é para o dono da clínica)"
    return ctx


def gerar(lead_id: int, tipo: str = "whatsapp") -> dict:
    lead = db.get_lead(lead_id)
    if not lead:
        return {"ok": False, "erro": "lead não encontrado"}
    if not ia_hook.IA_ATIVA:
        return {"ok": False, "erro": "IA não configurada, preencha a chave no .env"}

    instrucao = TIPOS.get(tipo, TIPOS["whatsapp"])
    # post = conteúdo público da clínica -> sem os dados internos de prospecção
    ctx = _contexto_publico(lead) if tipo == "post" else _contexto(lead)
    r = copy_ai.escrever(instrucao, ctx, max_tokens=650)
    return {"ok": bool(r["texto"]), "tipo": tipo, "texto": r["texto"],
            "qualidade": {"cheiro_ia": r["pontos"], "achados": r["achados"],
                          "tentativas": r["tentativas"]}}


if __name__ == "__main__":
    import sys, json
    lid = int(sys.argv[1]) if len(sys.argv) > 1 else 547
    tipo = sys.argv[2] if len(sys.argv) > 2 else "whatsapp"
    r = gerar(lid, tipo)
    print(f"--- {tipo.upper()} (cheiro de IA: {r.get('qualidade',{}).get('cheiro_ia')}) ---")
    print(r.get("texto") or r.get("erro"))
