"""
dashboard.py — App de gestão do Supremo Açaí (estoque + produção/MRP)
=====================================================================
Gera 'dashboard_supremo.html': aplicação web OFFLINE, com abas e filtros
interativos. Sem Chart.js/CDN (gráficos em SVG, JS puro embutido).

MODELO (a reposição depende da natureza do item):
  • Produção (39 itens) → PRODUZIR (batidas). Só na aba Produção.
  • Polpas (POLPA no nome) → aba Polpas. Fora de Compras.
  • Insumos e demais itens → COMPRAR. Aba Compras.
Necessidade de compra:
  • Insumo/polpa (consumido na produção): comprar = consumo − estoque.
  • Revenda (tem venda): comprar = alvo(venda) − estoque.
  • Produção: precisa PRODUÇÃO, não compra.

Rode com ▶ (mesma pasta que logic.py e credenciais.json.json).
"""
import os
import re
import sys
import json
import math
import calendar
import datetime as _dt
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

from logic import (
    TAB_NAMES, REC_ABAS, PROD_REFS, PROD_GRUPO, POLPA_REFS, LT_POLPA,
    parse_abc as _abc_2linhas, parse_altos, parse_valor, parse_peso as _peso_2linhas,
    build_master, parse_receita_aba, _norm, _num,
)
try:
    from classificacao import (CAMARA_FRIA_REFS, REPROCESSO_REFS,
                                POLPAS_AQUI_REFS, POLPAS_LOCKFRIO_REFS,
                                LITROS_UN_OVERRIDE, REPROCESSO_APELIDOS)
except ImportError:
    CAMARA_FRIA_REFS = REPROCESSO_REFS = set()
    POLPAS_AQUI_REFS = POLPAS_LOCKFRIO_REFS = set()
    LITROS_UN_OVERRIDE = {}
    REPROCESSO_APELIDOS = {}

PASTA = os.path.dirname(os.path.abspath(__file__))
_a = os.path.join(PASTA, "credenciais.json.json")
_b = os.path.join(PASTA, "credenciais.json")
CRED = _a if os.path.exists(_a) else _b
ID_ESTOQUE = "1g26gBmyxf_0a8U7c_ELyevolNS1bByBdUWqy56or774"
ID_RECEITAS = "1hbCJrbWeKa0sa19pgcgEVsEjoP3AuhmBvanTJaVobhw"

MESES_DMD, MESES_ABC = 3, 12
LEAD_TIME_PADRAO, COBERTURA_ALVO = 15, 15
ABC_A, ABC_B = 80, 95
HORIZONTE_MESES = 1
COB_ALVO_DIAS = 7
CAP_DIA_BATIDAS = 80
META_LITROS_MES = 55000     # meta mensal total (açaí + cremes + gelato) — de PRODUÇÃO
DIAS_UTEIS_SEMANA = 5       # trabalha de segunda a sexta

# Feriados de Fortaleza (municipais + estaduais + nacionais)
FERIADOS = {
    2026: {(1, 1), (3, 19), (3, 25), (4, 3), (4, 13), (4, 21), (5, 1),
           (6, 4), (8, 15), (9, 7), (10, 12), (11, 2), (11, 15), (11, 20), (12, 25)},
}


def eh_feriado(d):
    return (d.month, d.day) in FERIADOS.get(d.year, set())


def dias_uteis(ano, mes, apartir=None):
    """Nº de dias úteis (seg-sex, sem feriados) no mês. Se 'apartir' vier, conta só de hoje ao fim."""
    ini = apartir.day if (apartir and apartir.year == ano and apartir.month == mes) else 1
    fim = calendar.monthrange(ano, mes)[1]
    return sum(1 for d in range(ini, fim + 1)
               if _dt.date(ano, mes, d).weekday() < DIAS_UTEIS_SEMANA
               and not eh_feriado(_dt.date(ano, mes, d)))
GRUPO_ABA = {"mix": "Açai", "cremes": "Cremes", "gelatos": "Gelatos"}
GRUPO_NOME = {"mix": "Açaí", "cremes": "Cremes", "gelatos": "Gelato"}
GRUPO_DENS = {"mix": 0.8, "cremes": 0.7, "gelatos": 0.6}
TAM_PADRAO_L = 5.0
OVERRIDE_REC = {1358: "SUPREMO", 1459: "SUPREMO", 1305: "SUPREMO",
                1331: "SUPREMO", 1347: "SUPREMO"}
STOP = {"DE", "DA", "DO", "DOS", "DAS", "COM", "E", "C", "A", "O",
        "CX", "CAIXA", "SACHE", "EM", "POLPA"}
SIZE_TOK = re.compile(r"^\d+(?:[.,]\d+)?(LTS|LT|L|ML|KG|G)?$")
ESCOPOS = ["https://www.googleapis.com/auth/spreadsheets.readonly",
           "https://www.googleapis.com/auth/drive.readonly"]
C_DEEP, C_ACAI, C_BERRY, C_POLPA = "#2C1640", "#4A2270", "#9B3FD1", "#7B2FC9"
C_FALTA = "#C0392B"


def nf(v, d=0):
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "—"


def esc(s):
    import html
    return html.escape(str(s if s is not None else "—"))


