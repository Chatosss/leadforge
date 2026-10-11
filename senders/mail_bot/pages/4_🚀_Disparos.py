import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Shared UI components from hub dashboard
_HUB_DASH = Path(__file__).parents[3] / "hub" / "dashboard"
if _HUB_DASH.exists() and str(_HUB_DASH) not in sys.path:
    sys.path.insert(0, str(_HUB_DASH))

import streamlit as st
import pandas as pd
from core.database import (
    get_conn, get_accounts, get_templates, get_lists,
    get_campaigns, get_campaign_accounts,
    get_campaign_templates, get_template_stats,
)
from core.reply_checker import check_all_replies
from core.gmail_manager import is_authenticated
import core.sender as sender

try:
    from components.ui import apply_theme, log_viewer
    _HAS_THEME = True
except ImportError:
    _HAS_THEME = False

st.set_page_config(page_title="Disparos — PCZ", page_icon="🚀", layout="wide")
if _HAS_THEME:
    apply_theme()
st.title("🚀 Campanhas e Disparos")


@st.fragment(run_every=3)
def _live_log():
    """
    Auto-refreshes every 2s. Shows live log while running, last session log when
    stopped.
    """
    state = sender.get_state()
    log_lines = state.get("log", [])
    if not log_lines:
        return

    st.divider()
    if state["running"]:
        st.markdown(
            f"🔴 **Disparo em andamento** — "
            f"Enviados: **{state['sent']}** · "
            f"Erros: **{state['errors']}** · "
            f"Atual: `{state['current']}`"
        )
    else:
        st.markdown(
            f"⏹ **Último disparo encerrado** — "
            f"Enviados: **{state['sent']}** · "
            f"Erros: **{state['errors']}**"
        )
    if _HAS_THEME:
        log_viewer(log_lines[-50:], height=280, key="live_log_global")
    else:
        st.text_area("Log", value="\n".join(log_lines[-50:]),
                     height=280, disabled=True, key="live_log_global")
    st.divider()


_live_log()

# ── create campaign ───────────────────────────────────────────────────────────
st.subheader("Nova campanha")

templates = get_templates()
lists     = get_lists()
accounts  = get_accounts(only_active=True)

if not templates:
    st.warning("Crie pelo menos um template antes de criar uma campanha.")
elif not lists:
    st.warning("Importe pelo menos uma lista antes de criar uma campanha.")
else:
    with st.form("new_campaign"):
        camp_name = st.text_input("Nome da campanha", placeholder="Dentistas SP — Abordagem 1")

        c1, c2 = st.columns(2)
        tmpl_options = {t["name"]: t["id"] for t in templates}
        list_options = {f"{l['name']} ({l['pending']} pendentes)": l["id"] for l in lists}

        tmpl_sel = c1.selectbox("Template", list(tmpl_options.keys()))
        list_sel = c2.selectbox("Lista",    list(list_options.keys()))

        submitted = st.form_submit_button("➕ Criar campanha", type="primary")

    if submitted:
        if not camp_name.strip():
            st.error("Informe o nome da campanha.")
        else:
            conn = get_conn()
            cur = conn.execute(
                "INSERT INTO campaigns(name, template_id, list_id) VALUES(?,?,?)",
                (camp_name.strip(), tmpl_options[tmpl_sel], list_options[list_sel]),
            )
            camp_id = cur.lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO campaign_templates(campaign_id, template_id) VALUES(?,?)",
                (camp_id, tmpl_options[tmpl_sel]),
            )
            conn.commit()
            conn.close()
            st.success("Campanha criada!")
            st.rerun()

st.divider()

# ── reply checker ─────────────────────────────────────────────────────────────
col_rc, col_info = st.columns([1, 4])
if col_rc.button("🔄 Verificar respostas", type="secondary"):
    with st.spinner("Verificando respostas em todas as contas..."):
        results = check_all_replies()
    total = sum(results.values())
    if total:
        st.success(f"✅ {total} nova(s) resposta(s) encontrada(s)!")
    else:
        col_info.info("Nenhuma nova resposta encontrada.")

st.divider()

# ── campaigns list ────────────────────────────────────────────────────────────
st.subheader("Campanhas")

campaigns = get_campaigns()
if not campaigns:
    st.info("Nenhuma campanha criada ainda.")

state = sender.get_state()

