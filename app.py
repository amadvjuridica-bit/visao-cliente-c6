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
COL_BY = "FL_QUALIFICADO_COMISS"                  # pode vir 0/1 OU 2/3/4 OU texto
COL_BR = "MES_REF_COMISS"                         # M0/M1/M2 (ou vazio)
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"           # texto "CASH IN: X | ..."

# Planilha "Leads" (cadastros) - coluna M (13ª) vira DATA_CADASTRO
COL_LEADS_DATA = "DATA_CADASTRO"                  # mapeia a coluna M para este nome

# Regras de cor do indicador de conversão
ALVO_CONVERSAO = 0.20  # 20%

# A partir de qual data você quer "memorizar" histórico diário
HIST_START = dt.date(2026, 1, 1)

# =========================================================
# REGRAS DE REMUNERAÇÃO (POR FAIXA)
# =========================================================
# Faixa definida pela QUANTIDADE TOTAL de qualificadas do mês (CNPJs únicos qualificados no mês)
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

HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_aberturas_diario.json")      # aberturas por dia (1 valor por dia)
HIST_LEADS_DAILY = os.path.join(DATA_DIR, "hist_cadastros_diario.json")     # cadastros por dia (1 valor por dia)

# memória mensal (a partir dos diários): mapa do mês -> {cnpj: nivel_max_no_mes}
HIST_MONTH_LEVELS = os.path.join(DATA_DIR, "hist_mes_cnpj_nivel.json")

# incremental (global): max pago por CNPJ até agora
HIST_PAGO_POR_CNPJ = os.path.join(DATA_DIR, "pago_max_por_cnpj.json")

# resumo mensal consolidado (mm/aaaa -> métricas)
HIST_RESUMO_MENSAL = os.path.join(DATA_DIR, "resumo_mensal.json")


