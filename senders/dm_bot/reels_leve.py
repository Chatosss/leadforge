# -*- coding: utf-8 -*-
"""reels_leve.py — Reels do aquecimento na MENOR qualidade e SEM áudio."""

import asyncio
import random
import base64
import logging
import re
import time

logger = logging.getLogger(__name__)

# No HTML/JSON o "<" pode vir escrito como \u003C e a barra como \/
_LT = r"(?:<|\\u003[cC])"
_GT = r"(?:>|\\u003[eE])"           # e o ">" como \u003e (visto em 10/10, graphql)
_REPR = re.compile(_LT + r"Representation\b.*?" + _LT + r"\\?/Representation" + _GT, re.S)
_CONJUNTO = re.compile(_LT + r"AdaptationSet\b.*?" + _LT + r"\\?/AdaptationSet" + _GT, re.S)
_FIM_DA_TAG = re.compile(_GT)
_ALTURA = re.compile(r'\bheight=\\*"(\d+)')
_BANDA = re.compile(r'\bbandwidth=\\*"(\d+)')
_AUDIO = re.compile(r'contentType=\\*"audio|mimeType=\\*"audio')
# a versão "progressiva" (H.264 com áudio AAC, fora do manifesto) que o player
# usa para começar o PRIMEIRO vídeo rápido: esta janela não a toca, e o Reel de
# abertura ficava no erro. Sem ela, o player usa o manifesto já reduzido.
_PROGRESSIVO = re.compile(r'"video_versions":\[[^\]]*\]')

# Respostas que trazem manifestos (o HTML fica de fora de propósito; ver acima).
# route-definition: o pré-carregamento da rota ao entrar no Reels pelo menu —
# traz os primeiros vídeos.
PADROES = [
    {"urlPattern": "https://www.instagram.com/api/graphql*", "requestStage": "Response"},
    {"urlPattern": "https://www.instagram.com/graphql/query*", "requestStage": "Response"},
    {"urlPattern": "https://www.instagram.com/ajax/route-definition/*", "requestStage": "Response"},
]

# Lista de pré-carregamento em JSON, ao lado do manifesto, com TODAS as
# qualidades e o áudio AAC: o player pré-carrega por ela e o vídeo dava erro
# mesmo com o manifesto já reduzido.
_PREFETCH = '"all_video_dash_prefetch_representations":'


def _sem_prefetch(texto):
    """Troca cada lista de pré-carregamento por []. Devolve (texto, quantas)."""
    partes, i, busca, n = [], 0, 0, 0
    while True:
        k = texto.find(_PREFETCH, busca)
        if k < 0:
            break
        a = k + len(_PREFETCH)
        while a < len(texto) and texto[a] in " \t\r\n":
            a += 1
        fim = _fecho_do_colchete(texto, a) if a < len(texto) and texto[a] == "[" else -1
        if fim < 0:
            busca = a                   # fora do formato (null, por ex.): esta fica, segue
            continue
        partes.append(texto[i:a] + "[]")
        i = busca = fim + 1
        n += 1
    partes.append(texto[i:])
    return "".join(partes), n


def _fecho_do_colchete(texto, ini):
    """Posição do ']' que fecha o '[' em `ini` (respeitando strings JSON), ou -1."""
    nivel, em_texto, escape = 0, False, False
    for j in range(ini, len(texto)):
        c = texto[j]
        if em_texto:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                em_texto = False
        elif c == '"':
            em_texto = True
        elif c in "[{":
            nivel += 1
        elif c in "]}":
            nivel -= 1
            if nivel == 0:
                return j if c == "]" else -1
    return -1

# Mudo na hora do play (a janela já abre com --mute-audio e a trilha de áudio
# sai do manifesto; um vigia da página inteira só gastava processador)
JS_MUDO = r"""
(() => {
  const tocar = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    try { this.muted = true; this.volume = 0; } catch (e) {}
    return tocar.apply(this, arguments);
  };
})();
"""

# O aviso "Ativar notificações" cobre o menu e o clique no Reels não pega
JS_FECHAR_AVISO = r"""() => {
  const b = [...document.querySelectorAll('div[role=dialog] button, button')]
    .find(x => /^(agora não|not now)$/i.test((x.innerText || '').trim()));
  if (b) { b.click(); return true; }
  return false;
}"""


def menor_qualidade(texto):
    """
    O texto com cada manifesto reduzido à versão mais leve de vídeo e sem áudio. Devolve
    (texto, versões cortadas, trilhas de áudio cortadas). Conjunto que não dá para
    entender fica como veio.
    """
    cortes = [0, 0]

    def no_conjunto(m):
        bloco = m.group(0)
        reps = _REPR.findall(bloco)
        cabeca = _FIM_DA_TAG.split(bloco, 1)[0]
        if _AUDIO.search(cabeca) or (reps and all(_AUDIO.search(r) for r in reps)):
            cortes[1] += 1
            return ""
        versoes = []
        for r in reps:
            h, b = _ALTURA.search(r), _BANDA.search(r)
            if h:
                versoes.append((int(h.group(1)), int(b.group(1)) if b else 0, r))
        if len(versoes) < 2:
            return bloco
        fica = min(versoes, key=lambda v: (v[0], v[1]))[2]
        for _, _, r in versoes:
            if r is not fica:
                bloco = bloco.replace(r, "", 1)
                cortes[0] += 1
        return bloco

    if "Representation" not in texto:
        # pedaço só com a lista de pré-carregamento (sem o manifesto) também limpa
        novo, n = _sem_prefetch(texto) if _PREFETCH in texto else (texto, 0)
        return novo, 0, n
    novo = _CONJUNTO.sub(no_conjunto, texto)
    if cortes[0] or cortes[1]:
        novo, n = _PROGRESSIVO.subn('"video_versions":null', novo)
        cortes[0] += n
    novo, n = _sem_prefetch(novo)
    cortes[1] += n
    return novo, cortes[0], cortes[1]