for camp in campaigns:
    is_this_running = state["running"] and state["campaign_id"] == camp["id"]

    status_icon = {
        "active": "🟢",
        "paused": "⏸",
        "done":   "✅",
    }.get(camp["status"], "⏸")

    with st.container(border=True):
        st.markdown(
            f"{status_icon} **{camp['name']}**  \n"
            f"Template: `{camp['template_name']}` · Lista: `{camp['list_name']}`"
        )

        # Account selection
        all_account_ids   = [a["id"] for a in accounts]
        linked_accounts   = get_campaign_accounts(camp["id"])
        linked_ids        = [a["id"] for a in linked_accounts]
        account_labels    = {a["email"]: a["id"] for a in accounts}

        selected_emails = st.multiselect(
            "Contas para esta campanha",
            options=list(account_labels.keys()),
            default=[a["email"] for a in linked_accounts if a["id"] in all_account_ids],
            key=f"accs_{camp['id']}",
        )

        if st.button("💾 Salvar contas", key=f"save_accs_{camp['id']}"):
            conn = get_conn()
            conn.execute("DELETE FROM campaign_accounts WHERE campaign_id=?", (camp["id"],))
            for email in selected_emails:
                acc_id = account_labels[email]
                conn.execute(
                    "INSERT OR IGNORE INTO campaign_accounts(campaign_id, account_id) VALUES(?,?)",
                    (camp["id"], acc_id),
                )
            conn.commit()
            conn.close()
            st.success("Contas salvas.")
            st.rerun()

        # Template A/B management
        all_tmpl_labels = {t["name"]: t["id"] for t in templates}
        linked_tmpls = get_campaign_templates(camp["id"])
        linked_tmpl_names = [t["name"] for t in linked_tmpls]

        selected_tmpls = st.multiselect(
            "Templates para A/B (rotação automática)",
            options=list(all_tmpl_labels.keys()),
            default=linked_tmpl_names,
            key=f"tmpls_{camp['id']}",
        )

        if st.button("💾 Salvar templates", key=f"save_tmpls_{camp['id']}"):
            if not selected_tmpls:
                st.error("Selecione pelo menos um template.")
            else:
                conn = get_conn()
                conn.execute("DELETE FROM campaign_templates WHERE campaign_id=?", (camp["id"],))
                for tname in selected_tmpls:
                    conn.execute(
                        "INSERT OR IGNORE INTO campaign_templates(campaign_id, template_id) VALUES(?,?)",
                        (camp["id"], all_tmpl_labels[tname]),
                    )
                # Update primary template_id for display
                conn.execute(
                    "UPDATE campaigns SET template_id=? WHERE id=?",
                    (all_tmpl_labels[selected_tmpls[0]], camp["id"]),
                )
                conn.commit()
                conn.close()
                st.success("Templates salvos.")
                st.rerun()

        # A/B stats (shown whenever there are templates with data)
        if linked_tmpls:
            stats = get_template_stats(camp["id"])
            if stats and any(s["total_sent"] > 0 for s in stats):
                df_stats = pd.DataFrame(stats)
                df_stats["taxa"] = df_stats.apply(
                    lambda r: f"{r['replies']/r['total_sent']*100:.1f}%" if r["total_sent"] else "—",
                    axis=1,
                )
                df_stats = df_stats[["name", "subject", "total_sent", "replies", "taxa"]]
                df_stats.columns = ["Template", "Assunto", "Enviados", "Respostas", "Taxa"]
                st.dataframe(df_stats, width="stretch", hide_index=True)

        # Delay settings
        d1, d2 = st.columns(2)
        min_delay = d1.number_input("Intervalo mínimo (seg)", 10, 300, 30, key=f"mind_{camp['id']}")
        max_delay = d2.number_input("Intervalo máximo (seg)", 10, 600, 90, key=f"maxd_{camp['id']}")

        # Action buttons
        b1, b2, b3, b4 = st.columns(4)

        # Start / Stop
        if is_this_running:
            if b1.button("⏹ Parar disparo", key=f"stop_{camp['id']}", type="primary"):
                sender.stop()
                st.rerun()
        else:
            can_start = (
                not state["running"]
                and camp["status"] != "done"
                and len(selected_emails) > 0
                and any(is_authenticated(e) for e in selected_emails)
            )
            if b1.button("▶️ Iniciar disparo", key=f"start_{camp['id']}", type="primary", disabled=not can_start):
                conn = get_conn()
                conn.execute("UPDATE campaigns SET status='active' WHERE id=?", (camp["id"],))
                conn.commit()
                conn.close()
                ok = sender.start(camp["id"], min_delay, max_delay)
                if ok:
                    st.success("Disparo iniciado!")
                else:
                    st.error("Já existe um disparo em andamento.")
                st.rerun()

        # Pause / Resume
        if camp["status"] == "active" and not is_this_running:
            if b2.button("⏸ Pausar", key=f"pause_{camp['id']}"):
                conn = get_conn()
                conn.execute("UPDATE campaigns SET status='paused' WHERE id=?", (camp["id"],))
                conn.commit()
                conn.close()
                st.rerun()
        elif camp["status"] == "paused":
            if b2.button("▶️ Retomar", key=f"resume_{camp['id']}"):
                conn = get_conn()
                conn.execute("UPDATE campaigns SET status='active' WHERE id=?", (camp["id"],))
                conn.commit()
                conn.close()
                st.rerun()

        # Delete
        if b4.button("🗑 Excluir", key=f"del_{camp['id']}"):
            conn = get_conn()
            conn.execute("DELETE FROM campaign_accounts WHERE campaign_id=?", (camp["id"],))
            conn.execute("DELETE FROM campaigns WHERE id=?", (camp["id"],))
            conn.commit()
            conn.close()
            st.rerun()

