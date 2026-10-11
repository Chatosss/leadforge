"""proposta_designs.py, Variações de DESIGN da proposta (mesmo conteúdo)."""

import asyncio
import re
from datetime import date

import config
import db
import documento_pdf as dp
import proposta as prop_mod
import pagespeed
import diagnostico as diag_mod
from diagnostico import PROBLEMA_META
from proposta import NIVEL_DESC
from capa_designs import _iniciais, _nome_curto

esc, _br, _money, _slug = dp.esc, dp._br, dp._money, dp._slug

_MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
          "agosto", "setembro", "outubro", "novembro", "dezembro"]


def _data_pt():
    d = date.today()
    return f"{d.day} de {_MESES[d.month - 1]} de {d.year}"


# ── Personalização / dados ────────────────────────────────────────────────────

# Logradouro em qualquer posição da parte, não só no começo.
_RE_LOGRADOURO = re.compile(
    r"(?:^|\s)(?:r\.|av\.|al\.|rod\.|estr\.|q\.|conj\.|trav\.|pc\.|sl\.?|"
    r"cj\.?|apto?\.?|bl\.?)(?=\s|\d|$)"
    r"|\b(?:rua|avenida|alameda|travessa|rodovia|pra[çc]a|estrada|quadra|"
    r"lote|conjunto|km|sala|andar|bloco|loja|galeria|edif[íi]cio)\b", re.I)

# Nome de prédio, shopping ou complemento no lugar da cidade. Saíam coisas como
# "Atlantic Tower", "Prédio CEMOM", "Shopping Plaza" e até "Térreo" na frase
# "quem procura dentista em ___".
_RE_PREDIO = re.compile(
    r"\b(pr[ée]dio|ed\.|ed[íi]f\w*|shopping|tower|torre|condom[íi]nio|"
    r"empresarial|emp\.|t[ée]rreo|plaza|center|mall|altos|anexo|plat[ií]num|"
    r"skye|bairro|largo|n[º°]|sobreloja|piso)\b", re.I)

# Sobrou lixo que nenhum dos dois padrões acima descreve: "n° 46", "Correira",
# "emp.
_RE_BAIRRO_PLAUSIVEL = re.compile(r"[A-Za-zÀ-ÿ]{4,}")


def _regiao(lead) -> str:
    """O lugar do lead, para a frase "quem procura X em <região>"."""
    import cidade as cidade_mod
    apurada = cidade_mod.resolver(lead)
    if apurada["confianca"] == "apurada" and apurada["cidade"]:
        return apurada["cidade"]

    end = (lead.get("endereco") or "").strip()
    nome_lead = (lead.get("nome") or "").lower()
    partes = [p.strip() for p in re.split(r"[·,\-]", end) if p.strip()]
    cand = [p for p in partes
            if len(p) > 2
            and not re.fullmatch(r"[\d\s]+", p)
            and not _RE_LOGRADOURO.search(p)
            and not _RE_PREDIO.search(p)
            and not re.match(r"^\d", p)         # "46 Rua ..." e "1000 Centro"
            and not re.search(r"\d", p)         # "n° 46", "Sl 407", "Q. 12"
            and _RE_BAIRRO_PLAUSIVEL.search(p)
            # o próprio nome da clínica aparece no endereço em alguns registros,
            # e virava "quem procura dentista em CEO CLINICA ESPECIALIZADA..."
            and p.lower() not in nome_lead]
    if cand:
        return cand[-1]
    return "sua região"


def _reviews_validas(lead):
    """
    Nº de avaliações do lead, ignorando lixo: se for igual ao DDD do telefone, é o DDD
    que vazou na captura antiga -> retorna None (desconhecido).
    """
    import whatsapp_check
    t = lead.get("total_avaliacoes_num")
    if not t:
        return None
    n = whatsapp_check.normalizar(lead.get("telefone") or "")
    if n and str(t) == n["ddd"]:
        return None
    return t


def _textos_ia(lead, d, usar_analise: bool = True) -> dict:
    """Textos do PDF vindos da IA."""
    try:
        import copy_ai
        import conteudo
        import ia_hook
        if not ia_hook.IA_ATIVA:
            return {}
    except Exception:
        return {}

    # ── caminho novo: relatório a partir da ANÁLISE do dossiê ──
    if usar_analise:
        try:
            import analise_ia
            a = analise_ia.carregar(lead["id"]) or analise_ia.analisar(lead["id"])
        except Exception:
            a = {}
        if a and a.get("falhas"):
            probs = []
            for i, f in enumerate(a["falhas"][:3]):
                # Falha extra da IA não vira problema de posição.
                base = (d["problemas"][i] if i < len(d["problemas"])
                        else {"chave": "", "nivel": 1})
                probs.append({**base,
                              "titulo": f.get("titulo") or base.get("titulo", ""),
                              "texto": f.get("explicacao", ""),
                              "impacto": f.get("impacto", "")})
            return {
                "abertura": a.get("abertura_relatorio", ""),
                "problemas": probs,
                "leitura_geral": a.get("leitura_geral", ""),
                "achados_busca": a.get("achados_da_busca", ""),
                "pontos_fortes": a.get("pontos_fortes", []),
                "recomendacao": a.get("prioridade_1", ""),
                "fonte": "analise",
            }

    ctx = conteudo._contexto(lead)
    fcfg = config.FAIXAS_PRECO
    nivel = d["nivel"]
    out = {}

    # 1) Abertura da capa, carta curta e pessoal
    r = copy_ai.escrever(
        "Escreva a ABERTURA de um diagnóstico impresso entregue ao dono desta clínica.\n"
        "FORMATO: 2 frases, no máximo 40 palavras no total. Sem saudação ('Olá'), sem emoji.\n"
        "Diga o que foi olhado e o que o documento mostra. Tom de consultor, sereno.\n"
        "É PROIBIDO afirmar que o material NÃO é um modelo pronto, que não é "
        "genérico, ou qualquer negação parecida: negar planta a ideia na cabeça "
        "de quem lê, que não tinha pensado nisso. A personalização se prova pelos "
        "dados da clínica no corpo do documento, não por adjetivo na capa.",
        ctx, max_tokens=160)
    if r["texto"]:
        out["abertura"] = r["texto"].strip().strip('"')

    # 2) Os 3 problemas, explicação mais afiada (mantendo o título)
    probs = []
    for p in d["problemas"]:
        rp = copy_ai.escrever(
            f"Explique este problema da clínica para o dono dela, em 2 frases "
            f"(máx. 38 palavras). Concreto, sem hype, sem repetir o título.\n"
            f"REGRA: explique APENAS o mecanismo DESTE problema (o que o paciente vive "
            f"e por que ele vai embora). NÃO cite valores em R$, NÃO cite o número de "
            f"pacientes perdidos, NÃO cite o nome do concorrente e NÃO repita a posição "
            f"no Google, esses dados já aparecem em outras partes do documento.\n"
            f"TÍTULO DO PROBLEMA: {p['titulo']}\n"
            f"EXPLICAÇÃO BASE (mantenha o sentido): {p['texto']}",
            ctx, max_tokens=170)
        probs.append({**p, "texto": (rp["texto"].strip().strip('"') or p["texto"])
                      if rp["texto"] else p["texto"]})
    if probs:
        out["problemas"] = probs

    # 3) Recomendação final
    rr = copy_ai.escrever(
        f"Escreva o parágrafo de RECOMENDAÇÃO do diagnóstico.\n"
        f"FORMATO: 2 a 3 frases, máx. 55 palavras. Sem emoji.\n"
        f"Recomende o Nível {nivel} ({fcfg[nivel]['nome']}), preço R$ "
        f"{_br(fcfg[nivel]['min'])} a R$ {_br(fcfg[nivel]['max'])}/mês. "
        f"IMPORTANTE: não prometa retorno, lucro, payback nem multiplicação de investimento. "
        f"Fale do valor que a clínica DEIXA DE RECEBER hoje ({_money(d['roi'])} por mês) e de "
        f"que esse valor continua somando enquanto nada muda. Reconheça que a correção leva "
        f"tempo (semanas até o Google reconhecer, reputação construída aos poucos). "
        f"Feche com um convite curto e sem pressão.",
        ctx, max_tokens=220)
    if rr["texto"]:
        out["recomendacao"] = rr["texto"].strip().strip('"')
    return out