async def ligar(ctx, pagina, contagem=None):
    """Reescreve os manifestos que chegam a esta página (uma sessão CDP por página)."""
    cdp = await ctx.new_cdp_session(pagina)

    async def pausado(ev):
        rid = ev["requestId"]
        try:
            # com prazo: a resposta fica retida até aqui, e a página espera junto
            corpo = await asyncio.wait_for(cdp.send("Fetch.getResponseBody", {"requestId": rid}), 15)
            texto = (base64.b64decode(corpo["body"]).decode("utf-8", "replace")
                     if corpo.get("base64Encoded") else corpo["body"])
            texto, versoes, audios = menor_qualidade(texto)
            if not (versoes or audios):
                # nada a cortar (a maioria das respostas): segue como veio
                await cdp.send("Fetch.continueRequest", {"requestId": rid})
                return
            if contagem is not None:
                contagem["versoes"] = contagem.get("versoes", 0) + versoes
                contagem["audios"] = contagem.get("audios", 0) + audios
            cabecalhos = [h for h in ev.get("responseHeaders") or []
                          if h["name"].lower() not in ("content-length", "content-encoding")]
            await cdp.send("Fetch.fulfillRequest", {
                "requestId": rid, "responseCode": ev.get("responseStatusCode") or 200,
                "responseHeaders": cabecalhos,
                "body": base64.b64encode(texto.encode("utf-8")).decode()})
        except Exception as e:
            # sem a reescrita a resposta segue como veio: o vídeo só fica pesado
            logger.debug("reels leve: %s", e)
            try:
                await cdp.send("Fetch.continueRequest", {"requestId": rid})
            except Exception:
                pass

    cdp.on("Fetch.requestPaused", lambda ev: asyncio.ensure_future(pausado(ev)))
    await cdp.send("Fetch.enable", {"patterns": PADROES})
    try:
        await pagina.evaluate(JS_MUDO)
    except Exception:
        pass


async def ir_ao_reels(pagina, url_direct, url_reels, prazo_ms=30000):
    """Entra no Reels pelo menu, a partir da caixa do Direct (ver o cabeçalho)."""
    if "/direct/" not in (pagina.url or ""):
        try:
            await pagina.goto(url_direct, wait_until="domcontentloaded", timeout=prazo_ms)
        except Exception as e:
            # com 19 janelas carregando juntas o Direct passa de 30 s; a página
            # segue carregando, e a espera pelo menu logo abaixo cobre o resto
            logger.info("reels leve: o Direct demorou (%s); sigo esperando o menu", e)
    async def chegou(ms):
        """A página foi para o Reels pelo menu (o endereço mudou)."""
        try:
            await pagina.wait_for_url("**/reels/**", timeout=ms)
            return True
        except Exception:
            return False

    async def video_na_tela():
        # Chegou pelo menu: NUNCA recarrega pelo endereço direto, mesmo que o
        # vídeo demore (com 19 janelas carregando juntas passa de 10 s, e o
        # endereço direto traz os primeiros vídeos no HTML — esses dão erro).
        try:
            await pagina.wait_for_function("() => !!document.querySelector('video')", timeout=prazo_ms)
        except Exception:
            logger.info("reels leve: no Reels, mas o vídeo ainda não apareceu (página lenta)")

    motivo = ""
    try:
        link = pagina.locator('a[href="/reels/"]').first
        await link.wait_for(state="attached", timeout=prazo_ms)
        try:
            await pagina.evaluate(JS_FECHAR_AVISO)
        except Exception:
            pass
        # estreita) e a página nem saiu do Direct: o clique pelo elemento.
        try:
            await link.click(timeout=5000)
        except Exception:
            pass
        foi = await chegou(8000)
        if not foi and "/direct/" in (pagina.url or ""):
            await link.evaluate("a => a.click()")
            foi = await chegou(15000)
        if foi:
            await video_na_tela()
        else:
            motivo = "o menu não levou ao Reels"
    except Exception as e:
        motivo = f"menu sem o link do Reels ({e})"
    if motivo:
        logger.info("reels leve: %s; abrindo direto", motivo)
        await pagina.goto(url_reels, wait_until="domcontentloaded", timeout=prazo_ms)


