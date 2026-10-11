"""relatorios.py, Geradores de documentos a partir dos dados reais do banco."""

import argparse
import re
from datetime import date
from typing import Dict, List

import config
import db
import diagnostico as diag_mod
import proposta as prop_mod


# ── Pontos fortes / fracos ────────────────────────────────────────────────────

def pontos_fortes(lead: Dict) -> List[str]:
    fortes = []
    total = lead.get("total_avaliacoes_num") or 0
    nota = lead.get("avaliacao_num")
    pos = lead.get("posicao")

    if total >= 100:
        fortes.append(f"Movimento consolidado: {total} avaliações no Google (clínica ativa, "
                      f"com fluxo real de pacientes)")
    elif total >= config.VOLUME_ATIVO_MIN:
        fortes.append(f"Base de {total} avaliações, já tem histórico e prova social para alavancar")

    if nota is not None and nota >= config.NOTA_ALVO:
        fortes.append(f"Reputação forte: nota {nota:.1f} (acima do patamar de confiança)")
    elif nota is not None and nota >= 4.0:
        fortes.append(f"Nota {nota:.1f}, razoável, dá para transformar em vantagem com gestão")

    if lead.get("tem_site"):
        fortes.append("Já possui site (base para otimizar, não construir do zero)")
    if lead.get("tem_instagram"):
        fortes.append("Já está no Instagram (canal aberto para ativar)")
    if pos and pos <= 3:
        fortes.append(f"Bem posicionada no Maps ({pos}º lugar)")
    if lead.get("telefone"):
        fortes.append("Telefone público (canal direto de contato/WhatsApp)")

    if not fortes:
        fortes.append("Presença digital muito incipiente, quase tudo a construir")
    return fortes


def pontos_fracos(lead: Dict) -> List[str]:
    fracos = [p["titulo"] for p in diag_mod.detectar_problemas(lead)]
    total = lead.get("total_avaliacoes_num") or 0
    if total < config.VOLUME_ATIVO_MIN:
        fracos.append(f"Pouco movimento: só {total} avaliação(ões), clínica com baixa atividade")
    return fracos or ["Nenhuma falha digital relevante encontrada"]


def classificar(score: float) -> str:
    if score >= 60:
        return "🔥 QUENTE, prioridade de abordagem"
    if score >= 40:
        return "🟠 MORNO, bom potencial"
    if score >= 20:
        return "🔵 FRIO, abordar depois"
    return "⚪ BAIXA PRIORIDADE, pouca oportunidade (clínica já madura ou inativa)"


def _slug(nome: str) -> str:
    s = re.sub(r"[^\w\s-]", "", nome.lower())
    return re.sub(r"[\s]+", "-", s).strip("-")


def _br(n) -> str:
    return diag_mod._br(n)


# ── Documento 1: análise interna das empresas ─────────────────────────────────

