import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import streamlit as st
from core.database import init_db, dashboard_stats

st.set_page_config(
    page_title="PCZ — Prospectar a Custo Zero",
    page_icon="📧",
    layout="wide",
)

init_db()

st.title("📧 Prospectar a Custo Zero")
st.caption("Gerenciador de campanhas de cold email")

st.divider()

stats = dashboard_stats()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Contas ativas",    stats["accounts"])
c2.metric("Templates",        stats["templates"])
c3.metric("Listas",           stats["lists"])
c4.metric("Campanhas ativas", stats["campaigns_active"])

st.divider()

c5, c6, c7, c8 = st.columns(4)
c5.metric("Total prospects",  stats["prospects"])
c6.metric("Pendentes",        stats["pending"])
c7.metric("Enviados",         stats["sent"])
c8.metric("Enviados hoje",    stats["sent_today"])

st.divider()
st.markdown("""
**Navegue pelas páginas no menu lateral:**

| Página | Função |
|---|---|
| 📬 Contas | Adicionar contas Gmail, configurar filtros e encaminhamento |
| ✉️ Templates | Criar e editar modelos de e-mail com variáveis |
| 📋 Listas | Importar listas de prospects (CSV / colar) |
| 🚀 Disparos | Criar campanhas e iniciar envios automáticos |
""")
