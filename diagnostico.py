"""diagnostico.py, Módulo B (Diagnosticar)."""

from typing import Dict, List, Optional, Tuple

import config
import db
import ia_hook
import scorer


# Cada problema aponta para o nível de solução (usado pela proposta / Módulo C).
# nivel: 1=Presença  2=Conversão  3=Domínio
PROBLEMA_META = {
    "posicao":       {"comp": "posicao",       "nivel": 1},
    "sem_site":      {"comp": "sem_site",       "nivel": 1},
    "sem_instagram": {"comp": "sem_instagram",  "nivel": 2},
    "nota_baixa":    {"comp": "nota_baixa",     "nivel": 1},
}


def detectar_problemas(lead: Dict) -> List[Dict]:
    """Retorna os problemas aplicáveis, ordenados por peso no score (maior 1º)."""
    _, breakdown = scorer.score_lead(lead)
    comp = breakdown["componentes"]
    nota = lead.get("avaliacao_num")
    posicao = lead.get("posicao")
    categoria = lead.get("categoria") or "estabelecimentos"

    candidatos = []

    if posicao and posicao > 3:
        candidatos.append({
            "chave": "posicao", "nivel": 1,
            "titulo": f"{posicao}ª posição no Google Maps",
            "texto": (f"Seu estabelecimento aparece só na {posicao}ª posição quando alguém "
                      f"busca por {categoria.lower()} na região. A maioria dos pacientes "
                      f"escolhe entre os 3 primeiros, quem está embaixo é praticamente invisível."),
            "pontos": comp["posicao"]["pontos"],
        })

    if nota is not None and nota < config.NOTA_ALVO:
        candidatos.append({
            "chave": "nota_baixa", "nivel": 1,
            "titulo": f"Nota {nota:.1f} no Google (abaixo de {config.NOTA_ALVO})",
            "texto": (f"A nota {nota:.1f} está abaixo do patamar de confiança ({config.NOTA_ALVO}). "
                      f"Sem uma gestão ativa de avaliações, cada estrela a menos faz o paciente "
                      f"escolher o concorrente ao lado."),
            "pontos": comp["nota_baixa"]["pontos"],
        })

    if not lead.get("tem_site"):
        candidatos.append({
            "chave": "sem_site", "nivel": 1,
            "titulo": "Site não aparece no seu perfil do Google",
            "texto": ("Seu site não está vinculado ao seu perfil no Google Maps. Quem te encontra "
                      "ali não vê o botão para o site, não conhece seus serviços, fotos e o "
                      "agendamento, e acaba clicando em quem já mostra tudo isso no Google."),
            "pontos": comp["sem_site"]["pontos"],
        })

    if not lead.get("tem_instagram"):
        candidatos.append({
            "chave": "sem_instagram", "nivel": 2,
            "titulo": "Instagram não conectado ao seu Google",
            "texto": ("Seu Instagram não aparece vinculado ao seu perfil no Google. Quem te "
                      "descobre no Maps não é levado até suas redes, você perde o vínculo que "
                      "gera seguidores, prova social e mensagens diretas de novos pacientes."),
            "pontos": comp["sem_instagram"]["pontos"],
        })

    candidatos.sort(key=lambda p: p["pontos"], reverse=True)
    return candidatos


def estimar_roi(lead: Dict) -> Tuple[int, int, int]:
    """Retorna (pacientes_perdidos_mes, roi_reais_mes, ticket)."""
    return perda_apurada(lead)[:3]


def perda_apurada(lead: Dict) -> tuple:
    """(pacientes, roi, ticket, apurada)."""
    if getattr(config, "USAR_ESTIMATIVA_CTR", False):
        try:
            import estimativa
            r = estimativa.estimar(lead)
            if r["pacientes_mes"] > 0:
                return r["pacientes_mes"], r["valor_mes"], r["ticket"], True
            # zero apurado: não há perda por posição a cobrar deste lead
            return 0, 0, r["ticket"], False
        except Exception as e:
            print(f"[diagnostico] estimativa por CTR indisponível ({e}); usando a simples.")

    nicho = lead.get("nicho", config.NICHO_PADRAO)
    ticket = config.TICKET_POR_NICHO.get(nicho, config.TICKET_POR_NICHO["padrao"])

    posicao = lead.get("posicao") or 1
    pos_frac = scorer._frac_posicao(posicao)

    perdidos = config.PACIENTES_PERDIDOS_MIN + pos_frac * (
        config.PACIENTES_PERDIDOS_MAX - config.PACIENTES_PERDIDOS_MIN
    )
    # canais de captação ausentes ampliam a perda (até +40%)
    fator_canais = 1.0 + 0.2 * (0 if lead.get("tem_site") else 1) \
                       + 0.2 * (0 if lead.get("tem_instagram") else 1)
    perdidos = min(config.PACIENTES_PERDIDOS_MAX, perdidos * fator_canais)
    perdidos = max(config.PACIENTES_PERDIDOS_MIN, round(perdidos))

    roi = int(perdidos) * ticket
    # heurística é estimativa de trabalho, não conta defensável numa reunião
    return int(perdidos), roi, ticket, False


