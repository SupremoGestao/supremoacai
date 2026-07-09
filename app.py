"""
Supremo Açaí — Dashboard de Gestão de Estoque (versão Streamlit)
================================================================
Porte fiel do dashboard HTML/JS para Python + Streamlit.

Replica:
  • Conexão com 2 planilhas Google Sheets via Apps Script (estoque + receitas)
  • Parsers (Consumo ABC, Altos e Baixos, Valor em Estoque, Vendas Peso)
  • buildMaster (cruzamento das 4 fontes)
  • Abas: Visão Geral · Planejamento · Polpas · Produção · Receitas · Auditoria
  • Lógica de produção (39 produtos, Para Amanhã/Semana, sazonalidade)
  • Indicação com plano de batidas (capacidade/dia configurável, padrão 90)
  • Receitas com calculador por batidas, embalagens e coluna I (consumo)
  • Separação Produção × Planejamento (produção não vira sugestão de compra)

COMO RODAR:
    pip install -r requirements.txt
    streamlit run app.py
"""
import io
import math
import unicodedata
import requests
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from logic import (
    SCRIPT_URL, REC_SCRIPT_URL, TAB_NAMES, TAB_LABELS,
    PROD_REFS, PROD_GRUPO, PROD_EMN, PROD_SAZONAIS, PROD_GROUP_NAMES,
    POLPA_REFS, LT_POLPA, REC_ABAS,
    parse_abc, parse_altos, parse_valor, parse_peso, parse_receita_aba,
    build_master, status_of, brl, n0, n1,
    fetch_sheet_csv, compute_producao, build_indicacao_plan,
)

# ──────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Supremo Açaí — Estoque",
    page_icon="🫐",
    layout="wide",
)

# Tema escuro custom (aproxima o visual do dashboard original)
st.markdown("""
<style>
    .stApp { background: #0a0a0b; }
    section[data-testid="stSidebar"] { background: #141416; }
    h1, h2, h3 { letter-spacing: -0.02em; }
    div[data-testid="stMetricValue"] { font-size: 1.6rem; }
    .badge-A { background:#14532d; color:#86efac; padding:1px 7px; border-radius:4px; font-size:11px; font-weight:700; }
    .badge-B { background:#78350f; color:#fcd34d; padding:1px 7px; border-radius:4px; font-size:11px; font-weight:700; }
    .badge-C { background:#7f1d1d; color:#fca5a5; padding:1px 7px; border-radius:4px; font-size:11px; font-weight:700; }
    .pill-red { background:#7f1d1d; color:#fca5a5; padding:2px 9px; border-radius:5px; font-size:11px; font-weight:600; }
    .pill-amber { background:#78350f; color:#fcd34d; padding:2px 9px; border-radius:5px; font-size:11px; font-weight:600; }
    .pill-green { background:#14532d; color:#86efac; padding:2px 9px; border-radius:5px; font-size:11px; font-weight:600; }
    .pill-blue { background:#1e3a5f; color:#93c5fd; padding:2px 9px; border-radius:5px; font-size:11px; font-weight:600; }
</style>
""", unsafe_allow_html=True)


# ──────────────────────────────────────────────────────────────────
# CARREGAMENTO DE DADOS (cache de 5 min)
# ──────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def carregar_estoque():
    """Busca as 4 abas de estoque e monta o MASTER."""
    S = {}
    parsers = {"abc": parse_abc, "altos": parse_altos,
               "valor": parse_valor, "peso": parse_peso}
    erros = {}
    for tipo, nome in TAB_NAMES.items():
        try:
            rows = fetch_sheet_csv(SCRIPT_URL, nome)
            S[tipo] = parsers[tipo](rows)
        except Exception as e:
            S[tipo] = {}
            erros[tipo] = str(e)
    master = build_master(S)
    return master, S, erros


@st.cache_data(ttl=300, show_spinner=False)
def carregar_receitas(master):
    """Busca as 3 abas de receitas (Açai, Gelatos, Cremes)."""
    rec_data = {}
    erros = {}
    for aba_info in REC_ABAS:
        aba = aba_info["aba"]
        try:
            rows = fetch_sheet_csv(REC_SCRIPT_URL, aba)
            rec_data[aba] = parse_receita_aba(rows, master)
        except Exception as e:
            rec_data[aba] = []
            erros[aba] = str(e)
    return rec_data, erros