def gerar_analise_empresas() -> str:
    leads = db.leads_by_score(limit=1000)
    hoje = date.today().strftime("%d/%m/%Y")
    L = []
    L.append("# Análise de Empresas, Prospecção de Clínicas")
    L.append(f"\n*Documento interno • gerado em {hoje} • {len(leads)} clínicas analisadas*\n")
    L.append("> Ordenado por **oportunidade** (score). Quanto maior o score, mais a clínica "
             "está ativa **mas** com falhas digitais visíveis, o lead ideal.\n")

    # Panorama
    quentes = [l for l in leads if l["score"] >= 60]
    mornos = [l for l in leads if 40 <= l["score"] < 60]
    L.append("## Panorama\n")
    L.append(f"- 🔥 **Quentes (prioridade):** {len(quentes)}")
    L.append(f"- 🟠 **Mornos:** {len(mornos)}")
    L.append(f"- ⚪ **Baixa prioridade:** {len(leads) - len(quentes) - len(mornos)}\n")

    # Tabela-resumo
    L.append("## Resumo\n")
    L.append("| # | Clínica | Nicho | Score | Nota | Aval. | Pos. | Site | IG | ROI/mês |")
    L.append("|---|---------|-------|:-----:|:----:|:-----:|:----:|:----:|:--:|--------:|")
    for i, l in enumerate(leads, 1):
        diag = db.get_diagnostico(l["id"]) or {}
        roi = diag.get("roi_reais") or diag_mod.estimar_roi(l)[1]
        nota = f"{l['avaliacao_num']:.1f}" if l["avaliacao_num"] is not None else "-"
        L.append(f"| {i} | {l['nome']} | {l['nicho']} | {l['score']} | {nota} | "
                 f"{l['total_avaliacoes_num']} | {l['posicao'] or '-'} | "
                 f"{'✅' if l['tem_site'] else '❌'} | {'✅' if l['tem_instagram'] else '❌'} | "
                 f"R$ {_br(roi)} |")

    # Fichas por empresa
    L.append("\n---\n\n## Ficha por empresa\n")
    for i, l in enumerate(leads, 1):
        prop = db.get_proposta(l["id"]) or {}
        nivel = prop.get("nivel_nome", "-")
        roi = (db.get_diagnostico(l["id"]) or {}).get("roi_reais") or diag_mod.estimar_roi(l)[1]
        nota = f"{l['avaliacao_num']:.1f}" if l["avaliacao_num"] is not None else "sem nota"

        L.append(f"### {i}. {l['nome']}")
        L.append(f"*{l['categoria']} • {l.get('endereco','')}*\n")
        L.append(f"**{classificar(l['score'])}**  ·  Score **{l['score']}/100**  ·  "
                 f"ROI estimado **R$ {_br(roi)}/mês**  ·  Plano indicado: **{nivel}**\n")
        L.append(f"- 📍 Posição no Maps: **{l['posicao'] or '-'}º**  ·  ⭐ Nota: **{nota}**  ·  "
                 f"🗣️ Avaliações: **{l['total_avaliacoes_num']}**")
        L.append(f"- 🌐 Site: {'sim' if l['tem_site'] else '**não**'}  ·  "
                 f"📸 Instagram: {'sim' if l['tem_instagram'] else '**não**'}  ·  "
                 f"📞 {l.get('telefone') or '-'}\n")
        L.append("**✅ Pontos fortes**")
        for p in pontos_fortes(l):
            L.append(f"- {p}")
        L.append("\n**⚠️ Pontos fracos (oportunidades de venda)**")
        for p in pontos_fracos(l):
            L.append(f"- {p}")
        L.append("\n")
    return "\n".join(L)


# ── Documento 2: documento de prospecção (cliente) ────────────────────────────

