"""proposta.py, Módulo C (Propor)."""

import math
from typing import Dict

import config
import db
import diagnostico as diag_mod
import ia_hook

# Descrição de cada nível (o "o que resolve" é encaixado no texto).
NIVEL_DESC = {
    1: {
        "resumo": ("Perfil do Google otimizado, uma página de vendas e gestão "
                   "das avaliações."),
        "pilar": "Captação + Confiança",
    },
    2: {
        "resumo": ("tudo do Presença, mais blog no site com publicação de "
                   "conteúdo sobre os procedimentos."),
        "pilar": "Captação + Confiança",
    },
    3: {
        "resumo": ("tudo do Conversão, com site institucional no lugar da página "
                   "de vendas e automação de WhatsApp e Instagram."),
        "pilar": "Captação + Confiança + Conversão",
    },
}


def _preco_representativo(nivel: int) -> int:
    f = config.FAIXAS_PRECO[nivel]
    return round((f["min"] + f["max"]) / 2)


def net_return_12m(lead: Dict, nivel: int) -> float:
    """
    Retorno LÍQUIDO estimado em 12 meses do nível (retorno acumulado − investimento).
    Usa a mesma simulação do documento do cliente: recuperação de pacientes com rampa
    (curva-S) até o teto do nível, × ticket, menos o custo mensal × 12.
    """
    perdidos, _, ticket = diag_mod.estimar_roi(lead)
    teto = config.RECUP_TETO_NIVEL[nivel]
    preco = _preco_representativo(nivel)
    acc = sum(perdidos * teto * (1 - math.exp(-m / config.RECUP_VELOCIDADE)) * ticket
              for m in range(1, config.SIM_MESES + 1))
    return acc - preco * config.SIM_MESES


def recomendar_nivel(lead: Dict) -> int:
    """
    Recomenda o nível que dá o MAIOR retorno líquido em R$ para ESTA clínica (honesto e
    coerente com a simulação que o cliente vê). Em empate técnico entre Conversão (N2) e
    Domínio (N3), fica no N2, o carro-chefe.
    """
    nets = {n: net_return_12m(lead, n) for n in (1, 2, 3)}
    melhor = max(nets, key=nets.get)
    if melhor == 3 and nets[2] >= nets[3] * 0.92:
        melhor = 2
    return melhor


def _faixa_txt(nivel: int) -> str:
    f = config.FAIXAS_PRECO[nivel]
    return f"R$ {diag_mod._br(f['min'])} a R$ {diag_mod._br(f['max'])}/mês"


def montar_texto(lead: Dict, roi: int, ticket: int, problemas, nivel: int) -> str:
    nome = lead.get("nome", "sua clínica")
    linhas = [
        f"{nome}, com base no raio-x que fiz, montei 3 caminhos para parar de perder "
        f"pacientes, do essencial ao domínio total da sua região:",
        "",
    ]
    marcadores = {1: "🟢", 2: "🔵", 3: "🟣"}
    for n in (1, 2, 3):
        f = config.FAIXAS_PRECO[n]
        estrela = "  ⭐ O MAIS ESCOLHIDO" if f["mais_vendido"] else ""
        linhas.append(f"{marcadores[n]} Nível {n}, {f['nome']} ({_faixa_txt(n)}){estrela}")
        linhas.append(f"   {NIVEL_DESC[n]['resumo']}")
        linhas.append("")

    preco = _preco_representativo(nivel)
    # Payback: quantos pacientes recuperados cobrem o investimento.
    pacientes_payback = max(1, round(preco / ticket)) if ticket else 1
    linhas += [
        f"👉 Para a {nome}, o mais indicado é o Nível {nivel}, {config.FAIXAS_PRECO[nivel]['nome']} "
        f"({NIVEL_DESC[nivel]['pilar']}).",
        f"   Você tem hoje cerca de R$ {diag_mod._br(roi)}/mês escapando. Com "
        f"{pacientes_payback} paciente(s) recuperado(s), o plano já se paga, o resto vira lucro.",
        "",
        "Quer que eu te mostre exatamente como ficaria no seu caso, sem compromisso?",
    ]
    return "\n".join(linhas)


def gerar_proposta(lead: Dict) -> Dict:
    problemas = diag_mod.detectar_problemas(lead)[:3]
    _, roi, ticket = diag_mod.estimar_roi(lead)
    nivel = recomendar_nivel(lead)
    preco = _preco_representativo(nivel)
    texto = montar_texto(lead, roi, ticket, problemas, nivel)
    import copy_ai
    texto = copy_ai.refinar(
        texto,
        contexto={"clinica": lead.get("nome"), "nivel_indicado": config.FAIXAS_PRECO[nivel]["nome"],
                  "preco_mes": preco, "perda_mes_reais": roi, "nicho": lead.get("nicho"),
                  "categoria": lead.get("categoria"), "posicao_google": lead.get("posicao")},
        instrucao=("Proposta comercial. Mantenha os 3 níveis com os preços exatos e a "
                   "recomendação. Justifique o nível indicado pelo número, não por adjetivo. "
                   "Cite o serviço da clínica APENAS pela categoria informada, não invente "
                   "outra especialidade."))

    db.save_proposta(
        lead_id=lead["id"],
        nivel=nivel,
        nivel_nome=config.FAIXAS_PRECO[nivel]["nome"],
        preco=preco,
        texto=texto,
    )
    return {"nivel": nivel, "nivel_nome": config.FAIXAS_PRECO[nivel]["nome"],
            "preco": preco, "roi": roi, "texto": texto}


def run(top_n: int = 30):
    db.init_db()
    leads = db.leads_by_score(limit=top_n, status="qualified")
    if not leads:
        print("⚠ Nenhum lead qualificado. Rode scorer.py antes.")
        return 0
    for lead in leads:
        gerar_proposta(lead)
    print(f"✅ {len(leads)} propostas geradas e salvas.")
    return len(leads)


if __name__ == "__main__":
    db.init_db()
    top = db.leads_by_score(limit=1, status="qualified")
    if not top:
        print("⚠ Sem leads qualificados. Rode: python seed_amostra.py && python scorer.py")
    else:
        p = gerar_proposta(top[0])
        print("═" * 70)
        print(f"PROPOSTA, {top[0]['nome']}  → recomendado: Nível {p['nivel']} "
              f"({p['nivel_nome']}), R$ {diag_mod._br(p['preco'])}/mês")
        print("═" * 70)
        print(p["texto"])
        print("═" * 70)
