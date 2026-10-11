"""Publica o relé de webhooks do Instagram na hospedagem (Hostinger, FTPS) — v023."""

import argparse
import ftplib
import getpass
import hashlib
import io
import json
import os
import secrets
import ssl
import sys
from pathlib import Path

import requests

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
ENV = RAIZ / ".env"
PHP = AQUI / "ig-webhook.php"
PUBLICADO = AQUI / "ig-webhook.PUBLICADO.php"   # cópia do que está no ar

DADOS = "/leadforge-dados"
WEBROOT = "/public_html"


def _env_ler():
    vals = {}
    if ENV.exists():
        for linha in ENV.read_text(encoding="utf-8").splitlines():
            s = linha.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                vals[k.strip()] = v.strip()
    return vals


def _hospedagem(nome):
    """Dado da hospedagem do relé (ambiente ou .env; não fica no código)."""
    return os.environ.get(nome) or _env_ler().get(nome, "")


HOST = _hospedagem("IG_RELE_FTP_HOST")
USUARIO = _hospedagem("IG_RELE_FTP_USUARIO")
SITE = _hospedagem("IG_RELE_SITE").rstrip("/")


def _env_definir(chave, valor):
    """Troca a linha da chave (ou acrescenta), sem mexer no resto do .env."""
    linhas = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    nova, achou = [], False
    for linha in linhas:
        if linha.strip().startswith(chave + "="):
            nova.append(f"{chave}={valor}")
            achou = True
        else:
            nova.append(linha)
    if not achou:
        nova.append(f"{chave}={valor}")
    ENV.write_text("\n".join(nova) + "\n", encoding="utf-8")


def _sha(dados: bytes):
    return hashlib.sha256(dados).hexdigest()


def _baixar(ftp, caminho):
    buf = io.BytesIO()
    try:
        ftp.retrbinary(f"RETR {caminho}", buf.write)
    except ftplib.error_perm:
        return None
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conferir", action="store_true", help="só mostra o plano")
    ap.add_argument("--forcar", action="store_true",
                    help="publica mesmo se o arquivo no ar mudou desde a última publicação")
    a = ap.parse_args()

    env = _env_ler()
    segredo = env.get("IG_API_APP_SECRET", "")
    if not segredo:
        sys.exit("Falta IG_API_APP_SECRET no .env (chave secreta do app do Instagram, "
                 "no Painel de Apps da Meta). Nada foi publicado.")
    php = PHP.read_bytes()
    url = f"{SITE}/ig-webhook.php"
    print(f"Relé: {url}")
    print(f"  PHP local: {len(php)} bytes, sha256 {_sha(php)[:16]}…")
    print(f"  segredos em {DADOS}/ig-webhook.json (fora do site); fila em {DADOS}/ig-fila/")
    if a.conferir:
        print("Modo --conferir: nada foi enviado.")
        return

    verificar = env.get("IG_WEBHOOK_VERIFY_TOKEN") or secrets.token_urlsafe(24)
    puxar = env.get("IG_RELE_TOKEN") or secrets.token_urlsafe(32)
    cfg = json.dumps({"app_secret": segredo, "verify_token": verificar,
                      "token_pull": puxar}).encode("utf-8")
    # grava os tokens ANTES de subir: se o FTP cair no meio, a próxima rodada
    # reaproveita os mesmos em vez de sortear outros que o servidor não conhece
    _env_definir("IG_WEBHOOK_VERIFY_TOKEN", verificar)
    _env_definir("IG_RELE_TOKEN", puxar)

    senha = os.environ.get("LF_FTP_SENHA") or getpass.getpass(f"Senha do FTP de {USUARIO}: ")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False          # o certificado é do host, não do IP
    ftp = ftplib.FTP_TLS(context=ctx, timeout=40)
    ftp.connect(HOST, 21)
    ftp.login(USUARIO, senha)
    ftp.prot_p()
    ftp.voidcmd("TYPE I")               # em ASCII o upload corrompe fim de linha
    try:
        raiz = ftp.nlst("/")
        nomes = {n.rstrip("/").split("/")[-1] for n in raiz}
        if "leadforge-dados" not in nomes or "public_html" not in nomes:
            sys.exit(f"Não achei {DADOS} e {WEBROOT} na raiz do FTP ({sorted(nomes)}). Nada foi publicado.")

        # não sobrescreve um relé que alguém mudou no servidor por fora
        no_ar = _baixar(ftp, f"{WEBROOT}/ig-webhook.php")
        if no_ar is not None and no_ar != php and not a.forcar:
            antes = PUBLICADO.read_bytes() if PUBLICADO.exists() else None
            if antes is None or no_ar != antes:
                sys.exit("O ig-webhook.php no ar é diferente da última publicação daqui. "
                         "Confira antes; para publicar mesmo assim, use --forcar.")

        ftp.storbinary(f"STOR {DADOS}/ig-webhook.json.parcial", io.BytesIO(cfg))
        ftp.rename(f"{DADOS}/ig-webhook.json.parcial", f"{DADOS}/ig-webhook.json")
        try:
            ftp.sendcmd(f"SITE CHMOD 600 {DADOS}/ig-webhook.json")
        except ftplib.error_perm:
            print("  (o servidor não aceitou CHMOD 600; o arquivo continua fora do site)")

        ftp.storbinary(f"STOR {DADOS}/ig-webhook.novo.php", io.BytesIO(php))
        if _baixar(ftp, f"{DADOS}/ig-webhook.novo.php") != php:
            sys.exit("O PHP enviado não confere byte a byte. O relé no ar não foi trocado.")
        ftp.rename(f"{DADOS}/ig-webhook.novo.php", f"{WEBROOT}/ig-webhook.php")
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    PUBLICADO.write_bytes(php)
    _env_definir("IG_RELE_URL", url)

    # conferência no ar
    desafio = str(secrets.randbelow(10**9))
    r1 = requests.get(url, params={"hub.mode": "subscribe", "hub.verify_token": verificar,
                                   "hub.challenge": desafio}, timeout=20)
    r2 = requests.post(url, data=b"{}", headers={"X-Hub-Signature-256": "sha256=00"}, timeout=20)
    r3 = requests.post(url, params={"acao": "puxar"}, json={"token": puxar, "max": 1}, timeout=20)
    ok1 = r1.status_code == 200 and r1.text.strip() == desafio
    ok2 = r2.status_code == 403
    try:
        ok3 = r3.status_code == 200 and r3.json().get("ok") is True
    except ValueError:
        ok3 = False
    print(f"  verificação da Meta: {'ok' if ok1 else f'FALHOU (HTTP {r1.status_code})'}")
    print(f"  POST sem assinatura recusado: {'ok' if ok2 else f'FALHOU (HTTP {r2.status_code})'}")
    print(f"  puxada do painel: {'ok' if ok3 else f'FALHOU (HTTP {r3.status_code})'}")
    if not (ok1 and ok2 and ok3):
        sys.exit("O relé subiu, mas a conferência falhou — veja acima antes de configurar a Meta.")
    print("\nNo Painel de Apps da Meta > Instagram > Webhooks:")
    print(f"  URL de retorno de chamada: {url}")
    print(f"  Verificar token:           {verificar}")
    print("  Campos: messages e message_echoes")
    print("\nPronto. Reinicie o painel (python subir.py --reiniciar) para ele começar a puxar.")


if __name__ == "__main__":
    main()