# ── Passar para o próximo Reel quando o vídeo acaba ── O Reels repete o vídeo
# sem fim (não dispara "ended"): a volta é percebida pelo tempo voltando ao
# começo depois de ter passado de 70% da duração.
PARADO_S = 12          # sem o tempo andar (e sem estar pausado): passa
LONGO_S = 90           # Reel longo: no máximo isto
SEM_EFEITO_MAX = 2     # setas seguidas que não mudaram o vídeo: para de passar
PAUSA_SEM_EFEITO_S = 120

JS_VIDEO_DA_TELA = r"""() => {
  const meio = innerHeight / 2;
  let v = null, perto = 1e9;
  for (const x of document.querySelectorAll('video')) {
    const r = x.getBoundingClientRect();
    if (r.height < 80 || r.bottom < 0 || r.top > innerHeight) continue;
    const d = Math.abs((r.top + r.bottom) / 2 - meio);
    if (d < perto) { perto = d; v = x; }
  }
  if (!v) return null;
  let erro = !!v.error;
  if (!erro && (v.readyState < 2 || v.paused)) {
    // só procura o aviso de erro quando o vídeo não está tocando (ler o texto
    // da página a cada consulta faria o navegador recalcular a tela)
    let c = v;
    for (let i = 0; i < 6 && c && !erro; i++) {
      c = c.parentElement;
      if (c && /problemas para reproduzir|trouble playing|problemas al reproducir/i.test(c.innerText || '')) erro = true;
    }
  }
  return {id: (v.currentSrc || '') + '|' + (v.getAttribute('src') || ''),
          t: v.currentTime || 0, dur: isFinite(v.duration) ? v.duration : 0,
          pausado: v.paused, acabou: v.ended, oculto: document.hidden, erro};
}"""


def _meta_de_pessoa():
    """Quanto ver deste Reel: quase sempre uma volta, às vezes sai antes ou vê duas."""
    r = random.random()
    if r < 0.15:
        return ("fracao", random.uniform(0.5, 0.9))
    if r < 0.25:
        return ("voltas", 2)
    return ("voltas", 1)


def _pausa_de_pessoa():
    return random.uniform(0.6, 2.5) if random.random() < 0.85 else random.uniform(3, 8)


async def _ler_video(pagina):
    try:
        return await asyncio.wait_for(pagina.evaluate(JS_VIDEO_DA_TELA), 2)
    except Exception:
        return None


async def passar_se_acabou(pagina, rl, parar):
    """
    Consulta o vídeo da tela (chamar a cada ~2 s) e passa para o próximo se ele acabou.
    `rl`: dict de estado desta janela. `parar()`: True se a janela mudou de ideia
    (fechar, outra página). Devolve True (passou), "travou" (setas sem efeito: parou de
    passar por PAUSA_SEM_EFEITO_S) ou False.
    """
    agora = time.monotonic()
    if agora < rl.get("pausa_ate", 0):
        return False
    v = await _ler_video(pagina)
    if not v:
        return False
    if v["id"] != rl.get("id"):
        rl.update(id=v["id"], desde=agora, mexeu=agora, ult=-1.0, max=0.0, voltas=0,
                  meta=_meta_de_pessoa())
    t, dur = v["t"], v["dur"]
    if t > rl["ult"] + 0.05:
        rl["mexeu"] = agora
    if rl["ult"] > 0 and t + 0.5 < rl["ult"] and rl["max"] > max(1.0, dur * 0.7):
        rl["voltas"] += 1
    rl["max"], rl["ult"] = max(rl["max"], t), t
    tipo, alvo = rl["meta"]
    if v["erro"]:
        motivo = "erro"
    elif v["acabou"] or (tipo == "voltas" and rl["voltas"] >= alvo):
        motivo = "acabou"
    elif tipo == "fracao" and dur > 0 and t >= dur * alvo:
        motivo = "saiu antes do fim"
    elif not v["pausado"] and not v["oculto"] and agora - rl["mexeu"] > PARADO_S:
        motivo = "parado"
    elif agora - rl["desde"] > LONGO_S:
        motivo = "longo"
    else:
        return False
    await asyncio.sleep(_pausa_de_pessoa())
    # na pausa o operador pode ter rolado, pausado ou fechado: confere de novo
    if parar():
        return False
    v2 = await _ler_video(pagina)
    if not v2 or v2["id"] != rl["id"]:
        return False
    await pagina.keyboard.press("ArrowDown")
    await asyncio.sleep(3)
    v3 = await _ler_video(pagina)
    if v3 and v3["id"] == rl["id"]:
        # a seta não fez efeito (aviso aberto, foco num campo): não insiste
        rl["sem_efeito"] = rl.get("sem_efeito", 0) + 1
        if rl["sem_efeito"] >= SEM_EFEITO_MAX:
            rl["sem_efeito"], rl["pausa_ate"] = 0, time.monotonic() + PAUSA_SEM_EFEITO_S
            logger.info("reels leve: a seta não passou o Reel %d vezes; pausa de %d s",
                        SEM_EFEITO_MAX, PAUSA_SEM_EFEITO_S)
            return "travou"
        return False
    rl["sem_efeito"] = 0
    logger.debug("reels leve: passou (%s)", motivo)
    return True
