"""email_login.py — Autoriza UMA conta Gmail a enviar, pelo consentimento do Google."""

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
CORE = RAIZ / "senders" / "mail_bot" / "core"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    if len(sys.argv) < 2:
        print("uso: python -X utf8 email_login.py conta@gmail.com")
        return 2
    email = sys.argv[1].strip().lower()

    if str(CORE) not in sys.path:
        sys.path.insert(0, str(CORE))
    try:
        import gmail_manager as gm
    except Exception as e:
        print("Não consegui carregar o gmail_manager:", e)
        print("Faltam as bibliotecas do Google? Rode:")
        print("  pip install google-api-python-client google-auth-oauthlib")
        return 1

    if not gm.credentials_exist():
        print("Falta o credentials.json do Google em ~/.pcz/credentials.json")
        print("  1. https://console.cloud.google.com/apis/credentials")
        print("  2. Crie um OAuth Client ID do tipo Desktop")
        print("  3. Baixe o JSON e salve com esse nome")
        print("  4. Ative a Gmail API no projeto")
        return 1

    if gm.is_authenticated(email):
        print(f"{email} já está autenticada.")
        print("Para trocar o acesso, apague ~/.pcz/tokens/" + email + ".json e rode de novo.")
        return 0

    print("=" * 66)
    print(f"  Autorizando {email}")
    print("  O navegador vai abrir na tela do Google. Escolha ESTA conta e")
    print("  aprove o acesso. Ninguém além de você digita a senha.")
    print("=" * 66)

    try:
        resultado = gm.authenticate(email)
    except Exception as e:
        print("Falhou:", e)
        return 1

    if resultado == "ok":
        print(f"\n{email} autorizada. Volte ao painel e clique em Validar.")
        return 0
    print(f"\nNão deu certo: {resultado}")
    return 1


if __name__ == "__main__":
    codigo = main()
    try:
        input("\nPressione ENTER para fechar esta janela...")
    except Exception:
        pass
    sys.exit(codigo)
