import os
import io
import re
import json
import hashlib
import datetime as dt
from typing import Optional, Dict, Tuple, List

import pandas as pd
import streamlit as st

# ============================================================
# CONFIGURAÇÕES PRINCIPAIS
# ============================================================

APP_TITLE = "Assis & Mollerke"
APP_SUBTITLE = "Painel executivo — Visão Cliente C6 + Leads + Remuneração incremental"
LOGO_PATH = "LOGO CORRETA.png"  # arquivo no seu repositório (mesmo nome)

# A partir de quando guardar histórico (clientes somem do arquivo do dia seguinte)
HIST_START_DATE = dt.date(2026, 1, 1)

# Diretório de armazenamento local (Streamlit Cloud mantém entre execuções)
DATA_DIR = "data_store"
os.makedirs(DATA_DIR, exist_ok=True)

# Arquivos de histórico (CSV simples e robusto)
HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_aberturas_diario.csv")   # dia, abertas
HIST_LEADS_DAILY = os.path.join(DATA_DIR, "hist_cadastros_diario.csv")  # dia, cadastradas
HIST_SNAP_LATEST = os.path.join(DATA_DIR, "latest_snapshot.json")
HIST_SNAP_PREV = os.path.join(DATA_DIR, "prev_snapshot.json")

# Remuneração incremental (por CNPJ ao longo dos meses)
HIST_PAYMENTS = os.path.join(DATA_DIR, "hist_pagamentos_incremental.json")  # {cnpj: max_pago_ate_agora}

# ============================================================
# NOMES DE COLUNAS (quando existirem por nome)
# ============================================================

# Planilha diária (Visão Cliente)
COL_DATA_CONTA = "DT_CONTA_CRIADA"                 # data de abertura da conta (T)
COL_FUNDACAO = "DT_FUNDACAO_EMPRESA"               # data fundação (P)
COL_PIX_TIPO = "CHAVES_PIX_FORTE"                  # tipo de chave pix
COL_SALDO = "VL_SALDO_MEDIO_MENSALIZADO"           # saldo
COL_STATUS = "STATUS_CC"                           # status
COL_DOMICILIO = "BANCO_DOMICILIO"                  # banco domicílio
COL_CNPJ = "CD_CPF_CNPJ_CLIENTE"                   # CNPJ/CPF (você confirmou)

# Critérios (pode vir com nome — nos antigos vem assim)
COL_CRITERIOS_NOME = "CRITERIOS_ATINGIDOS_COMISS"

# Planilha Leads (cadastros) — você falou: COLUNA M = data do cadastro.
# Como o nome pode variar, vamos localizar por posição (M = 13) se não acharmos por nome.
# Se existir um nome padrão, você pode colocar aqui depois.
LEADS_DATE_NAME_CANDIDATES = ["DATA_CADASTRO", "DT_CADASTRO", "DATA", "DT"]

# ============================================================
# TABELA DE REMUNERAÇÃO POR FAIXA (conforme sua regra)
# ============================================================
# Faixa definida pela quantidade total de qualificadas do mês.
# Cada faixa define o valor cheio por nível.
PAYOUT_TIERS = [
    # (min_qualificadas, multiplicador_label, {nivel: valor})
    (0,   "1.0 (até 49)",   {1: 140.00, 2: 230.00, 3: 400.00, 4: 540.00}),
    (50,  "1.1 (50 a 149)", {1: 154.00, 2: 253.00, 3: 440.00, 4: 594.00}),
    (150, "1.25 (150 a 349)", {1: 175.00, 2: 287.50, 3: 500.00, 4: 675.00}),
    (350, "1.5 (350+)",     {1: 210.00, 2: 345.00, 3: 600.00, 4: 810.00}),
]

# ============================================================
# FUNÇÕES UTILITÁRIAS
# ============================================================

def br_money(x: float) -> str:
    try:
        s = f"{float(x):,.2f}"
        return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "R$ 0,00"

def br_int(n: int) -> str:
    try:
        return f"{int(n):,}".replace(",", ".")
    except Exception:
        return "0"

def br_date(d: Optional[dt.date]) -> str:
    if not d:
        return "-"
    return d.strftime("%d/%m/%Y")