def render_receita(rec, aba):
    """Renderiza a receita selecionada com calculador de batidas."""
    bmin = rec["batidasMin"]
    st.markdown(f"### {rec['titulo']}")
    st.caption(f"{aba} · {len(rec['ings'])} insumos · ×{bmin} batidas mínimas")

    embs = [e for e in rec["embalagens"] if e["label"] or e["rend"] > 0]

    batidas = st.number_input("Nº de Batidas", bmin, 10000, bmin, step=bmin,
                              key=f"calc_{rec['titulo']}")
    multiplo = math.ceil(batidas / bmin)
    batidas_final = multiplo * bmin
    fator = batidas_final / bmin

    if embs:
        rend_cols = st.columns(len(embs) + 1)
        rend_cols[0].metric("Batidas", batidas_final, f"múltiplo de {bmin}")
        for i, e in enumerate(embs):
            if e["rendPB"] > 0:
                qtd = batidas_final * e["rendPB"]
                rend_cols[i + 1].metric(f"📦 {e['label']}",
                                        f"{qtd:,.1f}".replace(",", "."))

    tem_col_i = any(g["qI"] > 0 for g in rec["ings"])
    st.markdown(f"**Consumo de insumos — {batidas_final} batidas**")
    ing_rows = []
    for ing in rec["ings"]:
        qtd = ing["qI"] * fator if tem_col_i else ing["q1b"] * batidas_final
        if qtd <= 0:
            continue
        ing_rows.append({
            "Ref": ing["ref"] if ing["temRef"] else "—",
            "Insumo": ing["nomeExib"] if ing["vinculado"] else ing["nomeOrig"],
            "1 Batida": round(ing["q1b"], 3) if ing["q1b"] else "—",
            "Consumo": round(qtd, 3),
        })
    if ing_rows:
        st.dataframe(pd.DataFrame(ing_rows), use_container_width=True, hide_index=True)

    sem_ref = sum(1 for i in rec["ings"] if not i["temRef"])
    if sem_ref:
        st.caption(f"⚠ {sem_ref} ingrediente(s) aguardam referência na planilha")