def _dados(lead) -> dict:
    sim = dp.simular(lead)
    problemas = sim["problemas"]
    nivel = sim["nivel_rec"]
    preco = prop_mod._preco_representativo(nivel)
    d = {
        "sim": sim, "problemas": problemas, "perdidos": sim["perdidos"],
        "roi": sim["roi"], "ticket": sim["ticket"], "nivel": nivel, "preco": preco,
        "payback": sim["payback_por_nivel"][nivel], "roi12": sim["roi12_por_nivel"][nivel],
        "retorno_ano": sim["retorno_por_nivel"][nivel][-1][1],
        "pacientes_payback": max(1, round(preco / sim["ticket"])) if sim["ticket"] else 1,
        "pagespeed": pagespeed.dados_para_pdf(lead),
    }
    # Comparação com a referência (números das duas clínicas).
    conc = diag_mod.montar_concorrente(lead) or {}
    c = dict(db.get_lead(conc["id"]) or {}) if conc.get("id") else None
    if c:
        pa, pb = lead.get("posicao") or 25, c.get("posicao") or 25
        na, nb = lead.get("avaliacao_num") or 0, c.get("avaliacao_num") or 0
        va_n, vb_n = _reviews_validas(lead), _reviews_validas(c)  # ignora DDD/lixo
        va, vb = va_n or 0, vb_n or 0
        vmax = max(va, vb, 1)
        presa = (1 if lead.get("tem_site") else 0) + (1 if lead.get("tem_instagram") else 0)
        presb = (1 if c.get("tem_site") else 0) + (1 if c.get("tem_instagram") else 0)
        d["versus"] = {
            "lider": c["nome"],
            "rows": [
                ("Posição no Maps", f"{pa}º", max(.05, 1 - (pa - 1) / 25), f"{pb}º", max(.05, 1 - (pb - 1) / 25)),
                ("Nota Google", f"{na:.1f}" if na else "-", na / 5, f"{nb:.1f}" if nb else "-", nb / 5),
                ("Avaliações", _br(va_n) if va_n else "-", va / vmax, _br(vb_n) if vb_n else "-", vb / vmax),
                ("Site/Insta no Google", f"{presa}/2", presa / 2, f"{presb}/2", presb / 2),
            ],
        }
    else:
        d["versus"] = None

    # o eixo da comparação e o texto que o explica, do mesmo lugar que a copy usa
    d["conc_meta"] = conc

    # textos refinados por IA (vazio = usa os determinísticos)
    d["ia"] = _textos_ia(lead, d)
    if d["ia"].get("problemas"):
        d["problemas"] = d["ia"]["problemas"]
    return d


# ── Temas ─────────────────────────────────────────────────────────────────────

THEMES = {
    1: {  # Sob Medida, editorial creme
        "bg": "#f7f3ec", "panel": "#fffdf9", "ink": "#2b2620", "muted": "#8a7f6d",
        "line": "#e7ded0", "accent": "#a97f4b", "accent2": "#4f8a6b", "red": "#bf5a4c",
        "hfont": "Georgia, 'Times New Roman', serif", "cover": "editorial",
    },
    2: {  # Executivo, azul petróleo
        "bg": "#eef2f6", "panel": "#ffffff", "ink": "#12283a", "muted": "#7a8a99",
        "line": "#e1e8ee", "accent": "#17607e", "accent2": "#2f9e6f", "red": "#d9534f",
        "hfont": "'Segoe UI', Roboto, Arial, sans-serif", "cover": "gradiente",
    },
    3: {  # Premium, escuro esmeralda
        "bg": "#0f1319", "panel": "#171d26", "ink": "#e8edf3", "muted": "#8ea0b2",
        "line": "#2a333f", "accent": "#34d399", "accent2": "#60a5fa", "red": "#f87171",
        "hfont": "'Segoe UI', Roboto, Arial, sans-serif", "cover": "dark",
    },
}


