import os
import io
import re
import sqlite3
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Tuple, List

import pandas as pd
import streamlit as st


# ============================================================
# CONFIG
# ============================================================
APP_TITLE = "Painel de controle Assis e Mollerke parceiro Banco C6"
APP_SUBTITLE = "Painel executivo — Visão Cliente C6 + Leads + Remuneração incremental"
LOGIN_USER = "admin"
LOGIN_PASS = "123456"

# Arquivo de logo (deve estar no mesmo diretório do app.py)
LOGO_FILENAME = "LOGO CORRETA.png"

# Colunas (Visão Cliente)
COL_OPEN_DATE = "DT_CONTA_CRIADA"
COL_FOUND_DATE = "DT_FUNDACAO_EMPRESA"
COL_PIX_TYPE = "CHAVES_PIX_FORTE"
COL_SALDO = "VL_SALDO_MEDIO_MENSALIZADO"
COL_STATUS = "STATUS_CC"
COL_DOMICILIO = "BANCO_DOMICILIO"
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"
COL_CNPJ = "CD_CPF_CNPJ_CLIENTE"

# Colunas (Leads)
COL_LEAD_DATE = "DATA_HORA_CADASTRO"

# Remuneração base (faixa 1.0)
BASE_PAYOUT = {1: 140.00, 2: 230.00, 3: 400.00, 4: 540.00}

def tier_multiplier(qtd_qualificadas: int) -> float:
    """
    Faixa do mês (pela quantidade de qualificadas do mês):
    - até 49 -> 1.0
    - 50 a 149 -> 1.1
    - 150 a 349 -> 1.25
    - 350+ -> 1.5
    """
    if qtd_qualificadas <= 49:
        return 1.0
    if qtd_qualificadas <= 149:
        return 1.1
    if qtd_qualificadas <= 349:
        return 1.25
    return 1.5

# Conversão alvo
CONV_TARGET = 0.20  # 20%

# Persistência
DATA_DIR = Path("data_store")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "assis_mollerke.db"


# ============================================================
# UTIL: formatação (pt-BR)
# ============================================================
def fmt_int(n: int) -> str:
    return f"{int(n):,}".replace(",", ".")

def fmt_money(v: float) -> str:
    # "R$ 793.104,30"
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

def fmt_month(d: dt.date) -> str:
    return d.strftime("%m/%Y")

