"""capa_designs.py — 3 variações de CAPA (estilo editorial, papel creme)."""

import asyncio
import re
from datetime import date

import config
import db
import documento_pdf as dp

esc = dp.esc

# paleta editorial (papel creme + bronze)
CREME = "#f6f1e8"
PAPEL = "#fbf8f2"
INK = "#2c2620"
BRONZE = "#a97f4b"
BRONZE2 = "#7c5a30"
MUTED = "#8a7d6a"
LINHA = "#e6dccb"
SERIF = "Georgia, 'Times New Roman', serif"

_MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
          "agosto", "setembro", "outubro", "novembro", "dezembro"]
_STOP = {"clinica", "clínica", "consultorio", "consultório", "odontologia", "odontologica",
         "odontológica", "dr", "dra", "de", "da", "do", "e", "centro", "dental", "instituto",
         "estetica", "estética", "saude", "saúde"}


def _data_pt():
    d = date.today()
    return f"{d.day} de {_MESES[d.month - 1]} de {d.year}"


def _regiao(lead):
    """Delega para a versão de proposta_designs."""
    from proposta_designs import _regiao as _r
    return _r(lead)


def _iniciais(nome):
    palavras = [w for w in re.findall(r"[A-Za-zÀ-ÿ]+", nome) if w.lower() not in _STOP]
    if not palavras:
        palavras = re.findall(r"[A-Za-zÀ-ÿ]+", nome)
    ini = "".join(w[0] for w in palavras[:2]).upper()
    return ini or (nome[:2].upper())


def _nome_curto(nome: str) -> str:
    """O nome sem a cauda de SEO do Maps."""
    curto = re.split(r"\s+[|\-–—]\s+", (nome or "").strip())[0].strip()
    palavras = curto.split()
    return " ".join(palavras[:5]) if len(palavras) > 5 else (curto or nome)


def _ctx(lead):
    cat = (lead.get("categoria") or "clínicas").lower()
    regiao = _regiao(lead)
    pos = lead.get("posicao")
    curto = _nome_curto(lead.get("nome") or "")

    # Clínica no topo não tem o que "mudar" na posição, e dizer que tem é a
    # primeira frase do documento contradizendo o dado logo abaixo dela.
    if pos and pos <= 3:
        teaser = (f"Quando alguém procura {cat} em {regiao}, a sua clínica já "
                  f"aparece em {pos}º lugar. O que está fora do lugar é o resto "
                  f"do perfil.")
    elif pos:
        teaser = (f"Hoje, quando alguém procura {cat} em {regiao}, a sua clínica "
                  f"aparece em {pos}º lugar. Dá para mudar isso.")
    else:
        teaser = (f"Um raio-x de como a sua clínica aparece hoje para quem "
                  f"procura {cat} em {regiao}.")

    # Negar que é modelo pronto planta a ideia de modelo pronto: o leitor não
    # tinha pensado nisso até a gente levantar a hipótese. Abre direto pelo que
    # foi feito, sem adjetivo e sem se defender de acusação que ninguém fez.
    pessoal = (f"Olá, equipe da {curto}. Olhamos como a clínica aparece hoje "
               f"para quem procura {cat} na região, e reunimos aqui o que "
               f"encontramos.")
    return {"cat": cat, "regiao": regiao, "pos": pos, "teaser": teaser, "pessoal": pessoal,
            "iniciais": _iniciais(lead["nome"]), "data": _data_pt(),
            "ref": f"{lead['id']:04d}", "nome": curto}


_BASE = f"""
  @page{{size:A4;margin:0}}
  *{{box-sizing:border-box;margin:0;padding:0;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
  body{{font-family:'Segoe UI',Roboto,Arial,sans-serif;color:{INK}}}
  .page{{width:210mm;height:297mm;position:relative;overflow:hidden;background:{CREME}}}
  .kick{{font-size:12px;letter-spacing:.22em;text-transform:uppercase;color:{BRONZE}}}
  .nome{{font-family:{SERIF};font-weight:700;line-height:1.05;color:{INK}}}
  .sub{{color:{MUTED};font-size:15px}}
  .pessoal{{color:#5c5344;font-size:15px;line-height:1.65}}
  .teaser{{color:{BRONZE2};font-weight:600;font-size:15px;line-height:1.6}}
  .foot{{font-size:12px;color:{MUTED}}}
"""