def _css(t: dict) -> str:
    return f"""
  /* margem vertical em TODA página; o conteúdo flui e preenche o espaço */
  @page{{size:A4;margin:14mm 0}}
  *{{box-sizing:border-box;margin:0;padding:0;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
  html,body{{background:{t['panel']}}}
  body{{font-family:'Segoe UI',Roboto,Arial,sans-serif;color:{t['ink']};font-size:13px;line-height:1.55}}
  h1,h2,h3{{font-family:{t['hfont']}}}
  /* cada bloco continua inteiro, mas não força página nova: sem buracos */
  .sheet{{padding:0 16mm 6mm}}
  .sheet.quebra{{break-before:page}}
  h2{{font-size:18px;color:{t['ink']};margin-bottom:6px}}
  .accent{{color:{t['accent']}}}
  .cap{{color:{t['muted']};font-size:12px;margin-bottom:12px}}
  .sec{{margin:22px 0}}
  .rule{{height:3px;width:54px;background:{t['accent']};border-radius:3px;margin:8px 0 14px}}
  .prob{{display:flex;gap:13px;padding:12px 0;border-bottom:1px solid {t['line']}}}
  .prob:last-child{{border:none}}
  .prob-n{{flex:none;width:28px;height:28px;border-radius:50%;background:{t['accent']};color:#fff;font-weight:700;display:flex;align-items:center;justify-content:center}}
  .prob h3{{font-size:14px}}.prob p{{font-size:12.5px;color:{t['muted']}}}
  .prob-imp{{margin-top:5px;padding:6px 10px;background:{t['bg']};border-left:3px solid {t['red']};
    border-radius:0 6px 6px 0;font-size:12px}}
  .prob-imp b{{color:{t['red']}}}
  .kpis{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
  .kpi{{background:{t['bg']};border:1px solid {t['line']};border-radius:11px;padding:13px 15px}}
  .kpi .v{{font-size:20px;font-weight:800}}.kpi .l{{font-size:11px;color:{t['muted']};margin-top:2px}}
  .callout{{background:{t['accent']};color:#fff;border-radius:12px;padding:18px 22px;text-align:center;
    margin:16px 0;break-inside:avoid;page-break-inside:avoid}}
  .callout .big{{font-size:32px;font-weight:800;line-height:1.15;margin:2px 0}}
  /* nada de bloco cortado ao virar a página */
  .sec,.box,.prob,.nivel,.rec,.kpi,svg{{break-inside:avoid;page-break-inside:avoid}}
  h2{{break-after:avoid;page-break-after:avoid}}
  .box{{background:{t['bg']};border:1px solid {t['line']};border-radius:12px;padding:16px 18px;margin:14px 0}}
  .passo-fase{{font-size:11px;letter-spacing:.10em;text-transform:uppercase;
    color:{t['muted']};margin:18px 0 10px;font-weight:700}}
  .passo{{display:flex;gap:12px;align-items:flex-start;padding:11px 0;
    border-bottom:1px solid {t['line']};break-inside:avoid;page-break-inside:avoid}}
  .passo:last-child{{border-bottom:none}}
  .passo-n{{flex:0 0 24px;height:24px;border-radius:50%;color:#fff;font-size:12px;
    font-weight:700;display:flex;align-items:center;justify-content:center;margin-top:1px}}
  .passo-txt{{flex:1;font-size:12.5px;line-height:1.5}}
  .passo-txt b{{font-size:13px}}
  .passo-ganho{{margin:5px 0 0;color:{t['accent2']};font-size:12px;font-weight:600}}
  .nivel-inc{{font-size:11.5px;color:{t['ink']};margin-top:8px}}
  .nivel-inc b{{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{t['muted']}}}
  .nivel-inc ul{{margin:4px 0 0 16px}}
  .nivel-inc li{{margin-bottom:2px}}
  .extra{{background:{t['bg']};border:1px dashed {t['line']};border-radius:12px;
    padding:14px 16px;margin-top:12px;break-inside:avoid;page-break-inside:avoid}}
  .extra-h{{display:flex;justify-content:space-between;align-items:baseline;gap:12px}}
  .extra-preco{{color:{t['accent']};font-weight:700;font-size:15px}}
  .extra-preco span{{font-size:11px;color:{t['muted']};font-weight:400}}
  .extra p{{margin:5px 0 0;font-size:12px;color:{t['muted']};line-height:1.5}}
  .niveis{{display:grid;gap:11px}}
  .nivel{{border:1.5px solid {t['line']};border-radius:12px;padding:14px 16px;background:{t['panel']}}}
  .nivel--rec{{border-color:{t['accent']};box-shadow:0 3px 14px rgba(0,0,0,.06)}}
  .nivel-h{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
  .nivel-nome{{font-weight:700;color:{t['accent']};font-size:14px}}
  .nivel-preco{{font-size:19px;font-weight:800;margin:3px 0}}
  .nivel-preco span{{font-size:12px;font-weight:500;color:{t['muted']}}}
  .nivel p{{font-size:12px;color:{t['muted']}}}
  .tag{{font-size:10.5px;background:{t['bg']};color:{t['muted']};padding:2px 8px;border-radius:999px;font-weight:600;border:1px solid {t['line']}}}
  .tag--rec{{background:{t['accent']};color:#fff;border-color:{t['accent']}}}
  .rec{{background:{t['bg']};border-left:4px solid {t['accent']};border-radius:0 12px 12px 0;padding:16px 20px;margin-top:16px}}
  .cta{{text-align:center;margin-top:22px;padding-top:18px;border-top:2px dashed {t['line']}}}
  .btn{{display:inline-block;background:{t['accent']};color:#fff;padding:12px 30px;border-radius:999px;font-weight:700}}
  .disc{{font-size:10px;color:{t['muted']};margin-top:20px;border-top:1px solid {t['line']};padding-top:10px}}
  .ps-shot{{max-width:250px;border:1px solid {t['line']};border-radius:8px}}
"""


# ── Capas personalizadas (3 estilos) ──────────────────────────────────────────