# ── SVG ───────────────────────────────────────────────────────────
def svg_hbars(rows, unit="", w=720, lab_w=190):
    if not rows:
        return "<p class='muted'>sem dados</p>"
    bh, gap, top = 22, 9, 6
    mx = max((r[1] for r in rows), default=1) or 1
    innerw = w - lab_w - 80
    h = top + len(rows) * (bh + gap)
    o = [f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" xmlns="http://www.w3.org/2000/svg">']
    y = top
    for r in rows:
        c = r[2] if len(r) > 2 else C_BERRY
        bw = max(2, innerw * (r[1] or 0) / mx)
        o.append(f'<text x="{lab_w-8}" y="{y+16:.0f}" text-anchor="end" font-size="12" fill="#2A1B3D">{esc(r[0])}</text>')
        o.append(f'<rect x="{lab_w}" y="{y}" width="{bw:.1f}" height="{bh}" rx="4" fill="{c}"/>')
        o.append(f'<text x="{lab_w+bw+6:.0f}" y="{y+16:.0f}" font-size="11" fill="#7C6E8A">{nf(r[1])}{unit}</text>')
        y += bh + gap
    o.append('</svg>')
    return "".join(o)


def svg_grouped(rows, w=720, lab_w=200):
    if not rows:
        return "<p class='muted'>sem dados</p>"
    bh, gap, grp, top = 11, 3, 12, 6
    mx = max((max(r[1] or 0, r[2] or 0) for r in rows), default=1) or 1
    innerw = w - lab_w - 90
    h = top + len(rows) * (bh * 2 + gap + grp)
    o = [f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" xmlns="http://www.w3.org/2000/svg">']
    y = top
    for label, prec, est in rows:
        est = est or 0
        falta = est < (prec or 0)
        o.append(f'<text x="{lab_w-8}" y="{y+bh+2:.0f}" text-anchor="end" font-size="11.5" fill="#2A1B3D">{esc(label)}</text>')
        wp = max(2, innerw * (prec or 0) / mx)
        o.append(f'<rect x="{lab_w}" y="{y}" width="{wp:.1f}" height="{bh}" rx="3" fill="{C_FALTA if falta else C_BERRY}"/>')
        o.append(f'<text x="{lab_w+wp+5:.0f}" y="{y+bh*0.85:.0f}" font-size="10" fill="#7C6E8A">{nf(prec)}</text>')
        we = max(2, innerw * est / mx)
        o.append(f'<rect x="{lab_w}" y="{y+bh+gap}" width="{we:.1f}" height="{bh}" rx="3" fill="#D8CBE6"/>')
        o.append(f'<text x="{lab_w+we+5:.0f}" y="{y+bh+gap+bh*0.85:.0f}" font-size="10" fill="#7C6E8A">{nf(est)}</text>')
        y += bh * 2 + gap + grp
    o.append('</svg>')
    leg = (f'<div class="leg"><span><i style="background:{C_BERRY}"></i>Precisa</span>'
           f'<span><i style="background:#D8CBE6"></i>Estoque</span>'
           f'<span><i style="background:{C_FALTA}"></i>Falta</span></div>')
    return leg + "".join(o)


def svg_line(labels, series, w=700, h=250):
    pt, pr, pb, pl = 16, 12, 26, 48
    iw, ih = w - pl - pr, h - pt - pb
    allv = [v for _, vals, _ in series for v in vals] or [1]
    mx = max(allv) or 1
    n = max(1, len(labels) - 1)
    X = lambda i: pl + iw * i / n
    Y = lambda v: pt + ih * (1 - v / mx)
    o = [f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" xmlns="http://www.w3.org/2000/svg">']
    for k in range(4):
        yv = mx * k / 3
        yy = Y(yv)
        o.append(f'<line x1="{pl}" y1="{yy:.0f}" x2="{w-pr}" y2="{yy:.0f}" stroke="#EEE6F2"/>')
        o.append(f'<text x="{pl-6}" y="{yy+3:.0f}" text-anchor="end" font-size="10" fill="#9A93A6">{nf(yv)}</text>')
    for i, lb in enumerate(labels):
        if i % 2 == 0 or i == len(labels) - 1:
            o.append(f'<text x="{X(i):.0f}" y="{h-8}" text-anchor="middle" font-size="9" fill="#9A93A6">{esc(lb[5:7]+"/"+lb[2:4])}</text>')
    for nome, vals, col in series:
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
        o.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2.2"/>')
        for i, v in enumerate(vals):
            o.append(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="2.4" fill="{col}"/>')
    o.append('</svg>')
    leg = '<div class="leg">' + "".join(
        f'<span><i style="background:{c}"></i>{esc(n)}</span>' for n, _, c in series) + '</div>'
    return leg + "".join(o)


# ── leitura ───────────────────────────────────────────────────────
def _conv(raw):
    return [(_num(c) if _num(c) is not None else (c.strip() if c else None)) for c in raw]


def fetch_aba(pl, nome):
    raw = pl.worksheet(nome).get_all_values()
    if not raw:
        return []
    larg = max(len(r) for r in raw)
    return [_conv(list(r) + [""] * (larg - len(r))) for r in raw]


def _rec_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_aba_limpa(rows, master):
    """Lê uma aba de receita em blocos: título (nome, Qtd Base=1, %=1, batida
    mínima na col I) + ingredientes (ref A, nome B, kg/batida C, % D) + TOTAL."""
    hi = 0
    for i, r in enumerate(rows[:12]):
        if len(r) > 1 and r[1] is not None and _norm(str(r[1])) == "INSUMO":
            hi = i
            break
    cel = lambda v: "" if v is None else str(v).strip()
    recs, cur = [], None
    for r in rows[hi + 1:]:
        r = list(r) + [None] * (9 - len(r))
        A, B = cel(r[0]), cel(r[1])
        cC, cD, cI = _rec_num(r[2]), _rec_num(r[3]), _rec_num(r[8])
        if not B:
            continue
        if _norm(B) == "TOTAL":
            if cur:
                cur["kgpb_total"] = cC
                recs.append(cur)
                cur = None
            continue
        eh_titulo = (not A) and cC is not None and abs(cC - 1) < 0.02 \
            and cD is not None and abs(cD - 1) < 0.02
        if eh_titulo:
            if cur:
                recs.append(cur)
            cur = {"titulo": B, "batida_min": int(round(cI)) if (cI and cI >= 1) else 1,
                   "ings": [], "kgpb_total": None}
        elif cur is not None:
            ca = _rec_num(r[0])
            ref = int(round(ca)) if ca is not None else None
            nome_master = master.get(ref, {}).get("prod") if ref else None
            cur["ings"].append({"ref": ref, "temRef": ref is not None,
                                "nomeOrig": B, "nomeExib": nome_master or B,
                                "q1b": cC or 0.0, "pct": cD})
    if cur:
        recs.append(cur)
    return recs


def tokens(s, isp=False):
    s = re.sub(r"\[.*?\]", " ", str(s or ""))
    return [t for t in _norm(s).split()
            if t not in STOP and len(t) >= 2 and not (isp and SIZE_TOK.match(t))]


def casar_receita(nome, recs):
    pt = set(tokens(nome, True))
    cands = [(len(set(tokens(r["titulo"]))), len(r["titulo"]), r)
             for r in recs if set(tokens(r["titulo"])) and set(tokens(r["titulo"])) <= pt]
    if not cands:
        return None
    cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
    return cands[0][2]


def tamanho(nome):
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(LTS|LT|L|ML|KG|G)\b", str(nome).upper())
    if not m:
        return None, None
    v = float(m.group(1).replace(",", "."))
    u = m.group(2)
    if u in ("KG", "G"):
        return (v / 1000 if u == "G" else v), "KG"
    return (v / 1000 if u == "ML" else v), "L"


def _col(header, *keys):
    for i, h in enumerate(header):
        if any(k in _norm(h) for k in keys):
            return i
    return None


def _f(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("%", "").replace(".", "").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def parse_relatorio(rows, com_peso=False):
    """Lê o formato NOVO (1 linha por produto): Referencia|Nome|Qtd_Vendida|
    Valor_Venda|[Peso_Total]|...|Classificacao. Colunas achadas pelo nome.
    Retorna None se não reconhecer o formato (aí usa-se o parser antigo)."""
    hi = header = None
    for i, r in enumerate(rows):
        cn = [_norm(str(c)) for c in r]
        if any("REFERENCIA" in c for c in cn) and any("NOME" in c for c in cn):
            hi, header = i, r
            break
    if hi is None:
        return None

    def col(*keys):
        for j, c in enumerate(header):
            if any(k in _norm(str(c)) for k in keys):
                return j
        return None

    iref, inome = col("REFERENCIA"), col("NOME")
    iqtd, irec = col("QTD VENDIDA", "QTD_VENDIDA"), col("VALOR VENDA", "VALOR_VENDA")
    ipeso, icls = col("PESO"), col("CLASSIFIC")
    if iref is None:
        return None
    data = {}
    for r in rows[hi + 1:]:
        if iref >= len(r):
            continue
        rv = _f(r[iref])
        if rv <= 0:
            continue
        ref = round(rv)

        def val(idx):
            return _f(r[idx]) if (idx is not None and idx < len(r)) else 0.0
        prod = (str(r[inome]).strip() if (inome is not None and inome < len(r) and r[inome]) else "")
        cls = (str(r[icls]).strip() if (icls is not None and icls < len(r) and r[icls]) else "C")
        e = {"prod": prod, "qtd": round(val(iqtd)), "rec": val(irec),
             "cls": cls if cls in ("A", "B", "C") else "C"}
        if com_peso:
            e["peso"] = val(ipeso)
        data[ref] = e
    return data


def parse_abc(rows):
    d = parse_relatorio(rows)
    return d if d is not None else _abc_2linhas(rows)


def parse_peso(rows):
    d = parse_relatorio(rows, com_peso=True)
    return d if d is not None else _peso_2linhas(rows)


def ler_vendas(pl):
    raw = pl.worksheet("Vendas").get_all_values()
    if len(raw) < 2 or not any(raw[0]):
        print("!! A aba 'Vendas' está VAZIA (sem cabeçalho/linhas).")
        print("   → No Google Sheets, cole os dados com 'Colar especial → Somente")
        print("     valores' (Ctrl+Shift+V). Colar com Ctrl+V normal traz fórmulas")
        print("     que não são lidas. Depois rode de novo.")
        return None
    hdr = raw[0]
    ir, iq = _col(hdr, "REFERENCIA"), _col(hdr, "QTD")
    idt, ist = _col(hdr, "DATA VENDA", "DATA"), _col(hdr, "STATUS ID", "STATUS_ID")
    inm = _col(hdr, "NOME PRODUTO", "NOME DO PRODUTO")
    if None in (ir, iq, idt, ist):
        print("!! Colunas da Vendas não reconhecidas. Cabeçalho lido:", hdr)
        print("   Esperado: Referencia_Produto, QTD, Data_Venda, Status_ID.")
        return None
    mx = max([ir, iq, idt, ist] + ([inm] if inm is not None else []))
    dados = [(r + [""] * (mx + 1 - len(r)))[:mx + 1] for r in raw[1:]]
    v = pd.DataFrame([(d[ir], d[iq], d[idt], d[ist], d[inm] if inm is not None else "")
                      for d in dados],
                     columns=["ref", "qtd", "data", "status", "nome"])
    v = v[v["status"] == "3"]
    v["qtd"] = pd.to_numeric(v["qtd"], errors="coerce").fillna(0)
    v["data"] = pd.to_datetime(v["data"], format="%d/%m/%Y", errors="coerce")
    v["ref"] = pd.to_numeric(v["ref"], errors="coerce")
    v = v.dropna(subset=["data", "ref"])
    v["ref"] = v["ref"].round().astype(int)
    v["mes"] = v["data"].dt.to_period("M").astype(str)
    v["dia"] = v["data"].dt.strftime("%Y-%m-%d")
    mes_atual = date.today().strftime("%Y-%m")
    diario = v[v["mes"] == mes_atual].groupby(["ref", "dia"])["qtd"].sum()
    nomes = {}
    if inm is not None:
        vn = v[v["nome"].astype(str).str.strip() != ""].sort_values("data")
        nomes = {int(r): nm for r, nm in vn.groupby("ref")["nome"].last().items()}
    return (v.groupby(["ref", "mes"])["qtd"].sum(),
            v.groupby("ref")["data"].max(), diario, nomes)


def prever(mensal, ref, jan, jabc, prox):
    base = (sum(mensal.get((ref, m), 0) for m in jan) / len(jan)) if jan else 0
    vals = [mensal.get((ref, m), 0) for m in jabc]
    f = 1.0
    if len([x for x in vals if x > 0]) >= 12:
        mg = sum(vals) / len(vals)
        alvo = [mensal.get((ref, m), 0) for m in jabc if int(m[5:7]) == prox]
        if mg > 0 and alvo:
            f = max(0.3, min(3.0, (sum(alvo) / len(alvo)) / mg))
    return float(base * f), round(f, 2)


# ── MRP ───────────────────────────────────────────────────────────
def calcular_mrp(master, mensal, jan, jabc, prox, recidx, rec_por_aba, peso_un=None):
    peso_un = peso_un or {}
    info, plano, needs = {}, [], {}
    tmp = {}
    for ref in PROD_REFS:
        nome = master.get(ref, {}).get("prod", f"ref {ref}")
        grupo = PROD_GRUPO.get(ref, "mix")
        rec = (recidx.get(_norm(OVERRIDE_REC[ref])) if ref in OVERRIDE_REC
               else casar_receita(nome, rec_por_aba.get(GRUPO_ABA[grupo], [])))
        val, uni = tamanho(nome)
        if val is None:
            val, uni = TAM_PADRAO_L, "L"
        kg_med = peso_un.get(ref)
        dens_med = (kg_med / val) if (kg_med and uni == "L" and val > 0) else None
        tmp[ref] = (nome, grupo, rec, val, uni, kg_med, dens_med)
    cat_dens = {}
    for g in ("mix", "cremes", "gelatos"):
        ds = sorted(t[6] for t in tmp.values() if t[1] == g and t[6])
        cat_dens[g] = ds[len(ds) // 2] if ds else None
    for ref, (nome, grupo, rec, val, uni, kg_med, dens_med) in tmp.items():
        dens = dens_med or cat_dens.get(grupo) or GRUPO_DENS[grupo]
        litros = val if uni == "L" else val / dens
        kgun = kg_med if kg_med else (val if uni == "KG" else val * dens)
        D = prever(mensal, ref, jan, jabc, prox)[0] * HORIZONTE_MESES
        info[ref] = {"nome": nome, "grupo": grupo, "rec": rec,
                     "litros": litros, "kg": kgun, "D": D,
                     "kgpb": sum(i["q1b"] for i in rec["ings"]) if rec else 0,
                     "bmin": rec.get("batida_min", 1) if rec else 1,
                     "kgMed": round(kg_med, 2) if kg_med else None,
                     "densMed": round(dens_med, 3) if dens_med else None}
    for ref, d in info.items():
        if not d["rec"]:
            continue
        kgpb = sum(i["q1b"] for i in d["rec"]["ings"])
        if kgpb <= 0:
            continue
        bat = (d["D"] * d["kg"]) / kgpb                 # consumo mensal (dimensiona insumos)
        eatp = master.get(ref, {}).get("eat", 0)
        emn = master.get(ref, {}).get("emn", 0) or 0
        dmd_dia = d["D"] / 30
        dias = (eatp / dmd_dia) if dmd_dia > 0 else None
        bmin = d.get("bmin", 1) or 1
        # Opção 1: necessidade de produção = estoque mínimo − atual (AltosEBaixos)
        falta_un = max(0, round(emn - eatp))
        raw_bat = (falta_un * d["kg"]) / kgpb if emn > 0 else bat
        # batida mínima: produção só em múltiplos (5 de 5 em 5, 2 de 2 em 2...), arredonda p/ cima
        bat_plano = int(math.ceil(raw_bat / bmin - 1e-9) * bmin) if raw_bat > 0.001 else 0
        bat_amanha = bat_plano
        urg = ("Amanhã" if (dias is not None and dias < 2)
               else ("Semana" if (dias is not None and dias < 7) else "Monitorar"))
        plano.append({"ref": ref, "produto": d["nome"], "receita": d["rec"]["titulo"],
                      "grupo": d["grupo"], "dem": round(d["D"]),
                      "batidas": bat_plano, "bmin": bmin,
                      "eat": round(eatp), "emn": round(emn), "falta": falta_un,
                      "dias": round(dias) if dias is not None else None,
                      "urg": urg, "batAmanha": bat_amanha})
        for ing in d["rec"]["ings"]:
            if ing["temRef"]:
                key, nm, iref = ing["ref"], master.get(ing["ref"], {}).get("prod", ing["nomeExib"]), ing["ref"]
            else:
                key, nm, iref = ("SR", _norm(ing["nomeOrig"])), ing["nomeOrig"], None
            n = needs.setdefault(key, {"nome": nm, "kg": 0.0, "ref": iref})
            n["kg"] += bat * ing["q1b"]
    insumos = []
    for n in needs.values():
        iref = n["ref"]
        est = master.get(iref, {}).get("eat") if iref else None
        cob = (est / n["kg"]) if (est is not None and n["kg"] > 0) else None
        insumos.append({"ref": iref, "insumo": n["nome"], "kg": round(n["kg"], 1),
                        "est": round(est, 1) if est is not None else None,
                        "cob": round(cob, 2) if cob is not None else None,
                        "polpa": "POLPA" in _norm(n["nome"]),
                        "falta": bool(est is not None and est < n["kg"]),
                        "semref": iref is None})
    insumos.sort(key=lambda x: -x["kg"])
    cat = {g: {"L": round(sum(d["D"] * d["litros"] for d in info.values() if d["grupo"] == g)),
               "kg": round(sum(d["D"] * d["kg"] for d in info.values() if d["grupo"] == g))}
           for g in ["mix", "cremes", "gelatos"]}
    mrp_consumo = {x["ref"]: x["kg"] for x in insumos if x["ref"] is not None}
    return plano, insumos, cat, info, mrp_consumo


# ── Estoque unificado ─────────────────────────────────────────────
def calcular_estoque(master, mensal, ultima, jan, jabc, prox, mrp_consumo):
    refs_venda = {r for (r, _) in mensal.index}
    prodset = set(PROD_REFS)
    rows = []
    for p in master.values():
        ref, eat, emn, cst = p["ref"], p["eat"], p["emn"], p["cst"]
        nome = p["prod"]
        dem, fsz = (prever(mensal, ref, jan, jabc, prox) if ref in refs_venda else (0, 1.0))
        dmd_dia = dem / 30
        preco = p.get("vnd") or ((p["rec"] / p["qtdVnd"]) if p["qtdVnd"] > 0 else 0)
        sales_rev = sum(mensal.get((ref, m), 0) for m in jabc) * preco
        consumo = mrp_consumo.get(ref, 0.0)          # kg/mês consumido na produção
        is_polpa = "POLPA" in _norm(nome)
        had = ref in refs_venda

        if ref in prodset:
            acao = "Produzir"
        elif is_polpa and consumo > 0:
            acao = "Polpa"
        else:
            acao = "Comprar"

        dias = round(eat / dmd_dia) if dmd_dia > 0 else None
        if consumo > 0:                               # insumo/polpa: precisa vem da produção
            origem, precisa = "produção", round(consumo)
            comprar = max(0, round(consumo - eat))
            cob = round(eat / consumo, 2) if consumo > 0 else None
        elif dem > 0:                                 # revenda: precisa vem da venda
            alvo = dmd_dia * ((LT_POLPA if ref in POLPA_REFS else LEAD_TIME_PADRAO) + COBERTURA_ALVO)
            origem, precisa = "venda", round(dem)
            comprar = max(0, round(alvo - eat))
            cob = round(dias / 30, 2) if dias is not None else None
        else:
            origem, precisa, comprar, cob = "", 0, 0, None

        if precisa <= 0:
            status = "Sem giro" if (had and eat > 0) else ("Sem movimento" if eat > 0 else "Inativo")
        elif eat <= 0:
            status = "Ruptura"
        elif comprar > 0:
            status = "Repor"
        else:
            status = "OK"

        grupo = PROD_GRUPO.get(ref) or (p.get("grp") or "")
        valor = p.get("sub") if p.get("sub") else eat * cst
        imob = "IMOBILIZADO" in _norm(grupo)
        # Sub-área do estoque: polpa > câmara fria > reprocesso > imobilizado > seco
        if ref in POLPAS_AQUI_REFS:
            subEst, polpaTipo = "polpa", "aqui"
        elif ref in POLPAS_LOCKFRIO_REFS:
            subEst, polpaTipo = "polpa", "lockfrio"
        elif is_polpa:                              # tem "POLPA" no nome mas fora das listas
            subEst, polpaTipo = "polpa", "outra"
        elif ref in CAMARA_FRIA_REFS:
            subEst, polpaTipo = "camara", ""
        elif ref in REPROCESSO_REFS:
            subEst, polpaTipo = "reprocesso", ""
        elif imob:
            subEst, polpaTipo = "imob", ""
        else:
            subEst, polpaTipo = "seco", ""
        ult = ultima.get(ref)
        rows.append({
            "ref": ref, "produto": nome, "grupo": grupo, "acao": acao,
            "eat": round(eat, 1), "dem": round(dem, 1), "fsz": fsz,
            "dias": dias, "valor": round(valor, 2), "comprar": comprar,
            "origem": origem, "cob": cob, "consumo": round(consumo, 1),
            "precisa": precisa, "status": status, "polpa": is_polpa, "imob": imob,
            "subEst": subEst, "polpaTipo": polpaTipo,
            "cst": round(cst, 4),
            "abc_val": sales_rev + consumo * cst * 12,
            "ult": ult.strftime("%d/%m/%Y") if pd.notna(ult) else "—"})

    rows.sort(key=lambda x: -x["abc_val"])
    tot = sum(x["abc_val"] for x in rows) or 1
    ac = 0
    for x in rows:
        ac += x["abc_val"]
        pc = ac / tot * 100
        x["abc"] = "A" if pc <= ABC_A else ("B" if pc <= ABC_B else "C")
        del x["abc_val"]
    return rows


def planejar_amanha(plano, cap=CAP_DIA_BATIDAS):
    """Cronograma para o dia seguinte: prioriza quem fura antes e enche até 'cap',
    sempre em múltiplos da batida mínima de cada produto."""
    cand = [p for p in plano if (p.get("batAmanha") or 0) > 0]
    cand.sort(key=lambda p: (p["dias"] if p["dias"] is not None else 1e9, -p["batAmanha"]))
    acc, out = 0.0, []
    for p in cand:
        resta = cap - acc
        bmin = p.get("bmin", 1) or 1
        if p["batAmanha"] <= resta + 1e-9:
            produzir = p["batAmanha"]
        else:
            produzir = math.floor(resta / bmin) * bmin      # maior lote que cabe hoje
        if produzir <= 0:
            continue                                          # nem 1 lote mínimo cabe → próximo dia
        out.append({"ref": p["ref"], "produto": p["produto"], "receita": p["receita"],
                    "grupo": p.get("grupo", ""), "bmin": bmin,
                    "dias": p["dias"], "ideal": p["batAmanha"], "produzir": round(produzir, 1),
                    "acum": round(acc + produzir, 1),
                    "cabe": produzir >= p["batAmanha"] - 0.05})
        acc += produzir
    return out, round(acc, 1)


def series_diarias(litros_map, diario):
    """Litros por categoria, dia a dia, no mês atual — mesma base do gráfico
    mensal (litros_map: TODOS os produtos, incl. potes e overrides)."""
    dias = sorted({d for (_, d) in diario.index})
    out = {"mix": [], "cremes": [], "gelatos": []}
    for dia in dias:
        acc = {"mix": 0.0, "cremes": 0.0, "gelatos": 0.0}
        for ref, lm in litros_map.items():
            if lm["cat"] in acc and lm["litros"]:
                acc[lm["cat"]] += diario.get((ref, dia), 0) * lm["litros"]
        for g in acc:
            out[g].append(round(acc[g]))
    return dias, out


def _cat_vol(nome):
    n = re.sub(r"\s+", " ", _norm(nome))
    # Produto acabado "EM POLPA" (caixa/litro de venda, não insumo) → conta pela
    # natureza real (mix/creme), não como polpa. Ex: "MIX DE AÇAÍ EM POLPA CX 5LTS".
    tem_caixa = bool(re.search(r"\b(CX|CAIXA)\b", n)) or bool(re.search(r"\d+\s*(LTS|LT|L)\b", n))
    prod_em_polpa = ("EM POLPA" in n and "INSUMO" not in n and tem_caixa)
    if "POLPA" in n and not prod_em_polpa:
        return "polpa"
    if "GELATO" in n:
        return "gelatos"
    if "CREME" in n or "MOUSSE" in n or "MILKSHAKE" in n or "MILK SHAKE" in n:
        return "cremes"          # base de milkshake = base láctea, conta em cremes
    if "ACAI" in n or "MIX" in n:
        return "mix"
    return "outros"


def modelo_litros(master, peso_un):
    """Litros por unidade de CADA produto (nome + peso medido). {ref:{litros,cat}}."""
    import statistics
    amostras = {"mix": [], "cremes": [], "gelatos": []}
    for ref, kgun in peso_un.items():
        nome = master.get(ref, {}).get("prod", "")
        g = _cat_vol(nome)
        val, uni = tamanho(nome)
        if g in amostras and uni == "L" and val and kgun > 0:
            amostras[g].append(kgun / val)
    dens = {g: (round(statistics.median(v), 3) if v else GRUPO_DENS[g])
            for g, v in amostras.items()}
    out = {}
    for ref, p in master.items():
        nome = p.get("prod", "")
        g = _cat_vol(nome)
        if g not in ("mix", "cremes", "gelatos"):
            out[ref] = {"litros": 0.0, "cat": g}
            continue
        # 1) Override manual (para produtos cujo nome não tem tamanho)
        if ref in LITROS_UN_OVERRIDE:
            out[ref] = {"litros": float(LITROS_UN_OVERRIDE[ref]), "cat": g}
            continue
        val, uni = tamanho(nome)
        d = dens.get(g) or GRUPO_DENS[g]
        if uni == "L" and val:
            lun = val
        elif uni == "KG" and val:
            lun = val / d
        elif ref in peso_un and peso_un[ref] > 0:
            lun = peso_un[ref] / d
        else:
            lun = 0.0
        out[ref] = {"litros": lun, "cat": g}
    return out, dens


def series_mensais(info, master, mensal, meses, litros_map):
    vol = {"mix": [], "cremes": [], "gelatos": []}
    polpa = []
    for m in meses:
        acc = {"mix": 0.0, "cremes": 0.0, "gelatos": 0.0}
        pol = 0.0
        for ref, lm in litros_map.items():          # litros vendidos: TODOS os produtos
            if lm["cat"] in acc and lm["litros"]:
                acc[lm["cat"]] += mensal.get((ref, m), 0) * lm["litros"]
        for ref, d in info.items():                 # polpa consumida na produção
            if d["rec"]:
                kgpb = sum(i["q1b"] for i in d["rec"]["ings"])
                if kgpb > 0:
                    bat = (mensal.get((ref, m), 0) * d["kg"]) / kgpb
                    for ing in d["rec"]["ings"]:
                        if ing["temRef"] and "POLPA" in _norm(master.get(ing["ref"], {}).get("prod", "")):
                            pol += bat * ing["q1b"]
        for g in acc:
            vol[g].append(round(acc[g]))
        polpa.append(round(pol))
    return vol, polpa


def ler_producao(pl):
    """Lê a aba 'Produção' (produção realizada). Retorna DataFrame nome/mes/qtd."""
    try:
        raw = pl.worksheet("Produção").get_all_values()
    except Exception:
        return None
    hi = 0
    for i, r in enumerate(raw[:15]):
        cn = [_norm(str(c)) for c in r]
        if any("PRODUTO" in c for c in cn) and any("DATA" in c for c in cn):
            hi = i
            break
    hdr = raw[hi]
    inm = _col(hdr, "PRODUTO", "NOME")
    idt = _col(hdr, "DATA")
    iq = _col(hdr, "QTD", "QUANTIDAD", "QUANT")
    if None in (inm, idt, iq):
        print("!! Aba Produção: colunas não reconhecidas:", hdr)
        return None
    mx = max(inm, idt, iq)
    linhas = []
    for r in raw[hi + 1:]:
        r = (list(r) + [""] * (mx + 1 - len(r)))[:mx + 1]
        nome = str(r[inm]).strip()
        if not nome:
            continue
        linhas.append((nome, r[idt], r[iq]))
    df = pd.DataFrame(linhas, columns=["nome", "data", "qtd"])
    df["qtd"] = pd.to_numeric(df["qtd"], errors="coerce").fillna(0)
    df["data"] = pd.to_datetime(df["data"], format="%d/%m/%Y", errors="coerce")
    df = df.dropna(subset=["data"])
    df["mes"] = df["data"].dt.to_period("M").astype(str)
    return df


def ler_reprocesso(pl, master):
    """Lê a aba 'Reprocessos': B=produto, C=litragem, D=qtd, E=validade.
    Casa o nome com o cadastro (coluna A opcional) e classifica a validade."""
    aba = None
    for w in pl.worksheets():
        if "REPROCESS" in _norm(w.title):
            aba = w.title
            break
    if not aba:
        return []
    try:
        raw = pl.worksheet(aba).get_all_values()
    except Exception:
        return []
    # localizar cabeçalho
    hi = 0
    for i, r in enumerate(raw[:8]):
        if any("REPROCESS" in _norm(str(c)) or "PRODUTO" in _norm(str(c)) for c in r):
            hi = i
            break

    def apelido_ref(nome):
        n = _norm(nome)
        if n in REPROCESSO_APELIDOS:
            return REPROCESSO_APELIDOS[n]
        for ap, ref in REPROCESSO_APELIDOS.items():   # match parcial
            if ap in n or n in ap:
                return ref
        return None

    hoje = date.today()
    itens = []
    for r in raw[hi + 1:]:
        r = list(r) + [""] * (5 - len(r))
        refA = _rec_num(r[0])
        nome = str(r[1]).strip()
        if not nome:
            continue
        litr = _rec_num(str(r[2]).upper().replace("LT", "").replace("L", "")) or 0  # "5lt"→5
        qtd = _rec_num(r[3]) or 0
        vtxt = str(r[4]).strip()
        # validade
        dt, situacao, dias = None, "", None
        if _norm(vtxt) in ("TESTE", "TESTES", ""):
            situacao = "teste"
        else:
            for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(vtxt, fmt).date()
                    break
                except ValueError:
                    continue
            if dt:
                dias = (dt - hoje).days
                situacao = "vencido" if dias < 0 else ("vencendo" if dias <= 30 else "ok")
            else:
                situacao = "teste"
        # ref: coluna A, senão apelido, senão casa por nome no master
        ref = int(refA) if refA else apelido_ref(nome)
        if not ref:
            ref = casar_ref_nome(nome, master)
        prod_cad = master.get(ref, {}).get("prod") if ref else None
        cat = _cat_vol(prod_cad or nome)
        itens.append({
            "ref": ref, "nome": nome, "prodCad": prod_cad,
            "litragem": litr, "qtd": qtd, "litros": round(litr * qtd, 1),
            "validade": dt.strftime("%d/%m/%Y") if dt else vtxt,
            "situacao": situacao, "dias": dias,
            "cat": cat if cat in ("mix", "cremes", "gelatos") else "outros",
            "eatSistema": master.get(ref, {}).get("eat") if ref else None})
    # ordena: vencido → vencendo → ok → teste; dentro, por dias
    ordem = {"vencido": 0, "vencendo": 1, "ok": 2, "teste": 3}
    itens.sort(key=lambda x: (ordem.get(x["situacao"], 9),
                              x["dias"] if x["dias"] is not None else 9999))
    return itens


def casar_ref_nome(nome, master):
    """Casa um nome informal de reprocesso com uma ref de produto acabado."""
    pt = set(tokens(nome))
    if not pt:
        return None
    melhor, best = None, 0
    for ref, m in master.items():
        prod = m.get("prod", "")
        n = _norm(prod)
        if not any(k in n for k in ("MIX", "CREME", "GELATO", "MOUSSE")):
            continue
        if any(k in n for k in ("PLACA", "ECOBAG", "CAIXA", "RELOGIO", "CINTA",
                                "INSUMO", "DEGUSTA", "PICOLE", "CORANTE", "TOPPING", "POTE")):
            continue
        pc = set(tokens(prod))
        if not pc:
            continue
        inter = pt & pc
        score = len(inter) / len(pc)
        if score > best and len(inter) >= 2:
            best, melhor = score, ref
    return melhor if best >= 0.5 else None


def ler_consumo_real(pl):
    """Lê a aba 'Produção com insumos' e retorna CADA produção como um registro
    individual: {mes, produto, qtd, insumos:[{ref,nome,qtd,custoUn}]}.
    Estrutura em blocos: linha de produção (G=produto, I=data, T=qtd) seguida
    das linhas de insumo (B=ref, E=nome, Q=custo unit, S=qtd consumida)."""
    aba = None
    for w in pl.worksheets():
        n = _norm(w.title)
        if "PRODUCAO" in n and "INSUMO" in n:
            aba = w.title
            break
    if not aba:
        return None
    try:
        raw = pl.worksheet(aba).get_all_values()
    except Exception:
        return None

    def money(s):
        s = str(s).upper().replace("R$", "").replace(".", "").replace(",", ".").strip()
        try:
            return float(s)
        except ValueError:
            return 0.0

    prods = []
    atual = None
    for r in raw[7:]:
        r = list(r) + [""] * 25
        colG, colI, colT = str(r[6]).strip(), str(r[8]).strip(), str(r[19]).strip()
        colB, colE, colQ, colS = str(r[1]).strip(), str(r[4]).strip(), str(r[16]).strip(), str(r[18]).strip()
        # linha de produção? (data em I + produto em G)
        if "/" in colI and colG and _norm(colG) not in ("PRODUTO",):
            dt = None
            for fmt in ("%d/%m/%Y", "%d/%m/%y"):
                try:
                    dt = datetime.strptime(colI, fmt).date()
                    break
                except ValueError:
                    dt = None
            atual = {"mes": dt.strftime("%Y-%m") if dt else None,
                     "produto": colG, "qtd": _rec_num(colT) or 0, "insumos": []}
            prods.append(atual)
            continue
        # linha de insumo? (ref em B + nome em E)
        cb = _rec_num(colB)
        if cb and colE and _norm(colE) not in ("INSUMO",) and atual is not None:
            atual["insumos"].append({"ref": int(cb), "nome": colE,
                                     "qtd": _rec_num(colS) or 0, "custoUn": money(colQ)})
    return prods


def analise_real_vs_teorico(pl, master, rec_por_aba):
    """Análise MENSAL de desperdício: para cada mês, cruza consumo REAL de insumo
    (aba Produção com insumos) com o TEÓRICO (receita × produção realizada).
    Dois níveis: resumo por RECEITA (qual produto desperdiça) e detalhe por INSUMO.
    Divergência = real − teórico; >5% sinaliza perda ou receita desatualizada."""
    prods = ler_consumo_real(pl)
    if not prods:
        return None
    todas_recs = [r for recs in rec_por_aba.values() for r in recs]
    _rec_cache = {}                 # nome_produto → receita casada (casa 1x por produto)

    def teorico_da_producao(nome, qtd):
        """Insumo esperado (ref→kg) para produzir 'qtd' unidades de 'nome'."""
        if nome in _rec_cache:
            rec = _rec_cache[nome]
        else:
            rec = casar_receita(nome, todas_recs)
            _rec_cache[nome] = rec
        if not rec:
            return None, None
        kgpb = sum(i["q1b"] for i in rec["ings"])
        if kgpb <= 0:
            return None, rec["titulo"]
        val, uni = tamanho(nome)
        g = _cat_vol(nome)
        kgun = (val if uni == "KG" else (val * GRUPO_DENS.get(g, 0.8) if uni == "L" and val else 0))
        if not kgun:
            return None, rec["titulo"]
        bat = (qtd * kgun) / kgpb
        esp = {ing["ref"]: bat * ing["q1b"] for ing in rec["ings"] if ing["temRef"]}
        return esp, rec["titulo"]

    # agrega por mês → receita → insumo
    meses = {}     # mes → {receitas:{titulo:{real,teo,custo,prod,ins:{ref:{nome,real,teo,custo}}}}}
    for p in prods:
        mes = p["mes"]
        if not mes:
            continue
        esp, titulo = teorico_da_producao(p["produto"], p["qtd"])
        titulo = titulo or p["produto"]
        M = meses.setdefault(mes, {"receitas": {}, "custoTot": 0.0})
        R = M["receitas"].setdefault(titulo, {"real": 0.0, "teo": 0.0, "custo": 0.0,
                                              "prodUn": 0.0, "temRec": esp is not None,
                                              "ins": {}})
        R["prodUn"] += p["qtd"]
        for it in p["insumos"]:
            ref, kg, cu = it["ref"], it["qtd"], it["custoUn"]
            custo = kg * cu
            R["real"] += kg
            R["custo"] += custo
            M["custoTot"] += custo
            teo = (esp or {}).get(ref, 0)
            R["teo"] += teo
            d = R["ins"].setdefault(ref, {"nome": it["nome"], "real": 0.0, "teo": 0.0, "custo": 0.0})
            d["real"] += kg
            d["custo"] += custo
            d["teo"] += teo
        # insumos esperados que nem apareceram no real (faltou lançar)
        for ref, teo in (esp or {}).items():
            if ref not in R["ins"]:
                R["ins"][ref] = {"nome": master.get(ref, {}).get("prod", f"ref {ref}"),
                                 "real": 0.0, "teo": teo, "custo": 0.0}
                R["teo"] += teo

    # serializa para o payload
    out_meses = {}
    for mes, M in meses.items():
        receitas = []
        for titulo, R in M["receitas"].items():
            dif = R["real"] - R["teo"]
            pct = (dif / R["teo"] * 100) if R["teo"] > 0 else None
            insumos = []
            for ref, d in R["ins"].items():
                idif = d["real"] - d["teo"]
                ipct = (idif / d["teo"] * 100) if d["teo"] > 0 else None
                insumos.append({"ref": ref, "nome": d["nome"], "real": round(d["real"], 1),
                                "teo": round(d["teo"], 1), "dif": round(idif, 1),
                                "pct": round(ipct, 1) if ipct is not None else None,
                                "custo": round(d["custo"], 2), "temTeo": d["teo"] > 0})
            insumos.sort(key=lambda x: -abs(x["dif"]))
            receitas.append({"receita": titulo, "real": round(R["real"], 1),
                             "teo": round(R["teo"], 1), "dif": round(dif, 1),
                             "pct": round(pct, 1) if pct is not None else None,
                             "custo": round(R["custo"], 2), "prodUn": round(R["prodUn"]),
                             "temRec": R["temRec"], "insumos": insumos})
        receitas.sort(key=lambda x: -(abs(x["pct"]) if x["pct"] is not None else 0))
        out_meses[mes] = {"receitas": receitas, "custoTot": round(M["custoTot"], 2)}

    return {"meses": out_meses, "mesesLista": sorted(out_meses.keys())}


def comparar_prod_venda(dfp, info, master, mensal, dem_por_ref, fsz_por_ref, frac_mes,
                        mes_atual, litros_map=None):
    """Cruza produção realizada x vendas no mesmo período (jan/2026+)."""
    litros_map = litros_map or {}
    pmeses = sorted(dfp["mes"].unique())
    mi = {m: i for i, m in enumerate(pmeses)}
    refs_venda = {r for (r, _) in mensal.index}
    cand = [p for p in master.values() if p["ref"] in refs_venda or p["ref"] in set(PROD_REFS)]
    nome2ref = {}
    for p in master.values():
        nome2ref.setdefault(_norm(p["prod"]), p["ref"])

    def classif(nome):
        n = _norm(nome)
        g = "gelatos" if "GELATO" in n else ("cremes" if ("CREME" in n or "MOUSSE" in n or "MILKSHAKE" in n or "MILK SHAKE" in n) else "mix")
        return g

    def litros_un(nome, ref):
        """Litros por unidade — MESMA referência da venda (litros_map do produto
        casado). Só cai na fórmula pelo nome se o produto não casar com ref."""
        if ref and ref in litros_map and litros_map[ref]["litros"]:
            return litros_map[ref]["litros"]
        val, uni = tamanho(nome)
        if val is None:
            return None
        g = classif(nome)
        return val if uni == "L" else val / GRUPO_DENS[g]

    def achar_ref(nome):
        k = _norm(nome)
        if k in nome2ref:
            return nome2ref[k]
        pt = set(tokens(nome, True))
        best, bl = None, -1
        for p in cand:
            mt = set(tokens(p["prod"], True))
            if mt and mt <= pt and len(mt) > bl:
                bl, best = len(mt), p["ref"]
        return best

    prodUn = {g: [0.0] * len(pmeses) for g in ("mix", "cremes", "gelatos")}
    prodLit = {g: [0.0] * len(pmeses) for g in ("mix", "cremes", "gelatos")}
    porNome = {}
    for _, row in dfp.iterrows():
        g = classif(row["nome"])
        i = mi[row["mes"]]
        prodUn[g][i] += row["qtd"]
        e = porNome.setdefault(_norm(row["nome"]), {"nome": row["nome"], "cat": g, "mes": {}})
        e["mes"][row["mes"]] = e["mes"].get(row["mes"], 0) + row["qtd"]

    vendUn = {g: [0.0] * len(pmeses) for g in ("mix", "cremes", "gelatos")}
    vendLit = {g: [0.0] * len(pmeses) for g in ("mix", "cremes", "gelatos")}
    for ref, lm in litros_map.items():          # venda: TODOS os produtos (como a Demanda)
        g = lm["cat"]
        if g not in vendUn:
            continue
        for m, i in mi.items():
            q = mensal.get((ref, m), 0)
            if not q:
                continue
            vendUn[g][i] += q
            vendLit[g][i] += q * (lm["litros"] or 0)

    dias_prod = dfp.groupby("mes")["data"].apply(lambda s: s.dt.date.nunique())
    diasProdMes = [int(dias_prod.get(m, 0)) for m in pmeses]

    produtos, naomap, batidasMes = [], [], [0.0] * len(pmeses)
    ia = mi.get(mes_atual)
    for e in porNome.values():
        ref = achar_ref(e["nome"])
        prodMes = [round(e["mes"].get(m, 0)) for m in pmeses]
        vendMes = [round(mensal.get((ref, m), 0)) if ref else 0 for m in pmeses]
        # litros da produção: MESMA referência da venda (litros do produto casado)
        lit = litros_un(e["nome"], ref)
        if lit:
            for m, q in e["mes"].items():
                prodLit[e["cat"]][mi[m]] += q * lit
        if ref is None:
            naomap.append({"nome": e["nome"], "qtd": sum(prodMes)})
        if ref in info and info[ref].get("kgpb"):
            kg, kgpb = info[ref]["kg"], info[ref]["kgpb"]
            for i in range(len(pmeses)):
                batidasMes[i] += prodMes[i] * kg / kgpb
        produtos.append({"nome": e["nome"], "ref": ref if ref else "—", "cat": e["cat"],
                         "prodMes": prodMes, "vendMes": vendMes,
                         "dem": round(dem_por_ref.get(ref, 0)) if ref else 0,
                         "prodAtual": prodMes[ia] if ia is not None else 0})
    produtos.sort(key=lambda x: -sum(x["prodMes"]))

    litrosEstCat = {g: 0.0 for g in ("mix", "cremes", "gelatos")}
    for ref, lm in litros_map.items():
        if lm["cat"] in litrosEstCat and lm["litros"]:
            litrosEstCat[lm["cat"]] += master.get(ref, {}).get("eat", 0) * lm["litros"]
    litrosEstoque = sum(litrosEstCat.values())

    completos = [m for m in pmeses if m != mes_atual]
    base = completos if completos else pmeses
    vend_base = sum(vendLit[g][mi[m]] for g in vendLit for m in base)
    dias_uteis_medio = sum(dias_uteis(int(m[:4]), int(m[5:7])) for m in base) / max(1, len(base))
    demandaDia = (vend_base / (len(base) * dias_uteis_medio)) if base and dias_uteis_medio else 0
    diasEstoque = (litrosEstoque / demandaDia) if demandaDia > 0 else 0
    saldo = sum(sum(prodLit[g]) for g in prodLit) - sum(sum(vendLit[g]) for g in vendLit)
    saldoDias = (saldo / demandaDia) if demandaDia > 0 else 0

    sazonais = []
    for ref, d in info.items():
        f = fsz_por_ref.get(ref, 1.0)
        if abs(f - 1) >= 0.25:
            sazonais.append({"nome": d["nome"], "ref": ref, "fsz": f, "cat": d["grupo"]})
    sazonais.sort(key=lambda x: -abs(x["fsz"] - 1))

    rnd = lambda dd: {g: [round(v) for v in dd[g]] for g in dd}
    return {"meses": pmeses, "mesAtual": mes_atual, "fracMes": round(frac_mes, 3),
            "prodUn": rnd(prodUn), "prodLit": rnd(prodLit),
            "vendUn": rnd(vendUn), "vendLit": rnd(vendLit),
            "produtos": produtos, "naoMap": naomap[:40],
            "batidasMes": [round(b, 1) for b in batidasMes], "diasProdMes": diasProdMes,
            "litrosEstoque": round(litrosEstoque),
            "litrosEstCat": {g: round(v) for g, v in litrosEstCat.items()},
            "demandaDia": round(demandaDia), "diasEstoque": round(diasEstoque, 1),
            "saldoDias": round(saldoDias, 1), "cap": CAP_DIA_BATIDAS,
            "sazonais": sazonais[:24]}


# ── HTML ──────────────────────────────────────────────────────────
def montar(estoque, plano, insumos, cat, info, master, mensal, meses_all,
           amanha, cap_usada, diario, pv=None, pesoval=None, litros_map=None, peso_un=None,
           reproc=None, custos=None):
    from collections import Counter
    hoje = date.today().strftime("%d/%m/%Y")
    meses12 = meses_all[-12:]
    litros_map = litros_map or {}
    peso_un = peso_un or {}
    vol, polpa = series_mensais(info, master, mensal, meses12, litros_map)
    dia_labels, dia = series_diarias(litros_map, diario)
    hoje_dt = date.today()

    # Cards de litros: média mensal do ANO corrente (apenas meses completos).
    ano_now = hoje_dt.strftime("%Y")
    mes_atual_ym = hoje_dt.strftime("%Y-%m")
    meses_ano = [m for m in meses12 if m.startswith(ano_now) and m != mes_atual_ym]

    def _avg_ano(a):
        if not meses_ano:
            return round(sum(a[-3:]) / min(3, len(a))) if a else 0
        idxs = [meses12.index(m) for m in meses_ano]
        return round(sum(a[i] for i in idxs) / len(idxs))
    lac = _avg_ano(vol["mix"])
    lcr = _avg_ano(vol["cremes"])
    lge = _avg_ano(vol["gelatos"])
    litros_tot_mes = lac + lcr + lge
    # volume (unidades e kg) por mês — média dos últimos 3 meses
    un_mes = kg_mes = 0.0
    base3 = meses12[-3:]
    if base3 and peso_un:
        refs = {r for (r, _) in mensal.index}
        for m in base3:
            for ref in refs:
                q = mensal.get((ref, m), 0)
                un_mes += q
                kg_mes += q * peso_un.get(ref, 0)
        un_mes = round(un_mes / len(base3))
        kg_mes = round(kg_mes / len(base3))

    valor_op = sum(x["valor"] for x in estoque if not x.get("imob"))
    valor_imob = sum(x["valor"] for x in estoque if x.get("imob"))
    parado_itens = [x for x in estoque if not x.get("imob") and x["eat"] > 0
                    and x["status"] in ("Sem giro", "Sem movimento")]
    parado = sum(x["valor"] for x in parado_itens)
    parado_qtd = round(sum(x["eat"] for x in parado_itens))
    polpa_itens = [x for x in estoque if x["polpa"]]
    polpa_valor = sum(x["valor"] for x in polpa_itens)
    polpa_qtd = round(sum(x["eat"] for x in polpa_itens))
    polpa_vend_mes = round(sum(x["dem"] for x in polpa_itens))
    polpa_dias = round(polpa_qtd / (polpa_vend_mes / 30)) if polpa_vend_mes > 0 else None
    n_comprar = sum(1 for x in estoque if x["acao"] == "Comprar" and x["status"] in ("Ruptura", "Repor"))
    n_prod = sum(1 for x in estoque if x["acao"] == "Produzir" and x["status"] in ("Ruptura", "Repor"))
    n_rup = sum(1 for x in estoque if x["status"] == "Ruptura")
    batidas = round(sum(p["batidas"] for p in plano))
    polpa_total = round(sum(x["kg"] for x in insumos if x["polpa"]))

    kpis = [("Valor em estoque", "R$ " + nf(valor_op, 2), ""),
            ("Estoque parado", "R$ " + nf(parado, 2), "al" if parado else ""),
            ("Volume parado (un)", nf(parado_qtd), ""),
            ("Valor polpas", "R$ " + nf(polpa_valor, 2), ""),
            ("Polpas vendidas / mês", nf(polpa_vend_mes), ""),
            ("Cobertura polpas (dias)", (nf(polpa_dias) if polpa_dias is not None else "—"),
             "al" if (polpa_dias is not None and polpa_dias < 60) else ""),
            ("Comprar (itens)", nf(n_comprar), "al" if n_comprar else ""),
            ("Ruptura", nf(n_rup), "al" if n_rup else ""),
            ("Batidas / mês", nf(batidas), ""),
            ("Imobilizado", "R$ " + nf(valor_imob, 2), "")]
    kpi_html = "".join(f'<div class="kpi {c}"><div class="l">{esc(l)}</div>'
                       f'<div class="v">{v}</div></div>' for l, v, c in kpis)

    abc_val = {c: round(sum(x["valor"] for x in estoque if x["abc"] == c)) for c in "ABC"}
    abc_cnt = {c: sum(1 for x in estoque if x["abc"] == c) for c in "ABC"}
    stt = dict(Counter(x["status"] for x in estoque))
    acao_nec = dict(Counter(x["acao"] for x in estoque if x["status"] in ("Ruptura", "Repor")))
    catg = {"mix": "Açaí", "cremes": "Cremes", "gelatos": "Gelato"}
    valc = Counter()
    for x in estoque:
        valc[catg.get(x["grupo"], "Outros")] += x["valor"]

    ins_svg = svg_grouped([(x["insumo"].replace("INSUMO ", "")[:26], x["kg"], x["est"] or 0)
                           for x in insumos if not x["polpa"]][:15])
    polpa_grp = svg_grouped([(x["insumo"].replace("INSUMO ", "")[:26], x["kg"], x["est"] or 0)
                             for x in insumos if x["polpa"]])
    al = sorted([x for x in insumos if x["falta"]], key=lambda x: (x["cob"] or 9))[:12]
    al_html = "".join(
        f'<div class="acard"><div class="nm">{"🫐 " if x["polpa"] else ""}{esc(x["insumo"])}</div>'
        f'<div class="dt">falta {nf((x["kg"] or 0)-(x["est"] or 0))} kg · cobre {nf(x["cob"],1)} mês</div></div>'
        for x in al) or '<div class="muted">Nada em falta 🎉</div>'
    volinfo = (f"Volume médio/mês: {nf(un_mes)} un · {nf(kg_mes)} kg · {nf(litros_tot_mes)} L"
               if peso_un else "")

    # META: 55 mil L/mês total, dividida na proporção real da PRODUÇÃO de cada categoria.
    # Referência: litros PRODUZIDOS (aba Produção), não vendidos — pois a meta é de produção.
    du_total = dias_uteis(hoje_dt.year, hoje_dt.month)
    du_rest = dias_uteis(hoje_dt.year, hoje_dt.month, apartir=hoje_dt)
    du_pass = max(0, du_total - du_rest)
    mes_now = hoje_dt.strftime("%Y-%m")
    # proporção histórica da PRODUÇÃO por categoria (do pv, se existir) — senão cai na venda
    if pv and pv.get("prodLit"):
        hist = {g: sum(pv["prodLit"].get(g, [])) for g in ("mix", "cremes", "gelatos")}
        prod_mes = {g: (pv["prodLit"][g][pv["meses"].index(mes_now)]
                        if mes_now in pv["meses"] else 0)
                    for g in ("mix", "cremes", "gelatos")}
    else:
        hist = {g: sum(vol[g]) for g in vol}
        prod_mes = {g: (vol[g][-1] if meses12 and meses12[-1] == mes_now else 0) for g in vol}
    hist_soma = sum(hist.values()) or 1
    prop = {g: hist[g] / hist_soma for g in hist}
    meta_cat = {g: round(META_LITROS_MES * prop[g]) for g in hist}
    prod_mes_tot = sum(prod_mes.values())
    meta_info = {"total": META_LITROS_MES, "porCat": meta_cat, "propCat": {g: round(p, 3) for g, p in prop.items()},
                 "duTotal": du_total, "duRest": du_rest, "duPass": du_pass,
                 "prodMes": prod_mes, "prodMesTot": prod_mes_tot,
                 "faltaTot": max(0, META_LITROS_MES - prod_mes_tot),
                 "faltaCat": {g: max(0, meta_cat[g] - prod_mes[g]) for g in hist},
                 "porDia": round(META_LITROS_MES / du_total) if du_total else 0,
                 "porDiaRest": round(max(0, META_LITROS_MES - prod_mes_tot) / du_rest) if du_rest else 0,
                 "fonte": "produção" if (pv and pv.get("prodLit")) else "venda (fallback)"}

    payload = {
        "estoque": estoque, "insumos": insumos, "plano": plano, "amanha": amanha,
        "meses": meses12, "vol": vol, "polpa": polpa,
        "diaLabels": dia_labels, "dia": dia, "cat": cat, "meta": meta_info,
        "resumo": {"abcVal": abc_val, "abcCnt": abc_cnt, "status": stt,
                   "acao": acao_nec, "valCat": dict(valc)},
        "cap": CAP_DIA_BATIDAS, "capUsada": cap_usada, "pv": pv, "pesoVal": pesoval or [],
        "reproc": reproc or [], "custos": custos}
    dump = json.dumps(payload, ensure_ascii=False,
                      default=lambda o: o.item() if hasattr(o, "item") else str(o))

    return (_PAGE.replace("__DATA_JSON__", dump).replace("__DATA__", hoje)
            .replace("__KPIS__", kpi_html).replace("__ALERTAS__", al_html)
            .replace("__INSGRAF__", ins_svg).replace("__POLPAGRAF__", polpa_grp)
            .replace("__VOLINFO__", esc(volinfo))
            .replace("__LTOT__", nf(litros_tot_mes)).replace("__LACAI__", nf(lac))
            .replace("__LCREMES__", nf(lcr)).replace("__LGELATO__", nf(lge))
            .replace("__NPROD__", str(len(estoque))).replace("__NPLANO__", str(len(plano)))
            .replace("__CAPUSADA__", nf(cap_usada)).replace("__CAP__", str(CAP_DIA_BATIDAS)))


_PAGE = r"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Supremo Açaí — Painel</title>
<style>
:root{--bg:#ECE9F2;--card:#fff;--ink:#241633;--mut:#7A7088;--line:#E7E2EE;--grid:#EDE8F4;
--deep:#2C1640;--acai:#5B2A86;--berry:#9B3FD1;--lil:#C88DE8;--teal:#1FA3A0;--amber:#E0912F;
--ok:#2E9E6B;--rup:#D9434E;--repor:#E0912F;--sg:#8A5A9E;--sm:#9A93A6;--ina:#BEB6C8;
--sh:0 1px 2px rgba(20,10,40,.05),0 3px 12px rgba(20,10,40,.06)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:'Segoe UI',system-ui,-apple-system,sans-serif;font-size:14px}
.wrap{max-width:1280px;margin:0 auto;padding:0 22px}
.topbar{background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20;box-shadow:0 1px 6px rgba(20,10,40,.04)}
.topbar .wrap{display:flex;align-items:center;justify-content:space-between;padding:11px 22px}
.brand{display:flex;align-items:center;gap:11px;font-weight:800;font-size:16px;color:var(--deep)}
.logo{width:32px;height:32px;border-radius:9px;background:linear-gradient(135deg,var(--deep),var(--berry));display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:15px;box-shadow:var(--sh)}
.upd{font-size:12px;color:var(--mut)}
.pages{background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:55px;z-index:19}
.pages .wrap{display:flex;gap:5px;flex-wrap:wrap;padding:8px 22px}
.pages button{background:none;border:0;color:var(--mut);padding:8px 15px;font-size:13.5px;font-weight:600;cursor:pointer;border-radius:9px;font-family:inherit;transition:.12s}
.pages button:hover{background:#F3EFF8;color:var(--acai)}
.pages button.on{background:var(--acai);color:#fff}
.tab{display:none;padding:22px 0 64px}.tab.on{display:block}
.ttl{font-size:19px;font-weight:800;margin:0 0 2px;color:var(--deep)}
.hint{color:var(--mut);font-size:12.5px;margin:0 0 18px}
.cockpit{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:2px 0 20px}
.act{background:var(--card);border-radius:14px;box-shadow:var(--sh);padding:15px 17px;border-top:4px solid var(--berry);cursor:pointer;transition:.12s}
.act:hover{transform:translateY(-2px)}
.act.rup{border-top-color:var(--rup)}.act.prod{border-top-color:var(--acai)}.act.buy{border-top-color:#2A6FB0}.act.pol{border-top-color:var(--polpa)}
.act .h{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut);font-weight:700}
.act .big{font-size:25px;font-weight:800;margin:4px 0 8px;color:var(--deep)}
.act.rup .big{color:var(--rup)}.act.pol.al .big{color:var(--rup)}
.act ul{margin:0;padding:0;list-style:none;font-size:12px;color:var(--ink)}
.act li{padding:3px 0;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:8px}
.act li span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.act li b{flex:none}
.act .go{margin-top:9px;font-size:11.5px;color:var(--acai);font-weight:700}
.btnp{background:var(--acai);color:#fff;border:0;border-radius:8px;padding:7px 14px;font-size:12.5px;font-weight:600;cursor:pointer;font-family:inherit}
.btnp:hover{background:var(--berry)}
@media print{.topbar,.pages,.slic,.btnp,.go,.leg,.noprint{display:none!important}.tab{display:none!important}.tab.on{display:block!important}
body{background:#fff}.tile,.kpi,.act{box-shadow:none;border:1px solid #ddd}.wrap{max-width:100%}.tablewrap{max-height:none!important}}
body.print-only *{visibility:hidden!important}
body.print-only .to-print,body.print-only .to-print *{visibility:visible!important}
body.print-only .to-print{position:absolute!important;left:0!important;top:0!important;width:100%!important;padding:20px!important;border-left:none!important;background:#fff!important;box-shadow:none!important;border:1px solid #ddd!important}
body.print-only .to-print .noprint{display:none!important}
body.print-only .to-print .tablewrap{max-height:none!important;overflow:visible!important}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:18px}
.kpi{background:var(--card);border-radius:12px;box-shadow:var(--sh);padding:13px 15px;position:relative;overflow:hidden}
.kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--berry)}
.kpi.al::before{background:var(--rup)}
.kpi .l{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut);font-weight:700}
.kpi .v{font-weight:800;font-size:19px;margin-top:6px;color:var(--deep)}.kpi.al .v{color:var(--rup)}
.tile{background:var(--card);border-radius:14px;box-shadow:var(--sh);padding:16px 18px;margin-bottom:16px}
.tile .th{font-size:11.5px;font-weight:800;letter-spacing:.5px;text-transform:uppercase;color:var(--mut);margin-bottom:12px}
.tile .th small{font-weight:600;text-transform:none;letter-spacing:0;color:var(--mut)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}
.leg{display:flex;gap:15px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin:0 0 8px}
.leg i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.muted{color:var(--mut)}
.donutwrap{display:flex;align-items:center;gap:16px;flex-wrap:wrap;justify-content:center}
.slice{transition:.1s;cursor:default}.slice:hover{opacity:.82}
.dleg{display:flex;flex-direction:column;gap:5px;font-size:12.5px;min-width:120px}
.dleg span{display:flex;align-items:center;padding:2px 0}.dleg i{width:10px;height:10px;border-radius:3px;margin-right:8px}.dleg b{margin-left:auto}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.dcard{background:var(--card);border-radius:12px;box-shadow:var(--sh);padding:14px 16px;cursor:pointer;transition:.12s;border:2px solid transparent}
.dcard:hover{transform:translateY(-1px)}.dcard.on{border-color:var(--acai)}
.dcard .l{font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--mut);font-weight:700}
.dcard .v{font-weight:800;font-size:21px;margin-top:5px;color:var(--acai)}.dcard .u{font-size:12px;color:var(--mut);font-weight:400}
.slic{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.slic .lbl{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:var(--mut);margin-right:2px}
.chip{padding:7px 15px;border:1px solid var(--line);border-radius:20px;background:#fff;cursor:pointer;font-size:12.5px;font-weight:600;color:var(--mut);transition:.12s}
.chip:hover{border-color:var(--berry)}.chip.on{background:var(--acai);color:#fff;border-color:var(--acai)}
.chip.A.on{background:var(--deep)}.chip.B.on{background:var(--berry)}.chip.C.on{background:var(--lil)}
input.busca,select.busca{padding:8px 12px;border:1px solid var(--line);border-radius:9px;font-size:13px;font-family:inherit;background:#fff}
input.busca{flex:1;min-width:150px}
.tablewrap{overflow:auto;max-height:560px;border:1px solid var(--line);border-radius:10px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{position:sticky;top:0;background:#F5F1F9;color:var(--deep);text-align:left;padding:10px 12px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.3px;white-space:nowrap;cursor:pointer;user-select:none}
th:hover{background:#EEE7F5}
td{padding:9px 12px;border-bottom:1px solid #F1ECF6;white-space:nowrap}tbody tr:hover td{background:#FAF7FD}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.cls{display:inline-block;width:20px;text-align:center;border-radius:5px;font-weight:800;font-size:11px}
.cls.A{background:var(--deep);color:#fff}.cls.B{background:#D6BEE9;color:var(--deep)}.cls.C{background:#EFE7F5;color:var(--mut)}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:10.5px;font-weight:700;color:#fff}
.b-Repor{background:var(--repor)}.b-Ruptura{background:var(--rup)}.b-OK{background:var(--ok)}
.b-SemGiro{background:var(--sg)}.b-SemMovimento{background:var(--sm)}.b-Inativo{background:var(--ina)}
.b-Comprar{background:#2A6FB0}.b-Produzir{background:#7A4FB0}.b-Polpa{background:#7B2FC9}
.b-Amanhã{background:var(--rup)}.b-Semana{background:var(--repor)}.b-Monitorar{background:#8A9A5A}
.alertas{display:flex;flex-wrap:wrap;gap:9px}
.acard{background:#FCEEF0;border:1px solid #F1C9CE;border-radius:10px;padding:9px 13px;min-width:190px}
.acard .nm{font-weight:700;font-size:12.5px}.acard .dt{font-size:11.5px;color:var(--rup);margin-top:3px}
.cont{color:var(--mut);font-size:12px;margin-top:10px}
.cbar{display:flex;align-items:center;gap:10px;padding:4px 0;cursor:pointer}.cbar:hover .cbl{color:var(--acai)}
.cbl{width:210px;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cbt{flex:1;background:#F1ECF6;border-radius:5px;height:15px;overflow:hidden}.cbt i{display:block;height:100%;background:linear-gradient(90deg,var(--acai),var(--berry))}
.cbv{width:74px;text-align:right;font-size:12px;font-variant-numeric:tabular-nums}
.capbar{background:#EEE6F2;border-radius:8px;height:15px;overflow:hidden}.capbar>i{display:block;height:100%;background:linear-gradient(90deg,var(--acai),var(--berry));transition:width .3s}
.chartbox{position:relative}
.chartbox svg{display:block;width:100%;height:242px}
.tip{position:absolute;pointer-events:none;background:#fff;border:1px solid var(--line);box-shadow:var(--sh);border-radius:9px;padding:8px 11px;font-size:11.5px;opacity:0;transition:opacity .06s;z-index:6;top:4px;min-width:120px}
.tip .tt{font-weight:800;margin-bottom:4px;color:var(--deep)}
.tip div{display:flex;align-items:center;gap:6px;padding:1px 0}.tip i{width:9px;height:9px;border-radius:2px}.tip b{margin-left:auto}
@media(max-width:920px){.kpis{grid-template-columns:repeat(2,1fr)}.grid2,.grid4,.cards{grid-template-columns:1fr 1fr}}
</style></head><body>
<div class="topbar"><div class="wrap"><div class="brand"><span class="logo">S</span>Supremo Açaí · Painel</div>
<div class="upd">Atualizado em __DATA__ · __NPROD__ itens · __NPLANO__ em produção</div></div></div>
<div class="pages"><div class="wrap">
<button class="on" data-t="geral" onclick="showTab('geral')">Visão Geral</button>
<button data-t="compras" onclick="showTab('compras')">Compras</button>
<button data-t="estoque" onclick="showTab('estoque')">Estoque</button>
<button data-t="producao" onclick="showTab('producao')">Produção</button>
<button data-t="prodvenda" onclick="showTab('prodvenda')">Produção / Venda</button>
<button data-t="custos" onclick="showTab('custos')">Custos</button>
<button data-t="demanda" onclick="showTab('demanda')">Demanda</button>
</div></div>
<div class="wrap">

<section id="geral" class="tab on"><h2 class="ttl">Visão Geral</h2><p class="hint">O que fazer hoje — clique num cartão para ir direto à ação. Abaixo, os indicadores e gráficos (clique nas fatias para filtrar).</p>
<div class="cockpit" id="cockpit"></div>
<div class="kpis">__KPIS__</div>
<div class="grid4">
<div class="tile"><div class="th">Valor por curva ABC <small>· faturamento 12m</small></div><div id="pizAbc"></div></div>
<div class="tile"><div class="th">Situação dos itens</div><div id="pizStatus"></div></div>
<div class="tile"><div class="th">Precisa de ação</div><div id="pizAcao"></div></div>
<div class="tile"><div class="th">Valor por categoria</div><div id="pizCat"></div></div>
</div></section>

<section id="compras" class="tab"><h2 class="ttl">O que comprar</h2><p class="hint">Itens de <b>compra</b> que precisam de reposição. Produção e polpas ficam em suas próprias páginas.</p>
<div class="slic" id="chips-cmp"><span class="lbl">Curva</span>
<span class="chip A on" data-v="" onclick="setABC('cmp','',this)">Todas</span>
<span class="chip A" data-v="A" onclick="setABC('cmp','A',this)">A</span>
<span class="chip B" data-v="B" onclick="setABC('cmp','B',this)">B</span>
<span class="chip C" data-v="C" onclick="setABC('cmp','C',this)">C</span></div>
<div class="slic" id="chips-cmpo"><span class="lbl">Base</span>
<span class="chip on" data-v="" onclick="setOrig('',this)">Todas</span>
<span class="chip" data-v="produção" onclick="setOrig('produção',this)">Produção</span>
<span class="chip" data-v="venda" onclick="setOrig('venda',this)">Venda</span>
<select class="busca" id="sCmp" onchange="rCmp()"><option value="">Todo status</option>
<option>Ruptura</option><option>Repor</option></select>
<input class="busca" id="qCmp" onkeyup="rCmp()" placeholder="Buscar item ou referência…"></div>
<div class="tile"><div class="th">Lista de compras</div>
<div class="tablewrap"><table><thead><tr>
<th onclick="sortT('cmp','ref')">Ref</th><th onclick="sortT('cmp','produto')">Item</th>
<th onclick="sortT('cmp','abc')">ABC</th><th onclick="sortT('cmp','origem')">Base</th>
<th onclick="sortT('cmp','eat')">Estoque</th><th onclick="sortT('cmp','precisa')">Precisa</th>
<th onclick="sortT('cmp','comprar')">Comprar</th><th onclick="sortT('cmp','cob')">Cob.(mês)</th>
<th onclick="sortT('cmp','status')">Status</th></tr></thead><tbody id="tCmp"></tbody></table></div>
<div class="cont" id="cCmp"></div></div>
<div class="tile"><div class="th">⚠ O que vai faltar</div><div class="alertas">__ALERTAS__</div></div>
<div class="grid2"><div class="tile"><div class="th">Top itens a comprar <small>· clique para focar</small></div><div id="barCmp"></div></div>
<div class="tile"><div class="th">Necessidade de insumos vs estoque</div>__INSGRAF__</div></div></section>

<section id="estoque" class="tab"><h2 class="ttl">Estoque</h2><p class="hint">Visão geral e detalhes por sub-área.</p>
<div class="slic" id="subnav-est" style="border-bottom:2px solid var(--line);padding-bottom:0;margin-bottom:14px">
<span class="chip on" data-s="visao" onclick="showSub('visao',this)">📊 Visão Geral</span>
<span class="chip" data-s="camara" onclick="showSub('camara',this)">❄️ Câmara Fria</span>
<span class="chip" data-s="polpas" onclick="showSub('polpas',this)">🫐 Polpas</span>
<span class="chip" data-s="seco" onclick="showSub('seco',this)">📦 Estoque Seco</span>
<span class="chip" data-s="reproc" onclick="showSub('reproc',this)">♻️ Reprocesso</span>
<span class="chip" data-s="todos" onclick="showSub('todos',this)">Ver todos</span>
</div>

<div class="sub" id="sub-visao">
<div class="kpis" id="visKpis" style="grid-template-columns:repeat(5,1fr)"></div>
<div class="grid2">
<div class="tile"><div class="th">Valor por sub-área</div><div id="pizSubEst"></div></div>
<div class="tile"><div class="th">🔥 Top 10 abaixo do mínimo <small>· qualquer sub-área</small></div>
<div class="tablewrap" style="max-height:340px"><table><thead><tr>
<th>Ref</th><th>Item</th><th>Área</th><th>Estoque</th><th>Mín.</th><th>Falta</th></tr></thead>
<tbody id="tRupTop"></tbody></table></div></div>
</div></div>

<div class="sub" id="sub-camara" style="display:none">
<div class="kpis" id="camKpis" style="grid-template-columns:repeat(4,1fr)"></div>
<div class="tile printarea" id="confCamTile" style="border-left:5px solid #4ba4ff;background:linear-gradient(90deg,#EBF4FE,var(--card) 30%)">
<div class="th" style="display:flex;justify-content:space-between;align-items:center">❄️ Conferência física — Câmara Fria <small style="font-weight:600;color:var(--mut)"><b id="camDia">—</b></small></div>
<p class="hint" style="margin-top:0">Preencha a contagem física dos produtos acabados na câmara. Salva localmente.</p>
<div class="tablewrap" style="max-height:400px"><table><thead><tr>
<th>Ref</th><th>Produto</th><th>Cat.</th><th>Sistema</th><th style="background:#DBEBFB">Físico</th><th>Dif</th><th>%</th></tr></thead>
<tbody id="tConfCam"></tbody></table></div>
<div class="noprint" style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
<button class="btnp" onclick="salvarCf('cam')">💾 Salvar</button>
<button class="btnp" style="background:#2A6FB0" onclick="imprimirSub('cam')">🖨 Imprimir</button>
<button class="btnp" style="background:#25D366" onclick="whatsCf('cam')">📱 WhatsApp</button>
<button class="btnp" style="background:#B0A6BE" onclick="limparCf('cam')">Limpar</button>
<span id="camInfo" class="muted"></span></div></div>
<div class="tile"><div class="th">Todos os itens da câmara fria</div>
<div class="tablewrap" style="max-height:420px"><table><thead><tr>
<th>Ref</th><th>Produto</th><th>Estoque</th><th>Mín.</th><th>Falta</th><th>Cob.</th><th>Valor</th><th>Status</th></tr></thead>
<tbody id="tCam"></tbody></table></div></div></div>

<div class="sub" id="sub-polpas" style="display:none">
<div class="kpis" id="polpaKpis" style="grid-template-columns:repeat(4,1fr)"></div>
<div class="grid2"><div class="tile"><div class="th">Polpa consumida por mês</div><div id="polpaLine"></div></div>
<div class="tile"><div class="th">Necessidade vs estoque</div>__POLPAGRAF__</div></div>
<div class="tile printarea" id="confAquiTile" style="border-left:5px solid var(--polpa);background:linear-gradient(90deg,#F9F3FE,var(--card) 30%)">
<div class="th" style="display:flex;justify-content:space-between;align-items:center">📋 Conferência física — Polpas (aqui) <small style="font-weight:600;color:var(--mut)"><b id="confDia">—</b></small></div>
<p class="hint" style="margin-top:0">Polpas guardadas na fábrica. Salva localmente.</p>
<div class="tablewrap" style="max-height:340px"><table><thead><tr>
<th>Ref</th><th>Polpa</th><th>Sistema</th><th style="background:#F0E5F8">Físico</th><th>Dif</th><th>%</th></tr></thead>
<tbody id="tConf"></tbody></table></div>
<div class="noprint" style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
<button class="btnp" onclick="salvarConf()">💾 Salvar</button>
<button class="btnp" style="background:#2A6FB0" onclick="imprimirSub('aqui')">🖨 Imprimir</button>
<button class="btnp" style="background:#25D366" onclick="whatsConf()">📱 WhatsApp</button>
<button class="btnp" style="background:#B0A6BE" onclick="limparConf()">Limpar</button>
<span id="confInfo" class="muted"></span></div></div>
<div class="tile printarea" id="confLfTile" style="border-left:5px solid #0e7f9e;background:linear-gradient(90deg,#E6F5F9,var(--card) 30%)">
<div class="th" style="display:flex;justify-content:space-between;align-items:center">🧊 Conferência física — Polpas Lockfrio (terceiros) <small style="font-weight:600;color:var(--mut)"><b id="lfDia">—</b></small></div>
<p class="hint" style="margin-top:0">Polpas armazenadas no Lockfrio. Contagem independente.</p>
<div class="tablewrap" style="max-height:280px"><table><thead><tr>
<th>Ref</th><th>Polpa</th><th>Sistema</th><th style="background:#D6EEF6">Físico</th><th>Dif</th><th>%</th></tr></thead>
<tbody id="tConfLf"></tbody></table></div>
<div class="noprint" style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
<button class="btnp" onclick="salvarCf('lf')">💾 Salvar</button>
<button class="btnp" style="background:#2A6FB0" onclick="imprimirSub('lf')">🖨 Imprimir</button>
<button class="btnp" style="background:#25D366" onclick="whatsCf('lf')">📱 WhatsApp</button>
<button class="btnp" style="background:#B0A6BE" onclick="limparCf('lf')">Limpar</button>
<span id="lfInfo" class="muted"></span></div></div>
<div class="tile"><div class="th">Todas as polpas — estoque, venda e reposição</div>
<div class="tablewrap" style="max-height:420px"><table><thead><tr>
<th>Ref</th><th>Polpa</th><th>Local</th><th>Tipo</th><th>Estoque</th><th>Valor</th><th>Vendido/mês</th>
<th>Consumo/mês</th><th>Comprar</th><th>Cob.(mês)</th><th>Status</th></tr></thead>
<tbody id="tPolpa"></tbody></table></div><div class="cont" id="cPolpa"></div></div></div>

<div class="sub" id="sub-seco" style="display:none">
<div class="kpis" id="secoKpis" style="grid-template-columns:repeat(4,1fr)"></div>
<div class="tile printarea" id="confSecoTile" style="border-left:5px solid #b8952a;background:linear-gradient(90deg,#FDF6E6,var(--card) 30%)">
<div class="th" style="display:flex;justify-content:space-between;align-items:center">📦 Conferência física — Estoque Seco <small style="font-weight:600;color:var(--mut)"><b id="secoDia">—</b></small></div>
<p class="hint" style="margin-top:0">Insumos, embalagens (caixa/tampa) e revenda não-inventário. Salva localmente.</p>
<div class="slic"><input class="busca noprint" id="qConfSeco" onkeyup="rConfSeco()" placeholder="Filtrar item…"></div>
<div class="tablewrap" style="max-height:400px"><table><thead><tr>
<th>Ref</th><th>Item</th><th>Sistema</th><th style="background:#FBF0D3">Físico</th><th>Dif</th><th>%</th></tr></thead>
<tbody id="tConfSeco"></tbody></table></div>
<div class="noprint" style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
<button class="btnp" onclick="salvarCf('seco')">💾 Salvar</button>
<button class="btnp" style="background:#2A6FB0" onclick="imprimirSub('seco')">🖨 Imprimir</button>
<button class="btnp" style="background:#25D366" onclick="whatsCf('seco')">📱 WhatsApp</button>
<button class="btnp" style="background:#B0A6BE" onclick="limparCf('seco')">Limpar</button>
<span id="secoInfo" class="muted"></span></div></div>
<div class="tile"><div class="th">Todos os itens do estoque seco</div>
<div class="slic"><input class="busca" id="qSeco" onkeyup="rSeco()" placeholder="Buscar…"></div>
<div class="tablewrap" style="max-height:440px"><table><thead><tr>
<th>Ref</th><th>Produto</th><th>Grupo ERP</th><th>Estoque</th><th>Consumo</th><th>Valor</th><th>Cob.</th><th>Status</th></tr></thead>
<tbody id="tSeco"></tbody></table></div><div class="cont" id="cSeco"></div></div></div>

<div class="sub" id="sub-reproc" style="display:none">
<div class="kpis" id="repKpis" style="grid-template-columns:repeat(4,1fr)"></div>
<div class="tile printarea" id="confRepTile" style="border-left:5px solid #8ba05a;background:linear-gradient(90deg,#F1F5E8,var(--card) 30%)">
<div class="th" style="display:flex;justify-content:space-between;align-items:center">♻️ Reprocessos <small style="font-weight:600;color:var(--mut)"><b id="repDia">—</b></small></div>
<p class="hint" style="margin-top:0">Produtos para reprocessar/realocar. 🔴 vencido · 🟡 vence ≤30d · 🟢 ok · ⚪ teste (receita nova sem cadastro). Da aba "Reprocessos" da planilha.</p>
<div class="tablewrap" style="max-height:420px"><table><thead><tr>
<th>Produto</th><th>Ref</th><th>Cat.</th><th>Emb.</th><th>Qtd</th><th>Litros</th><th>Validade</th><th>Situação</th></tr></thead>
<tbody id="tReproc"></tbody></table></div>
<div class="noprint" style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
<button class="btnp" style="background:#2A6FB0" onclick="imprimirSub('rep')">🖨 Imprimir</button>
<button class="btnp" style="background:#25D366" onclick="whatsRep()">📱 WhatsApp</button>
<span class="muted">Lançado na planilha — edite lá para atualizar.</span></div></div></div>

<div class="sub" id="sub-todos" style="display:none">
<div class="slic" id="chips-est"><span class="lbl">Curva</span>
<span class="chip A on" data-v="" onclick="setABC('est','',this)">Todas</span>
<span class="chip A" data-v="A" onclick="setABC('est','A',this)">A</span>
<span class="chip B" data-v="B" onclick="setABC('est','B',this)">B</span>
<span class="chip C" data-v="C" onclick="setABC('est','C',this)">C</span>
<select class="busca" id="acEst" onchange="rEst()"><option value="">Toda ação</option>
<option>Comprar</option><option>Produzir</option><option>Polpa</option></select>
<select class="busca" id="sEst" onchange="rEst()"><option value="">Todo status</option>
<option>Ruptura</option><option>Repor</option><option>OK</option><option>Sem giro</option>
<option>Sem movimento</option><option>Inativo</option></select>
<input class="busca" id="qEst" onkeyup="rEst()" placeholder="Buscar…"></div>
<div class="tile"><div class="tablewrap"><table><thead><tr>
<th onclick="sortT('est','ref')">Ref</th><th onclick="sortT('est','produto')">Item</th>
<th onclick="sortT('est','abc')">ABC</th><th onclick="sortT('est','acao')">Ação</th>
<th onclick="sortT('est','eat')">Estoque</th><th onclick="sortT('est','precisa')">Precisa</th>
<th onclick="sortT('est','comprar')">Repor</th><th onclick="sortT('est','cob')">Cob.(mês)</th>
<th onclick="sortT('est','valor')">Valor</th><th onclick="sortT('est','status')">Status</th>
<th onclick="sortT('est','ult')">Últ. venda</th></tr></thead><tbody id="tEst"></tbody></table></div>
<div class="cont" id="cEst"></div></div></div>
</section>

<section id="producao" class="tab"><h2 class="ttl">Produção — plano de amanhã</h2><p class="hint">Produz o que está abaixo do estoque mínimo (AltosEBaixos): falta = mínimo − atual. Prioriza quem fura antes e enche até __CAP__ batidas/dia.</p>
<div class="slic" id="chips-prod"><span class="lbl">Categoria</span>
<span class="chip on" data-v="" onclick="setCat('prod','',this)">Todas</span>
<span class="chip" data-v="mix" onclick="setCat('prod','mix',this)">Açaí</span>
<span class="chip" data-v="cremes" onclick="setCat('prod','cremes',this)">Cremes</span>
<span class="chip" data-v="gelatos" onclick="setCat('prod','gelatos',this)">Gelato</span></div>
<div class="tile"><div class="th" style="display:flex;justify-content:space-between;align-items:center">🔥 Plano para amanhã <button class="btnp" onclick="window.print()">🖨 Imprimir</button></div>
<div style="margin:0 0 5px;display:flex;justify-content:space-between;font-size:12.5px">
<span class="muted">Capacidade usada</span><span id="capTxt"><b>__CAPUSADA__</b> / __CAP__ batidas</span></div>
<div class="capbar"><i id="capBar"></i></div>
<div class="tablewrap" style="max-height:330px;margin-top:12px"><table><thead><tr>
<th>#</th><th>Ref</th><th>Produto</th><th>Receita</th><th>Dias cob.</th>
<th>Batidas ideais</th><th>Produzir amanhã</th><th>Acumulado</th></tr></thead>
<tbody id="tAmanha"></tbody></table></div><div class="cont" id="cAmanha"></div></div>
<div class="grid2"><div class="tile"><div class="th">Volume previsto por categoria</div><div id="pizVol"></div></div>
<div class="tile"><div class="th">Plano completo <small>· repor ao estoque mínimo · batidas em múltiplos do lote mínimo</small></div>
<div class="tablewrap" style="max-height:340px"><table><thead><tr>
<th>Ref</th><th>Produto</th><th>Estoque</th><th>Mínimo</th><th>Falta</th><th>Lote mín</th><th>Batidas</th><th>Urg.</th></tr></thead>
<tbody id="tPlano"></tbody></table></div></div></div>
<div class="tile"><div class="th">Produção por base <small>· produtos que usam a mesma receita/base — produza a base junto</small></div>
<div class="tablewrap" style="max-height:300px"><table><thead><tr>
<th>Base (receita)</th><th>Lote mín</th><th>Batidas totais</th><th>Produtos</th></tr></thead>
<tbody id="tBases"></tbody></table></div></div></section>


<section id="custos" class="tab"><h2 class="ttl">Custos & Desperdício por Receita</h2><p class="hint">Consumo <b>real</b> de insumo (aba "Produção com insumos") vs <b>teórico</b> (ficha técnica × produção) por <b>mês</b>. Divergência acima de 5% = possível desperdício ou receita desatualizada.</p>
<div class="slic" id="custMeses" style="margin-bottom:8px"></div>
<div class="kpis" id="custKpis" style="grid-template-columns:repeat(4,1fr)"></div>
<div class="slic"><span class="lbl">Mostrar</span>
<span class="chip on" data-v="" onclick="setCust('',this)">Todas as receitas</span>
<span class="chip" data-v="desp" onclick="setCust('desp',this)">Só desperdício (&gt;5%)</span>
<span class="chip" data-v="semrec" onclick="setCust('semrec',this)">Sem ficha técnica</span>
<input class="busca" id="qCust" onkeyup="rCust()" placeholder="Buscar receita…"></div>
<div class="tile"><div class="th">Real × teórico por receita <small>· clique numa receita para ver os insumos</small></div>
<div class="tablewrap" style="max-height:600px"><table><thead><tr>
<th>Receita / Insumo</th><th>Real (kg)</th><th>Teórico (kg)</th><th>Diferença</th><th>% div.</th><th>Custo real (R$)</th></tr></thead>
<tbody id="tCust"></tbody></table></div><div class="cont" id="cCust"></div></div></section>

<section id="prodvenda" class="tab"><h2 class="ttl">Produção / Venda</h2><p class="hint">Produção realizada (aba Produção) × vendas, no mesmo período. Passe o mouse nos gráficos.</p>
<div class="kpis" id="pvKpis" style="grid-template-columns:repeat(8,1fr)"></div>
<div class="grid2"><div class="tile"><div class="th">Litros produzidos × vendidos <small>· clique na legenda para ocultar</small></div><div id="pvLine"></div></div>
<div class="tile"><div class="th">Capacidade produtiva <small>· batidas/dia vs limite</small></div><div id="pvCap"></div></div></div>
<div class="tile" id="pvSazTile"><div class="th">Produtos sazonais <small>· fator do próximo mês (▲ alta / ▼ baixa)</small></div><div id="pvSaz"></div></div>
<div class="slic"><span class="lbl">Análise</span>
<span class="chip on" data-m="anual" onclick="setPVmode('anual',this)">Anual</span>
<span class="chip" data-m="mes" onclick="setPVmode('mes',this)">Por mês</span>
<span class="chip" data-m="exp" onclick="setPVmode('exp',this)">Expectativa (mês atual)</span></div>
<div class="slic" id="pvMeses" style="display:none"><span class="lbl">Meses</span></div>
<div class="slic" id="chips-pv"><span class="lbl">Categoria</span>
<span class="chip on" data-v="" onclick="setCatPV('',this)">Todas</span>
<span class="chip" data-v="mix" onclick="setCatPV('mix',this)">Açaí</span>
<span class="chip" data-v="cremes" onclick="setCatPV('cremes',this)">Cremes</span>
<span class="chip" data-v="gelatos" onclick="setCatPV('gelatos',this)">Gelato</span>
<input class="busca" id="qPV" onkeyup="rPV()" placeholder="Buscar produto…"></div>
<div class="tile"><div class="th" id="pvTh">Produzido × vendido por produto</div>
<div class="tablewrap" style="max-height:460px"><table><thead><tr>
<th onclick="sortPV('nome')">Produto</th><th onclick="sortPV('ref')">Ref</th><th onclick="sortPV('cat')">Cat.</th>
<th id="thProd" onclick="sortPV('prod')">Produzido</th><th id="thVend" onclick="sortPV('vend')">Vendido</th>
<th onclick="sortPV('dif')">Diferença</th><th onclick="sortPV('atend')">Atend. %</th></tr></thead>
<tbody id="tPV"></tbody></table></div><div class="cont" id="cPV"></div></div>
<div class="tile" id="pvNaoMapTile"><div class="th">Produzidos sem correspondência na venda <small>· conferir nome</small></div><div id="pvNaoMap"></div></div>
<div class="tile"><div class="th">Densidade por categoria <small>· usada para converter kg em litros</small></div>
<div style="padding:14px;font-size:13px;color:var(--ink);line-height:1.7">
O painel converte peso em litros usando densidades fixas, validadas com a operação:
<b>Açaí 0,80 kg/L</b> · <b>Cremes 0,70 kg/L</b> · <b>Gelato 0,60 kg/L</b>.<br>
<span style="color:var(--mut);font-size:12px">Ex.: um sachê de 10 kg de açaí = 10 ÷ 0,80 = <b>12,5 L</b>. Produtos com tamanho em litros no nome (CX 5L) usam o litro direto.</span>
</div></div>
</section>

<section id="demanda" class="tab"><h2 class="ttl">Demanda</h2><p class="hint">Clique nos cartões para filtrar. Passe o mouse nos gráficos para ver o valor de cada período. <b>__VOLINFO__</b></p>
<div class="tile" style="border-left:5px solid var(--acai)"><div class="th">🎯 Meta de PRODUÇÃO — 55.000 L/mês <small>· dividida pela proporção real da produção · 5 dias úteis (feriados de Fortaleza descontados)</small></div><div id="metaBox"></div></div>
<div class="cards">
<div class="dcard on" data-v="" onclick="setCat('dem','',this)"><div class="l">Média mensal 2026</div><div class="v">__LTOT__ <span class="u">L / mês</span></div></div>
<div class="dcard" data-v="mix" onclick="setCat('dem','mix',this)"><div class="l">Açaí — média/mês</div><div class="v">__LACAI__ <span class="u">L</span></div></div>
<div class="dcard" data-v="cremes" onclick="setCat('dem','cremes',this)"><div class="l">Cremes — média/mês</div><div class="v">__LCREMES__ <span class="u">L</span></div></div>
<div class="dcard" data-v="gelatos" onclick="setCat('dem','gelatos',this)"><div class="l">Gelato — média/mês</div><div class="v">__LGELATO__ <span class="u">L</span></div></div>
</div>
<div class="grid2"><div class="tile"><div class="th">Litros por mês <small>· 12 meses</small></div><div id="volLine"></div></div>
<div class="tile"><div class="th">Dia a dia <small>· mês atual</small></div><div id="diaLine"></div></div></div>
<div class="tile"><div class="th">Previsão por produto</div>
<div class="slic"><input class="busca" id="qDem" onkeyup="rDem()" placeholder="Buscar produto…"></div>
<div class="tablewrap"><table><thead><tr>
<th onclick="sortT('dem','ref')">Ref</th><th onclick="sortT('dem','produto')">Produto</th>
<th onclick="sortT('dem','grupo')">Grupo</th><th onclick="sortT('dem','dem')">Dem/mês</th>
<th onclick="sortT('dem','fsz')">Fator sazonal</th><th onclick="sortT('dem','dias')">Dias cob.</th></tr></thead>
<tbody id="tDem"></tbody></table></div><div class="cont" id="cDem"></div></div></section>
</div>
<script>
var D=__DATA_JSON__;
var C={acai:'#5B2A86',berry:'#9B3FD1',lil:'#C88DE8',deep:'#2C1640',polpa:'#7B2FC9',
ok:'#2E9E6B',repor:'#E0912F',rup:'#D9434E',sg:'#8A5A9E',sm:'#9A93A6',ina:'#BEB6C8',outros:'#C9B3DD'};
function fmt(v,d){return v==null?'—':new Intl.NumberFormat('pt-BR',{minimumFractionDigits:d||0,maximumFractionDigits:d||0}).format(v);}
function brl(v){return v==null?'—':'R$ '+fmt(v,2);}
function showTab(id){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
document.getElementById(id).classList.add('on');
document.querySelectorAll('.pages button').forEach(b=>b.classList.toggle('on',b.getAttribute('data-t')==id));
window.scrollTo(0,0);}
function cls(a){return '<span class="cls '+a+'">'+a+'</span>';}
function bdg(s){return '<span class="badge b-'+s.replace(/[ /]/g,'')+'">'+s+'</span>';}

function donut(data){var tot=data.reduce((s,d)=>s+(d.value||0),0)||1,R=66,IR=42,cx=76,cy=76,a=-Math.PI/2,p='';
data.forEach(function(d){var v=d.value||0;if(v<=0)return;var f=v/tot,a2=a+f*2*Math.PI,L=f>.5?1:0,ev=d.act?' style="cursor:pointer" onclick="'+d.act+'"':'';
if(f>=0.9999){p+='<circle cx="'+cx+'" cy="'+cy+'" r="'+((R+IR)/2)+'" fill="none" stroke="'+d.color+'" stroke-width="'+(R-IR)+'" class="slice"'+ev+'><title>'+d.label+': '+fmt(v)+'</title></circle>';a=a2;return;}
var x1=cx+R*Math.cos(a),y1=cy+R*Math.sin(a),x2=cx+R*Math.cos(a2),y2=cy+R*Math.sin(a2),
j1=cx+IR*Math.cos(a2),k1=cy+IR*Math.sin(a2),j2=cx+IR*Math.cos(a),k2=cy+IR*Math.sin(a);
p+='<path d="M'+x1.toFixed(1)+' '+y1.toFixed(1)+'A'+R+' '+R+' 0 '+L+' 1 '+x2.toFixed(1)+' '+y2.toFixed(1)+'L'+j1.toFixed(1)+' '+k1.toFixed(1)+'A'+IR+' '+IR+' 0 '+L+' 0 '+j2.toFixed(1)+' '+k2.toFixed(1)+'Z" fill="'+d.color+'" class="slice"'+ev+'><title>'+d.label+': '+fmt(v)+'</title></path>';a=a2;});
var svg='<svg viewBox="0 0 152 152" width="140" height="140" style="flex:none">'+p+'</svg>';
var leg='<div class="dleg">'+data.filter(d=>(d.value||0)>0).map(d=>'<span'+(d.act?' style="cursor:pointer" onclick="'+d.act+'"':'')+'><i style="background:'+d.color+'"></i>'+d.label+'<b>'+fmt(d.value)+'</b></span>').join('')+'</div>';
return '<div class="donutwrap">'+svg+leg+'</div>';}

// gráfico de linha com tooltip que segue o mouse
var LC={};
function lineChart(elId,labels,series,fx){var prev=LC[elId];LC[elId]={labels:labels,series:series,fx:fx,hidden:(prev&&prev.hidden)?prev.hidden:{}};drawLine(elId);}
function toggleSerie(elId,i){var s=LC[elId];if(!s)return;s.hidden[i]=!s.hidden[i];drawLine(elId);}
function drawLine(elId){var st=LC[elId];if(!st)return;var labels=st.labels,series=st.series,fx=st.fx,hidden=st.hidden;
var el=document.getElementById(elId);if(!el)return;
if(!labels.length){el.innerHTML='<p class="muted">Sem dados no período.</p>';return;}
var w=680,h=252,pt=14,pr=14,pb=44,pl=54,iw=w-pl-pr,ih=h-pt-pb,mx=1;
series.forEach((s,si)=>{if(hidden[si])return;s.vals.forEach(v=>{if(v>mx)mx=v;});});
var n=Math.max(1,labels.length-1),X=i=>pl+iw*i/n,Y=v=>pt+ih*(1-v/mx),g='',GL=5;
for(var k=0;k<=GL;k++){var yv=mx*k/GL,yy=Y(yv);
g+='<line x1="'+pl+'" y1="'+yy.toFixed(1)+'" x2="'+(w-pr)+'" y2="'+yy.toFixed(1)+'" stroke="var(--grid)"/>'+
'<text x="'+(pl-6)+'" y="'+(yy+3).toFixed(0)+'" text-anchor="end" font-size="9.5" fill="var(--mut)">'+fmt(yv)+'</text>';}
var rot=labels.length>7;
labels.forEach((lb,i)=>{var xx=X(i).toFixed(1),yy=(h-pb+15).toFixed(0);
g+=rot?'<text x="'+xx+'" y="'+yy+'" text-anchor="end" font-size="9" fill="var(--mut)" transform="rotate(-42 '+xx+' '+yy+')">'+fx(lb)+'</text>'
:'<text x="'+xx+'" y="'+yy+'" text-anchor="middle" font-size="9.5" fill="var(--mut)">'+fx(lb)+'</text>';});
g+='<line id="'+elId+'-gd" x1="0" y1="'+pt+'" x2="0" y2="'+(pt+ih)+'" stroke="var(--acai)" stroke-dasharray="3 3" opacity="0"/>';
series.forEach((s,si)=>{if(hidden[si])return;var pts=s.vals.map((v,i)=>X(i).toFixed(1)+','+Y(v).toFixed(1)).join(' ');
g+='<polyline points="'+pts+'" fill="none" stroke="'+s.color+'" stroke-width="2.4"'+(s.dash?' stroke-dasharray="6 4"':'')+'/>';});
series.forEach((s,si)=>{if(hidden[si])return;s.vals.forEach((v,i)=>{g+='<circle id="'+elId+'-p'+si+'-'+i+'" cx="'+X(i).toFixed(1)+'" cy="'+Y(v).toFixed(1)+'" r="0" fill="'+s.color+'" stroke="#fff" stroke-width="1.5"/>';});});
var leg='<div class="leg">'+series.map((s,si)=>'<span onclick="toggleSerie(\''+elId+'\','+si+')" title="clique para ocultar" style="cursor:pointer;'+(hidden[si]?'opacity:.35;text-decoration:line-through':'')+'"><i style="background:'+s.color+'"></i>'+s.name+'</span>').join('')+'</div>';
el.innerHTML=leg+'<div class="chartbox"><svg id="'+elId+'-svg" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none">'+g+'<rect id="'+elId+'-ov" x="0" y="0" width="'+w+'" height="'+h+'" fill="transparent"/></svg><div class="tip" id="'+elId+'-tip"></div></div>';
var svg=document.getElementById(elId+'-svg'),ov=document.getElementById(elId+'-ov'),gd=document.getElementById(elId+'-gd'),tip=document.getElementById(elId+'-tip');
function mv(ev){var r=svg.getBoundingClientRect();var clientX=ev.touches?ev.touches[0].clientX:ev.clientX;
var vbx=(clientX-r.left)/r.width*w;var frac=Math.max(0,Math.min(1,(vbx-pl)/iw));var i=Math.round(frac*n);
gd.setAttribute('x1',X(i));gd.setAttribute('x2',X(i));gd.setAttribute('opacity','1');
series.forEach((s,si)=>{if(hidden[si])return;s.vals.forEach((v,j)=>{var c=document.getElementById(elId+'-p'+si+'-'+j);if(c)c.setAttribute('r',j==i?4.5:0);});});
tip.innerHTML='<div class="tt">'+fx(labels[i])+'</div>'+series.map((s,si)=>hidden[si]?'':'<div><i style="background:'+s.color+'"></i>'+s.name+'<b>'+fmt(s.vals[i])+'</b></div>').join('');
tip.style.opacity='1';var px=X(i)/w*r.width;tip.style.left=Math.min(r.width-150,Math.max(4,px-70))+'px';}
function lv(){gd.setAttribute('opacity','0');tip.style.opacity='0';series.forEach((s,si)=>{if(hidden[si])return;s.vals.forEach((v,j)=>{var c=document.getElementById(elId+'-p'+si+'-'+j);if(c)c.setAttribute('r',0);});});}
ov.addEventListener('mousemove',mv);ov.addEventListener('mouseleave',lv);ov.addEventListener('touchmove',function(e){e.preventDefault();mv(e);});}
var fMes=lb=>lb.slice(5,7)+'/'+lb.slice(2,4),fDia=lb=>lb.slice(8,10);

function rGeral(){var r=D.resumo;
document.getElementById('pizAbc').innerHTML=donut([
{label:'A',value:r.abcVal.A,color:C.deep,act:"goEst('abc','A')"},
{label:'B',value:r.abcVal.B,color:C.berry,act:"goEst('abc','B')"},
{label:'C',value:r.abcVal.C,color:C.lil,act:"goEst('abc','C')"}]);
var sc={'Ruptura':C.rup,'Repor':C.repor,'OK':C.ok,'Sem giro':C.sg,'Sem movimento':C.sm,'Inativo':C.ina};
document.getElementById('pizStatus').innerHTML=donut(Object.keys(r.status).map(k=>({label:k,value:r.status[k],color:sc[k]||C.sm,act:"goStatus('"+k+"')"})));
var ac={'Comprar':C.berry,'Produzir':C.acai,'Polpa':C.polpa};
document.getElementById('pizAcao').innerHTML=donut(Object.keys(r.acao).map(k=>({label:k,value:r.acao[k],color:ac[k]||C.sm,act:"goAcao('"+k+"')"})));
var vc=r.valCat,ck={'Açaí':C.acai,'Cremes':C.berry,'Gelato':C.lil,'Outros':C.outros};
document.getElementById('pizCat').innerHTML=donut(Object.keys(vc).map(k=>({label:k,value:vc[k],color:ck[k]||C.outros})));}
function goEst(f,v){showTab('estoque');aEst=v;chipSet('est',v);rEst();}
function goStatus(s){showTab('estoque');aEst='';chipSet('est','');document.getElementById('sEst').value=s;rEst();}
function goAcao(a){showTab(a=='Comprar'?'compras':a=='Produzir'?'producao':'polpas');}

function chipSet(sc,v){var b=document.getElementById('chips-'+sc);if(!b)return;
b.querySelectorAll('.chip').forEach(c=>c.classList.toggle('on',(c.getAttribute('data-v')||'')==v));}
var sortState={cmp:{k:'cob',asc:true},est:{k:'',asc:true},dem:{k:'dem',asc:false}};
function sortT(sc,k){var s=sortState[sc];if(s.k==k)s.asc=!s.asc;else{s.k=k;s.asc=true;}sc=='cmp'?rCmp():sc=='est'?rEst():rDem();}
function applySort(arr,sc){var s=sortState[sc];if(!s.k)return arr;
return arr.slice().sort(function(a,b){var x=a[s.k],y=b[s.k];if(x==null)x=-Infinity;if(y==null)y=-Infinity;
if(typeof x=='string'||typeof y=='string')return s.asc?(''+x).localeCompare(y):(''+y).localeCompare(x);return s.asc?x-y:y-x;});}
var aCmp='',aEst='',oCmp='',gProd='',gDem='';
function setABC(sc,v,el){if(sc=='cmp')aCmp=v;else aEst=v;el.parentNode.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));el.classList.add('on');sc=='cmp'?rCmp():rEst();}
function setOrig(v,el){oCmp=v;el.parentNode.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));el.classList.add('on');rCmp();}
function setCat(sc,v,el){if(sc=='prod')gProd=v;else gDem=v;el.parentNode.querySelectorAll(sc=='dem'?'.dcard':'.chip').forEach(c=>c.classList.remove('on'));el.classList.add('on');sc=='prod'?rPlano():rDem();}

function comprasBar(ref){document.getElementById('qCmp').value=ref;rCmp();}
function filtraCmp(){return D.estoque.filter(x=>x.acao=='Comprar'&&(x.status=='Ruptura'||x.status=='Repor')&&(!aCmp||x.abc==aCmp)&&(!oCmp||x.origem==oCmp));}
function rCmp(){var q=(document.getElementById('qCmp').value||'').toLowerCase(),st=document.getElementById('sCmp').value;
var base=filtraCmp().filter(x=>!st||x.status==st),r=applySort(base.filter(x=>!q||(x.produto+x.ref).toLowerCase().indexOf(q)>-1),'cmp');
document.getElementById('tCmp').innerHTML=r.map(x=>'<tr><td class="n">'+x.ref+'</td><td>'+x.produto+'</td><td>'+cls(x.abc)+
'</td><td>'+(x.origem||'—')+'</td><td class="n">'+fmt(x.eat,1)+'</td><td class="n">'+fmt(x.precisa)+
'</td><td class="n"><b>'+fmt(x.comprar)+'</b></td><td class="n">'+fmt(x.cob,1)+'</td><td>'+bdg(x.status)+'</td></tr>').join('');
document.getElementById('cCmp').textContent=r.length+' itens para comprar';
var top=base.slice().sort((a,b)=>b.comprar-a.comprar).slice(0,12),mx=Math.max.apply(null,top.map(x=>x.comprar).concat([1]));
document.getElementById('barCmp').innerHTML=top.length?top.map(x=>'<div class="cbar" onclick="comprasBar('+x.ref+')"><span class="cbl">'+x.produto+'</span><span class="cbt"><i style="width:'+Math.max(3,x.comprar/mx*100)+'%"></i></span><span class="cbv">'+fmt(x.comprar)+'</span></div>').join(''):'<p class="muted">nada a comprar</p>';}

function rEst(){var q=(document.getElementById('qEst').value||'').toLowerCase(),st=document.getElementById('sEst').value,ac=document.getElementById('acEst').value;
var r=applySort(D.estoque.filter(x=>(!aEst||x.abc==aEst)&&(!st||x.status==st)&&(!ac||x.acao==ac)&&(!q||(x.produto+x.ref).toLowerCase().indexOf(q)>-1)),'est');
document.getElementById('tEst').innerHTML=r.map(x=>'<tr><td class="n">'+x.ref+'</td><td>'+x.produto+'</td><td>'+cls(x.abc)+
'</td><td>'+bdg(x.acao)+'</td><td class="n">'+fmt(x.eat,1)+'</td><td class="n">'+fmt(x.precisa)+
'</td><td class="n">'+(x.comprar>0?'<b>'+fmt(x.comprar)+'</b>':'—')+'</td><td class="n">'+fmt(x.cob,1)+
'</td><td class="n">'+brl(x.valor)+'</td><td>'+bdg(x.status)+'</td><td>'+x.ult+'</td></tr>').join('');
document.getElementById('cEst').textContent=r.length+' de '+D.estoque.length+' itens';}

function rPlano(){var pl=D.plano.filter(x=>!gProd||x.grupo==gProd).sort((a,b)=>(b.falta||0)-(a.falta||0)||b.batidas-a.batidas);
document.getElementById('tPlano').innerHTML=pl.map(x=>'<tr><td class="n">'+x.ref+'</td><td>'+x.produto+
'</td><td class="n">'+fmt(x.eat)+'</td><td class="n">'+fmt(x.emn)+'</td><td class="n">'+(x.falta>0?'<b style="color:#C0392B">'+fmt(x.falta)+'</b>':'—')+
'</td><td class="n">'+fmt(x.bmin)+'</td><td class="n">'+fmt(x.batidas,0)+'</td><td>'+bdg(x.urg)+'</td></tr>').join('');
var bg={};pl.forEach(function(x){if(!(x.batidas>0))return;var k=x.receita||'—';var e=bg[k]||(bg[k]={bmin:x.bmin,bat:0,prods:[]});e.bat+=x.batidas;e.prods.push(x.produto);});
var barr=Object.entries(bg).sort((a,b)=>b[1].bat-a[1].bat);
document.getElementById('tBases').innerHTML=barr.length?barr.map(function(kv){var k=kv[0],e=kv[1];
return '<tr><td><b>'+k+'</b></td><td class="n">'+fmt(e.bmin)+'</td><td class="n"><b>'+fmt(e.bat)+'</b></td><td style="white-space:normal;font-size:11.5px;color:var(--mut)">'+e.prods.join(' · ')+'</td></tr>';}).join('')
:'<tr><td colspan="4" class="muted" style="padding:12px">Nada a produzir agora nesta categoria.</td></tr>';
var amAll=(D.amanha||[]);
var am=amAll.filter(x=>!gProd||x.grupo==gProd);
document.getElementById('tAmanha').innerHTML=am.length?am.map(function(x,i){return '<tr'+(x.cabe?'':' style="opacity:.55"')+'><td class="n">'+(i+1)+'</td><td class="n">'+x.ref+'</td><td>'+x.produto+'</td><td>'+x.receita+
'</td><td class="n">'+fmt(x.dias)+'</td><td class="n">'+fmt(x.ideal,1)+'</td><td class="n"><b>'+fmt(x.produzir,1)+'</b></td><td class="n">'+fmt(x.acum,1)+'</td></tr>';}).join('')
:'<tr><td colspan="8" class="muted" style="padding:14px">Nada urgente nesta categoria 🎉</td></tr>';
var usadaTot=amAll.reduce((s,x)=>s+(x.produzir||0),0),usadaCat=am.reduce((s,x)=>s+(x.produzir||0),0);
var pct=Math.min(100,Math.round(usadaTot/D.cap*100));
document.getElementById('capBar').style.width=pct+'%';
document.getElementById('capTxt').innerHTML='<b>'+fmt(usadaTot,1)+'</b> / '+D.cap+' batidas';
var fora=am.filter(x=>!x.cabe).length,gn={mix:'Açaí',cremes:'Cremes',gelatos:'Gelato'}[gProd];
document.getElementById('cAmanha').textContent=(gProd?gn+': '+fmt(usadaCat,1)+' batidas · ':'')+am.length+' itens · plano total '+fmt(usadaTot,1)+'/'+D.cap+' batidas'+(fora?' · '+fora+' não couberam (próximo dia)':'');
document.getElementById('pizVol').innerHTML=donut([
{label:'Açaí',value:D.cat.mix.L,color:C.acai,act:"setCatFromPie('mix')"},
{label:'Cremes',value:D.cat.cremes.L,color:C.berry,act:"setCatFromPie('cremes')"},
{label:'Gelato',value:D.cat.gelatos.L,color:C.lil,act:"setCatFromPie('gelatos')"}]);}
function setCatFromPie(g){gProd=g;chipSetProd(g);rPlano();}
function chipSetProd(g){document.querySelectorAll('#chips-prod .chip').forEach(c=>c.classList.toggle('on',(c.getAttribute('data-v')||'')==g));}

function rPolpa(){var r=D.estoque.filter(x=>x.polpa).sort((a,b)=>(b.eat||0)-(a.eat||0));
var vTot=0,qTot=0,vendTot=0;r.forEach(x=>{vTot+=x.valor||0;qTot+=x.eat||0;vendTot+=x.dem||0;});
var dias=vendTot>0?Math.round(qTot/(vendTot/30)):null;
var K=[['Valor em estoque','R$ '+fmt(vTot,2),''],['Estoque (un)',fmt(qTot),''],
['Vendidas / mês',fmt(vendTot),''],['Cobertura (dias)',dias==null?'—':fmt(dias),(dias!=null&&dias<60)?'al':'']];
document.getElementById('polpaKpis').innerHTML=K.map(k=>'<div class="kpi '+k[2]+'"><div class="l">'+k[0]+'</div><div class="v">'+k[1]+'</div></div>').join('');
document.getElementById('tPolpa').innerHTML=r.length?r.map(function(x){
var tipo=x.consumo>0?(x.dem>0?'Insumo+Venda':'Insumo'):(x.dem>0?'Revenda':'Parado');
var loc=x.polpaTipo=='lockfrio'?'<span style="color:#0e7f9e;font-weight:700">🧊 Lockfrio</span>':(x.polpaTipo=='aqui'?'Aqui':'—');
var alerta=(x.valor==0&&x.eat>0)?' ⚠':'';
return '<tr><td class="n">'+x.ref+'</td><td>'+x.produto+alerta+'</td><td>'+loc+'</td><td>'+tipo+'</td><td class="n">'+fmt(x.eat,1)+
'</td><td class="n">'+brl(x.valor)+'</td><td class="n">'+fmt(x.dem,1)+'</td><td class="n">'+fmt(x.consumo,1)+'</td><td class="n">'+
(x.comprar>0?'<b>'+fmt(x.comprar)+'</b>':'—')+'</td><td class="n">'+fmt(x.cob,1)+'</td><td>'+bdg(x.status)+'</td></tr>';}).join('')
:'<tr><td colspan="11" class="muted" style="padding:14px">Nenhuma polpa identificada</td></tr>';
document.getElementById('cPolpa').textContent=r.length+' polpas · cobertura considera lead time de 60 dias';
lineChart('polpaLine',D.meses,[{name:'Polpa (kg)',vals:D.polpa,color:C.polpa}],fMes);}

// ============ SUB-ABAS DO ESTOQUE ============
function showSub(s,el){el.parentNode.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));el.classList.add('on');
['visao','camara','polpas','seco','reproc','todos'].forEach(k=>{var d=document.getElementById('sub-'+k);if(d)d.style.display=k==s?'':'none';});
if(s=='visao')rVisao();if(s=='camara')rCam();if(s=='seco')rSeco();if(s=='reproc')rReproc();
if(s=='polpas')rPolpaFull();}
function rPolpaFull(){rPolpa();rConf();rConfLf();}

// Visão geral do estoque
function rVisao(){var todos=D.estoque.filter(x=>!x.imob);
var acc={camara:{v:0,n:0,rup:0},polpa:{v:0,n:0,rup:0},seco:{v:0,n:0,rup:0},reprocesso:{v:0,n:0,rup:0}};
todos.forEach(function(x){var g=acc[x.subEst];if(!g)return;g.v+=x.valor||0;g.n++;if(x.status=='Ruptura'||x.status=='Repor')g.rup++;});
var totV=acc.camara.v+acc.polpa.v+acc.seco.v+acc.reprocesso.v;
var parado=todos.filter(x=>(x.status=='Sem giro'||x.status=='Sem movimento')&&x.eat>0).reduce((s,x)=>s+(x.valor||0),0);
var imob=D.estoque.filter(x=>x.imob).reduce((s,x)=>s+(x.valor||0),0);
var K=[['Valor operacional','R$ '+fmt(totV,2),''],['Câmara Fria','R$ '+fmt(acc.camara.v,2),''],
['Polpas','R$ '+fmt(acc.polpa.v,2),''],['Estoque Seco','R$ '+fmt(acc.seco.v,2),''],
['Imobilizado','R$ '+fmt(imob,2),'']];
document.getElementById('visKpis').innerHTML=K.map(k=>'<div class="kpi '+k[2]+'"><div class="l">'+k[0]+'</div><div class="v">'+k[1]+'</div></div>').join('');
document.getElementById('pizSubEst').innerHTML=donut([
{label:'Câmara Fria',value:acc.camara.v,color:'#4ba4ff',act:"showSubById('camara')"},
{label:'Polpas',value:acc.polpa.v,color:C.polpa,act:"showSubById('polpas')"},
{label:'Estoque Seco',value:acc.seco.v,color:'#b8952a',act:"showSubById('seco')"},
{label:'Reprocesso',value:acc.reprocesso.v,color:'#8ba05a',act:"showSubById('reproc')"}]);
var sub={camara:'❄️ Câmara',polpa:'🫐 Polpa',seco:'📦 Seco',reprocesso:'♻️ Reproc',imob:'Imob'};
var top=todos.filter(x=>x.status=='Ruptura'||x.status=='Repor').sort((a,b)=>(b.dem||0)-(a.dem||0)).slice(0,10);
document.getElementById('tRupTop').innerHTML=top.length?top.map(x=>'<tr><td class="n">'+x.ref+'</td><td>'+x.produto+'</td><td>'+(sub[x.subEst]||'—')+'</td><td class="n">'+fmt(x.eat,0)+'</td><td class="n">'+fmt(x.precisa,0)+'</td><td class="n" style="color:#C0392B;font-weight:700">'+fmt((x.precisa||0)-(x.eat||0),0)+'</td></tr>').join(''):'<tr><td colspan="6" class="muted" style="padding:14px">Nada em ruptura 🎉</td></tr>';}
function showSubById(s){var el=document.querySelector('#subnav-est .chip[data-s="'+s+'"]');if(el)showSub(s,el);}

// Câmara fria
function rCam(){var r=D.estoque.filter(x=>x.subEst=='camara').sort((a,b)=>(b.eat||0)-(a.eat||0));
var vTot=0,qTot=0,rup=0;r.forEach(x=>{vTot+=x.valor||0;qTot+=x.eat||0;if(x.status=='Ruptura'||x.status=='Repor')rup++;});
var K=[['Valor em estoque','R$ '+fmt(vTot,2),''],['Itens',fmt(r.length),''],['Unidades',fmt(qTot),''],['Abaixo do mínimo',fmt(rup),rup?'al':'']];
document.getElementById('camKpis').innerHTML=K.map(k=>'<div class="kpi '+k[2]+'"><div class="l">'+k[0]+'</div><div class="v">'+k[1]+'</div></div>').join('');
var cat=function(g){return g=='mix'?'Açaí':g=='cremes'?'Cremes':g=='gelatos'?'Gelato':(g||'—');};
document.getElementById('tCam').innerHTML=r.length?r.map(x=>'<tr><td class="n">'+x.ref+'</td><td>'+x.produto+'</td><td class="n">'+fmt(x.eat,0)+'</td><td class="n">'+fmt(x.precisa,0)+'</td><td class="n">'+((x.precisa||0)>(x.eat||0)?'<b style="color:#C0392B">'+fmt((x.precisa||0)-(x.eat||0),0)+'</b>':'—')+'</td><td class="n">'+fmt(x.cob,1)+'</td><td class="n">'+brl(x.valor)+'</td><td>'+bdg(x.status)+'</td></tr>').join(''):'<tr><td colspan="8" class="muted" style="padding:14px">Nenhum item classificado como Câmara Fria</td></tr>';
rConfSub('cam',r,'tConfCam','camDia','camInfo',function(x){return '<td>'+cat(x.grupo)+'</td>';},'cat');}

// Estoque Seco
function rSeco(){var q=(document.getElementById('qSeco')?document.getElementById('qSeco').value:'').toLowerCase();
var all=D.estoque.filter(x=>x.subEst=='seco').sort((a,b)=>(b.valor||0)-(a.valor||0));
var vTot=0,parado=0,rup=0;all.forEach(x=>{vTot+=x.valor||0;if((x.status=='Sem giro'||x.status=='Sem movimento')&&x.eat>0)parado+=x.valor||0;if(x.status=='Ruptura'||x.status=='Repor')rup++;});
var K=[['Valor em estoque','R$ '+fmt(vTot,2),''],['Itens',fmt(all.length),''],['Parado (R$)','R$ '+fmt(parado,2),parado?'al':''],['Abaixo do mínimo',fmt(rup),rup?'al':'']];
document.getElementById('secoKpis').innerHTML=K.map(k=>'<div class="kpi '+k[2]+'"><div class="l">'+k[0]+'</div><div class="v">'+k[1]+'</div></div>').join('');
var r=q?all.filter(x=>(x.produto+x.ref).toLowerCase().indexOf(q)>-1):all;
document.getElementById('tSeco').innerHTML=r.length?r.map(x=>'<tr><td class="n">'+x.ref+'</td><td>'+x.produto+'</td><td style="font-size:11px;color:var(--mut)">'+(x.grupo||'—').slice(0,26)+'</td><td class="n">'+fmt(x.eat,0)+'</td><td class="n">'+fmt(x.consumo,0)+'</td><td class="n">'+brl(x.valor)+'</td><td class="n">'+fmt(x.cob,1)+'</td><td>'+bdg(x.status)+'</td></tr>').join(''):'<tr><td colspan="8" class="muted" style="padding:14px">—</td></tr>';
document.getElementById('cSeco').textContent=r.length+' de '+all.length+' itens';
rConfSeco();}
function rConfSeco(){var q=(document.getElementById('qConfSeco').value||'').toLowerCase();
var all=D.estoque.filter(x=>x.subEst=='seco').sort((a,b)=>(b.valor||0)-(a.valor||0));
var r=q?all.filter(x=>(x.produto+x.ref).toLowerCase().indexOf(q)>-1):all.slice(0,80);
rConfSub('seco',r,'tConfSeco','secoDia','secoInfo',function(x){return '';},'');}

// Reprocesso
function rReproc(){var r=D.reproc||[];
var Ltot=0,venc=0,teste=0;r.forEach(function(x){Ltot+=x.litros||0;if(x.situacao=='vencido')venc++;if(x.situacao=='teste')teste++;});
var K=[['Litros em reprocesso',fmt(Ltot,0)+' L',''],['Itens',fmt(r.length),''],
['Vencidos',fmt(venc),venc?'al':''],['Em teste',fmt(teste),'']];
document.getElementById('repKpis').innerHTML=K.map(k=>'<div class="kpi '+k[2]+'"><div class="l">'+k[0]+'</div><div class="v">'+k[1]+'</div></div>').join('');
if(document.getElementById('repDia'))document.getElementById('repDia').textContent=new Date().toLocaleDateString('pt-BR');
var sinal={vencido:'🔴',vencendo:'🟡',ok:'🟢',teste:'⚪'};
var stxt={vencido:'Vencido',vencendo:'Vence ≤30d',ok:'OK',teste:'Teste'};
var cat=function(g){return g=='mix'?'Açaí':g=='cremes'?'Cremes':g=='gelatos'?'Gelato':'—';};
document.getElementById('tReproc').innerHTML=r.length?r.map(function(x){
var cor=x.situacao=='vencido'?'#C0392B':x.situacao=='vencendo'?'#B7791F':'var(--ink)';
var dd=x.dias!=null?(x.dias<0?' ('+Math.abs(x.dias)+'d atrás)':' ('+x.dias+'d)'):'';
return '<tr><td>'+x.nome+(x.prodCad?'<br><span style="font-size:10px;color:var(--mut)">'+x.prodCad+'</span>':'')+
'</td><td class="n">'+(x.ref||'—')+'</td><td>'+cat(x.cat)+'</td><td class="n">'+fmt(x.litragem,0)+'L</td>'+
'<td class="n">'+fmt(x.qtd,0)+'</td><td class="n"><b>'+fmt(x.litros,0)+'</b></td>'+
'<td class="n">'+x.validade+dd+'</td><td style="color:'+cor+';font-weight:700">'+(sinal[x.situacao]||'')+' '+(stxt[x.situacao]||'')+'</td></tr>';}).join('')
:'<tr><td colspan="8" class="muted" style="padding:14px">Aba "Reprocessos" vazia.</td></tr>';}
function whatsRep(){var r=D.reproc||[];if(!r.length){alert('Sem reprocessos lançados.');return;}
var dia=new Date().toLocaleDateString('pt-BR');var Ltot=0;
var stxt={vencido:'VENCIDO',vencendo:'vence ≤30d',ok:'ok',teste:'teste'};
var linhas=r.map(function(x){Ltot+=x.litros||0;
return '• '+x.nome+' ('+fmt(x.litragem,0)+'L × '+fmt(x.qtd,0)+' = '+fmt(x.litros,0)+'L)\n   '+x.validade+' — '+(stxt[x.situacao]||'');});
var txt='*♻️ Reprocessos — Supremo Açaí*\n'+dia+'\n\n'+linhas.join('\n')+'\n\n*Total:* '+fmt(Ltot,0)+' L em reprocesso';
window.open('https://wa.me/?text='+encodeURIComponent(txt),'_blank');}

// Conferência genérica (compartilhada por câmara, polpa lockfrio e seco)
var CONF_KEYS={cam:'confCam',lf:'confLf',seco:'confSeco'};
function _kconf(sub){return CONF_KEYS[sub]+'_'+(new Date().toISOString().slice(0,10));}
function _loadCf(sub){try{return JSON.parse(localStorage.getItem(_kconf(sub))||'{}');}catch(e){return {};}}
function rConfSub(sub,arr,tbodyId,diaId,infoId,extraCol,extraColKey){
var salvo=_loadCf(sub);
if(document.getElementById(diaId))document.getElementById(diaId).textContent=new Date().toLocaleDateString('pt-BR');
document.getElementById(tbodyId).innerHTML=arr.map(function(x){var v=salvo[x.ref];var dif=v!=null?v-x.eat:null;var pd=(v!=null&&x.eat>0)?(dif/x.eat*100):null;
var bg=sub=='cam'?'#DBEBFB':sub=='lf'?'#D6EEF6':'#FBF0D3';
return '<tr><td class="n">'+x.ref+'</td><td>'+x.produto+'</td>'+extraCol(x)+'<td class="n">'+fmt(x.eat,0)+'</td>'+
'<td style="background:'+bg+'"><input type="number" step="0.1" data-ref="'+x.ref+'" value="'+(v!=null?v:'')+'" style="width:100%;padding:5px 8px;border:1px solid var(--line);border-radius:6px;font-family:inherit;font-size:13px;text-align:right" placeholder="—"></td>'+
'<td class="n" style="color:'+(dif==null?'var(--mut)':(dif<0?'var(--rup)':'var(--ok)'))+';font-weight:700">'+(dif==null?'—':(dif>0?'+':'')+fmt(dif,0))+'</td>'+
'<td class="n" style="color:'+(pd==null?'var(--mut)':_pctColor(pd))+';font-weight:700">'+(pd==null?'—':(pd>0?'+':'')+fmt(pd,1)+'%')+'</td></tr>';}).join('');
var n=Object.keys(salvo).length;
if(document.getElementById(infoId))document.getElementById(infoId).textContent=n?n+' item(ns) contado(s) hoje':'preencha a contagem física';}
function salvarCf(sub){var tbody={cam:'tConfCam',lf:'tConfLf',seco:'tConfSeco'}[sub];var obj={};
document.querySelectorAll('#'+tbody+' input[data-ref]').forEach(function(i){var v=parseFloat(i.value.replace(',','.'));if(!isNaN(v))obj[i.getAttribute('data-ref')]=v;});
try{localStorage.setItem(_kconf(sub),JSON.stringify(obj));
if(sub=='cam')rCam();if(sub=='lf')rConfLf();if(sub=='seco')rConfSeco();
var info={cam:'camInfo',lf:'lfInfo',seco:'secoInfo'}[sub];document.getElementById(info).textContent='✓ salvo · '+Object.keys(obj).length+' item(ns)';}
catch(e){alert('Não foi possível salvar: '+e);}}
function limparCf(sub){if(!confirm('Limpar contagem de hoje?'))return;try{localStorage.removeItem(_kconf(sub));}catch(e){}
if(sub=='cam')rCam();if(sub=='lf')rConfLf();if(sub=='seco')rConfSeco();}
function _doPrint(elId){var el=document.getElementById(elId);if(!el){window.print();return;}
el.classList.add('to-print');document.body.classList.add('print-only');
setTimeout(function(){window.print();setTimeout(function(){el.classList.remove('to-print');document.body.classList.remove('print-only');},200);},80);}
function imprimirSub(sub){var id={cam:'confCamTile',lf:'confLfTile',seco:'confSecoTile',aqui:'confAquiTile',rep:'confRepTile'}[sub];_doPrint(id);}
function imprimirConf(){_doPrint('confAquiTile');}
function whatsCf(sub){
var arr;if(sub=='cam')arr=D.estoque.filter(x=>x.subEst=='camara');
else if(sub=='lf')arr=D.estoque.filter(x=>x.polpa&&x.polpaTipo=='lockfrio');
else arr=D.estoque.filter(x=>x.subEst=='seco');
var salvo=_loadCf(sub);var linhas=[],contadas=0,divTot=0;
arr.forEach(function(x){var v=salvo[x.ref];if(v==null)return;contadas++;var dif=v-x.eat;divTot+=Math.abs(dif);
var sinal=dif>0?'+':(dif<0?'':'');var nome=x.produto.length>26?x.produto.slice(0,26):x.produto;
linhas.push('• '+nome+'\n   Sistema '+fmt(x.eat,0)+' · Físico '+fmt(v,0)+' · Dif '+sinal+fmt(dif,0));});
if(!contadas){alert('Preencha e salve a contagem antes de compartilhar.');return;}
var titulo={cam:'❄️ Câmara Fria',lf:'🧊 Polpas Lockfrio',seco:'📦 Estoque Seco'}[sub];
var dia=new Date().toLocaleDateString('pt-BR');
var txt='*📋 Conferência Física — '+titulo+'*\n*Supremo Açaí* · '+dia+'\n\n'+linhas.join('\n\n')+
'\n\n*Resumo:* '+contadas+' item(ns) contado(s) · Divergência total: '+fmt(divTot,0)+' un';
window.open('https://wa.me/?text='+encodeURIComponent(txt),'_blank');}
function rConfLf(){var pol=D.estoque.filter(x=>x.polpa&&x.polpaTipo=='lockfrio').sort((a,b)=>(b.eat||0)-(a.eat||0));
if(document.getElementById('lfDia'))document.getElementById('lfDia').textContent=new Date().toLocaleDateString('pt-BR');
var salvo=_loadCf('lf');
document.getElementById('tConfLf').innerHTML=pol.map(function(x){var v=salvo[x.ref];var dif=v!=null?v-x.eat:null;var pd=(v!=null&&x.eat>0)?(dif/x.eat*100):null;
return '<tr><td class="n">'+x.ref+'</td><td>'+x.produto+'</td><td class="n">'+fmt(x.eat,0)+
'</td><td style="background:#D6EEF6"><input type="number" step="0.1" data-ref="'+x.ref+'" value="'+(v!=null?v:'')+'" style="width:100%;padding:5px 8px;border:1px solid var(--line);border-radius:6px;font-family:inherit;font-size:13px;text-align:right" placeholder="—"></td>'+
'<td class="n" style="color:'+(dif==null?'var(--mut)':(dif<0?'var(--rup)':'var(--ok)'))+';font-weight:700">'+(dif==null?'—':(dif>0?'+':'')+fmt(dif,0))+'</td>'+
'<td class="n" style="color:'+(pd==null?'var(--mut)':_pctColor(pd))+';font-weight:700">'+(pd==null?'—':(pd>0?'+':'')+fmt(pd,1)+'%')+'</td></tr>';}).join('');
var n=Object.keys(salvo).length;document.getElementById('lfInfo').textContent=n?n+' polpa(s) contadas hoje':'preencha a contagem física';}

function rDemGraf(){lineChart('volLine',D.meses,[
{name:'Açaí',vals:D.vol.mix,color:C.acai},{name:'Cremes',vals:D.vol.cremes,color:C.berry},{name:'Gelato',vals:D.vol.gelatos,color:C.lil}],fMes);
lineChart('diaLine',D.diaLabels||[],[
{name:'Açaí',vals:D.dia.mix,color:C.acai},{name:'Cremes',vals:D.dia.cremes,color:C.berry},{name:'Gelato',vals:D.dia.gelatos,color:C.lil}],fDia);}
function rDem(){var q=(document.getElementById('qDem').value||'').toLowerCase();
var r=applySort(D.estoque.filter(x=>x.dem>0&&(!gDem||x.grupo==gDem)&&(!q||(x.produto+x.ref).toLowerCase().indexOf(q)>-1)),'dem');
document.getElementById('tDem').innerHTML=r.map(x=>'<tr><td class="n">'+x.ref+'</td><td>'+x.produto+'</td><td>'+
(x.grupo||'—')+'</td><td class="n">'+fmt(x.dem,1)+'</td><td class="n">'+fmt(x.fsz,2)+'</td><td class="n">'+fmt(x.dias)+'</td></tr>').join('');
document.getElementById('cDem').textContent=r.length+' produtos'+(gDem?' ('+gDem+')':'');}

var gPV='',sortPVs={k:'prod',asc:false},pvMode='anual',pvSel=[];
function setCatPV(v,el){gPV=v;el.parentNode.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));el.classList.add('on');rPVgraf();rPV();}
function togMes(i,el){var k=pvSel.indexOf(i);if(k>-1)pvSel.splice(k,1);else pvSel.push(i);el.classList.toggle('on');rPV();}
function setPVmode(m,el){pvMode=m;el.parentNode.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));el.classList.add('on');
document.getElementById('pvMeses').style.display=(m=='mes')?'':'none';
var th={anual:['Produzido (ano)','Vendido (ano)'],mes:['Produzido','Vendido'],exp:['Produção projetada','Venda esperada']}[m];
document.getElementById('thProd').textContent=th[0];document.getElementById('thVend').textContent=th[1];
document.getElementById('pvTh').textContent={anual:'Produção × venda anual',mes:'Produção × venda nos meses escolhidos',exp:'Expectativa do mês atual'}[m];rPV();}
function sortPV(k){if(sortPVs.k==k)sortPVs.asc=!sortPVs.asc;else{sortPVs.k=k;sortPVs.asc=false;}rPV();}
function somaCat(o,i){return o.mix[i]+o.cremes[i]+o.gelatos[i];}
function serieCat(o){return gPV?o[gPV]:D.pv.meses.map((m,i)=>somaCat(o,i));}
function rPVgraf(){var pv=D.pv;if(!pv)return;
lineChart('pvLine',pv.meses,[{name:'Produzido',vals:serieCat(pv.prodLit),color:C.acai},{name:'Vendido',vals:serieCat(pv.vendLit),color:C.berry}],fMes);
var bd=pv.meses.map((m,i)=>pv.diasProdMes[i]>0?Math.round(pv.batidasMes[i]/pv.diasProdMes[i]):0);
lineChart('pvCap',pv.meses,[{name:'Batidas/dia',vals:bd,color:C.acai},{name:'Capacidade ('+pv.cap+')',vals:pv.meses.map(()=>pv.cap),color:C.rup,dash:true}],fMes);}
function rPVkpis(){var pv=D.pv;
var pu=0,vu=0,pl2=0,vl=0;['mix','cremes','gelatos'].forEach(g=>{
pv.prodUn[g].forEach(v=>pu+=v);pv.vendUn[g].forEach(v=>vu+=v);
pv.prodLit[g].forEach(v=>pl2+=v);pv.vendLit[g].forEach(v=>vl+=v);});
var atend=vu>0?Math.round(pu/vu*100):0,sd=pv.saldoDias;
var K=[['Produzido (un)',fmt(pu),''],['Vendido (un)',fmt(vu),''],
['Litros produzidos',fmt(pl2),''],['Litros vendidos',fmt(vl),''],
['Atendimento',atend+'%',atend<90?'al':''],['Litros em estoque',fmt(pv.litrosEstoque),''],
['Dias de estoque',fmt(pv.diasEstoque,1),''],
[(sd>=0?'À frente':'Atrás')+' (dias)',(sd>=0?'+':'')+fmt(sd,1),sd<0?'al':'']];
document.getElementById('pvKpis').innerHTML=K.map(k=>'<div class="kpi '+k[2]+'"><div class="l">'+k[0]+'</div><div class="v">'+k[1]+'</div></div>').join('');}
function calcRow(x){var prod,vend;
if(pvMode=='anual'){prod=x.prodMes.reduce((s,v)=>s+v,0);vend=x.vendMes.reduce((s,v)=>s+v,0);}
else if(pvMode=='mes'){var idx=pvSel.length?pvSel:x.prodMes.map((v,i)=>i);prod=0;vend=0;idx.forEach(i=>{prod+=x.prodMes[i]||0;vend+=x.vendMes[i]||0;});}
else{prod=Math.round((x.prodAtual||0)/(D.pv.fracMes||1));vend=x.dem||0;}
return {nome:x.nome,ref:x.ref,cat:x.cat,prod:prod,vend:vend,dif:prod-vend,atend:vend>0?Math.round(prod/vend*100):null};}
function rPV(){var pv=D.pv;if(!pv)return;var q=(document.getElementById('qPV').value||'').toLowerCase();
var r=pv.produtos.filter(x=>(!gPV||x.cat==gPV)&&(!q||(x.nome+x.ref).toLowerCase().indexOf(q)>-1)).map(calcRow);
var s=sortPVs;r.sort(function(a,b){var x=a[s.k],y=b[s.k];if(x==null)x=-Infinity;if(y==null)y=-Infinity;
if(typeof x=='string')return s.asc?(''+x).localeCompare(y):(''+y).localeCompare(x);return s.asc?x-y:y-x;});
document.getElementById('tPV').innerHTML=r.map(function(x){var col=x.atend==null?'':(x.atend<80?'color:#C0392B':x.atend>130?'color:#2A6FB0':'');
return '<tr><td>'+x.nome+'</td><td class="n">'+x.ref+'</td><td>'+({mix:'Açaí',cremes:'Cremes',gelatos:'Gelato'}[x.cat]||x.cat)+
'</td><td class="n">'+fmt(x.prod)+'</td><td class="n">'+fmt(x.vend)+'</td><td class="n">'+fmt(x.dif)+
'</td><td class="n" style="'+col+'">'+(x.atend==null?'—':x.atend+'%')+'</td></tr>';}).join('');
document.getElementById('cPV').textContent=r.length+' produtos';
document.getElementById('pvNaoMap').innerHTML=pv.naoMap.length?('<div class="cont">'+pv.naoMap.map(x=>x.nome+' ('+fmt(x.qtd)+')').join(' · ')+'</div>'):'<div class="muted">Todos os produzidos casaram com um item de venda ✓</div>';}
function rSaz(){var pv=D.pv;if(!pv.sazonais||!pv.sazonais.length){document.getElementById('pvSaz').innerHTML='<div class="muted">Nenhum produto com sazonalidade forte.</div>';return;}
document.getElementById('pvSaz').innerHTML='<div style="display:flex;flex-wrap:wrap;gap:8px">'+pv.sazonais.map(function(x){var alta=x.fsz>1;
return '<span style="padding:5px 11px;border-radius:20px;font-size:12px;font-weight:600;background:'+(alta?'#EAF3EC':'#FCEEF0')+';color:'+(alta?'#2E7D5B':'#C0392B')+'">'+x.nome+' · '+(alta?'▲':'▼')+' '+fmt(x.fsz,2)+'×</span>';}).join('')+'</div>';}
function initPV(){if(!D.pv){document.getElementById('prodvenda').innerHTML='<h2 class="ttl">Produção / Venda</h2><p class="hint">Aba \"Produção\" não encontrada ou vazia.</p>';return;}
var box=document.getElementById('pvMeses'),last=D.pv.meses.length-1;
box.innerHTML='<span class="lbl">Meses</span>'+D.pv.meses.map((m,i)=>'<span class="chip'+(i==last?' on':'')+'" onclick="togMes('+i+',this)">'+m.slice(5,7)+'/'+m.slice(0,4)+'</span>').join('');
pvSel=[last];rPVkpis();rPVgraf();rSaz();rPV();}

function rCockpit(){
var li=function(nm,val){return '<li><span>'+nm+'</span><b>'+val+'</b></li>';};
var rup=D.estoque.filter(x=>x.status=='Ruptura').sort((a,b)=>(b.dem||0)-(a.dem||0));
var buy=D.estoque.filter(x=>x.acao=='Comprar'&&(x.status=='Ruptura'||x.status=='Repor')).sort((a,b)=>b.comprar-a.comprar);
var am=(D.amanha||[]).slice().sort((a,b)=>b.produzir-a.produzir);
var batTot=am.reduce((s,x)=>s+(x.produzir||0),0);
var pol=D.estoque.filter(x=>x.polpa);var pq=0,pv=0;pol.forEach(x=>{pq+=x.eat||0;pv+=x.dem||0;});
var pdias=pv>0?Math.round(pq/(pv/30)):null;var polAl=(pdias!=null&&pdias<60);
var cards=[
{cls:'rup',h:'Rupturas agora',big:fmt(rup.length),act:"goStatus('Ruptura')",go:'Ver no estoque',
 items:rup.slice(0,3).map(x=>li(x.produto,'estq '+fmt(x.eat,0)))},
{cls:'prod',h:'Produzir amanhã',big:fmt(batTot,0)+' <span style=\"font-size:13px\">batidas</span>',act:"showTab('producao')",go:'Ver produção',
 items:am.slice(0,3).map(x=>li(x.produto,fmt(x.produzir,0)+' bat'))},
{cls:'buy',h:'Comprar agora',big:fmt(buy.length),act:"showTab('compras')",go:'Ver compras',
 items:buy.slice(0,3).map(x=>li(x.produto,fmt(x.comprar,0)))},
{cls:'pol'+(polAl?' al':''),h:'Cobertura de polpa',big:(pdias==null?'—':fmt(pdias)+' <span style=\"font-size:13px\">dias</span>'),act:"showTab('polpas')",go:'Ver polpas',
 items:[li('Lead time','60 dias'),li('Situação',polAl?'⚠ abaixo':'ok')]}];
document.getElementById('cockpit').innerHTML=cards.map(c=>'<div class="act '+c.cls+'" onclick="'+c.act+'">'+
'<div class="h">'+c.h+'</div><div class="big">'+c.big+'</div><ul>'+(c.items.join('')||'<li class="muted">nada 🎉</li>')+'</ul>'+
'<div class="go">'+c.go+' →</div></div>').join('');}

function rMeta(){var m=D.meta;if(!m){document.getElementById('metaBox').innerHTML='<p class="muted">Meta indisponível.</p>';return;}
var pct=Math.min(100,Math.round(m.prodMesTot/m.total*100)),ok=pct>=100;
var raio=function(p){var R=54,r=44,cx=64,cy=64,a=-Math.PI/2,a2=a+2*Math.PI*p/100,L=p>50?1:0;
if(p>=100)a2=a+2*Math.PI-0.001;
var x1=cx+R*Math.cos(a),y1=cy+R*Math.sin(a),x2=cx+R*Math.cos(a2),y2=cy+R*Math.sin(a2);
var j1=cx+r*Math.cos(a2),k1=cy+r*Math.sin(a2),j2=cx+r*Math.cos(a),k2=cy+r*Math.sin(a);
var arc='<path d="M'+x1.toFixed(1)+' '+y1.toFixed(1)+'A'+R+' '+R+' 0 '+L+' 1 '+x2.toFixed(1)+' '+y2.toFixed(1)+'L'+j1.toFixed(1)+' '+k1.toFixed(1)+'A'+r+' '+r+' 0 '+L+' 0 '+j2.toFixed(1)+' '+k2.toFixed(1)+'Z" fill="'+(ok?C.ok:C.acai)+'"/>';
return '<svg viewBox="0 0 128 128" width="120" height="120" style="flex:none"><circle cx="64" cy="64" r="'+R+'" fill="none" stroke="#EEE6F2" stroke-width="10"/>'+arc+
'<text x="64" y="66" text-anchor="middle" font-size="22" font-weight="800" fill="var(--deep)">'+p+'%</text>'+
'<text x="64" y="82" text-anchor="middle" font-size="9.5" fill="var(--mut)">da meta</text></svg>';};
var faltaBar=Math.min(100,Math.round(m.prodMesTot/m.total*100));
var cats=[{k:'mix',n:'Açaí',c:C.acai},{k:'cremes',n:'Cremes',c:C.berry},{k:'gelatos',n:'Gelato',c:C.lil}];
var linhas=cats.map(function(c){var meta=m.porCat[c.k],feito=m.prodMes[c.k],p=meta>0?Math.min(100,Math.round(feito/meta*100)):0;
var porDia=m.duRest>0?Math.round(Math.max(0,meta-feito)/m.duRest):0;
return '<div style="margin:8px 0"><div style="display:flex;justify-content:space-between;font-size:12.5px"><span><i style="display:inline-block;width:10px;height:10px;background:'+c.c+';border-radius:3px;margin-right:6px"></i><b>'+c.n+'</b> '+Math.round(m.propCat[c.k]*100)+'% · meta '+fmt(meta)+' L</span><span>produzido '+fmt(feito)+' / '+fmt(meta)+' L <b>('+p+'%)</b></span></div>'+
'<div style="background:#F1ECF6;border-radius:5px;height:11px;margin-top:3px;overflow:hidden"><i style="display:block;height:100%;background:'+c.c+';width:'+p+'%"></i></div>'+
'<div style="font-size:11px;color:var(--mut);margin-top:2px">precisa produzir <b>'+fmt(porDia)+' L/dia</b> nos próximos '+m.duRest+' dias úteis</div></div>';}).join('');
document.getElementById('metaBox').innerHTML='<div style="display:flex;gap:22px;align-items:center;flex-wrap:wrap">'+raio(pct)+
'<div style="flex:1;min-width:260px"><div style="display:flex;gap:20px;flex-wrap:wrap;font-size:12.5px">'+
'<div><div class="muted">Produzido no mês</div><div style="font-size:19px;font-weight:800">'+fmt(m.prodMesTot)+' L</div></div>'+
'<div><div class="muted">Falta produzir</div><div style="font-size:19px;font-weight:800;color:'+(m.faltaTot>0?'#C0392B':'var(--ok)')+'">'+fmt(m.faltaTot)+' L</div></div>'+
'<div><div class="muted">Ritmo diário total</div><div style="font-size:19px;font-weight:800">'+fmt(m.porDia)+' L/dia</div></div>'+
'<div><div class="muted">Por dia p/ bater</div><div style="font-size:19px;font-weight:800;color:var(--acai)">'+fmt(m.porDiaRest)+' L/dia</div></div>'+
'<div><div class="muted">Dias úteis restantes</div><div style="font-size:19px;font-weight:800">'+m.duRest+' / '+m.duTotal+'</div></div>'+
'</div><div style="background:#F1ECF6;border-radius:6px;height:13px;margin-top:11px;overflow:hidden"><i style="display:block;height:100%;background:linear-gradient(90deg,var(--acai),var(--berry));width:'+faltaBar+'%"></i></div>'+
'<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">'+linhas+'</div></div></div>';}

var CONF_KEY='confPolpa_'+(new Date().toISOString().slice(0,10));
function _loadConf(){try{return JSON.parse(localStorage.getItem(CONF_KEY)||'{}');}catch(e){return {};}}
function _pctColor(p){var a=Math.abs(p);return a<3?'var(--ok)':a<10?'var(--repor)':'var(--rup)';}
function rConf(){var pol=D.estoque.filter(x=>x.polpa&&x.polpaTipo=='aqui').sort((a,b)=>(b.eat||0)-(a.eat||0));
var salvo=_loadConf();document.getElementById('confDia',new Date().toLocaleDateString('pt-BR'));
document.getElementById('confDia').textContent=new Date().toLocaleDateString('pt-BR');
document.getElementById('tConf').innerHTML=pol.map(function(x){var v=salvo[x.ref];var dif=v!=null?v-x.eat:null;var pd=(v!=null&&x.eat>0)?(dif/x.eat*100):null;
return '<tr><td class="n">'+x.ref+'</td><td>'+x.produto+'</td><td class="n">'+fmt(x.eat,0)+'</td>'+
'<td style="background:#FAF5FE"><input type="number" step="0.1" data-ref="'+x.ref+'" value="'+(v!=null?v:'')+'" style="width:100%;padding:5px 8px;border:1px solid var(--line);border-radius:6px;font-family:inherit;font-size:13px;text-align:right" placeholder="—"></td>'+
'<td class="n" style="color:'+(dif==null?'var(--mut)':(dif<0?'var(--rup)':'var(--ok)'))+';font-weight:700">'+(dif==null?'—':(dif>0?'+':'')+fmt(dif,0))+'</td>'+
'<td class="n" style="color:'+(pd==null?'var(--mut)':_pctColor(pd))+';font-weight:700">'+(pd==null?'—':(pd>0?'+':'')+fmt(pd,1)+'%')+'</td></tr>';}).join('');
var preenchidos=Object.keys(salvo).length;
document.getElementById('confInfo').textContent=preenchidos?preenchidos+' polpa(s) contadas hoje':'preencha a contagem física';}
function salvarConf(){var obj={};document.querySelectorAll('#tConf input[data-ref]').forEach(function(i){var v=parseFloat(i.value.replace(',','.'));if(!isNaN(v))obj[i.getAttribute('data-ref')]=v;});
try{localStorage.setItem(CONF_KEY,JSON.stringify(obj));rConf();document.getElementById('confInfo').textContent='✓ salvo · '+Object.keys(obj).length+' polpa(s)';}
catch(e){alert('Não foi possível salvar: '+e);}}
function limparConf(){if(!confirm('Limpar contagem de hoje?'))return;try{localStorage.removeItem(CONF_KEY);}catch(e){}rConf();}
function whatsConf(){var pol=D.estoque.filter(x=>x.polpa&&x.polpaTipo=='aqui').sort((a,b)=>(b.eat||0)-(a.eat||0));
var salvo=_loadConf();var pad=function(s,n){s=String(s);while(s.length<n)s+=' ';return s;};
var linhas=[],tot=0,contadas=0,divTot=0;
pol.forEach(function(x){var v=salvo[x.ref];if(v==null)return;contadas++;var dif=v-x.eat;tot+=v;divTot+=Math.abs(dif);
var sinal=dif>0?'+':(dif<0?'':'');var nome=x.produto.length>26?x.produto.slice(0,26):x.produto;
linhas.push('• '+nome+'\n   Sistema '+fmt(x.eat,0)+' · Físico '+fmt(v,0)+' · Dif '+sinal+fmt(dif,0));});
if(!contadas){alert('Preencha e salve a contagem antes de compartilhar.');return;}
var dia=new Date().toLocaleDateString('pt-BR');
var txt='*📋 Conferência Física de Polpas*\n*Supremo Açaí* · '+dia+'\n\n'+linhas.join('\n\n')+
'\n\n*Resumo:* '+contadas+' polpa(s) contadas · Divergência total: '+fmt(divTot,0)+' un';
window.open('https://wa.me/?text='+encodeURIComponent(txt),'_blank');}

var custMes='',custF='',custExp={};
function setCust(v,el){custF=v;el.parentNode.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));el.classList.add('on');rCust();}
function setCustMes(m){custMes=m;custExp={};document.querySelectorAll('#custMeses .chip').forEach(c=>c.classList.toggle('on',c.dataset.m==m));rCust();}
function toggReceita(i){custExp[i]=!custExp[i];rCust();}
function rCust(){var c=D.custos;
if(!c||!c.mesesLista||!c.mesesLista.length){document.getElementById('custKpis').innerHTML='<div class="kpi"><div class="l">Sem dados</div><div class="v">—</div></div>';
document.getElementById('tCust').innerHTML='<tr><td colspan="7" class="muted" style="padding:14px">Aba "Produção com insumos" não encontrada ou vazia.</td></tr>';
document.getElementById('custMeses').innerHTML='';return;}
if(!custMes||c.mesesLista.indexOf(custMes)<0)custMes=c.mesesLista[c.mesesLista.length-1];
// chips de mês
document.getElementById('custMeses').innerHTML='<span class="lbl">Mês</span>'+c.mesesLista.map(m=>'<span class="chip'+(m==custMes?' on':'')+'" data-m="'+m+'" onclick="setCustMes(\''+m+'\')">'+m.slice(5)+'/'+m.slice(0,4)+'</span>').join('');
var M=c.meses[custMes],recs=M.receitas;
// KPIs do mês
var comRec=recs.filter(r=>r.temRec);
var desp=comRec.filter(r=>r.pct!=null&&r.pct>5);       // desperdício >5%
var custoDesp=desp.reduce((s,r)=>s+Math.max(0,r.custo*(r.pct/100)/(1+r.pct/100)),0);
var K=[['Custo real do mês','R$ '+fmt(M.custoTot,2),''],['Receitas produzidas',fmt(recs.length),''],
['Receitas desperdiçando &gt;5%',fmt(desp.length),desp.length?'al':''],['Perda estimada (R$)','R$ '+fmt(custoDesp,0),custoDesp>0?'al':'']];
document.getElementById('custKpis').innerHTML=K.map(k=>'<div class="kpi '+k[2]+'"><div class="l">'+k[0]+'</div><div class="v">'+k[1]+'</div></div>').join('');
// filtro
var q=(document.getElementById('qCust').value||'').toLowerCase();
var rr=recs.filter(function(r){
if(q&&r.receita.toLowerCase().indexOf(q)<0)return false;
if(custF=='desp')return r.temRec&&r.pct!=null&&r.pct>5;
if(custF=='semrec')return !r.temRec;
return true;});
// tabela: linha de receita (clicável) + linhas de insumo quando expandida
var html='';
rr.forEach(function(r,idx){
var cor=r.pct==null?'var(--mut)':(r.pct>15?'var(--rup)':r.pct>5?'#B7791F':r.pct<-5?'#2A6FB0':'var(--ok)');
var pcttxt=r.pct==null?'<span title="receita não cadastrada">s/ receita</span>':(r.pct>0?'+':'')+fmt(r.pct,1)+'%';
var seta=r.temRec?(custExp[idx]?'▾':'▸'):'';
html+='<tr style="cursor:'+(r.temRec?'pointer':'default')+';background:var(--card)" onclick="toggReceita('+idx+')">'+
'<td><b>'+seta+' '+r.receita+'</b><br><span style="font-size:10px;color:var(--mut)">'+fmt(r.prodUn)+' un produzidas</span></td>'+
'<td class="n">'+fmt(r.real,1)+'</td><td class="n">'+(r.temRec?fmt(r.teo,1):'—')+'</td>'+
'<td class="n" style="color:'+cor+';font-weight:700">'+(r.temRec?(r.dif>0?'+':'')+fmt(r.dif,1):'—')+'</td>'+
'<td class="n" style="color:'+cor+';font-weight:700">'+pcttxt+'</td><td class="n">'+brl(r.custo)+'</td></tr>';
if(custExp[idx]&&r.temRec){
r.insumos.forEach(function(i){
var ic=i.pct==null?'var(--mut)':(i.pct>15?'var(--rup)':i.pct>5?'#B7791F':i.pct<-5?'#2A6FB0':'var(--ok)');
var ipct=i.pct==null?'s/ receita':(i.pct>0?'+':'')+fmt(i.pct,1)+'%';
html+='<tr style="background:var(--bg)"><td style="padding-left:24px;font-size:12px">'+i.nome+'</td>'+
'<td class="n" style="font-size:12px">'+fmt(i.real,1)+'</td><td class="n" style="font-size:12px">'+(i.temTeo?fmt(i.teo,1):'—')+'</td>'+
'<td class="n" style="font-size:12px;color:'+ic+'">'+(i.temTeo?(i.dif>0?'+':'')+fmt(i.dif,1):'—')+'</td>'+
'<td class="n" style="font-size:12px;color:'+ic+';font-weight:600">'+ipct+'</td><td class="n" style="font-size:12px">'+brl(i.custo)+'</td></tr>';});}});
document.getElementById('tCust').innerHTML=html||'<tr><td colspan="6" class="muted" style="padding:14px">Nenhuma receita neste filtro.</td></tr>';
document.getElementById('cCust').innerHTML='Clique numa receita para ver os insumos · <b style="color:var(--rup)">+% real&gt;teórico</b>=consumiu mais (desperdício) · <b style="color:#2A6FB0">−% real&lt;teórico</b>=consumiu menos · <b>s/ receita</b>=produto sem ficha técnica cadastrada';}

try{rCockpit();rGeral();rCmp();rEst();rPlano();rConf();rPolpa();rMeta();rVisao();rDemGraf();rDem();rCust();initPV();}catch(e){
document.body.insertAdjacentHTML('afterbegin','<pre style="color:#C0392B;padding:12px">Erro: '+e+'</pre>');}
</script></body></html>"""


def construir(cli):
    """Lê as planilhas via cliente gspread e devolve o HTML do painel (ou None)."""
    pl_e = cli.open_by_key(ID_ESTOQUE)
    parsers = {"abc": parse_abc, "altos": parse_altos, "valor": parse_valor, "peso": parse_peso}
    # VendasPeso removida do fluxo: não lemos mais a aba 'peso'. Densidade fixa (GRUPO_DENS).
    tabs = {t: parsers[t](fetch_aba(pl_e, nome))
            for t, nome in TAB_NAMES.items() if t != "peso"}
    master = build_master(tabs)
    peso_raw = {}
    peso_un = {}                       # sem peso medido → densidade padrão por categoria
    vend = ler_vendas(pl_e)
    if vend is None:
        return None
    mensal, ultima, diario, nomes = vend
    # O nome da Vendas vem completo; o da AltosEBaixos vem cortado. Vendas manda.
    for ref, nm in nomes.items():
        if ref in master and nm and str(nm).strip():
            master[ref]["prod"] = str(nm).strip()
    pl_r = cli.open_by_key(ID_RECEITAS)
    rec_por_aba = {c["aba"]: parse_aba_limpa(fetch_aba(pl_r, c["aba"]), master) for c in REC_ABAS}
    recidx = {_norm(r["titulo"]): r for recs in rec_por_aba.values() for r in recs}
    meses_all = sorted({m for (_, m) in mensal.index})
    comp = [m for m in meses_all if m < date.today().strftime("%Y-%m")]
    jan, jabc = comp[-MESES_DMD:], comp[-MESES_ABC:]
    prox = (date.today().month % 12) + 1
    plano, insumos, cat, info, mrp_consumo = calcular_mrp(
        master, mensal, jan, jabc, prox, recidx, rec_por_aba, peso_un)
    estoque = calcular_estoque(master, mensal, ultima, jan, jabc, prox, mrp_consumo)
    amanha, cap_usada = planejar_amanha(plano)
    pesoval = []
    for r, e in peso_raw.items():
        if r in info and e.get("qtd") and e.get("peso"):
            it = info[r]
            ku = e["peso"] / e["qtd"]
            pesoval.append({"nome": it["nome"], "ref": r, "qtd": round(e["qtd"]),
                            "peso": round(e["peso"], 1), "kgun": round(ku, 2),
                            "litros": round(it["litros"], 2),
                            "lkg": round(it["litros"] / ku, 3) if ku else None,
                            "densAss": GRUPO_DENS[it["grupo"]]})
    pesoval.sort(key=lambda x: -x["qtd"])
    dfp = ler_producao(pl_e)
    _hoje = date.today()
    _frac = _hoje.day / calendar.monthrange(_hoje.year, _hoje.month)[1]
    _dem = {x["ref"]: x["dem"] for x in estoque}
    _fsz = {x["ref"]: x["fsz"] for x in estoque}
    litros_map, _densmed = modelo_litros(master, peso_un)
    try:
        reproc = ler_reprocesso(pl_e, master)
    except Exception as _e:
        print(f"[aviso] reprocesso falhou: {_e}")
        reproc = []
    try:
        custos = analise_real_vs_teorico(pl_e, master, rec_por_aba)
    except Exception as _e:
        import traceback as _tb
        print(f"[aviso] análise de custos falhou: {_e}")
        _tb.print_exc()
        custos = None
    pv = (comparar_prod_venda(dfp, info, master, mensal, _dem, _fsz, _frac,
                              _hoje.strftime("%Y-%m"), litros_map)
          if (dfp is not None and len(dfp)) else None)
    return montar(estoque, plano, insumos, cat, info, master, mensal, meses_all,
                  amanha, cap_usada, diario, pv, pesoval, litros_map, peso_un, reproc, custos)


def main():
    cli = gspread.authorize(Credentials.from_service_account_file(CRED, scopes=ESCOPOS))
    print("Carregando estoque, vendas e receitas...")
    html = construir(cli)
    if html is None:
        return
    caminho = os.path.join(PASTA, "dashboard_supremo.html")
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Dashboard salvo: {caminho}  → abra no navegador (offline)")


if __name__ == "__main__":
    main()
