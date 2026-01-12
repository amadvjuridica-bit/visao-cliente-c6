def apply_theme():
    st.markdown(
        """
        <style>
            /* =========================================================
               TEMA AJUSTADO PARA LEGIBILIDADE NO CELULAR
               - Não mexe em layout/estrutura, só cores/contraste
               - Funciona melhor em desktop e mobile, light/dark do sistema
               ========================================================= */

            :root{
                --am-bg: #ffffff;
                --am-text: #0b1220;
                --am-muted: #516079;

                --am-sidebar-bg: #0f1b3a;
                --am-sidebar-text: #ffffff;

                --am-card-bg: #ffffff;
                --am-card-border: #d7e2f2;

                /* Cores "suaves" (background) + texto escuro (mobile-friendly) */
                --am-ok-bg: #e9f2ff;     /* azul bem claro */
                --am-ok-text: #0b1220;   /* texto escuro */
                --am-bad-bg: #ffeceb;    /* vermelho bem claro */
                --am-bad-text: #0b1220;  /* texto escuro */
            }

            /* Sidebar */
            section[data-testid="stSidebar"]{
                background: var(--am-sidebar-bg) !important;
            }
            section[data-testid="stSidebar"] *{
                color: var(--am-sidebar-text) !important;
            }

            /* Títulos e textos */
            h1,h2,h3{
                color: var(--am-text) !important;
            }
            p, li, span, div{
                color: var(--am-text);
            }

            /* Cards (metric) com contraste mais forte */
            div[data-testid="stMetric"]{
                background: var(--am-card-bg) !important;
                border: 1px solid var(--am-card-border) !important;
                border-radius: 14px;
                padding: 12px 14px;
                box-shadow: 0 2px 10px rgba(15,27,58,0.05);
            }
            div[data-testid="stMetric"] *{
                color: var(--am-text) !important;
            }

            /* Badges (mantém cor no fundo, texto sempre escuro pra ficar legível no celular) */
            .am-badge-ok{
                display:inline-block;
                padding: 6px 12px;
                border-radius: 999px;
                background: var(--am-ok-bg) !important;
                color: var(--am-ok-text) !important;
                font-weight: 900;
                font-size: 12px;
                border: 1px solid #b8d6ff;
            }
            .am-badge-bad{
                display:inline-block;
                padding: 6px 12px;
                border-radius: 999px;
                background: var(--am-bad-bg) !important;
                color: var(--am-bad-text) !important;
                font-weight: 900;
                font-size: 12px;
                border: 1px solid #ffc2bf;
            }

            /* Dataframes: aumenta contraste e evita texto “apagado” no mobile */
            .stDataFrame, div[data-testid="stDataFrame"]{
                background: #ffffff !important;
            }
            div[data-testid="stDataFrame"] *{
                color: var(--am-text) !important;
            }

            /* Se o Streamlit aplicar tema escuro, ainda força legibilidade */
            @media (prefers-color-scheme: dark){
                :root{
                    --am-bg: #0b1220;
                    --am-text: #f2f5fb;
                    --am-muted: #c0c9d8;
                    --am-card-bg: #111a2e;
                    --am-card-border: #243150;
                    --am-ok-bg: rgba(0, 122, 255, 0.18);
                    --am-ok-text: #f2f5fb;
                    --am-bad-bg: rgba(255, 59, 48, 0.18);
                    --am-bad-text: #f2f5fb;
                }

                body, .main{
                    background: var(--am-bg) !important;
                }

                h1,h2,h3, p, li, span, div{
                    color: var(--am-text) !important;
                }

                div[data-testid="stMetric"]{
                    background: var(--am-card-bg) !important;
                    border: 1px solid var(--am-card-border) !important;
                }
                div[data-testid="stMetric"] *{
                    color: var(--am-text) !important;
                }

                .stDataFrame, div[data-testid="stDataFrame"]{
                    background: var(--am-card-bg) !important;
                    border: 1px solid var(--am-card-border) !important;
                }
                div[data-testid="stDataFrame"] *{
                    color: var(--am-text) !important;
                }

                .am-badge-ok{
                    border: 1px solid rgba(0,122,255,0.35) !important;
                }
                .am-badge-bad{
                    border: 1px solid rgba(255,59,48,0.35) !important;
                }
            }
        </style>
        """,
        unsafe_allow_html=True,
    )
import os
import io
import json
import re
import datetime as dt
from typing import Dict, Tuple, Optional, List

import pandas as pd
import streamlit as st

# =========================================================
# CONFIGURAÇÕES (COLUNAS)
# =========================================================
# C6 (Visão Cliente)
COL_CNPJ = "CD_CPF_CNPJ_CLIENTE"
COL_ABERTURA = "DT_CONTA_CRIADA"
COL_FUNDACAO = "DT_FUNDACAO_EMPRESA"
COL_PIX = "CHAVES_PIX_FORTE"
COL_SALDO = "VL_SALDO_MEDIO_MENSALIZADO"
COL_STATUS = "STATUS_CC"
COL_DOMICILIO = "BANCO_DOMICILIO"
COL_BY = "FL_QUALIFICADO_COMISS"
COL_BR = "MES_REF_COMISS"  # M0/M1/M2
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"