def _cover(estilo, lead, d, t):
    nome = esc(_nome_curto(lead["nome"]))
    cat = esc((lead.get("categoria") or "clínicas").lower())
    regiao = esc(_regiao(lead))
    hoje = _data_pt()
    ref = f"{lead['id']:04d}"
    pos = lead.get("posicao")
    # se a IA escreveu a abertura (que já traz os números), o teaser vira um fecho
    # curto, evita repetir a mesma informação duas vezes na capa.
    if d.get("ia", {}).get("abertura"):
        teaser = "Nas próximas páginas, o que está acontecendo e como corrigir."
    elif pos and pos <= 3:
        # "dá para mudar isso" para quem já está em 1º é a primeira frase do
        # documento contradizendo o dado logo abaixo dela
        teaser = (f"Quando alguém procura {cat} em {regiao}, a sua clínica já "
                  f"aparece em <b>{pos}º lugar</b>. O que está fora do lugar é o "
                  f"resto do perfil.")
    elif pos:
        teaser = (f"Hoje, quando alguém procura {cat} em {regiao}, a sua clínica aparece em "
                  f"<b>{pos}º lugar</b>. Dá para mudar isso.")
    else:
        teaser = f"Um raio-x de como a {nome} aparece hoje para quem procura {cat} em {regiao}."

    # Negar que é modelo pronto planta a ideia de modelo pronto: o leitor não tinha
    # pensado nisso até a gente levantar a hipótese, e a frase é longa e genérica
    # justamente onde precisa ser curta.
    pessoal = d.get("ia", {}).get("abertura") or (
        f"Olá, equipe da {nome}. Olhamos como a clínica aparece hoje para quem "
        f"procura {cat} na região, e reunimos aqui o que encontramos.")
    pessoal = esc(pessoal)

    if t["cover"] == "editorial":  # capa A, Moldura
        ini = esc(_iniciais(lead["nome"]))
        return f"""<div class="cover cov-moldura">
          <div class="mold-frame"></div><div class="mold-frame2"></div>
          <div class="mold-wrap">
            <div class="mold-mono">{ini}</div>
            <div class="kick">Preparado exclusivamente para</div>
            <h1 class="mold-nome">{nome}</h1>
            <div class="mold-orn"><i></i><b>✦</b><i></i></div>
            <div class="mold-sub">{cat.capitalize()} · {regiao}</div>
            <p class="mold-pessoal">{pessoal}</p>
            <p class="mold-teaser">{teaser}</p>
          </div>
          <div class="mold-foot"><span>Diagnóstico &amp; Plano de Crescimento</span>
            <span>Análise nº {ref} · {hoje}</span></div>
        </div>"""
    if t["cover"] == "gradiente":
        return f"""<div class="cover cov-grad">
          <div class="kick">Dossiê exclusivo · {regiao}</div>
          <h1 class="cov-nome">{nome}</h1>
          <p class="cov-pessoal">{pessoal}</p>
          <div class="cov-teaserbox">{teaser}</div>
          <div class="cov-mini">
            <div><b>{pos or '-'}º</b><span>no Google Maps</span></div>
            <div><b>{_money(d['roi'])}</b><span>escapando/mês</span></div>
            <div><b>{lead.get('avaliacao_num') or '-'}</b><span>nota atual</span></div>
          </div>
          <div class="cov-foot">Análise nº {ref} · {hoje} · Captação • Confiança • Conversão</div>
        </div>"""
    # dark / premium
    return f"""<div class="cover cov-dark">
      <div class="kick">Análise confidencial preparada para</div>
      <h1 class="cov-nome">{nome}</h1>
      <div class="cov-accentline"></div>
      <div class="cov-sub">{cat.capitalize()} · {regiao}</div>
      <p class="cov-pessoal">{pessoal}</p>
      <div class="cov-teaserbox">{teaser}</div>
      <div class="cov-foot"><span>Diagnóstico &amp; Plano de Crescimento</span>
        <span>Nº {ref} · {hoje}</span></div>
    </div>"""


def _cover_css(t):
    return f"""
  /* área útil da página com a margem de 14mm em cima e embaixo */
  .cover{{height:269mm;padding:40px 50px;display:flex;flex-direction:column;position:relative}}
  .cover .kick{{text-transform:uppercase;letter-spacing:.16em;font-size:12px;opacity:.8}}
  .cover .cov-nome{{font-size:44px;line-height:1.08;margin:10px 0;font-weight:800}}
  .cover .cov-sub{{font-size:15px;opacity:.8;margin-bottom:8px}}
  .cover .cov-pessoal{{font-size:16px;line-height:1.6;max-width:80%;margin:18px 0}}
  .cover .cov-teaser,.cover .cov-teaserbox{{font-size:15px}}
  .cover .cov-foot{{margin-top:auto;display:flex;justify-content:space-between;font-size:12.5px;opacity:.8;border-top:1px solid rgba(0,0,0,.12);padding-top:16px}}
  /* capa A, Moldura (editorial emoldurada) */
  .cov-moldura{{padding:0;display:block;position:relative;background:#f6f1e8;color:#2c2620}}
  .cov-moldura .mold-frame{{position:absolute;inset:12mm;border:1.5px solid #a97f4b}}
  .cov-moldura .mold-frame2{{position:absolute;inset:14mm;border:1px solid rgba(169,127,75,.45)}}
  .cov-moldura .mold-wrap{{position:absolute;inset:14mm;display:flex;flex-direction:column;
    align-items:center;justify-content:center;text-align:center;padding:0 26mm}}
  .cov-moldura .kick{{color:#a97f4b;letter-spacing:.22em;opacity:1}}
  .cov-moldura .mold-mono{{width:74px;height:74px;border:2px solid #a97f4b;border-radius:50%;
    display:flex;align-items:center;justify-content:center;font-family:Georgia,serif;font-size:28px;
    color:#a97f4b;margin-bottom:22px}}
  .cov-moldura .mold-nome{{font-family:Georgia,serif;font-size:46px;font-weight:700;margin:14px 0;color:#2c2620}}
  .cov-moldura .mold-orn{{display:flex;align-items:center;gap:12px;color:#a97f4b;margin:6px 0 16px}}
  .cov-moldura .mold-orn i{{display:block;width:60px;height:1px;background:#a97f4b}}
  .cov-moldura .mold-sub{{color:#8a7d6a;font-size:15px}}
  .cov-moldura .mold-pessoal{{color:#5c5344;font-size:15px;line-height:1.65;max-width:135mm;margin:18px 0}}
  .cov-moldura .mold-teaser{{color:#7c5a30;font-weight:600;font-size:15px;max-width:130mm}}
  .cov-moldura .mold-foot{{position:absolute;left:20mm;right:20mm;bottom:19mm;display:flex;
    justify-content:space-between;font-size:12px;color:#8a7d6a}}
  /* gradiente */
  .cov-grad{{background:linear-gradient(150deg,#0f2f43,#17607e 70%);color:#fff}}
  .cov-grad .cov-nome{{font-size:46px}}
  .cov-grad .cov-teaserbox{{background:rgba(255,255,255,.12);border-radius:12px;padding:14px 18px;max-width:82%}}
  .cov-grad .cov-mini{{display:flex;gap:26px;margin-top:26px}}
  .cov-grad .cov-mini b{{display:block;font-size:26px;color:#58d0a0}}
  .cov-grad .cov-mini span{{font-size:12px;opacity:.8}}
  .cov-grad .cov-foot{{border-top-color:rgba(255,255,255,.25)}}
  /* dark */
  .cov-dark{{background:radial-gradient(120% 100% at 20% 0%,#1b2430,#0d1016);color:#e8edf3}}
  .cov-dark .cov-accentline{{height:3px;width:90px;background:{THEMES[3]['accent']};margin:8px 0 12px}}
  .cov-dark .cov-nome{{background:linear-gradient(90deg,#e8edf3,#9fb0c2);-webkit-background-clip:text;background-clip:text;color:transparent}}
  .cov-dark .cov-teaserbox{{background:rgba(52,211,153,.1);border:1px solid rgba(52,211,153,.3);border-radius:12px;padding:14px 18px;max-width:82%;color:#c9f5e4}}
  .cov-dark .cov-foot{{border-top-color:rgba(255,255,255,.14)}}
"""