def _eixo_da_comparacao(lead: Dict, c: Dict) -> Optional[str]:
    """Em QUE a referência é comprovadamente melhor que o lead."""
    pos_l, pos_c = lead.get("posicao") or 999, c.get("posicao") or 999
    if pos_c < pos_l:
        return "posicao"
    aval_l = lead.get("total_avaliacoes_num") or 0
    aval_c = c.get("total_avaliacoes_num") or 0
    # dobro de avaliações é diferença que o operador enxerga sozinho no perfil
    if aval_c >= max(5, aval_l * 2):
        return "reputacao"
    nota_l, nota_c = lead.get("avaliacao_num"), c.get("avaliacao_num")
    if nota_c is not None and nota_l is not None and nota_c - nota_l >= 0.3:
        return "reputacao"
    falta = ((c.get("tem_site") and not lead.get("tem_site")) or
             (c.get("tem_instagram") and not lead.get("tem_instagram")))
    return "presenca" if falta else None


def montar_concorrente(lead: Dict) -> Optional[Dict]:
    """A clínica de referência da região e o EIXO em que ela é, de fato, melhor."""
    # pool grande de propósito: com limit=2 quase nunca sobrava alguém à frente,
    # que era o que empurrava a função para o fallback errado
    cands = db.leaders_by_nicho(lead["nicho"], lead["id"], limit=12)
    if not cands:
        return None

    pos_l = lead.get("posicao") or 999
    a_frente = [c for c in cands if (c.get("posicao") or 999) < pos_l]
    if a_frente:
        c, eixo = a_frente[0], "posicao"
    else:
        c = cands[0]
        eixo = _eixo_da_comparacao(lead, c)
        if not eixo:
            return None

    nota_c = c.get("avaliacao_num")
    aval_c = c.get("total_avaliacoes_num")
    tem = [t for t, k in (("site", "tem_site"), ("Instagram", "tem_instagram"))
           if c.get(k)]

    if eixo == "posicao":
        # Só entra na lista o que a referência de fato faz MELHOR.
        nota_l = lead.get("avaliacao_num")
        aval_l = lead.get("total_avaliacoes_num") or 0
        partes = [f"aparece em {c['posicao']}º lugar"]
        if nota_c is not None and (nota_l is None or nota_c > nota_l):
            partes.append(f"tem nota {nota_c:.1f}")
        if aval_c and aval_c > aval_l:
            partes.append(f"{aval_c} avaliações")
        # presença só conta quando é ela que tem e o lead não
        falta_no_lead = [t for t, k in (("site", "tem_site"), ("Instagram", "tem_instagram"))
                         if c.get(k) and not lead.get(k)]
        if falta_no_lead:
            partes.append("mostra " + " e ".join(falta_no_lead) + " direto no Google")
        texto = (f"Enquanto a {c['nome']} " + ", ".join(partes) + ", "
                 f"o seu perfil ainda não aparece assim. É exatamente essa diferença "
                 f"que faz o paciente escolher ela, e não você.")
    elif eixo == "reputacao":
        prova = []
        if aval_c:
            prova.append(f"{aval_c} avaliações")
        if nota_c is not None:
            prova.append(f"nota {nota_c:.1f}")
        aval_l = lead.get("total_avaliacoes_num") or 0
        # POR QUE ela ganha paciente aparecendo depois.
        if aval_l:
            comparativo = f", contra {aval_l} da sua"
        elif lead.get("avaliacao_num") is not None:
            comparativo = ""          # contagem desconhecida: não comparar volume
        else:
            comparativo = ", enquanto o seu perfil quase não tem"
        texto = (f"A {c['nome']} aparece DEPOIS da sua clínica na busca e mesmo "
                 f"assim tem " + " e ".join(prova) + comparativo + ". "
                 f"Posição decide quem é VISTO; volume de avaliação decide quem é "
                 f"ESCOLHIDO. O paciente abre os dois perfis, compara, e fecha com "
                 f"quem tem mais gente confirmando o trabalho, mesmo que precise "
                 f"rolar um pouco mais para achar. É por isso que ela atende mais "
                 f"aparecendo abaixo de você.")
    else:
        texto = (f"A {c['nome']} mostra " + " e ".join(tem) + " direto no Google, "
                 f"e o seu perfil ainda não. Não é questão de posição, é o que o "
                 f"paciente consegue conferir antes de decidir.")

    # posição e volume vão junto: sem eles, quem monta copy com IA não tem como
    # dizer se a referência está à frente ou atrás, e o modelo preenche sozinho.
    return {"id": c["id"], "nome": c["nome"], "nota": nota_c, "texto": texto,
            "posicao": c.get("posicao"),
            "avaliacoes": aval_c,
            "eixo": eixo,
            "a_frente": eixo == "posicao"}


