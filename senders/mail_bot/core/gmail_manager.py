"""Gmail API wrapper — authentication, filter setup, forwarding, sending."""
import base64
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.settings.sharing",
    "https://www.googleapis.com/auth/gmail.modify",
]

PCZ_DIR    = Path.home() / ".pcz"
TOKENS_DIR = PCZ_DIR / "tokens"
CREDS_FILE = PCZ_DIR / "credentials.json"


# ── auth ─────────────────────────────────────────────────────────────────────

def credentials_exist() -> bool:
    return CREDS_FILE.exists()


def is_authenticated(email: str) -> bool:
    token_path = TOKENS_DIR / f"{email}.json"
    if not token_path.exists():
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        return creds.valid or (creds.expired and creds.refresh_token is not None)
    except Exception:
        return False


def authenticate(email: str) -> str:
    """
    Runs the OAuth2 browser flow for the given Gmail account. Returns "ok" on success or
    an error message.
    """
    if not CREDS_FILE.exists():
        return "credentials.json não encontrado em ~/.pcz/"

    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    token_path = TOKENS_DIR / f"{email}.json"

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
        token_path.write_text(creds.to_json())
        return "ok"
    except Exception as e:
        return str(e)


def get_service(email: str):
    token_path = TOKENS_DIR / f"{email}.json"
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ── filters ──────────────────────────────────────────────────────────────────

BOUNCE_FILTERS = [
    {"from": "mailer-daemon@googlemail.com"},
    {"from": "mailer-daemon@"},
    {"subject": "Delivery Status Notification"},
    {"subject": "Mail Delivery Failed"},
    {"subject": "Undelivered Mail Returned to Sender"},
    {"subject": "Delivery Failure"},
    {"subject": "Falha na entrega"},
    {"subject": "Mensagem não entregue"},
]


def setup_filters(email: str) -> tuple[int, list[str]]:
    """Create bounce-suppression filters. Returns (count_created, errors)."""
    service = get_service(email)
    created, errors = 0, []

    for criteria in BOUNCE_FILTERS:
        body = {
            "criteria": criteria,
            "action": {
                "removeLabelIds": ["INBOX"],
                "addLabelIds": ["TRASH"],
            },
        }
        try:
            service.users().settings().filters().create(userId="me", body=body).execute()
            created += 1
        except HttpError as e:
            if e.status_code == 409:  # already exists
                created += 1
            else:
                errors.append(f"{criteria}: {e}")
        except Exception as e:
            errors.append(str(e))

    return created, errors


# ── forwarding ───────────────────────────────────────────────────────────────

def setup_forwarding(email: str, forward_to: str) -> str:
    """
    Add a forwarding address and enable auto-forwarding. Gmail will send a verification
    e-mail to forward_to — the user must click the link before forwarding activates.
    Returns "ok" or an error message.
    """
    service = get_service(email)
    try:
        service.users().settings().forwardingAddresses().create(
            userId="me",
            body={"forwardingEmail": forward_to},
        ).execute()
    except HttpError as e:
        if e.status_code != 409:  # ignore "already exists"
            return f"Erro ao adicionar endereço: {e}"

    try:
        service.users().settings().updateAutoForwarding(
            userId="me",
            body={
                "enabled": True,
                "emailAddress": forward_to,
                "disposition": "leaveInInbox",
            },
        ).execute()
    except HttpError as e:
        return f"Erro ao ativar encaminhamento: {e}"

    return "ok"


def remove_forwarding(email: str, forward_to: str) -> str:
    service = get_service(email)
    try:
        service.users().settings().updateAutoForwarding(
            userId="me",
            body={"enabled": False, "emailAddress": forward_to, "disposition": "leaveInInbox"},
        ).execute()
        service.users().settings().forwardingAddresses().delete(
            userId="me", forwardingEmail=forward_to
        ).execute()
    except Exception as e:
        return str(e)
    return "ok"


# ── sending ──────────────────────────────────────────────────────────────────

def _sanitize_email(addr: str) -> str:
    """
    Limpa endereços de email com dados sujos comuns: - Remove espaços - Corrige vírgulas
    dentro do domínio (ex:.com,.br →.com.br) - Converte para minúsculas Retorna string
    vazia se o resultado não parecer um email válido.
    """
    import re
    addr = addr.strip().lower()
    # Remove espaços internos
    addr = addr.replace(" ", "")
    # Vírgula dentro do domínio (após @): substitui por ponto
    # ex: CONTATO@EMPRESA.COM,.BR → contato@empresa.com.br
    parts = addr.split("@", 1)
    if len(parts) == 2:
        local, domain = parts
        domain = domain.replace(",", ".")   # .com,.br → .com..br
        domain = re.sub(r"\.{2,}", ".", domain)  # .com..br → .com.br
        domain = domain.rstrip(".")         # remove ponto(s) trailing: .com. → .com
        addr = f"{local}@{domain}"
    # Valida formato mínimo: algo@algo.algo  (sem ponto no final)
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$", addr):
        return ""
    return addr


def send_email(
    account_email: str,
    to: str,
    subject: str,
    body: str,
    sender_name: str = "",
) -> tuple[str, str | None]:
    """Send a plain-text email. Returns ('ok', thread_id) or (error_msg, None)."""
    to = _sanitize_email(to)
    if not to:
        return "Email inválido — ignorado", None

    try:
        service = get_service(account_email)

        msg = MIMEMultipart("alternative")
        msg["To"] = to
        msg["Subject"] = subject
        from_field = f"{sender_name} <{account_email}>" if sender_name else account_email
        msg["From"] = from_field
        msg.attach(MIMEText(body, "plain", "utf-8"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return "ok", sent.get("threadId")
    except HttpError as e:
        return str(e), None
    except Exception as e:
        return str(e), None


def get_profile(email: str) -> dict:
    service = get_service(email)
    return service.users().getProfile(userId="me").execute()
