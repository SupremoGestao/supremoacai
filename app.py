"""
app.py — Versão ONLINE do painel Supremo Açaí (Streamlit)
==========================================================
Roda o mesmo dashboard.py, mas lendo a planilha ao vivo e servindo na web.
Publique de graça no Streamlit Community Cloud (veja GUIA_ONLINE.md).

As credenciais NÃO ficam no código: são lidas de st.secrets["gcp_service_account"]
(configuradas no painel do Streamlit Cloud).
"""
import datetime
import streamlit as st
import streamlit.components.v1 as components
import gspread
from google.oauth2.service_account import Credentials

import dashboard  # reaproveita todo o cálculo + geração de HTML

st.set_page_config(page_title="Supremo Açaí — Painel",
                   page_icon="🫐", layout="wide")

# tira as margens padrão do Streamlit para o painel ocupar a tela toda
st.markdown("""<style>
#MainMenu,header,footer{visibility:hidden}
.block-container{padding:0.4rem 0.6rem 0 0.6rem;max-width:100%}
</style>""", unsafe_allow_html=True)


@st.cache_data(ttl=600, show_spinner="Carregando dados da planilha…")
def gerar_html():
    info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(info, scopes=dashboard.ESCOPOS)
    cli = gspread.authorize(creds)
    return dashboard.construir(cli)


c1, c2 = st.columns([8, 1])
with c1:
    st.caption("🫐 Supremo Açaí · Painel de Estoque & Produção — dados ao vivo da planilha")
with c2:
    if st.button("🔄 Atualizar", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

try:
    html = gerar_html()
except KeyError:
    st.error("Credenciais não configuradas. No Streamlit Cloud, vá em **Settings → "
             "Secrets** e cole o bloco [gcp_service_account] (veja o GUIA_ONLINE.md).")
    st.stop()
except Exception as e:
    st.error(f"Erro ao ler a planilha: {e}")
    st.stop()

if html is None:
    st.warning("A aba **Vendas** está vazia. No Google Sheets, cole os dados com "
               "**Colar especial → Somente valores** e clique em Atualizar.")
else:
    components.html(html, height=1900, scrolling=True)
    st.caption(f"Atualizado às {datetime.datetime.now():%H:%M} · "
               "os dados são recarregados a cada 10 min ou ao clicar em Atualizar.")