def sha_bytes(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()

def to_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.date

def normalize_str(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").str.strip()

def read_excel_bytes(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")

def excel_col_letter_to_index(letter: str) -> int:
    # A->1, B->2... Z->26, AA->27... BY->77
    letter = letter.upper().strip()
    num = 0
    for ch in letter:
        if "A" <= ch <= "Z":
            num = num * 26 + (ord(ch) - ord("A") + 1)
    return num  # 1-based

def get_col_by_excel_letter(df: pd.DataFrame, letter: str) -> Optional[str]:
    idx_1based = excel_col_letter_to_index(letter)
    idx_0 = idx_1based - 1
    if 0 <= idx_0 < len(df.columns):
        return df.columns[idx_0]
    return None

def safe_get_column(df: pd.DataFrame, col_name: str) -> pd.Series:
    if col_name in df.columns:
        return df[col_name]
    return pd.Series([pd.NA] * len(df))

def contains_c6(val: str) -> bool:
    return "c6" in str(val).lower()

# ============================================================
# REGRAS DE CRITÉRIOS (QUALIFICAÇÃO E NÍVEL)
# ============================================================

CRIT_PATTERN = re.compile(r":\s*(\d+)")

def extract_max_level_from_criteria(val) -> int:
    """
    Retorna:
      - 0 se não qualifica (0, vazio, texto sem números 1..4)
      - 1..4 (maior valor encontrado) se qualifica
    """
    if val is None:
        return 0
    t = str(val).strip().upper()
    if t == "" or t in {"0", "NAN", "NONE", "-"}:
        return 0

    nums = [int(x) for x in CRIT_PATTERN.findall(t)]
    if not nums:
        return 0

    # Considera apenas 1..4
    nums = [n for n in nums if 1 <= n <= 4]
    if not nums:
        return 0
    return max(nums)

def find_criteria_column(df: pd.DataFrame) -> str:
    """
    Prioridade:
      1) coluna nomeada CRITERIOS_ATINGIDOS_COMISS (para Nov/Dez e quando vier por nome)
      2) senão, usa a coluna BY (posição 77 no Excel)
    """
    if COL_CRITERIOS_NOME in df.columns:
        return COL_CRITERIOS_NOME

    by_col = get_col_by_excel_letter(df, "BY")
    if by_col is not None:
        return by_col

    # Se não achar, devolve um "fake" para não quebrar (vai dar tudo 0)
    return "__CRITERIOS_NAO_ENCONTRADO__"

# ============================================================
# PIX (com e sem)
# ============================================================

def pix_summary(df: pd.DataFrame) -> Tuple[int, int, pd.DataFrame]:
    s = normalize_str(safe_get_column(df, COL_PIX_TIPO)).str.upper()
    s = s.str.replace("'", "", regex=False)

    # Regra: tem pix se não for vazio e não for "-"
    has = ~s.isin(["", "-", "NAN", "NONE", "SEM", "SEM PIX"])
    com = int(has.sum())
    sem = int((~has).sum())

    dist = (
        s[has]
        .value_counts()
        .rename_axis("Tipo de chave")
        .reset_index(name="Quantidade")
    )
    return com, sem, dist

# ============================================================
# ABERTURAS (contas abertas) — por dia e por mês
# ============================================================

def openings_daily_monthly(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, int]:
    s = to_date_series(safe_get_column(df, COL_DATA_CONTA))
    s = s.dropna()
    total = int(len(s))

    by_day = (
        s.value_counts()
        .sort_index()
        .rename_axis("Dia")
        .reset_index(name="Contas abertas")
    )

    t = pd.to_datetime(s, errors="coerce")
    by_month = (
        t.dt.to_period("M")
        .astype(str)
        .value_counts()
        .sort_index()
        .rename_axis("Mês")
        .reset_index(name="Contas abertas")
    )
    return by_day, by_month, total

def fundacao_month_distribution_for_day(df: pd.DataFrame, day: dt.date) -> pd.DataFrame:
    d_open = to_date_series(safe_get_column(df, COL_DATA_CONTA))
    d_fund = to_date_series(safe_get_column(df, COL_FUNDACAO))

    aux = pd.DataFrame({"abertura": d_open, "fundacao": d_fund})
    aux = aux[(aux["abertura"] == day) & aux["fundacao"].notna()].copy()
    if aux.empty:
        return pd.DataFrame(columns=["Mês de fundação", "Quantidade"])

    # mês de referência da fundação (mm/aaaa)
    mf = pd.to_datetime(aux["fundacao"]).dt.to_period("M").astype(str)
    out = (
        mf.value_counts()
        .sort_index()
        .rename_axis("Mês de fundação")
        .reset_index(name="Quantidade")
    )
    return out

# ============================================================
# STATUS
# ============================================================

def status_counts(df: pd.DataFrame) -> pd.DataFrame:
    s = normalize_str(safe_get_column(df, COL_STATUS))
    s = s.replace("", "Sem status")
    return (
        s.value_counts()
        .rename_axis("Status")
        .reset_index(name="Quantidade")
    )

# ============================================================
# DOMICÍLIO C6
# ============================================================

def domicilio_c6_count(df: pd.DataFrame) -> int:
    s = normalize_str(safe_get_column(df, COL_DOMICILIO))
    return int(s.apply(contains_c6).sum())

# ============================================================
# QUALIFICADAS / RECEITA (NÍVEL VITORIOSO)
# ============================================================

def qualified_levels_table(df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
    crit_col = find_criteria_column(df)
    crit = safe_get_column(df, crit_col)

    levels = crit.apply(extract_max_level_from_criteria)
    dfq = df.copy()
    dfq["Nivel"] = levels

    dfq = dfq[dfq["Nivel"] >= 1].copy()
    total_qual = int(len(dfq))

    dist = (
        dfq["Nivel"].value_counts()
        .sort_index()
        .rename_axis("Nível")
        .reset_index(name="Quantidade")
    )
    return dist, total_qual

def tier_for_qualified_count(qty: int) -> Tuple[str, Dict[int, float]]:
    # pega a maior faixa cujo min_qualificadas <= qty
    chosen_label = PAYOUT_TIERS[0][1]
    chosen_map = PAYOUT_TIERS[0][2]
    for min_q, label, mapping in PAYOUT_TIERS:
        if qty >= min_q:
            chosen_label = label
            chosen_map = mapping
    return chosen_label, chosen_map

# ============================================================
# HISTÓRICO (SALVAR ABERTURAS / CADASTROS)
# ============================================================

def upsert_hist(csv_path: str, key_col: str, df_new: pd.DataFrame) -> pd.DataFrame:
    """
    Une histórico existente com df_new pelo key_col.
    df_new deve conter key_col + demais colunas (ex.: abertas).
    """
    if os.path.exists(csv_path):
        base = pd.read_csv(csv_path)
    else:
        base = pd.DataFrame(columns=df_new.columns)

    # garante tipos
    if key_col in base.columns:
        base[key_col] = pd.to_datetime(base[key_col], errors="coerce").dt.date
    if key_col in df_new.columns:
        df_new[key_col] = pd.to_datetime(df_new[key_col], errors="coerce").dt.date

    # remove duplicados pelo dia (mantém o mais novo)
    merged = pd.concat([base, df_new], ignore_index=True)
    merged = merged.dropna(subset=[key_col])
    merged = merged.sort_values(key_col)
    merged = merged.drop_duplicates(subset=[key_col], keep="last")

    # filtra a partir de HIST_START_DATE
    merged = merged[merged[key_col] >= HIST_START_DATE].copy()

    merged.to_csv(csv_path, index=False)
    return merged

def load_hist(csv_path: str, key_col: str) -> pd.DataFrame:
    if not os.path.exists(csv_path):
        return pd.DataFrame(columns=[key_col])
    df = pd.read_csv(csv_path)
    df[key_col] = pd.to_datetime(df[key_col], errors="coerce").dt.date
    df = df.dropna(subset=[key_col]).sort_values(key_col)
    df = df[df[key_col] >= HIST_START_DATE].copy()
    return df

# ============================================================
# LEADS (cadastros) — conta por dia e por mês
# ============================================================

def detect_leads_date_column(df: pd.DataFrame) -> str:
    # 1) tenta por nomes candidatos
    cols_up = {c.upper(): c for c in df.columns}
    for cand in LEADS_DATE_NAME_CANDIDATES:
        if cand.upper() in cols_up:
            return cols_up[cand.upper()]

    # 2) fallback: coluna M (13)
    m_col = get_col_by_excel_letter(df, "M")
    if m_col is not None:
        return m_col

    return "__LEADS_DATA_NAO_ENCONTRADA__"

def leads_daily_monthly(df_leads: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, int]:
    date_col = detect_leads_date_column(df_leads)
    s = to_date_series(safe_get_column(df_leads, date_col)).dropna()
    total = int(len(s))

    by_day = (
        s.value_counts()
        .sort_index()
        .rename_axis("Dia")
        .reset_index(name="Contas cadastradas")
    )
    t = pd.to_datetime(s, errors="coerce")
    by_month = (
        t.dt.to_period("M")
        .astype(str)
        .value_counts()
        .sort_index()
        .rename_axis("Mês")
        .reset_index(name="Contas cadastradas")
    )
    return by_day, by_month, total

# ============================================================
# SNAPSHOT (HOJE VS ONTEM) — apenas para resumo diário rápido
# ============================================================

def save_snapshot(file_hash: str, metrics: Dict, tag: str):
    payload = {
        "saved_at": dt.datetime.now().isoformat(),
        "file_hash": file_hash,
        "tag": tag,
        "metrics": metrics,
    }
    # move latest -> prev
    if os.path.exists(HIST_SNAP_LATEST):
        with open(HIST_SNAP_LATEST, "r", encoding="utf-8") as f:
            old = f.read()
        with open(HIST_SNAP_PREV, "w", encoding="utf-8") as f:
            f.write(old)

    with open(HIST_SNAP_LATEST, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def load_snapshots() -> Tuple[Optional[dict], Optional[dict]]:
    prev = latest = None
    if os.path.exists(HIST_SNAP_LATEST):
        with open(HIST_SNAP_LATEST, "r", encoding="utf-8") as f:
            latest = json.load(f)
    if os.path.exists(HIST_SNAP_PREV):
        with open(HIST_SNAP_PREV, "r", encoding="utf-8") as f:
            prev = json.load(f)
    return prev, latest

# ============================================================
# REMUNERAÇÃO INCREMENTAL (POR MÊS)
# ============================================================

def parse_month_from_filename(name: str) -> Optional[str]:
    # tenta achar "NOVEMBRO2025" etc
    base = os.path.splitext(os.path.basename(name))[0].upper()
    # se tiver YYYYMM ou YYYY-MM
    m = re.search(r"(20\d{2})[^\d]?(0[1-9]|1[0-2])", base)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    # mapas de mês por nome PT
    months = {
        "JANEIRO": "01", "FEVEREIRO": "02", "MARCO": "03", "MARÇO": "03",
        "ABRIL": "04", "MAIO": "05", "JUNHO": "06", "JULHO": "07",
        "AGOSTO": "08", "SETEMBRO": "09", "OUTUBRO": "10", "NOVEMBRO": "11", "DEZEMBRO": "12"
    }
    y = re.search(r"(20\d{2})", base)
    for k, v in months.items():
        if k in base and y:
            return f"{y.group(1)}-{v}"
    return None

def load_paid_history() -> Dict[str, float]:
    if not os.path.exists(HIST_PAYMENTS):
        return {}
    with open(HIST_PAYMENTS, "r", encoding="utf-8") as f:
        return json.load(f)

def save_paid_history(hist: Dict[str, float]):
    with open(HIST_PAYMENTS, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)

def normalize_cnpj(v) -> str:
    s = str(v).strip()
    s = re.sub(r"\D", "", s)
    return s

def remun_month_calc(df_month: pd.DataFrame) -> Tuple[pd.DataFrame, Dict]:
    """
    Retorna:
      - detalhe por CNPJ com: nível, valor cheio, já pago, incremental (>=0)
      - resumo do mês
    """
    # CNPJ
    if COL_CNPJ not in df_month.columns:
        df_month[COL_CNPJ] = pd.NA
    cnpj = df_month[COL_CNPJ].apply(normalize_cnpj)

    # Critérios
    crit_col = find_criteria_column(df_month)
    crit = safe_get_column(df_month, crit_col)

    # nível vitorioso por linha
    lvl = crit.apply(extract_max_level_from_criteria)

    aux = pd.DataFrame({
        "CNPJ": cnpj,
        "Nivel": lvl,
        "Criterios": crit.astype("string").fillna("").astype(str),
    })

    # remove sem CNPJ
    aux = aux[aux["CNPJ"].str.len() > 0].copy()

    # só qualificados
    auxq = aux[aux["Nivel"] >= 1].copy()

    # nível vitorioso por cliente (se tiver repetido)
    auxq = (
        auxq.groupby("CNPJ", as_index=False)
            .agg(Nivel=("Nivel", "max"),
                 Criterios=("Criterios", "first"))
    )

    total_qual = int(len(auxq))
    tier_label, payout_map = tier_for_qualified_count(total_qual)

    auxq["Valor_cheio"] = auxq["Nivel"].map(payout_map).fillna(0.0).astype(float)

    # incremental por CNPJ
    paid_hist = load_paid_history()
    ja_pago = []
    incremental = []
    novo_hist = dict(paid_hist)  # copia

    for _, row in auxq.iterrows():
        c = row["CNPJ"]
        full = float(row["Valor_cheio"])
        prev_paid = float(paid_hist.get(c, 0.0))
        inc = max(0.0, full - prev_paid)
        ja_pago.append(prev_paid)
        incremental.append(inc)
        # atualiza histórico para o MAIOR valor cheio já atingido
        novo_hist[c] = max(prev_paid, full)

    auxq["Ja_pago_ate_agora"] = ja_pago
    auxq["Incremental_no_mes"] = incremental

    # salva histórico atualizado
    save_paid_history(novo_hist)

    resumo = {
        "qualificadas": total_qual,
        "faixa": tier_label,
        "receita_cheia": float(auxq["Valor_cheio"].sum()),
        "receita_incremental": float(auxq["Incremental_no_mes"].sum()),
        "cnpjs_no_mes": int(auxq["CNPJ"].nunique()),
        "nivel_dist": auxq["Nivel"].value_counts().sort_index().to_dict(),
    }

    # ordena para visualização
    auxq = auxq.sort_values(["Nivel", "Incremental_no_mes"], ascending=[False, False])

    return auxq, resumo

# ============================================================
# LOGIN SIMPLES
# ============================================================

def login_gate() -> bool:
    st.sidebar.markdown("### Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")
    if st.sidebar.button("Entrar"):
        st.session_state["logged_in"] = (u == "admin" and p == "123456")
        if not st.session_state["logged_in"]:
            st.sidebar.error("Usuário ou senha inválidos.")
    return bool(st.session_state.get("logged_in", False))

# ============================================================
# UI — ESTILO LIMPO + CORES PRÓXIMAS DA MARCA
# ============================================================

st.set_page_config(page_title=APP_TITLE, layout="wide")

# CSS básico (sem exagero)
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.2rem; padding-bottom: 2.5rem; }
      [data-testid="stMetricValue"] { font-size: 1.6rem; }
      .am-chip-ok {
        display:inline-block; padding:4px 10px; border-radius: 999px;
        background:#E8F0FE; color:#1A4FD6; font-weight:600; font-size:0.92rem;
      }
      .am-chip-bad {
        display:inline-block; padding:4px 10px; border-radius: 999px;
        background:#FDE8E8; color:#B42318; font-weight:700; font-size:0.92rem;
      }
      .am-title { font-weight:800; font-size:2.1rem; }
      .am-sub { color:#5b6472; margin-top:-6px; }
      .am-card { border:1px solid #eef0f4; border-radius:18px; padding:14px 16px; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Header com logo
h1, h2 = st.columns([1, 5])
with h1:
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, use_container_width=True)
with h2:
    st.markdown(f"<div class='am-title'>{APP_TITLE}</div>", unsafe_allow_html=True)
    st.markdown(f"<div class='am-sub'>{APP_SUBTITLE}</div>", unsafe_allow_html=True)

st.divider()

if not login_gate():
    st.stop()

# ============================================================
# UPLOADS
# ============================================================

st.markdown("## Importação do dia")
cA, cB = st.columns(2)

with cA:
    up_c6 = st.file_uploader("Planilha C6 (Visão Cliente) — diária (.xlsx)", type=["xlsx"], key="c6_daily")

with cB:
    up_leads = st.file_uploader("Planilha Leads — diária (.xlsx)", type=["xlsx"], key="leads_daily")

st.markdown("## Importação mensal (Remuneração incremental)")
up_months = st.file_uploader(
    "Envie 1 ou mais arquivos mensais (Nov/25 em diante) — você pode enviar vários de uma vez",
    type=["xlsx"],
    accept_multiple_files=True,
    key="months_upload",
)

st.divider()

# ============================================================
# PROCESSAMENTO DIÁRIO (C6)
# ============================================================

daily_metrics = None
daily_details = {}

if up_c6 is not None:
    b = up_c6.getvalue()
    df = read_excel_bytes(b)

    # Coerções mínimas
    df[COL_DATA_CONTA] = to_date_series(safe_get_column(df, COL_DATA_CONTA))
    df[COL_FUNDACAO] = to_date_series(safe_get_column(df, COL_FUNDACAO))
    df[COL_PIX_TIPO] = normalize_str(safe_get_column(df, COL_PIX_TIPO))
    df[COL_STATUS] = normalize_str(safe_get_column(df, COL_STATUS))
    df[COL_DOMICILIO] = normalize_str(safe_get_column(df, COL_DOMICILIO))
    df[COL_SALDO] = pd.to_numeric(safe_get_column(df, COL_SALDO), errors="coerce").fillna(0.0)

    # Aberturas
    open_day, open_month, open_total = openings_daily_monthly(df)

    # Atualiza histórico diário (somente datas >= 01/01/2026)
    open_hist_new = open_day.rename(columns={"Dia": "dia", "Contas abertas": "abertas"}).copy()
    open_hist_new = open_hist_new[open_hist_new["dia"] >= HIST_START_DATE]
    upsert_hist(HIST_OPEN_DAILY, "dia", open_hist_new)

    # Pix
    pix_com, pix_sem, pix_dist = pix_summary(df)

    # Saldo
    saldo_total = float(df[COL_SALDO].sum())

    # Status
    st_dist = status_counts(df)

    # Domicílio C6
    dom_c6 = domicilio_c6_count(df)

    # Qualificadas por critérios (coluna BY ou CRITERIOS_ATINGIDOS_COMISS)
    level_dist, total_qual = qualified_levels_table(df)
    tier_label, payout_map = tier_for_qualified_count(total_qual)

    # Receita cheia estimada (DIÁRIA apenas como indicador, sem incremental)
    # Regra: por linha, nível vitorioso (>=1). Se tiver repetido por CNPJ, aqui é apenas indicador do arquivo,
    # então somamos por linha qualificada.
    crit_col = find_criteria_column(df)
    lvl_line = safe_get_column(df, crit_col).apply(extract_max_level_from_criteria)
    receita_estimada = float(pd.Series(lvl_line).map(payout_map).fillna(0.0).sum())

    daily_metrics = {
        "abertas_total": int(open_total),
        "saldo_total": float(saldo_total),
        "pix_com": int(pix_com),
        "pix_sem": int(pix_sem),
        "dom_c6": int(dom_c6),
        "qualificadas": int(total_qual),
        "receita_estimada": float(receita_estimada),
        "arquivo": up_c6.name,
    }

    # snapshot hoje vs ontem
    save_snapshot(file_hash=sha_bytes(b), metrics=daily_metrics, tag=up_c6.name)

    daily_details = {
        "open_day": open_day,
        "open_month": open_month,
        "pix_dist": pix_dist,
        "status_dist": st_dist,
        "level_dist": level_dist,
        "df": df,
    }

# ============================================================
# PROCESSAMENTO DIÁRIO (LEADS) + HISTÓRICO + PERCENTUAL
# ============================================================

leads_metrics = None
leads_details = {}

if up_leads is not None:
    b = up_leads.getvalue()
    dfL = read_excel_bytes(b)

    date_col = detect_leads_date_column(dfL)
    dfL[date_col] = to_date_series(safe_get_column(dfL, date_col))

    leads_day, leads_month, leads_total = leads_daily_monthly(dfL)

    # Atualiza histórico de cadastros (somente >= 01/01/2026)
    leads_hist_new = leads_day.rename(columns={"Dia": "dia", "Contas cadastradas": "cadastradas"}).copy()
    leads_hist_new = leads_hist_new[leads_hist_new["dia"] >= HIST_START_DATE]
    upsert_hist(HIST_LEADS_DAILY, "dia", leads_hist_new)

    leads_metrics = {
        "cadastradas_total": int(leads_total),
        "arquivo": up_leads.name,
    }

    leads_details = {
        "leads_day": leads_day,
        "leads_month": leads_month,
        "date_col": date_col,
    }

# ============================================================
# RESUMO EXECUTIVO (DIA)
# ============================================================

st.markdown("## Resumo executivo (dia)")

prev_snap, latest_snap = load_snapshots()

if latest_snap and latest_snap.get("metrics"):
    m = latest_snap["metrics"]

    r1 = st.columns(4)
    r1[0].metric("Contas abertas (arquivo)", br_int(m.get("abertas_total", 0)))
    r1[1].metric("Saldo total", br_money(m.get("saldo_total", 0.0)))
    r1[2].metric("Clientes com Pix", br_int(m.get("pix_com", 0)))
    r1[3].metric("Clientes sem Pix", br_int(m.get("pix_sem", 0)))

    r2 = st.columns(4)
    r2[0].metric("Domicílio C6", br_int(m.get("dom_c6", 0)))
    r2[1].metric("Contas qualificadas (arquivo)", br_int(m.get("qualificadas", 0)))
    r2[2].metric("Receita estimada (arquivo)", br_money(m.get("receita_estimada", 0.0)))
    r2[3].metric("Arquivo", m.get("arquivo", "-"))

    if prev_snap and prev_snap.get("metrics"):
        st.markdown("### Evolução (hoje vs último envio)")
        pm = prev_snap["metrics"]
        d = st.columns(4)
        d[0].metric("Δ Abertas", f"{int(m.get('abertas_total',0) - pm.get('abertas_total',0)):+,}".replace(",", "."))
        d[1].metric("Δ Saldo", br_money(m.get("saldo_total",0.0) - pm.get("saldo_total",0.0)))
        d[2].metric("Δ Qualificadas", f"{int(m.get('qualificadas',0) - pm.get('qualificadas',0)):+,}".replace(",", "."))
        d[3].metric("Δ Receita estimada", br_money(m.get("receita_estimada",0.0) - pm.get("receita_estimada",0.0)))
else:
    st.info("Envie a planilha C6 do dia para o painel gerar o resumo.")

st.divider()

# ============================================================
# CADASTRO x ABERTURA — HISTÓRICO A PARTIR DE JAN/2026
# ============================================================

st.markdown("## Conversão — Cadastros x Aberturas (histórico)")

open_hist = load_hist(HIST_OPEN_DAILY, "dia")
leads_hist = load_hist(HIST_LEADS_DAILY, "dia")

if open_hist.empty or leads_hist.empty:
    st.info("Envie as duas planilhas (C6 e Leads) de um dia para começar a formar o histórico a partir de 01/01/2026.")
else:
    base = pd.merge(leads_hist, open_hist, on="dia", how="outer").fillna(0)
    base["cadastradas"] = base["cadastradas"].astype(int)
    base["abertas"] = base["abertas"].astype(int)
    base = base.sort_values("dia")

    # percentual correto: abertas / cadastradas
    base["percentual"] = base.apply(
        lambda r: (r["abertas"] / r["cadastradas"] * 100.0) if r["cadastradas"] > 0 else 0.0,
        axis=1
    )

    # visão diária
    base_view = base.copy()
    base_view["Dia"] = base_view["dia"].apply(br_date)
    base_view["Cadastradas"] = base_view["cadastradas"].astype(int)
    base_view["Abertas"] = base_view["abertas"].astype(int)
    base_view["% Abertas/Cadastradas"] = base_view["percentual"].map(lambda x: f"{x:.1f}%".replace(".", ","))

    def chip(pct: float) -> str:
        # regra interna (não mostrar "≥20% azul ...")
        if pct >= 20.0:
            return "<span class='am-chip-ok'>Dentro do alvo</span>"
        return "<span class='am-chip-bad'>Abaixo do alvo</span>"

    base_view["Indicador"] = base_view["percentual"].apply(chip)

    # resumo do mês atual (pelo histórico)
    base["mes"] = pd.to_datetime(base["dia"]).dt.to_period("M").astype(str)
    mes_sel = st.selectbox("Selecione o mês (histórico)", sorted(base["mes"].unique()), index=len(sorted(base["mes"].unique()))-1)

    mdf = base[base["mes"] == mes_sel].copy()
    cad_mes = int(mdf["cadastradas"].sum())
    ab_mes = int(mdf["abertas"].sum())
    pct_mes = (ab_mes / cad_mes * 100.0) if cad_mes > 0 else 0.0

    colm = st.columns(4)
    colm[0].metric("Cadastradas no mês", br_int(cad_mes))
    colm[1].metric("Abertas no mês", br_int(ab_mes))
    colm[2].metric("% Abertas/Cadastradas (mês)", f"{pct_mes:.1f}%".replace(".", ","))
    colm[3].metric("Mês", mes_sel)

    st.markdown("### Diário (histórico)")
    st.dataframe(
        base_view[["Dia", "Cadastradas", "Abertas", "% Abertas/Cadastradas", "Indicador"]],
        use_container_width=True,
        hide_index=True
    )
    st.markdown(
        "<div style='color:#5b6472; font-size:0.9rem;'>Observação: o histórico é guardado a partir de 01/01/2026 mesmo que o cliente não apareça no dia seguinte.</div>",
        unsafe_allow_html=True
    )

st.divider()

# ============================================================
# DETALHES (DIÁRIO)
# ============================================================

st.markdown("## Relatórios (diário)")

if up_c6 is None:
    st.info("Envie a planilha C6 do dia para ver os relatórios detalhados.")
else:
    tab1, tab2, tab3, tab4 = st.tabs([
        "Aberturas",
        "Fundações (por dia)",
        "Pix e Status",
        "Qualificação (níveis e critérios)",
    ])

    with tab1:
        st.markdown("### Contas abertas por dia (arquivo)")
        df1 = daily_details["open_day"].copy()
        df1["Dia"] = df1["Dia"].apply(br_date)
        st.dataframe(df1[["Dia", "Contas abertas"]], use_container_width=True, hide_index=True)

        st.markdown("### Contas abertas por mês (arquivo)")
        st.dataframe(daily_details["open_month"], use_container_width=True, hide_index=True)

    with tab2:
        st.markdown("### Distribuição do mês de fundação — por dia de abertura")
        open_day = daily_details["open_day"].copy()
        if open_day.empty:
            st.info("Não há datas de abertura válidas no arquivo.")
        else:
            days = open_day["Dia"].dropna().tolist()
            # seleciona dia (drill-down)
            day_sel = st.selectbox(
                "Selecione o dia de abertura",
                options=days,
                format_func=lambda d: br_date(d) if isinstance(d, dt.date) else str(d)
            )
            dist = fundacao_month_distribution_for_day(daily_details["df"], day_sel)
            if dist.empty:
                st.info("Sem datas de fundação para esse dia.")
            else:
                st.dataframe(dist, use_container_width=True, hide_index=True)

    with tab3:
        st.markdown("### Pix — distribuição por tipo de chave")
        com, sem, dist = pix_summary(daily_details["df"])
        c1, c2 = st.columns(2)
        c1.metric("Clientes com Pix", br_int(com))
        c2.metric("Clientes sem Pix", br_int(sem))
        st.dataframe(dist, use_container_width=True, hide_index=True)

        st.markdown("### Status — distribuição")
        st.dataframe(daily_details["status_dist"], use_container_width=True, hide_index=True)

    with tab4:
        st.markdown("### Distribuição de nível (vitorioso)")
        st.dataframe(daily_details["level_dist"], use_container_width=True, hide_index=True)

        st.markdown("### Critérios — exemplos (para auditoria)")
        # Mostra amostra de critérios e nível vitorioso (sem expor "coluna BY")
        df_show = daily_details["df"].copy()
        crit_col = find_criteria_column(df_show)
        df_show["Nivel"] = safe_get_column(df_show, crit_col).apply(extract_max_level_from_criteria)
        df_show = df_show[df_show["Nivel"] >= 1].copy()

        if df_show.empty:
            st.info("Nenhuma conta qualificada pelo critério (1 a 4) no arquivo.")
        else:
            # colunas elegantes
            cols = []
            if COL_CNPJ in df_show.columns:
                cols.append(COL_CNPJ)
            cols += [COL_DATA_CONTA, "Nivel", crit_col]

            out = df_show[cols].head(50).copy()
            out.rename(columns={
                COL_CNPJ: "CNPJ",
                COL_DATA_CONTA: "Data de abertura",
                crit_col: "Critérios",
                "Nivel": "Nível"
            }, inplace=True)

            out["Data de abertura"] = out["Data de abertura"].apply(br_date)
            st.dataframe(out, use_container_width=True, hide_index=True)

st.divider()

# ============================================================
# REMUNERAÇÃO INCREMENTAL — MENSAL
# ============================================================

st.markdown("## Remuneração incremental (mensal)")

if not up_months:
    st.info("Envie os arquivos mensais (ex.: NOVEMBRO2025.xlsx, DEZEMBRO2025.xlsx...) para calcular a remuneração incremental.")
else:
    # Processa em ordem por mês
    month_rows = []
    detail_by_month = {}

    # Ordenar arquivos pela data inferida do nome (se não achar, fica no final)
    def month_key(f):
        m = parse_month_from_filename(f.name) or "9999-99"
        return m

    files_sorted = sorted(up_months, key=month_key)

    for f in files_sorted:
        b = f.getvalue()
        dfm = read_excel_bytes(b)

        month_id = parse_month_from_filename(f.name) or f.name
        det, resumo = remun_month_calc(dfm)
        resumo["mes"] = month_id
        resumo["arquivo"] = f.name

        month_rows.append(resumo)
        detail_by_month[month_id] = det

    resumo_df = pd.DataFrame(month_rows)

    # organiza e formata
    if not resumo_df.empty:
        # ordena mês (quando estiver no formato YYYY-MM)
        def sort_month_val(x: str):
            m = re.match(r"^(\d{4})-(\d{2})$", str(x))
            if m:
                return int(m.group(1)) * 100 + int(m.group(2))
            return 999999

        resumo_df = resumo_df.sort_values("mes", key=lambda s: s.map(sort_month_val))

        resumo_view = resumo_df.copy()
        resumo_view.rename(columns={
            "mes": "Mês",
            "qualificadas": "Qualificadas",
            "faixa": "Faixa aplicada",
            "receita_cheia": "Receita cheia (mês)",
            "receita_incremental": "Receita incremental (mês)",
            "arquivo": "Arquivo",
        }, inplace=True)

        resumo_view["Receita cheia (mês)"] = resumo_view["Receita cheia (mês)"].apply(br_money)
        resumo_view["Receita incremental (mês)"] = resumo_view["Receita incremental (mês)"].apply(br_money)
        resumo_view["Qualificadas"] = resumo_view["Qualificadas"].apply(br_int)

        st.markdown("### Resumo por mês")
        st.dataframe(
            resumo_view[["Mês", "Qualificadas", "Faixa aplicada", "Receita cheia (mês)", "Receita incremental (mês)", "Arquivo"]],
            use_container_width=True,
            hide_index=True
        )

        # Selecionar mês para detalhes
        meses = resumo_df["mes"].tolist()
        mes_sel = st.selectbox("Ver detalhes do mês", options=meses, index=len(meses)-1)

        det = detail_by_month.get(mes_sel)
        if det is not None and not det.empty:
            det_view = det.copy()
            det_view["CNPJ"] = det_view["CNPJ"].astype(str)
            det_view["Valor_cheio"] = det_view["Valor_cheio"].apply(br_money)
            det_view["Ja_pago_ate_agora"] = det_view["Ja_pago_ate_agora"].apply(br_money)
            det_view["Incremental_no_mes"] = det_view["Incremental_no_mes"].apply(br_money)

            st.markdown("### Detalhe por CNPJ (incremental)")
            st.dataframe(
                det_view[["CNPJ", "Nivel", "Valor_cheio", "Ja_pago_ate_agora", "Incremental_no_mes", "Criterios"]],
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("Sem detalhes válidos para esse mês.")

    else:
        st.warning("Não foi possível calcular os meses enviados. Verifique se os arquivos possuem CNPJ e critérios.")

st.divider()

st.markdown(
    "<div style='color:#5b6472; font-size:0.9rem;'>"
    "Dica: se algo parecer diferente do esperado, o primeiro passo é conferir se o arquivo tem CNPJ preenchido e os critérios com números 1 a 4."
    "</div>",
    unsafe_allow_html=True
)
