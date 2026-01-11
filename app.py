import os
import io
import re
import json
import hashlib
import datetime as dt
from typing import Optional, Tuple, Dict, List

import pandas as pd
import streamlit as st


# ============================================================
# CONFIG — IDENTIDADE
# ============================================================
APP_TITLE = "Assis & Mollerke"
APP_SUBTITLE = "Painel executivo — Visão Cliente C6 + Leads + Remuneração Incremental"
LOGO_PATH = "LOGO CORRETA.png"  # deixe na raiz do repositório

LOGIN_USER = "admin"
LOGIN_PASS = "123456"

START_DATE = dt.date(2026, 1, 1)   # histórico diário/mensal (aberturas/leads) a partir de 01/01/2026
START_REM_MONTH = "2025-11"        # remuneração incremental a partir de Nov/25 (como você pediu)
META_PCT = 0.20                    # 20% para conversão (Abertas/Cadastradas)


# ============================================================
# CONFIG — COLUNAS (VISÃO CLIENTE)
# ============================================================
COL_T  = "DT_CONTA_CRIADA"                 # abertura
COL_P  = "DT_FUNDACAO_EMPRESA"             # fundação
COL_X  = "CHAVES_PIX_FORTE"                # chave pix
COL_Y  = "VL_SALDO_MEDIO_MENSALIZADO"      # saldo
COL_V  = "STATUS_CC"                       # status
COL_AQ = "BANCO_DOMICILIO"                 # domicílio
COL_BY = "FL_QUALIFICADO_COMISS"           # flag qualificada (0/1)
COL_BR = "MES_REF_COMISS"                  # M0/M1/M2 (relatório)
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"    # texto critérios

COL_DOC  = "CD_CPF_CNPJ_CLIENTE"
COL_NOME = "NOME_CLIENTE"


# ============================================================
# CONFIG — LEADS (cadastro)
# ============================================================
LEADS_DATE_COL_INDEX = 12  # coluna M (13ª) — data do cadastro


# ============================================================
# REMUNERAÇÃO — FAIXAS (por quantidade de qualificadas no mês)
# ============================================================
TIER_TABLE = [
    (0,   49,  "Até 49 qualificadas",     {1: 140.00, 2: 230.00, 3: 400.00, 4: 540.00}),
    (50,  149, "50 a 149 qualificadas",   {1: 154.00, 2: 253.00, 3: 440.00, 4: 594.00}),
    (150, 349, "150 a 349 qualificadas",  {1: 175.00, 2: 287.50, 3: 500.00, 4: 675.00}),
    (350, 10**9, "350+ qualificadas",     {1: 210.00, 2: 345.00, 3: 600.00, 4: 810.00}),
]


# ============================================================
# PERSISTÊNCIA (memória do app)
# ============================================================
DATA_DIR = "data_uploads"
os.makedirs(DATA_DIR, exist_ok=True)

# snapshots (hoje vs ontem) — somente resumo do arquivo C6 enviado
LATEST_SNAPSHOT = os.path.join(DATA_DIR, "latest.json")
PREV_SNAPSHOT   = os.path.join(DATA_DIR, "prev.json")

# históricos de aberturas e cadastros (desde 01/01/2026)
HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_aberturas_diario.csv")
HIST_OPEN_MONTH = os.path.join(DATA_DIR, "hist_aberturas_mensal.csv")
HIST_CAD_DAILY  = os.path.join(DATA_DIR, "hist_cadastros_diario.csv")
HIST_CAD_MONTH  = os.path.join(DATA_DIR, "hist_cadastros_mensal.csv")

# remuneração incremental
HIST_MAXPAID = os.path.join(DATA_DIR, "hist_max_pago_por_cnpj.csv")     # cnpj, max_pago
HIST_REMMONTH = os.path.join(DATA_DIR, "hist_remuneracao_mensal.csv")   # mes, qualificadas, faixa, cheio, incremental
LAST_REM_DETAIL = os.path.join(DATA_DIR, "last_remuneracao_detalhe.csv")


