import os
import io
import json
import datetime as dt
import re
from typing import Optional, Dict, Tuple, List

import pandas as pd
import streamlit as st

# =========================================================
# CONFIGURAÇÕES (NOMES DAS COLUNAS)
# =========================================================
# Planilha "Visão Cliente" (C6)
COL_CNPJ = "CD_CPF_CNPJ_CLIENTE"
COL_ABERTURA = "DT_CONTA_CRIADA"
COL_FUNDACAO = "DT_FUNDACAO_EMPRESA"
COL_PIX = "CHAVES_PIX_FORTE"
COL_SALDO = "VL_SALDO_MEDIO_MENSALIZADO"
COL_STATUS = "STATUS_CC"
COL_DOMICILIO = "BANCO_DOMICILIO"
COL_BY = "FL_QUALIFICADO_COMISS"
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"

# BR (M0/M1/M2)
COL_BR = "MES_REF_COMISS"

# Planilha "Leads" (cadastros) — coluna M vira DATA_CADASTRO
COL_LEADS_DATA = "DATA_CADASTRO"

# Conversão (Azul >= 20%, Vermelho < 20%)
ALVO_CONVERSAO = 0.20

# Histórico diário a partir de:
HIST_START = dt.date(2026, 1, 1)

# =========================================================
# REGRAS DE REMUNERAÇÃO (POR FAIXA - conforme você informou)
# =========================================================
FAIXAS = [
    (0,   "Até 49 (1.0)",   {1: 140.00, 2: 230.00, 3: 400.00, 4: 540.00}),
    (50,  "50+ (1.1)",      {1: 154.00, 2: 253.00, 3: 440.00, 4: 594.00}),
    (150, "150+ (1.25)",    {1: 175.00, 2: 287.50, 3: 500.00, 4: 675.00}),
    (350, "350+ (1.5)",     {1: 210.00, 2: 345.00, 3: 600.00, 4: 810.00}),
]

# =========================================================
# ARQUIVOS DE MEMÓRIA
# =========================================================
DATA_DIR = "data_store"
os.makedirs(DATA_DIR, exist_ok=True)

HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_aberturas_diario.json")     # {dd/mm/aaaa: qtd}
HIST_LEADS_DAILY = os.path.join(DATA_DIR, "hist_cadastros_diario.json")    # {dd/mm/aaaa: qtd}

# baseline incremental: "maior valor já pago por CNPJ"
HIST_PAGO_POR_CNPJ = os.path.join(DATA_DIR, "pago_max_por_cnpj.json")      # {"CNPJ": valor}

# resumo mensal consolidado (cards e comparativo)
HIST_RESUMO_MENSAL = os.path.join(DATA_DIR, "resumo_mensal.json")          # {"mm/aaaa": {...}}

# memória do mês (a partir dos diários): guarda maior nível por CNPJ no mês
HIST_MONTH_LEVELS = os.path.join(DATA_DIR, "month_levels.json")            # {"mm/aaaa": {"cnpj": nivel}}

# para "fechar mês anterior automaticamente"
HIST_LAST_MONTH_SEEN = os.path.join(DATA_DIR, "last_month_seen.json")      # {"last_month": "mm/aaaa"}


