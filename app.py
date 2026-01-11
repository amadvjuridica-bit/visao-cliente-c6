import os
import io
import re
import sqlite3
import datetime as dt
from pathlib import Path
from typing import Optional, Dict, Tuple, List

import pandas as pd
import streamlit as st


# ============================================================
# CONFIG GERAL
# ============================================================
APP_TITLE = "Painel de controle Assis e Mollerke parceiro Banco C6"
APP_SUBTITLE = "Visão Cliente C6 + Leads + Remuneração mensal (incremental)"
LOGIN_USER = "admin"
LOGIN_PASS = "123456"

LOGO_FILENAME = "LOGO CORRETA.png"  # tem que estar na raiz do repo, junto do app.py

# Conversão alvo
CONV_TARGET = 0.20  # 20%

# Persistência local (Streamlit Cloud mantém no container enquanto o app existir)
DATA_DIR = Path("data_store")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "assis_mollerke.db"


# ============================================================
# COLUNAS (ajustadas para suas planilhas)
# ============================================================
# Visão Cliente
COL_OPEN_DATE = "DT_CONTA_CRIADA"
COL_FOUND_DATE = "DT_FUNDACAO_EMPRESA"
COL_PIX_TYPE = "CHAVES_PIX_FORTE"
COL_SALDO = "VL_SALDO_MEDIO_MENSALIZADO"
COL_STATUS = "STATUS_CC"
COL_DOMICILIO = "BANCO_DOMICILIO"
COL_BY = "FL_QUALIFICADO_COMISS"          # pode vir 0/1 OU texto dependendo do arquivo
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"   # texto com: CASH IN: 3 | ... | SALDO MEDIO: 4 ...
COL_CNPJ = "CD_CPF_CNPJ_CLIENTE"

# Leads
COL_LEAD_DATE = "DATA_HORA_CADASTRO"

# Remuneração base (faixa 1.0)
BASE_PAYOUT = {1: 140.00, 2: 230.00, 3: 400.00, 4: 540.00}


def tier_multiplier(qtd_qualificadas: int) -> float:
    """
    Faixa por qtd de qualificadas no mês:
    - até 49 -> 1.0
    - 50..149 -> 1.1
    - 150..349 -> 1.25
    - 350+ -> 1.5
    """
    if qtd_qualificadas <= 49:
        return 1.0
    if qtd_qualificadas <= 149:
        return 1.1
    if qtd_qualificadas <= 349:
        return 1.25
    return 1.5


# ============================================================
# FORMATAÇÃO (pt-BR)
# ============================================================
def fmt_int(n: int) -> str:
    return f"{int(n):,}".replace(",", ".")

def fmt_money(v: float) -> str:
    s = f"{float(v):,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"

def fmt_date(d: Optional[dt.date]) -> str:
    if d is None or pd.isna(d):
        return ""
    if isinstance(d, dt.datetime):
        d = d.date()
    if isinstance(d, pd.Timestamp):
        d = d.date()
    return d.strftime("%d/%m/%Y")

def fmt_month_from_date(d: dt.date) -> str:
    return d.strftime("%m/%Y")

def safe_to_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.date

