import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

_HUB_DASH = Path(__file__).parents[3] / "hub" / "dashboard"
if _HUB_DASH.exists() and str(_HUB_DASH) not in sys.path:
    sys.path.insert(0, str(_HUB_DASH))

import streamlit as st
from core.database import get_conn, get_templates

try:
    from components.ui import apply_theme
except ImportError:
    apply_theme = None

st.set_page_config(page_title="Templates — PCZ", page_icon="✉️", layout="wide")
if apply_theme:
    apply_theme()
st.title("✉️ Templates de E-mail")

st.info(
    "**Variáveis disponíveis:** `{{empresa}}` · `{{email}}` · `{{dominio}}`  \n"
    "Exemplo de assunto: `Agendamento — {{empresa}}`"
)

# ── create / edit ─────────────────────────────────────────────────────────────
edit_id = st.session_state.get("edit_template_id")
editing = None
if edit_id:
    conn = get_conn()
    row = conn.execute("SELECT * FROM templates WHERE id=?", (edit_id,)).fetchone()
    conn.close()
    editing = dict(row) if row else None

with st.form("template_form"):
    st.subheader("✏️ Editar template" if editing else "➕ Novo template")

    name    = st.text_input("Nome interno",        value=editing["name"]    if editing else "")
    subject = st.text_input("Assunto do e-mail",   value=editing["subject"] if editing else "")
    body    = st.text_area("Corpo do e-mail",       value=editing["body"]   if editing else "",
                            height=300,
                            placeholder="Olá, tudo bem?\n\nMeu nome é {{empresa}}...\n\nAtt,\nSeu nome")

    c1, c2 = st.columns(2)
    save = c1.form_submit_button("💾 Salvar", type="primary")
    cancel = c2.form_submit_button("✖ Cancelar")

if cancel:
    st.session_state.pop("edit_template_id", None)
    st.rerun()

if save:
    if not name.strip() or not subject.strip() or not body.strip():
        st.error("Preencha todos os campos.")
    else:
        conn = get_conn()
        if editing:
            conn.execute(
                "UPDATE templates SET name=?, subject=?, body=? WHERE id=?",
                (name.strip(), subject.strip(), body.strip(), edit_id),
            )
        else:
            conn.execute(
                "INSERT INTO templates(name, subject, body) VALUES(?,?,?)",
                (name.strip(), subject.strip(), body.strip()),
            )
        conn.commit()
        conn.close()
        st.session_state.pop("edit_template_id", None)
        st.success("Template salvo!")
        st.rerun()

st.divider()

# ── list templates ────────────────────────────────────────────────────────────
st.subheader("Templates cadastrados")

templates = get_templates()
if not templates:
    st.info("Nenhum template ainda. Crie o primeiro acima.")
else:
    for t in templates:
        with st.expander(f"**{t['name']}** — `{t['subject']}`"):
            st.markdown(f"**Assunto:** {t['subject']}")
            st.text_area("Corpo", value=t["body"], height=200, disabled=True, key=f"body_{t['id']}")

            col1, col2, col3 = st.columns([1, 1, 6])
            if col1.button("✏️ Editar", key=f"edit_{t['id']}"):
                st.session_state["edit_template_id"] = t["id"]
                st.rerun()

            if col2.button("🗑 Excluir", key=f"del_{t['id']}"):
                conn = get_conn()
                conn.execute("DELETE FROM templates WHERE id=?", (t["id"],))
                conn.commit()
                conn.close()
                st.rerun()