def parse_any_date(x) -> Optional[dt.date]:
    if x is None or pd.isna(x):
        return None
    try:
        ts = pd.to_datetime(x, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.date()
    except Exception:
        return None


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
# Leitura Excel
# ============================================================
def load_excel(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")

def safe_col(df: pd.DataFrame, col: str):
    if col not in df.columns:
        df[col] = pd.NA

def normalize_str(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").str.strip()

def contains_c6(val: str) -> bool:
    return "c6" in str(val).lower()

def clean_pix_series(s: pd.Series) -> pd.Series:
    s = s.astype("string").fillna("").str.strip().str.upper()
    s = s.str.replace("'", "", regex=False)
    return s

def is_pix_present(v: str) -> bool:
    v = str(v).strip().upper()
    if v in ["", "-", "NAN", "NONE", "SEM", "SEM PIX"]:
        return False
    return True


# ============================================================
# QUALIFICAÇÃO (nível vencedor e critério vencedor)
# ============================================================
CRIT_PATTERNS = [
    ("CASH IN", re.compile(r"CASH\s*IN\s*:\s*(\d+)", re.IGNORECASE)),
    ("DOMICILIO", re.compile(r"DOMIC[IÍ]LIO\s*:\s*(\d+)", re.IGNORECASE)),
    ("SALDO MEDIO", re.compile(r"SALDO\s*M[ÉE]DIO\s*:\s*(\d+)", re.IGNORECASE)),
    ("SPENDING", re.compile(r"SPENDING\s*:\s*(\d+)", re.IGNORECASE)),
    ("CONTA GLOBAL", re.compile(r"CONTA\s*GLOBAL\s*:\s*(\d+)", re.IGNORECASE)),
]

def extract_level_and_winner(txt: str) -> Tuple[int, str]:
    """
    Retorna:
    - level: maior valor 1..4 encontrado nos critérios (0 se não qualifica)
    - winner: nome do critério vencedor + (valor)
    """
    if not isinstance(txt, str):
        return 0, ""

    t = txt.upper()
    found: List[Tuple[str, int]] = []

    # Extrai os valores padrão "CRITERIO: N"
    for name, pat in CRIT_PATTERNS:
        m = pat.search(txt)
        if m:
            try:
                v = int(m.group(1))
            except Exception:
                v = 0
            found.append((name, v))

    # Fallback: pegar números após ":" se padrão diferente
    if not found:
        nums = re.findall(r":\s*(\d+)", txt)
        if nums:
            vals = [int(x) for x in nums if x.isdigit()]
            if vals:
                lv = max(vals)
                lv = max(0, min(4, lv))
                return lv, f"NÍVEL ({lv})"

    if not found:
        return 0, ""

    # maior valor
    maxv = max(v for _, v in found)
    maxv = max(0, min(4, maxv))

    if maxv <= 0:
        return 0, ""

    winners = [name for name, v in found if v == maxv]
    # se empatar, mostra o primeiro
    w = winners[0] if winners else "CRITÉRIO"
    return maxv, f"{w} ({maxv})"

def compute_qualification(df: pd.DataFrame) -> pd.DataFrame:
    """
    Cria colunas:
    - _level (0..4)
    - _winner (texto)
    - _qualified (bool)
    """
    safe_col(df, COL_CRIT)

    levels = []
    winners = []
    for x in df[COL_CRIT].tolist():
        lv, win = extract_level_and_winner(x)
        levels.append(lv)
        winners.append(win)

    df["_level"] = pd.Series(levels, index=df.index).fillna(0).astype(int)
    df["_winner"] = pd.Series(winners, index=df.index).fillna("").astype(str)
    df["_qualified"] = df["_level"] >= 1
    return df


# ============================================================
# LOGIN / UI
# ============================================================
def apply_theme():
    # azul-marinho inspirado na marca
    st.markdown(
        """
        <style>
        .block-container { padding-top: 1.2rem; }
        h1, h2, h3 { color: #1f2a44; }
        .am-title { font-weight: 800; letter-spacing: 0.2px; }
        .am-sub { color: #5f6b84; margin-top: -8px; }
        .am-card { border: 1px solid #e9edf5; border-radius: 14px; padding: 14px 16px; background: #ffffff; }
        .am-chip-ok { background: rgba(33, 150, 243, 0.10); color: #0b5394; padding: 4px 10px; border-radius: 999px; font-weight: 600; display: inline-block; }
        .am-chip-bad { background: rgba(244, 67, 54, 0.10); color: #a61c00; padding: 4px 10px; border-radius: 999px; font-weight: 600; display: inline-block; }
        .stDataFrame { border-radius: 12px; overflow: hidden; }
        </style>
        """,
        unsafe_allow_html=True
    )

def header():
    # Logo + título sem cortar
    logo_path = Path(__file__).parent / LOGO_FILENAME
    c1, c2 = st.columns([1, 5], vertical_alignment="center")
    with c1:
        if logo_path.exists():
            st.image(str(logo_path), use_container_width=True)
        else:
            st.write("")  # sem erro na tela
    with c2:
        st.markdown(f"<h1 class='am-title'>{APP_TITLE}</h1>", unsafe_allow_html=True)
        st.markdown(f"<div class='am-sub'>{APP_SUBTITLE}</div>", unsafe_allow_html=True)

def login_gate() -> bool:
    st.sidebar.markdown("## Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")

    if st.sidebar.button("Entrar"):
        st.session_state["logged_in"] = (u == LOGIN_USER and p == LOGIN_PASS)
        if not st.session_state["logged_in"]:
            st.sidebar.error("Usuário ou senha inválidos.")

    return st.session_state.get("logged_in", False)


# ============================================================
# DIÁRIO: Visão Cliente (aberturas)
# ============================================================
def daily_open_metrics(df: pd.DataFrame, ref_date: dt.date) -> Dict:
    # garante colunas
    for c in [COL_OPEN_DATE, COL_FOUND_DATE, COL_PIX_TYPE, COL_SALDO, COL_STATUS, COL_DOMICILIO, COL_CRIT, COL_CNPJ]:
        safe_col(df, c)

    # datas
    df[COL_OPEN_DATE] = pd.to_datetime(df[COL_OPEN_DATE], errors="coerce").dt.date
    df[COL_FOUND_DATE] = pd.to_datetime(df[COL_FOUND_DATE], errors="coerce").dt.date

    # saldo
    df[COL_SALDO] = pd.to_numeric(df[COL_SALDO], errors="coerce").fillna(0.0)

    # pix
    pix = clean_pix_series(df[COL_PIX_TYPE])
    pix_with = int(pix.apply(is_pix_present).sum())
    pix_without = int((~pix.apply(is_pix_present)).sum())

    # domicilio
    dom = normalize_str(df[COL_DOMICILIO])
    domicilio_c6 = int(dom.apply(contains_c6).sum())

    # qualificação / nível
    df = compute_qualification(df)

    # filtra dia (se a coluna existir com datas válidas)
    mask_day = df[COL_OPEN_DATE] == ref_date
    if mask_day.any():
        dfd = df.loc[mask_day].copy()
    else:
        # fallback: considera o arquivo inteiro como o dia
        dfd = df.copy()

    opened_count = int(dfd.shape[0])
    saldo_total = float(dfd[COL_SALDO].sum())
    qualified_count = int(dfd["_qualified"].sum())

    # níveis no dia
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
        "df_day": dfd
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
# DIÁRIO: Leads (cadastrados)
# ============================================================
def daily_leads_metrics(df: pd.DataFrame, ref_date: dt.date) -> Dict:
    safe_col(df, COL_LEAD_DATE)
    df[COL_LEAD_DATE] = pd.to_datetime(df[COL_LEAD_DATE], errors="coerce").dt.date

    mask = df[COL_LEAD_DATE] == ref_date
    if mask.any():
        dfd = df.loc[mask].copy()
    else:
        # fallback: arquivo inteiro
        dfd = df.copy()

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
# CONVERSÃO: Abertas ÷ Cadastradas
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

    # Conversão correta: abertas / cadastradas
    base["percent_num"] = base.apply(
        lambda r: (r["opened_count"] / r["leads_count"]) if r["leads_count"] > 0 else 0.0,
        axis=1
    )

    base["Data"] = base["ref_date"].apply(fmt_date)
    base["Cadastradas"] = base["leads_count"]
    base["Abertas"] = base["opened_count"]
    base["% Abertas/Cadastradas"] = base["percent_num"].apply(lambda x: f"{x*100:.1f}%".replace(".", ","))

    def indicator_chip(x: float) -> str:
        return "<span class='am-chip-ok'>Dentro do alvo</span>" if x >= CONV_TARGET else "<span class='am-chip-bad'>Abaixo do alvo</span>"

    base["Indicador"] = base["percent_num"].apply(indicator_chip)

    out = base[["ref_date", "Data", "Cadastradas", "Abertas", "% Abertas/Cadastradas", "Indicador", "percent_num"]].copy()
    out = out.sort_values("ref_date", ascending=False)
    return out

def month_summary(df_conv: pd.DataFrame, month_ref: str) -> Dict:
    # month_ref: "01/2026"
    if df_conv.empty:
        return {"opened": 0, "leads": 0, "rate": 0.0}

    def to_month(d: dt.date) -> str:
        return dt.date(d.year, d.month, 1).strftime("%m/%Y")

    df = df_conv.copy()
    df["month_ref"] = df["ref_date"].apply(to_month)
    mdf = df[df["month_ref"] == month_ref]
    opened = int(mdf["Abertas"].sum())
    leads = int(mdf["Cadastradas"].sum())
    rate = (opened / leads) if leads > 0 else 0.0
    return {"opened": opened, "leads": leads, "rate": rate}


# ============================================================
# REMUNERAÇÃO MENSAL (incremental por CNPJ)
# ============================================================
def detect_month_from_monthly_file(df: pd.DataFrame) -> Optional[str]:
    # preferencial: coluna REFERENCIA (data do mês)
    if "REFERENCIA" in df.columns:
        d = parse_any_date(df["REFERENCIA"].dropna().iloc[0]) if df["REFERENCIA"].dropna().shape[0] else None
        if d:
            return fmt_month(dt.date(d.year, d.month, 1))
    return None

def compute_monthly_remuneration(df: pd.DataFrame, month_ref: str) -> Tuple[pd.DataFrame, Dict]:
    """
    Retorna:
    - clientes_mes: colunas [cnpj, level, winner_crit, full_value]
    - summary: dict com qualified_count, multiplier, full_total
    """
    # garante colunas
    for c in [COL_CNPJ, COL_CRIT]:
        safe_col(df, c)

    # cnpj normalizado
    df[COL_CNPJ] = normalize_str(df[COL_CNPJ]).str.replace(r"\D", "", regex=True)

    df = compute_qualification(df)

    # somente qualificadas
    dfq = df[df["_qualified"]].copy()

    qualified_count = int(dfq.shape[0])
    mult = tier_multiplier(qualified_count)

    # valor cheio do mês
    dfq["full_value"] = dfq["_level"].apply(lambda lv: float(BASE_PAYOUT.get(int(lv), 0.0) * mult))

    clients = dfq[[COL_CNPJ, "_level", "_winner", "full_value"]].copy()
    clients = clients.rename(columns={
        COL_CNPJ: "CNPJ",
        "_level": "Nível",
        "_winner": "Critério vencedor",
        "full_value": "Valor cheio (mês)"
    })

    # remove vazios
    clients = clients[clients["CNPJ"] != ""].copy()

    # se existir duplicidade do mesmo CNPJ no mês, fica com o maior valor (garantia)
    clients = (
        clients.sort_values("Valor cheio (mês)", ascending=False)
               .groupby("CNPJ", as_index=False)
               .first()
    )

    summary = {
        "month_ref": month_ref,
        "qualified_count": int(clients.shape[0]),
        "multiplier": float(mult),
        "full_total": float(clients["Valor cheio (mês)"].sum())
    }
    return clients, summary

def upsert_month(month_ref: str, clients: pd.DataFrame, summary: Dict):
    """
    Salva no banco:
    - monthly_clients (month_ref + cnpj)
    - monthly_summary (apenas valores parciais: full_total, qualified_count, multiplier)
    O cálculo incremental (paid_before/due) é recalculado ao final com todos os meses.
    """
    with db_conn() as conn:
        # limpa mês e reinsere (pra permitir reimportar)
        conn.execute("DELETE FROM monthly_clients WHERE month_ref = ?", (month_ref,))
        for _, r in clients.iterrows():
            conn.execute("""
            INSERT OR REPLACE INTO monthly_clients(month_ref, cnpj, level, winner_crit, full_value)
            VALUES(?,?,?,?,?)
            """, (month_ref, r["CNPJ"], int(r["Nível"]), str(r["Critério vencedor"]), float(r["Valor cheio (mês)"])))

        conn.execute("""
        INSERT INTO monthly_summary(month_ref, qualified_count, multiplier, full_total, paid_before_total, due_total)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(month_ref) DO UPDATE SET
            qualified_count=excluded.qualified_count,
            multiplier=excluded.multiplier,
            full_total=excluded.full_total;
        """, (month_ref, int(summary["qualified_count"]), float(summary["multiplier"]), float(summary["full_total"]), 0.0, 0.0))

def load_months() -> List[str]:
    with db_conn() as conn:
        rows = conn.execute("SELECT DISTINCT month_ref FROM monthly_clients").fetchall()
    months = sorted([r[0] for r in rows], key=lambda x: dt.datetime.strptime("01/" + x, "%d/%m/%Y"))
    return months

def recompute_incremental_all_months():
    """
    Recalcula paid_before_total e due_total por mês, e também permite
    relatório por CNPJ (pago antes / diferença).
    """
    months = load_months()
    if not months:
        return

    # ordem cronológica
    months_sorted = sorted(months, key=lambda x: dt.datetime.strptime("01/" + x, "%d/%m/%Y"))

    paid_max_by_cnpj: Dict[str, float] = {}  # maior valor já "pago" antes por CNPJ

    with db_conn() as conn:
        for mref in months_sorted:
            dfm = pd.read_sql_query(
                "SELECT cnpj, level, winner_crit, full_value FROM monthly_clients WHERE month_ref = ?",
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
                full_value = float(r["full_value"])
                prev_paid = float(paid_max_by_cnpj.get(cnpj, 0.0))

                # pago antes (limitado ao valor cheio do mês)
                paid_here = min(prev_paid, full_value)
                diff = max(0.0, full_value - prev_paid)

                paid_before_sum += paid_here
                due_sum += diff

                # atualiza o máximo já atingido (pagamento "cheio" daquele mês vira novo teto)
                paid_max_by_cnpj[cnpj] = max(prev_paid, full_value)

            conn.execute("""
            UPDATE monthly_summary
            SET paid_before_total=?, due_total=?
            WHERE month_ref=?
            """, (float(paid_before_sum), float(due_sum), mref))

def monthly_summary_table() -> pd.DataFrame:
    recompute_incremental_all_months()
    with db_conn() as conn:
        df = pd.read_sql_query("SELECT * FROM monthly_summary", conn)
    if df.empty:
        return df

    # ordenar por data
    df["_sort"] = df["month_ref"].apply(lambda x: dt.datetime.strptime("01/" + x, "%d/%m/%Y"))
    df = df.sort_values("_sort", ascending=False).drop(columns=["_sort"])

    df["Mês"] = df["month_ref"]
    df["Qualificadas (mês)"] = df["qualified_count"].astype(int)
    df["Multiplicador"] = df["multiplier"].apply(lambda x: f"{float(x):.2f}".replace(".", ","))
    df["Receita total (mês)"] = df["full_total"].apply(fmt_money)
    df["Já pago (antes)"] = df["paid_before_total"].apply(fmt_money)
    df["A receber (diferença)"] = df["due_total"].apply(fmt_money)

    return df[["Mês", "Qualificadas (mês)", "Multiplicador", "Receita total (mês)", "Já pago (antes)", "A receber (diferença)"]]

def monthly_clients_detail(month_ref: str) -> pd.DataFrame:
    recompute_incremental_all_months()

    # para detalhar pago antes por CNPJ no mês selecionado, precisamos simular histórico até o mês anterior
    months = load_months()
    if not months:
        return pd.DataFrame()

    months_sorted = sorted(months, key=lambda x: dt.datetime.strptime("01/" + x, "%d/%m/%Y"))
    if month_ref not in months_sorted:
        return pd.DataFrame()

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
                # constrói detalhe com prev_paid antes de atualizar
                rows = []
                for _, r in dfm.iterrows():
                    cnpj = str(r["cnpj"])
                    fullv = float(r["full_value"])
                    prev = float(paid_max_by_cnpj.get(cnpj, 0.0))
                    due = max(0.0, fullv - prev)
                    paid_here = min(prev, fullv)

                    rows.append({
                        "CNPJ": cnpj,
                        "Nível": int(r["level"]),
                        "Critério vencedor": str(r["winner_crit"]),
                        "Valor cheio (mês)": fullv,
                        "Já pago (antes)": paid_here,
                        "Diferença a receber": due,
                    })

                out = pd.DataFrame(rows)

                # formata para exibição
                out["Valor cheio (mês)"] = out["Valor cheio (mês)"].apply(fmt_money)
                out["Já pago (antes)"] = out["Já pago (antes)"].apply(fmt_money)
                out["Diferença a receber"] = out["Diferença a receber"].apply(fmt_money)

                # ordena por maior diferença
                out["_sort"] = out["Diferença a receber"].apply(lambda x: float(x.replace("R$ ", "").replace(".", "").replace(",", ".")) if isinstance(x, str) else 0.0)
                out = out.sort_values("_sort", ascending=False).drop(columns=["_sort"])

                return out.reset_index(drop=True)

            # atualiza teto até antes do mês escolhido
            for _, r in dfm.iterrows():
                cnpj = str(r["cnpj"])
                fullv = float(r["full_value"])
                prev = float(paid_max_by_cnpj.get(cnpj, 0.0))
                paid_max_by_cnpj[cnpj] = max(prev, fullv)

    return pd.DataFrame()


# ============================================================
# RELATÓRIOS (Visão Cliente)
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
    df["month_ref"] = df["ref_date"].apply(lambda d: fmt_month(dt.date(d.year, d.month, 1)))
    out = df.groupby("month_ref", as_index=False)["opened_count"].sum()
    out = out.rename(columns={"month_ref": "Mês", "opened_count": "Contas abertas"})
    # ordena desc
    out["_sort"] = out["Mês"].apply(lambda x: dt.datetime.strptime("01/" + x, "%d/%m/%Y"))
    out = out.sort_values("_sort", ascending=False).drop(columns=["_sort"])
    return out.reset_index(drop=True)

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

def report_fundacoes_mes_por_dia(df_day: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """
    Para cada dia de abertura (no arquivo diário ou conjunto),
    gera contagem por mês/ano de fundação.
    """
    x = df_day[[COL_OPEN_DATE, COL_FOUND_DATE]].copy()
    x[COL_OPEN_DATE] = pd.to_datetime(x[COL_OPEN_DATE], errors="coerce").dt.date
    x[COL_FOUND_DATE] = pd.to_datetime(x[COL_FOUND_DATE], errors="coerce").dt.date
    x = x.dropna(subset=[COL_OPEN_DATE, COL_FOUND_DATE])

    if x.empty:
        return {}

    x["Fundação (mês)"] = x[COL_FOUND_DATE].apply(lambda d: fmt_month(dt.date(d.year, d.month, 1)))
    out: Dict[str, pd.DataFrame] = {}

    for day, g in x.groupby(COL_OPEN_DATE):
        t = g["Fundação (mês)"].value_counts().rename_axis("Fundação (mês)").reset_index(name="Quantidade")
        # ordena cronológico crescente dentro do expander
        t["_sort"] = t["Fundação (mês)"].apply(lambda s: dt.datetime.strptime("01/" + s, "%d/%m/%Y"))
        t = t.sort_values("_sort", ascending=True).drop(columns=["_sort"])
        out[fmt_date(day)] = t.reset_index(drop=True)

    # dias mais recentes primeiro
    out = dict(sorted(out.items(), key=lambda kv: dt.datetime.strptime(kv[0], "%d/%m/%Y"), reverse=True))
    return out

def qualification_table(df_day: pd.DataFrame) -> pd.DataFrame:
    df = df_day.copy()
    df = compute_qualification(df)

    # CNPJ
    df[COL_CNPJ] = normalize_str(df[COL_CNPJ]).str.replace(r"\D", "", regex=True)

    # data abertura
    df[COL_OPEN_DATE] = pd.to_datetime(df[COL_OPEN_DATE], errors="coerce").dt.date

    # mantém apenas colunas úteis
    out = df[df["_qualified"]].copy()
    out = out.rename(columns={
        COL_CNPJ: "CNPJ",
        COL_OPEN_DATE: "Data de abertura",
        "_level": "Nível",
        "_winner": "Critério vencedor",
    })

    out["Data de abertura"] = out["Data de abertura"].apply(fmt_date)

    out = out[["CNPJ", "Data de abertura", "Nível", "Critério vencedor"]]
    # ordena: recentes primeiro
    # se data vazia, joga pro fim
    out["_sort"] = pd.to_datetime(out["Data de abertura"], format="%d/%m/%Y", errors="coerce")
    out = out.sort_values("_sort", ascending=False).drop(columns=["_sort"])

    # remove index
    return out.reset_index(drop=True)


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
# Importação do dia (OBRIGATÓRIO selecionar data)
# ------------------------------------------------------------
st.markdown("## Importação do dia")

cA, cB, cC = st.columns([1.2, 2.5, 2.5], vertical_alignment="top")
with cA:
    ref_date = st.date_input(
        "Data de referência (obrigatório)",
        value=dt.date.today(),
        format="DD/MM/YYYY"
    )
    st.caption("Use o calendário para escolher o dia correto do arquivo.")

with cB:
    st.markdown("**Planilha C6 (Visão Cliente) — diária**")
    up_open = st.file_uploader("Enviar arquivo (.xlsx)", type=["xlsx"], key="open_daily")

with cC:
    st.markdown("**Planilha Leads — diária**")
    up_leads = st.file_uploader("Enviar arquivo (.xlsx)", type=["xlsx"], key="leads_daily")

# Processa uploads diários
df_day_open = None
if up_open is not None:
    df = load_excel(up_open.getvalue())
    m = daily_open_metrics(df, ref_date)
    upsert_daily_open(m)
    df_day_open = m["df_day"]

if up_leads is not None:
    df = load_excel(up_leads.getvalue())
    m = daily_leads_metrics(df, ref_date)
    upsert_daily_leads(m)

st.divider()

# ------------------------------------------------------------
# Resumo executivo do dia
# ------------------------------------------------------------
st.markdown("## Resumo executivo (dia)")

open_hist = load_daily_open()
leads_hist = load_daily_leads()

# pega o dia selecionado se existir na base
sel_iso = ref_date.isoformat()
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
# Conversão por mês + tabela diária do mês
# ------------------------------------------------------------
st.markdown("## Conversão (Abertas ÷ Cadastradas)")

df_conv = conversion_table()
# lista meses disponíveis
if not df_conv.empty:
    df_conv["month_ref"] = df_conv["ref_date"].apply(lambda d: fmt_month(dt.date(d.year, d.month, 1)))
    months_available = sorted(df_conv["month_ref"].unique().tolist(), key=lambda x: dt.datetime.strptime("01/" + x, "%d/%m/%Y"))
else:
    months_available = []

if months_available:
    default_month = months_available[-1]
    month_sel = st.selectbox("Selecione o mês", options=months_available[::-1], index=0)
    ms = month_summary(df_conv, month_sel)

    # cards do mês
    m1, m2, m3 = st.columns(3)
    m1.metric("Abertas no mês", fmt_int(ms["opened"]))
    m2.metric("Cadastradas no mês", fmt_int(ms["leads"]))
    m3.metric("Conversão do mês", f"{ms['rate']*100:.1f}%".replace(".", ","))

    # tabela do mês selecionado
    aux = df_conv.copy()
    aux = aux[aux["month_ref"] == month_sel].copy()
    aux = aux.sort_values("ref_date", ascending=False)

    # Estilização (sem quebrar se faltarem colunas)
    def row_style(row):
        v = float(row.get("percent_num", 0.0))
        if v >= CONV_TARGET:
            return ["background-color: rgba(33,150,243,0.10); color: #0b5394; font-weight: 600;" for _ in row]
        return ["background-color: rgba(244,67,54,0.08); color: #a61c00; font-weight: 600;" for _ in row]

    show = aux.drop(columns=["percent_num", "ref_date", "month_ref"], errors="ignore").copy()
    # indicador como HTML (renderiza como texto; vamos trocar por texto simples na tabela e deixar cor na linha)
    show["Indicador"] = show["Indicador"].str.replace(r"<[^>]+>", "", regex=True)

    st.dataframe(
        show.style.apply(row_style, axis=1),
        use_container_width=True,
        hide_index=True
    )
else:
    st.info("Importe pelo menos 1 dia de Visão Cliente e 1 dia de Leads para formar a conversão.")

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
# REMUNERAÇÃO (mensal) — incremental por CNPJ
# ------------------------------------------------------------
st.markdown("## Remuneração (mensal) — incremental por CNPJ")

st.caption("Envie 1 ou mais arquivos mensais (Nov/25 em diante). Você pode reenviar um mês para corrigir e o sistema recalcula.")

monthly_files = st.file_uploader(
    "Importar planilhas mensais (.xlsx)",
    type=["xlsx"],
    accept_multiple_files=True,
    key="monthly_files"
)

if monthly_files:
    for f in monthly_files:
        dfm = load_excel(f.getvalue())
        detected = detect_month_from_monthly_file(dfm)

        colL, colR = st.columns([1.3, 2.7], vertical_alignment="center")
        with colL:
            st.markdown(f"**Arquivo:** {f.name}")
            if detected:
                st.success(f"Mês detectado: {detected}")
                month_ref = detected
            else:
                month_ref = st.text_input(f"Mês (mm/aaaa) — {f.name}", value="01/2026")

        with colR:
            try:
                clients, summ = compute_monthly_remuneration(dfm, month_ref)
                upsert_month(month_ref, clients, summ)
                st.info(f"Qualificadas no mês: {fmt_int(summ['qualified_count'])} | Multiplicador: {summ['multiplier']:.2f} | Receita total (cheia): {fmt_money(summ['full_total'])}")
            except Exception as e:
                st.error(f"Falha ao processar {f.name}. Verifique se as colunas principais existem. Erro: {e}")

# mostra resumo de todos os meses
msum = monthly_summary_table()
if not msum.empty:
    st.markdown("### Resumo por mês (deveria receber × já pago × a receber)")
    st.dataframe(msum, use_container_width=True, hide_index=True)

    months = msum["Mês"].tolist()
    month_pick = st.selectbox("Detalhar mês", options=months, index=0)

    det = monthly_clients_detail(month_pick)
    st.markdown(f"### Detalhe por CNPJ — {month_pick}")
    st.dataframe(det, use_container_width=True, hide_index=True)
else:
    st.info("Importe planilhas mensais (ex.: NOVEMBRO2025.xlsx, DEZEMBRO2025.xlsx) para gerar a remuneração incremental.")