# ============================================================
# UI / CSS
# ============================================================
st.set_page_config(page_title=APP_TITLE, layout="wide")
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.1rem; padding-bottom: 2.2rem; }

      section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #1b2440 0%, #11182d 100%);
      }
      section[data-testid="stSidebar"] * { color: #ffffff !important; }

      .am-title { font-size: 28px; font-weight: 900; color: #1b2440; margin:0; }
      .am-sub { color: #5b6280; margin-top: 6px; }

      .am-card {
        border: 1px solid #eef0f6;
        border-radius: 14px;
        padding: 14px 16px;
        background: #ffffff;
        box-shadow: 0 4px 18px rgba(17, 24, 45, 0.06);
      }
      .am-k { font-size: 13px; opacity:.75; margin-bottom:6px; }
      .am-v { font-size: 26px; font-weight: 900; color:#1b2440; margin:0; }
      .am-s { font-size: 12px; opacity:.70; margin-top:6px; }

      .chip { display:inline-block; padding:6px 10px; border-radius:999px; font-weight:800; font-size:12px; }
      .chip-ok { background: rgba(30,136,229,.12); color:#1e88e5; border:1px solid rgba(30,136,229,.25); }
      .chip-bad { background: rgba(229,57,53,.10); color:#e53935; border:1px solid rgba(229,57,53,.22); }
    </style>
    """,
    unsafe_allow_html=True
)

def card(title: str, value: str, sub: str = ""):
    st.markdown(
        f"""
        <div class="am-card">
          <div class="am-k">{title}</div>
          <p class="am-v">{value}</p>
          <div class="am-s">{sub}</div>
        </div>
        """,
        unsafe_allow_html=True
    )

def br_date(d) -> str:
    if pd.isna(d) or d is None:
        return ""
    if isinstance(d, dt.datetime):
        d = d.date()
    if isinstance(d, dt.date):
        return d.strftime("%d/%m/%Y")
    dd = pd.to_datetime(d, errors="coerce")
    if pd.isna(dd):
        return ""
    return dd.strftime("%d/%m/%Y")

def br_month_from_ym(ym: str) -> str:
    try:
        y, m = ym.split("-")
        return f"{m}/{y}"
    except Exception:
        return ym

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

def _chip_pct(p: float) -> str:
    if float(p) >= META_PCT:
        return f'<span class="chip chip-ok">OK • {fmt_pct(p)}</span>'
    return f'<span class="chip chip-bad">Atenção • {fmt_pct(p)}</span>'


# ============================================================
# LOGIN
# ============================================================
def login_gate():
    st.sidebar.markdown("### Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")
    if st.sidebar.button("Entrar"):
        st.session_state["logged_in"] = (u == LOGIN_USER and p == LOGIN_PASS)
        if not st.session_state["logged_in"]:
            st.sidebar.error("Usuário ou senha inválidos.")
    return st.session_state.get("logged_in", False)

if not login_gate():
    st.stop()


# ============================================================
# UTIL / LEITURA
# ============================================================
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

def _ensure_cols(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    return df


# ============================================================
# HISTÓRICOS (aberturas/cadastros)
# ============================================================
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


# ============================================================
# SNAPSHOT (ontem x hoje) — apenas para o upload C6 diário
# ============================================================
def _snapshot_to_disk(tag: str, file_hash: str, metrics: Dict):
    payload = {
        "tag": tag,
        "file_hash": file_hash,
        "saved_at": dt.datetime.now().isoformat(),
        "metrics": metrics,
    }
    if os.path.exists(LATEST_SNAPSHOT):
        with open(LATEST_SNAPSHOT, "r", encoding="utf-8") as f:
            old = f.read()
        with open(PREV_SNAPSHOT, "w", encoding="utf-8") as f:
            f.write(old)

    with open(LATEST_SNAPSHOT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def _load_prev_latest() -> Tuple[Optional[dict], Optional[dict]]:
    latest = prev = None
    if os.path.exists(LATEST_SNAPSHOT):
        with open(LATEST_SNAPSHOT, "r", encoding="utf-8") as f:
            latest = json.load(f)
    if os.path.exists(PREV_SNAPSHOT):
        with open(PREV_SNAPSHOT, "r", encoding="utf-8") as f:
            prev = json.load(f)
    return prev, latest


# ============================================================
# MÉTRICAS — C6 (painel principal)
# ============================================================
def _coerce_c6_all(df: pd.DataFrame) -> pd.DataFrame:
    required = [COL_T, COL_P, COL_X, COL_Y, COL_V, COL_AQ, COL_BY, COL_BR, COL_CRIT, COL_DOC, COL_NOME]
    df = _ensure_cols(df, required)

    df[COL_T] = _safe_to_date_series(df[COL_T])
    df[COL_P] = _safe_to_date_series(df[COL_P])

    df[COL_X] = _normalize_str(df[COL_X])
    df[COL_V] = _normalize_str(df[COL_V])
    df[COL_AQ] = _normalize_str(df[COL_AQ])
    df[COL_BR] = _normalize_str(df[COL_BR])
    df[COL_CRIT] = _normalize_str(df[COL_CRIT])
    df[COL_DOC] = _normalize_str(df[COL_DOC])
    df[COL_NOME] = _normalize_str(df[COL_NOME])

    df[COL_BY] = pd.to_numeric(df[COL_BY], errors="coerce").fillna(0).astype(int)
    df[COL_Y] = pd.to_numeric(df[COL_Y], errors="coerce").fillna(0.0)

    # filtro geral >= 01/01/2026, mas mantém linhas sem DT_CONTA_CRIADA para não “sumir” dados
    mask = df[COL_T].isna() | (df[COL_T] >= START_DATE)
    return df[mask].copy()

def _df_aberturas(df_all: pd.DataFrame) -> pd.DataFrame:
    dfo = df_all[df_all[COL_T].notna()].copy()
    dfo = dfo[dfo[COL_T] >= START_DATE].copy()
    return dfo

def _aberturas_por_dia(df_open: pd.DataFrame) -> pd.DataFrame:
    s = df_open[COL_T].dropna().apply(lambda d: d.strftime("%Y-%m-%d") if isinstance(d, dt.date) else "")
    s = s[s != ""]
    return s.value_counts().sort_index().rename_axis("dia").reset_index(name="valor")

def _aberturas_por_mes(df_open: pd.DataFrame) -> pd.DataFrame:
    t = pd.to_datetime(df_open[COL_T], errors="coerce")
    m = t.dropna().dt.to_period("M").astype(str)
    return m.value_counts().sort_index().rename_axis("mes").reset_index(name="valor")

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

def _pix_info(df_all: pd.DataFrame):
    s = df_all[COL_X].astype("string").fillna("").str.strip().str.upper()
    s = s.str.replace("'", "", regex=False)
    has_pix = ~s.isin(["", "-", "NAN", "NONE", "SEM", "SEM PIX"])
    qtd_com = int(has_pix.sum())
    qtd_sem = int((~has_pix).sum())
    por_chave = (
        s.loc[has_pix]
        .value_counts()
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

def _br_counts(dfq: pd.DataFrame) -> pd.DataFrame:
    if dfq.empty:
        return pd.DataFrame(columns=["Referência", "Quantidade"])
    s = dfq[COL_BR].fillna("").astype(str).str.upper().str.strip()
    return s.replace("", "SEM").value_counts().rename_axis("Referência").reset_index(name="Quantidade")


# ============================================================
# LEADS — histórico e conversão (Abertas/Cadastradas)
# ============================================================
def _load_leads_dates(df_leads: pd.DataFrame) -> pd.DataFrame:
    if df_leads.shape[1] <= LEADS_DATE_COL_INDEX:
        raise ValueError("A planilha de Leads não possui a coluna M (13ª coluna).")
    col_used = df_leads.columns[LEADS_DATE_COL_INDEX]
    s = pd.to_datetime(df_leads[col_used], errors="coerce").dt.date
    out = pd.DataFrame({"dia": s}).dropna()
    out = out[out["dia"] >= START_DATE].copy()
    return out

def _cadastros_por_dia(df_dates: pd.DataFrame) -> pd.DataFrame:
    s = df_dates["dia"].dropna().apply(lambda d: d.strftime("%Y-%m-%d") if isinstance(d, dt.date) else "")
    s = s[s != ""]
    return s.value_counts().sort_index().rename_axis("dia").reset_index(name="valor")

def _cadastros_por_mes(df_dates: pd.DataFrame) -> pd.DataFrame:
    t = pd.to_datetime(df_dates["dia"], errors="coerce")
    m = t.dropna().dt.to_period("M").astype(str)
    return m.value_counts().sort_index().rename_axis("mes").reset_index(name="valor")

def _pct_daily_table() -> pd.DataFrame:
    o = _read_hist(HIST_OPEN_DAILY, "dia").rename(columns={"valor": "abertas"})
    c = _read_hist(HIST_CAD_DAILY, "dia").rename(columns={"valor": "cadastradas"})
    df = pd.merge(o, c, on="dia", how="outer").fillna(0)
    df["abertas"] = df["abertas"].astype(int)
    df["cadastradas"] = df["cadastradas"].astype(int)
    df["percentual"] = df.apply(lambda r: (r["abertas"] / r["cadastradas"]) if r["cadastradas"] > 0 else 0.0, axis=1)
    df = _sort_hist(df, "dia")
    return df

def _pct_month_table() -> pd.DataFrame:
    o = _read_hist(HIST_OPEN_MONTH, "mes").rename(columns={"valor": "abertas"})
    c = _read_hist(HIST_CAD_MONTH, "mes").rename(columns={"valor": "cadastradas"})
    df = pd.merge(o, c, on="mes", how="outer").fillna(0)
    df["abertas"] = df["abertas"].astype(int)
    df["cadastradas"] = df["cadastradas"].astype(int)
    df["percentual"] = df.apply(lambda r: (r["abertas"] / r["cadastradas"]) if r["cadastradas"] > 0 else 0.0, axis=1)
    df = _sort_hist(df, "mes")
    return df


# ============================================================
# REMUNERAÇÃO INCREMENTAL (aba específica)
# ============================================================
def normalize_cnpj(x) -> str:
    s = str(x) if x is not None else ""
    s = re.sub(r"\D", "", s)
    return s

def parse_max_level(criteria_text: str) -> int:
    # pega o MAIOR número após ":" no texto de critérios
    nums = re.findall(r":\s*(\d+)", str(criteria_text))
    if not nums:
        return 0
    vals = [int(x) for x in nums]
    return max(vals) if vals else 0

def tier_for_qty(qty: int) -> Tuple[str, Dict[int, float]]:
    for mn, mx, name, table in TIER_TABLE:
        if mn <= qty <= mx:
            return name, table
    return TIER_TABLE[0][2], TIER_TABLE[0][3]

def ym_from_filename(name: str) -> Optional[str]:
    # tenta detectar "NOVEMBRO2025", "DEZEMBRO2025", "NOV_2025", "2025-11" etc.
    up = name.upper()
    month_map = {
        "JANEIRO": "01", "FEVEREIRO": "02", "MARCO": "03", "MARÇO": "03", "ABRIL": "04",
        "MAIO": "05", "JUNHO": "06", "JULHO": "07", "AGOSTO": "08",
        "SETEMBRO": "09", "OUTUBRO": "10", "NOVEMBRO": "11", "DEZEMBRO": "12"
    }
    # padrão YYYY-MM
    m = re.search(r"(20\d{2})[-_\.](\d{2})", up)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    # padrão NOMEMES + ANO
    for nm, mm in month_map.items():
        if nm in up:
            y = re.search(r"(20\d{2})", up)
            if y:
                return f"{y.group(1)}-{mm}"
    return None

def ym_from_any_date_col(df: pd.DataFrame) -> Optional[str]:
    # tenta achar uma coluna de data razoável e pegar o primeiro mês
    for c in df.columns:
        if "DATA" in str(c).upper() or "DT_" in str(c).upper():
            s = pd.to_datetime(df[c], errors="coerce").dropna()
            if not s.empty:
                d = s.iloc[0]
                return f"{d.year:04d}-{d.month:02d}"
    return None

def load_maxpaid() -> pd.DataFrame:
    if not os.path.exists(HIST_MAXPAID):
        return pd.DataFrame(columns=["cnpj", "max_pago"])
    df = pd.read_csv(HIST_MAXPAID, dtype={"cnpj": "string"})
    df["cnpj"] = df["cnpj"].astype("string").fillna("").str.strip()
    df["max_pago"] = pd.to_numeric(df["max_pago"], errors="coerce").fillna(0.0)
    df = df[df["cnpj"] != ""].copy()
    return df

def save_maxpaid(df: pd.DataFrame):
    df = df.copy()
    df["cnpj"] = df["cnpj"].astype("string").fillna("").str.strip()
    df["max_pago"] = pd.to_numeric(df["max_pago"], errors="coerce").fillna(0.0)
    df = df[df["cnpj"] != ""].copy()
    df.to_csv(HIST_MAXPAID, index=False)

def load_rem_month_hist() -> pd.DataFrame:
    if not os.path.exists(HIST_REMMONTH):
        return pd.DataFrame(columns=["mes", "qualificadas", "faixa", "valor_cheio", "valor_incremental"])
    df = pd.read_csv(HIST_REMMONTH, dtype={"mes": "string"})
    df["mes"] = df["mes"].astype("string").fillna("").str.strip()
    df["qualificadas"] = pd.to_numeric(df["qualificadas"], errors="coerce").fillna(0).astype(int)
    df["valor_cheio"] = pd.to_numeric(df["valor_cheio"], errors="coerce").fillna(0.0)
    df["valor_incremental"] = pd.to_numeric(df["valor_incremental"], errors="coerce").fillna(0.0)
    return df

def upsert_rem_month_row(row: Dict):
    base = load_rem_month_hist()
    mes = str(row["mes"])
    base = base[base["mes"] != mes].copy()
    base = pd.concat([base, pd.DataFrame([row])], ignore_index=True)
    sort_key = pd.to_datetime(base["mes"] + "-01", errors="coerce")
    base = base.assign(_k=sort_key).sort_values("_k").drop(columns=["_k"])
    base.to_csv(HIST_REMMONTH, index=False)

def process_monthly_remuneration(file_bytes: bytes, filename: str, forced_month: Optional[str] = None) -> Dict:
    df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")

    # tenta pegar mês (prioridade: forçado > nome arquivo > qualquer coluna de data)
    ym = forced_month.strip() if (forced_month and forced_month.strip()) else None
    if not ym:
        ym = ym_from_filename(filename)
    if not ym:
        ym = ym_from_any_date_col(df)
    if not ym:
        raise ValueError("Não consegui identificar o mês do arquivo. Informe o mês manualmente (YYYY-MM).")

    # trava: só calcula a partir de Nov/25 (como você pediu)
    if ym < START_REM_MONTH:
        raise ValueError(f"Mês {ym} é anterior a {START_REM_MONTH}. A remuneração começa em Nov/25.")

    # garante colunas mínimas
    df = _ensure_cols(df, [COL_DOC, COL_NOME, COL_BY, COL_CRIT])

    # filtra BY=1 se existir com conteúdo
    by = pd.to_numeric(df[COL_BY], errors="coerce")
    if by.notna().any():
        by = by.fillna(0).astype(int)
        df = df[by == 1].copy()

    # nível vitorioso
    df["nivel"] = df[COL_CRIT].apply(parse_max_level).astype(int)
    df = df[df["nivel"] >= 1].copy()

    # CNPJ
    df["cnpj"] = df[COL_DOC].apply(normalize_cnpj)
    df = df[df["cnpj"] != ""].copy()

    df["cliente"] = df[COL_NOME].astype("string").fillna("").str.strip()

    # faixa do mês (pela quantidade de qualificadas do mês)
    qty = int(df.shape[0])
    faixa, tabela = tier_for_qty(qty)

    # valor cheio por CNPJ (no mês)
    df["valor_cheio"] = df["nivel"].map(tabela).fillna(0.0).astype(float)

    # histórico: máximo já pago por CNPJ
    hist = load_maxpaid()
    paid_map = dict(zip(hist["cnpj"].astype(str), hist["max_pago"].astype(float)))

    df["ja_pago_max"] = df["cnpj"].map(paid_map).fillna(0.0).astype(float)
    df["a_receber"] = (df["valor_cheio"] - df["ja_pago_max"]).clip(lower=0.0)

    # atualiza histórico max pago: max(antigo, valor_cheio)
    new_map = paid_map.copy()
    for cnpj, v in zip(df["cnpj"].tolist(), df["valor_cheio"].tolist()):
        old = float(new_map.get(cnpj, 0.0))
        new_map[cnpj] = max(old, float(v))

    new_hist = pd.DataFrame({"cnpj": list(new_map.keys()), "max_pago": list(new_map.values())})
    save_maxpaid(new_hist)

    # totais
    cheio_total = float(df["valor_cheio"].sum())
    inc_total = float(df["a_receber"].sum())

    # distribuição por nível (na faixa do mês)
    dist = (
        df["nivel"].value_counts().sort_index()
        .rename_axis("Nível")
        .reset_index(name="Quantidade")
    )
    dist["Valor unitário (faixa)"] = dist["Nível"].map(tabela).fillna(0.0)
    dist["Total (cheio)"] = dist["Valor unitário (faixa)"] * dist["Quantidade"]

    # detalhe (auditoria)
    detalhe = df[["cnpj", "cliente", "nivel", "ja_pago_max", "valor_cheio", "a_receber", COL_CRIT]].copy()
    detalhe = detalhe.rename(columns={
        "cnpj": "CNPJ",
        "cliente": "Cliente",
        "nivel": "Nível (vitorioso)",
        "ja_pago_max": "Já pago (máx histórico)",
        "valor_cheio": "Valor cheio (mês)",
        "a_receber": "A receber (delta)",
        COL_CRIT: "Critérios"
    })
    detalhe.to_csv(LAST_REM_DETAIL, index=False)

    # grava resumo mensal
    upsert_rem_month_row({
        "mes": ym,
        "qualificadas": qty,
        "faixa": faixa,
        "valor_cheio": cheio_total,
        "valor_incremental": inc_total
    })

    return {
        "mes": ym,
        "faixa": faixa,
        "qualificadas": qty,
        "cheio_total": cheio_total,
        "inc_total": inc_total,
        "dist": dist,
        "detalhe": detalhe,
    }


# ============================================================
# HEADER
# ============================================================
top_l, top_r = st.columns([1, 4])
with top_l:
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, use_container_width=True)
with top_r:
    st.markdown(f'<p class="am-title">{APP_TITLE}</p>', unsafe_allow_html=True)
    st.markdown(f'<p class="am-sub">{APP_SUBTITLE}</p>', unsafe_allow_html=True)

st.divider()


# ============================================================
# UPLOADS (diário)
# ============================================================
st.markdown("### Importação do dia")

col_up1, col_up2 = st.columns(2)
with col_up1:
    uploaded_c6 = st.file_uploader("Planilha C6 (Visão Cliente) — diário", type=["xlsx"], key="c6")
with col_up2:
    uploaded_leads = st.file_uploader("Planilha Leads — diário", type=["xlsx"], key="leads")

prev, latest_saved = _load_prev_latest()

# variáveis do painel
df_all = None
df_open = None
qtd_com_pix = qtd_sem_pix = 0
pix_por_chave = pd.DataFrame()
status_tbl = pd.DataFrame()
br_counts_tbl = pd.DataFrame()
saldo_total = 0.0
qtd_c6 = 0
qtd_qualif_by1 = 0

# Processa C6 diário
if uploaded_c6:
    file_bytes = uploaded_c6.getvalue()
    file_hash = _hash_bytes(file_bytes)

    df_raw = _load_excel(file_bytes)
    df_all = _coerce_c6_all(df_raw)
    df_open = _df_aberturas(df_all)

    # históricos (aberturas)
    open_daily = _aberturas_por_dia(df_open)
    open_month = _aberturas_por_mes(df_open)
    _upsert_hist(HIST_OPEN_DAILY, "dia", open_daily)
    _upsert_hist(HIST_OPEN_MONTH, "mes", open_month)

    # métricas
    qtd_com_pix, qtd_sem_pix, pix_por_chave = _pix_info(df_all)
    saldo_total = _sum_saldo(df_all)
    status_tbl = _status_counts(df_all)
    qtd_c6 = _domicilio_c6_count(df_all)

    # qualificadas simples (apenas BY=1) — relatório do dia (não é a remuneração incremental)
    dfq_simple = df_all[df_all[COL_BY] == 1].copy()
    qtd_qualif_by1 = int(dfq_simple.shape[0])
    br_counts_tbl = _br_counts(dfq_simple)

    metrics = {
        "total_abertas": int(df_open.shape[0]),
        "saldo_total": float(saldo_total),
        "qtd_com_pix": int(qtd_com_pix),
        "qtd_sem_pix": int(qtd_sem_pix),
        "qtd_c6": int(qtd_c6),
        "qualificadas_by1": int(qtd_qualif_by1),
    }

    _snapshot_to_disk(tag=uploaded_c6.name, file_hash=file_hash, metrics=metrics)
    prev, latest_saved = _load_prev_latest()

# Processa Leads diário
if uploaded_leads:
    try:
        df_leads = _load_excel(uploaded_leads.getvalue())
        df_dates = _load_leads_dates(df_leads)

        cad_daily = _cadastros_por_dia(df_dates)
        cad_month = _cadastros_por_mes(df_dates)

        _upsert_hist(HIST_CAD_DAILY, "dia", cad_daily)
        _upsert_hist(HIST_CAD_MONTH, "mes", cad_month)
    except Exception as e:
        st.error(f"Não consegui ler a planilha de Leads. Motivo: {str(e)}")

st.divider()


# ============================================================
# RESUMO (C6 diário)
# ============================================================
if latest_saved and latest_saved.get("metrics"):
    m = latest_saved["metrics"]

    st.markdown("### Resumo executivo (dia)")

    c1, c2, c3, c4 = st.columns(4)
    with c1: card("Contas abertas (arquivo)", fmt_int(m["total_abertas"]))
    with c2: card("Saldo total", fmt_money(m["saldo_total"]))
    with c3: card("Clientes com Pix", fmt_int(m["qtd_com_pix"]))
    with c4: card("Clientes sem Pix", fmt_int(m["qtd_sem_pix"]))

    c5, c6, c7, c8 = st.columns(4)
    with c5: card("Domicílio C6", fmt_int(m["qtd_c6"]))
    with c6: card("Qualificadas (BY=1)", fmt_int(m["qualificadas_by1"]), "Indicador do arquivo diário (não é o cálculo incremental).")
    with c7: card("Arquivo", latest_saved.get("tag", "-"))
    with c8:
        if prev and prev.get("metrics"):
            pm = prev["metrics"]
            delta = m["total_abertas"] - pm.get("total_abertas", 0)
            card("Variação de aberturas", f"{delta:+d}")
        else:
            card("Variação", "—", "Envie 2 dias para comparar.")
else:
    st.info("Envie a planilha C6 diária para gerar o resumo.")


# ============================================================
# ABAS PRINCIPAIS + ABA REMUNERAÇÃO (incremental)
# ============================================================
st.divider()

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "Aberturas (Histórico)",
    "Fundações (Detalhe do dia)",
    "Pix e Status",
    "Qualificadas (arquivo diário)",
    "Cadastro x Abertura (Leads)",
    "Remuneração (Incremental)"
])

# --- Tab 1
with tab1:
    st.markdown("#### Aberturas acumuladas (desde 01/01/2026)")
    h_daily = _read_hist(HIST_OPEN_DAILY, "dia")
    if h_daily.empty:
        st.info("Sem histórico diário ainda.")
    else:
        show = h_daily.copy()
        show["Data"] = show["dia"].apply(lambda x: br_date(pd.to_datetime(x).date()))
        show = show[["Data", "valor"]].rename(columns={"valor": "Contas abertas"})
        st.dataframe(show, use_container_width=True, hide_index=True)

    h_month = _read_hist(HIST_OPEN_MONTH, "mes")
    if h_month.empty:
        st.info("Sem histórico mensal ainda.")
    else:
        showm = h_month.copy()
        showm["Mês"] = showm["mes"].apply(br_month_from_ym)
        showm = showm[["Mês", "valor"]].rename(columns={"valor": "Contas abertas"})
        st.dataframe(showm, use_container_width=True, hide_index=True)

# --- Tab 2
with tab2:
    st.markdown("#### Fundações por dia (mês/ano)")
    if df_open is None or df_open.empty:
        st.info("Envie a planilha C6 diária para ver este relatório.")
    else:
        dias = sorted(pd.Series(df_open[COL_T]).dropna().unique().tolist())
        dia_sel = st.selectbox("Selecione a data de abertura", dias, format_func=br_date)
        fund = _fundacoes_mes_por_dia(df_open, dia_sel)
        st.dataframe(fund, use_container_width=True, hide_index=True)

# --- Tab 3
with tab3:
    st.markdown("#### Pix")
    if df_all is None:
        st.info("Envie a planilha C6 diária.")
    else:
        a, b = st.columns(2)
        with a: card("Clientes com Pix", fmt_int(qtd_com_pix))
        with b: card("Clientes sem Pix", fmt_int(qtd_sem_pix))

        st.markdown("##### Tipos de chave Pix (somente clientes com Pix)")
        st.dataframe(pix_por_chave, use_container_width=True, hide_index=True)

        st.markdown("#### Status")
        st.dataframe(status_tbl, use_container_width=True, hide_index=True)

# --- Tab 4
with tab4:
    st.markdown("#### Qualificadas (arquivo diário)")
    st.caption("Aqui é somente a contagem BY=1 do arquivo diário. A remuneração incremental fica na aba Remuneração.")
    if df_all is None:
        st.info("Envie a planilha C6 diária.")
    else:
        card1, card2 = st.columns(2)
        with card1: card("Qualificadas (BY=1)", fmt_int(qtd_qualif_by1))
        with card2: card("Domicílio C6", fmt_int(qtd_c6))
        st.markdown("##### Referência (M0/M1/M2)")
        st.dataframe(br_counts_tbl, use_container_width=True, hide_index=True)

# --- Tab 5
with tab5:
    st.markdown("#### Conversão: Abertas sobre Cadastradas (>= 20% em azul)")
    daily_ok = os.path.exists(HIST_OPEN_DAILY) and os.path.exists(HIST_CAD_DAILY)
    month_ok = os.path.exists(HIST_OPEN_MONTH) and os.path.exists(HIST_CAD_MONTH)

    if not (daily_ok or month_ok):
        st.info("Envie as duas planilhas (C6 diária e Leads diária) pelo menos uma vez.")
    else:
        if daily_ok:
            pct_d = _pct_daily_table()
            if not pct_d.empty:
                view = pct_d.copy()
                view["Data"] = view["dia"].apply(lambda x: br_date(pd.to_datetime(x).date()))
                view["Percentual"] = view["percentual"].apply(fmt_pct)
                view["Indicador"] = view["percentual"].apply(_chip_pct)
                view = view.rename(columns={"abertas": "Abertas", "cadastradas": "Cadastradas"})[
                    ["Data", "Cadastradas", "Abertas", "Indicador", "Percentual"]
                ]
                st.markdown(view.to_html(index=False, escape=False), unsafe_allow_html=True)

        st.divider()

        if month_ok:
            pct_m = _pct_month_table()
            if not pct_m.empty:
                viewm = pct_m.copy()
                viewm["Mês"] = viewm["mes"].apply(br_month_from_ym)
                viewm["Percentual"] = viewm["percentual"].apply(fmt_pct)
                viewm["Indicador"] = viewm["percentual"].apply(_chip_pct)
                viewm = viewm.rename(columns={"abertas": "Abertas", "cadastradas": "Cadastradas"})[
                    ["Mês", "Cadastradas", "Abertas", "Indicador", "Percentual"]
                ]
                st.markdown(viewm.to_html(index=False, escape=False), unsafe_allow_html=True)

# --- Tab 6 (REMUNERAÇÃO INCREMENTAL)
with tab6:
    st.markdown("#### Remuneração (Incremental por CNPJ) — a partir de Nov/25")
    st.caption(
        "Regra: valor cheio do mês depende da faixa (qtd qualificadas no mês) e do nível vitorioso (maior critério). "
        "A receber no mês = diferença positiva do valor cheio vs. o máximo já pago desse CNPJ em meses anteriores."
    )

    forced_month = st.text_input("Se necessário, informe o mês do arquivo (YYYY-MM). Ex.: 2025-11", value="")
    monthly_files = st.file_uploader(
        "Importe os arquivos mensais (ex.: NOVEMBRO2025.xlsx, DEZEMBRO2025.xlsx). Pode enviar vários de uma vez.",
        type=["xlsx"],
        accept_multiple_files=True,
        key="rem_files"
    )

    if monthly_files:
        for f in monthly_files:
            try:
                res = process_monthly_remuneration(
                    f.getvalue(),
                    f.name,
                    forced_month=forced_month if forced_month.strip() else None
                )

                st.success(f"Processado: {f.name}  •  Mês: {br_month_from_ym(res['mes'])}  •  Faixa: {res['faixa']}")

                r1, r2, r3, r4 = st.columns(4)
                with r1: card("Qualificadas no mês", fmt_int(res["qualificadas"]))
                with r2: card("Receita cheia do mês", fmt_money(res["cheio_total"]))
                with r3: card("A receber (incremental)", fmt_money(res["inc_total"]))
                with r4: card("Mês", br_month_from_ym(res["mes"]))

                st.markdown("##### Distribuição por nível (na faixa do mês)")
                dist = res["dist"].copy()
                dist["Valor unitário (faixa)"] = dist["Valor unitário (faixa)"].apply(fmt_money)
                dist["Total (cheio)"] = dist["Total (cheio)"].apply(fmt_money)
                st.dataframe(dist, use_container_width=True, hide_index=True)

                st.markdown("##### Auditoria por CNPJ (mostra o delta por cliente)")
                det = res["detalhe"].copy()
                for col in ["Já pago (máx histórico)", "Valor cheio (mês)", "A receber (delta)"]:
                    det[col] = det[col].apply(fmt_money)
                st.dataframe(det, use_container_width=True, hide_index=True)

                if os.path.exists(LAST_REM_DETAIL):
                    with open(LAST_REM_DETAIL, "rb") as fp:
                        st.download_button(
                            "Baixar detalhe do último processamento (CSV)",
                            data=fp,
                            file_name=f"remuneracao_detalhe_{res['mes']}.csv",
                            mime="text/csv"
                        )

                st.divider()

            except Exception as e:
                st.error(f"Erro ao processar {f.name}: {str(e)}")

    st.markdown("##### Histórico mensal (remuneração)")
    hist = load_rem_month_hist()
    if hist.empty:
        st.info("Sem histórico ainda. Importe NOVEMBRO/25 e DEZEMBRO/25 para iniciar.")
    else:
        showh = hist.copy()
        showh["Mês"] = showh["mes"].apply(br_month_from_ym)
        showh = showh.drop(columns=["mes"])
        showh["valor_cheio"] = showh["valor_cheio"].apply(fmt_money)
        showh["valor_incremental"] = showh["valor_incremental"].apply(fmt_money)
        showh = showh.rename(columns={
            "qualificadas": "Qualificadas",
            "faixa": "Faixa",
            "valor_cheio": "Receita cheia (mês)",
            "valor_incremental": "A receber (incremental)",
        })
        st.dataframe(showh[["Mês", "Qualificadas", "Faixa", "Receita cheia (mês)", "A receber (incremental)"]],
                     use_container_width=True, hide_index=True)

    with st.expander("Histórico por CNPJ (máximo já pago)"):
        h = load_maxpaid()
        if h.empty:
            st.info("Ainda não existe histórico por CNPJ.")
        else:
            hh = h.copy()
            hh["max_pago"] = hh["max_pago"].apply(fmt_money)
            hh = hh.rename(columns={"cnpj": "CNPJ", "max_pago": "Máx já pago"})
            st.dataframe(hh, use_container_width=True, hide_index=True)
            csv_bytes = hh.to_csv(index=False).encode("utf-8")
            st.download_button("Baixar histórico por CNPJ (CSV)", csv_bytes, "historico_max_pago.csv", "text/csv")
