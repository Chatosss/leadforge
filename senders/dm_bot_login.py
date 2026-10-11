"""dm_bot_login.py — Abre o Instagram numa janela visível para VOCÊ fazer o login."""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent / "dm_bot"
RAIZ = Path(__file__).resolve().parent.parent

# `python senders/dm_bot_login.py` poe `senders/` no sys.path, nao a raiz.
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))
try:
    import config                      # fonte unica do caminho de perfil
except Exception:
    config = None


def perfil_da_conta(conta: str = "") -> str:
    """Pasta do perfil de UMA conta. Sem conta, o perfil unico de antes."""
    if config is not None:
        return str(config.ig_perfil(conta))
    c = (conta or "").strip().lstrip("@").lower()
    if not c:
        return SESSION_DIR
    return str(Path(SESSION_DIR).parent / "ig_sessions" / c)

# Onde fica o perfil do navegador do bot.
SESSION_DIR = os.environ.get("IG_SESSION_DIR") or str(
    Path(os.environ.get("LOCALAPPDATA", Path.home())) / "clinica_prospector" / "ig_session")
SESSION_DIR_ANTIGO = str(BASE / "session_data")


def _caminho_cookies(base: str) -> Path:
    p = Path(base) / "Default" / "Network" / "Cookies"
    return p if p.exists() else Path(base) / "Default" / "Cookies"


# perfil -> ((mtime_ns, tamanho) do arquivo de cookies, resultado)
_CACHE_SESSAO = {}


def sessao_no_disco(base: str = None) -> dict:
    """Lê os cookies do perfil SEM abrir navegador."""
    base = base or SESSION_DIR
    arq = _caminho_cookies(base)
    if not arq.exists():
        return {"ativa": False, "motivo": f"perfil sem banco de cookies em {base}"}
    try:
        st = arq.stat()
        marca = (st.st_mtime_ns, st.st_size)
    except OSError:
        marca = None
    chave = str(arq)
    guardado = _CACHE_SESSAO.get(chave)
    if guardado and marca and guardado[0] == marca:
        return dict(guardado[1])
    # Nome único por leitura: com nome fixo, duas consultas ao mesmo tempo (aba
    # Aquecimento e Direct abertas) copiavam por cima uma da outra, e uma conta
    # lia os cookies da outra ou aparecia "sem sessão".
    fd, tmp = tempfile.mkstemp(prefix="ig_cookies_", suffix=".db")
    os.close(fd)
    tmp = Path(tmp)
    try:
        shutil.copy2(arq, tmp)       # cópia: o original pode estar em uso
        con = sqlite3.connect(str(tmp))
        # O valor fica em `encrypted_value` (criptografado pelo Windows), então
        # só dá para conferir a PRESENÇA do cookie, não o conteúdo. É o bastante:
        # sessionid + ds_user_id só existem em perfil autenticado.
        nomes = {r[0] for r in con.execute(
            "SELECT name FROM cookies WHERE host_key LIKE '%instagram%'")}
        con.close()
    except Exception as e:
        # Com o navegador da conta aberto, o Chromium trava o arquivo: vale a
        # última leitura boa (a sessão não sumiu por a janela estar aberta).
        if guardado:
            r = dict(guardado[1])
            r["motivo"] = r.get("motivo") or "perfil em uso; última leitura"
            return r
        return {"ativa": False, "motivo": f"não consegui ler os cookies: {e}"}
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass
    ativa = "sessionid" in nomes and "ds_user_id" in nomes
    r = {"ativa": ativa, "cookies": sorted(nomes),
         "motivo": "" if ativa else "sem sessionid/ds_user_id: login não concluído"}
    if marca:
        _CACHE_SESSAO[chave] = (marca, dict(r))
    return r

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


def migrar_perfil(verbose: bool = True) -> bool:
    """Traz o perfil antigo (dentro do OneDrive) para o novo lugar, uma vez só."""
    destino, origem = Path(SESSION_DIR), Path(SESSION_DIR_ANTIGO)
    if sessao_no_disco(str(destino)).get("ativa"):
        return True                                   # novo já está bom
    if not origem.exists() or not sessao_no_disco(str(origem)).get("ativa"):
        return False                                  # não há o que trazer
    if verbose:
        print(f"Trazendo a sessão de {origem}\n                    para {destino}")
    destino.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(origem, destino, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(
                            "Crashpad", "*Cache*", "*cache*", "GrShaderCache",
                            "GraphiteDawnCache", "component_crx_cache",
                            "extensions_crx_cache", "Safe Browsing", "*.log"))
    except Exception as e:
        if verbose:
            print("falhou ao copiar:", e)
        return False
    ok = sessao_no_disco(str(destino)).get("ativa", False)
    if verbose:
        print("sessão migrada com sucesso." if ok
              else "copiei, mas a sessão não veio junto.")
    return ok