def gerar_documento_prospeccao(lead: Dict) -> str:
    problemas = diag_mod.detectar_problemas(lead)[:3]
    perdidos, roi, ticket = diag_mod.estimar_roi(lead)
    concorrente = diag_mod.montar_concorrente(lead)
    nivel = prop_mod.recomendar_nivel(lead)
    preco = prop_mod._preco_representativo(nivel)
    fcfg = config.FAIXAS_PRECO
    hoje = date.today().strftime("%d/%m/%Y")
    nome = lead["nome"]

    L = []
    L.append(f"# Raio-X da Presença Digital")
    L.append(f"## {nome}\n")
    L.append(f"*Preparado especialmente para a {nome} • {hoje}*\n")
    L.append("---\n")
    L.append(f"Olá! Fizemos um diagnóstico rápido de como a **{nome}** aparece hoje na internet "
             f"para quem procura {lead['categoria'].lower()} na região. "
             f"Encontramos **{len(problemas)} pontos** que provavelmente estão fazendo a clínica "
             f"perder pacientes todos os meses. Segue o resumo, sem rodeios.\n")

    L.append("## 🔍 O que encontramos\n")
    for i, p in enumerate(problemas, 1):
        L.append(f"**{i}. {p['titulo']}**  ")
        L.append(f"{p['texto']}\n")

    L.append("## 📉 Quanto isso está custando\n")
    L.append(f"Estimamos que a {nome} deixa escapar cerca de **{perdidos} novos pacientes por mês**. "
             f"A um ticket médio de **R$ {_br(ticket)}**, isso representa aproximadamente:\n")
    L.append(f"> ## 💰 R$ {_br(roi)} por mês deixados na mesa\n")
    L.append(f"*(≈ R$ {_br(roi*12)} por ano)*\n")

    if concorrente:
        L.append("## 🏆 Como está a concorrência\n")
        L.append(f"{concorrente['texto']}\n")

    L.append("## ✅ Como resolvemos, 3 caminhos\n")
    L.append("Nosso sistema resolve isso em 3 níveis. Você escolhe o ritmo:\n")
    marc = {1: "🟢", 2: "🔵", 3: "🟣"}
    from proposta import NIVEL_DESC
    for n in (1, 2, 3):
        f = fcfg[n]
        estrela = " ⭐ **O mais escolhido**" if f["mais_vendido"] else ""
        rec = "  ← **recomendado para você**" if n == nivel else ""
        L.append(f"### {marc[n]} Nível {n}, {f['nome']}  ·  R$ {_br(f['min'])} a R$ {_br(f['max'])}/mês{estrela}{rec}")
        L.append(f"{NIVEL_DESC[n]['resumo']}\n")

    pacientes_payback = max(1, round(preco / ticket)) if ticket else 1
    L.append("## 🎯 Nossa recomendação\n")
    L.append(f"Para o momento da {nome}, indicamos o **Nível {nivel}, {fcfg[nivel]['nome']}**. "
             f"Com apenas **{pacientes_payback} paciente(s) recuperado(s) por mês**, o "
             f"investimento já se paga, o resto vira lucro.\n")
    L.append("---\n")
    L.append(f"### Quer ver como ficaria na prática?\n")
    L.append(f"Responda esta mensagem ou chame a gente. Montamos uma demonstração sem compromisso "
             f"para a {nome}.\n")
    L.append(f"\n*{config.ASSINATURA_EMAIL.strip()}*")
    return "\n".join(L)


# ── Documento 2 (versão HTML profissional, imprimível/enviável) ───────────────