# Leads (cadastros) – coluna M (13ª col) como fallback
COL_LEADS_DATA = "DATA_CADASTRO"

# Possíveis colunas para detectar o "mês do relatório" (mês do arquivo)
POSSIVEIS_COL_DATA_BASE = [
    "DATA_BASE", "DT_BASE", "DATA_REFERENCIA", "DT_REFERENCIA",
    "DATA_RELATORIO", "DT_RELATORIO", "DATA_ATUALIZACAO", "DT_ATUALIZACAO"
]

# Conversão
ALVO_CONVERSAO = 0.20

# A partir de Jan/26 salvar histórico diário
HIST_START = dt.date(2026, 1, 1)

# =========================================================
# REMUNERAÇÃO (FAIXAS)
# =========================================================
FAIXAS = [
    (0,   "Até 49 (1.0)",   {1: 140.00, 2: 230.00, 3: 400.00, 4: 540.00}),
    (50,  "50+ (1.1)",      {1: 154.00, 2: 253.00, 3: 440.00, 4: 594.00}),
    (150, "150+ (1.25)",    {1: 175.00, 2: 287.50, 3: 500.00, 4: 675.00}),
    (350, "350+ (1.5)",     {1: 210.00, 2: 345.00, 3: 600.00, 4: 810.00}),
]

# =========================================================
# MEMÓRIA / STORAGE
# =========================================================
DATA_DIR = "data_store"
os.makedirs(DATA_DIR, exist_ok=True)

HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_aberturas_diario.json")        # dd/mm/aaaa -> aberturas
HIST_LEADS_DAILY = os.path.join(DATA_DIR, "hist_cadastros_diario.json")       # dd/mm/aaaa -> cadastradas
HIST_MONTH_LEVELS = os.path.join(DATA_DIR, "hist_mes_cnpj_nivel.json")        # mm/aaaa -> {cnpj: nivel_max_no_mes}
HIST_PAGO_POR_CNPJ = os.path.join(DATA_DIR, "pago_max_por_cnpj.json")         # cnpj -> max pago acumulado
HIST_RESUMO_MENSAL = os.path.join(DATA_DIR, "resumo_mensal.json")             # mm/aaaa -> resumo calculado
HIST_SNAPSHOT_MENSAL = os.path.join(DATA_DIR, "snapshot_mensal.json")         # mm/aaaa -> estado (saldo/pix/domicilio/qualificadas)


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


def fmt_date(d) -> str:
    if d is None or pd.isna(d):
        return ""
    if isinstance(d, pd.Timestamp):
        d = d.date()
    if isinstance(d, dt.datetime):
        d = d.date()
    if not isinstance(d, dt.date):
        return ""
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


def hide_index_df(df: pd.DataFrame) -> pd.DataFrame:
    return df.reset_index(drop=True)


# =========================================================
# DETECÇÃO DO MÊS DO RELATÓRIO (mês do arquivo)
# =========================================================
def detect_report_month_from_df(df: pd.DataFrame) -> Optional[dt.date]:
    """
    Detecta o mês do relatório (mês do arquivo), NÃO o mês de abertura.
    Prioridade:
      1) Colunas tipo DATA_BASE/DT_BASE etc
      2) Fallback: maior data existente em DT_CONTA_CRIADA
    """
    for c in POSSIVEIS_COL_DATA_BASE:
        if c in df.columns:
            d = to_date_series(df[c]).dropna()
            if len(d) > 0:
                m = pd.Series([dt.date(x.year, x.month, 1) for x in d]).mode()
                if len(m) > 0:
                    return m.iloc[0]

    if COL_ABERTURA in df.columns:
        d = to_date_series(df[COL_ABERTURA]).dropna()
        if len(d) > 0:
            mx = max(d)
            return dt.date(mx.year, mx.month, 1)

    return None


