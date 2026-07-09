"""
logic.py — Lógica de negócio do dashboard Supremo Açaí
=======================================================
Porte fiel das funções JavaScript para Python:
  • Constantes (URLs, refs de produção/polpas, estoque mínimo, sazonalidade)
  • Parsers das 4 abas de estoque (ABC, Altos, Valor, Peso)
  • build_master (cruzamento das fontes)
  • Parser de receitas (col A=ref, B=nome, C=1 batida, D=%, E-H=embalagens, I=consumo)
  • compute_producao (39 produtos, status Para Amanhã/Semana/Monitorar/Sazonal)
  • build_indicacao_plan (plano de batidas por capacidade/dia)
"""
import io
import csv
import math
import unicodedata
import requests

# ──────────────────────────────────────────────────────────────────
# CONSTANTES (extraídas do dashboard v18)
# ──────────────────────────────────────────────────────────────────
SCRIPT_URL = "https://script.google.com/macros/s/AKfycbyzEE5eKnEcxVbZSF5ZVcCCrOed7lI80xBTWdC7eQERrA2mfKg5Sa1q6sqaUXLye7em/exec"
REC_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbyWl1of5tUj_joeNIeNZvkYD2PBabpno2T0L6PQUPz57aPftV2epdxJhyTNTzo-3gA/exec"

TAB_NAMES = {"abc": "ConsumoABC", "altos": "AltosEBaixos",
             "valor": "ValorEstoque", "peso": "VendasPeso"}
TAB_LABELS = {"abc": "Consumo ABC", "altos": "Altos e Baixos",
              "valor": "Valor Estoque", "peso": "Vendas Peso"}

LT_POLPA = 60
POLPA_REFS = [568, 787, 879, 851, 742, 805, 677]

PROD_REFS = [1315, 1317, 1360, 1359, 1311, 903, 1379, 1380, 1355, 1300, 519, 553, 1322,
             536, 1351, 1354, 701, 747, 1356, 627, 925, 861, 528, 1348, 1353, 1349, 1352, 869,
             1323, 1358, 1459, 1305, 1331, 1347, 1325, 255, 588, 1306, 270]

PROD_EMN = {
    1315: 19, 1317: 63, 1360: 78, 1359: 10, 1311: 36, 903: 34, 1379: 7, 1380: 15,
    1355: 266, 1300: 39, 519: 34, 553: 14, 1322: 10, 536: 21, 1351: 23, 1354: 26,
    701: 41, 747: 8, 1356: 33, 627: 41, 925: 47, 861: 35, 528: 40, 1348: 92,
    1353: 103, 1349: 78, 1352: 29, 869: 30, 1323: 70, 1358: 130, 1459: 130, 1305: 215,
    1331: 274, 1347: 150, 1325: 5, 255: 102, 588: 70, 1306: 325, 270: 80,
}

PROD_SAZONAIS = {869: "Páscoa (mar–abr)"}

PROD_GRUPO = {
    1315: "cremes", 1317: "cremes", 1360: "cremes", 1359: "cremes", 1311: "cremes",
    903: "cremes", 1379: "cremes", 1380: "cremes", 1355: "cremes", 1300: "cremes",
    519: "cremes", 553: "cremes", 1322: "cremes",
    536: "gelatos", 1351: "gelatos", 1354: "gelatos", 701: "gelatos", 747: "gelatos",
    1356: "gelatos", 627: "gelatos", 925: "gelatos", 861: "gelatos", 528: "gelatos",
    1348: "gelatos", 1353: "gelatos", 1349: "gelatos", 1352: "gelatos", 869: "gelatos",
    1323: "mix", 1358: "mix", 1459: "mix", 1305: "mix", 1331: "mix", 1347: "mix",
    1325: "mix", 255: "mix", 588: "mix", 1306: "mix", 270: "mix",
}

PROD_GROUP_NAMES = {"cremes": "🍦 Cremes", "gelatos": "🍨 Gelatos", "mix": "🫐 Mix de Açaí"}