async def _logado(page) -> bool:
    """Sessão viva?"""
    try:
        cookies = await page.context.cookies("https://www.instagram.com")
        nomes = {c["name"]: (c.get("value") or "") for c in cookies}
        if nomes.get("sessionid") and nomes.get("ds_user_id"):
            return True
    except Exception:
        pass
    for rotulo in ("Página inicial", "Home", "Inicio", "Início"):
        try:
            if await page.locator(f'svg[aria-label="{rotulo}"]').count() > 0:
                return True
        except Exception:
            pass
    return False


async def diagnostico(page) -> str:
    """
    Onde o login parou. Serve para não ficar no escuro quando a sessão não vem:
    formulário aberto, desafio de segurança e conta bloqueada são coisas diferentes, e
    só a primeira se resolve digitando de novo.
    """
    try:
        url = (page.url or "").lower()
    except Exception:
        return "não consegui ler a página"
    if "challenge" in url:
        return ("o Instagram pediu VERIFICAÇÃO DE SEGURANÇA (challenge). "
                "Conclua na janela: ele costuma mandar código por e-mail ou SMS.")
    if "two_factor" in url or "2fa" in url:
        return "falta o código de dois fatores (2FA). Digite-o na janela."
    if "suspended" in url or "disabled" in url:
        return "esta conta aparece como suspensa ou desativada pelo Instagram."
    if "accounts/login" in url or "/login" in url:
        return "ainda na tela de login: usuário e senha não foram aceitos ou não foram enviados."
    if "instagram.com" in url:
        return f"na página {url}, mas sem cookie de sessão."
    return f"fora do Instagram: {url}"


async def quem_esta_logado(page) -> str:
    """Devolve o ds_user_id da sessão, útil para confirmar que é a conta certa."""
    try:
        cookies = await page.context.cookies("https://www.instagram.com")
        for c in cookies:
            if c["name"] == "ds_user_id":
                return c.get("value") or ""
    except Exception:
        pass
    return ""


async def abrir(espera_max: int = 600, perfil: str = None):
    from playwright.async_api import async_playwright

    perfil = perfil or SESSION_DIR
    Path(perfil).mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=perfil,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        await asyncio.sleep(4)

        if await _logado(page):
            print("Já estava logado. Sessão salva em:", perfil)
            await ctx.close()
            return True

        print("=" * 66)
        print("  Faça o login do Instagram NA JANELA que abriu.")
        print("  Digite você mesmo usuário e senha. Resolva o 2FA se pedir.")
        print("  Assim que a tela inicial carregar, a sessão é salva sozinha.")
        print(f"  Aguardo por até {espera_max // 60} minutos. Ctrl+C cancela.")
        print("=" * 66)

        esperado = 0
        while esperado < espera_max:
            await asyncio.sleep(5)
            esperado += 5
            try:
                if await _logado(page):
                    print(f"\nLogin detectado. Sessão salva em: {SESSION_DIR}")
                    await asyncio.sleep(3)   # deixa o storage state assentar
                    await ctx.close()
                    return True
            except Exception:
                # a janela pode ter sido fechada
                break
            if esperado % 30 == 0:
                print(f"  ...aguardando ({esperado}s) — {await diagnostico(page)}")

        print("\nNão detectei o login. A sessão não foi confirmada.")
        try:
            print("Onde parou:", await diagnostico(page))
        except Exception:
            pass
        try:
            await ctx.close()
        except Exception:
            pass
        return False


if __name__ == "__main__":
    # `python senders/dm_bot_login.py minha_conta` loga NESSA conta.
    # Sem conta, mexe no perfil unico legado (compatibilidade).
    contas = [a for a in sys.argv[1:] if not a.startswith("--")]
    conta = contas[0] if contas else ""
    perfil = perfil_da_conta(conta)
    rotulo = f"@{conta}" if conta else "perfil unico (sem conta)"

    if not conta:
        migrar_perfil(verbose="--checar" not in sys.argv)

    if "--checar" in sys.argv:
        # Verificação de rotina: lê o disco, NÃO abre navegador.
        st = sessao_no_disco(perfil)
        print("conta: ", rotulo)
        print("perfil:", perfil)
        if st["ativa"]:
            print("sessão do Instagram: ATIVA (sessionid + ds_user_id presentes)")
        else:
            print("sessão do Instagram: AUSENTE")
            print("  motivo:", st["motivo"])
            print(f"  rode sem --checar para abrir a janela de login: "
                  f"python -X utf8 senders/dm_bot_login.py {conta}".rstrip())
        sys.exit(0 if st["ativa"] else 1)

    if sessao_no_disco(perfil).get("ativa"):
        print(f"Já existe sessão salva para {rotulo} em:", perfil)
        print("Para trocar de conta, apague essa pasta e rode de novo.")
        sys.exit(0)

    print(f"Login do Instagram para {rotulo}")
    print("perfil:", perfil)
    try:
        ok = asyncio.run(abrir(perfil=perfil))
    except KeyboardInterrupt:
        print()
        print("cancelado.")
        ok = False
    sys.exit(0 if ok else 1)