# =========================================================
# QUALIFICAÇÃO (NÍVEL)
# =========================================================
def parse_level_from_criterios(txt: str) -> int:
    """
    Ex.: "CASH IN: 3 | DOMICILIO: 0 | SALDO MEDIO: 4 | ..."
    Regra: considerar SOMENTE o maior valor (1..4).
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
    Regra robusta:
    - BY pode vir 0/1/2/3/4 ou texto. Se for número 1..4, considera como nível.
    - CRITERIOS_ATINGIDOS_COMISS também tem níveis por critério -> pega o maior.
    - nível final = max(nível_BY, nível_CRIT)
    """
    by_raw = df.get(COL_BY, pd.Series([None] * len(df)))
    by_num = pd.to_numeric(by_raw, errors="coerce").fillna(0).astype(int)
    level_by = by_num.where(by_num.between(1, 4), 0)

    crit_raw = df.get(COL_CRIT, pd.Series([""] * len(df))).astype("string").fillna("")
    level_crit = crit_raw.apply(parse_level_from_criterios).astype(int)

    lvl = pd.concat([level_by, level_crit], axis=1).max(axis=1).astype(int)
    return lvl.where(lvl.between(1, 4), 0)


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
# HISTÓRICO DIÁRIO (SALVA SOMENTE O QUE EXISTE NO ARQUIVO)
# =========================================================
def daily_upsert_many(path: str, counts: Dict[str, int]):
    """
    counts: {"dd/mm/aaaa": qty}
    Salva/atualiza SEM criar datas.
    """
    base = safe_json_load(path, default={})
    for k, v in counts.items():
        base[k] = int(v)
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
# MENSAL POR CNPJ (NÍVEL MÁXIMO NO MÊS) - A PARTIR DO DIÁRIO
# =========================================================
def month_levels_upsert_from_daily_df(df_c6: pd.DataFrame):
    """
    Grava qualificação por MÊS DO RELATÓRIO (mês do arquivo),
    não por DT_CONTA_CRIADA.

    Regra:
    - Para o mês do arquivo, para cada CNPJ, salva o MAIOR nível visto no mês.
    - Só a partir de Jan/26 em diante.
    """
    store = safe_json_load(HIST_MONTH_LEVELS, default={})

    mes_rel = detect_report_month_from_df(df_c6)
    if mes_rel is None:
        return

    if mes_rel < dt.date(2026, 1, 1):
        return

    mkey = fmt_month(mes_rel)

    df = df_c6.copy()

    # CNPJ
    if COL_CNPJ not in df.columns:
        cand = [c for c in df.columns if "CNPJ" in str(c).upper()]
        df[COL_CNPJ] = df[cand[0]] if cand else ""

    df["_cnpj"] = normalize_str(df[COL_CNPJ]).str.replace(r"\D", "", regex=True)

    # nível por linha
    df["_nivel"] = parse_level(df)

    q = df[(df["_cnpj"] != "") & (df["_nivel"] >= 1)].copy()
    if q.empty:
        store[mkey] = store.get(mkey, {}) or {}
        safe_json_save(HIST_MONTH_LEVELS, store)
        return

    by_cnpj = q.groupby("_cnpj")["_nivel"].max().reset_index()

    month_map: Dict[str, int] = store.get(mkey, {}) or {}
    for _, r in by_cnpj.iterrows():
        cnpj = str(r["_cnpj"])
        lvl = int(r["_nivel"])
        prev = int(month_map.get(cnpj, 0))
        if lvl > prev:
            month_map[cnpj] = lvl

    store[mkey] = month_map
    safe_json_save(HIST_MONTH_LEVELS, store)


# =========================================================
# IMPORTAÇÃO MENSAL (EXCEÇÃO NOV/25 e DEZ/25) - SEED
# =========================================================
def detect_month_from_filename(name: str) -> Optional[dt.date]:
    n = name.upper()
    if "NOVEMBRO2025" in n or "NOV/2025" in n or "NOV_2025" in n or "NOV-2025" in n:
        return dt.date(2025, 11, 1)
    if "DEZEMBRO2025" in n or "DEZ/2025" in n or "DEZ_2025" in n or "DEZ-2025" in n:
        return dt.date(2025, 12, 1)
    return None


def month_levels_upsert_from_monthly_file(file_name: str, file_bytes: bytes):
    df = read_excel_any(file_bytes)

    m = None
    if COL_ABERTURA in df.columns:
        d = to_date_series(df[COL_ABERTURA]).dropna()
        if len(d) > 0:
            mm = pd.Series([dt.date(x.year, x.month, 1) for x in d]).mode()
            if len(mm) > 0:
                m = mm.iloc[0]
    if m is None:
        m = detect_month_from_filename(file_name)
    if m is None:
        return

    store = safe_json_load(HIST_MONTH_LEVELS, default={})
    mkey = fmt_month(m)
    month_map: Dict[str, int] = store.get(mkey, {}) or {}

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
# FAIXA
# =========================================================
def faixa_por_qtd(qtd_qualificadas: int) -> Tuple[str, Dict[int, float]]:
    chosen_name, chosen_tbl = FAIXAS[0][1], FAIXAS[0][2]
    for min_q, nm, tbl in FAIXAS:
        if qtd_qualificadas >= min_q:
            chosen_name, chosen_tbl = nm, tbl
    return chosen_name, chosen_tbl


def faixa_tbl_por_nome(nome: str) -> Dict[int, float]:
    for _, nm, tbl in FAIXAS:
        if nm == nome:
            return tbl
    return FAIXAS[0][2]


