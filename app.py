"""
app.py — Versão ONLINE do painel Supremo Açaí (Streamlit)
==========================================================
Roda o mesmo dashboard.py, mas lendo a planilha ao vivo e servindo na web.

As credenciais NÃO ficam no código: são lidas de st.secrets["gcp_service_account"]
(configuradas no painel do Streamlit Cloud). A conta de serviço precisa ter
permissão de EDITOR na planilha (não só Leitor) para a Contagem Física
gravar direto no Sheets — veja "Compartilhar" no Google Sheets.
"""
import datetime
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
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


@st.cache_resource(show_spinner=False)
def obter_cliente():
    info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(info, scopes=dashboard.ESCOPOS)
    return gspread.authorize(creds)


@st.cache_resource(show_spinner=False)
def obter_planilha_estoque(_cli):
    return _cli.open_by_key(dashboard.ID_ESTOQUE)


@st.cache_data(ttl=600, show_spinner="Carregando dados da planilha…")
def gerar_html(_cli):
    return dashboard.construir(_cli)


@st.cache_data(ttl=300, show_spinner="Carregando cadastro de produtos…")
def carregar_master(_cli):
    master, _ = dashboard.carregar_master_leve(_cli)
    return master


SUBAREAS = ["Câmara Fria", "Polpas (Aqui)", "Polpas (Lockfrio)", "Estoque Seco", "Reprocesso"]

c1, c2 = st.columns([8, 1])
with c1:
    st.caption("🫐 Supremo Açaí · Painel de Estoque & Produção — dados ao vivo da planilha")
with c2:
    if st.button("🔄 Atualizar", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

try:
    cli = obter_cliente()
except KeyError:
    st.error("Credenciais não configuradas. No Streamlit Cloud, vá em **Settings → "
             "Secrets** e cole o bloco [gcp_service_account].")
    st.stop()

try:
    html = gerar_html(cli)
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

# ══════════════════════════════════════════════════════════════════════
# CONTAGEM FÍSICA — grava direto na planilha (Inventário / Reprocesso)
# ══════════════════════════════════════════════════════════════════════
st.divider()
st.subheader("📋 Contagem Física")
st.caption("Preenche e grava direto na planilha — some da tela assim que salvo, "
           "e a reconciliação (aba Inventário do painel acima) já reflete na próxima atualização. "
           "Reforçar a contagem de um item substitui a anterior (fica registrado o histórico na planilha).")

try:
    pl_e = obter_planilha_estoque(cli)
except Exception as e:
    st.error(f"Não consegui abrir a planilha para gravação: {e}")
    st.stop()

sub = st.selectbox("Área", SUBAREAS, key="sub_contagem")

if sub == "Reprocesso":
    with st.form("form_reproc", clear_on_submit=True):
        fc1, fc2, fc3 = st.columns([3, 1, 1])
        produto = fc1.text_input("Produto")
        litragem = fc2.selectbox("Embalagem", [5, 10])
        qtd = fc3.number_input("Qtd", min_value=0.0, step=1.0)
        validade = st.text_input("Validade (dd/mm/aaaa — ou deixe em branco/'teste' "
                                 "para produto novo sem cadastro)")
        enviar = st.form_submit_button("➕ Adicionar ao Reprocesso")
    if enviar:
        if not produto.strip():
            st.error("Informe o nome do produto.")
        else:
            ok = dashboard.salvar_reprocesso(pl_e, produto.strip(), litragem, qtd,
                                             validade.strip() or "teste")
            if ok:
                st.success(f"'{produto}' adicionado ao Reprocesso ({litragem}L × {qtd:.0f}).")
                st.cache_data.clear()
            else:
                st.error("Aba 'Reprocessos' não encontrada na planilha.")

else:
    master = carregar_master(cli)
    itens = dashboard.itens_por_subarea(master, sub)
    busca = st.text_input("🔍 Buscar produto", key=f"busca_{sub}")
    if busca:
        itens = [i for i in itens if busca.upper() in i["produto"].upper()]

    if not itens:
        st.info("Nenhum item encontrado para esta área/busca.")
    else:
        st.caption(f"{len(itens)} itens · preencha só o que for contar agora, deixe o resto em branco")
        df = pd.DataFrame(itens)[["ref", "produto", "eat"]]
        df.columns = ["Ref", "Produto", "Sistema"]
        df["Contado"] = None
        df["Observação"] = ""
        edited = st.data_editor(
            df, disabled=["Ref", "Produto", "Sistema"],
            use_container_width=True, hide_index=True, num_rows="fixed",
            key=f"editor_{sub}",
            column_config={
                "Contado": st.column_config.NumberColumn(min_value=0, step=1),
                "Observação": st.column_config.TextColumn(width="medium"),
            })
        if st.button("💾 Salvar contagem", key=f"salvar_{sub}", type="primary"):
            linhas = [{"ref": int(r["Ref"]), "produto": r["Produto"],
                      "contado": r["Contado"], "obs": r["Observação"]}
                     for _, r in edited.iterrows() if r["Contado"] not in (None, "")]
            if not linhas:
                st.warning("Nenhum item preenchido — digite a quantidade contada na coluna 'Contado'.")
            else:
                n = dashboard.salvar_contagem_inventario(pl_e, linhas)
                st.success(f"✓ {n} itens salvos na aba Inventário.")
                st.cache_data.clear()
                st.rerun()