REC_ABAS = [
    {"aba": "Açai", "icon": "🫐", "label": "Açaí"},
    {"aba": "Gelatos", "icon": "🍨", "label": "Gelatos"},
    {"aba": "Cremes", "icon": "🍦", "label": "Cremes"},
]


# ──────────────────────────────────────────────────────────────────
# HELPERS de formatação
# ──────────────────────────────────────────────────────────────────
def brl(v):
    try:
        return "R$ " + f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "R$ 0,00"


def n0(v):
    try:
        return f"{round(float(v)):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def n1(v):
    try:
        return f"{float(v):,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "0,0"


def parse_brl(s):
    """Converte texto 'R$ 1.234,56' em float (igual ao parseBRL do JS)."""
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _num(v):
    """Tenta converter célula para número; retorna None se não for."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(".", "").replace(",", ".") if _looks_brl(v) else str(v).strip().replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _looks_brl(v):
    s = str(v)
    return "." in s and "," in s and s.rfind(",") > s.rfind(".")


# ──────────────────────────────────────────────────────────────────
# FETCH — busca CSV do Apps Script
# ──────────────────────────────────────────────────────────────────
def fetch_sheet_csv(base_url, sheet_name):
    """
    Busca uma aba do Apps Script e devolve lista de linhas (lista de listas).
    O Apps Script devolve CSV (ou TSV). Tentamos detectar o separador.
    """
    url = f"{base_url}?sheet={requests.utils.quote(sheet_name)}"
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    text = resp.text
    if text.lstrip().startswith("<"):
        raise RuntimeError("Script não autorizado — republique como 'Qualquer pessoa'")
    if text.lstrip().startswith("{"):
        raise RuntimeError("Apps Script retornou erro JSON")
    # Detectar separador
    sample = text[:2000]
    sep = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=sep)
    rows = []
    for raw in reader:
        # converter números quando possível, manter string caso contrário
        row = []
        for cell in raw:
            num = _num(cell)
            row.append(num if num is not None else (cell.strip() if cell else None))
        rows.append(row)
    return rows


# ──────────────────────────────────────────────────────────────────
# PARSERS de estoque
# ──────────────────────────────────────────────────────────────────
def _parse_two_line(rows, extra_fn):
    """Base para ABC e Peso (estrutura de 2 linhas por produto)."""
    data = {}
    r = 0
    # pular até linha com 'referenc'
    while r < len(rows):
        row = rows[r]
        if row and any(v and "referenc" in str(v).lower() for v in row):
            break
        r += 1
    r += 1
    # pular até primeira linha com ref numérica
    while r < len(rows) and not (rows[r] and any(
            isinstance(v, float) and 0 < v < 99999 for v in rows[r])):
        r += 1
    while r < len(rows) - 1:
        pr = rows[r]
        vr = rows[r + 1] if r + 1 < len(rows) else None
        if not pr:
            r += 2
            continue
        if any(v and str(v).strip() == "Total:" for v in pr):
            break
        ref = None
        for v in pr:
            if isinstance(v, float) and 0 < v < 99999:
                ref = v
                break
        if not ref:
            r += 2
            continue
        prod = ""
        for v in pr[1:]:
            if isinstance(v, str) and len(v) > 3:
                prod = v.strip()
                break
        rec, cls = 0.0, "C"
        if vr:
            for v in vr:
                if v and "R$" in str(v):
                    rec = parse_brl(v)
                    break
            for v in reversed(vr):
                if v and str(v).strip() in ("A", "B", "C"):
                    cls = str(v).strip()
                    break
        if prod:
            entry = {"prod": prod, "rec": rec, "cls": cls}
            extra_fn(pr, vr, entry)
            data[round(ref)] = entry
        r += 2
    return data


def parse_abc(rows):
    def extra(pr, vr, e):
        q = 0
        for v in pr[1:]:
            if isinstance(v, float) and 0 < v < 1000000:
                q = v
                break
        e["qtd"] = round(q)
    return _parse_two_line(rows, extra)


def parse_peso(rows):
    def extra(pr, vr, e):
        nums = [v for v in pr if isinstance(v, float) and 0 < v < 1000000]
        e["qtd"] = round(nums[0]) if nums else 0
        e["peso"] = nums[1] if len(nums) > 1 else 0
    return _parse_two_line(rows, extra)


def parse_altos(rows):
    """col 0=ref, 3/4=prod, 6=cst, 7=vnd, 12=emn, 15=eat, 17=dif."""
    data = {}
    cg = ""
    for row in rows:
        if not row:
            continue
        if (len(row) > 1 and isinstance(row[1], str) and "-" in row[1]
                and not row[0]):
            cg = row[1].strip()
        if row[0] and isinstance(row[0], float) and row[0] > 0:
            ref = round(row[0])
            def g(i):
                return row[i] if (len(row) > i and isinstance(row[i], float)) else 0
            data[ref] = {
                "grp": cg,
                "prod": str(row[3] or (row[4] if len(row) > 4 else "") or "").strip(),
                "cst": g(6), "vnd": g(7), "emn": g(12), "eat": g(15), "dif": g(17),
            }
    return data


def parse_valor(rows):
    """col 1=ref, 7=qtd, 11=cst(R$ texto), 20=sub."""
    data = {}
    cg = "Geral"
    for row in rows:
        if not row:
            continue
        c1 = row[0] if len(row) > 0 else None
        c2 = row[1] if len(row) > 1 else None
        if (c1 and isinstance(c1, str) and not c2 and c1 != "Grupo"
                and "total" not in c1.lower() and "refer" not in c1.lower()
                and len(c1) > 3):
            cg = c1.strip()
        if c2 and isinstance(c2, float) and c2 > 0:
            ref = round(c2)
            if ref not in data:
                data[ref] = {
                    "grp": cg,
                    "prod": str(row[3] if len(row) > 3 else "" or "").strip(),
                    "qtd": row[7] if (len(row) > 7 and isinstance(row[7], float)) else 0,
                    "cst": parse_brl(row[11] if len(row) > 11 else None),
                    "sub": parse_brl(row[20] if len(row) > 20 else None),
                }
    return data


def build_master(S):
    """Cruza as 4 fontes em um único dicionário ref → produto."""
    master = {}
    all_refs = set()
    for key in ("abc", "altos", "valor"):
        all_refs.update(S.get(key, {}).keys())
    for ref in all_refs:
        a = S.get("abc", {}).get(ref, {})
        al = S.get("altos", {}).get(ref, {})
        v = S.get("valor", {}).get(ref, {})
        p = S.get("peso", {}).get(ref, {})
        master[ref] = {
            "ref": ref,
            "prod": a.get("prod") or v.get("prod") or al.get("prod") or p.get("prod") or str(ref),
            "grp": v.get("grp") or al.get("grp") or "—",
            "abc": a.get("cls", "-"),
            "qtdVnd": a.get("qtd", 0), "rec": a.get("rec", 0),
            "cst": al.get("cst") or v.get("cst") or 0,
            "vnd": al.get("vnd", 0),
            "emn": al.get("emn", 0),
            "eat": v.get("qtd") or al.get("eat") or 0,
            "sub": v.get("sub", 0),
            "peso": p.get("peso", 0),
        }
    return master


def status_of(cob, lt, ss):
    if cob is None:
        return "sem"
    if cob < lt:
        return "urgente"
    if cob < lt + ss:
        return "planejar"
    return "ok"


# ──────────────────────────────────────────────────────────────────
# PARSER de receitas
# ──────────────────────────────────────────────────────────────────
def parse_receita_aba(rows, master):
    """
    Estrutura por aba:
      A=ref insumo · B=nome · C=1 batida · D=% · E-H=embalagens · I=consumo/batidas
      Linha título: A vazia, B=nome receita, I=batidas mín
      Linha TOTAL: rendimentos nas cols E-H
    """
    receitas = []
    cur = None
    for row in rows:
        if not row or not any(v not in (None, "") for v in row):
            continue
        colA = row[0] if len(row) > 0 else None
        colB = row[1] if len(row) > 1 else None
        colC = row[2] if len(row) > 2 else None
        colI = row[8] if len(row) > 8 else None

        if colB is None and colA is None:
            continue
        nome_b = str(colB).strip() if colB else ""
        if not nome_b:
            continue

        # FIM: linha TOTAL
        if "total" in nome_b.lower():
            if cur:
                for ci in range(4, 8):
                    v = row[ci] if len(row) > ci else None
                    num = v if isinstance(v, (int, float)) else 0
                    if num and num > 0:
                        cur["embalagens"][ci - 4]["rend"] = num
                        cur["embalagens"][ci - 4]["rendPB"] = num / cur["batidasMin"]
                receitas.append(cur)
                cur = None
            continue

        ref_num = round(colA) if isinstance(colA, (int, float)) and colA > 0 else 0
        has_qtd_c = isinstance(colC, (int, float))
        is_ing = ref_num > 0 or (cur is not None and has_qtd_c)
        is_titulo = (not is_ing and len(nome_b) > 1
                     and not any(k in nome_b.lower() for k in
                                 ["ref", "ingrediente", "produto", "nome", "insumo", "batida"]))

        if is_titulo:
            if cur:
                receitas.append(cur)
            bat = round(colI) if isinstance(colI, (int, float)) and colI > 0 else 1
            embalagens = []
            for ci in range(4, 8):
                v = row[ci] if len(row) > ci else None
                if v is None or v == "":
                    label = ""
                elif isinstance(v, (int, float)):
                    label = str(v).replace(".", ",")
                else:
                    label = str(v).strip()
                embalagens.append({"idx": ci - 4, "label": label, "rend": 0, "rendPB": 0})
            cur = {"titulo": nome_b, "batidasMin": bat, "embalagens": embalagens,
                   "ings": [], "grp": _rec_grupo(nome_b)}
            continue

        if cur is not None:
            q1b = colC if isinstance(colC, (int, float)) else 0
            pct_d = row[3] if (len(row) > 3 and isinstance(row[3], (int, float))) else None
            q_emb = []
            for ci in range(4, 8):
                v = row[ci] if len(row) > ci else None
                q_emb.append(v if isinstance(v, (int, float)) else 0)
            qI = colI if isinstance(colI, (int, float)) else 0
            nome_exib = (master[ref_num]["prod"]
                         if ref_num > 0 and ref_num in master else nome_b)
            cur["ings"].append({
                "ref": ref_num, "nomeOrig": nome_b, "nomeExib": nome_exib,
                "temRef": ref_num > 0,
                "vinculado": ref_num > 0 and ref_num in master,
                "q1b": q1b, "pct": pct_d, "qEmb": q_emb, "qI": qI,
            })
    if cur:
        receitas.append(cur)
    return receitas


def _rec_grupo(titulo):
    t = titulo.lower()
    if "creme" in t:
        return "cremes"
    if "gelato" in t:
        return "gelatos"
    if "mix" in t or "aça" in t or "acai" in t:
        return "mix"
    return ""


# ──────────────────────────────────────────────────────────────────
# PRODUÇÃO — 39 produtos, status e sugestões
# ──────────────────────────────────────────────────────────────────
def compute_producao(master, S, periodo):
    rows = []
    for ref in PROD_REFS:
        al = S.get("altos", {}).get(ref, {})
        ab = S.get("abc", {}).get(ref, {})
        m = master.get(ref, {})

        prod = (al.get("prod") or m.get("prod") or f"Ref {ref}")
        cls = ab.get("cls") or m.get("abc") or "-"
        qtd_vnd = ab.get("qtd", 0)

        al_emn = al.get("emn", 0)
        eat = al.get("eat", 0) if al_emn > 0 else m.get("eat", 0)
        emn = PROD_EMN.get(ref, al_emn or m.get("emn", 0))
        dif = max(0, emn - eat)

        dmd = qtd_vnd / (periodo * 30) if qtd_vnd > 0 else 0
        cob = (eat / dmd) if dmd > 0 else None

        is_sazonal = ref in PROD_SAZONAIS
        sem_hist = qtd_vnd == 0 and not is_sazonal

        sug_amanha = math.ceil(max(dif, dmd * 2)) if dmd > 0 else math.ceil(dif)
        sug_semana = math.ceil(max(0, dmd * 7 - eat)) if dmd > 0 else math.ceil(dif)

        if is_sazonal:
            status = "sazonal"
        elif dmd > 0 and cob is not None and cob < 2:
            status = "amanha"
        elif dmd > 0 and cob is not None and cob < 7:
            status = "semana"
        elif sem_hist and dif > 0:
            status = "sem_hist"
        else:
            status = "monitorar"

        rows.append({
            "ref": ref, "prod": str(prod).strip(), "cls": cls, "qtdVnd": qtd_vnd,
            "eat": eat, "emn": emn, "dif": dif, "dmd": dmd, "cob": cob,
            "sug_amanha": sug_amanha, "sug_semana": sug_semana,
            "status": status, "is_sazonal": is_sazonal, "sem_hist": sem_hist,
            "grp": PROD_GRUPO.get(ref, "cremes"),
        })

    # ordenar por prioridade
    order = {"amanha": 0, "semana": 1, "sem_hist": 2, "monitorar": 3, "sazonal": 4}
    abc_w = {"A": 0, "B": 1, "C": 2, "-": 3}

    def score(r):
        cob_v = r["cob"] if r["cob"] is not None else 999
        return (order.get(r["status"], 4) * 1000 + cob_v * 5
                - abc_w.get(r["cls"], 3) * 200 - r["dif"] * 0.5)

    rows.sort(key=score)
    return rows


# ──────────────────────────────────────────────────────────────────
# INDICAÇÃO — plano de batidas (capacidade/dia)
# ──────────────────────────────────────────────────────────────────
def _norm(s):
    s = unicodedata.normalize("NFD", str(s or "").upper())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join("".join(c if c.isalnum() or c == " " else " " for c in s).split())


def _find_receita(prod_name, rec_data):
    pn = _norm(prod_name)
    best = None
    for aba, recs in rec_data.items():
        for rec in recs:
            tn = _norm(rec["titulo"])
            if len(tn) >= 4 and tn in pn:
                if best is None or len(tn) > len(best[2]):
                    best = (aba, rec, tn)
    return best


def _escolher_emb(rec, prod_name):
    pn = _norm(prod_name).replace(",", "")
    validas = [e for e in rec["embalagens"] if e["rendPB"] > 0]
    if not validas:
        return None
    for e in validas:
        ln = _norm(e["label"]).replace(",", "")
        if ln and ln in pn:
            return e
    return max(validas, key=lambda e: e["rendPB"])


def build_indicacao_plan(prod_rows, rec_data, cap):
    """Aloca batidas por prioridade até esgotar a capacidade do dia."""
    if not any(rec_data.values()):
        return [], cap, []
    restante = cap
    plano = []
    sem_receita = []
    for r in prod_rows:
        if r["is_sazonal"]:
            continue
        necessidade = (r["sug_amanha"] if r["status"] == "amanha"
                       else r["sug_semana"] if r["status"] == "semana" else 0)
        if necessidade <= 0:
            continue
        m = _find_receita(r["prod"], rec_data)
        if not m:
            if r["status"] == "amanha":
                sem_receita.append(r)
            continue
        aba, rec, _ = m
        emb = _escolher_emb(rec, r["prod"])
        if not emb:
            if r["status"] == "amanha":
                sem_receita.append(r)
            continue
        bmin = rec["batidasMin"]
        bat_nec = math.ceil((necessidade / emb["rendPB"]) / bmin) * bmin
        bat_aloc = min(bat_nec, (restante // bmin) * bmin)
        if bat_aloc <= 0:
            continue
        restante -= bat_aloc
        plano.append({
            "prod": r["prod"], "status": r["status"], "receita": rec["titulo"],
            "aba": aba, "batidas": bat_aloc, "bat_nec": bat_nec, "bMin": bmin,
            "emb": emb["label"], "rendimento": bat_aloc * emb["rendPB"],
            "necessidade": necessidade,
        })
        if restante <= 0:
            break
    return plano, restante, sem_receita