# ──────────────────────────────────────────────────────────────────
# SIDEBAR — controles globais
# ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🫐 Supremo Açaí")
    st.caption("Gestão de Estoque")

    if st.button("↻ Atualizar dados", use_container_width=True, type="primary"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.markdown("**Parâmetros de planejamento**")
    lt = st.number_input("Lead Time (dias)", 1, 90, 15)
    ss = st.number_input("Seg. Estoque (dias)", 0, 60, 10)
    periodo = st.number_input("Período histórico (meses)", 1, 12, 1,
                              help="Ajuste para o intervalo real exportado no ControleCrt")


# ──────────────────────────────────────────────────────────────────
# CARREGAR
# ──────────────────────────────────────────────────────────────────
with st.spinner("Carregando planilhas..."):
    MASTER, S, erros_estoque = carregar_estoque()
    REC_DATA, erros_rec = carregar_receitas(MASTER)

if not MASTER:
    st.error("⚠️ Não foi possível carregar os dados de estoque. "
             "Verifique se o Apps Script está publicado como 'Qualquer pessoa'.")
    if erros_estoque:
        with st.expander("Detalhes dos erros"):
            for k, v in erros_estoque.items():
                st.text(f"{TAB_LABELS.get(k, k)}: {v}")
    st.stop()

prods = list(MASTER.values())
prod_set = set(PROD_REFS)

# ──────────────────────────────────────────────────────────────────
# ABAS
# ──────────────────────────────────────────────────────────────────
tab_visao, tab_plan, tab_polpas, tab_prod, tab_rec, tab_audit = st.tabs([
    "📊 Visão Geral", "🛒 Planejamento", "🫐 Polpas",
    "🏭 Produção", "📖 Receitas", "🔍 Auditoria",
])

# ═══════════════════════════════════════════════════════════════════
# VISÃO GERAL
# ═══════════════════════════════════════════════════════════════════
with tab_visao:
    abc_filter = st.session_state.get("abc_filter", "")

    pf = [p for p in prods if (not abc_filter or p["abc"] == abc_filter)]
    total_val = sum(p["sub"] for p in pf)
    abaixo_min = sum(1 for p in S["altos"].values()
                     if p.get("emn", 0) > 0 and p.get("eat", 0) < p.get("emn", 0))
    sem_estoque = sum(1 for p in prods if p["eat"] == 0)

    suffix = f" — Curva {abc_filter}" if abc_filter else ""
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"Produtos{suffix}", n0(len(pf)))
    c2.metric(f"Valor em Estoque{' ('+abc_filter+')' if abc_filter else ''}", brl(total_val))
    c3.metric("Abaixo do Mínimo", abaixo_min)
    c4.metric("Sem Estoque", sem_estoque)

    st.divider()
    col_donut, col_bars = st.columns([1, 1.4])

    with col_donut:
        st.markdown("##### Curva ABC")
        ac = {"A": 0, "B": 0, "C": 0}
        for p in prods:
            if p["abc"] in ac:
                ac[p["abc"]] += 1
        total_abc = sum(ac.values())
        if total_abc > 0:
            fig = go.Figure(go.Pie(
                labels=["Classe A", "Classe B", "Classe C"],
                values=[ac["A"], ac["B"], ac["C"]],
                hole=0.55,
                marker=dict(colors=["#22c55e", "#f59e0b", "#ef4444"]),
                textinfo="value+percent",
            ))
            fig.update_layout(
                showlegend=True, height=320, margin=dict(t=10, b=10, l=10, r=10),
                paper_bgcolor="rgba(0,0,0,0)", font_color="#e4e4e7",
                legend=dict(orientation="h", yanchor="bottom", y=-0.15),
            )
            st.plotly_chart(fig, use_container_width=True)

        # Filtro ABC por botões
        bc1, bc2, bc3, bc4 = st.columns(4)
        if bc1.button("A", use_container_width=True):
            st.session_state["abc_filter"] = "" if abc_filter == "A" else "A"; st.rerun()
        if bc2.button("B", use_container_width=True):
            st.session_state["abc_filter"] = "" if abc_filter == "B" else "B"; st.rerun()
        if bc3.button("C", use_container_width=True):
            st.session_state["abc_filter"] = "" if abc_filter == "C" else "C"; st.rerun()
        if bc4.button("Limpar", use_container_width=True):
            st.session_state["abc_filter"] = ""; st.rerun()

    with col_bars:
        st.markdown("##### Top 10 — Estoque Atual")
        pbar = [p for p in prods if (not abc_filter or p["abc"] == abc_filter)]
        top_est = sorted([p for p in pbar if p["eat"] > 0],
                         key=lambda p: -p["eat"])[:10]
        if top_est:
            df = pd.DataFrame([{"Produto": p["prod"][:30], "Estoque": p["eat"]}
                              for p in top_est])
            st.bar_chart(df.set_index("Produto"), horizontal=True, color="#3b82f6")

        st.markdown("##### Top 10 — Abaixo do Mínimo")
        top_altos = sorted(S["altos"].values(),
                           key=lambda p: -p.get("dif", 0))[:10]
        if top_altos:
            df2 = pd.DataFrame([{"Produto": str(p.get("prod", ""))[:30],
                                 "Déficit": p.get("dif", 0)} for p in top_altos])
            st.bar_chart(df2.set_index("Produto"), horizontal=True, color="#ef4444")


