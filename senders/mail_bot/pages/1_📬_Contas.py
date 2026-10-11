import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Shared UI components from hub dashboard
_HUB_DASH = Path(__file__).parents[3] / "hub" / "dashboard"
if _HUB_DASH.exists() and str(_HUB_DASH) not in sys.path:
    sys.path.insert(0, str(_HUB_DASH))

from datetime import date, datetime, timedelta
import streamlit as st
from core.database import get_conn, get_accounts, get_setting, set_setting
from core.gmail_manager import (
    authenticate, credentials_exist, is_authenticated,
    setup_filters, setup_forwarding, remove_forwarding, get_profile,
    CREDS_FILE,
)

try:
    from components.ui import apply_theme, log_viewer, status_badge, empty_state
    _HAS_THEME = True
except ImportError:
    _HAS_THEME = False

st.set_page_config(page_title="Contas — PCZ", page_icon="📬", layout="wide")
if _HAS_THEME:
    apply_theme()
st.title("📬 Contas Gmail")


def _countdown_to_midnight() -> str:
    now = datetime.now()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    remaining = midnight - now
    h = remaining.seconds // 3600
    m = (remaining.seconds % 3600) // 60
    return f"{h}h {m}min"


def _time_since(iso_str: str | None) -> str:
    if not iso_str:
        return "nunca"
    try:
        sent = datetime.fromisoformat(iso_str)          # stored in UTC by SQLite
        total = int((datetime.utcnow() - sent).total_seconds())
        if total < 0:
            return "agora"
        if total < 3600:
            return f"{total // 60}min atrás"
        if total < 86400:
            return f"{total // 3600}h {(total % 3600) // 60}min atrás"
        return f"{total // 86400}d {(total % 86400) // 3600}h atrás"
    except Exception:
        return "—"


# ── credentials.json setup ───────────────────────────────────────────────────
with st.expander("⚙️ Configuração do Google Cloud (fazer uma vez)", expanded=not credentials_exist()):
    st.markdown("""
**Passos para obter o `credentials.json`:**
1. Acesse [console.cloud.google.com](https://console.cloud.google.com)
2. Crie um projeto (ou selecione um existente)
3. Vá em **APIs & Services → Enable APIs** → ative a **Gmail API**
4. Vá em **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
5. Tipo: **Desktop App** → baixe o JSON
6. Faça upload abaixo
""")
    uploaded = st.file_uploader("Upload credentials.json", type="json", key="creds_upload")
    if uploaded:
        CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        CREDS_FILE.write_bytes(uploaded.read())
        st.success("✅ credentials.json salvo com sucesso!")
        st.rerun()

    if credentials_exist():
        st.success(f"✅ credentials.json presente em `{CREDS_FILE}`")

st.divider()

# ── add account ───────────────────────────────────────────────────────────────
st.subheader("Adicionar conta")

with st.form("add_account"):
    col1, col2 = st.columns(2)
    new_email     = col1.text_input("E-mail Gmail", placeholder="seuemail@gmail.com")
    sender_name   = col2.text_input("Nome do remetente", placeholder="João Silva")
    daily_limit   = col1.number_input("Limite diário de envios", min_value=10, max_value=500, value=95)
    forwarding_to = col2.text_input("Encaminhar respostas para (opcional)", placeholder="central@gmail.com")

    submitted = st.form_submit_button("➕ Adicionar conta", type="primary")

if submitted:
    if not new_email.strip():
        st.error("Informe o e-mail.")
    elif not credentials_exist():
        st.error("Faça o upload do credentials.json antes de adicionar contas.")
    else:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO accounts(email, sender_name, daily_limit, forwarding_to) VALUES(?,?,?,?)",
                (new_email.strip(), sender_name.strip(), daily_limit, forwarding_to.strip() or None),
            )
            conn.commit()
            conn.close()
            st.success(f"✅ Conta {new_email} adicionada! Clique em **Autenticar** abaixo.")
            st.rerun()
        except Exception as e:
            conn.close()
            st.error(f"Erro ao salvar conta: {e}")

st.divider()

# ── accounts list ─────────────────────────────────────────────────────────────
st.subheader("Contas cadastradas")

accounts = get_accounts()

if not accounts:
    st.info("Nenhuma conta cadastrada ainda.")