# =========================================================
# RECOMPUTE INCREMENTAL (SEM CRIAR MESES)
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

        lvl_counts = {1: 0, 2: 0, 3: 0, 4: 0}
        for _, lvl in cmap.items():
            if lvl in lvl_counts:
                lvl_counts[lvl] += 1

        total_cheio = 0.0
        total_receber = 0.0

        for cnpj, lvl in cmap.items():
            cheio = float(precos.get(int(lvl), 0.0))
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
            "n1": lvl_counts[1],
            "n2": lvl_counts[2],
            "n3": lvl_counts[3],
            "n4": lvl_counts[4],
            "deveria_receber": total_cheio,
            "ja_pago_ref": ja_pago_ref,
            "receber_mes": total_receber,
        }

        rows.append([
            mkey, faixa_nome, qtd_qual,
            lvl_counts[1], lvl_counts[2], lvl_counts[3], lvl_counts[4],
            total_cheio, ja_pago_ref, total_receber
        ])

    safe_json_save(HIST_PAGO_POR_CNPJ, paid_max)
    safe_json_save(HIST_RESUMO_MENSAL, resumo)

    return pd.DataFrame(
        rows,
        columns=[
            "Mês", "Faixa", "Qualificadas",
            "Nível 1", "Nível 2", "Nível 3", "Nível 4",
            "Deveria receber (cheio)", "Já pago (referência)", "A receber no mês"
        ],
    )