def _br(n) -> str:
    """Formata inteiro no padrão BR de milhar: 9600 -> '9.600'."""
    return f"{int(n):,}".replace(",", ".")


def montar_texto(lead: Dict, problemas: List[Dict], perdidos: int, roi: int,
                 ticket: int, concorrente: Optional[Dict]) -> str:
    nome = lead.get("nome", "sua clínica")
    linhas = [
        f"Olá! Fiz um raio-x rápido da presença digital da {nome} e encontrei "
        f"{len(problemas)} pontos que provavelmente estão custando pacientes:",
        "",
    ]
    for i, p in enumerate(problemas, 1):
        linhas.append(f"{i}. {p['titulo']}, {p['texto']}")
    linhas += [
        "",
        f"📉 Impacto estimado: cerca de {perdidos} novos pacientes por mês escapando. "
        f"A um ticket médio de R$ {_br(ticket)} cada, são aproximadamente "
        f"R$ {_br(roi)}/mês deixados na mesa.",
    ]
    if concorrente:
        linhas += ["", f"🔍 {concorrente['texto']}"]
    linhas += [
        "",
        "A boa notícia: os 3 pontos acima são resolvíveis, e é exatamente isso que a "
        "gente faz. Posso te mostrar como?",
    ]
    return "\n".join(linhas)


def gerar_diagnostico(lead: Dict) -> Dict:
    """Gera, refina (ia_hook) e persiste o diagnóstico de um lead."""
    problemas = detectar_problemas(lead)[:3]
    perdidos, roi, ticket = estimar_roi(lead)
    concorrente = montar_concorrente(lead)

    texto = montar_texto(lead, problemas, perdidos, roi, ticket, concorrente)
    import copy_ai
    texto = copy_ai.refinar(
        texto,
        contexto={"clinica": lead.get("nome"), "nicho": lead.get("nicho"),
                  "nota": lead.get("avaliacao_num"), "posicao_google": lead.get("posicao"),
                  "perda_mes_reais": roi, "pacientes_perdidos_mes": perdidos,
                  "concorrente": concorrente["nome"] if concorrente else None},
        instrucao=("Diagnóstico enviado ao dono da clínica. Abra com o dado mais forte. "
                   "Mantenha os 3 problemas e o valor em R$. Termine com um convite curto."),
    )

    db.save_diagnostico(
        lead_id=lead["id"],
        problemas=[p["titulo"] for p in problemas],
        roi_reais=roi,
        concorrente_nome=concorrente["nome"] if concorrente else "",
        concorrente_nota=concorrente["nota"] if concorrente else None,
        texto=texto,
    )
    return {"problemas": problemas, "roi": roi, "perdidos": perdidos,
            "ticket": ticket, "concorrente": concorrente, "texto": texto}


def run(top_n: int = 30):
    db.init_db()
    leads = db.leads_by_score(limit=top_n, status="qualified")
    if not leads:
        print("⚠ Nenhum lead qualificado. Rode scorer.py antes.")
        return 0
    for lead in leads:
        gerar_diagnostico(lead)
    print(f"✅ {len(leads)} diagnósticos gerados e salvos.")
    return len(leads)


if __name__ == "__main__":
    db.init_db()
    top = db.leads_by_score(limit=1, status="qualified")
    if not top:
        print("⚠ Sem leads qualificados. Rode: python seed_amostra.py && python scorer.py")
    else:
        d = gerar_diagnostico(top[0])
        print("═" * 70)
        print(f"DIAGNÓSTICO, {top[0]['nome']}  (score {top[0]['score']}, nicho {top[0]['nicho']})")
        print("═" * 70)
        print(d["texto"])
        print("═" * 70)
        print(f"Problemas: {[p['chave'] for p in d['problemas']]}")
        print(f"ROI estimado: R$ {_br(d['roi'])}/mês  "
              f"({d['perdidos']} pacientes × R$ {_br(d['ticket'])})")