# ═══════════════════════════════════════════════════════════════════
# PLANEJAMENTO (só compra — exclui produção)
# ═══════════════════════════════════════════════════════════════════
with tab_plan:
    st.info("🛒 Esta aba mostra apenas itens de **compra**. "
            "Os 39 produtos de fabricação própria estão na aba **🏭 Produção**.")

    # Excluir produtos de produção
    plan_prods = [p for p in prods if p["ref"] not in prod_set]

    rows_calc = []
    counts = {"urgente": 0, "planejar": 0, "ok": 0, "sem": 0}
    for p in plan_prods:
        dmm = p["qtdVnd"] / periodo if periodo > 0 else 0
        dmd = dmm / 30
        emd = math.ceil(dmd * (lt + ss)) if dmd > 0 else 0
        cob = (p["eat"] / dmd) if dmd > 0 else None
        sug = max(0, emd + dmd * lt - p["eat"]) if dmd > 0 else 0
        st_ = status_of(cob, lt, ss)
        if p["eat"] > 0 or p["qtdVnd"] > 0:
            counts[st_] += 1
        rows_calc.append({"p": p, "dmm": dmm, "emd": emd, "cob": cob,
                          "sug": sug, "st": st_})

    k1, k2, k3 = st.columns(3)
    k1.metric("🔴 Urgente — comprar agora", counts["urgente"])
    k2.metric("🟡 Planejar — em breve", counts["planejar"])
    k3.metric("✅ OK — coberto", counts["ok"])

    fc1, fc2 = st.columns(2)
    f_abc = fc1.multiselect("Curva ABC", ["A", "B", "C", "-"], default=[])
    f_status = fc2.multiselect("Status",
                               ["urgente", "planejar", "ok", "sem"], default=[])

    filtered = []
    for rc in rows_calc:
        p, st_ = rc["p"], rc["st"]
        if f_abc and (p["abc"] or "-") not in f_abc:
            continue
        if f_status and st_ not in f_status:
            continue
        if p["eat"] == 0 and p["qtdVnd"] == 0 and "sem" not in f_status:
            continue
        filtered.append(rc)

    badge = {"urgente": "🔴", "planejar": "🟡", "ok": "✅", "sem": "⚪"}
    df_plan = pd.DataFrame([{
        "Ref": rc["p"]["ref"],
        "Produto": rc["p"]["prod"],
        "ABC": rc["p"]["abc"],
        "Est. Atual": round(rc["p"]["eat"], 1),
        "Est. Mín.": rc["emd"] or "—",
        "DMM": round(rc["dmm"], 1) if rc["dmm"] > 0 else "—",
        "Cobertura (d)": round(rc["cob"], 1) if rc["cob"] is not None else "—",
        "Sug. Compra": round(rc["sug"], 1) if rc["sug"] > 0 else "—",
        "Status": f'{badge[rc["st"]]} {rc["st"]}',
    } for rc in filtered[:400]])

    st.dataframe(df_plan, use_container_width=True, hide_index=True, height=500)


# ═══════════════════════════════════════════════════════════════════
# POLPAS (LT 60d + contagem física)
# ═══════════════════════════════════════════════════════════════════
with tab_polpas:
    st.markdown(f"#### 🫐 Polpas — Lead Time fixo: {LT_POLPA} dias")

    polpas = []
    for ref in POLPA_REFS:
        m = MASTER.get(ref, {"ref": ref, "prod": f"Ref {ref}", "abc": "-",
                             "qtdVnd": 0, "eat": 0, "cst": 0, "peso": 0})
        polpas.append(m)

    total_est = sum(p.get("eat", 0) for p in polpas)
    total_peso = sum(p.get("peso", 0) for p in polpas)

    cc1, cc2 = st.columns([2, 1])
    cc1.metric("📦 Estoque Total de Polpas", n1(total_est),
               help=f"{len(POLPA_REFS)} polpas · LT {LT_POLPA}d")
    if total_peso > 0:
        cc2.metric("Peso Vendido", f"{n1(total_peso)} Kg")

    st.divider()
    st.caption("Preencha a contagem física para ver a diferença vs sistema")

    for p in polpas:
        dmm = p.get("qtdVnd", 0) / periodo if periodo > 0 else 0
        dmd = dmm / 30
        emd = math.ceil(dmd * (LT_POLPA + ss)) if dmd > 0 else 0
        cob = (p.get("eat", 0) / dmd) if dmd > 0 else None
        sug = max(0, emd + dmd * LT_POLPA - p.get("eat", 0)) if dmd > 0 else 0
        st_ = status_of(cob, LT_POLPA, ss)

        with st.container(border=True):
            cols = st.columns([2.5, 1, 1, 1, 1, 1.3])
            badge_map = {"A": "badge-A", "B": "badge-B", "C": "badge-C"}
            cls = badge_map.get(p.get("abc", "-"), "")
            cols[0].markdown(
                f"**{p['prod']}**<br>"
                f"<span class='{cls}'>{p.get('abc','-')}</span> "
                f"<span style='color:#71717a;font-size:11px'>Ref {p['ref']}</span>",
                unsafe_allow_html=True)
            cols[1].metric("Est. Atual", n1(p.get("eat", 0)))
            cols[2].metric("Est. Mín.", n0(emd) if emd > 0 else "—")
            cols[3].metric("Cobertura", f"{n1(cob)}d" if cob is not None else "—")
            cols[4].metric("Sug. Compra", n1(sug) if sug > 0 else "OK")
            contagem = cols[5].number_input(
                "Contagem física", value=0.0, step=0.5,
                key=f"polpa_cnt_{p['ref']}", label_visibility="collapsed",
                placeholder="Contagem")
            if contagem > 0:
                dif = contagem - p.get("eat", 0)
                cor = "🟢" if dif >= 0 else "🔴"
                cols[5].caption(f"{cor} {'+' if dif>=0 else ''}{n1(dif)} vs sistema")