else:
    reset_in = _countdown_to_midnight()

    today_str = date.today().isoformat()

    for acc in accounts:
        email  = acc["email"]
        authed = is_authenticated(email)
        # Display 0 if the counter is from a previous day (actual DB reset happens in sender on next run)
        sent  = acc["sent_today"] if acc.get("last_reset") == today_str else 0
        limit = acc["daily_limit"]
        pct   = sent / limit if limit else 0

        with st.container(border=True):

            # ── header row ────────────────────────────────────────────────────
            h1, h2, h3 = st.columns([3, 3, 3])

            h1.markdown(
                f"**{email}**  \n"
                f"{'🟢 Autenticada' if authed else '🔴 Não autenticada'}"
            )

            h2.markdown(
                f"📤 **{sent} / {limit}** enviados hoje  \n"
                f"⏱ Último envio: **{_time_since(acc.get('last_sent_at'))}**  \n"
                f"🔄 Reset em: **{reset_in}**"
            )

            col_fwd = h3.empty()
            if acc["forwarding_to"]:
                col_fwd.markdown(f"Encaminha para: `{acc['forwarding_to']}`")
            col_fwd.markdown(f"Filtros: {'✅' if acc['filters_set'] else '❌'}")

            # ── progress bar ─────────────────────────────────────────────────
            st.progress(min(pct, 1.0), text=f"{sent}/{limit} enviados hoje ({pct*100:.0f}%)")

            # ── edit daily limit ──────────────────────────────────────────────
            with st.expander("✏️ Editar limite diário"):
                lim_col, save_col = st.columns([2, 1])
                new_limit = lim_col.number_input(
                    "Novo limite diário",
                    min_value=10, max_value=500,
                    value=int(limit),
                    key=f"newlim_{acc['id']}",
                )
                if save_col.button("💾 Salvar", key=f"savelim_{acc['id']}"):
                    conn = get_conn()
                    conn.execute("UPDATE accounts SET daily_limit=? WHERE id=?", (new_limit, acc["id"]))
                    conn.commit()
                    conn.close()
                    st.success(f"Limite atualizado para {new_limit}/dia.")
                    st.rerun()

            # ── action buttons ────────────────────────────────────────────────
            btn_cols = st.columns(5)

            # Authenticate
            if btn_cols[0].button("🔑 Autenticar", key=f"auth_{acc['id']}", disabled=not credentials_exist()):
                with st.spinner(f"Abrindo navegador para {email}..."):
                    result = authenticate(email)
                if result == "ok":
                    st.success(f"✅ {email} autenticada com sucesso!")
                    st.rerun()
                else:
                    st.error(f"Erro: {result}")

            # Setup filters
            if btn_cols[1].button("🔧 Filtros", key=f"flt_{acc['id']}", disabled=not authed):
                with st.spinner("Configurando filtros..."):
                    created, errors = setup_filters(email)
                if errors:
                    st.warning(f"{created} filtros criados. Erros: {errors}")
                else:
                    conn = get_conn()
                    conn.execute("UPDATE accounts SET filters_set=1 WHERE id=?", (acc["id"],))
                    conn.commit()
                    conn.close()
                    st.success(f"✅ {created} filtros de bounce configurados!")
                    st.rerun()

            # Setup forwarding
            if acc["forwarding_to"] and btn_cols[2].button("📨 Encaminhar", key=f"fwd_{acc['id']}", disabled=not authed):
                with st.spinner("Configurando encaminhamento..."):
                    result = setup_forwarding(email, acc["forwarding_to"])
                if result == "ok":
                    st.success(
                        f"✅ Encaminhamento configurado! Verifique a caixa de entrada de "
                        f"**{acc['forwarding_to']}** para confirmar o link que o Google enviou."
                    )
                else:
                    st.error(f"Erro: {result}")

            # Toggle active
            label = "⏸ Desativar" if acc["active"] else "▶️ Ativar"
            if btn_cols[3].button(label, key=f"tog_{acc['id']}"):
                conn = get_conn()
                conn.execute("UPDATE accounts SET active=? WHERE id=?", (0 if acc["active"] else 1, acc["id"]))
                conn.commit()
                conn.close()
                st.rerun()

            # Delete
            if btn_cols[4].button("🗑 Remover", key=f"del_{acc['id']}"):
                conn = get_conn()
                conn.execute("DELETE FROM accounts WHERE id=?", (acc["id"],))
                conn.commit()
                conn.close()
                st.rerun()

# ── configure all at once ─────────────────────────────────────────────────────
st.divider()
st.subheader("Configurar todas as contas autenticadas de uma vez")

authed_accounts = [a for a in accounts if is_authenticated(a["email"])]
if authed_accounts:
    if st.button("🔧 Aplicar filtros em todas as contas autenticadas", type="primary"):
        ok, fail = 0, 0
        progress = st.progress(0)
        for i, acc in enumerate(authed_accounts):
            created, errors = setup_filters(acc["email"])
            if not errors:
                conn = get_conn()
                conn.execute("UPDATE accounts SET filters_set=1 WHERE id=?", (acc["id"],))
                conn.commit()
                conn.close()
                ok += 1
            else:
                fail += 1
            progress.progress((i + 1) / len(authed_accounts))
        st.success(f"✅ {ok} contas configuradas. {fail} com erros.")
        st.rerun()
else:
    st.info("Nenhuma conta autenticada ainda.")
