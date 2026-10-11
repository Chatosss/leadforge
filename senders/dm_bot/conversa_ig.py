# -*- coding: utf-8 -*-
"""
conversa_ig.py — antes de mandar a primeira DM, a conversa com a pessoa está vazia?
"""

import asyncio
import time

# Trecho comum: acha, entre as conversas da página, as de @u.
_DELA = r"""
  const alvo = '/' + String(u || '').toLowerCase().replace(/^@/, '') + '/';
  const linkDela = (raiz) => [...raiz.querySelectorAll('a[href]')].some(a => {
    const h = (a.getAttribute('href') || '').toLowerCase().split('?')[0].split('#')[0];
    return h === alvo || h.endsWith('instagram.com' + alvo);
  });
  // Mensagem = um article na lista. Horário ("09:16", "13 de mar de 2026
  // 15:52") é a segunda prova: aparece nos separadores de toda conversa com
  // mensagem e não aparece no cartão de uma conversa vazia. Sem `\b`: texto
  // colado ("Ver perfil09:16") não tem fronteira de palavra antes do número.
  const mensagens = (l) => l.querySelectorAll('[role="article"]').length
      + (/(^|\D)\d{1,2}:\d{2}(?!\d)/.test(l.innerText || '') ? 1 : 0);
  // O painel: sobe da lista enquanto o de cima não trouxer o menu lateral, a
  // página de baixo ou uma segunda conversa.
  const painel = (l) => {
    let j = l;
    while (j.parentElement && j.parentElement !== document.body) {
      const p = j.parentElement;
      if (p.querySelector('nav, [role="navigation"], main, [role="main"]')) break;
      if (p.querySelectorAll('[data-pagelet="IGDMessagesList"]').length > 1) break;
      j = p;
    }
    return j.querySelector('div[role="textbox"]') ? j : null;
  };
  const listas = [...document.querySelectorAll('[data-pagelet="IGDMessagesList"]')];
  const conversas = listas.map(l => ({l, p: painel(l)}))
      .filter(c => c.p && (linkDela(c.l) || linkDela(c.p)));
"""

JS_CONVERSA = "(u) => {" + _DELA + r"""
  if (!listas.length) return {estado: 'incerto', motivo: 'a lista de mensagens não apareceu'};
  if (!conversas.length) return {estado: 'incerto', motivo: 'a conversa na tela não é a de @' + u};
  for (const c of conversas) {
    const n = mensagens(c.l);
    if (n) return {estado: 'existe', mensagens: n, motivo: ''};
  }
  const comCartao = conversas.filter(c => linkDela(c.l));
  if (!comCartao.length) {
    return {estado: 'incerto', motivo: 'a conversa ainda não mostrou mensagem nem o cartão do perfil'};
  }
  const carregando = comCartao.some(c => !!c.p.querySelector('[role="progressbar"]'));
  return {estado: 'vazia', carregando, motivo: carregando ? 'a conversa ainda está carregando' : ''};
}"""

JS_CAIXA = "(u) => {" + _DELA + r"""
  for (const c of conversas) return c.p.querySelector('div[role="textbox"]');
  return null;
}"""

JS_ENVIAR = "(u) => {" + _DELA + r"""
  for (const c of conversas) {
    const b = [...c.p.querySelectorAll('div[role="button"], button')]
      .find(x => /^(enviar|send)$/i.test((x.innerText || '').trim()));
    if (b) { b.click(); return true; }
  }
  return false;
}"""


async def conversa_com(page, username, parar=None, prazo=15.0, estavel=5.0):
    """("existe" | "vazia" | "incerto", motivo)."""
    inicio = time.monotonic()
    vazias = 0
    motivo = "a conversa não terminou de carregar"
    while time.monotonic() - inicio < prazo:
        if parar and parar():
            return "incerto", "parada solicitada"
        try:
            r = await page.evaluate(JS_CONVERSA, username) or {}
        except Exception as e:
            r = {"estado": "incerto", "motivo": f"erro lendo a tela ({type(e).__name__})"}
        estado = r.get("estado")
        if estado == "existe":
            return "existe", (r.get("motivo")
                              or f"{r.get('mensagens', 0)} mensagem(ns) na conversa")
        if estado == "vazia" and not r.get("carregando"):
            vazias += 1
            if vazias >= 3 and time.monotonic() - inicio >= estavel:
                return "vazia", ""
        else:
            vazias = 0
            if r.get("motivo"):
                motivo = r["motivo"]
        await asyncio.sleep(1)
    return "incerto", motivo


async def caixa_da_conversa(page, username):
    """A caixa de texto DA conversa com @username — nunca a de outra janela."""
    try:
        h = await page.evaluate_handle(JS_CAIXA, username)
        return h.as_element()
    except Exception:
        return None


async def clicar_enviar(page, username) -> bool:
    """O botão "Enviar" da conversa verificada, pelo texto EXATO."""
    try:
        return bool(await page.evaluate(JS_ENVIAR, username))
    except Exception:
        return False
