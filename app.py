import os
import io
import json
import hashlib
import datetime as dt
from typing import Optional, Tuple, Dict

import pandas as pd
import streamlit as st

# =========================
# CONFIG GERAL
# =========================
START_DATE = dt.date(2026, 1, 1)  # memorizar a partir de 01/01/2026
META_PCT = 0.20                  # 20%

# =========================
# COLUNAS (PLANILHA C6)
# =========================
COL_T = "DT_CONTA_CRIADA"                 # abertura
COL_P = "DT_FUNDACAO_EMPRESA"             # fundação
COL_X = "CHAVES_PIX_FORTE"                # tipo chave pix
COL_Y = "VL_SALDO_MEDIO_MENSALIZADO"      # saldo
COL_V = "STATUS_CC"                       # status
COL_AQ = "BANCO_DOMICILIO"                # domicílio
COL_BY = "FL_QUALIFICADO_COMISS"          # flag comissão (0/1)
COL_BR = "MES_REF_COMISS"                 # M0/M1/M2
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"   # critérios texto

COL_NOME = "NOME_CLIENTE"
COL_DOC  = "CD_CPF_CNPJ_CLIENTE"

# Pagamento por nível (1..4)
PAYOUT = {1: 210, 2: 345, 3: 600, 4: 810}

# =========================
# PLANILHA LEADS (CADASTRO)
# =========================
LEADS_DATE_COL_INDEX = 12  # coluna M (13ª)

# =========================
# PERSISTÊNCIA
# =========================
DATA_DIR = "data_uploads"
os.makedirs(DATA_DIR, exist_ok=True)

LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
PREV_PATH   = os.path.join(DATA_DIR, "prev.json")

HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_aberturas_diario.csv")
HIST_OPEN_MONTH = os.path.join(DATA_DIR, "hist_aberturas_mensal.csv")
HIST_CAD_DAILY  = os.path.join(DATA_DIR, "hist_cadastros_diario.csv")
HIST_CAD_MONTH  = os.path.join(DATA_DIR, "hist_cadastros_mensal.csv")

# =========================
# ESTILO
# =========================
NAVY = "#1f2852"
OK = "#1e88e5"
BAD = "#e53935"
SOFT_BG = "#f6f8ff"

st.set_page_config(page_title="Assis & Mollerke", layout="wide")

st.markdown(
    f"""
    <style>
      .block-container {{ padding-top: 1.2rem; padding-bottom: 2rem; }}
      .am-title {{ font-size: 28px; font-weight: 800; color: {NAVY}; margin: 0; }}
      .am-sub {{ color: #5b6280; margin-top: 4px; }}
      .chip {{ display:inline-block; padding:6px 10px; border-radius:999px; font-weight:700; font-size:12px; }}
      .chip-ok {{ background: rgba(30,136,229,.12); color:{OK}; border:1px solid rgba(30,136,229,.25); }}
      .chip-bad {{ background: rgba(229,57,53,.10); color:{BAD}; border:1px solid rgba(229,57,53,.22); }}
      div[data-testid="metric-container"] {{
        border: 1px solid #e7eaf6; padding: 14px 14px; border-radius: 14px; background: {SOFT_BG};
      }}
      thead th {{ text-align:left; border-bottom:1px solid #e7eaf6; padding:10px; color:{NAVY}; }}
      tbody td {{ border-bottom:1px solid #f0f2ff; padding:10px; }}
    </style>
    """,
    unsafe_allow_html=True,
)

# =========================
# FORMATAÇÃO
# =========================
def fmt_date_br(d) -> str:
    if pd.isna(d) or d is None:
        return ""
    if isinstance(d, dt.date):
        return d.strftime("%d/%m/%Y")
    dd = pd.to_datetime(d, errors="coerce")
    if pd.isna(dd):
        return ""
    return dd.strftime("%d/%m/%Y")

def fmt_month_br(s: str) -> str:
    if not isinstance(s, str) or len(s) < 7:
        return str(s)
    try:
        y, m = s[:4], s[5:7]
        return f"{m}/{y}"
    except Exception:
        return str(s)

def fmt_int(n) -> str:
    try:
        return f"{int(n):,}".replace(",", ".")
    except Exception:
        return "0"

def fmt_money(v: float) -> str:
    try:
        s = f"{float(v):,.2f}"
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {s}"
    except Exception:
        return "R$ 0,00"