# ── Corpo (compartilhado, temável) ────────────────────────────────────────────

# ── Plano de ação ─────────────────────────────────────────────────────────────
# O documento mostrava os erros, o custo de não agir e os três planos com preço,
# mas em nenhum momento dizia O QUE SERIA FEITO.
PASSOS_POR_PROBLEMA = {
    "posicao": [
        ("Perfil do Google reescrito e categorizado",
         "Categoria, serviços, área de atendimento e horários preenchidos do jeito "
         "que o Google usa para decidir quem mostrar primeiro.",
         "A clínica passa a ser considerada nas buscas em que hoje ela nem entra."),
        ("Fotos e informações atualizadas todo mês",
         "Perfil parado perde posição para perfil ativo. A atualização é contínua, "
         "não uma arrumação única.",
         "O perfil deixa de envelhecer sozinho enquanto o concorrente mexe no dele."),
    ],
    "sem_site": [
        ("Criação do site da clínica",
         "Páginas de serviços, equipe, convênios, localização e botão de WhatsApp, "
         "ligado ao perfil do Google.",
         "Quem acha a clínica no mapa consegue conhecer o trabalho e agendar sem ligar."),
        ("Conteúdo no blog do site, publicado com regularidade",
         "Textos sobre os procedimentos que a clínica realmente faz, escritos para as "
         "dúvidas que o paciente digita antes de escolher.",
         "A clínica passa a aparecer também em quem busca o procedimento, não só o nome."),
    ],
    "sem_instagram": [
        ("Instagram vinculado ao perfil do Google",
         "A ponte entre quem encontra no mapa e quem quer ver o trabalho antes de marcar.",
         "O paciente confere casos e rotina da clínica sem sair do caminho."),
        ("Calendário de postagens",
         "Publicações no ritmo que a clínica consegue sustentar, com os casos e o "
         "dia a dia que ela já tem.",
         "O perfil deixa de parecer abandonado para quem chega nele."),
    ],
    "nota_baixa": [
        ("Rotina de pedido de avaliação",
         "Um jeito simples de pedir avaliação ao paciente satisfeito, no momento em "
         "que ele está mais propenso a deixar.",
         "A nota sobe pela via legítima, com paciente real."),
        ("Resposta a todas as avaliações, boas e ruins",
         "Resposta pública, sem discussão, mostrando como a clínica trata quem reclama.",
         "Quem lê antes de escolher vê uma clínica que responde, não uma que ignora."),
    ],
}

# Trabalho que não nasce de um problema específico e vale para qualquer clínica.
PASSOS_CONTINUOS = [
    ("Acompanhamento da posição e da concorrência",
     "Medição mensal de onde a clínica aparece e do que mudou em volta dela.",
     "Você passa a saber se está subindo ou descendo, em vez de supor."),
    ("Relatório mensal do que foi feito e do que mudou",
     "Uma página com o que saiu, o que mexeu e o que vem no mês seguinte.",
     "Fica claro no que o investimento foi aplicado."),
]


def _plano_de_acao(problemas):
    """Os próximos passos, derivados dos problemas encontrados neste lead."""
    vistos, imediatos = set(), []
    for p in problemas:
        for passo in PASSOS_POR_PROBLEMA.get(p.get("chave"), []):
            if passo[0] in vistos:
                continue
            vistos.add(passo[0])
            imediatos.append(passo)
    return imediatos, PASSOS_CONTINUOS