# =========================================================
# HELPERS
# =========================================================
def read_excel_any(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")

def safe_json_load(path: str, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def safe_json_save(path: str, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def br_money(v: float) -> str:
    s = f"{float(v):,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"

def br_int(n: int) -> str:
    return f"{int(n):,}".replace(",", ".")

def fmt_date(d: dt.date) -> str:
    return d.strftime("%d/%m/%Y")

def fmt_month(d: dt.date) -> str:
    return d.strftime("%m/%Y")

def month_to_key(m: str) -> int:
    # "mm/aaaa" -> aaaamm
    try:
        mm, aa = m.split("/")
        return int(aa) * 100 + int(mm)
    except Exception:
        return 0

def to_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.date

def normalize_str(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").str.strip()

def contains_c6(x) -> bool:
    if x is None or pd.isna(x):
        return False
    return "c6" in str(x).lower()


# =========================================================
# QUALIFICAÇÃO (NÍVEL VENCEDOR)
# =========================================================
def parse_level_from_criterios(txt: str) -> int:
    """
    Ex.: "CASH IN: 3 | DOMICILIO: 0 | SALDO MEDIO: 4 | ..."
    Regra: considerar SOMENTE o MAIOR valor (vencedor).
    """
    if not isinstance(txt, str) or not txt.strip():
        return 0
    nums = []
    for n in re.findall(r":\s*(\d+)", txt):
        try:
            nums.append(int(n))
        except Exception:
            pass
    if not nums:
        return 0
    m = max(nums)
    if m < 1:
        return 0
    return min(m, 4)

def parse_level(df: pd.DataFrame) -> pd.Series:
    """
    Regra robusta:
    - Se BY for só 0/1 (flag), NÃO define nível -> nível vem do CRIT.
    - Se BY trouxer 2/3/4, usa BY quando for 1..4; senão CRIT.
    """
    by_raw = df.get(COL_BY, pd.Series([None] * len(df)))
    by_num = pd.to_numeric(by_raw, errors="coerce")

    by_vals = by_num.dropna().astype(int)
    by_is_flag = False
    if len(by_vals) > 0:
        uniq = set(by_vals.unique().tolist())
        by_is_flag = uniq.issubset({0, 1})

    crit_raw = df.get(COL_CRIT, pd.Series([""] * len(df))).astype("string").fillna("")
    level_crit = crit_raw.apply(parse_level_from_criterios).astype(int)

    if by_is_flag:
        level = level_crit
    else:
        level_by = by_num.fillna(0).astype(int)
        level_by = level_by.where(level_by.between(1, 4), 0)
        level = level_by.where(level_by > 0, level_crit)

    level = level.fillna(0).astype(int)
    level = level.where(level.between(1, 4), 0)
    return level

def criterio_vencedor(txt: str) -> str:
    if not isinstance(txt, str) or not txt.strip():
        return ""
    parts = [p.strip() for p in txt.split("|")]
    best = ("", 0)
    for p in parts:
        m = re.search(r"(.+):\s*(\d+)", p)
        if m:
            nome = m.group(1).strip()
            val = int(m.group(2))
            if val > best[1]:
                best = (nome, val)
    if best[1] <= 0:
        return ""
    return f"{best[0]} ({best[1]})"


# =========================================================
# PIX
# =========================================================
def pix_summary(df: pd.DataFrame) -> Tuple[int, int, pd.DataFrame]:
    s = normalize_str(df.get(COL_PIX, pd.Series([""] * len(df)))).str.upper()
    s = s.str.replace("'", "", regex=False)
    has_pix = ~s.isin(["", "-", "NAN", "NONE", "SEM", "SEM PIX"])
    com = int(has_pix.sum())
    sem = int((~has_pix).sum())
    por_chave = (
        s[has_pix]
        .value_counts()
        .rename_axis("Chave Pix")
        .reset_index(name="Quantidade")
    )
    return com, sem, por_chave


# =========================================================
# HISTÓRICO DIÁRIO
# =========================================================
def upsert_daily_hist(path: str, date_key: str, qty: int):
    base = safe_json_load(path, default={})
    base[date_key] = int(qty)  # substitui pelo último envio daquele dia
    safe_json_save(path, base)

def hist_to_df(path: str, colname: str) -> pd.DataFrame:
    d = safe_json_load(path, default={})
    rows = []
    for k, v in d.items():
        try:
            dd = dt.datetime.strptime(k, "%d/%m/%Y").date()
        except Exception:
            continue
        if dd < HIST_START:
            continue
        rows.append((dd, int(v)))
    rows.sort(key=lambda x: x[0])
    return pd.DataFrame(rows, columns=["Data", colname])


# =========================================================
# REMUNERAÇÃO (FAIXA POR QTD QUALIFICADAS)
# =========================================================
def faixa_por_qtd(qtd_qualificadas: int) -> Tuple[str, Dict[int, float]]:
    chosen_name, chosen_tbl = FAIXAS[0][1], FAIXAS[0][2]
    for min_q, nm, tbl in FAIXAS:
        if qtd_qualificadas >= min_q:
            chosen_name, chosen_tbl = nm, tbl
    return chosen_name, chosen_tbl


# =========================================================
# ✅ MÊS via DIÁRIO: guarda maior nível por CNPJ no mês
# =========================================================
def update_month_levels_from_daily(df_c6: pd.DataFrame, ref_date: dt.date):
    month_key = fmt_month(dt.date(ref_date.year, ref_date.month, 1))
    store = safe_json_load(HIST_MONTH_LEVELS, default={})
    month_map: Dict[str, int] = store.get(month_key, {})

    # CNPJ
    if COL_CNPJ not in df_c6.columns:
        cand = [c for c in df_c6.columns if "CNPJ" in str(c).upper()]
        df_c6[COL_CNPJ] = df_c6[cand[0]] if cand else ""

    df_c6["_cnpj"] = normalize_str(df_c6[COL_CNPJ]).str.replace(r"\D", "", regex=True)
    df_c6["_nivel"] = parse_level(df_c6)

    q = df_c6[(df_c6["_nivel"] >= 1) & (df_c6["_cnpj"] != "")].copy()
    if q.empty:
        store[month_key] = month_map
        safe_json_save(HIST_MONTH_LEVELS, store)
        return

    by_cnpj = q.groupby("_cnpj")["_nivel"].max().reset_index()

    for _, r in by_cnpj.iterrows():
        c = str(r["_cnpj"])
        lvl = int(r["_nivel"])
        prev = int(month_map.get(c, 0))
        if lvl > prev:
            month_map[c] = lvl

    store[month_key] = month_map
    safe_json_save(HIST_MONTH_LEVELS, store)


# =========================================================
# ✅ FECHAMENTO AUTOMÁTICO DO MÊS ANTERIOR
# Quando entrar num novo mês, o mês anterior vira baseline pago.
# =========================================================
def close_previous_month_if_needed(current_month_key: str):
    state = safe_json_load(HIST_LAST_MONTH_SEEN, default={"last_month": ""})
    last = state.get("last_month", "")

    # primeira vez
    if not last:
        state["last_month"] = current_month_key
        safe_json_save(HIST_LAST_MONTH_SEEN, state)
        return

    # se mudou o mês, fecha o last
    if month_to_key(current_month_key) > month_to_key(last):
        # fecha "last" -> atualiza paid_max por CNPJ com o valor cheio daquele mês
        store = safe_json_load(HIST_MONTH_LEVELS, default={})
        month_map = store.get(last, {})

        if month_map:
            paid_max = safe_json_load(HIST_PAGO_POR_CNPJ, default={})

            qtd_qual = len(month_map)
            _, precos = faixa_por_qtd(qtd_qual)

            for cnpj, nivel in month_map.items():
                cheio = float(precos.get(int(nivel), 0.0))
                prev = float(paid_max.get(cnpj, 0.0))
                # baseline vira "maior recebido até então"
                paid_max[cnpj] = max(prev, cheio)

            safe_json_save(HIST_PAGO_POR_CNPJ, paid_max)

        # atualiza estado
        state["last_month"] = current_month_key
        safe_json_save(HIST_LAST_MONTH_SEEN, state)


# =========================================================
# ✅ REMUNERAÇÃO DO MÊS ATUAL (incremental vs baseline pago)
# =========================================================
def compute_incremental_for_month(month_key: str) -> Optional[dict]:
    store = safe_json_load(HIST_MONTH_LEVELS, default={})
    month_map: Dict[str, int] = store.get(month_key, {})
    if not month_map:
        return None

    paid_max = safe_json_load(HIST_PAGO_POR_CNPJ, default={})
    resumo_mensal = safe_json_load(HIST_RESUMO_MENSAL, default={})

    qtd_qual = len(month_map)
    faixa_nome, precos = faixa_por_qtd(qtd_qual)

    total_cheio = 0.0
    total_receber = 0.0

    # incremental puro por CNPJ: max(mês atual - pago_max, 0)
    for cnpj, nivel in month_map.items():
        cheio = float(precos.get(int(nivel), 0.0))
        prev_max = float(paid_max.get(cnpj, 0.0))
        diff = cheio - prev_max
        if diff < 0:
            diff = 0.0
        total_cheio += cheio
        total_receber += diff

    total_japago_ref = total_cheio - total_receber

    resumo_mensal[month_key] = {
        "arquivo": "Acumulado diário (Jan/26 em diante)",
        "faixa": faixa_nome,
        "qualificadas": qtd_qual,
        "deveria_receber": float(total_cheio),
        "ja_pago_ref": float(total_japago_ref),
        "receber_mes": float(total_receber),
    }
    safe_json_save(HIST_RESUMO_MENSAL, resumo_mensal)

    return resumo_mensal[month_key]


# =========================================================
# LOGIN + TEMA
# =========================================================
def login_gate() -> bool:
    st.sidebar.markdown("### Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")
    if st.sidebar.button("Entrar"):
        st.session_state["logged_in"] = (u == "admin" and p == "123456")
        if not st.session_state["logged_in"]:
            st.sidebar.error("Usuário ou senha inválidos.")
    return st.session_state.get("logged_in", False)

def apply_theme():
    st.markdown(
        """
        <style>
            section[data-testid="stSidebar"]{ background: #0f1b3a; }
            section[data-testid="stSidebar"] * { color: #ffffff !important; }

            div[data-testid="stMetric"]{
                background: #ffffff;
                border: 1px solid #e9eef7;
                border-radius: 14px;
                padding: 12px 14px;
                box-shadow: 0 2px 10px rgba(15,27,58,0.05);
            }

            h1, h2, h3 { color: #0f1b3a; }

            .am-badge-ok{
                display:inline-block;
                padding: 4px 10px;
                border-radius: 999px;
                background: rgba(0, 122, 255, 0.12);
                color: #007AFF;
                font-weight: 700;
                font-size: 12px;
            }
            .am-badge-bad{
                display:inline-block;
                padding: 4px 10px;
                border-radius: 999px;
                background: rgba(255, 59, 48, 0.12);
                color: #FF3B30;
                font-weight: 700;
                font-size: 12px;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )

def show_logo_and_title():
    here = os.path.dirname(__file__)
    logo_path = os.path.join(here, "LOGO CORRETA.png")

    c1, c2 = st.columns([1, 5], vertical_alignment="center")
    with c1:
        if os.path.exists(logo_path):
            st.image(logo_path, width=140)
        else:
            st.warning("Logo não encontrada. Coloque 'LOGO CORRETA.png' na mesma pasta do app.py.")
    with c2:
        st.markdown(
            """
            <div style="line-height:1.1">
              <h1 style="margin-bottom:6px;">Painel de controle Assis e Mollerke parceiro Banco C6</h1>
              <div style="color:#5b6b8c;font-weight:600;">Visão Cliente + Leads + Remuneração incremental (somente diários a partir de Jan/26)</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

def style_conversao_table(df: pd.DataFrame) -> "pd.io.formats.style.Styler":
    if "Percentual_num" not in df.columns:
        df["Percentual_num"] = 0.0

    def row_style(row):
        v = float(row.get("Percentual_num", 0.0))
        if v >= ALVO_CONVERSAO:
            return ["background-color: rgba(0,122,255,0.10); color: #0f1b3a; font-weight: 600;"] * len(row)
        else:
            return ["background-color: rgba(255,59,48,0.10); color: #0f1b3a; font-weight: 600;"] * len(row)

    return df.style.apply(row_style, axis=1)


# =========================================================
# APP
# =========================================================
st.set_page_config(page_title="Assis & Mollerke | Banco C6", layout="wide")
apply_theme()

if not login_gate():
    st.stop()

show_logo_and_title()
st.divider()

# -------------------------------
# IMPORTAÇÃO DO DIA (obrigatório: data referência)
# -------------------------------
st.subheader("Importação do dia (Janeiro/2026 em diante)")

ref_date = st.date_input(
    "Data de referência (obrigatória)",
    value=dt.date.today(),
    format="DD/MM/YYYY"
)
ref_date_key = fmt_date(ref_date)
ref_month_key = fmt_month(dt.date(ref_date.year, ref_date.month, 1))

# fecha mês anterior se mudou (isso mantém o incremental correto mês a mês)
close_previous_month_if_needed(ref_month_key)

colA, colB = st.columns(2)
with colA:
    up_c6 = st.file_uploader("Planilha C6 (Visão Cliente) — diária (.xlsx)", type=["xlsx"], key="c6")
with colB:
    up_leads = st.file_uploader("Planilha Leads — diária (.xlsx)", type=["xlsx"], key="leads")

# -------------------------------
# PROCESSA DIÁRIO C6
# -------------------------------
daily_ready_c6 = False
df_c6 = None

if up_c6:
    df_c6 = read_excel_any(up_c6.getvalue())

    # Normalizações
    if COL_SALDO not in df_c6.columns:
        df_c6[COL_SALDO] = 0.0
    df_c6[COL_SALDO] = pd.to_numeric(df_c6[COL_SALDO], errors="coerce").fillna(0.0)

    df_c6[COL_DOMICILIO] = normalize_str(df_c6.get(COL_DOMICILIO, pd.Series([""] * len(df_c6))))
    df_c6[COL_STATUS] = normalize_str(df_c6.get(COL_STATUS, pd.Series([""] * len(df_c6))))
    df_c6[COL_CRIT] = normalize_str(df_c6.get(COL_CRIT, pd.Series([""] * len(df_c6))))
    df_c6[COL_BY] = df_c6.get(COL_BY, pd.Series([""] * len(df_c6)))
    df_c6[COL_BR] = normalize_str(df_c6.get(COL_BR, pd.Series([""] * len(df_c6)))).str.upper()

    # Contas abertas no dia (arquivo): conta registros com DT_CONTA_CRIADA preenchido
    if COL_ABERTURA in df_c6.columns:
        opened_day = int(to_date_series(df_c6[COL_ABERTURA]).dropna().shape[0])
    else:
        opened_day = int(len(df_c6))

    upsert_daily_hist(HIST_OPEN_DAILY, ref_date_key, opened_day)

    # Atualiza memória mensal (somente Jan/26 em diante)
    if ref_date >= HIST_START:
        update_month_levels_from_daily(df_c6, ref_date)

    daily_ready_c6 = True

# -------------------------------
# PROCESSA DIÁRIO LEADS
# -------------------------------
daily_ready_leads = False
df_leads = None

if up_leads:
    df_leads = read_excel_any(up_leads.getvalue())

    if COL_LEADS_DATA not in df_leads.columns:
        cand = [c for c in df_leads.columns if "CADAST" in str(c).upper() and "DATA" in str(c).upper()]
        if cand:
            df_leads[COL_LEADS_DATA] = df_leads[cand[0]]
        else:
            if len(df_leads.columns) >= 13:
                df_leads[COL_LEADS_DATA] = df_leads.iloc[:, 12]  # coluna M
            else:
                df_leads[COL_LEADS_DATA] = pd.NA

    df_leads[COL_LEADS_DATA] = to_date_series(df_leads[COL_LEADS_DATA])

    leads_day = int(df_leads[COL_LEADS_DATA].dropna().shape[0]) if COL_LEADS_DATA in df_leads.columns else int(len(df_leads))
    upsert_daily_hist(HIST_LEADS_DAILY, ref_date_key, leads_day)

    daily_ready_leads = True

# =========================================================
# PRIMEIRA VISTA: MÊS ATUAL (via diários)
# =========================================================
st.subheader("Resumo do mês atual (produção + remuneração incremental)")

month_info = compute_incremental_for_month(ref_month_key)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Mês atual", ref_month_key)

if month_info:
    c2.metric("Receita cheia (mês)", br_money(month_info["deveria_receber"]))
    c3.metric("Já pago (M0/M1 anteriores)", br_money(month_info["ja_pago_ref"]))
    c4.metric("A receber do banco (mês)", br_money(month_info["receber_mes"]))
else:
    c2.metric("Receita cheia (mês)", "—")
    c3.metric("Já pago (M0/M1 anteriores)", "—")
    c4.metric("A receber do banco (mês)", "—")
    st.info("Ainda não há qualificados acumulados neste mês (envie os diários C6 do mês).")

st.divider()

# =========================================================
# RESUMO DO DIA (arquivo do C6) + BR M0/M1/M2
# =========================================================
if daily_ready_c6:
    st.subheader("Resumo do dia (arquivo C6)")

    saldo_total = float(df_c6[COL_SALDO].sum())
    pix_com, pix_sem, _ = pix_summary(df_c6)
    domicilio_c6 = int(df_c6[COL_DOMICILIO].apply(contains_c6).sum())

    df_c6["_nivel"] = parse_level(df_c6)
    qualificadas_arquivo = int((df_c6["_nivel"] >= 1).sum())

    # BR (M0/M1/M2) — contagem total e contagem apenas qualificadas
    br_all = df_c6[COL_BR].replace("", "SEM BR").value_counts().reset_index()
    br_all.columns = ["BR", "Quantidade"]

    br_q = df_c6[df_c6["_nivel"] >= 1][COL_BR].replace("", "SEM BR").value_counts().reset_index()
    br_q.columns = ["BR", "Qualificadas"]

    br_merge = pd.merge(br_all, br_q, on="BR", how="left").fillna(0)
    br_merge["Quantidade"] = br_merge["Quantidade"].astype(int)
    br_merge["Qualificadas"] = br_merge["Qualificadas"].astype(int)

    # cards
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Data (referência)", ref_date_key)
    r2.metric("Saldo total", br_money(saldo_total))
    r3.metric("Com Pix", br_int(pix_com))
    r4.metric("Sem Pix", br_int(pix_sem))

    r5, r6, r7, r8 = st.columns(4)
    r5.metric("Domicílio C6", br_int(domicilio_c6))
    r6.metric("Qualificadas (arquivo)", br_int(qualificadas_arquivo))
    r7.metric("Arquivo C6", up_c6.name if up_c6 else "—")
    r8.metric("Mês do dia", ref_month_key)

    # BR cards (se existirem)
    m0 = int(br_merge.loc[br_merge["BR"].eq("M0"), "Quantidade"].sum()) if "M0" in br_merge["BR"].values else 0
    m1 = int(br_merge.loc[br_merge["BR"].eq("M1"), "Quantidade"].sum()) if "M1" in br_merge["BR"].values else 0
    m2 = int(br_merge.loc[br_merge["BR"].eq("M2"), "Quantidade"].sum()) if "M2" in br_merge["BR"].values else 0

    b1, b2, b3 = st.columns(3)
    b1.metric("M0 (no arquivo)", br_int(m0))
    b2.metric("M1 (no arquivo)", br_int(m1))
    b3.metric("M2 (no arquivo)", br_int(m2))

    st.caption("Distribuição BR (total x qualificadas)")
    st.dataframe(br_merge.sort_values("BR"), use_container_width=True, hide_index=True)

st.divider()

# =========================================================
# CONVERSÃO (Abertas ÷ Cadastradas) — com cor
# =========================================================
st.subheader("Conversão do mês (Abertas ÷ Cadastradas)")

hist_open = hist_to_df(HIST_OPEN_DAILY, "Abertas")
hist_leads = hist_to_df(HIST_LEADS_DAILY, "Cadastradas")

if hist_open.empty or hist_leads.empty:
    st.info("Envie pelo menos 1 dia de Leads e 1 dia de C6 para liberar a conversão.")
else:
    base = pd.merge(hist_leads, hist_open, on="Data", how="outer").fillna(0)
    base["Cadastradas"] = base["Cadastradas"].astype(int)
    base["Abertas"] = base["Abertas"].astype(int)

    base["Mes_ref"] = base["Data"].map(lambda d: dt.date(d.year, d.month, 1))
    meses = sorted(base["Mes_ref"].unique())
    meses_lbl = [fmt_month(m) for m in meses]

    # default: mês atual
    default_idx = meses_lbl.index(ref_month_key) if ref_month_key in meses_lbl else len(meses_lbl) - 1
    mes_sel_lbl = st.selectbox("Selecione o mês", meses_lbl, index=default_idx)
    mes_sel = meses[meses_lbl.index(mes_sel_lbl)]

    mes_df = base[base["Mes_ref"] == mes_sel].copy()
    mes_df["Percentual_num"] = mes_df.apply(
        lambda r: (r["Abertas"] / r["Cadastradas"]) if r["Cadastradas"] > 0 else 0.0, axis=1
    )
    mes_df["% Abertas/Cadastradas"] = mes_df["Percentual_num"].map(lambda x: f"{x*100:.1f}%".replace(".", ","))
    mes_df["Indicador"] = mes_df["Percentual_num"].map(lambda x: "Dentro do alvo" if x >= ALVO_CONVERSAO else "Abaixo do alvo")

    total_ab = int(mes_df["Abertas"].sum())
    total_cad = int(mes_df["Cadastradas"].sum())
    perc_mes = (total_ab / total_cad) if total_cad > 0 else 0.0

    if perc_mes >= ALVO_CONVERSAO:
        st.markdown(f"<div class='am-badge-ok'>Conversão do mês: {perc_mes*100:.1f}%</div>", unsafe_allow_html=True)
    else:
        st.markdown(f"<div class='am-badge-bad'>Conversão do mês: {perc_mes*100:.1f}%</div>", unsafe_allow_html=True)

    a1, a2, a3 = st.columns(3)
    a1.metric("Abertas (mês)", br_int(total_ab))
    a2.metric("Cadastradas (mês)", br_int(total_cad))
    a3.metric("Mês", mes_sel_lbl)

    # tabela diária (mais recente -> mais antigo)
    mes_df = mes_df.sort_values("Data", ascending=False)
    show = mes_df[["Data", "Cadastradas", "Abertas", "% Abertas/Cadastradas", "Indicador", "Percentual_num"]].copy()
    show["Data"] = show["Data"].apply(fmt_date)

    st.caption("Tabela diária do mês (azul ≥ 20% | vermelho < 20%)")
    st.dataframe(style_conversao_table(show).hide(axis="index"), use_container_width=True)

    chart = mes_df.sort_values("Data", ascending=True).copy()
    chart["Dia"] = chart["Data"].apply(lambda d: d.day)
    st.caption("Produção diária (cadastradas x abertas)")
    st.line_chart(chart.set_index("Dia")[["Cadastradas", "Abertas"]])

st.divider()

# =========================================================
# COMPARATIVO DE REMUNERAÇÃO (todos os meses)
# =========================================================
st.subheader("Comparativo de remuneração (todos os meses com resultado)")

saved = safe_json_load(HIST_RESUMO_MENSAL, default={})
if not saved:
    st.info("Ainda não há meses calculados. Envie os diários do mês (Jan/26 em diante).")
else:
    rows = []
    for mes, info in saved.items():
        rows.append([
            mes,
            info.get("faixa", ""),
            int(info.get("qualificadas", 0)),
            float(info.get("deveria_receber", 0.0)),
            float(info.get("ja_pago_ref", 0.0)),
            float(info.get("receber_mes", 0.0)),
        ])

    dfm = pd.DataFrame(rows, columns=[
        "Mês", "Faixa", "Qualificadas", "Receita cheia", "Já pago (ref.)", "A receber"
    ])
    dfm = dfm.sort_values("Mês", key=lambda c: c.map(month_to_key), ascending=False)

    view = dfm.copy()
    view["Qualificadas"] = view["Qualificadas"].apply(br_int)
    view["Receita cheia"] = view["Receita cheia"].apply(br_money)
    view["Já pago (ref.)"] = view["Já pago (ref.)"].apply(br_money)
    view["A receber"] = view["A receber"].apply(br_money)

    st.dataframe(view, use_container_width=True, hide_index=True)

    chart = dfm.sort_values("Mês", key=lambda c: c.map(month_to_key), ascending=True).copy()
    chart = chart.set_index("Mês")[["Receita cheia", "A receber"]]
    st.caption("Evolução mensal (Receita cheia x A receber)")
    st.line_chart(chart)

st.divider()

# =========================================================
# RELATÓRIOS (diário) — Qualificação bonito + níveis corretos
# =========================================================
st.subheader("Relatórios (diário)")

if not daily_ready_c6:
    st.info("Envie a planilha diária do C6 para liberar os relatórios.")
else:
    tabs = st.tabs(["Qualificação (executivo)", "Fundações", "Pix + Status"])

    with tabs[0]:
        st.markdown("#### Qualificação (nível vencedor e critério vencedor)")

        dfq = df_c6.copy()
        if COL_CNPJ not in dfq.columns:
            cand = [c for c in dfq.columns if "CNPJ" in str(c).upper()]
            dfq[COL_CNPJ] = dfq[cand[0]] if cand else ""
        dfq["CNPJ"] = normalize_str(dfq[COL_CNPJ]).str.replace(r"\D", "", regex=True)

        dfq["Nível"] = parse_level(dfq)
        dfq["Critério vencedor"] = dfq[COL_CRIT].astype("string").fillna("").apply(criterio_vencedor)
        dfq["BR"] = normalize_str(dfq.get(COL_BR, pd.Series([""] * len(dfq)))).str.upper()

        qual = dfq[dfq["Nível"] >= 1].copy()

        total_qual = int(qual.shape[0])
        n1 = int((qual["Nível"] == 1).sum())
        n2 = int((qual["Nível"] == 2).sum())
        n3 = int((qual["Nível"] == 3).sum())
        n4 = int((qual["Nível"] == 4).sum())

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Qualificadas (arquivo)", br_int(total_qual))
        c2.metric("Nível 1", br_int(n1))
        c3.metric("Nível 2", br_int(n2))
        c4.metric("Nível 3", br_int(n3))
        c5.metric("Nível 4", br_int(n4))

        show = qual[["CNPJ", "BR", "Nível", "Critério vencedor"]].copy()
        st.dataframe(show, use_container_width=True, hide_index=True)

    with tabs[1]:
        st.markdown("#### Fundação (mês/ano)")
        if COL_FUNDACAO not in df_c6.columns:
            st.info("Sem coluna de fundação no arquivo.")
        else:
            temp = df_c6[[COL_FUNDACAO]].copy()
            temp[COL_FUNDACAO] = to_date_series(temp[COL_FUNDACAO])
            temp = temp.dropna()
            if temp.empty:
                st.info("Sem dados de fundação preenchidos.")
            else:
                temp["Mês fundação"] = temp[COL_FUNDACAO].apply(lambda d: f"{d.month:02d}/{d.year}")
                out = temp["Mês fundação"].value_counts().reset_index()
                out.columns = ["Mês fundação", "Quantidade"]
                st.dataframe(out, use_container_width=True, hide_index=True)
                st.bar_chart(out.set_index("Mês fundação")["Quantidade"])

    with tabs[2]:
        st.markdown("#### Pix")
        pix_com, pix_sem, pix_por_chave = pix_summary(df_c6)
        a, b = st.columns(2)
        a.metric("Com Pix", br_int(pix_com))
        b.metric("Sem Pix", br_int(pix_sem))
        st.dataframe(pix_por_chave, use_container_width=True, hide_index=True)

        st.markdown("#### Status")
        status = (
            df_c6[COL_STATUS]
            .replace("", "SEM STATUS")
            .fillna("SEM STATUS")
            .value_counts()
            .rename_axis("Status")
            .reset_index(name="Quantidade")
        )
        st.dataframe(status, use_container_width=True, hide_index=True)
        st.bar_chart(status.set_index("Status")["Quantidade"])