def fmt_pct(p: float) -> str:
    try:
        return f"{p*100:.1f}%".replace(".", ",")
    except Exception:
        return "0,0%"

# =========================
# UTIL
# =========================
def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _load_excel(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")

def _safe_to_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.date

def _normalize_str(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").str.strip()

def _contains_c6(val: str) -> bool:
    return "c6" in str(val).lower()

# =========================
# HISTÓRICO (CSV)
# =========================
def _read_hist(path: str, key_col: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=[key_col, "valor"])
    df = pd.read_csv(path, dtype={key_col: "string"})
    if key_col not in df.columns:
        return pd.DataFrame(columns=[key_col, "valor"])
    df[key_col] = df[key_col].astype("string").fillna("").str.strip()
    df["valor"] = pd.to_numeric(df["valor"], errors="coerce").fillna(0).astype(int)
    df = df[df[key_col] != ""].copy()
    return df

def _write_hist(df: pd.DataFrame, path: str):
    df.to_csv(path, index=False)

def _sort_hist(df: pd.DataFrame, key_col: str) -> pd.DataFrame:
    if key_col == "dia":
        k = pd.to_datetime(df[key_col], errors="coerce")
        df = df.assign(_k=k).sort_values("_k").drop(columns=["_k"])
    elif key_col == "mes":
        k = pd.to_datetime(df[key_col] + "-01", errors="coerce")
        df = df.assign(_k=k).sort_values("_k").drop(columns=["_k"])
    else:
        df = df.sort_values(key_col)
    return df

def _upsert_hist(path: str, key_col: str, new_df: pd.DataFrame):
    base = _read_hist(path, key_col)

    base[key_col] = base[key_col].astype("string").fillna("").str.strip()
    new_df[key_col] = new_df[key_col].astype("string").fillna("").str.strip()

    base = base.set_index(key_col)
    new_df = new_df.set_index(key_col)

    base.update(new_df)
    missing = new_df.index.difference(base.index)
    if len(missing) > 0:
        base = pd.concat([base, new_df.loc[missing]], axis=0)

    base = base.reset_index()
    base = base[base[key_col] != ""].copy()
    base = _sort_hist(base, key_col)
    _write_hist(base, path)

# =========================
# COERCE C6
# =========================
def _coerce_c6_all(df: pd.DataFrame) -> pd.DataFrame:
    required = [COL_T, COL_P, COL_X, COL_Y, COL_V, COL_AQ, COL_BY, COL_BR, COL_CRIT]
    for c in required:
        if c not in df.columns:
            df[c] = pd.NA

    df[COL_T] = _safe_to_date_series(df[COL_T])
    df[COL_P] = _safe_to_date_series(df[COL_P])

    df[COL_X] = _normalize_str(df[COL_X])
    df[COL_V] = _normalize_str(df[COL_V])
    df[COL_AQ] = _normalize_str(df[COL_AQ])
    df[COL_BR] = _normalize_str(df[COL_BR])
    df[COL_CRIT] = _normalize_str(df[COL_CRIT])

    df[COL_BY] = pd.to_numeric(df[COL_BY], errors="coerce").fillna(0).astype(int)
    df[COL_Y] = pd.to_numeric(df[COL_Y], errors="coerce").fillna(0.0)

    # filtro desde 01/01/2026:
    # mantém linhas sem DT_CONTA_CRIADA (para não “sumir” coisas), mas corta se tiver data anterior
    mask = df[COL_T].isna() | (df[COL_T] >= START_DATE)
    df = df[mask].copy()
    return df

def _df_aberturas(df_all: pd.DataFrame) -> pd.DataFrame:
    dfo = df_all[df_all[COL_T].notna()].copy()
    dfo = dfo[dfo[COL_T] >= START_DATE].copy()
    return dfo

# =========================
# MÉTRICAS C6
# =========================
def _aberturas_por_dia(df_open: pd.DataFrame) -> pd.DataFrame:
    s = df_open[COL_T].dropna().apply(lambda d: d.strftime("%Y-%m-%d") if isinstance(d, dt.date) else "")
    s = s[s != ""]
    return s.value_counts().sort_index().rename_axis("dia").reset_index(name="valor")

def _aberturas_por_mes(df_open: pd.DataFrame) -> pd.DataFrame:
    t = pd.to_datetime(df_open[COL_T], errors="coerce")
    m = t.dropna().dt.to_period("M").astype(str)
    return m.value_counts().sort_index().rename_axis("mes").reset_index(name="valor")

def _pix_info(df_all: pd.DataFrame):
    s = df_all[COL_X].astype("string").fillna("").str.strip().str.upper()
    s = s.str.replace("'", "", regex=False)
    has_pix = ~s.isin(["", "-", "NAN", "NONE", "SEM", "SEM PIX"])
    qtd_com = int(has_pix.sum())
    qtd_sem = int((~has_pix).sum())
    por_chave = (
        s.loc[has_pix].value_counts()
        .rename_axis("Tipo de chave Pix")
        .reset_index(name="Quantidade")
    )
    return qtd_com, qtd_sem, por_chave

def _sum_saldo(df_all: pd.DataFrame) -> float:
    return float(df_all[COL_Y].sum())

def _status_counts(df_all: pd.DataFrame) -> pd.DataFrame:
    return (
        df_all[COL_V].fillna("SEM STATUS").replace("", "SEM STATUS")
        .value_counts()
        .rename_axis("Status")
        .reset_index(name="Quantidade")
    )

def _domicilio_c6_count(df_all: pd.DataFrame) -> int:
    return int(df_all[COL_AQ].fillna("").astype(str).apply(_contains_c6).sum())

# =========================
# QUALIFICAÇÃO (B) + RECEITA (MAIOR VALOR)
# =========================
def _parse_criterios_max(txt: str) -> Tuple[int, str]:
    """
    Retorna:
      - maior nível (0..4)
      - nome do critério "vitorioso"
    """
    if not isinstance(txt, str) or not txt.strip():
        return 0, "N/A"

    parts = [p.strip() for p in txt.upper().split("|")]
    best_val = 0
    best_name = "N/A"

    for p in parts:
        if ":" not in p:
            continue
        name, val = p.split(":", 1)
        name = name.strip()
        val = val.strip()
        try:
            n = int(val)
        except Exception:
            continue
        if n > best_val:
            best_val = n
            best_name = name

    best_val = max(0, min(best_val, 4))
    return best_val, best_name

def _qualificadas_B(df_all: pd.DataFrame) -> pd.DataFrame:
    """
    REGRA B:
      qualificada = (BY == 1) E (maior_criterio >= 1)
    """
    df = df_all.copy()
    parsed = df[COL_CRIT].apply(_parse_criterios_max)
    df["Critério principal"] = parsed.apply(lambda x: x[1])
    df["Nível principal"] = parsed.apply(lambda x: int(x[0]))
    df["Receita estimada"] = df["Nível principal"].apply(lambda n: PAYOUT.get(int(n), 0))

    dfq = df[(df[COL_BY] == 1) & (df["Nível principal"] >= 1)].copy()
    return dfq

def _payout_table_from_dfq(dfq: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
    if dfq.empty:
        return pd.DataFrame(columns=["Nível", "Quantidade", "Valor unitário", "Total"]), 0

    levels = dfq["Nível principal"].astype(int)
    counts = levels.value_counts().sort_index()
    rows = []
    for level, qty in counts.items():
        unit = PAYOUT.get(int(level), 0)
        total = int(qty) * int(unit)
        rows.append([int(level), int(qty), unit, total])

    tbl = pd.DataFrame(rows, columns=["Nível", "Quantidade", "Valor unitário", "Total"])
    total_payout = int(tbl["Total"].sum()) if not tbl.empty else 0
    return tbl, total_payout

def _crit_principal_table(dfq: pd.DataFrame) -> pd.DataFrame:
    if dfq.empty:
        return pd.DataFrame(columns=["Critério (principal)", "Quantidade"])
    return (
        dfq["Critério principal"].fillna("N/A").replace("", "N/A")
        .value_counts()
        .rename_axis("Critério (principal)")
        .reset_index(name="Quantidade")
    )

def _br_counts(dfq: pd.DataFrame) -> pd.DataFrame:
    if dfq.empty:
        return pd.DataFrame(columns=["Referência", "Quantidade"])
    s = dfq[COL_BR].fillna("").astype(str).str.upper().str.strip()
    return s.replace("", "SEM").value_counts().rename_axis("Referência").reset_index(name="Quantidade")

# =========================
# FUNDAÇÕES (MÊS/ANO POR DIA)
# =========================
def _fundacoes_mes_por_dia(df_open: pd.DataFrame, dia: dt.date) -> pd.DataFrame:
    x = df_open[df_open[COL_T] == dia][[COL_P]].dropna().copy()
    if x.empty:
        return pd.DataFrame(columns=["Mês de fundação", "Quantidade"])
    x["Mês de fundação"] = x[COL_P].apply(lambda d: d.strftime("%m/%Y") if isinstance(d, dt.date) else "")
    out = (
        x["Mês de fundação"].replace("", "SEM FUNDAÇÃO")
        .value_counts()
        .rename_axis("Mês de fundação")
        .reset_index(name="Quantidade")
    )
    return out

# =========================
# LEADS: CADASTROS
# =========================
def _load_leads_dates(df_leads: pd.DataFrame) -> Tuple[pd.DataFrame, str]:
    if df_leads.shape[1] <= LEADS_DATE_COL_INDEX:
        raise ValueError("A planilha de Leads não possui a coluna M (13ª coluna).")
    col_used = df_leads.columns[LEADS_DATE_COL_INDEX]
    s = pd.to_datetime(df_leads[col_used], errors="coerce").dt.date
    out = pd.DataFrame({"dia": s}).dropna()
    out = out[out["dia"] >= START_DATE].copy()
    return out, str(col_used)

def _cadastros_por_dia(df_dates: pd.DataFrame) -> pd.DataFrame:
    s = df_dates["dia"].dropna().apply(lambda d: d.strftime("%Y-%m-%d") if isinstance(d, dt.date) else "")
    s = s[s != ""]
    return s.value_counts().sort_index().rename_axis("dia").reset_index(name="valor")

def _cadastros_por_mes(df_dates: pd.DataFrame) -> pd.DataFrame:
    t = pd.to_datetime(df_dates["dia"], errors="coerce")
    m = t.dropna().dt.to_period("M").astype(str)
    return m.value_counts().sort_index().rename_axis("mes").reset_index(name="valor")

# =========================
# % ABERTAS SOBRE CADASTRADAS
# =========================
def _pct_daily_table() -> pd.DataFrame:
    o = _read_hist(HIST_OPEN_DAILY, "dia").rename(columns={"valor": "abertas"})
    c = _read_hist(HIST_CAD_DAILY, "dia").rename(columns={"valor": "cadastradas"})
    df = pd.merge(o, c, on="dia", how="outer").fillna(0)
    df["abertas"] = pd.to_numeric(df["abertas"], errors="coerce").fillna(0).astype(int)
    df["cadastradas"] = pd.to_numeric(df["cadastradas"], errors="coerce").fillna(0).astype(int)
    df["percentual"] = df.apply(lambda r: (r["abertas"] / r["cadastradas"]) if r["cadastradas"] > 0 else 0.0, axis=1)
    df = _sort_hist(df, "dia")
    return df

def _pct_month_table() -> pd.DataFrame:
    o = _read_hist(HIST_OPEN_MONTH, "mes").rename(columns={"valor": "abertas"})
    c = _read_hist(HIST_CAD_MONTH, "mes").rename(columns={"valor": "cadastradas"})
    df = pd.merge(o, c, on="mes", how="outer").fillna(0)
    df["abertas"] = pd.to_numeric(df["abertas"], errors="coerce").fillna(0).astype(int)
    df["cadastradas"] = pd.to_numeric(df["cadastradas"], errors="coerce").fillna(0).astype(int)
    df["percentual"] = df.apply(lambda r: (r["abertas"] / r["cadastradas"]) if r["cadastradas"] > 0 else 0.0, axis=1)
    df = _sort_hist(df, "mes")
    return df

def _chip_pct(p: float) -> str:
    if float(p) >= META_PCT:
        return f'<span class="chip chip-ok">OK • {fmt_pct(p)}</span>'
    return f'<span class="chip chip-bad">Atenção • {fmt_pct(p)}</span>'

# =========================
# SNAPSHOT (ontem x hoje)
# =========================
def _snapshot_to_disk(tag: str, file_hash: str, metrics: Dict):
    payload = {
        "tag": tag,
        "file_hash": file_hash,
        "saved_at": dt.datetime.now().isoformat(),
        "metrics": metrics,
    }
    if os.path.exists(LATEST_PATH):
        with open(LATEST_PATH, "r", encoding="utf-8") as f:
            old = f.read()
        with open(PREV_PATH, "w", encoding="utf-8") as f:
            f.write(old)

    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def _load_prev_latest() -> Tuple[Optional[dict], Optional[dict]]:
    latest = prev = None
    if os.path.exists(LATEST_PATH):
        with open(LATEST_PATH, "r", encoding="utf-8") as f:
            latest = json.load(f)
    if os.path.exists(PREV_PATH):
        with open(PREV_PATH, "r", encoding="utf-8") as f:
            prev = json.load(f)
    return prev, latest

# =========================
# LOGIN
# =========================
def login_gate():
    st.sidebar.markdown("### Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")
    if st.sidebar.button("Entrar"):
        st.session_state["logged_in"] = (u == "admin" and p == "123456")
        if not st.session_state["logged_in"]:
            st.sidebar.error("Usuário ou senha inválidos.")
    return st.session_state.get("logged_in", False)

# =========================
# TOPO (LOGO + TÍTULO)
# =========================
logo_candidates = ["LOGO CORRETA.png", "logo.png", "assets/logo.png", "LOGO_CORRETA.png"]
logo_found = next((p for p in logo_candidates if os.path.exists(p)), None)

header_l, header_r = st.columns([1, 3])
with header_l:
    if logo_found:
        st.image(logo_found, use_container_width=True)
with header_r:
    st.markdown('<p class="am-title">Assis & Mollerke</p>', unsafe_allow_html=True)
    st.markdown('<p class="am-sub">Painel executivo com memória desde 01/01/2026.</p>', unsafe_allow_html=True)

if not login_gate():
    st.stop()

# =========================
# IMPORTAÇÃO
# =========================
st.markdown("### Importação do dia")

col_up1, col_up2 = st.columns(2)
with col_up1:
    uploaded_c6 = st.file_uploader("Planilha C6 (aberturas)", type=["xlsx"], key="c6")
with col_up2:
    uploaded_leads = st.file_uploader("Planilha Leads (cadastros)", type=["xlsx"], key="leads")

prev, latest_saved = _load_prev_latest()

# variáveis
df_all = None
df_open = None

qtd_com_pix = qtd_sem_pix = 0
pix_por_chave = pd.DataFrame()
status_tbl = pd.DataFrame()

dfq = pd.DataFrame()
dfq_view = pd.DataFrame()
payout_tbl = pd.DataFrame()
crit_tbl = pd.DataFrame()
br_counts_tbl = pd.DataFrame()
total_payout = 0

# Processa C6
if uploaded_c6:
    file_bytes = uploaded_c6.getvalue()
    file_hash = _hash_bytes(file_bytes)

    df_raw = _load_excel(file_bytes)
    df_all = _coerce_c6_all(df_raw)
    df_open = _df_aberturas(df_all)

    # Históricos (aberturas)
    open_daily = _aberturas_por_dia(df_open)
    open_month = _aberturas_por_mes(df_open)
    _upsert_hist(HIST_OPEN_DAILY, "dia", open_daily)
    _upsert_hist(HIST_OPEN_MONTH, "mes", open_month)

    # Métricas gerais
    qtd_com_pix, qtd_sem_pix, pix_por_chave = _pix_info(df_all)
    saldo_total = _sum_saldo(df_all)
    status_tbl = _status_counts(df_all)
    qtd_c6 = _domicilio_c6_count(df_all)

    # Qualificadas (REGRA B)
    dfq = _qualificadas_B(df_all)
    dfq_view = dfq.copy()

    # Receita por nível (maior valor)
    payout_tbl, total_payout = _payout_table_from_dfq(dfq)
    crit_tbl = _crit_principal_table(dfq)
    br_counts_tbl = _br_counts(dfq)

    total_abertas_arquivo = int(df_open.shape[0])
    total_qualificadas = int(dfq.shape[0])

    metrics = {
        "total_abertas": total_abertas_arquivo,
        "qtd_com_pix": qtd_com_pix,
        "qtd_sem_pix": qtd_sem_pix,
        "saldo_total": float(saldo_total),
        "qtd_c6": int(qtd_c6),
        "total_qualificadas": int(total_qualificadas),
        "total_payout": int(total_payout),
    }

    _snapshot_to_disk(tag=uploaded_c6.name, file_hash=file_hash, metrics=metrics)
    prev, latest_saved = _load_prev_latest()

# Processa Leads
if uploaded_leads:
    try:
        df_leads = _load_excel(uploaded_leads.getvalue())
        df_dates, _ = _load_leads_dates(df_leads)

        cad_daily = _cadastros_por_dia(df_dates)
        cad_month = _cadastros_por_mes(df_dates)

        _upsert_hist(HIST_CAD_DAILY, "dia", cad_daily)
        _upsert_hist(HIST_CAD_MONTH, "mes", cad_month)
    except Exception as e:
        st.error(f"Não consegui ler a planilha de Leads. Motivo: {str(e)}")

st.divider()

# =========================
# RESUMO
# =========================
if latest_saved:
    m = latest_saved["metrics"]

    st.markdown("### Resumo executivo")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Contas abertas (arquivo)", fmt_int(m["total_abertas"]))
    c2.metric("Saldo total", fmt_money(m["saldo_total"]))
    c3.metric("Clientes com Pix", fmt_int(m["qtd_com_pix"]))
    c4.metric("Clientes sem Pix", fmt_int(m["qtd_sem_pix"]))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Domicílio C6", fmt_int(m["qtd_c6"]))
    c6.metric("Contas qualificadas", fmt_int(m["total_qualificadas"]))
    c7.metric("Receita estimada", fmt_money(m["total_payout"]))
    c8.metric("Arquivo processado", latest_saved.get("tag", "-"))

    st.markdown("### Variação vs arquivo anterior")
    if prev and prev.get("metrics"):
        pm = prev["metrics"]
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Abertas", fmt_int(m["total_abertas"]), delta=f"{m['total_abertas']-pm.get('total_abertas',0):+d}")
        d2.metric("Saldo", fmt_money(m["saldo_total"]), delta=fmt_money(m["saldo_total"]-pm.get("saldo_total",0.0)))
        d3.metric("Qualificadas", fmt_int(m["total_qualificadas"]), delta=f"{m['total_qualificadas']-pm.get('total_qualificadas',0):+d}")
        d4.metric("Receita", fmt_money(m["total_payout"]), delta=fmt_money(m["total_payout"]-pm.get("total_payout",0)))
    else:
        st.info("Envie pelo menos 2 dias para aparecer a variação.")
else:
    st.info("Envie a planilha C6 para gerar o resumo e alimentar o histórico.")

st.divider()

# =========================
# ABAS
# =========================
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "Aberturas (Histórico)",
    "Fundações (Detalhe do dia)",
    "Pix e Status",
    "Qualificadas e Receita",
    "Cadastro x Abertura",
])

with tab1:
    st.markdown("#### Aberturas acumuladas (desde 01/01/2026)")

    h_daily = _read_hist(HIST_OPEN_DAILY, "dia")
    if h_daily.empty:
        st.info("Sem histórico diário ainda.")
    else:
        show = h_daily.copy()
        show["Data"] = show["dia"].apply(fmt_date_br)
        show = show[["Data", "valor"]].rename(columns={"valor": "Contas abertas"})
        st.dataframe(show, use_container_width=True, hide_index=True)

    h_month = _read_hist(HIST_OPEN_MONTH, "mes")
    if h_month.empty:
        st.info("Sem histórico mensal ainda.")
    else:
        showm = h_month.copy()
        showm["Mês"] = showm["mes"].apply(fmt_month_br)
        showm = showm[["Mês", "valor"]].rename(columns={"valor": "Contas abertas"})
        st.dataframe(showm, use_container_width=True, hide_index=True)

with tab2:
    st.markdown("#### Fundações por dia (mês/ano)")
    if df_open is None or df_open.empty:
        st.info("Envie a planilha C6 para ver este relatório.")
    else:
        dias = sorted(pd.Series(df_open[COL_T]).dropna().unique().tolist())
        dia_sel = st.selectbox("Selecione a data de abertura", dias, format_func=fmt_date_br)
        fund = _fundacoes_mes_por_dia(df_open, dia_sel)
        st.dataframe(fund, use_container_width=True, hide_index=True)

with tab3:
    st.markdown("#### Pix")
    if df_all is None:
        st.info("Envie a planilha C6 para ver Pix e Status.")
    else:
        a, b = st.columns(2)
        a.metric("Clientes com Pix", fmt_int(qtd_com_pix))
        b.metric("Clientes sem Pix", fmt_int(qtd_sem_pix))

        st.markdown("##### Tipos de chave Pix (somente clientes com Pix)")
        st.dataframe(pix_por_chave, use_container_width=True, hide_index=True)

        st.markdown("#### Status")
        st.dataframe(status_tbl, use_container_width=True, hide_index=True)

with tab4:
    st.markdown("#### Qualificadas e Receita")
    st.caption("Regra: qualificada = BY=1 e maior nível do texto de critérios >= 1. Receita usa somente o maior nível.")

    if df_all is None:
        st.info("Envie a planilha C6 para ver as qualificadas.")
    else:
        st.markdown("##### Critério principal (vitorioso)")
        st.dataframe(crit_tbl, use_container_width=True, hide_index=True)

        st.markdown("##### Receita por nível (maior valor por cliente)")
        payout_show = payout_tbl.copy()
        if not payout_show.empty:
            payout_show["Valor unitário"] = payout_show["Valor unitário"].apply(fmt_money)
            payout_show["Total"] = payout_show["Total"].apply(fmt_money)
        st.dataframe(payout_show, use_container_width=True, hide_index=True)
        st.success(f"Receita estimada: {fmt_money(total_payout)}")

        st.markdown("##### Auditoria por cliente (para você enxergar o nível e o critério)")
        cols_show = []
        if COL_DOC in dfq_view.columns:
            cols_show.append(COL_DOC)
        if COL_NOME in dfq_view.columns:
            cols_show.append(COL_NOME)
        cols_show += ["Critério principal", "Nível principal", "Receita estimada", COL_CRIT]

        view = dfq_view[cols_show].copy()
        view["Receita estimada"] = view["Receita estimada"].apply(fmt_money)
        view = view.rename(columns={COL_CRIT: "Texto completo dos critérios"})
        st.dataframe(view, use_container_width=True, hide_index=True)

        st.markdown("##### Referência (M0/M1/M2)")
        st.dataframe(br_counts_tbl, use_container_width=True, hide_index=True)

with tab5:
    st.markdown("#### Conversão: Abertas sobre Cadastradas")

    daily_ok = os.path.exists(HIST_OPEN_DAILY) and os.path.exists(HIST_CAD_DAILY)
    month_ok = os.path.exists(HIST_OPEN_MONTH) and os.path.exists(HIST_CAD_MONTH)

    if not (daily_ok or month_ok):
        st.info("Envie as duas planilhas (C6 e Leads) pelo menos uma vez para iniciar o histórico.")
    else:
        if daily_ok:
            pct_d = _pct_daily_table()
            if pct_d.empty:
                st.info("Sem dados diários suficientes ainda.")
            else:
                st.markdown("##### Diário")
                view = pct_d.copy()
                view["Data"] = view["dia"].apply(fmt_date_br)
                view["Percentual"] = view["percentual"].apply(fmt_pct)
                view["Indicador"] = view["percentual"].apply(_chip_pct)

                view = view.rename(columns={
                    "abertas": "Contas abertas",
                    "cadastradas": "Contas cadastradas",
                })[["Data", "Contas cadastradas", "Contas abertas", "Indicador", "Percentual"]]

                st.markdown(view.to_html(index=False, escape=False), unsafe_allow_html=True)

        st.divider()

        if month_ok:
            pct_m = _pct_month_table()
            if pct_m.empty:
                st.info("Sem dados mensais suficientes ainda.")
            else:
                st.markdown("##### Mensal")
                viewm = pct_m.copy()
                viewm["Mês"] = viewm["mes"].apply(fmt_month_br)
                viewm["Percentual"] = viewm["percentual"].apply(fmt_pct)
                viewm["Indicador"] = viewm["percentual"].apply(_chip_pct)

                viewm = viewm.rename(columns={
                    "abertas": "Contas abertas",
                    "cadastradas": "Contas cadastradas",
                })[["Mês", "Contas cadastradas", "Contas abertas", "Indicador", "Percentual"]]

                st.markdown(viewm.to_html(index=False, escape=False), unsafe_allow_html=True)