def gerar_documento_prospeccao_html(lead: Dict) -> str:
    problemas = diag_mod.detectar_problemas(lead)[:3]
    perdidos, roi, ticket = diag_mod.estimar_roi(lead)
    concorrente = diag_mod.montar_concorrente(lead)
    nivel = prop_mod.recomendar_nivel(lead)
    preco = prop_mod._preco_representativo(nivel)
    fcfg = config.FAIXAS_PRECO
    from proposta import NIVEL_DESC
    hoje = date.today().strftime("%d/%m/%Y")
    nome = lead["nome"]
    pacientes_payback = max(1, round(preco / ticket)) if ticket else 1

    def esc(t):
        return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    problemas_html = "".join(
        f'<div class="prob"><div class="prob-num">{i}</div><div>'
        f'<h3>{esc(p["titulo"])}</h3><p>{esc(p["texto"])}</p></div></div>'
        for i, p in enumerate(problemas, 1)
    )

    concorrente_html = ""
    if concorrente:
        concorrente_html = (
            f'<div class="sec"><h2>🏆 Como está a concorrência</h2>'
            f'<p class="conc">{esc(concorrente["texto"])}</p></div>'
        )

    niveis_html = ""
    marc = {1: "#2f9e6f", 2: "#2563eb", 3: "#7c3aed"}
    for n in (1, 2, 3):
        f = fcfg[n]
        rec = " nivel--rec" if n == nivel else ""
        badge = '<span class="badge">⭐ Mais escolhido</span>' if f["mais_vendido"] else ""
        badge_rec = '<span class="badge badge--rec">Recomendado p/ você</span>' if n == nivel else ""
        niveis_html += (
            f'<div class="nivel{rec}" style="--c:{marc[n]}">'
            f'<div class="nivel-top"><span class="nivel-nome">Nível {n} · {esc(f["nome"])}</span>'
            f'{badge}{badge_rec}</div>'
            f'<div class="nivel-preco">R$ {_br(f["min"])} – R$ {_br(f["max"])}<span>/mês</span></div>'
            f'<p>{esc(NIVEL_DESC[n]["resumo"])}</p></div>'
        )

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Raio-X Presença Digital, {esc(nome)}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         color: #1a2430; background: #eef2f6; line-height: 1.55; padding: 24px; }}
  .page {{ max-width: 820px; margin: 0 auto; background: #fff; border-radius: 14px;
          overflow: hidden; box-shadow: 0 10px 40px rgba(20,40,60,.12); }}
  .hero {{ background: linear-gradient(135deg, #0f2f43, #17607e); color: #fff; padding: 38px 44px; }}
  .hero .kicker {{ text-transform: uppercase; letter-spacing: .14em; font-size: 12px;
                  opacity: .8; }}
  .hero h1 {{ font-size: 30px; margin: 6px 0 2px; }}
  .hero .sub {{ opacity: .85; font-size: 14px; }}
  .body {{ padding: 34px 44px 40px; }}
  .lead-in {{ font-size: 16px; color: #33475b; margin-bottom: 26px; }}
  .sec {{ margin: 30px 0; }}
  h2 {{ font-size: 18px; color: #0f2f43; border-left: 4px solid #17607e; padding-left: 10px;
       margin-bottom: 14px; }}
  .prob {{ display: flex; gap: 14px; padding: 14px 0; border-bottom: 1px solid #eef2f6; }}
  .prob:last-child {{ border-bottom: none; }}
  .prob-num {{ flex: none; width: 30px; height: 30px; border-radius: 50%; background: #17607e;
              color: #fff; font-weight: 700; display: flex; align-items: center;
              justify-content: center; }}
  .prob h3 {{ font-size: 15px; color: #12384d; margin-bottom: 3px; }}
  .prob p {{ font-size: 14px; color: #4a5a6a; }}
  .roi {{ background: #0f2f43; color: #fff; border-radius: 12px; padding: 24px 28px;
         text-align: center; margin: 26px 0; }}
  .roi .small {{ font-size: 14px; opacity: .85; }}
  .roi .big {{ font-size: 40px; font-weight: 800; color: #58d0a0; margin: 6px 0; }}
  .roi .year {{ font-size: 13px; opacity: .7; }}
  .conc {{ background: #fff8ec; border: 1px solid #f4dfae; border-radius: 10px; padding: 16px;
          font-size: 14px; color: #6a5320; }}
  .niveis {{ display: grid; gap: 12px; }}
  .nivel {{ border: 1.5px solid #e3e9ef; border-radius: 12px; padding: 16px 18px; }}
  .nivel--rec {{ border-color: var(--c); box-shadow: 0 4px 16px color-mix(in srgb, var(--c) 22%, transparent); }}
  .nivel-top {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .nivel-nome {{ font-weight: 700; color: var(--c); font-size: 15px; }}
  .nivel-preco {{ font-size: 22px; font-weight: 800; color: #12384d; margin: 4px 0 6px; }}
  .nivel-preco span {{ font-size: 13px; font-weight: 500; color: #7a8a99; }}
  .nivel p {{ font-size: 13.5px; color: #4a5a6a; }}
  .badge {{ font-size: 11px; background: #eef2f6; color: #33475b; padding: 3px 9px;
           border-radius: 999px; font-weight: 600; }}
  .badge--rec {{ background: var(--c); color: #fff; }}
  .rec-box {{ background: #ecf6f1; border-radius: 12px; padding: 20px 24px; margin-top: 26px; }}
  .rec-box h2 {{ border: none; padding: 0; }}
  .cta {{ text-align: center; margin-top: 30px; padding-top: 24px; border-top: 2px dashed #dde5ec; }}
  .cta .btn {{ display: inline-block; background: #17607e; color: #fff; padding: 13px 30px;
              border-radius: 999px; font-weight: 700; text-decoration: none; }}
  .foot {{ text-align: center; font-size: 12px; color: #90a0af; margin-top: 22px; }}
  @media print {{ body {{ background: #fff; padding: 0; }}
                 .page {{ box-shadow: none; border-radius: 0; }} }}
</style></head>
<body><div class="page">
  <div class="hero">
    <div class="kicker">Raio-X da Presença Digital</div>
    <h1>{esc(nome)}</h1>
    <div class="sub">Preparado especialmente para a sua clínica · {hoje}</div>
  </div>
  <div class="body">
    <p class="lead-in">Fizemos um diagnóstico rápido de como a <b>{esc(nome)}</b> aparece hoje para
      quem procura {esc(lead['categoria'].lower())} na região. Encontramos
      <b>{len(problemas)} pontos</b> que provavelmente estão fazendo a clínica perder pacientes
      todos os meses. Segue o resumo, sem rodeios.</p>

    <div class="sec"><h2>🔍 O que encontramos</h2>{problemas_html}</div>

    <div class="roi">
      <div class="small">Estimamos ~<b>{perdidos} novos pacientes/mês</b> escapando · ticket médio R$ {_br(ticket)}</div>
      <div class="big">R$ {_br(roi)}/mês</div>
      <div class="year">deixados na mesa · ≈ R$ {_br(roi*12)} por ano</div>
    </div>

    {concorrente_html}

    <div class="sec"><h2>✅ Como resolvemos, 3 caminhos</h2>
      <div class="niveis">{niveis_html}</div>
    </div>

    <div class="rec-box"><h2>🎯 Nossa recomendação</h2>
      <p>Para o momento da {esc(nome)}, indicamos o <b>Nível {nivel}, {esc(fcfg[nivel]['nome'])}</b>.
      Com apenas <b>{pacientes_payback} paciente(s) recuperado(s)/mês</b>, o investimento já se paga -
      o resto vira lucro.</p></div>

    <div class="cta">
      <p style="margin-bottom:14px;color:#33475b">Quer ver como ficaria na prática, sem compromisso?</p>
      <a class="btn">Falar com a gente</a>
    </div>
    <div class="foot">Equipe Presença Digital para Clínicas · Captação • Confiança • Conversão</div>
  </div>
</div></body></html>"""


# ── Orquestração ──────────────────────────────────────────────────────────────

def salvar(nome_arquivo: str, conteudo: str):
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    caminho = config.OUT_DIR / nome_arquivo
    caminho.write_text(conteudo, encoding="utf-8")
    return caminho


def main(lead_id: int = None, todos: bool = False):
    db.init_db()
    leads = db.leads_by_score(limit=1000)
    if not leads:
        print("⚠ Sem leads. Rode: python run_pilot.py")
        return

    # Documento 1, análise interna (sempre)
    cam1 = salvar("analise_empresas.md", gerar_analise_empresas())
    print(f"✅ Análise interna das empresas: {cam1}")

    # Documento 2, prospecção (Markdown + HTML profissional)
    if todos:
        for l in leads:
            slug = _slug(l["nome"])
            salvar(f"prospeccao/{slug}.md", gerar_documento_prospeccao(l))
            salvar(f"prospeccao/{slug}.html", gerar_documento_prospeccao_html(l))
        print(f"✅ {len(leads)} documentos de prospecção (.md + .html) em: "
              f"{config.OUT_DIR / 'prospeccao'}")
    else:
        alvo = db.get_lead(lead_id) if lead_id else leads[0]
        if not alvo:
            print(f"⚠ Lead id {lead_id} não encontrado.")
            return
        slug = _slug(alvo["nome"])
        cam2 = salvar(f"documento_prospeccao_{slug}.md", gerar_documento_prospeccao(alvo))
        cam3 = salvar(f"documento_prospeccao_{slug}.html",
                      gerar_documento_prospeccao_html(alvo))
        print(f"✅ Documento de prospecção ({alvo['nome']}):")
        print(f"   • Markdown: {cam2}")
        print(f"   • HTML (imprimível/enviável): {cam3}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead", type=int, default=None, help="id do lead p/ prospecção")
    ap.add_argument("--todos", action="store_true", help="1 documento por lead")
    args = ap.parse_args()
    main(lead_id=args.lead, todos=args.todos)
