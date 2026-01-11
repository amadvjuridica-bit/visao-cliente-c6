import os
import io
import json
import hashlib
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
COL_ABERTURA = "DT_CONTA_CRIADA"                  # data de abertura da conta
COL_FUNDACAO = "DT_FUNDACAO_EMPRESA"              # data de fundação
COL_PIX = "CHAVES_PIX_FORTE"                      # tipo de chave pix (CNPJ/EMAIL/PHONE/-)
COL_SALDO = "VL_SALDO_MEDIO_MENSALIZADO"          # saldo (valor)
COL_STATUS = "STATUS_CC"                          # status
COL_DOMICILIO = "BANCO_DOMICILIO"                 # banco domicílio
COL_BY = "FL_QUALIFICADO_COMISS"                  # pode vir 0/1 OU 1..4 OU texto
COL_BR = "MES_REF_COMISS"                         # M0/M1/M2
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"           # texto "CASH IN: X | ..."

# Planilha "Leads" (cadastros) - coluna M (13ª) vira DATA_CADASTRO
COL_LEADS_DATA = "DATA_CADASTRO"

# Conversão
ALVO_CONVERSAO = 0.20  # 20%

# Memória diária a partir daqui (não considera datas antes)
HIST_START = dt.date(2026, 1, 1)

# =========================================================
# REGRAS DE REMUNERAÇÃO (POR FAIXA)
# =========================================================
FAIXAS = [
    (0,   "Até 49 (1.0)",   {1: 140.00, 2: 230.00, 3: 400.00, 4: 540.00}),
    (50,  "50+ (1.1)",      {1: 154.00, 2: 253.00, 3: 440.00, 4: 594.00}),
    (150, "150+ (1.25)",    {1: 175.00, 2: 287.50, 3: 500.00, 4: 675.00}),
    (350, "350+ (1.5)",     {1: 210.00, 2: 345.00, 3: 600.00, 4: 810.00}),
]

# =========================================================
# ARQUIVOS DE MEMÓRIA (HISTÓRICO)
# =========================================================
DATA_DIR = "data_store"
os.makedirs(DATA_DIR, exist_ok=True)

# Histórico diário (1 valor por dia)
HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_aberturas_diario.json")      # dd/mm/aaaa -> aberturas (dia)
HIST_LEADS_DAILY = os.path.join(DATA_DIR, "hist_cadastros_diario.json")     # dd/mm/aaaa -> cadastradas (dia)

# Histórico mensal por CNPJ -> nível máximo no mês (mm/aaaa -> {cnpj: nivel})
HIST_MONTH_LEVELS = os.path.join(DATA_DIR, "hist_mes_cnpj_nivel.json")

# Incremental global: max pago por CNPJ até o momento
HIST_PAGO_POR_CNPJ = os.path.join(DATA_DIR, "pago_max_por_cnpj.json")

# Resumo mensal consolidado: mm/aaaa -> métricas
HIST_RESUMO_MENSAL = os.path.join(DATA_DIR, "resumo_mensal.json")


