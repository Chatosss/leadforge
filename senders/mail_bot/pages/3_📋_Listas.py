import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

_HUB_DASH = Path(__file__).parents[3] / "hub" / "dashboard"
if _HUB_DASH.exists() and str(_HUB_DASH) not in sys.path:
    sys.path.insert(0, str(_HUB_DASH))

import io
import pandas as pd
import streamlit as st
from core.database import get_conn, get_lists

try:
    from components.ui import apply_theme
except ImportError:
    apply_theme = None

st.set_page_config(page_title="Listas — PCZ", page_icon="📋", layout="wide")
if apply_theme:
    apply_theme()
st.title("📋 Listas de Prospects")

# ── create list ───────────────────────────────────────────────────────────────
st.subheader("Nova lista")

tab_upload, tab_paste = st.tabs(["📁 Upload CSV/Excel", "📝 Colar e-mails"])

# -- upload tab
with tab_upload:
    list_name_up = st.text_input("Nome da lista", key="list_name_up", placeholder="Dentistas SP")
    uploaded_file = st.file_uploader(
        "Selecione um arquivo CSV ou Excel",
        type=["csv", "xlsx", "xls"],
        key="list_file",
    )

    if uploaded_file:
        try:
            if uploaded_file.name.endswith(".csv"):
                df = pd.read_csv(uploaded_file)
            else:
                df = pd.read_excel(uploaded_file)
            st.write("Pré-visualização:", df.head())

            # Column mapping
            cols = list(df.columns)
            st.markdown("**Mapeie as colunas:**")
            m1, m2, m3 = st.columns(3)
            email_col   = m1.selectbox("Coluna de e-mail *",   [""] + cols, key="col_email")
            company_col = m2.selectbox("Coluna de empresa",     [""] + cols, key="col_company")
            domain_col  = m3.selectbox("Coluna de domínio",     [""] + cols, key="col_domain")

            if st.button("💾 Importar lista", type="primary", key="import_upload"):
                if not list_name_up.strip():
                    st.error("Informe o nome da lista.")
                elif not email_col:
                    st.error("Selecione a coluna de e-mail.")
                else:
                    conn = get_conn()
                    cursor = conn.execute("INSERT INTO lists(name) VALUES(?)", (list_name_up.strip(),))
                    list_id = cursor.lastrowid
                    inserted = 0
                    for _, row in df.iterrows():
                        email = str(row.get(email_col, "")).strip()
                        if "@" not in email:
                            continue
                        company = str(row.get(company_col, "")).strip() if company_col else ""
                        domain  = str(row.get(domain_col,  "")).strip() if domain_col  else ""
                        conn.execute(
                            "INSERT INTO prospects(list_id, email, company, domain) VALUES(?,?,?,?)",
                            (list_id, email, company or None, domain or None),
                        )
                        inserted += 1
                    conn.commit()
                    conn.close()
                    st.success(f"✅ {inserted} prospects importados para **{list_name_up}**!")
                    st.rerun()
        except Exception as e:
            st.error(f"Erro ao ler arquivo: {e}")

# -- paste tab
with tab_paste:
    list_name_paste = st.text_input("Nome da lista", key="list_name_paste", placeholder="Advogados RJ")
    st.caption("Um e-mail por linha. Formato aceito: `email`, `email,empresa` ou `email,empresa,dominio`")
    pasted = st.text_area("Cole os e-mails aqui", height=250, key="pasted_emails",
                          placeholder="contato@empresa.com.br\nvendas@outra.com,Outra Empresa,outra.com")

    if st.button("💾 Importar lista", type="primary", key="import_paste"):
        if not list_name_paste.strip():
            st.error("Informe o nome da lista.")
        elif not pasted.strip():
            st.error("Cole pelo menos um e-mail.")
        else:
            lines = [l.strip() for l in pasted.strip().splitlines() if l.strip()]
            conn = get_conn()
            cursor = conn.execute("INSERT INTO lists(name) VALUES(?)", (list_name_paste.strip(),))
            list_id = cursor.lastrowid
            inserted = 0
            for line in lines:
                parts = [p.strip() for p in line.split(",")]
                email = parts[0]
                if "@" not in email:
                    continue
                company = parts[1] if len(parts) > 1 else None
                domain  = parts[2] if len(parts) > 2 else None
                conn.execute(
                    "INSERT INTO prospects(list_id, email, company, domain) VALUES(?,?,?,?)",
                    (list_id, email, company, domain),
                )
                inserted += 1
            conn.commit()
            conn.close()
            st.success(f"✅ {inserted} prospects importados para **{list_name_paste}**!")
            st.rerun()

st.divider()

# ── list all lists ────────────────────────────────────────────────────────────
st.subheader("Listas cadastradas")

lists = get_lists()
if not lists:
    st.info("Nenhuma lista ainda.")
else:
    for lst in lists:
        with st.expander(
            f"**{lst['name']}** — {lst['total']} prospects "
            f"({lst['pending']} pendentes · {lst['sent']} enviados)"
        ):
            conn = get_conn()
            prospects = conn.execute(
                "SELECT * FROM prospects WHERE list_id=? ORDER BY id LIMIT 100",
                (lst["id"],),
            ).fetchall()
            conn.close()

            if prospects:
                df_show = pd.DataFrame([dict(p) for p in prospects])[
                    ["email", "company", "domain", "status", "sent_at"]
                ]
                st.dataframe(df_show, width="stretch", height=250)

            col1, col2 = st.columns([1, 8])

            # Export
            if col1.button("⬇️ Exportar", key=f"exp_{lst['id']}"):
                conn = get_conn()
                all_p = conn.execute(
                    "SELECT email, company, domain, status, sent_at, sent_by FROM prospects WHERE list_id=?",
                    (lst["id"],),
                ).fetchall()
                conn.close()
                csv = pd.DataFrame([dict(p) for p in all_p]).to_csv(index=False)
                st.download_button(
                    "⬇️ Baixar CSV",
                    data=csv,
                    file_name=f"{lst['name']}.csv",
                    mime="text/csv",
                    key=f"dl_{lst['id']}",
                )

            if col2.button("🗑 Excluir lista", key=f"del_{lst['id']}"):
                conn = get_conn()
                conn.execute("DELETE FROM prospects WHERE list_id=?", (lst["id"],))
                conn.execute("DELETE FROM lists WHERE id=?", (lst["id"],))
                conn.commit()
                conn.close()
                st.rerun()