# =========================================================
# HELPERS GERAIS
# =========================================================
def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def safe_json_load(path: str, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def safe_json_save(path: str, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def br_money(v: float) -> str:
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
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
# QUALIFICAÇÃO (NÍVEL VENCEDOR)
# =========================================================
def parse_level_from_criterios(txt: str) -> int:
    """
    Ex.: "CASH IN: 3 | DOMICILIO: 0 | SALDO MEDIO: 4 | SPENDING: 0 | CONTA GLOBAL: 0"
    Regra: considerar SOMENTE o MAIOR valor (vencedor).
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
    Regra robusta (conforme você orientou):
    - Se BY vier 2/3/4: usa BY como nível
    - Se BY vier 0/1/texto: NÃO define nível -> usa CRITERIOS (maior valor)
    - Qualificada = nível >= 1
    """
    by_raw = df.get(COL_BY, pd.Series([None] * len(df)))
    by_num = pd.to_numeric(by_raw, errors="coerce")

    # só considera BY como nível se for 2..4 (porque BY==1 geralmente só indica "qualificada", não o nível real)
    level_by = by_num.fillna(0).astype(int)
    level_by = level_by.where(level_by.between(2, 4), 0)

    crit_raw = df.get(COL_CRIT, pd.Series([""] * len(df))).astype("string").fillna("")
    level_crit = crit_raw.apply(parse_level_from_criterios).astype(int)

    level = level_by.where(level_by > 0, level_crit)
    level = level.fillna(0).astype(int)
    level = level.where(level.between(1, 4), 0)
    return level


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
# HISTÓRICO DIÁRIO (MEMÓRIA)
# =========================================================
def upsert_daily_hist(path: str, date_key: str, qty: int):
    """
    Guarda 1 valor por dia (dd/mm/aaaa -> quantidade)
    Sempre substitui o valor do dia pelo último enviado.
    """
    base = safe_json_load(path, default={})
    base[date_key] = int(qty)
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
# REMUNERAÇÃO (MENSAL) A PARTIR DOS DIÁRIOS
# =========================================================
def faixa_por_qtd(qtd_qualificadas: int) -> Tuple[str, Dict[int, float]]:
    chosen_name, chosen_tbl = FAIXAS[0][1], FAIXAS[0][2]
    for min_q, nm, tbl in FAIXAS:
        if qtd_qualificadas >= min_q:
            chosen_name, chosen_tbl = nm, tbl
    return chosen_name, chosen_tbl


def update_month_levels_from_daily(df_c6: pd.DataFrame, ref_date: dt.date):
    """
    Atualiza a memória mensal (mm/aaaa) usando SOMENTE os registros do dia ref_date
    (DT_CONTA_CRIADA == ref_date). Isso evita contaminar o mês com linhas de outros dias/meses.
    """
    month_key = fmt_month(dt.date(ref_date.year, ref_date.month, 1))
    store = safe_json_load(HIST_MONTH_LEVELS, default={})
    month_map: Dict[str, int] = store.get(month_key, {})

    if COL_ABERTURA not in df_c6.columns:
        df_day = df_c6.copy()
    else:
        abertura = to_date_series(df_c6[COL_ABERTURA])
        df_day = df_c6[abertura == ref_date].copy()

    if df_day.empty:
        store[month_key] = month_map
        safe_json_save(HIST_MONTH_LEVELS, store)
        return

    # garante CNPJ
    if COL_CNPJ not in df_day.columns:
        cand = [c for c in df_day.columns if "CNPJ" in str(c).upper()]
        df_day[COL_CNPJ] = df_day[cand[0]] if cand else ""

    df_day["_cnpj"] = normalize_str(df_day[COL_CNPJ]).str.replace(r"\D", "", regex=True)
    df_day["_nivel"] = parse_level(df_day)

    q = df_day[(df_day["_nivel"] >= 1) & (df_day["_cnpj"] != "")].copy()
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


def recompute_monthly_incremental_from_memory() -> pd.DataFrame:
    """
    Recalcula TODOS os meses (em HIST_MONTH_LEVELS) e atualiza:
    - HIST_PAGO_POR_CNPJ (max pago acumulado)
    - HIST_RESUMO_MENSAL (resumo por mês)
    Retorna dataframe consolidado.
    """
    month_levels = safe_json_load(HIST_MONTH_LEVELS, default={})
    months = list(month_levels.keys())

    def month_sort_key(m: str) -> int:
        try:
            mm, aa = m.split("/")
            return int(aa) * 100 + int(mm)
        except Exception:
            return 0

    months = sorted(months, key=month_sort_key)

    paid_max: Dict[str, float] = safe_json_load(HIST_PAGO_POR_CNPJ, default={})
    resumo_mensal: Dict[str, dict] = safe_json_load(HIST_RESUMO_MENSAL, default={})

    # REFAZ do zero (para não acumular erro)
    paid_max = {}

    rows = []
    for m in months:
        cmap: Dict[str, int] = month_levels.get(m, {}) or {}
        # limpa CNPJ vazio
        cmap = {k: int(v) for k, v in cmap.items() if str(k).strip() != ""}

        qtd_qual = len(cmap)
        faixa_nome, precos = faixa_por_qtd(qtd_qual)

        total_cheio = 0.0
        total_receber = 0.0

        # soma e incremental por CNPJ
        for cnpj, lvl in cmap.items():
            lvl = int(lvl)
            cheio = float(precos.get(lvl, 0.0))
            prev_max = float(paid_max.get(cnpj, 0.0))
            diff = cheio - prev_max
            if diff < 0:
                diff = 0.0
            total_cheio += cheio
            total_receber += diff
            paid_max[cnpj] = max(prev_max, cheio)

        ja_pago_ref = total_cheio - total_receber

        resumo_mensal[m] = {
            "faixa": faixa_nome,
            "qualificadas": qtd_qual,
            "deveria_receber": total_cheio,
            "ja_pago_ref": ja_pago_ref,
            "receber_mes": total_receber,
        }

        rows.append([m, faixa_nome, qtd_qual, total_cheio, ja_pago_ref, total_receber])

    safe_json_save(HIST_PAGO_POR_CNPJ, paid_max)
    safe_json_save(HIST_RESUMO_MENSAL, resumo_mensal)

    return pd.DataFrame(
        rows,
        columns=["Mês", "Faixa", "Qualificadas", "Deveria receber (cheio)", "Já pago (referência)", "A receber no mês"],
    )


# =========================================================
# ESTILO / TEMA / HEADER
# =========================================================
def apply_theme():
    st.markdown(
        """
        <style>
            section[data-testid="stSidebar"]{
                background: #0f1b3a;
            }
            section[data-testid="stSidebar"] * {
                color: #ffffff !important;
            }
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
            st.image(logo_path, width=150)
        else:
            st.warning("Logo não encontrada: coloque 'LOGO CORRETA.png' na mesma pasta do app.py.")

    with c2:
        st.markdown(
            """
            <div style="line-height:1.1">
              <div style="font-size:28px;font-weight:900;color:#0f1b3a;margin-bottom:4px;">
                Painel de controle Assis e Mollerke parceiro Banco C6
              </div>
              <div style="color:#5b6b8c;font-weight:600;">
                Visão Cliente + Leads + Remuneração (incremental)
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
# TABELA CONVERSÃO (COLORIDA SEM COLUNA TÉCNICA)
# =========================================================
def style_conversao_display(display_df: pd.DataFrame, perc_series: pd.Series) -> "pd.io.formats.style.Styler":
    """
    display_df: tabela final SEM Percentual_num
    perc_series: série com o percentual numérico alinhado pelo índice
    """
    def row_style(row):
        v = float(perc_series.loc[row.name]) if row.name in perc_series.index else 0.0
        if v >= ALVO_CONVERSAO:
            return ["background-color: rgba(0,122,255,0.10); color:#0f1b3a; font-weight:600;"] * len(row)
        return ["background-color: rgba(255,59,48,0.10); color:#0f1b3a; font-weight:600;"] * len(row)

    return display_df.style.apply(row_style, axis=1)


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
# IMPORTAÇÃO (DIÁRIO)
# =========================================================
st.subheader("Importação do dia")

colA, colB, colC = st.columns([1.2, 1.2, 1.0])
with colA:
    ref_date = st.date_input("Data de referência (obrigatório)", value=dt.date.today(), format="DD/MM/YYYY")
with colB:
    up_c6 = st.file_uploader("Planilha C6 (Visão Cliente) — diária (.xlsx)", type=["xlsx"], key="c6")
with colC:
    up_leads = st.file_uploader("Planilha Leads — diária (.xlsx)", type=["xlsx"], key="leads")

if ref_date < HIST_START:
    st.warning(f"A memória do painel considera apenas a partir de {fmt_date(HIST_START)}. Selecione uma data a partir disso.")

st.divider()

daily_ready = False
leads_ready = False
df_c6 = None
df_leads = None

# =========================================================
# PROCESSAMENTO C6 (DIÁRIO) - SEM CONTAMINAR O MÊS
# =========================================================
if up_c6:
    b = up_c6.getvalue()
    df_c6 = read_excel_any(b)

    # garante colunas essenciais
    if COL_CNPJ not in df_c6.columns:
        cand = [c for c in df_c6.columns if "CNPJ" in str(c).upper()]
        df_c6[COL_CNPJ] = df_c6[cand[0]] if cand else ""

    df_c6[COL_CNPJ] = normalize_str(df_c6[COL_CNPJ]).str.replace(r"\D", "", regex=True)

    if COL_ABERTURA not in df_c6.columns:
        df_c6[COL_ABERTURA] = pd.NA
    if COL_FUNDACAO not in df_c6.columns:
        df_c6[COL_FUNDACAO] = pd.NA

    df_c6[COL_ABERTURA] = to_date_series(df_c6[COL_ABERTURA])
    df_c6[COL_FUNDACAO] = to_date_series(df_c6[COL_FUNDACAO])

    if COL_SALDO not in df_c6.columns:
        df_c6[COL_SALDO] = 0.0
    df_c6[COL_SALDO] = pd.to_numeric(df_c6[COL_SALDO], errors="coerce").fillna(0.0)

    df_c6[COL_PIX] = normalize_str(df_c6.get(COL_PIX, pd.Series([""] * len(df_c6))))
    df_c6[COL_STATUS] = normalize_str(df_c6.get(COL_STATUS, pd.Series([""] * len(df_c6))))
    df_c6[COL_DOMICILIO] = normalize_str(df_c6.get(COL_DOMICILIO, pd.Series([""] * len(df_c6))))
    df_c6[COL_CRIT] = normalize_str(df_c6.get(COL_CRIT, pd.Series([""] * len(df_c6))))
    df_c6[COL_BY] = df_c6.get(COL_BY, pd.Series([""] * len(df_c6)))
    if COL_BR not in df_c6.columns:
        df_c6[COL_BR] = ""

    # ✅ Contas abertas NO DIA (somente DT_CONTA_CRIADA == ref_date)
    ab = df_c6[COL_ABERTURA]
    opened_day = int((ab == ref_date).sum())

    ref_date_key = fmt_date(ref_date)
    if ref_date >= HIST_START:
        upsert_daily_hist(HIST_OPEN_DAILY, ref_date_key, opened_day)
        update_month_levels_from_daily(df_c6, ref_date)

    daily_ready = True

# =========================================================
# PROCESSAMENTO LEADS (DIÁRIO) - SEM CONTAMINAR O MÊS
# =========================================================
if up_leads:
    b = up_leads.getvalue()
    df_leads = read_excel_any(b)

    # Mapeia coluna DATA_CADASTRO (ou coluna M)
    if COL_LEADS_DATA not in df_leads.columns:
        cand = [c for c in df_leads.columns if "CADAST" in str(c).upper() and "DATA" in str(c).upper()]
        if cand:
            df_leads[COL_LEADS_DATA] = df_leads[cand[0]]
        else:
            # fallback: coluna M (index 12)
            if len(df_leads.columns) >= 13:
                df_leads[COL_LEADS_DATA] = df_leads.iloc[:, 12]
            else:
                df_leads[COL_LEADS_DATA] = pd.NA

    df_leads[COL_LEADS_DATA] = to_date_series(df_leads[COL_LEADS_DATA])

    # ✅ Cadastradas NO DIA (somente == ref_date)
    ld = df_leads[COL_LEADS_DATA]
    leads_day = int((ld == ref_date).sum())

    ref_date_key = fmt_date(ref_date)
    if ref_date >= HIST_START:
        upsert_daily_hist(HIST_LEADS_DAILY, ref_date_key, leads_day)

    leads_ready = True

# =========================================================
# RECOMPUTA REMUNERAÇÃO MENSAL (a partir da memória do mês)
# =========================================================
df_monthly = recompute_monthly_incremental_from_memory()

# =========================================================
# PRIMEIRA VISTA: RESUMO DO DIA + REMUNERAÇÃO DO MÊS ATUAL
# =========================================================
if daily_ready:
    # filtros do dia para métricas do arquivo (somente do dia ref_date)
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

# ✅ Remuneração do mês atual (sempre visível)
mes_atual_key = fmt_month(dt.date(ref_date.year, ref_date.month, 1))
st.subheader("Remuneração do mês atual (incremental)")

if df_monthly.empty or mes_atual_key not in set(df_monthly["Mês"].tolist()):
    st.info("Ainda não há dados suficientes do mês atual para calcular a remuneração. Importe ao menos 1 dia do mês atual (C6) para começar.")
else:
    row = df_monthly[df_monthly["Mês"] == mes_atual_key].iloc[0]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Mês", str(row["Mês"]))
    m2.metric("Receita cheia (mês)", br_money(float(row["Deveria receber (cheio)"])))
    m3.metric("Já pago (referência)", br_money(float(row["Já pago (referência)"])))
    m4.metric("A receber (mês)", br_money(float(row["A receber no mês"])))

st.divider()

# =========================================================
# CONVERSÃO DO MÊS (APENAS DIAS IMPORTADOS) + FECHAMENTO
# =========================================================
st.subheader("Conversão do mês (Abertas ÷ Cadastradas)")

hist_open = hist_to_df(HIST_OPEN_DAILY, "Abertas")
hist_leads = hist_to_df(HIST_LEADS_DAILY, "Cadastradas")

if hist_open.empty or hist_leads.empty:
    st.info("Envie ao menos 1 dia de C6 e 1 dia de Leads para o painel montar a conversão.")
else:
    base = pd.merge(hist_leads, hist_open, on="Data", how="outer").fillna(0)
    base["Cadastradas"] = base["Cadastradas"].astype(int)
    base["Abertas"] = base["Abertas"].astype(int)

    base["Percentual_num"] = base.apply(
        lambda r: (r["Abertas"] / r["Cadastradas"]) if r["Cadastradas"] > 0 else 0.0,
        axis=1
    )

    base["% Abertas/Cadastradas"] = base["Percentual_num"].map(lambda x: f"{x*100:.1f}%".replace(".", ","))
    base["Indicador"] = base["Percentual_num"].map(lambda x: "Dentro do alvo" if x >= ALVO_CONVERSAO else "Abaixo do alvo")
    base["Mes_ref"] = base["Data"].map(lambda d: dt.date(d.year, d.month, 1))

    meses = sorted(base["Mes_ref"].unique())
    meses_lbl = [fmt_month(m) for m in meses]

    # por padrão, mostra o mês do ref_date
    ref_month = dt.date(ref_date.year, ref_date.month, 1)
    default_idx = meses.index(ref_month) if ref_month in meses else len(meses) - 1

    mes_sel_lbl = st.selectbox("Selecione o mês", meses_lbl, index=default_idx)
    mes_sel = meses[meses_lbl.index(mes_sel_lbl)]

    mes_df = base[base["Mes_ref"] == mes_sel].copy()

    # ordena do mais recente para o mais antigo
    mes_df = mes_df.sort_values("Data", ascending=False)

    total_ab_mes = int(mes_df["Abertas"].sum())
    total_cad_mes = int(mes_df["Cadastradas"].sum())
    perc_mes = (total_ab_mes / total_cad_mes) if total_cad_mes > 0 else 0.0

    if perc_mes >= ALVO_CONVERSAO:
        st.markdown(f"<div class='am-badge-ok'>Conversão do mês: {str(round(perc_mes*100,1)).replace('.',',')}%</div>", unsafe_allow_html=True)
    else:
        st.markdown(f"<div class='am-badge-bad'>Conversão do mês: {str(round(perc_mes*100,1)).replace('.',',')}%</div>", unsafe_allow_html=True)

    ca, cb, cc = st.columns(3)
    ca.metric("Abertas (mês)", br_int(total_ab_mes))
    cb.metric("Cadastradas (mês)", br_int(total_cad_mes))
    cc.metric("Mês", mes_sel_lbl)

    # tabela SEM numeração de linha, SEM coluna técnica
    display = mes_df[["Data", "Cadastradas", "Abertas", "% Abertas/Cadastradas", "Indicador"]].copy()
    display["Data"] = display["Data"].apply(fmt_date)
    display["Cadastradas"] = display["Cadastradas"].apply(br_int)
    display["Abertas"] = display["Abertas"].apply(br_int)

    perc_series = mes_df["Percentual_num"].copy()
    perc_series.index = display.index  # garante alinhamento

    st.caption("Tabela diária do mês selecionado (azul ≥ 20% | vermelho < 20%)")
    st.dataframe(
        style_conversao_display(display, perc_series).hide(axis="index"),
        use_container_width=True
    )

    st.markdown("#### Fechamento do mês")
    f1, f2, f3 = st.columns(3)
    f1.metric("Total Cadastradas (mês)", br_int(total_cad_mes))
    f2.metric("Total Abertas (mês)", br_int(total_ab_mes))
    f3.metric("% Geral (mês)", f"{perc_mes*100:.1f}%".replace(".", ","))

    st.caption("Produção diária (cadastradas vs abertas)")
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
    st.info("Envie a planilha diária do C6 para liberar relatórios diários.")
else:
    tabs = st.tabs(["Aberturas", "Fundações (por dia)", "Pix + Status", "Qualificação + BR (M0/M1/M2)"])

    # Aberturas (mais bonito, sem linha, mais recente -> mais antigo)
    with tabs[0]:
        st.markdown("#### Contas abertas por dia (arquivo)")

        por_dia = (
            pd.Series(df_c6[COL_ABERTURA]).dropna().value_counts().sort_index()
            .rename_axis("Dia")
            .reset_index(name="Contas abertas")
        )
        por_dia["Dia"] = por_dia["Dia"].apply(fmt_date)
        # mais recente primeiro
        por_dia = por_dia.sort_values("Dia", ascending=False)

        st.bar_chart(por_dia.set_index("Dia")["Contas abertas"])
        st.dataframe(por_dia.style.hide(axis="index"), use_container_width=True)

    # Fundações por dia (mantém estilo que você disse que está perfeito)
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

            pivot["Dia_fmt"] = pivot["Dia"].apply(fmt_date)
            dia_df = pivot[pivot["Dia"] == dia_sel].copy()
            dia_df = dia_df[["Mês fundação", "Quantidade"]].sort_values("Mês fundação")

            total_dia = int(dia_df["Quantidade"].sum())
            st.markdown(f"**No dia {dia_sel_lbl} foram abertas {br_int(total_dia)} empresas.**")

            st.dataframe(dia_df.style.hide(axis="index"), use_container_width=True)
            st.bar_chart(dia_df.set_index("Mês fundação")["Quantidade"])

    # Pix + Status
    with tabs[2]:
        st.markdown("#### Pix (arquivo)")
        pix_com, pix_sem, pix_por_chave = pix_summary(df_c6)
        a, b = st.columns(2)
        a.metric("Clientes com Pix", br_int(pix_com))
        b.metric("Clientes sem Pix", br_int(pix_sem))

        st.dataframe(pix_por_chave.style.hide(axis="index"), use_container_width=True)

        st.markdown("#### Status (arquivo)")
        status = (
            df_c6[COL_STATUS]
            .replace("", "SEM STATUS")
            .fillna("SEM STATUS")
            .value_counts()
            .rename_axis("Status")
            .reset_index(name="Quantidade")
        )
        st.dataframe(status.style.hide(axis="index"), use_container_width=True)
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

        # BR (M0/M1/M2) - contagem
        st.markdown("#### Distribuição BR (M0/M1/M2)")
        brs = normalize_str(dfq.get(COL_BR, pd.Series([""] * len(dfq)))).str.upper()
        br_counts = (
            brs.replace("", "SEM BR")
            .value_counts()
            .rename_axis("BR")
            .reset_index(name="Quantidade")
        )
        st.dataframe(br_counts.style.hide(axis="index"), use_container_width=True)

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

        st.dataframe(show.style.hide(axis="index"), use_container_width=True)

st.divider()

# =========================================================
# HISTÓRICO DE REMUNERAÇÃO (TODOS OS MESES DISPONÍVEIS)
# =========================================================
st.subheader("Comparativo de remuneração (histórico mensal)")

if df_monthly.empty:
    st.info("Ainda não há histórico mensal. Importe dias (C6) a partir de janeiro para formar o mês e calcular a remuneração.")
else:
    view = df_monthly.copy()

    # ordena (mm/aaaa)
    def month_key(s: str) -> int:
        try:
            mm, aa = s.split("/")
            return int(aa) * 100 + int(mm)
        except Exception:
            return 0

    view = view.sort_values("Mês", key=lambda col: col.map(month_key), ascending=True)

    view_show = view.copy()
    view_show["Qualificadas"] = view_show["Qualificadas"].apply(lambda x: br_int(int(x)))
    view_show["Deveria receber (cheio)"] = view_show["Deveria receber (cheio)"].apply(lambda x: br_money(float(x)))
    view_show["Já pago (referência)"] = view_show["Já pago (referência)"].apply(lambda x: br_money(float(x)))
    view_show["A receber no mês"] = view_show["A receber no mês"].apply(lambda x: br_money(float(x)))

    st.dataframe(view_show.style.hide(axis="index"), use_container_width=True)

    # último mês disponível (cartões)
    last = view.iloc[-1]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Último mês calculado", str(last["Mês"]))
    c2.metric("Qualificadas", br_int(int(last["Qualificadas"])))
    c3.metric("Receita cheia", br_money(float(last["Deveria receber (cheio)"])))
    c4.metric("A receber", br_money(float(last["A receber no mês"])))