def _body(lead, d, t):
    nome = esc(_nome_curto(lead["nome"]))
    sim = d["sim"]
    problemas = d["problemas"]
    nivel = d["nivel"]
    fcfg = config.FAIXAS_PRECO

    # o que cada elemento ausente custa (informativo, sem prometer retorno)
    pos = lead.get("posicao")
    itens_impacto = [
        ("Posição no Google Maps", bool(pos and pos <= 3),
         (f"Em {pos}º lugar: o paciente não rola até lá, escolhe nos primeiros da lista."
          if pos and pos > 3 else "Aparece entre os primeiros, que é onde o paciente escolhe.")),
        ("Site no perfil do Google", bool(lead.get("tem_site")),
         "Sem site no perfil, o paciente não vê serviços, fotos nem como agendar."
         if not lead.get("tem_site") else "O paciente consegue conhecer a clínica antes de ligar."),
        ("Instagram vinculado ao Google", bool(lead.get("tem_instagram")),
         "Quem acha a clínica no Maps não chega às redes, onde veria casos e rotina."
         if not lead.get("tem_instagram") else "Do Maps o paciente chega às redes e vê o trabalho."),
        ("Reputação visível (nota)", lead.get("avaliacao_num") is not None,
         "Sem nota no perfil, falta o sinal de confiança que o paciente procura."
         if lead.get("avaliacao_num") is None
         else f"Nota {lead.get('avaliacao_num')} exibida no perfil, sinal de confiança."),
        # `_reviews_validas` devolve None tanto para "não capturamos" quanto para zero,
        # e a linha aqui dizia "Poucas avaliações visíveis" nos dois casos.
        ("Volume de avaliações", bool(_reviews_validas(lead)),
         (f"{_br(_reviews_validas(lead))} avaliações sustentam a decisão do paciente."
          if _reviews_validas(lead)
          # o card corta o texto por altura, sem avisar: a primeira versão desta
          # frase tinha 117 caracteres e saiu no PDF como "é o que decid". As
          # outras linhas deste bloco ficam em ~75, e este é o orçamento real.
          else ("Não foi possível ler o número de avaliações no perfil."
                if lead.get("avaliacao_num") is not None
                else "Poucas avaliações visíveis: o paciente compara e escolhe "
                     "quem tem mais provas."))),
    ]

    # bloco de leitura geral + achados da busca na web (vem da análise por IA)
    ia = d.get("ia", {})
    leitura_block = ""
    if ia.get("leitura_geral") or ia.get("achados_busca"):
        fortes = "".join(f"<li>{esc(x)}</li>" for x in (ia.get("pontos_fortes") or [])[:3])
        fortes_html = (f'<div style="margin-top:10px"><b>A favor da clínica:</b>'
                       f'<ul style="margin:4px 0 0 18px;font-size:12.5px;color:{t["muted"]}">'
                       f'{fortes}</ul></div>') if fortes else ""
        achados = (f'<p style="margin-top:10px"><b>O que a busca na web mostrou:</b> '
                   f'{esc(ia["achados_busca"])}</p>') if ia.get("achados_busca") else ""
        leitura_block = (
            f'<div class="sec"><h2>O que um paciente encontra hoje</h2><div class="rule"></div>'
            f'<div class="box"><p>{esc(ia.get("leitura_geral", ""))}</p>{achados}{fortes_html}</div></div>')

    # Clínica já entre as primeiras: a conta por CTR dá zero e NÃO existe perda por
    # posição a cobrar.
    sem_perda = not diag_mod.perda_apurada(lead)[3]

    # recomendação: IA quando disponível, senão o texto determinístico
    if d.get("ia", {}).get("recomendacao"):
        rec_txt = esc(d["ia"]["recomendacao"])
    elif sem_perda:
        rec_txt = (
            f"Para a <b>{nome}</b>, indicamos o <b>Nível {nivel}, "
            f"{esc(fcfg[nivel]['nome'])}</b>. A clínica já ganhou a parte difícil, "
            f"que é aparecer na frente. O trabalho aqui é fazer o perfil sustentar "
            f"isso, para quem chega não voltar para a busca.")
    else:
        rec_txt = (
            f"Para a <b>{nome}</b>, indicamos o <b>Nível {nivel}, "
            f"{esc(fcfg[nivel]['nome'])}</b>, porque ele ataca os pontos encontrados "
            f"acima. O primeiro passo é estancar a perda de "
            f"<b>{_money(d['roi'])} por mês</b>. A reputação se constrói depois, "
            f"com o tempo.")

    def _prob_html(i, p):
        imp = (f'<p class="prob-imp"><b>O que isso causa:</b> {esc(p["impacto"])}</p>'
               if p.get("impacto") else "")
        return (f'<div class="prob"><div class="prob-n">{i}</div><div>'
                f'<h3>{esc(p["titulo"])}</h3><p>{esc(p["texto"])}</p>{imp}</div></div>')

    probs = "".join(_prob_html(i, p) for i, p in enumerate(problemas, 1))

    # Seção "Para onde vão os cliques", no lugar da comparação com uma clínica de
    # referência.
    versus = ""
    pos_lead = lead.get("posicao")
    if pos_lead:
        import estimativa
        alvo = getattr(config, "POSICAO_ALVO", 3)
        ctr_lead = estimativa.ctr_da_posicao(pos_lead)
        ctr_alvo = estimativa.ctr_da_posicao(alvo)
        if pos_lead <= alvo:
            legenda = (f"A {nome} aparece em <b>{pos_lead}º</b>, dentro das primeiras "
                       f"posições, que concentram a maior parte dos cliques de quem "
                       f"procura {esc((lead.get('categoria') or 'clínicas').lower())} "
                       f"na região.")
            corpo = ("Estar aqui é o que separa quem é encontrado de quem não é. O "
                     "trabalho, neste caso, é o perfil sustentar a posição: quem "
                     "chega precisa achar o que procura, ou volta para a lista e "
                     "escolhe o seguinte.")
        else:
            vezes = (ctr_alvo / ctr_lead) if ctr_lead else 0
            comp = (f"cerca de <b>{vezes:.0f} vezes mais</b>" if vezes >= 2
                    else "consideravelmente mais")
            pct = lambda v: f"{v*100:.1f}".replace(".", ",") + "%"
            legenda = (f"Quem procura no Maps quase não passa das primeiras opções. "
                       f"A {nome} está em <b>{pos_lead}º</b>, que recebe "
                       f"<b>{pct(ctr_lead)}</b> dos cliques; a {alvo}ª posição "
                       f"recebe <b>{pct(ctr_alvo)}</b>.")
            corpo = (f"Não é uma diferença de grau, é de ordem: a {alvo}ª posição "
                     f"recebe {comp} do que a sua hoje. É desta curva que sai a "
                     f"estimativa deste documento. Ela não projeta vendas: conta "
                     f"quantas pessoas chegam a ver a clínica.")
        # clínica já dentro do alvo não recebe marcação de meta: não há para
        # onde subir dentro da régua do produto, e marcar a 3ª posição para quem
        # está em 2º é apontar uma meta que é retrocesso
        alvo_no_grafico = alvo if pos_lead > alvo else None
        # o título não termina em "quem procura": pendurado assim ele emendava
        # na legenda seguinte no texto extraído do PDF, e a leitura corrida
        # ficava "quem procura A Clínica X aparece em 1º"
        versus = (f'<div class="sec"><h2>Para onde vão os cliques da busca</h2>'
                  f'<div class="rule"></div>'
                  f'<p class="cap">{legenda}</p>'
                  f'{dp.svg_curva_cliques(pos_lead, alvo_no_grafico, estimativa.ctr_da_posicao)}'
                  f'<div class="box" style="margin-top:12px">'
                  f'<p style="font-size:12.5px">{corpo}</p></div></div>')

    ps = d["pagespeed"]
    ps_block = ""
    if ps and ps.get("score") is not None:
        m = ps.get("metrics", {})
        shot = f'<img class="ps-shot" src="{ps["screenshot"]}">' if ps.get("screenshot") else ""
        ps_block = (f'<div class="sec"><h2>⚡ Velocidade do site (Google PageSpeed)</h2><div class="rule"></div>'
                    f'<div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">'
                    f'<div style="font-size:40px;font-weight:800" class="accent">{ps["score"]}</div>'
                    f'<div style="font-size:12px">LCP {esc(m.get("LCP","?"))} · CLS {esc(m.get("CLS","?"))}</div>'
                    f'{shot}</div></div>')

    if sem_perda:
        kpis_perda = (
            f'<div class="kpi"><div class="v accent">{esc(fcfg[nivel]["nome"])}</div>'
            f'<div class="l">plano indicado</div></div>')
        callout = (
            f'<div class="callout"><div style="font-size:12px;opacity:.9">Sobre o alcance hoje</div>'
            f'<div class="big" style="font-size:18px">A posição já está boa</div>'
            f'<div style="font-size:12px;opacity:.8">O que trava agora é o que o '
            f'perfil mostra a quem chega nele</div></div>')
        custo_block = ""
        demora_block = (
            f'<div class="sec"><h2>Por que isso ainda importa</h2><div class="rule"></div>'
            f'<div class="box"><p style="font-size:12.5px">Aparecer primeiro resolve '
            f'metade do problema: leva o paciente até o seu perfil. A outra metade '
            f'é o que ele encontra ali. Um perfil incompleto devolve para a busca '
            f'quem já tinha chegado até você, e essa perda não aparece em relatório '
            f'nenhum, porque o paciente nunca chegou a ligar.</p></div></div>')
        resumo_cap = (
            f"Este documento mostra como a {nome} aparece hoje para quem procura "
            f"{esc((lead.get('categoria') or 'clínicas').lower())} na sua região e o que "
            f"dá para corrigir. A clínica já está bem posicionada na busca, então "
            f"aqui não há estimativa de perda por posição: o foco é o que o perfil "
            f"deixa de mostrar.")
        # O rodapé precisa bifurcar junto.
        disc_txt = (
            "Este documento não traz estimativa de perda por posição, porque a "
            "clínica já aparece entre as primeiras na busca. O que está descrito "
            "aqui é o que o perfil deixa de mostrar a quem chega. Corrigir "
            "presença digital é um processo: o Google leva semanas para reconhecer "
            "mudanças e a reputação se constrói ao longo do tempo. O ritmo dos "
            "resultados varia conforme execução, concorrência e mercado local.")
    else:
        kpis_perda = (
            f'<div class="kpi"><div class="v" style="color:{t["red"]}">{d["perdidos"]}</div><div class="l">pacientes/mês escapando</div></div>'
            f'<div class="kpi"><div class="v" style="color:{t["red"]}">{_money(d["roi"])}</div><div class="l">deixa de receber/mês</div></div>'
            f'<div class="kpi"><div class="v" style="color:{t["red"]}">{_money(d["roi"]*3)}</div><div class="l">em 3 meses parado</div></div>'
            f'<div class="kpi"><div class="v" style="color:{t["red"]}">{_money(d["roi"]*12)}</div><div class="l">em 1 ano parado</div></div>'
            f'<div class="kpi"><div class="v accent">{esc(fcfg[nivel]["nome"])}</div><div class="l">plano indicado</div></div>')
        callout = (
            f'<div class="callout"><div style="font-size:12px;opacity:.9">Estimativa do que deixa de entrar hoje</div>'
            f'<div class="big">{_money(d["roi"])}/mês</div>'
            f'<div style="font-size:12px;opacity:.8">≈ {_money(d["roi"]*12)} em um ano</div></div>')
        custo_block = (
            f'<div class="sec"><h2>📉 O custo de não fazer nada</h2><div class="rule"></div>'
            f'<p class="cap">Perda acumulada mês a mês, se seguir como está (12 meses):</p>'
            f'{dp.svg_perda(sim["perda_acumulada"], sim["meses"])}</div>')
        demora_block = (
            f'<div class="sec"><h2>O preço de deixar para depois</h2><div class="rule"></div>'
            f'<p class="cap">A perda não para enquanto a situação continua igual. Cada mês '
            f'parado soma {_money(d["roi"])} que não entra no caixa.</p>'
            f'{dp.svg_custo_demora(d["roi"])}'
            f'<div class="box" style="margin-top:14px"><p style="font-size:12.5px">Corrigir a '
            f'presença digital não é instantâneo. O Google leva algumas semanas para reconhecer '
            f'as mudanças no perfil, e a reputação se constrói avaliação por avaliação. Por isso '
            f'o custo real não é só o valor do plano: é o tempo em que a clínica segue perdendo '
            f'pacientes enquanto nada muda.</p></div></div>')
        resumo_cap = (
            f"Este documento mostra como a {nome} aparece hoje para quem procura "
            f"{esc((lead.get('categoria') or 'clínicas').lower())} na sua região, o que isso "
            f"causa no dia a dia da clínica e o que dá para corrigir. Os números abaixo são "
            f"estimativas calculadas a partir da sua posição no Google, da sua reputação e do "
            f"ticket médio do seu nicho.")
        disc_txt = (
            "Os valores deste documento são <b>estimativas do que a clínica deixa "
            "de receber</b>, calculadas a partir da posição atual no Google, da "
            "reputação visível e do ticket médio do nicho. Não são promessa de "
            "retorno nem de lucro. Corrigir presença digital é um processo: o Google "
            "leva semanas para reconhecer mudanças e a reputação se constrói ao longo "
            "do tempo. O ritmo dos resultados varia conforme execução, concorrência "
            "e mercado local.")

    # próximos passos: o que será feito e o que a clínica ganha com cada coisa
    imediatos, continuos = _plano_de_acao(problemas)

    def _passo_html(i, passo, cor):
        titulo, oque, ganho = passo
        return (f'<div class="passo">'
                f'<div class="passo-n" style="background:{cor}">{i}</div>'
                f'<div class="passo-txt">'
                f'<b>{esc(titulo)}</b>'
                f'<p style="margin:3px 0 0">{esc(oque)}</p>'
                f'<p class="passo-ganho">O que muda: {esc(ganho)}</p>'
                f'</div></div>')

    blocos = ""
    if imediatos:
        blocos += (f'<p class="passo-fase">Primeiro, corrigir o que foi encontrado</p>'
                   + "".join(_passo_html(i, p, t["accent"])
                             for i, p in enumerate(imediatos, 1)))
    inicio = len(imediatos) + 1
    blocos += (f'<p class="passo-fase">Depois, manter e acompanhar</p>'
               + "".join(_passo_html(i, p, t["accent2"])
                         for i, p in enumerate(continuos, inicio)))
    passos_html = blocos

    niveis = ""
    marc = {1: dp.COR[1], 2: dp.COR[2], 3: dp.COR[3]}
    for n in (1, 2, 3):
        f = fcfg[n]
        resolve = [p["titulo"] for p in problemas if PROBLEMA_META.get(p["chave"], {}).get("nivel", 1) <= n]
        li = "".join(f"<li>{esc(x)}</li>" for x in resolve) or "<li>Base de presença</li>"
        badge = '<span class="tag">⭐ Mais escolhido</span>' if f["mais_vendido"] else ""
        rec = ' nivel--rec' if n == nivel else ""
        rtag = '<span class="tag tag--rec">Recomendado</span>' if n == nivel else ""
        entregas = config.ENTREGAS_POR_NIVEL.get(n, {})
        inc = "".join(f"<li>{esc(x)}</li>" for x in entregas.get("itens", []))
        niveis += (f'<div class="nivel{rec}">'
                   f'<div class="nivel-h"><span class="nivel-nome">Nível {n} · {esc(f["nome"])}</span>{badge}{rtag}</div>'
                   f'<div class="nivel-preco">R$ {_br(f["min"])}–{_br(f["max"])}<span>/mês</span></div>'
                   f'<p>{esc(NIVEL_DESC[n]["resumo"])}</p>'
                   f'<div class="nivel-inc"><b>Inclui</b><ul>{inc}</ul></div>'
                   f'<div style="font-size:11.5px;color:{t["muted"]};margin-top:6px">Resolve: <ul style="margin-left:16px">{li}</ul></div></div>')

    # serviços fora dos planos: sem isto o leitor supõe que tráfego pago entra
    # em algum nível, e a conversa de fechamento começa com uma correção
    extras = "".join(
        f'<div class="extra"><div class="extra-h"><b>{esc(s["nome"])}</b>'
        f'<span class="extra-preco">R$ {_br(s["preco"])}<span>/mês</span></span></div>'
        f'<p>{esc(s["desc"])}</p></div>'
        for s in config.SERVICOS_EXTRAS)
    extras_html = (f'<div class="sec"><h2>À parte dos planos</h2><div class="rule"></div>'
                   f'{extras}</div>') if extras else ""

    return f"""
<section class="sheet quebra">
  <h2>Resumo executivo</h2><div class="rule"></div>
  <p class="cap" style="margin-bottom:14px">{resumo_cap}</p>
  <div style="display:flex;gap:18px;align-items:center;margin-bottom:8px">
    <div style="text-align:center">{dp.svg_gauge(lead['score'] if lead.get('score') is not None else 0)}</div>
    <div class="kpis" style="flex:1">
      <div class="kpi"><div class="v" style="color:{t['red']}">{lead.get('posicao') or '-'}º</div><div class="l">posição no Google</div></div>
      {kpis_perda}
    </div>
  </div>
  {leitura_block}
</section>

<section class="sheet">
  <div class="sec"><h2>🔍 O que encontramos</h2><div class="rule"></div>{probs}</div>
  {callout}
</section>

<section class="sheet">
  {versus}
  {ps_block}
  {custo_block}
</section>

<section class="sheet">
  {demora_block}

  <div class="sec"><h2>O que falta e o que cada peça faz</h2><div class="rule"></div>
    <p class="cap">Cada elemento ausente fecha uma porta de entrada de pacientes.</p>
    {dp.svg_impacto_elementos(itens_impacto)}
  </div>
</section>

<section class="sheet">
  <div class="sec"><h2>🛠 Os próximos passos</h2><div class="rule"></div>
    <p class="cap">O que entra em execução, na ordem, e o que muda na clínica em cada ponto.</p>
    {passos_html}
  </div>
</section>

<section class="sheet">
  <h2>✅ Como resolvemos, 3 caminhos</h2><div class="rule"></div>
  <div class="niveis">{niveis}</div>
  <div class="rec"><h2 style="font-size:15px">🎯 Nossa recomendação</h2>
    <p style="margin-top:4px">{rec_txt}</p></div>
  {extras_html}
  <div class="cta"><p style="margin-bottom:12px;color:{t['muted']}">Quer ver como fica na prática, sem compromisso?</p>
    <span class="btn">Falar com a nossa equipe</span></div>
  <div class="disc">{disc_txt}</div>
</section>"""


