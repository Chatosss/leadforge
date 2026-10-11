"""Checks Gmail threads for genuine human replies, filtering out auto-replies."""
import re
from datetime import datetime

from .database import get_conn, get_threads_to_check, get_accounts
from .gmail_manager import get_service, is_authenticated

# Headers that indicate an automatic message
_AUTO_HEADERS = {
    "x-auto-response-suppress",
    "x-autoreply",
    "x-autorespond",
    "x-auto-reply",
}

# Subject patterns for auto-replies (PT + EN)
_AUTO_SUBJECT = re.compile(
    r"(out of office|fora do escrit|férias|de férias|vacation|"
    r"automatic.?reply|auto.?reply|resposta autom|"
    r"ausente|away from|i am out|estou ausente|"
    r"delivery.?fail|undelivered|mail delivery|"
    r"entrega.*falhou|mensagem não entregue)",
    re.IGNORECASE,
)

# Sender addresses that are never human
_AUTO_FROM = re.compile(
    r"(no[.\-]?reply|noreply|mailer.daemon|postmaster|"
    r"auto.?mailer|bounce|return.path)@",
    re.IGNORECASE,
)


def _is_auto_reply(headers: list[dict]) -> bool:
    hmap = {h["name"].lower(): h["value"] for h in headers}

    # Explicit auto-submit header
    auto_submitted = hmap.get("auto-submitted", "no").lower()
    if auto_submitted != "no":
        return True

    # Other auto headers (presence is enough)
    for h in _AUTO_HEADERS:
        if h in hmap:
            return True

    # Precedence header
    precedence = hmap.get("precedence", "").lower()
    if precedence in ("bulk", "auto_reply", "junk"):
        return True

    # Subject pattern
    if _AUTO_SUBJECT.search(hmap.get("subject", "")):
        return True

    # From address pattern
    if _AUTO_FROM.search(hmap.get("from", "")):
        return True

    return False


def check_replies_for_account(account_email: str) -> int:
    """
    Poll Gmail for replies to sent threads on this account. Updates send_log.replied_at
    and prospects.replied_at for genuine replies. Returns number of new replies found.
    """
    if not is_authenticated(account_email):
        return 0

    threads = get_threads_to_check(account_email)
    if not threads:
        return 0

    try:
        service = get_service(account_email)
    except Exception:
        return 0

    conn = get_conn()
    new_replies = 0

    for entry in threads:
        try:
            thread = service.users().threads().get(
                userId="me",
                id=entry["thread_id"],
                format="metadata",
                metadataHeaders=[
                    "From", "Subject", "Auto-Submitted",
                    "X-Auto-Response-Suppress", "X-Autoreply",
                    "X-Autorespond", "X-Auto-Reply", "Precedence",
                ],
            ).execute()
        except Exception:
            continue

        messages = thread.get("messages", [])
        if len(messages) <= 1:
            continue  # no reply yet

        # Check messages after the first (which is our sent email)
        replied = False
        for msg in messages[1:]:
            headers = msg.get("payload", {}).get("headers", [])
            if not _is_auto_reply(headers):
                replied = True
                break

        if replied:
            now = datetime.now().isoformat()
            conn.execute(
                "UPDATE prospects SET replied_at=? WHERE id=?",
                (now, entry["prospect_id"]),
            )
            conn.execute(
                "UPDATE send_log SET replied_at=? WHERE id=?",
                (now, entry["log_id"]),
            )
            conn.commit()
            new_replies += 1

    conn.close()
    return new_replies


def check_all_replies() -> dict[str, int]:
    """
    Check replies across all active authenticated accounts. Returns {email: new_count}.
    """
    results = {}
    for acc in get_accounts(only_active=True):
        email = acc["email"]
        results[email] = check_replies_for_account(email)
    return results