# =========================================================
# LOGIN / TEMA / HEADER
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
                font-weight:900; font-size:12px;
            }
            .am-badge-bad{
                display:inline-block; padding:4px 10px; border-radius:999px;
                background:rgba(255,59,48,0.12); color:#FF3B30;
                font-weight:900; font-size:12px;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def show_logo_and_title():
    here = os.path.dirname(__file__)
    logo_path = os.path.join(here, "LOGO CORRETA.png")

    c1, c2 = st.columns([1, 6], vertical_alignment="center")
    with c1:
        if os.path.exists(logo_path):
            st.image(logo_path, width=160)
        else:
            st.warning("Logo não encontrada. Coloque 'LOGO CORRETA.png' na mesma pasta do app.py.")
    with c2:
        st.markdown(
            """
            <div style="line-height:1.1">
              <div style="font-size:28px;font-weight:900;color:#0f1b3a;margin-bottom:4px;">
                Painel de controle Assis e Mollerke parceiro Banco C6
              </div>
              <div style="color:#5b6b8c;font-weight:700;">
                Visão Cliente + Leads + Remuneração
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def reset_all_data():
    for p in [
        HIST_OPEN_DAILY, HIST_LEADS_DAILY, HIST_MONTH_LEVELS,
        HIST_PAGO_POR_CNPJ, HIST_RESUMO_MENSAL, HIST_SNAPSHOT_MENSAL
    ]:
        if os.path.exists(p):
            os.remove(p)


# =========================================================
# APP
# =========================================================
st.set_page_config(page_title="Assis e Mollerke | Banco C6", layout="wide")
apply_theme()

if not login_gate():
    st.stop()

# Sidebar reset
st.sidebar.markdown("---")
if st.sidebar.button("RESETAR HISTÓRICO (ZERAR TUDO)"):
    reset_all_data()
    st.sidebar.success("Histórico resetado. Reimporte Nov/25 e Dez/25 (se quiser) e depois os diários.")

show_logo_and_title()
st.divider()

# =========================================================
# IMPORTAÇÃO
# =========================================================
st.subheader("Importação diária (Janeiro/26 em diante)")

colA, colB = st.columns(2)
with colA:
    up_c6 = st.file_uploader("Planilha C6 (Visão Cliente) — diária (.xlsx)", type=["xlsx"], key="c6")
with colB:
    up_leads = st.file_uploader("Planilha Leads — diária (.xlsx)", type=["xlsx"], key="leads")

st.subheader("Importação mensal (exceção: Nov/25 e Dez/25)")
up_monthly = st.file_uploader(
    "Envie Nov/25 e Dez/25 (apenas se precisar iniciar histórico antigo)",
    type=["xlsx"],
    accept_multiple_files=True,
    key="monthly",
)

# Processa mensal (seed)
if up_monthly and len(up_monthly) > 0:
    for f in up_monthly:
        month_levels_upsert_from_monthly_file(f.name, f.getvalue())

# =========================================================
# PROCESSA DIÁRIO (SEM DATA REFERÊNCIA)
# =========================================================
df_c6 = None
df_leads = None

if up_c6:
    df_c6 = read_excel_any(up_c6.getvalue())

    # garantias
    if COL_ABERTURA not in df_c6.columns:
        df_c6[COL_ABERTURA] = pd.NA
    if COL_FUNDACAO not in df_c6.columns:
        df_c6[COL_FUNDACAO] = pd.NA
    if COL_SALDO not in df_c6.columns:
        df_c6[COL_SALDO] = 0.0
    if COL_BR not in df_c6.columns:
        df_c6[COL_BR] = ""
    if COL_CRIT not in df_c6.columns:
        df_c6[COL_CRIT] = ""
    if COL_BY not in df_c6.columns:
        df_c6[COL_BY] = ""

    df_c6[COL_ABERTURA] = to_date_series(df_c6[COL_ABERTURA])
    df_c6[COL_FUNDACAO] = to_date_series(df_c6[COL_FUNDACAO])
    df_c6[COL_SALDO] = pd.to_numeric(df_c6[COL_SALDO], errors="coerce").fillna(0.0)
    df_c6[COL_BR] = normalize_str(df_c6[COL_BR]).str.upper()
    df_c6[COL_CRIT] = normalize_str(df_c6[COL_CRIT])

    # histórico diário de aberturas (apenas datas existentes no arquivo e >= Jan/26)
    opened_counts = (
        df_c6[df_c6[COL_ABERTURA].notna()]
        .assign(_d=df_c6[COL_ABERTURA])
        .query("_d >= @HIST_START")
        .groupby("_d")
        .size()
        .to_dict()
    )
    opened_counts = {fmt_date(k): int(v) for k, v in opened_counts.items()}
    if opened_counts:
        daily_upsert_many(HIST_OPEN_DAILY, opened_counts)

    # grava níveis por mês do relatório (mês do arquivo)
    month_levels_upsert_from_daily_df(df_c6)

    # snapshot mensal (estado do arquivo para cards do mês)
    mes_rel = detect_report_month_from_df(df_c6)
    if mes_rel and mes_rel >= dt.date(2026, 1, 1):
        mkey = fmt_month(mes_rel)

        df_tmp = df_c6.copy()
        df_tmp["_nivel"] = parse_level(df_tmp)

        pix_com, pix_sem, _ = pix_summary(df_tmp)
        domicilio_c6 = int(df_tmp.get(COL_DOMICILIO, pd.Series([""] * len(df_tmp))).apply(contains_c6).sum())
        qualificadas = int((df_tmp["_nivel"] >= 1).sum())
        saldo_total = float(df_tmp[COL_SALDO].sum())

        snap = safe_json_load(HIST_SNAPSHOT_MENSAL, default={})
        snap[mkey] = {
            "saldo_total": saldo_total,
            "pix_com": pix_com,
            "pix_sem": pix_sem,
            "domicilio_c6": domicilio_c6,
            "qualificadas_arquivo": qualificadas,
            "arquivo_c6": up_c6.name if up_c6 else "",
        }
        safe_json_save(HIST_SNAPSHOT_MENSAL, snap)

if up_leads:
    df_leads = read_excel_any(up_leads.getvalue())

    # mapear DATA_CADASTRO
    if COL_LEADS_DATA not in df_leads.columns:
        cand = [c for c in df_leads.columns if "CADAST" in str(c).upper() and "DATA" in str(c).upper()]
        if cand:
            df_leads[COL_LEADS_DATA] = df_leads[cand[0]]
        else:
            # fallback coluna M (13ª)
            if len(df_leads.columns) >= 13:
                df_leads[COL_LEADS_DATA] = df_leads.iloc[:, 12]
            else:
                df_leads[COL_LEADS_DATA] = pd.NA

    df_leads[COL_LEADS_DATA] = to_date_series(df_leads[COL_LEADS_DATA])

    leads_counts = (
        df_leads[df_leads[COL_LEADS_DATA].notna()]
        .assign(_d=df_leads[COL_LEADS_DATA])
        .query("_d >= @HIST_START")
        .groupby("_d")
        .size()
        .to_dict()
    )
    leads_counts = {fmt_date(k): int(v) for k, v in leads_counts.items()}
    if leads_counts:
        daily_upsert_many(HIST_LEADS_DAILY, leads_counts)

st.divider()

# =========================================================
# RECOMPUTE REMUNERAÇÃO (INCREMENTAL CONSOLIDADA)
# =========================================================
_ = recompute_incremental()
saved_resumo = safe_json_load(HIST_RESUMO_MENSAL, default={})

# =========================================================
# RESUMO EXECUTIVO (MÊS) + % GERAL DO MÊS
# =========================================================
st.subheader("Resumo executivo (mês)")

hist_open = hist_to_df(HIST_OPEN_DAILY, "Abertas")
hist_leads = hist_to_df(HIST_LEADS_DAILY, "Cadastradas")

if hist_open.empty or hist_leads.empty:
    st.info("Importe C6 + Leads (diário) para montar o mês.")
else:
    base = pd.merge(hist_leads, hist_open, on="Data", how="outer").fillna(0)
    base["Abertas"] = base["Abertas"].astype(int)
    base["Cadastradas"] = base["Cadastradas"].astype(int)
    base["Mes_ref"] = base["Data"].map(month_first)

    meses = sorted(base["Mes_ref"].unique())
    mes_atual = meses[-1]
    mes_lbl = fmt_month(mes_atual)

    mes_df = base[base["Mes_ref"] == mes_atual].copy()
    total_ab_mes = int(mes_df["Abertas"].sum())
    total_cad_mes = int(mes_df["Cadastradas"].sum())
    perc_mes = (total_ab_mes / total_cad_mes) if total_cad_mes > 0 else 0.0

    badge = "am-badge-ok" if perc_mes >= ALVO_CONVERSAO else "am-badge-bad"
    st.markdown(
        f"<div class='{badge}'>Conversão do mês: {str(round(perc_mes*100,1)).replace('.',',')}%</div>",
        unsafe_allow_html=True
    )

    snap = safe_json_load(HIST_SNAPSHOT_MENSAL, default={})
    s = snap.get(mes_lbl, {})

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Mês", mes_lbl)
    c2.metric("Cadastradas (mês)", br_int(total_cad_mes))
    c3.metric("Abertas (mês)", br_int(total_ab_mes))
    c4.metric("% geral (mês)", f"{str(round(perc_mes*100,1)).replace('.',',')}%")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Saldo total (snapshot)", br_money(float(s.get("saldo_total", 0.0))))
    c6.metric("Pix (snapshot)", f'{br_int(int(s.get("pix_com",0)))} com | {br_int(int(s.get("pix_sem",0)))} sem')
    c7.metric("Domicílio C6 (snapshot)", br_int(int(s.get("domicilio_c6", 0))))
    c8.metric("Qualificadas (arquivo)", br_int(int(s.get("qualificadas_arquivo", 0))))

st.divider()

# =========================================================
# REMUNERAÇÃO DO MÊS ATUAL (NO TOPO)
# =========================================================
st.subheader("Remuneração do mês atual (incremental)")

if saved_resumo:
    months_sorted = sorted(saved_resumo.keys(), key=month_key_str)
    mes_atual = months_sorted[-1]
    info = saved_resumo.get(mes_atual, {})

    faixa = info.get("faixa", "-")
    qual = int(info.get("qualificadas", 0))
    n1 = int(info.get("n1", 0))
    n2 = int(info.get("n2", 0))
    n3 = int(info.get("n3", 0))
    n4 = int(info.get("n4", 0))
    cheio = float(info.get("deveria_receber", 0.0))
    japago = float(info.get("ja_pago_ref", 0.0))
    receber = float(info.get("receber_mes", 0.0))

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Mês", mes_atual)
    m2.metric("Faixa", faixa)
    m3.metric("Qualificadas", br_int(qual))
    m4.metric("A receber (mês)", br_money(receber))

    m5, m6, m7 = st.columns(3)
    m5.metric("Receita cheia (mês)", br_money(cheio))
    m6.metric("Já pago (referência)", br_money(japago))
    m7.metric("Níveis (1/2/3/4)", f"{br_int(n1)} / {br_int(n2)} / {br_int(n3)} / {br_int(n4)}")

else:
    st.info("Ainda não há histórico de remuneração. Importe os diários (Jan/26 em diante) e/ou Nov/25 e Dez/25.")

st.divider()

# =========================================================
# CONVERSÃO DO MÊS (TABELA DIÁRIA + % GERAL DO MÊS)
# =========================================================
st.subheader("Conversão do mês (detalhamento diário)")

if hist_open.empty or hist_leads.empty:
    st.info("Para ver a conversão, envie planilhas diárias de C6 e Leads (Jan/26 em diante).")
else:
    base = pd.merge(hist_leads, hist_open, on="Data", how="outer").fillna(0)
    base["Cadastradas"] = base["Cadastradas"].astype(int)
    base["Abertas"] = base["Abertas"].astype(int)
    base["Mes_ref"] = base["Data"].map(month_first)

    meses = sorted(base["Mes_ref"].unique())
    meses_lbl = [fmt_month(m) for m in meses]

    mes_sel_lbl = st.selectbox("Selecione o mês", meses_lbl, index=len(meses_lbl) - 1)
    mes_sel = meses[meses_lbl.index(mes_sel_lbl)]

    mes_df = base[base["Mes_ref"] == mes_sel].copy()
    mes_df["Percentual_num"] = mes_df.apply(
        lambda r: (r["Abertas"] / r["Cadastradas"]) if r["Cadastradas"] > 0 else 0.0,
        axis=1
    )
    mes_df["% Conversão"] = mes_df["Percentual_num"].map(lambda x: f"{x*100:.1f}%".replace(".", ","))
    mes_df["Indicador"] = mes_df["Percentual_num"].map(lambda x: "Dentro do alvo" if x >= ALVO_CONVERSAO else "Abaixo do alvo")

    # mais recente -> mais antigo
    mes_df = mes_df.sort_values("Data", ascending=False).reset_index(drop=True)

    total_ab_mes = int(mes_df["Abertas"].sum())
    total_cad_mes = int(mes_df["Cadastradas"].sum())
    perc_mes = (total_ab_mes / total_cad_mes) if total_cad_mes > 0 else 0.0

    badge = "am-badge-ok" if perc_mes >= ALVO_CONVERSAO else "am-badge-bad"
    st.markdown(
        f"<div class='{badge}'>% geral do mês: {str(round(perc_mes*100,1)).replace('.',',')}%</div>",
        unsafe_allow_html=True
    )

    cA, cB, cC = st.columns(3)
    cA.metric("Cadastradas (mês)", br_int(total_cad_mes))
    cB.metric("Abertas (mês)", br_int(total_ab_mes))
    cC.metric("% geral (mês)", f"{str(round(perc_mes*100,1)).replace('.',',')}%")

    display = mes_df[["Data", "Cadastradas", "Abertas", "% Conversão", "Indicador"]].copy()
    display["Data"] = display["Data"].apply(fmt_date)
    display["Cadastradas"] = display["Cadastradas"].apply(br_int)
    display["Abertas"] = display["Abertas"].apply(br_int)

    def highlight_row(row):
        # usa o Percentual_num da mes_df (mesma ordem da display)
        v = float(mes_df.loc[row.name, "Percentual_num"])
        if v >= ALVO_CONVERSAO:
            return ["background-color: rgba(0,122,255,0.10); font-weight: 800;"] * len(row)
        return ["background-color: rgba(255,59,48,0.10); font-weight: 800;"] * len(row)

    st.dataframe(display.style.apply(highlight_row, axis=1), use_container_width=True, hide_index=True)

st.divider()

# =========================================================
# RELATÓRIOS (DIÁRIO)
# =========================================================
st.subheader("Relatórios (diário)")

if df_c6 is None:
    st.info("Envie a planilha diária do C6 para liberar os relatórios.")
else:
    tabs = st.tabs(["Aberturas", "Fundações (por dia)", "Pix + Status", "Qualificação + BR + Valores"])

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

    # Fundações
    with tabs[1]:
        st.markdown("#### Fundação (mês/ano) dentro do dia de abertura")
        temp = df_c6[[COL_ABERTURA, COL_FUNDACAO]].dropna().copy()
        if temp.empty:
            st.info("Sem dados de fundação no arquivo.")
        else:
            temp["Dia"] = temp[COL_ABERTURA]
            temp["Mês fundação"] = temp[COL_FUNDACAO].apply(
                lambda d: f"{d.month:02d}/{d.year}" if isinstance(d, dt.date) else ""
            )

            pivot = (
                temp.groupby(["Dia", "Mês fundação"])
                .size()
                .reset_index(name="Quantidade")
                .sort_values(["Dia", "Mês fundação"])
            )

            dias = sorted(temp[COL_ABERTURA].unique())
            dias_lbl = [fmt_date(d) for d in dias]
            dia_sel_lbl = st.selectbox("Selecione o dia de abertura", dias_lbl, index=len(dias_lbl) - 1)
            dia_sel = dias[dias_lbl.index(dia_sel_lbl)]

            dia_df = pivot[pivot["Dia"] == dia_sel].copy()
            total_dia = int(dia_df["Quantidade"].sum())

            st.markdown(f"**No dia {dia_sel_lbl} foram abertas {br_int(total_dia)} empresas.**")
            dia_df_show = dia_df[["Mês fundação", "Quantidade"]].copy()
            st.dataframe(dia_df_show, use_container_width=True, hide_index=True)
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
            normalize_str(df_c6.get(COL_STATUS, pd.Series([""] * len(df_c6))))
            .replace("", "SEM STATUS")
            .value_counts()
            .rename_axis("Status")
            .reset_index(name="Quantidade")
        )
        st.dataframe(status, use_container_width=True, hide_index=True)
        st.bar_chart(status.set_index("Status")["Quantidade"])

    # Qualificação + BR + Valores
    with tabs[3]:
        st.markdown("#### Qualificação (nível vencedor, critério vencedor e BR)")

        dfq = df_c6.copy()
        dfq["_nivel"] = parse_level(dfq)
        dfq["_qualificada"] = dfq["_nivel"].apply(lambda x: "Sim" if x >= 1 else "Não")
        dfq["_criterio_vencedor"] = normalize_str(dfq.get(COL_CRIT, pd.Series([""] * len(dfq)))).apply(criterio_vencedor)

        # BR
        brs = normalize_str(dfq.get(COL_BR, pd.Series([""] * len(dfq)))).str.upper().replace("", "SEM BR")
        br_counts = brs.value_counts().rename_axis("BR").reset_index(name="Quantidade")

        c1, c2 = st.columns([2, 3])
        with c1:
            st.markdown("**BR (M0/M1/M2)**")
            st.dataframe(br_counts, use_container_width=True, hide_index=True)
        with c2:
            total_qual = int((dfq["_nivel"] >= 1).sum())
            n1 = int((dfq["_nivel"] == 1).sum())
            n2 = int((dfq["_nivel"] == 2).sum())
            n3 = int((dfq["_nivel"] == 3).sum())
            n4 = int((dfq["_nivel"] == 4).sum())

            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric("Qualificadas (arquivo)", br_int(total_qual))
            k2.metric("Nível 1", br_int(n1))
            k3.metric("Nível 2", br_int(n2))
            k4.metric("Nível 3", br_int(n3))
            k5.metric("Nível 4", br_int(n4))

        # Valores do mês atual (se existir no histórico)
        saved = safe_json_load(HIST_RESUMO_MENSAL, default={})
        if saved:
            mes_atual = sorted(saved.keys(), key=month_key_str)[-1]
            info = saved.get(mes_atual, {})
            faixa_nome = info.get("faixa", "-")
            precos = faixa_tbl_por_nome(faixa_nome)

            n1 = int(info.get("n1", 0))
            n2 = int(info.get("n2", 0))
            n3 = int(info.get("n3", 0))
            n4 = int(info.get("n4", 0))

            rows_val = []
            for lvl, qtd in [(1, n1), (2, n2), (3, n3), (4, n4)]:
                unit = float(precos.get(lvl, 0.0))
                total = unit * float(qtd)
                rows_val.append([f"Nível {lvl}", br_int(qtd), br_money(unit), br_money(total)])

            st.markdown(f"#### Valores (mês atual: {mes_atual}) — Faixa: {faixa_nome}")
            df_vals = pd.DataFrame(rows_val, columns=["Nível", "Quantidade", "Valor unitário", "Total (cheio)"])
            st.dataframe(df_vals, use_container_width=True, hide_index=True)

            st.markdown("#### Resumo do mês (incremental)")
            r1, r2, r3 = st.columns(3)
            r1.metric("Receita cheia (mês)", br_money(float(info.get("deveria_receber", 0.0))))
            r2.metric("Já pago (referência)", br_money(float(info.get("ja_pago_ref", 0.0))))
            r3.metric("A receber (mês)", br_money(float(info.get("receber_mes", 0.0))))
        else:
            st.info("Ainda não há mês atual calculado. Importe arquivos diários (Jan/26 em diante).")

        # Lista de qualificadas (arquivo)
        st.markdown("#### Lista de qualificadas (arquivo)")
        if COL_CNPJ not in dfq.columns:
            cand = [c for c in dfq.columns if "CNPJ" in str(c).upper()]
            dfq[COL_CNPJ] = dfq[cand[0]] if cand else ""

        show = dfq[dfq["_qualificada"] == "Sim"].copy()
        show = show[[COL_CNPJ, COL_ABERTURA, "_nivel", "_criterio_vencedor", COL_BR]].rename(columns={
            COL_CNPJ: "CNPJ",
            COL_ABERTURA: "Data de abertura",
            "_nivel": "Nível",
            "_criterio_vencedor": "Critério vencedor",
            COL_BR: "BR",
        })
        show["Data de abertura"] = show["Data de abertura"].apply(fmt_date)
        show = show.sort_values("Data de abertura", ascending=False)
        st.dataframe(show, use_container_width=True, hide_index=True)

st.divider()

# =========================================================
# COMPARATIVO MENSAL (NÃO CRIA MESES)
# =========================================================
st.subheader("Comparativo mensal de remuneração")

saved = safe_json_load(HIST_RESUMO_MENSAL, default={})
if not saved:
    st.info("Sem histórico mensal ainda. Importe diários (Jan/26 em diante) e/ou Nov/25 e Dez/25.")
else:
    rows = []
    for mes, info in saved.items():
        rows.append([
            mes,
            info.get("faixa", ""),
            int(info.get("qualificadas", 0)),
            int(info.get("n1", 0)),
            int(info.get("n2", 0)),
            int(info.get("n3", 0)),
            int(info.get("n4", 0)),
            float(info.get("deveria_receber", 0.0)),
            float(info.get("ja_pago_ref", 0.0)),
            float(info.get("receber_mes", 0.0)),
        ])

    dfm = pd.DataFrame(rows, columns=[
        "Mês", "Faixa", "Qualificadas", "N1", "N2", "N3", "N4",
        "Deveria receber (cheio)", "Já pago (referência)", "A receber no mês"
    ]).sort_values("Mês", key=lambda col: col.map(month_key_str), ascending=True)

    view = dfm.copy()
    view["Qualificadas"] = view["Qualificadas"].apply(br_int)
    view["N1"] = view["N1"].apply(br_int)
    view["N2"] = view["N2"].apply(br_int)
    view["N3"] = view["N3"].apply(br_int)
    view["N4"] = view["N4"].apply(br_int)
    view["Deveria receber (cheio)"] = view["Deveria receber (cheio)"].apply(br_money)
    view["Já pago (referência)"] = view["Já pago (referência)"].apply(br_money)
    view["A receber no mês"] = view["A receber no mês"].apply(br_money)

    st.dataframe(view, use_container_width=True, hide_index=True)

    last = dfm.iloc[-1]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Último mês", str(last["Mês"]))
    c2.metric("Qualificadas", br_int(int(last["Qualificadas"])))
    c3.metric("Receita cheia", br_money(float(last["Deveria receber (cheio)"])))
    c4.metric("A receber", br_money(float(last["A receber no mês"])))