# ── A) Moldura ────────────────────────────────────────────────────────────────
def capa_moldura(lead):
    c = _ctx(lead)
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>{_BASE}
      .frame{{position:absolute;inset:12mm;border:1.5px solid {BRONZE}}}
      .frame2{{position:absolute;inset:14mm;border:1px solid rgba(169,127,75,.45)}}
      .wrap{{position:absolute;inset:14mm;display:flex;flex-direction:column;align-items:center;
        justify-content:center;text-align:center;padding:0 26mm}}
      .mono{{width:74px;height:74px;border:2px solid {BRONZE};border-radius:50%;display:flex;
        align-items:center;justify-content:center;font-family:{SERIF};font-size:28px;color:{BRONZE};margin-bottom:22px}}
      .nome{{font-size:46px;margin:14px 0}}
      .orn{{display:flex;align-items:center;gap:12px;color:{BRONZE};margin:6px 0 16px}}
      .orn i{{display:block;width:60px;height:1px;background:{BRONZE}}}
      .orn b{{font-size:12px}}
      .pessoal{{max-width:135mm;margin:18px 0}}
      .teaser{{max-width:130mm}}
      .foot{{position:absolute;left:20mm;right:20mm;bottom:19mm;display:flex;justify-content:space-between}}
    </style></head><body><div class='page'>
      <div class='frame'></div><div class='frame2'></div>
      <div class='wrap'>
        <div class='mono'>{esc(c['iniciais'])}</div>
        <div class='kick'>Preparado exclusivamente para</div>
        <h1 class='nome'>{esc(c['nome'])}</h1>
        <div class='orn'><i></i><b>✦</b><i></i></div>
        <div class='sub'>{esc(c['cat'].capitalize())} · {esc(c['regiao'])}</div>
        <p class='pessoal'>{esc(c['pessoal'])}</p>
        <p class='teaser'>{esc(c['teaser'])}</p>
      </div>
      <div class='foot'><span>Diagnóstico &amp; Plano de Crescimento</span>
        <span>Análise nº {c['ref']} · {c['data']}</span></div>
    </div></body></html>"""


# ── B) Bloco lateral ──────────────────────────────────────────────────────────
def capa_bloco(lead):
    c = _ctx(lead)
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>{_BASE}
      .page{{display:flex}}
      .side{{width:78mm;background:linear-gradient(160deg,{BRONZE},{BRONZE2});color:{CREME};
        padding:26mm 18mm;display:flex;flex-direction:column;justify-content:space-between}}
      .side .mono{{width:96px;height:96px;border:2px solid rgba(255,255,255,.6);border-radius:50%;
        display:flex;align-items:center;justify-content:center;font-family:{SERIF};font-size:38px}}
      .side .vt{{font-family:{SERIF};font-size:19px;line-height:1.4;opacity:.95}}
      .side .num{{font-size:12px;letter-spacing:.06em;opacity:.85;border-top:1px solid rgba(255,255,255,.35);padding-top:12px}}
      .main{{flex:1;padding:30mm 22mm;display:flex;flex-direction:column;justify-content:center}}
      .nome{{font-size:42px;margin:12px 0}}
      .rule{{height:3px;width:64px;background:{BRONZE};border-radius:3px;margin:10px 0 18px}}
      .pessoal{{margin-bottom:20px}}
      .teaserbox{{background:{PAPEL};border:1px solid {LINHA};border-left:4px solid {BRONZE};
        border-radius:0 10px 10px 0;padding:14px 16px}}
    </style></head><body><div class='page'>
      <div class='side'>
        <div class='mono'>{esc(c['iniciais'])}</div>
        <div class='vt'>Diagnóstico<br>&amp; Plano de<br>Crescimento</div>
        <div class='num'>Análise nº {c['ref']}<br>{c['data']}</div>
      </div>
      <div class='main'>
        <div class='kick'>Preparado exclusivamente para</div>
        <h1 class='nome'>{esc(c['nome'])}</h1>
        <div class='sub'>{esc(c['cat'].capitalize())} · {esc(c['regiao'])}</div>
        <div class='rule'></div>
        <p class='pessoal'>{esc(c['pessoal'])}</p>
        <div class='teaserbox'><p class='teaser'>{esc(c['teaser'])}</p></div>
      </div>
    </div></body></html>"""