# =========================================================
# HELPERS
# =========================================================
def safe_json_load(path: str, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def safe_json_save(path: str, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def br_money(v: float) -> str:
    s = f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def br_int(n: int) -> str:
    return f"{int(n):,}".replace(",", ".")


def fmt_date(d: Optional[dt.date]) -> str:
    if d is None or pd.isna(d):
        return ""
    if isinstance(d, pd.Timestamp):
        d = d.date()
    if isinstance(d, dt.datetime):
        d = d.date()
    return d.strftime("%d/%m/%Y")


def fmt_month(d: dt.date) -> str:
    return d.strftime("%m/%Y")


def month_first(d: dt.date) -> dt.date:
    return dt.date(d.year, d.month, 1)


def month_key_str(m: str) -> int:
    try:
        mm, aa = m.split("/")
        return int(aa) * 100 + int(mm)
    except Exception:
        return 0


def to_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.date


def normalize_str(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").str.strip()


def read_excel_any(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")


def contains_c6(x) -> bool:
    if x is None or pd.isna(x):
        return False
    return "c6" in str(x).lower()


# =========================================================
# QUALIFICAÇÃO
# =========================================================
def parse_level_from_criterios(txt: str) -> int:
    """
    "CASH IN: 3 | DOMICILIO: 0 | SALDO MEDIO: 4 | ..."
    Regra: considerar o MAIOR valor (1..4). Se não tiver, 0.
    """
    if not isinstance(txt, str) or not txt.strip():
        return 0
    nums = [int(n) for n in re.findall(r":\s*(\d+)", txt)]
    if not nums:
        return 0
    m = max(nums)
    if m < 1:
        return 0
    return min(m, 4)


def parse_level(df: pd.DataFrame) -> pd.Series:
    """
    Regra FINAL (evita errar):
    - Se BY vier número 1..4, isso pode indicar qualificada/nível em algumas bases => considera
    - Também extrai do CRITERIOS (maior valor)
    - Nível final = max(BY_nivel, CRIT_nivel)
    """
    by_raw = df.get(COL_BY, pd.Series([None] * len(df)))
    by_num = pd.to_numeric(by_raw, errors="coerce").fillna(0).astype(int)
    by_lvl = by_num.where(by_num.between(1, 4), 0)

    crit_raw = df.get(COL_CRIT, pd.Series([""] * len(df))).astype("string").fillna("")
    crit_lvl = crit_raw.apply(parse_level_from_criterios).astype(int)

    lvl = pd.concat([by_lvl, crit_lvl], axis=1).max(axis=1).astype(int)
    lvl = lvl.where(lvl.between(1, 4), 0)
    return lvl


def criterio_vencedor(txt: str) -> str:
    if not isinstance(txt, str) or not txt.strip():
        return ""
    parts = [p.strip() for p in txt.split("|")]
    best_name, best_val = "", 0
    for p in parts:
        m = re.search(r"(.+):\s*(\d+)", p)
        if m:
            nome = m.group(1).strip()
            val = int(m.group(2))
            if val > best_val:
                best_name, best_val = nome, val
    if best_val <= 0:
        return ""
    return f"{best_name} ({best_val})"


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
def upsert_daily_value(path: str, date_key: str, qty: int):
    base = safe_json_load(path, default={})
    base[date_key] = int(qty)  # substitui pelo último valor daquele dia
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
# REMUNERAÇÃO - FAIXA
# =========================================================
def faixa_por_qtd(qtd_qualificadas: int) -> Tuple[str, Dict[int, float]]:
    chosen_name, chosen_tbl = FAIXAS[0][1], FAIXAS[0][2]
    for min_q, nm, tbl in FAIXAS:
        if qtd_qualificadas >= min_q:
            chosen_name, chosen_tbl = nm, tbl
    return chosen_name, chosen_tbl


# =========================================================
# MEMÓRIA MENSAL POR CNPJ (base diária)
# =========================================================
def month_levels_upsert_from_daily(df_c6: pd.DataFrame, ref_date: dt.date):
    """
    Atualiza HIST_MONTH_LEVELS com base SOMENTE nos registros do dia (DT_CONTA_CRIADA == ref_date)
    """
    store = safe_json_load(HIST_MONTH_LEVELS, default={})
    mkey = fmt_month(month_first(ref_date))
    month_map: Dict[str, int] = store.get(mkey, {}) or {}

    # filtra somente o dia
    if COL_ABERTURA in df_c6.columns:
        abertura = to_date_series(df_c6[COL_ABERTURA])
        df_day = df_c6[abertura == ref_date].copy()
    else:
        df_day = df_c6.copy()

    if df_day.empty:
        store[mkey] = month_map
        safe_json_save(HIST_MONTH_LEVELS, store)
        return

    if COL_CNPJ not in df_day.columns:
        cand = [c for c in df_day.columns if "CNPJ" in str(c).upper()]
        df_day[COL_CNPJ] = df_day[cand[0]] if cand else ""

    df_day["_cnpj"] = normalize_str(df_day[COL_CNPJ]).str.replace(r"\D", "", regex=True)
    df_day["_nivel"] = parse_level(df_day)

    q = df_day[(df_day["_cnpj"] != "") & (df_day["_nivel"] >= 1)].copy()
    if q.empty:
        store[mkey] = month_map
        safe_json_save(HIST_MONTH_LEVELS, store)
        return

    by_cnpj = q.groupby("_cnpj")["_nivel"].max().reset_index()
    for _, r in by_cnpj.iterrows():
        cnpj = str(r["_cnpj"])
        lvl = int(r["_nivel"])
        prev = int(month_map.get(cnpj, 0))
        if lvl > prev:
            month_map[cnpj] = lvl

    store[mkey] = month_map
    safe_json_save(HIST_MONTH_LEVELS, store)


# =========================================================
# IMPORTAÇÃO MENSAL (NOV/25 e DEZ/25) - EXCEÇÃO
# =========================================================
def detect_month_from_filename(name: str) -> Optional[dt.date]:
    n = name.upper()
    if "NOVEMBRO2025" in n or "NOV/2025" in n or "NOV_2025" in n or "NOV-2025" in n:
        return dt.date(2025, 11, 1)
    if "DEZEMBRO2025" in n or "DEZ/2025" in n or "DEZ_2025" in n or "DEZ-2025" in n:
        return dt.date(2025, 12, 1)
    return None


def month_levels_upsert_from_monthly_file(file_name: str, file_bytes: bytes):
    """
    Lê arquivo mensal e grava no HIST_MONTH_LEVELS:
    - agrupa por CNPJ
    - pega nível máximo por CNPJ no mês
    """
    df = read_excel_any(file_bytes)

    # tenta detectar mês
    m = None
    if COL_ABERTURA in df.columns:
        d = to_date_series(df[COL_ABERTURA]).dropna()
        if len(d) > 0:
            # mês mais frequente
            mm = pd.Series([dt.date(x.year, x.month, 1) for x in d]).mode()
            if len(mm) > 0:
                m = mm.iloc[0]
    if m is None:
        m = detect_month_from_filename(file_name)
    if m is None:
        return  # ignora arquivo sem mês detectável

    store = safe_json_load(HIST_MONTH_LEVELS, default={})
    mkey = fmt_month(m)
    month_map: Dict[str, int] = store.get(mkey, {}) or {}

    # cnpj
    if COL_CNPJ not in df.columns:
        cand = [c for c in df.columns if "CNPJ" in str(c).upper()]
        df[COL_CNPJ] = df[cand[0]] if cand else ""

    df["_cnpj"] = normalize_str(df[COL_CNPJ]).str.replace(r"\D", "", regex=True)
    df["_nivel"] = parse_level(df)

    q = df[(df["_cnpj"] != "") & (df["_nivel"] >= 1)].copy()
    if q.empty:
        store[mkey] = month_map
        safe_json_save(HIST_MONTH_LEVELS, store)
        return

    by_cnpj = q.groupby("_cnpj")["_nivel"].max().reset_index()
    for _, r in by_cnpj.iterrows():
        cnpj = str(r["_cnpj"])
        lvl = int(r["_nivel"])
        prev = int(month_map.get(cnpj, 0))
        if lvl > prev:
            month_map[cnpj] = lvl

    store[mkey] = month_map
    safe_json_save(HIST_MONTH_LEVELS, store)


# =========================================================
# RECALCULA REMUNERAÇÃO (INCREMENTAL) A PARTIR DO HIST_MONTH_LEVELS
# =========================================================
def recompute_incremental() -> pd.DataFrame:
    month_levels = safe_json_load(HIST_MONTH_LEVELS, default={})
    months = sorted(list(month_levels.keys()), key=month_key_str)

    paid_max: Dict[str, float] = {}
    resumo: Dict[str, dict] = {}

    rows = []
    for mkey in months:
        cmap: Dict[str, int] = month_levels.get(mkey, {}) or {}
        cmap = {k: int(v) for k, v in cmap.items() if str(k).strip() != ""}

        qtd_qual = len(cmap)
        faixa_nome, precos = faixa_por_qtd(qtd_qual)

        total_cheio = 0.0
        total_receber = 0.0

        for cnpj, lvl in cmap.items():
            lvl = int(lvl)
            cheio = float(precos.get(lvl, 0.0))
            prev = float(paid_max.get(cnpj, 0.0))
            diff = cheio - prev
            if diff < 0:
                diff = 0.0

            total_cheio += cheio
            total_receber += diff
            paid_max[cnpj] = max(prev, cheio)

        ja_pago_ref = total_cheio - total_receber

        resumo[mkey] = {
            "faixa": faixa_nome,
            "qualificadas": qtd_qual,
            "deveria_receber": total_cheio,
            "ja_pago_ref": ja_pago_ref,
            "receber_mes": total_receber,
        }

        rows.append([mkey, faixa_nome, qtd_qual, total_cheio, ja_pago_ref, total_receber])

    safe_json_save(HIST_PAGO_POR_CNPJ, paid_max)
    safe_json_save(HIST_RESUMO_MENSAL, resumo)

    return pd.DataFrame(
        rows,
        columns=["Mês", "Faixa", "Qualificadas", "Deveria receber (cheio)", "Já pago (referência)", "A receber no mês"],
    )


# =========================================================
# TEMA / HEADER / LOGIN
# =========================================================
def apply_theme():
    st.markdown(
        """
        <style>
            section[data-testid="stSidebar"]{ background:#0f1b3a; }
            section[data-testid="stSidebar"] * { color:#ffffff !important; }
            div[data-testid="stMetric"]{
                background:#ffffff;
                border:1px solid #e9eef7;
                border-radius:14px;
                padding:12px 14px;
                box-shadow:0 2px 10px rgba(15,27,58,0.05);
            }
            h1,h2,h3{ color:#0f1b3a; }
            .am-badge-ok{
                display:inline-block; padding:4px 10px; border-radius:999px;
                background:rgba(0,122,255,0.12); color:#007AFF;
                font-weight:800; font-size:12px;
            }
            .am-badge-bad{
                display:inline-block; padding:4px 10px; border-radius:999px;
                background:rgba(255,59,48,0.12); color:#FF3B30;
                font-weight:800; font-size:12px;
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
            st.image(logo_path, width=150)
        else:
            st.warning("Logo não encontrada. Coloque 'LOGO CORRETA.png' na mesma pasta do app.py.")
    with c2:
        st.markdown(
            """
            <div style="line-height:1.1">
              <div style="font-size:28px;font-weight:900;color:#0f1b3a;margin-bottom:4px;">
                Painel de controle Assis e Mollerke parceiro Banco C6
              </div>
              <div style="color:#5b6b8c;font-weight:600;">
                Visão Cliente + Leads + Remuneração incremental
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def login_gate() -> bool:
    st.sidebar.markdown("### Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")
    if st.sidebar.button("Entrar"):
        st.session_state["logged_in"] = (u == "admin" and p == "123456")
        if not st.session_state["logged_in"]:
            st.sidebar.error("Usuário ou senha inválidos.")
    return st.session_state.get("logged_in", False)


# =========================================================
# APP
# =========================================================
st.set_page_config(page_title="Assis & Mollerke | Banco C6", layout="wide")
apply_theme()

if not login_gate():
    st.stop()

show_logo_and_title()
st.divider()

# =========================================================
# IMPORTAÇÃO
# =========================================================
st.subheader("Importação do dia (Janeiro/26 em diante)")

colA, colB, colC = st.columns([1.1, 1.3, 1.3])
with colA:
    ref_date = st.date_input("Data de referência", value=dt.date.today(), format="DD/MM/YYYY")
with colB:
    up_c6 = st.file_uploader("Planilha C6 (Visão Cliente) — diária (.xlsx)", type=["xlsx"], key="c6")
with colC:
    up_leads = st.file_uploader("Planilha Leads — diária (.xlsx)", type=["xlsx"], key="leads")

st.subheader("Importação mensal (exceção: Nov/25 e Dez/25)")
up_monthly = st.file_uploader(
    "Envie os arquivos mensais (Nov/25 e Dez/25) — pode enviar os dois de uma vez",
    type=["xlsx"],
    accept_multiple_files=True,
    key="monthly",
)

# processa mensal (se enviar)
if up_monthly and len(up_monthly) > 0:
    for f in up_monthly:
        month_levels_upsert_from_monthly_file(f.name, f.getvalue())

st.divider()

# =========================================================
# PROCESSAMENTO DIÁRIO
# =========================================================
daily_ready = False
leads_ready = False
df_c6 = None
df_leads = None

if ref_date < HIST_START:
    st.warning(f"O painel memoriza a partir de {fmt_date(HIST_START)}. Selecione uma data a partir disso para diário.")

if up_c6:
    b = up_c6.getvalue()
    df_c6 = read_excel_any(b)

    # normalizações
    if COL_CNPJ not in df_c6.columns:
        cand = [c for c in df_c6.columns if "CNPJ" in str(c).upper()]
        df_c6[COL_CNPJ] = df_c6[cand[0]] if cand else ""

    if COL_ABERTURA not in df_c6.columns:
        df_c6[COL_ABERTURA] = pd.NA
    if COL_FUNDACAO not in df_c6.columns:
        df_c6[COL_FUNDACAO] = pd.NA
    if COL_SALDO not in df_c6.columns:
        df_c6[COL_SALDO] = 0.0
    if COL_BR not in df_c6.columns:
        df_c6[COL_BR] = ""

    df_c6[COL_CNPJ] = normalize_str(df_c6[COL_CNPJ]).str.replace(r"\D", "", regex=True)
    df_c6[COL_ABERTURA] = to_date_series(df_c6[COL_ABERTURA])
    df_c6[COL_FUNDACAO] = to_date_series(df_c6[COL_FUNDACAO])
    df_c6[COL_SALDO] = pd.to_numeric(df_c6[COL_SALDO], errors="coerce").fillna(0.0)

    df_c6[COL_PIX] = normalize_str(df_c6.get(COL_PIX, pd.Series([""] * len(df_c6))))
    df_c6[COL_STATUS] = normalize_str(df_c6.get(COL_STATUS, pd.Series([""] * len(df_c6))))
    df_c6[COL_DOMICILIO] = normalize_str(df_c6.get(COL_DOMICILIO, pd.Series([""] * len(df_c6))))
    df_c6[COL_CRIT] = normalize_str(df_c6.get(COL_CRIT, pd.Series([""] * len(df_c6))))
    df_c6[COL_BY] = df_c6.get(COL_BY, pd.Series([""] * len(df_c6)))
    df_c6[COL_BR] = normalize_str(df_c6.get(COL_BR, pd.Series([""] * len(df_c6)))).str.upper()

    # só contar do dia selecionado
    opened_day = int((df_c6[COL_ABERTURA] == ref_date).sum())

    if ref_date >= HIST_START:
        upsert_daily_value(HIST_OPEN_DAILY, fmt_date(ref_date), opened_day)
        month_levels_upsert_from_daily(df_c6, ref_date)

    daily_ready = True

if up_leads:
    b = up_leads.getvalue()
    df_leads = read_excel_any(b)

    # mapear DATA_CADASTRO (ou coluna M)
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
    leads_day = int((df_leads[COL_LEADS_DATA] == ref_date).sum())

    if ref_date >= HIST_START:
        upsert_daily_value(HIST_LEADS_DAILY, fmt_date(ref_date), leads_day)

    leads_ready = True

# =========================================================
# REMUNERAÇÃO CONSOLIDADA (NOV/25 em diante, se existir)
# =========================================================
df_monthly = recompute_incremental()

# =========================================================
# PRIMEIRA VISTA: RESUMO DO DIA + MÊS ATUAL
# =========================================================
if daily_ready:
    df_day = df_c6[df_c6[COL_ABERTURA] == ref_date].copy()

    saldo_total = float(df_day[COL_SALDO].sum()) if not df_day.empty else 0.0
    pix_com, pix_sem, _ = pix_summary(df_day if not df_day.empty else df_c6)
    domicilio_c6 = int(df_day[COL_DOMICILIO].apply(contains_c6).sum()) if not df_day.empty else 0

    df_day["_nivel"] = parse_level(df_day) if not df_day.empty else pd.Series([], dtype=int)
    qualificadas_day = int((df_day["_nivel"] >= 1).sum()) if not df_day.empty else 0

    st.subheader("Resumo executivo (dia)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Contas abertas (dia)", br_int(opened_day))
    c2.metric("Saldo total (dia)", br_money(saldo_total))
    c3.metric("Clientes com Pix (dia)", br_int(pix_com))
    c4.metric("Clientes sem Pix (dia)", br_int(pix_sem))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Domicílio C6 (dia)", br_int(domicilio_c6))
    c6.metric("Contas qualificadas (dia)", br_int(qualificadas_day))
    c7.metric("Arquivo C6", up_c6.name)
    c8.metric("Data referência", fmt_date(ref_date))

    st.divider()

# Mostra remuneração do mês atual (do diário)
st.subheader("Remuneração do mês atual (incremental)")

mes_atual = fmt_month(month_first(ref_date))
if not df_monthly.empty and mes_atual in set(df_monthly["Mês"].tolist()):
    row = df_monthly[df_monthly["Mês"] == mes_atual].iloc[0]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Mês", row["Mês"])
    m2.metric("Receita cheia (mês)", br_money(float(row["Deveria receber (cheio)"])))
    m3.metric("Já pago (referência)", br_money(float(row["Já pago (referência)"])))
    m4.metric("A receber (mês)", br_money(float(row["A receber no mês"])))
else:
    st.info("Ainda não há dados suficientes do mês atual para calcular a remuneração. Importe ao menos 1 dia do mês (C6).")

st.divider()

# =========================================================
# CONVERSÃO DO MÊS (somente mês selecionado)
# =========================================================
st.subheader("Conversão do mês (Abertas ÷ Cadastradas)")

hist_open = hist_to_df(HIST_OPEN_DAILY, "Abertas")
hist_leads = hist_to_df(HIST_LEADS_DAILY, "Cadastradas")

if hist_open.empty or hist_leads.empty:
    st.info("Envie ao menos 1 dia de C6 e 1 dia de Leads para montar a conversão.")
else:
    base = pd.merge(hist_leads, hist_open, on="Data", how="outer").fillna(0)
    base["Cadastradas"] = base["Cadastradas"].astype(int)
    base["Abertas"] = base["Abertas"].astype(int)
    base["Mes_ref"] = base["Data"].map(month_first)

    meses = sorted(base["Mes_ref"].unique())
    meses_lbl = [fmt_month(m) for m in meses]

    # por padrão, mês do ref_date
    default_month = month_first(ref_date)
    idx = meses.index(default_month) if default_month in meses else len(meses) - 1

    mes_sel_lbl = st.selectbox("Selecione o mês", meses_lbl, index=idx)
    mes_sel = meses[meses_lbl.index(mes_sel_lbl)]

    mes_df = base[base["Mes_ref"] == mes_sel].copy()
    mes_df["Percentual_num"] = mes_df.apply(
        lambda r: (r["Abertas"] / r["Cadastradas"]) if r["Cadastradas"] > 0 else 0.0,
        axis=1
    )
    mes_df["% Abertas/Cadastradas"] = mes_df["Percentual_num"].map(lambda x: f"{x*100:.1f}%".replace(".", ","))
    mes_df["Indicador"] = mes_df["Percentual_num"].map(lambda x: "Dentro do alvo" if x >= ALVO_CONVERSAO else "Abaixo do alvo")

    # ordena mais recente -> mais antigo
    mes_df = mes_df.sort_values("Data", ascending=False).reset_index(drop=True)

    total_ab_mes = int(mes_df["Abertas"].sum())
    total_cad_mes = int(mes_df["Cadastradas"].sum())
    perc_mes = (total_ab_mes / total_cad_mes) if total_cad_mes > 0 else 0.0

    badge = "am-badge-ok" if perc_mes >= ALVO_CONVERSAO else "am-badge-bad"
    st.markdown(f"<div class='{badge}'>Conversão do mês: {str(round(perc_mes*100,1)).replace('.',',')}%</div>", unsafe_allow_html=True)

    ca, cb, cc = st.columns(3)
    ca.metric("Abertas (mês)", br_int(total_ab_mes))
    cb.metric("Cadastradas (mês)", br_int(total_cad_mes))
    cc.metric("Mês", mes_sel_lbl)

    # tabela com cor por linha
    display = mes_df[["Data", "Cadastradas", "Abertas", "% Abertas/Cadastradas", "Indicador"]].copy()
    display["Data"] = display["Data"].apply(fmt_date)
    display["Cadastradas"] = display["Cadastradas"].apply(br_int)
    display["Abertas"] = display["Abertas"].apply(br_int)

    def highlight_row(row):
        v = float(mes_df.loc[row.name, "Percentual_num"])
        if v >= ALVO_CONVERSAO:
            return ["background-color: rgba(0,122,255,0.10); font-weight: 700;"] * len(row)
        return ["background-color: rgba(255,59,48,0.10); font-weight: 700;"] * len(row)

    st.caption("Produção diária do mês selecionado")
    st.dataframe(display.style.apply(highlight_row, axis=1), use_container_width=True, hide_index=True)

    st.markdown("#### Fechamento do mês")
    f1, f2, f3 = st.columns(3)
    f1.metric("Total Cadastradas (mês)", br_int(total_cad_mes))
    f2.metric("Total Abertas (mês)", br_int(total_ab_mes))
    f3.metric("% Geral (mês)", f"{perc_mes*100:.1f}%".replace(".", ","))

    chart = mes_df.copy()
    chart["Dia"] = chart["Data"].apply(lambda d: d.day)
    chart = chart.sort_values("Dia")
    st.line_chart(chart.set_index("Dia")[["Cadastradas", "Abertas"]])

st.divider()

# =========================================================
# RELATÓRIOS (DIÁRIO)
# =========================================================
st.subheader("Relatórios (diário)")

if not daily_ready:
    st.info("Envie a planilha diária do C6 para liberar os relatórios.")
else:
    tabs = st.tabs(["Aberturas", "Fundações (por dia)", "Pix + Status", "Qualificação + BR"])

    # Aberturas
    with tabs[0]:
        st.markdown("#### Contas abertas por dia (arquivo)")
        por_dia = (
            pd.Series(df_c6[COL_ABERTURA]).dropna().value_counts().sort_index()
            .rename_axis("Dia")
            .reset_index(name="Contas abertas")
        )
        por_dia["Dia"] = por_dia["Dia"].apply(fmt_date)
        por_dia = por_dia.sort_values("Dia", ascending=False)
        st.bar_chart(por_dia.set_index("Dia")["Contas abertas"])
        st.dataframe(por_dia, use_container_width=True, hide_index=True)

    # Fundações (igual você disse que estava perfeito)
    with tabs[1]:
        st.markdown("#### Fundação (mês/ano) dentro do dia de abertura")
        temp = df_c6[[COL_ABERTURA, COL_FUNDACAO]].dropna().copy()
        if temp.empty:
            st.info("Sem dados de fundação no arquivo.")
        else:
            temp["Dia"] = temp[COL_ABERTURA]
            temp["Mês fundação"] = temp[COL_FUNDACAO].apply(lambda d: f"{d.month:02d}/{d.year}" if isinstance(d, dt.date) else "")

            pivot = (
                temp.groupby(["Dia", "Mês fundação"])
                .size()
                .reset_index(name="Quantidade")
                .sort_values(["Dia", "Mês fundação"])
            )

            dias = sorted(temp[COL_ABERTURA].unique())
            dias_lbl = [fmt_date(d) for d in dias]
            dia_sel_lbl = st.selectbox("Selecione o dia de abertura", dias_lbl, index=len(dias_lbl)-1)
            dia_sel = dias[dias_lbl.index(dia_sel_lbl)]

            dia_df = pivot[pivot["Dia"] == dia_sel].copy()
            dia_df["Dia"] = dia_df["Dia"].apply(fmt_date)

            total_dia = int(dia_df["Quantidade"].sum())
            st.markdown(f"**No dia {dia_sel_lbl} foram abertas {br_int(total_dia)} empresas.**")

            st.dataframe(dia_df[["Mês fundação", "Quantidade"]], use_container_width=True, hide_index=True)
            st.bar_chart(dia_df.set_index("Mês fundação")["Quantidade"])

    # Pix + Status
    with tabs[2]:
        st.markdown("#### Pix")
        pix_com, pix_sem, pix_por_chave = pix_summary(df_c6)
        a, b = st.columns(2)
        a.metric("Clientes com Pix", br_int(pix_com))
        b.metric("Clientes sem Pix", br_int(pix_sem))
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

    # Qualificação + BR
    with tabs[3]:
        st.markdown("#### Qualificação (nível vencedor e critério vencedor)")
        dfq = df_c6.copy()
        dfq["_nivel"] = parse_level(dfq)
        dfq["_qualificada"] = dfq["_nivel"].apply(lambda x: "Sim" if x >= 1 else "Não")
        dfq["_criterio_vencedor"] = dfq[COL_CRIT].astype("string").fillna("").apply(criterio_vencedor)

        total_qual = int((dfq["_nivel"] >= 1).sum())
        n1 = int((dfq["_nivel"] == 1).sum())
        n2 = int((dfq["_nivel"] == 2).sum())
        n3 = int((dfq["_nivel"] == 3).sum())
        n4 = int((dfq["_nivel"] == 4).sum())

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Qualificadas", br_int(total_qual))
        c2.metric("Nível 1", br_int(n1))
        c3.metric("Nível 2", br_int(n2))
        c4.metric("Nível 3", br_int(n3))
        c5.metric("Nível 4", br_int(n4))

        st.markdown("#### BR (M0/M1/M2)")
        brs = normalize_str(dfq.get(COL_BR, pd.Series([""] * len(dfq)))).str.upper().replace("", "SEM BR")
        br_counts = (
            brs.value_counts()
            .rename_axis("BR")
            .reset_index(name="Quantidade")
        )
        st.dataframe(br_counts, use_container_width=True, hide_index=True)

        st.markdown("#### Lista de qualificadas (arquivo)")
        show = dfq[dfq["_qualificada"] == "Sim"].copy()
        show = show[[COL_CNPJ, COL_ABERTURA, "_nivel", "_criterio_vencedor"]].rename(columns={
            COL_CNPJ: "CNPJ",
            COL_ABERTURA: "Data de abertura",
            "_nivel": "Nível",
            "_criterio_vencedor": "Critério vencedor"
        })
        show["Data de abertura"] = show["Data de abertura"].apply(fmt_date)
        show = show.sort_values("Data de abertura", ascending=False)
        st.dataframe(show, use_container_width=True, hide_index=True)

st.divider()

# =========================================================
# COMPARATIVO MENSAL (NOV/25 + DEZ/25 + meses de 2026 em diante)
# =========================================================
st.subheader("Comparativo mensal de remuneração (histórico)")

saved = safe_json_load(HIST_RESUMO_MENSAL, default={})
if not saved:
    st.info("Ainda não há histórico de remuneração. Envie Nov/25 e Dez/25 (mensal) e/ou dias de 2026 em diante (diário).")
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
        "Mês", "Faixa", "Qualificadas", "Deveria receber (cheio)", "Já pago (referência)", "A receber no mês"
    ]).sort_values("Mês", key=lambda col: col.map(month_key_str), ascending=True)

    view = dfm.copy()
    view["Qualificadas"] = view["Qualificadas"].apply(br_int)
    view["Deveria receber (cheio)"] = view["Deveria receber (cheio)"].apply(br_money)
    view["Já pago (referência)"] = view["Já pago (referência)"].apply(br_money)
    view["A receber no mês"] = view["A receber no mês"].apply(br_money)

    st.dataframe(view, use_container_width=True, hide_index=True)

    last = dfm.iloc[-1]
    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Último mês", str(last["Mês"]))
    a2.metric("Qualificadas", br_int(int(last["Qualificadas"])))
    a3.metric("Receita cheia", br_money(float(last["Deveria receber (cheio)"])))
    a4.metric("A receber", br_money(float(last["A receber no mês"])))