# ═══════════════════════════════════════════════════════════════════
# PRODUÇÃO (39 produtos próprios)
# ═══════════════════════════════════════════════════════════════════
with tab_prod:
    st.markdown("#### 🏭 Produção — produtos de fabricação própria")
    prod_periodo = st.number_input("Período histórico (meses)", 1, 12, 1,
                                   key="prod_periodo",
                                   help="DMD = Qtd vendida ÷ (período × 30)")

    prod_rows = compute_producao(MASTER, S, prod_periodo)

    cnt = {"amanha": 0, "semana": 0, "monitorar": 0, "sazonal": 0, "sem_hist": 0}
    for r in prod_rows:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    sug_a_total = sum(r["sug_amanha"] for r in prod_rows if r["status"] == "amanha")
    sug_s_total = sum(r["sug_semana"] for r in prod_rows if r["status"] == "semana")

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total", len(PROD_REFS))
    k2.metric("🔴 Para Amanhã", cnt["amanha"], f"{n0(sug_a_total)} un.")
    k3.metric("🟡 Para Semana", cnt["semana"], f"{n0(sug_s_total)} un.")
    k4.metric("🟢 Monitorar", cnt["monitorar"])
    k5.metric("❄️ Sazonais", cnt["sazonal"])

    # ── Botão Indicação (plano de batidas) ──
    st.divider()
    with st.expander("🧠 **Indicação de Produção — Plano de Batidas (dia seguinte)**", expanded=False):
        cap = st.number_input("Capacidade do dia (batidas)", 1, 500, 90,
                              help="Total de batidas que cabem na produção do dia")
        plano, restante, sem_receita = build_indicacao_plan(
            prod_rows, REC_DATA, cap)
        usadas = cap - restante
        st.progress(min(usadas / cap, 1.0),
                    text=f"{usadas}/{cap} batidas ({round(usadas/cap*100)}%)")

        if plano:
            df_plano = pd.DataFrame([{
                "Batidas": pl["batidas"],
                "Produto": pl["prod"],
                "Receita": pl["receita"],
                "Embalagem": pl["emb"],
                "Rendimento (cx)": round(pl["rendimento"], 1),
                "Necessidade": pl["necessidade"],
                "Status": "🔴" if pl["status"] == "amanha" else "🟡",
            } for pl in plano])
            st.dataframe(df_plano, use_container_width=True, hide_index=True)
            if restante > 0:
                st.success(f"✓ {restante} batidas livres — capacidade para adiantar produção")
            else:
                st.warning("⚠ Capacidade do dia esgotada — restante fica para o próximo dia")
        else:
            if not any(REC_DATA.values()):
                st.warning("⚠ Receitas não carregadas — verifique a aba 📖 Receitas")
            else:
                st.info("Nenhum produto com necessidade de produção no momento")
        if sem_receita:
            nomes = " · ".join(r["prod"] for r in sem_receita[:5])
            st.caption(f"⚠ Sem receita vinculada (produzir por unidades): {nomes}")

    # ── Tabela por grupo ──
    st.divider()
    filtro_prod = st.radio("Filtrar", ["Todos", "🔴 Para Amanhã", "🟡 Para Semana",
                                       "🟢 Monitorar"], horizontal=True)
    fmap = {"Todos": None, "🔴 Para Amanhã": "amanha",
            "🟡 Para Semana": "semana", "🟢 Monitorar": "monitorar"}
    fstatus = fmap[filtro_prod]

    for grp_key, grp_name in PROD_GROUP_NAMES.items():
        grp_rows = [r for r in prod_rows if r["grp"] == grp_key]
        if fstatus:
            grp_rows = [r for r in grp_rows if r["status"] == fstatus]
        if not grp_rows:
            continue
        st.markdown(f"##### {grp_name} ({len(grp_rows)})")
        order = {"amanha": 0, "semana": 1, "monitorar": 2, "sem_hist": 3, "sazonal": 4}
        grp_rows.sort(key=lambda r: (order.get(r["status"], 9),
                                     r["cob"] if r["cob"] is not None else 9999))
        badge = {"amanha": "🔴 Amanhã", "semana": "🟡 Semana",
                 "monitorar": "🟢 Monitorar", "sazonal": "❄️ Sazonal",
                 "sem_hist": "⚪ Sem hist."}
        df_grp = pd.DataFrame([{
            "Produto": r["prod"],
            "ABC": r["cls"],
            "Est. Atual": round(r["eat"], 1),
            "Déficit": n0(r["dif"]) if r["dif"] > 0 else "OK",
            "DMD/d": round(r["dmd"], 2) if r["dmd"] > 0 else "—",
            "Cobertura": f'{round(r["cob"],1)}d' if r["cob"] is not None else "—",
            "🔴 Amanhã": n0(r["sug_amanha"]) if r["status"] == "amanha" else "—",
            "🟡 Semana": n0(r["sug_semana"]) if r["status"] == "semana" else "—",
            "Status": badge.get(r["status"], r["status"]),
        } for r in grp_rows])
        st.dataframe(df_grp, use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════
# RECEITAS (calculador de batidas + embalagens + col I)
# ═══════════════════════════════════════════════════════════════════
with tab_rec:
    st.markdown("#### 📖 Receitas da Produção")

    total_rec = sum(len(v) for v in REC_DATA.values())
    if total_rec == 0:
        st.warning("⚠ Nenhuma receita carregada. Verifique se o Apps Script "
                   "de receitas está publicado.")
        if erros_rec:
            with st.expander("Erros"):
                for k, v in erros_rec.items():
                    st.text(f"{k}: {v}")
    else:
        rc1, rc2, rc3, rc4 = st.columns(4)
        rc1.metric("Total de Receitas", total_rec)
        rc2.metric("🫐 Açaí", len(REC_DATA.get("Açai", [])))
        rc3.metric("🍨 Gelatos", len(REC_DATA.get("Gelatos", [])))
        rc4.metric("🍦 Cremes", len(REC_DATA.get("Cremes", [])))

        st.divider()
        aba_sel = st.radio("Categoria", ["Todas"] + [a["aba"] for a in REC_ABAS],
                           horizontal=True)
        busca = st.text_input("🔍 Buscar receita", "")

        # Montar lista
        items = []
        abas_show = [aba_sel] if aba_sel != "Todas" else [a["aba"] for a in REC_ABAS]
        for aba in abas_show:
            for rec in REC_DATA.get(aba, []):
                if busca and busca.lower() not in rec["titulo"].lower():
                    continue
                items.append((aba, rec))

        if not items:
            st.info("Nenhuma receita encontrada")
        else:
            nomes = [f"{a} — {r['titulo']}" for a, r in items]
            escolha = st.selectbox("Selecione uma receita para ver o calculador",
                                   ["—"] + nomes)
            if escolha != "—":
                idx = nomes.index(escolha)
                aba, rec = items[idx]
                render_receita(rec, aba)


# ═══════════════════════════════════════════════════════════════════
# AUDITORIA
# ═══════════════════════════════════════════════════════════════════
with tab_audit:
    st.markdown("#### 🔍 Auditoria — contagem física vs sistema")
    st.caption("Compare o estoque do sistema com a contagem física e veja o impacto financeiro")

    busca_aud = st.text_input("🔍 Buscar produto", "", key="audit_busca")
    prods_aud = sorted(prods, key=lambda p: -p["sub"])
    if busca_aud:
        prods_aud = [p for p in prods_aud
                     if busca_aud.lower() in p["prod"].lower()
                     or busca_aud in str(p["ref"])]

    df_aud = pd.DataFrame([{
        "Ref": p["ref"],
        "Produto": p["prod"],
        "ABC": p["abc"],
        "Est. Sistema": round(p["eat"], 1),
        "Custo Unit.": brl(p["cst"]),
        "Valor Total": brl(p["sub"]),
    } for p in prods_aud[:200]])
    st.dataframe(df_aud, use_container_width=True, hide_index=True, height=500)