def montar(estilo: int, lead: dict) -> str:
    t = THEMES[estilo]
    d = _dados(lead)
    return (f"<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'>"
            f"<title>Proposta, {esc(lead['nome'])}</title>"
            f"<style>{_css(t)}{_cover_css(t)}</style></head><body>"
            f"{_cover(estilo, lead, d, t)}{_body(lead, d, t)}</body></html>")


def gerar_variante(lead: dict, estilo: int):
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    html = montar(estilo, lead)
    slug = _slug(lead["nome"])
    nome_arq = f"design{estilo}_{THEMES[estilo]['cover']}_{slug}"
    (config.OUT_DIR / f"{nome_arq}.html").write_text(html, encoding="utf-8")
    pdf = config.OUT_DIR / f"{nome_arq}.pdf"
    asyncio.run(dp._html_para_pdf(html, pdf))
    return pdf


def gerar_todas(lead_id: int):
    lead = db.get_lead(lead_id)
    if not lead:
        print("lead não encontrado")
        return []
    saidas = []
    for estilo in (1, 2, 3):
        p = gerar_variante(lead, estilo)
        saidas.append(p)
        print(f"✅ Estilo {estilo} ({THEMES[estilo]['cover']}): {p.name}")
    return saidas


# estilo oficial adotado (capa A / editorial). Usado pela esteira e pelo dashboard.
ESTILO_PADRAO = 1


def gerar_final(lead_id: int, estilo: int = ESTILO_PADRAO):
    """
    Gera o material OFICIAL como out/proposta_<slug>.pdf (nome esperado pela esteira).
    """
    lead = db.get_lead(lead_id)
    if not lead:
        print("lead não encontrado")
        return None
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    html = montar(estilo, lead)
    slug = _slug(lead["nome"])
    (config.OUT_DIR / f"proposta_{slug}.html").write_text(html, encoding="utf-8")
    pdf = config.OUT_DIR / f"proposta_{slug}.pdf"
    asyncio.run(dp._html_para_pdf(html, pdf))
    print(f"✅ Material: {pdf.name}")
    return pdf


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "--final":
        gerar_final(int(sys.argv[2]))
    elif len(sys.argv) > 1:
        gerar_todas(int(sys.argv[1]))
    else:
        print("uso: python proposta_designs.py <lead_id>  |  --final <lead_id>")