def normalize_str(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").str.strip()


# ============================================================
# DB
# ============================================================
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def db_init():
    with db_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_open (
            ref_date TEXT PRIMARY KEY,
            opened_count INTEGER,
            saldo_total REAL,
            pix_with INTEGER,
            pix_without INTEGER,
            domicilio_c6 INTEGER,
            qualified_count INTEGER
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_leads (
            ref_date TEXT PRIMARY KEY,
            leads_count INTEGER
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS monthly_clients (
            month_ref TEXT,
            cnpj TEXT,
            level INTEGER,
            winner_crit TEXT,
            full_value REAL,
            PRIMARY KEY (month_ref, cnpj)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS monthly_summary (
            month_ref TEXT PRIMARY KEY,
            qualified_count INTEGER,
            multiplier REAL,
            full_total REAL,
            paid_before_total REAL,
            due_total REAL
        );
        """)

db_init()


# ============================================================
# UI / TEMA
# ============================================================
def apply_theme():
    st.markdown(
        """
        <style>
        .block-container { padding-top: 1.0rem; }
        h1,h2,h3 { color:#1f2a44; }
        .am-sub { color:#5f6b84; margin-top:-10px; }
        .stDataFrame { border-radius: 12px; overflow: hidden; }
        </style>
        """,
        unsafe_allow_html=True
    )

def header():
    logo_path = Path(__file__).parent / LOGO_FILENAME
    c1, c2 = st.columns([1.2, 6], vertical_alignment="center")
    with c1:
        if logo_path.exists():
            # Proporcional (sem estourar)
            st.image(str(logo_path), use_container_width=True)
        else:
            st.write("")
    with c2:
        st.markdown(f"<h1 style='margin-bottom:0px'>{APP_TITLE}</h1>", unsafe_allow_html=True)
        st.markdown(f"<div class='am-sub'>{APP_SUBTITLE}</div>", unsafe_allow_html=True)

def login_gate() -> bool:
    st.sidebar.markdown("## Acesso")
    u = st.sidebar.text_input("Usuário")
    p = st.sidebar.text_input("Senha", type="password")
    if st.sidebar.button("Entrar"):
        st.session_state["logged_in"] = (u == LOGIN_USER and p == LOGIN_PASS)
        if not st.session_state["logged_in"]:
            st.sidebar.error("Usuário ou senha inválidos.")
    return st.session_state.get("logged_in", False)


# ============================================================
# EXCEL
# ============================================================
def load_excel(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")

def safe_col(df: pd.DataFrame, col: str):
    if col not in df.columns:
        df[col] = pd.NA

def clean_pix_series(s: pd.Series) -> pd.Series:
    s = s.astype("string").fillna("").str.strip().str.upper()
    s = s.str.replace("'", "", regex=False)
    return s

def is_pix_present(v: str) -> bool:
    v = str(v).strip().upper()
    return v not in ["", "-", "NAN", "NONE", "SEM", "SEM PIX"]

def contains_c6(val: str) -> bool:
    return "c6" in str(val).lower()


# ============================================================
# QUALIFICAÇÃO: sempre pega o MAIOR (nível vencedor)
# - Fonte pode ser CRITERIOS_ATINGIDOS_COMISS OU BY (se BY vier texto)
# - Se BY vier 0/1, ele não define nível, apenas ajuda a dizer "qualificada"
# ============================================================
CRIT_PATTERNS = [
    ("CASH IN", re.compile(r"CASH\s*IN\s*:\s*(\d+)", re.IGNORECASE)),
    ("DOMICILIO", re.compile(r"DOMIC[IÍ]LIO\s*:\s*(\d+)", re.IGNORECASE)),
    ("SALDO MEDIO", re.compile(r"SALDO\s*M[ÉE]DIO\s*:\s*(\d+)", re.IGNORECASE)),
    ("SPENDING", re.compile(r"SPENDING\s*:\s*(\d+)", re.IGNORECASE)),
    ("CONTA GLOBAL", re.compile(r"CONTA\s*GLOBAL\s*:\s*(\d+)", re.IGNORECASE)),
]

def extract_level_and_winner(txt: str) -> Tuple[int, str]:
    if not isinstance(txt, str):
        return 0, ""
    found: List[Tuple[str, int]] = []
    for name, pat in CRIT_PATTERNS:
        m = pat.search(txt)
        if m:
            try:
                v = int(m.group(1))
            except Exception:
                v = 0
            found.append((name, v))

    # fallback: pega qualquer número após ":" (caso venha diferente)
    if not found:
        nums = re.findall(r":\s*(\d+)", txt)
        vals = []
        for n in nums:
            try:
                vals.append(int(n))
            except Exception:
                pass
        if vals:
            lv = max(vals)
            lv = max(0, min(4, lv))
            if lv >= 1:
                return lv, f"NÍVEL ({lv})"
        return 0, ""

    maxv = max(v for _, v in found)
    maxv = max(0, min(4, maxv))
    if maxv < 1:
        return 0, ""

    winners = [name for name, v in found if v == maxv]
    w = winners[0] if winners else "CRITÉRIO"
    return maxv, f"{w} ({maxv})"

def compute_qualification(df: pd.DataFrame) -> pd.DataFrame:
    # garante colunas
    safe_col(df, COL_CRIT)
    safe_col(df, COL_BY)

    crit_text = df[COL_CRIT].astype("string").fillna("").astype(str)

    # BY pode ser:
    # - 0/1 (numérico)
    # - texto com critérios (alguns arquivos)
    by_raw = df[COL_BY]

    # tenta tratar BY como numérico 0/1
    by_num = pd.to_numeric(by_raw, errors="coerce")  # NaN se não for número
    by_is_numeric = by_num.notna().any()

    levels = []
    winners = []
    qualified = []

    for i in range(len(df)):
        tcrit = str(crit_text.iloc[i]).strip()

        # tenta extrair nível do CRIT
        lv, win = extract_level_and_winner(tcrit)

        # se não achou nível no CRIT, tenta BY como texto
        if lv == 0:
            byv = by_raw.iloc[i]
            if isinstance(byv, str) and byv.strip():
                lv2, win2 = extract_level_and_winner(byv)
                if lv2 > 0:
                    lv, win = lv2, win2

        # determina qualificada:
        # - se nível >=1 -> qualificada
        # - senão, se BY numérico == 1 -> qualificada (mas sem nível)
        is_q = (lv >= 1)
        if not is_q and by_is_numeric:
            try:
                is_q = int(by_num.iloc[i] if pd.notna(by_num.iloc[i]) else 0) == 1
            except Exception:
                is_q = False

        levels.append(int(lv))
        winners.append(str(win))
        qualified.append(bool(is_q))

    df["_level"] = pd.Series(levels, index=df.index).fillna(0).astype(int)
    df["_winner"] = pd.Series(winners, index=df.index).fillna("").astype(str)
    df["_qualified"] = pd.Series(qualified, index=df.index).fillna(False).astype(bool)
    return df


# ============================================================
# DIÁRIO: métricas Visão Cliente (aberturas)
# ============================================================
def daily_open_metrics(df: pd.DataFrame, ref_date: dt.date) -> Dict:
    for c in [COL_OPEN_DATE, COL_FOUND_DATE, COL_PIX_TYPE, COL_SALDO, COL_STATUS, COL_DOMICILIO, COL_CRIT, COL_BY, COL_CNPJ]:
        safe_col(df, c)

    df[COL_OPEN_DATE] = safe_to_date_series(df[COL_OPEN_DATE])
    df[COL_FOUND_DATE] = safe_to_date_series(df[COL_FOUND_DATE])
    df[COL_SALDO] = pd.to_numeric(df[COL_SALDO], errors="coerce").fillna(0.0)

    # filtra por data (se existir no arquivo)
    mask_day = df[COL_OPEN_DATE] == ref_date
    dfd = df.loc[mask_day].copy() if mask_day.any() else df.copy()

    pix = clean_pix_series(dfd[COL_PIX_TYPE])
    pix_with = int(pix.apply(is_pix_present).sum())
    pix_without = int((~pix.apply(is_pix_present)).sum())

    dom = normalize_str(dfd[COL_DOMICILIO])
    domicilio_c6 = int(dom.apply(contains_c6).sum())

    dfd = compute_qualification(dfd)
    opened_count = int(dfd.shape[0])
    saldo_total = float(dfd[COL_SALDO].sum())
    qualified_count = int(dfd["_qualified"].sum())

    lvl_counts = dfd.loc[dfd["_qualified"], "_level"].value_counts().to_dict()
    lvl1 = int(lvl_counts.get(1, 0))
    lvl2 = int(lvl_counts.get(2, 0))
    lvl3 = int(lvl_counts.get(3, 0))
    lvl4 = int(lvl_counts.get(4, 0))

    return {
        "ref_date": ref_date,
        "opened_count": opened_count,
        "saldo_total": saldo_total,
        "pix_with": pix_with,
        "pix_without": pix_without,
        "domicilio_c6": domicilio_c6,
        "qualified_count": qualified_count,
        "lvl1": lvl1, "lvl2": lvl2, "lvl3": lvl3, "lvl4": lvl4,
        "df_day": dfd,
    }

def upsert_daily_open(m: Dict):
    with db_conn() as conn:
        conn.execute("""
        INSERT INTO daily_open(ref_date, opened_count, saldo_total, pix_with, pix_without, domicilio_c6, qualified_count)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(ref_date) DO UPDATE SET
            opened_count=excluded.opened_count,
            saldo_total=excluded.saldo_total,
            pix_with=excluded.pix_with,
            pix_without=excluded.pix_without,
            domicilio_c6=excluded.domicilio_c6,
            qualified_count=excluded.qualified_count;
        """, (
            m["ref_date"].isoformat(),
            m["opened_count"],
            m["saldo_total"],
            m["pix_with"],
            m["pix_without"],
            m["domicilio_c6"],
            m["qualified_count"],
        ))

def load_daily_open() -> pd.DataFrame:
    with db_conn() as conn:
        df = pd.read_sql_query("SELECT * FROM daily_open", conn)
    if df.empty:
        return df
    df["ref_date"] = pd.to_datetime(df["ref_date"]).dt.date
    return df.sort_values("ref_date", ascending=False)


# ============================================================
# DIÁRIO: leads (cadastrados)
# ============================================================
def daily_leads_metrics(df: pd.DataFrame, ref_date: dt.date) -> Dict:
    safe_col(df, COL_LEAD_DATE)
    df[COL_LEAD_DATE] = safe_to_date_series(df[COL_LEAD_DATE])

    mask = df[COL_LEAD_DATE] == ref_date
    dfd = df.loc[mask].copy() if mask.any() else df.copy()
    leads_count = int(dfd.shape[0])
    return {"ref_date": ref_date, "leads_count": leads_count}

def upsert_daily_leads(m: Dict):
    with db_conn() as conn:
        conn.execute("""
        INSERT INTO daily_leads(ref_date, leads_count)
        VALUES(?,?)
        ON CONFLICT(ref_date) DO UPDATE SET
            leads_count=excluded.leads_count;
        """, (m["ref_date"].isoformat(), m["leads_count"]))

def load_daily_leads() -> pd.DataFrame:
    with db_conn() as conn:
        df = pd.read_sql_query("SELECT * FROM daily_leads", conn)
    if df.empty:
        return df
    df["ref_date"] = pd.to_datetime(df["ref_date"]).dt.date
    return df.sort_values("ref_date", ascending=False)


# ============================================================
# CONVERSÃO: Abertas ÷ Cadastradas (com cor azul/vermelho)
# ============================================================
def conversion_table() -> pd.DataFrame:
    open_df = load_daily_open()
    leads_df = load_daily_leads()

    if open_df.empty and leads_df.empty:
        return pd.DataFrame()

    base = pd.merge(
        leads_df[["ref_date", "leads_count"]] if not leads_df.empty else pd.DataFrame(columns=["ref_date", "leads_count"]),
        open_df[["ref_date", "opened_count"]] if not open_df.empty else pd.DataFrame(columns=["ref_date", "opened_count"]),
        on="ref_date",
        how="outer"
    ).fillna(0)

    base["leads_count"] = base["leads_count"].astype(int)
    base["opened_count"] = base["opened_count"].astype(int)

    # Correto: abertas / cadastradas
    base["percent_num"] = base.apply(
        lambda r: (r["opened_count"] / r["leads_count"]) if r["leads_count"] > 0 else 0.0,
        axis=1
    )

    base["Data"] = base["ref_date"].apply(fmt_date)
    base["Cadastradas"] = base["leads_count"]
    base["Abertas"] = base["opened_count"]
    base["% Abertas/Cadastradas"] = base["percent_num"].apply(lambda x: f"{x*100:.1f}%".replace(".", ","))

    out = base[["ref_date", "Data", "Cadastradas", "Abertas", "% Abertas/Cadastradas", "percent_num"]].copy()
    out = out.sort_values("ref_date", ascending=False).reset_index(drop=True)
    return out

def style_conversion(df: pd.DataFrame):
    def row_style(row):
        v = float(row.get("percent_num", 0.0))
        if v >= CONV_TARGET:
            return ["background-color: rgba(33,150,243,0.10); color:#0b5394; font-weight:600;"] * len(row)
        return ["background-color: rgba(244,67,54,0.08); color:#a61c00; font-weight:600;"] * len(row)
    return df.style.apply(row_style, axis=1)


# ============================================================
# RELATÓRIOS: aberturas por dia/mês
# ============================================================
def report_aberturas_por_dia(open_df: pd.DataFrame) -> pd.DataFrame:
    if open_df.empty:
        return pd.DataFrame(columns=["Dia", "Contas abertas"])
    out = open_df.copy()
    out["Dia"] = out["ref_date"].apply(fmt_date)
    out["Contas abertas"] = out["opened_count"].astype(int)
    out = out.sort_values("ref_date", ascending=False)
    return out[["Dia", "Contas abertas"]].reset_index(drop=True)

def report_aberturas_por_mes(open_df: pd.DataFrame) -> pd.DataFrame:
    if open_df.empty:
        return pd.DataFrame(columns=["Mês", "Contas abertas"])
    df = open_df.copy()
    df["Mês"] = df["ref_date"].apply(lambda d: fmt_month_from_date(dt.date(d.year, d.month, 1)))
    out = df.groupby("Mês", as_index=False)["opened_count"].sum().rename(columns={"opened_count": "Contas abertas"})
    out["_sort"] = out["Mês"].apply(lambda x: dt.datetime.strptime("01/" + x, "%d/%m/%Y"))
    out = out.sort_values("_sort", ascending=False).drop(columns=["_sort"])
    return out.reset_index(drop=True)


# ============================================================
# PIX + STATUS
# ============================================================
def report_pix_status(df_day: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, int, int]:
    pix = clean_pix_series(df_day[COL_PIX_TYPE])
    has = pix.apply(is_pix_present)
    qtd_com = int(has.sum())
    qtd_sem = int((~has).sum())

    por_chave = (
        pix.loc[has]
           .value_counts()
           .rename_axis("Chave Pix")
           .reset_index(name="Quantidade")
    )

    status = (
        normalize_str(df_day[COL_STATUS])
        .replace("", "SEM STATUS")
        .fillna("SEM STATUS")
        .value_counts()
        .rename_axis("Status")
        .reset_index(name="Quantidade")
    )
    return por_chave, status, qtd_com, qtd_sem


# ============================================================
# FUNDAÇÕES por dia (mês/ano apenas) — você disse que está perfeito
# ============================================================
def report_fundacoes_mes_por_dia(df_day: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    x = df_day[[COL_OPEN_DATE, COL_FOUND_DATE]].copy()
    x[COL_OPEN_DATE] = safe_to_date_series(x[COL_OPEN_DATE])
    x[COL_FOUND_DATE] = safe_to_date_series(x[COL_FOUND_DATE])
    x = x.dropna(subset=[COL_OPEN_DATE, COL_FOUND_DATE])

    if x.empty:
        return {}

    x["Fundação (mês)"] = x[COL_FOUND_DATE].apply(lambda d: fmt_month_from_date(dt.date(d.year, d.month, 1)))
    out: Dict[str, pd.DataFrame] = {}

    for day, g in x.groupby(COL_OPEN_DATE):
        t = g["Fundação (mês)"].value_counts().rename_axis("Fundação (mês)").reset_index(name="Quantidade")
        t["_sort"] = t["Fundação (mês)"].apply(lambda s: dt.datetime.strptime("01/" + s, "%d/%m/%Y"))
        t = t.sort_values("_sort", ascending=True).drop(columns=["_sort"])
        out[fmt_date(day)] = t.reset_index(drop=True)

    out = dict(sorted(out.items(), key=lambda kv: dt.datetime.strptime(kv[0], "%d/%m/%Y"), reverse=True))
    return out


# ============================================================
# QUALIFICAÇÃO (tabela e contagem por nível)
# ============================================================
def qualification_table(df_day: pd.DataFrame) -> pd.DataFrame:
    df = df_day.copy()
    df = compute_qualification(df)
    safe_col(df, COL_CNPJ)

    df[COL_CNPJ] = normalize_str(df[COL_CNPJ]).str.replace(r"\D", "", regex=True)
    df[COL_OPEN_DATE] = safe_to_date_series(df[COL_OPEN_DATE])

    out = df[df["_qualified"]].copy()
    out["Data de abertura"] = out[COL_OPEN_DATE].apply(fmt_date)
    out = out.rename(columns={
        COL_CNPJ: "CNPJ",
        "_level": "Nível",
        "_winner": "Critério vencedor",
    })

    out = out[["CNPJ", "Data de abertura", "Nível", "Critério vencedor"]].copy()
    out["_sort"] = pd.to_datetime(out["Data de abertura"], format="%d/%m/%Y", errors="coerce")
    out = out.sort_values("_sort", ascending=False).drop(columns=["_sort"])

    return out.reset_index(drop=True)


# ============================================================
# REMUNERAÇÃO MENSAL (incremental por CNPJ)
# - Valor cheio do mês depende da FAIXA do mês (qtd qualificadas no mês)
# - Diferença a receber: max(0, valor_cheio_mes - maior_valor_ja_pago_antes_do_cnpj)
# ============================================================
def compute_monthly_clients(df: pd.DataFrame) -> pd.DataFrame:
    for c in [COL_CNPJ, COL_CRIT, COL_BY]:
        safe_col(df, c)

    # normaliza CNPJ
    df[COL_CNPJ] = normalize_str(df[COL_CNPJ]).str.replace(r"\D", "", regex=True)

    df = compute_qualification(df)

    # qualificada = True
    dfq = df[df["_qualified"]].copy()
    dfq = dfq[dfq[COL_CNPJ] != ""].copy()

    # Se o mesmo CNPJ repetir no mês, fica com o MAIOR nível
    dfq = dfq.sort_values("_level", ascending=False).groupby(COL_CNPJ, as_index=False).first()

    out = dfq[[COL_CNPJ, "_level", "_winner"]].copy()
    out = out.rename(columns={
        COL_CNPJ: "CNPJ",
        "_level": "Nível",
        "_winner": "Critério vencedor",
    })
    return out

def upsert_month(month_ref: str, clients: pd.DataFrame, qualified_count: int, mult: float, full_total: float):
    with db_conn() as conn:
        conn.execute("DELETE FROM monthly_clients WHERE month_ref = ?", (month_ref,))
        for _, r in clients.iterrows():
            conn.execute("""
            INSERT OR REPLACE INTO monthly_clients(month_ref, cnpj, level, winner_crit, full_value)
            VALUES(?,?,?,?,?)
            """, (
                month_ref,
                str(r["CNPJ"]),
                int(r["Nível"]),
                str(r["Critério vencedor"]),
                float(r["Valor cheio (mês)"]),
            ))

        conn.execute("""
        INSERT INTO monthly_summary(month_ref, qualified_count, multiplier, full_total, paid_before_total, due_total)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(month_ref) DO UPDATE SET
            qualified_count=excluded.qualified_count,
            multiplier=excluded.multiplier,
            full_total=excluded.full_total;
        """, (month_ref, int(qualified_count), float(mult), float(full_total), 0.0, 0.0))

def load_months() -> List[str]:
    with db_conn() as conn:
        rows = conn.execute("SELECT DISTINCT month_ref FROM monthly_clients").fetchall()
    months = [r[0] for r in rows]
    def mkey(x: str):
        return dt.datetime.strptime("01/" + x, "%d/%m/%Y")
    return sorted(months, key=mkey)

def recompute_incremental():
    months = load_months()
    if not months:
        return

    def mkey(x: str):
        return dt.datetime.strptime("01/" + x, "%d/%m/%Y")

    months_sorted = sorted(months, key=mkey)  # cronológico
    paid_max_by_cnpj: Dict[str, float] = {}

    with db_conn() as conn:
        for mref in months_sorted:
            dfm = pd.read_sql_query(
                "SELECT cnpj, full_value FROM monthly_clients WHERE month_ref = ?",
                conn,
                params=(mref,)
            )
            if dfm.empty:
                conn.execute("UPDATE monthly_summary SET paid_before_total=0, due_total=0 WHERE month_ref=?", (mref,))
                continue

            paid_before_sum = 0.0
            due_sum = 0.0

            for _, r in dfm.iterrows():
                cnpj = str(r["cnpj"])
                fullv = float(r["full_value"])
                prev_paid = float(paid_max_by_cnpj.get(cnpj, 0.0))

                # já pago antes (limitado)
                paid_before_sum += min(prev_paid, fullv)
                # diferença positiva
                due_sum += max(0.0, fullv - prev_paid)

                # atualiza teto pago
                paid_max_by_cnpj[cnpj] = max(prev_paid, fullv)

            conn.execute("""
            UPDATE monthly_summary
            SET paid_before_total=?, due_total=?
            WHERE month_ref=?
            """, (float(paid_before_sum), float(due_sum), mref))

def monthly_summary_table() -> pd.DataFrame:
    recompute_incremental()
    with db_conn() as conn:
        df = pd.read_sql_query("SELECT * FROM monthly_summary", conn)
    if df.empty:
        return df

    df["_sort"] = df["month_ref"].apply(lambda x: dt.datetime.strptime("01/" + x, "%d/%m/%Y"))
    df = df.sort_values("_sort", ascending=False).drop(columns=["_sort"])

    out = pd.DataFrame({
        "Mês": df["month_ref"],
        "Qualificadas (mês)": df["qualified_count"].astype(int),
        "Multiplicador": df["multiplier"].apply(lambda x: f"{float(x):.2f}".replace(".", ",")),
        "Receita total (mês)": df["full_total"].apply(fmt_money),
        "Já pago (antes)": df["paid_before_total"].apply(fmt_money),
        "A receber (diferença)": df["due_total"].apply(fmt_money),
    })
    return out.reset_index(drop=True)

def monthly_detail_table(month_ref: str) -> pd.DataFrame:
    recompute_incremental()
    months = load_months()
    if month_ref not in months:
        return pd.DataFrame()

    def mkey(x: str):
        return dt.datetime.strptime("01/" + x, "%d/%m/%Y")

    months_sorted = sorted(months, key=mkey)
    paid_max_by_cnpj: Dict[str, float] = {}

    with db_conn() as conn:
        for mref in months_sorted:
            dfm = pd.read_sql_query(
                "SELECT cnpj, level, winner_crit, full_value FROM monthly_clients WHERE month_ref = ?",
                conn,
                params=(mref,)
            )

            if dfm.empty:
                continue

            if mref == month_ref:
                rows = []
                for _, r in dfm.iterrows():
                    cnpj = str(r["cnpj"])
                    fullv = float(r["full_value"])
                    prev_paid = float(paid_max_by_cnpj.get(cnpj, 0.0))
                    diff = max(0.0, fullv - prev_paid)

                    rows.append({
                        "CNPJ": cnpj,
                        "Nível": int(r["level"]),
                        "Critério vencedor": str(r["winner_crit"]),
                        "Valor cheio (mês)": fullv,
                        "Já pago (antes)": min(prev_paid, fullv),
                        "Diferença a receber": diff,
                    })

                out = pd.DataFrame(rows)
                if out.empty:
                    return out

                out = out.sort_values("Diferença a receber", ascending=False)

                # formata dinheiro
                out["Valor cheio (mês)"] = out["Valor cheio (mês)"].apply(fmt_money)
                out["Já pago (antes)"] = out["Já pago (antes)"].apply(fmt_money)
                out["Diferença a receber"] = out["Diferença a receber"].apply(fmt_money)

                return out.reset_index(drop=True)

            # atualiza teto até antes do mês alvo
            for _, r in dfm.iterrows():
                cnpj = str(r["cnpj"])
                fullv = float(r["full_value"])
                paid_max_by_cnpj[cnpj] = max(float(paid_max_by_cnpj.get(cnpj, 0.0)), fullv)

    return pd.DataFrame()


# ============================================================
# APP
# ============================================================
st.set_page_config(page_title="Assis & Mollerke", layout="wide")
apply_theme()

if not login_gate():
    header()
    st.stop()

header()
st.divider()

# ------------------------------------------------------------
# Importação diária com data obrigatória (calendário)
# ------------------------------------------------------------
st.markdown("## Importação do dia")

c0, c1, c2 = st.columns([1.2, 2.4, 2.4], vertical_alignment="top")
with c0:
    ref_date = st.date_input("Data de referência (obrigatório)", value=dt.date.today(), format="DD/MM/YYYY")
    st.caption("Selecione a data correta do arquivo antes de enviar.")

with c1:
    st.markdown("**Planilha C6 (Visão Cliente) — diária**")
    up_open = st.file_uploader("Enviar arquivo (.xlsx)", type=["xlsx"], key="open_daily")

with c2:
    st.markdown("**Planilha Leads — diária**")
    up_leads = st.file_uploader("Enviar arquivo (.xlsx)", type=["xlsx"], key="leads_daily")

df_day_open = None
if up_open is not None:
    df = load_excel(up_open.getvalue())
    m = daily_open_metrics(df, ref_date)
    upsert_daily_open(m)
    df_day_open = m["df_day"]

if up_leads is not None:
    df = load_excel(up_leads.getvalue())
    m2 = daily_leads_metrics(df, ref_date)
    upsert_daily_leads(m2)

st.divider()

# ------------------------------------------------------------
# Resumo do dia
# ------------------------------------------------------------
st.markdown("## Resumo executivo (dia)")

open_hist = load_daily_open()
leads_hist = load_daily_leads()

sel_open = open_hist[open_hist["ref_date"] == ref_date].head(1) if not open_hist.empty else pd.DataFrame()
sel_leads = leads_hist[leads_hist["ref_date"] == ref_date].head(1) if not leads_hist.empty else pd.DataFrame()

opened_today = int(sel_open["opened_count"].iloc[0]) if not sel_open.empty else 0
saldo_today = float(sel_open["saldo_total"].iloc[0]) if not sel_open.empty else 0.0
pix_with_today = int(sel_open["pix_with"].iloc[0]) if not sel_open.empty else 0
pix_without_today = int(sel_open["pix_without"].iloc[0]) if not sel_open.empty else 0
dom_today = int(sel_open["domicilio_c6"].iloc[0]) if not sel_open.empty else 0
qual_today = int(sel_open["qualified_count"].iloc[0]) if not sel_open.empty else 0
leads_today = int(sel_leads["leads_count"].iloc[0]) if not sel_leads.empty else 0

conv_today = (opened_today / leads_today) if leads_today > 0 else 0.0

k1, k2, k3, k4 = st.columns(4)
k1.metric("Contas abertas (dia)", fmt_int(opened_today))
k2.metric("Leads cadastrados (dia)", fmt_int(leads_today))
k3.metric("Conversão do dia", f"{conv_today*100:.1f}%".replace(".", ","))
k4.metric("Saldo total (dia)", fmt_money(saldo_today))

k5, k6, k7, k8 = st.columns(4)
k5.metric("Clientes com Pix", fmt_int(pix_with_today))
k6.metric("Clientes sem Pix", fmt_int(pix_without_today))
k7.metric("Domicílio C6", fmt_int(dom_today))
k8.metric("Contas qualificadas (dia)", fmt_int(qual_today))

st.caption(f"Data de referência: **{fmt_date(ref_date)}**")
st.divider()

# ------------------------------------------------------------
# Conversão (mês e tabela diária com cores azul/vermelho)
# ------------------------------------------------------------
st.markdown("## Conversão (Abertas ÷ Cadastradas)")

df_conv = conversion_table()

if not df_conv.empty:
    df_conv["Mês"] = df_conv["ref_date"].apply(lambda d: fmt_month_from_date(dt.date(d.year, d.month, 1)))
    months_available = df_conv["Mês"].unique().tolist()
    # mais recente primeiro
    months_available = sorted(months_available, key=lambda x: dt.datetime.strptime("01/" + x, "%d/%m/%Y"), reverse=True)

    month_sel = st.selectbox("Selecione o mês", options=months_available, index=0)
    aux = df_conv[df_conv["Mês"] == month_sel].copy()
    aux = aux.sort_values("ref_date", ascending=False)

    opened_month = int(aux["Abertas"].sum())
    leads_month = int(aux["Cadastradas"].sum())
    rate_month = (opened_month / leads_month) if leads_month > 0 else 0.0

    m1, m2, m3 = st.columns(3)
    m1.metric("Abertas no mês", fmt_int(opened_month))
    m2.metric("Cadastradas no mês", fmt_int(leads_month))
    m3.metric("Conversão do mês", f"{rate_month*100:.1f}%".replace(".", ","))

    show = aux[["Data", "Cadastradas", "Abertas", "% Abertas/Cadastradas", "percent_num"]].copy()

    st.dataframe(
        style_conversion(show).drop(columns=["percent_num"]),
        use_container_width=True,
        hide_index=True
    )

else:
    st.info("Importe pelo menos 1 dia de Visão Cliente + 1 dia de Leads para gerar a conversão.")

st.divider()

# ------------------------------------------------------------
# Relatórios (diário)
# ------------------------------------------------------------
st.markdown("## Relatórios (diário)")

tabA, tabB, tabC, tabD = st.tabs([
    "Aberturas",
    "Fundações (por dia)",
    "Pix + Status",
    "Qualificação (níveis e critérios)"
])

with tabA:
    st.markdown("### Contas abertas (histórico)")
    a1, a2 = st.columns(2)
    with a1:
        st.markdown("**Por dia (mais recente → mais antigo)**")
        st.dataframe(report_aberturas_por_dia(open_hist), use_container_width=True, hide_index=True)
    with a2:
        st.markdown("**Por mês**")
        st.dataframe(report_aberturas_por_mes(open_hist), use_container_width=True, hide_index=True)

with tabB:
    st.markdown("### Fundações por dia (mês de fundação)")
    if df_day_open is None:
        st.warning("Envie a planilha diária (Visão Cliente) para ver o detalhe de fundações por dia.")
    else:
        by_day = report_fundacoes_mes_por_dia(df_day_open)
        if not by_day:
            st.info("Não encontrei datas válidas de fundação e abertura neste arquivo.")
        else:
            st.caption("Clique em um dia para ver a distribuição por mês de fundação.")
            for day_str, t in by_day.items():
                with st.expander(f"Dia {day_str}"):
                    st.dataframe(t, use_container_width=True, hide_index=True)

with tabC:
    st.markdown("### Pix e Status")
    if df_day_open is None:
        st.warning("Envie a planilha diária (Visão Cliente) para ver Pix e Status.")
    else:
        por_chave, status_df, qtd_com, qtd_sem = report_pix_status(df_day_open)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Com Pix", fmt_int(qtd_com))
        c2.metric("Sem Pix", fmt_int(qtd_sem))
        c3.metric("Total (arquivo)", fmt_int(int(df_day_open.shape[0])))
        c4.metric("Saldo (arquivo)", fmt_money(float(pd.to_numeric(df_day_open[COL_SALDO], errors="coerce").fillna(0).sum())))

        st.markdown("**Pix por tipo de chave**")
        st.dataframe(por_chave, use_container_width=True, hide_index=True)

        st.markdown("**Status**")
        st.dataframe(status_df, use_container_width=True, hide_index=True)

with tabD:
    st.markdown("### Qualificação (nível vencedor e critério vencedor)")
    if df_day_open is None:
        st.warning("Envie a planilha diária (Visão Cliente) para ver qualificação.")
    else:
        qt = compute_qualification(df_day_open.copy())
        qual = qt[qt["_qualified"]].copy()
        lvl_counts = qual["_level"].value_counts().to_dict()

        s1, s2, s3, s4, s5 = st.columns(5)
        s1.metric("Qualificadas", fmt_int(int(qual.shape[0])))
        s2.metric("Nível 1", fmt_int(int(lvl_counts.get(1, 0))))
        s3.metric("Nível 2", fmt_int(int(lvl_counts.get(2, 0))))
        s4.metric("Nível 3", fmt_int(int(lvl_counts.get(3, 0))))
        s5.metric("Nível 4", fmt_int(int(lvl_counts.get(4, 0))))

        st.dataframe(qualification_table(df_day_open), use_container_width=True, hide_index=True)

st.divider()

# ------------------------------------------------------------
# Remuneração mensal (incremental)
# ------------------------------------------------------------
st.markdown("## Remuneração mensal (incremental)")

st.caption("Envie arquivos mensais (Nov/25 em diante). O sistema calcula o valor cheio do mês e desconta o que já foi pago antes por CNPJ (somente diferença positiva).")

monthly_files = st.file_uploader(
    "Importar planilhas mensais (.xlsx)",
    type=["xlsx"],
    accept_multiple_files=True,
    key="monthly_files"
)

if monthly_files:
    for f in monthly_files:
        dfm = load_excel(f.getvalue())

        # mês manual obrigatório (para evitar erro)
        month_ref = st.text_input(f"Mês de referência (mm/aaaa) — {f.name}", value="11/2025")

        clients_base = compute_monthly_clients(dfm)

        # qtd qualificadas do mês (por CNPJ único)
        qtd_q = int(clients_base.shape[0])
        mult = tier_multiplier(qtd_q)

        # valor cheio por CNPJ no mês
        def full_value(lv: int) -> float:
            return float(BASE_PAYOUT.get(int(lv), 0.0) * mult)

        clients_base["Valor cheio (mês)"] = clients_base["Nível"].apply(full_value)

        full_total = float(clients_base["Valor cheio (mês)"].sum())

        upsert_month(month_ref, clients_base, qtd_q, mult, full_total)

        st.success(
            f"{f.name} importado | Mês: {month_ref} | Qualificadas (CNPJ únicos): {fmt_int(qtd_q)} | "
            f"Multiplicador: {str(mult).replace('.', ',')} | Receita total (cheia): {fmt_money(full_total)}"
        )

# resumo por mês (com desconto incremental)
msum = monthly_summary_table()
if not msum.empty:
    st.markdown("### Resumo por mês (valor cheio × já pago × a receber)")
    st.dataframe(msum, use_container_width=True, hide_index=True)

    months = msum["Mês"].tolist()
    month_pick = st.selectbox("Detalhar mês", options=months, index=0)

    st.markdown(f"### Detalhamento por CNPJ — {month_pick}")
    det = monthly_detail_table(month_pick)
    st.dataframe(det, use_container_width=True, hide_index=True)
else:
    st.info("Importe planilhas mensais para gerar o resumo de remuneração.")