# ── C) Emblema central ────────────────────────────────────────────────────────
def capa_emblema(lead):
    c = _ctx(lead)
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>{_BASE}
      .band{{position:absolute;top:0;left:0;right:0;height:12mm;background:linear-gradient(90deg,{BRONZE},{BRONZE2})}}
      .wrap{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
        text-align:center;padding:44mm 26mm 24mm}}
      .kick{{margin-bottom:26px}}
      .emblema{{width:150px;height:150px;border-radius:50%;background:{PAPEL};
        border:2px solid {BRONZE};display:flex;align-items:center;justify-content:center;position:relative;margin-bottom:6px}}
      .emblema::after{{content:'';position:absolute;inset:9px;border:1px solid rgba(169,127,75,.5);border-radius:50%}}
      .emblema b{{font-family:{SERIF};font-size:56px;color:{BRONZE}}}
      .nome{{font-size:44px;margin:26px 0 6px}}
      .rule{{display:flex;align-items:center;gap:10px;color:{BRONZE};margin:8px 0 4px}}
      .rule i{{width:46px;height:1px;background:{BRONZE}}}
      .sub{{margin-top:4px}}
      .quote{{margin-top:auto;max-width:140mm;font-family:{SERIF};font-style:italic;font-size:19px;
        color:{BRONZE2};line-height:1.5;position:relative;padding-top:20px}}
      .quote::before{{content:'\\201C';font-size:64px;color:rgba(169,127,75,.35);position:absolute;top:-10px;left:50%;transform:translateX(-50%)}}
      .foot{{position:absolute;left:0;right:0;bottom:16mm;text-align:center}}
    </style></head><body><div class='page'>
      <div class='band'></div>
      <div class='wrap'>
        <div class='kick'>Preparado exclusivamente para</div>
        <div class='emblema'><b>{esc(c['iniciais'])}</b></div>
        <h1 class='nome'>{esc(c['nome'])}</h1>
        <div class='rule'><i></i>✦<i></i></div>
        <div class='sub'>{esc(c['cat'].capitalize())} · {esc(c['regiao'])}</div>
        <p class='quote'>{esc(c['teaser'])}</p>
      </div>
      <div class='foot'>Diagnóstico &amp; Plano de Crescimento · Análise nº {c['ref']} · {c['data']}</div>
    </div></body></html>"""


VARIANTES = [("A_moldura", capa_moldura), ("B_bloco", capa_bloco), ("C_emblema", capa_emblema)]


async def _render(html, png):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        pg = await b.new_page(viewport={"width": 794, "height": 1123}, device_scale_factor=2)
        await pg.set_content(html, wait_until="load")
        await pg.wait_for_timeout(250)
        await pg.screenshot(path=str(png))
        await b.close()


def gerar(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        print("lead não encontrado")
        return []
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = dp._slug(lead["nome"])
    saidas = []
    for nome, fn in VARIANTES:
        html = fn(lead)
        (config.OUT_DIR / f"capa_{nome}_{slug}.html").write_text(html, encoding="utf-8")
        png = config.OUT_DIR / f"capa_{nome}_{slug}.png"
        asyncio.run(_render(html, png))
        saidas.append(png)
        print(f"✅ Capa {nome}: {png.name}")
    return saidas


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        gerar(int(sys.argv[1]))
    else:
        print("uso: python capa_designs.py <lead_id>")
