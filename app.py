import os
import io
import re
import json
import hashlib
import datetime as dt
from typing import Optional, Dict, Tuple, List

import pandas as pd
import streamlit as st

# =========================================================
# CONFIGURAÇÕES (colunas)
# =========================================================

# Planilha C6 (Visão Cliente) - diária
COL_OPEN_DATE = "DT_CONTA_CRIADA"            # data abertura/conta criada (você chama de "coluna T")
COL_FOUND_DATE = "DT_FUNDACAO_EMPRESA"       # data fundação (você chama de "coluna P")
COL_PIX_TYPE = "CHAVES_PIX_FORTE"            # (você chama de "coluna X")
COL_SALDO = "VL_SALDO_MEDIO_MENSALIZADO"     # (você chama de "coluna Y")
COL_STATUS = "STATUS_CC"                     # (você chama de "coluna V")
COL_DOMICILIO = "BANCO_DOMICILIO"            # (você chama de "coluna AQ")

# Coluna de critérios (vale para tudo: diária e mensal)
COL_CRITERIOS = "CRITERIOS_ATINGIDOS_COMISS"

# Coluna do CNPJ (você confirmou SIM)
COL_CNPJ = "CD_CPF_CNPJ_CLIENTE"

# Planilha Leads - diária
LEADS_COL_DATE = "DATA_CADASTRO"  # você disse "coluna M tem a data do cadastro"
# Caso sua planilha venha com outro nome, o app tenta achar automaticamente.

# =========================================================
# Regras de remuneração por faixa
# =========================================================
# Faixa definida pela quantidade total de QUALIFICADAS no mês
# (nível: 1..4)
PAYOUT_TIERS = [
    # (min_qualificadas, multiplicador_nome, tabela_nivel)
    (0,   "Até 49 qualificadas (1.0)",  {1: 140.00, 2: 230.00, 3: 400.00, 4: 540.00}),
    (50,  "50 a 149 qualificadas (1.1)", {1: 154.00, 2: 253.00, 3: 440.00, 4: 594.00}),
    (150, "150 a 349 qualificadas (1.25)", {1: 175.00, 2: 287.50, 3: 500.00, 4: 675.00}),
    (350, "350+ qualificadas (1.5)",  {1: 210.00, 2: 345.00, 3: 600.00, 4: 810.00}),
]

# =========================================================
# Persistência local (histórico) - OBS: em Streamlit Cloud, pode resetar após redeploy
# =========================================================
DATA_DIR = "data_store"
os.makedirs(DATA_DIR, exist_ok=True)

HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_open_daily.csv")     # aberturas por dia
HIST_LEADS_DAILY = os.path.join(DATA_DIR, "hist_leads_daily.csv")   # cadastradas por dia
HIST_MONTHLY_PAY = os.path.join(DATA_DIR, "hist_monthly_pay.csv")   # remuneração mensal incremental

# =========================================================
# Helpers
# =========================================================
def br_date(d: Optional[dt.date]) -> str:
    if pd.isna(d) or d is None:
        return ""
    if isinstance(d, pd.Timestamp):
        d = d.date()
    return d.strftime("%d/%m/%Y")

def br_money(v: float) -> str:
    try:
        v = float(v)
    except:
        v = 0.0
    s = f"{v:,.2f}"
    # troca padrão americano para BR
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"

def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _read_excel(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")

def _to_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.date

def _norm_str(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").str.strip()

def _contains_c6(x: str) -> bool:
    return "c6" in str(x).lower()

def _ensure_cols(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    return df

# =========================================================
# REGRA DE QUALIFICAÇÃO (ÚNICA E OFICIAL)
# - Qualifica se CRITERIOS_ATINGIDOS_COMISS tiver qualquer 1..4
# - Nível = MAIOR número encontrado (1..4)
# =========================================================
CRIT_RE = re.compile(r":\s*(\d+)")
def parse_level_from_criterios(txt: str) -> int:
    if not isinstance(txt, str):
        return 0
    nums = [int(n) for n in CRIT_RE.findall(txt)]
    nums = [n for n in nums if n in (1, 2, 3, 4)]
    return max(nums) if nums else 0

def add_qual_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = _ensure_cols(df, [COL_CRITERIOS])
    df["Nivel_Qualificacao"] = df[COL_CRITERIOS].astype(str).apply(parse_level_from_criterios).astype(int)
    df["Qualificada"] = (df["Nivel_Qualificacao"] >= 1).astype(int)
    return df

# =========================================================
# Pix
# =========================================================
def pix_summary(df: pd.DataFrame) -> Tuple[int, int, pd.DataFrame]:
    df = _ensure_cols(df, [COL_PIX_TYPE])
    s = df[COL_PIX_TYPE].astype("string").fillna("").str.strip().str.upper()
    s = s.str.replace("'", "", regex=False)

    # considera sem pix: vazio, "-", "0", "NAN", etc
    sem = s.isin(["", "-", "0", "NAN", "NONE", "SEM", "SEM PIX"])
    com = ~sem

    qtd_com = int(com.sum())
    qtd_sem = int(sem.sum())

    por_tipo = (
        s.loc[com]
        .value_counts(dropna=True)
        .rename_axis("Tipo de chave Pix")
        .reset_index(name="Quantidade")
    )
    return qtd_com, qtd_sem, por_tipo

# =========================================================
# Aberturas (contas criadas) e fundações
# =========================================================
def contas_por_dia(df: pd.DataFrame) -> pd.DataFrame:
    df = _ensure_cols(df, [COL_OPEN_DATE])
    d = _to_date_series(df[COL_OPEN_DATE])
    out = (
        pd.Series(d).dropna()
        .value_counts()
        .sort_index()
        .rename_axis("Dia")
        .reset_index(name="Contas abertas")
    )
    return out

def contas_por_mes(df: pd.DataFrame) -> pd.DataFrame:
    df = _ensure_cols(df, [COL_OPEN_DATE])
    t = pd.to_datetime(df[COL_OPEN_DATE], errors="coerce")
    out = (
        t.dropna().dt.to_period("M").astype(str)
        .value_counts().sort_index()
        .rename_axis("Mês")
        .reset_index(name="Contas abertas")
    )
    return out

def fundacoes_mes_por_dia(df: pd.DataFrame) -> pd.DataFrame:
    df = _ensure_cols(df, [COL_OPEN_DATE, COL_FOUND_DATE])
    d_open = _to_date_series(df[COL_OPEN_DATE])
    d_found = pd.to_datetime(df[COL_FOUND_DATE], errors="coerce")

    tmp = pd.DataFrame({
        "Dia": d_open,
        "Mes_Fundacao": d_found.dt.to_period("M").astype(str)
    }).dropna()

    if tmp.empty:
        return pd.DataFrame(columns=["Dia", "Mês de fundação", "Quantidade"])

    out = (
        tmp.groupby(["Dia", "Mes_Fundacao"])
        .size()
        .reset_index(name="Quantidade")
        .rename(columns={"Mes_Fundacao": "Mês de fundação"})
        .sort_values(["Dia", "Mês de fundação"])
    )
    return out

# =========================================================
# Status
# =========================================================
def status_counts(df: pd.DataFrame) -> pd.DataFrame:
    df = _ensure_cols(df, [COL_STATUS])
    s = df[COL_STATUS].fillna("").astype(str).str.strip()
    s = s.replace("", "Sem status")
    out = (
        s.value_counts()
        .rename_axis("Status")
        .reset_index(name="Quantidade")
    )
    return out

# =========================================================
# Domicílio C6
# =========================================================
def domicilio_c6_count(df: pd.DataFrame) -> int:
    df = _ensure_cols(df, [COL_DOMICILIO])
    s = df[COL_DOMICILIO].fillna("").astype(str)
    return int(s.apply(_contains_c6).sum())

# =========================================================
# Leads (cadastradas)
# =========================================================
def guess_leads_date_col(df: pd.DataFrame) -> str:
    # tenta achar automaticamente algo com "DATA" e "CAD" se não for exatamente DATA_CADASTRO
    if LEADS_COL_DATE in df.columns:
        return LEADS_COL_DATE

    cols = [c for c in df.columns if isinstance(c, str)]
    for c in cols:
        cu = c.upper()
        if "DATA" in cu and ("CAD" in cu or "CADAST" in cu):
            return c
    # fallback: primeira coluna com datetime-like
    for c in cols:
        try:
            s = pd.to_datetime(df[c], errors="coerce")
            if s.notna().sum() > 0:
                return c
        except:
            pass
    return LEADS_COL_DATE  # cria vazio depois

def leads_por_dia(df: pd.DataFrame) -> pd.DataFrame:
    col = guess_leads_date_col(df)
    df = _ensure_cols(df, [col])
    d = _to_date_series(df[col])
    out = (
        pd.Series(d).dropna()
        .value_counts()
        .sort_index()
        .rename_axis("Dia")
        .reset_index(name="Contas cadastradas")
    )
    return out

# =========================================================
# Histórico (upsert)
# =========================================================
def _load_hist(path: str, key_col: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=[key_col])
    try:
        return pd.read_csv(path)
    except:
        return pd.DataFrame(columns=[key_col])

def _save_hist(path: str, df: pd.DataFrame) -> None:
    df.to_csv(path, index=False)

def upsert_hist(path: str, key_col: str, new_df: pd.DataFrame) -> pd.DataFrame:
    base = _load_hist(path, key_col)
    if base.empty:
        base = pd.DataFrame(columns=new_df.columns)

    merged = pd.concat([base, new_df], ignore_index=True)

    # remove duplicados mantendo o último
    if key_col in merged.columns:
        merged = merged.drop_duplicates(subset=[key_col], keep="last")

    # ordena (tenta converter para date)
    try:
        merged[key_col] = pd.to_datetime(merged[key_col], errors="coerce").dt.date.astype(str)
        merged = merged.sort_values(by=key_col)
    except:
        pass

    _save_hist(path, merged)
    return merged

# =========================================================
# Cadastro x Abertura (abertas / cadastradas)
# =========================================================
def cadastro_x_abertura(open_daily: pd.DataFrame, leads_daily: pd.DataFrame) -> pd.DataFrame:
    # open_daily: Dia, Contas abertas
    # leads_daily: Dia, Contas cadastradas
    a = open_daily.copy()
    b = leads_daily.copy()
    if a.empty and b.empty:
        return pd.DataFrame(columns=["Dia", "Cadastradas", "Abertas", "Percentual"])

    a["Dia"] = pd.to_datetime(a["Dia"], errors="coerce").dt.date.astype(str)
    b["Dia"] = pd.to_datetime(b["Dia"], errors="coerce").dt.date.astype(str)

    a = a.rename(columns={"Contas abertas": "Abertas"})
    b = b.rename(columns={"Contas cadastradas": "Cadastradas"})

    aux = pd.merge(b, a, on="Dia", how="outer").fillna(0)
    aux["Cadastradas"] = aux["Cadastradas"].astype(int)
    aux["Abertas"] = aux["Abertas"].astype(int)

    # fórmula correta: abertas / cadastradas
    aux["Percentual_num"] = aux.apply(
        lambda r: (r["Abertas"] / r["Cadastradas"]) if r["Cadastradas"] > 0 else 0.0,
        axis=1
    )
    aux["Percentual"] = aux["Percentual_num"].apply(lambda x: f"{x*100:.1f}%".replace(".", ","))

    # formato BR para exibição
    aux["Dia"] = pd.to_datetime(aux["Dia"], errors="coerce").dt.date.apply(lambda d: br_date(d))

    aux = aux.sort_values(by="Dia")
    return aux

def monthly_ratio_table(daily_ratio: pd.DataFrame) -> pd.DataFrame:
    if daily_ratio.empty:
        return pd.DataFrame(columns=["Mês", "Cadastradas", "Abertas", "Percentual"])

    tmp = daily_ratio.copy()
    # reconverte "Dia" BR para date
    tmp["_dia"] = pd.to_datetime(tmp["Dia"], format="%d/%m/%Y", errors="coerce")
    tmp["_mes"] = tmp["_dia"].dt.to_period("M").astype(str)

    out = tmp.groupby("_mes")[["Cadastradas", "Abertas"]].sum().reset_index()
    out = out.rename(columns={"_mes": "Mês"})
    out["Percentual_num"] = out.apply(
        lambda r: (r["Abertas"] / r["Cadastradas"]) if r["Cadastradas"] > 0 else 0.0,
        axis=1
    )
    out["Percentual"] = out["Percentual_num"].apply(lambda x: f"{x*100:.1f}%".replace(".", ","))
    return out

# =========================================================
# Remuneração mensal incremental
# =========================================================
def pick_payout_table(total_qual: int) -> Tuple[str, Dict[int, float]]:
    chosen_name = PAYOUT_TIERS[0][1]
    chosen_table = PAYOUT_TIERS[0][2]
    for minq, name, table in PAYOUT_TIERS:
        if total_qual >= minq:
            chosen_name = name
            chosen_table = table
    return chosen_name, chosen_table

def month_key_from_filename(filename: str) -> str:
    # tenta extrair "NOVEMBRO2025" / "DEZEMBRO2025"
    # se não achar, usa data de hoje
    base = os.path.splitext(os.path.basename(filename))[0].upper()
    # tenta achar padrão MMMMYYYY
    m = re.search(r"(JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ)", base)
    y = re.search(r"(20\d{2})", base)
    if m and y:
        mon = m.group(1)
        yr = y.group(1)
        map_pt = {
            "JAN":"01","FEV":"02","MAR":"03","ABR":"04","MAI":"05","JUN":"06",
            "JUL":"07","AGO":"08","SET":"09","OUT":"10","NOV":"11","DEZ":"12"
        }
        return f"{yr}-{map_pt[mon]}"
    # fallback
    today = dt.date.today()
    return f"{today.year}-{today.month:02d}"

def calc_monthly_incremental(month_dfs: List[Tuple[str, pd.DataFrame]]) -> pd.DataFrame:
    """
    month_dfs: lista de (month_key 'YYYY-MM', df do mês)
    Retorna tabela mensal com:
      Mês, Qualificadas, Faixa, Receita cheia, Já recebido (acumulado), Receita do mês
    """
    # ordena meses
    month_dfs = sorted(month_dfs, key=lambda x: x[0])

    paid_max_by_cnpj: Dict[str, float] = {}  # maior valor cheio já pago por CNPJ até agora
    received_acc = 0.0

    rows = []
    for mkey, df in month_dfs:
        df = _ensure_cols(df, [COL_CNPJ, COL_CRITERIOS])
        df[COL_CNPJ] = _norm_str(df[COL_CNPJ])

        df = add_qual_columns(df)
        dfq = df[df["Qualificada"] == 1].copy()

        total_qual = int(dfq.shape[0])

        faixa_nome, payout_table = pick_payout_table(total_qual)

        # receita cheia por CNPJ = valor do nível (vitorioso) na faixa do mês
        dfq["Valor_Cheio"] = dfq["Nivel_Qualificacao"].map(payout_table).fillna(0.0).astype(float)

        # por CNPJ, se tiver duplicado, fica com o maior valor do mês (segurança)
        by_cnpj = dfq.groupby(COL_CNPJ)["Valor_Cheio"].max().reset_index()

        # incremental por CNPJ
        inc_list = []
        for _, r in by_cnpj.iterrows():
            cnpj = str(r[COL_CNPJ]).strip()
            cheio = float(r["Valor_Cheio"])
            prev_paid = float(paid_max_by_cnpj.get(cnpj, 0.0))
            inc = max(0.0, cheio - prev_paid)
            inc_list.append(inc)
            # atualiza histórico de "maior pago"
            paid_max_by_cnpj[cnpj] = max(prev_paid, cheio)

        receita_mes = float(sum(inc_list))
        receita_cheia = float(by_cnpj["Valor_Cheio"].sum()) if not by_cnpj.empty else 0.0

        rows.append({
            "Mês": mkey,
            "Qualificadas": total_qual,
            "Faixa aplicada": faixa_nome,
            "Receita cheia (mês)": receita_cheia,
            "Já recebido (acumulado até mês anterior)": received_acc,
            "Receita do mês (incremental)": receita_mes,
        })

        received_acc += receita_mes

    out = pd.DataFrame(rows)
    return out

# =========================================================
# UI / Branding
# =========================================================
def inject_css():
    st.markdown(
        """
        <style>
          .block-container { padding-top: 1.0rem; }
          [data-testid="stSidebar"] { background: linear-gradient(180deg, #1f2a4a 0%, #0f172a 100%); }
          [data-testid="stSidebar"] * { color: #ffffff !important; }
          .am-title { font-size: 2.0rem; font-weight: 800; color: #1f2a4a; margin-bottom: 0.2rem; }
          .am-sub { color: #6b7280; margin-top: -0.2rem; }
          .kpi-card { border-radius: 16px; padding: 14px 16px; border: 1px solid #e5e7eb; background: #ffffff; }
          .kpi-label { font-size: 0.8rem; color: #6b7280; }
          .kpi-value { font-size: 1.4rem; font-weight: 800; color: #111827; }
          .pill { display:inline-block; padding: 6px 10px; border-radius: 999px; font-weight: 700; font-size: 0.85rem; }
          .pill-blue { background:#dbeafe; color:#1d4ed8; }
          .pill-red { background:#fee2e2; color:#b91c1c; }
          .pill-gray { background:#f3f4f6; color:#374151; }
        </style>
        """,
        unsafe_allow_html=True
    )

def load_logo():
    # tenta carregar sua logo do repositório
    for p in ["LOGO CORRETA.png", "logo.png", "LOGO.png"]:
        if os.path.exists(p):
            return p
    return None

# =========================================================
# Login simples
# =========================================================
def login_gate() -> bool:
    st.sidebar.markdown("## Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")
    if st.sidebar.button("Entrar"):
        if u == "admin" and p == "123456":
            st.session_state["logged_in"] = True
        else:
            st.session_state["logged_in"] = False
            st.sidebar.error("Usuário ou senha inválidos.")
    return st.session_state.get("logged_in", False)

# =========================================================
# APP
# =========================================================
st.set_page_config(page_title="Assis & Mollerke", layout="wide")
inject_css()

if not login_gate():
    st.stop()

logo_path = load_logo()

colA, colB = st.columns([1, 6])
with colA:
    if logo_path:
        st.image(logo_path, use_container_width=True)
with colB:
    st.markdown('<div class="am-title">Assis & Mollerke</div>', unsafe_allow_html=True)
    st.markdown('<div class="am-sub">Painel executivo — Aberturas, Leads e Remuneração Incremental</div>', unsafe_allow_html=True)

st.divider()

# ==========================
# Uploads
# ==========================
st.markdown("### Importação do dia")

up1, up2 = st.columns(2)
with up1:
    st.caption("Planilha C6 (Visão Cliente) — diária")
    file_c6 = st.file_uploader("Enviar arquivo C6 (.xlsx)", type=["xlsx"], key="c6")
with up2:
    st.caption("Planilha Leads — diária")
    file_leads = st.file_uploader("Enviar arquivo Leads (.xlsx)", type=["xlsx"], key="leads")

st.markdown("### Remuneração (mensal)")
st.caption("Envie os arquivos mensais (ex.: NOVEMBRO2025.xlsx, DEZEMBRO2025.xlsx...). O sistema calcula incremental por CNPJ.")
files_month = st.file_uploader("Enviar arquivos mensais (.xlsx) — múltiplos", type=["xlsx"], accept_multiple_files=True, key="monthfiles")

# ==========================
# Processar C6 diário
# ==========================
daily_open = pd.DataFrame()
daily_found = pd.DataFrame()
pix_types = pd.DataFrame()
status_df = pd.DataFrame()
saldo_total = 0.0
dom_c6 = 0
qual_count = 0
qual_by_level = pd.DataFrame()

if file_c6:
    df_c6 = _read_excel(file_c6.getvalue())
    df_c6 = _ensure_cols(df_c6, [COL_OPEN_DATE, COL_FOUND_DATE, COL_PIX_TYPE, COL_SALDO, COL_STATUS, COL_DOMICILIO, COL_CRITERIOS, COL_CNPJ])
    df_c6[COL_OPEN_DATE] = _to_date_series(df_c6[COL_OPEN_DATE])
    df_c6[COL_FOUND_DATE] = pd.to_datetime(df_c6[COL_FOUND_DATE], errors="coerce")
    df_c6[COL_SALDO] = pd.to_numeric(df_c6[COL_SALDO], errors="coerce").fillna(0.0)

    df_c6 = add_qual_columns(df_c6)

    daily_open = contas_por_dia(df_c6)
    daily_found = fundacoes_mes_por_dia(df_c6)

    qtd_pix, qtd_sem_pix, pix_types = pix_summary(df_c6)
    status_df = status_counts(df_c6)
    saldo_total = float(df_c6[COL_SALDO].sum())
    dom_c6 = domicilio_c6_count(df_c6)

    qual_count = int(df_c6["Qualificada"].sum())
    qual_by_level = (
        df_c6[df_c6["Qualificada"] == 1]["Nivel_Qualificacao"]
        .value_counts().sort_index()
        .rename_axis("Nível")
        .reset_index(name="Quantidade")
    )

    # grava histórico de ABERTURAS por dia (para não "sumir" se o cliente sair amanhã)
    if not daily_open.empty:
        hist = daily_open.copy()
        hist["Dia"] = pd.to_datetime(hist["Dia"], errors="coerce").dt.date.astype(str)
        upsert_hist(HIST_OPEN_DAILY, "Dia", hist)

# ==========================
# Processar Leads diário
# ==========================
daily_leads = pd.DataFrame()

if file_leads:
    df_leads = _read_excel(file_leads.getvalue())
    col_ld = guess_leads_date_col(df_leads)
    df_leads = _ensure_cols(df_leads, [col_ld])
    df_leads[col_ld] = _to_date_series(df_leads[col_ld])
    daily_leads = leads_por_dia(df_leads)

    if not daily_leads.empty:
        hist = daily_leads.copy()
        hist["Dia"] = pd.to_datetime(hist["Dia"], errors="coerce").dt.date.astype(str)
        upsert_hist(HIST_LEADS_DAILY, "Dia", hist)

# ==========================
# Carregar histórico para cálculo a partir de Jan/2026
# ==========================
hist_open = _load_hist(HIST_OPEN_DAILY, "Dia")
hist_leads = _load_hist(HIST_LEADS_DAILY, "Dia")

def filter_from_jan_2026(df: pd.DataFrame, key="Dia") -> pd.DataFrame:
    if df.empty or key not in df.columns:
        return df
    d = pd.to_datetime(df[key], errors="coerce")
    df2 = df.copy()
    df2["_d"] = d
    df2 = df2[df2["_d"] >= pd.Timestamp("2026-01-01")].drop(columns=["_d"])
    return df2

hist_open = filter_from_jan_2026(hist_open, "Dia")
hist_leads = filter_from_jan_2026(hist_leads, "Dia")

# ==========================
# KPI Resumo do dia
# ==========================
st.markdown("### Resumo executivo (dia)")

k1, k2, k3, k4 = st.columns(4)
abertas_hoje_total = int(daily_open["Contas abertas"].sum()) if not daily_open.empty else 0
pix_com = int(qtd_pix) if file_c6 else 0
pix_sem = int(qtd_sem_pix) if file_c6 else 0

with k1:
    st.markdown(f"""
      <div class="kpi-card">
        <div class="kpi-label">Contas abertas (arquivo)</div>
        <div class="kpi-value">{abertas_hoje_total:,}".replace(",", ".")}</div>
      </div>
    """, unsafe_allow_html=True)

with k2:
    st.markdown(f"""
      <div class="kpi-card">
        <div class="kpi-label">Saldo total</div>
        <div class="kpi-value">{br_money(saldo_total)}</div>
      </div>
    """, unsafe_allow_html=True)

with k3:
    st.markdown(f"""
      <div class="kpi-card">
        <div class="kpi-label">Clientes com Pix</div>
        <div class="kpi-value">{pix_com:,}".replace(",", ".")}</div>
      </div>
    """, unsafe_allow_html=True)

with k4:
    st.markdown(f"""
      <div class="kpi-card">
        <div class="kpi-label">Clientes sem Pix</div>
        <div class="kpi-value">{pix_sem:,}".replace(",", ".")}</div>
      </div>
    """, unsafe_allow_html=True)

k5, k6, k7, k8 = st.columns(4)

with k5:
    st.markdown(f"""
      <div class="kpi-card">
        <div class="kpi-label">Clientes com domicílio C6</div>
        <div class="kpi-value">{dom_c6:,}".replace(",", ".")}</div>
      </div>
    """, unsafe_allow_html=True)

with k6:
    st.markdown(f"""
      <div class="kpi-card">
        <div class="kpi-label">Contas qualificadas</div>
        <div class="kpi-value">{qual_count:,}".replace(",", ".")}</div>
      </div>
    """, unsafe_allow_html=True)

with k7:
    # Receita "cheia" do dia não é incremental mensal.
    # Aqui mostramos apenas uma leitura rápida do dia pela regra da faixa 350+ (não é o cálculo incremental).
    # Para a receita oficial, use a aba Remuneração.
    st.markdown(f"""
      <div class="kpi-card">
        <div class="kpi-label">Receita (use aba Remuneração)</div>
        <div class="kpi-value"><span class="pill pill-gray">ver mensal</span></div>
      </div>
    """, unsafe_allow_html=True)

with k8:
    st.markdown(f"""
      <div class="kpi-card">
        <div class="kpi-label">Arquivo C6</div>
        <div class="kpi-value">{file_c6.name if file_c6 else "-"}</div>
      </div>
    """, unsafe_allow_html=True)

st.divider()

# ==========================
# Abas principais
# ==========================
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "Aberturas",
    "Fundações",
    "Pix e Status",
    "Cadastro x Abertura",
    "Remuneração"
])

# ---- TAB 1: Aberturas
with tab1:
    cA, cB = st.columns(2)

    with cA:
        st.markdown("#### Aberturas por dia (arquivo enviado)")
        if daily_open.empty:
            st.info("Envie a planilha C6 diária para exibir.")
        else:
            df_show = daily_open.copy()
            df_show["Dia"] = pd.to_datetime(df_show["Dia"], errors="coerce").dt.date.apply(br_date)
            st.dataframe(df_show, hide_index=True, use_container_width=True)

    with cB:
        st.markdown("#### Aberturas por mês (arquivo enviado)")
        if file_c6:
            st.dataframe(contas_por_mes(df_c6), hide_index=True, use_container_width=True)
        else:
            st.info("Envie a planilha C6 diária para exibir.")

# ---- TAB 2: Fundações
with tab2:
    st.markdown("#### Fundações por dia (mês/ano de fundação)")
    st.caption("Selecione um dia para ver a distribuição por mês de fundação das empresas abertas naquele dia.")

    if daily_found.empty:
        st.info("Envie a planilha C6 diária para exibir.")
    else:
        # prepara lista de dias
        dias = sorted(daily_found["Dia"].dropna().unique())
        dias_br = [br_date(pd.to_datetime(d, errors="coerce").date()) for d in dias]
        dia_sel = st.selectbox("Escolha o dia", dias_br)

        # filtra
        dia_sel_date = pd.to_datetime(dia_sel, format="%d/%m/%Y", errors="coerce").date()
        df_day = daily_found[daily_found["Dia"] == dia_sel_date].copy()

        # formata mês fundação como MM/AAAA
        def mes_br(period_str: str) -> str:
            # period_str "YYYY-MM"
            try:
                y, m = period_str.split("-")
                return f"{m}/{y}"
            except:
                return str(period_str)

        df_day["Mês de fundação"] = df_day["Mês de fundação"].apply(mes_br)
        st.dataframe(df_day[["Mês de fundação", "Quantidade"]], hide_index=True, use_container_width=True)

# ---- TAB 3: Pix e Status
with tab3:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### Chaves Pix")
        if file_c6:
            st.dataframe(pix_types, hide_index=True, use_container_width=True)
        else:
            st.info("Envie a planilha C6 diária para exibir.")

    with c2:
        st.markdown("#### Status das contas")
        if file_c6:
            st.dataframe(status_df, hide_index=True, use_container_width=True)
        else:
            st.info("Envie a planilha C6 diária para exibir.")

    st.markdown("#### Qualificadas por nível (no arquivo do dia)")
    if file_c6:
        st.dataframe(qual_by_level, hide_index=True, use_container_width=True)
    else:
        st.info("Envie a planilha C6 diária para exibir.")

# ---- TAB 4: Cadastro x Abertura
with tab4:
    st.markdown("#### Percentual de Aberturas sobre Cadastradas")
    st.caption("Cálculo: Abertas / Cadastradas. Destaque: azul se ≥ 20%, vermelho se < 20%.")

    # usa históricos (a partir de Jan/2026)
    if hist_open.empty or hist_leads.empty:
        st.info("Envie a planilha C6 diária e a Leads diária para alimentar o histórico (a partir de 01/01/2026).")
    else:
        # prepara dataframes diários do histórico
        ho = hist_open.copy()
        hl = hist_leads.copy()

        ho["Dia"] = pd.to_datetime(ho["Dia"], errors="coerce").dt.date
        hl["Dia"] = pd.to_datetime(hl["Dia"], errors="coerce").dt.date

        ho = ho.rename(columns={"Contas abertas": "Contas abertas"})
        hl = hl.rename(columns={"Contas cadastradas": "Contas cadastradas"})

        # reconstrói no formato esperado
        open_daily_hist = ho[["Dia", "Contas abertas"]].dropna()
        leads_daily_hist = hl[["Dia", "Contas cadastradas"]].dropna()

        # volta pra formato padrão das funções
        open_daily_hist = open_daily_hist.rename(columns={"Contas abertas": "Contas abertas"})
        leads_daily_hist = leads_daily_hist.rename(columns={"Contas cadastradas": "Contas cadastradas"})

        # para o cálculo
        open_daily_hist = open_daily_hist.rename(columns={"Contas abertas": "Contas abertas"})
        open_daily_hist = open_daily_hist.rename(columns={"Contas abertas": "Contas abertas"})

        # cria nos nomes que funções esperam
        od = open_daily_hist.rename(columns={"Contas abertas": "Contas abertas"})
        ld = leads_daily_hist.rename(columns={"Contas cadastradas": "Contas cadastradas"})

        # adapta para função
        od2 = od.rename(columns={"Contas abertas": "Contas abertas"})
        od2 = od2.rename(columns={"Contas abertas": "Contas abertas"})
        od2 = od2.rename(columns={"Contas abertas": "Contas abertas"})
        # (simples: monta igual as tabelas originais)
        od_table = pd.DataFrame({"Dia": od["Dia"], "Contas abertas": od["Contas abertas"].astype(int)})
        ld_table = pd.DataFrame({"Dia": ld["Dia"], "Contas cadastradas": ld["Contas cadastradas"].astype(int)})

        ratio = cadastro_x_abertura(
            open_daily=od_table.rename(columns={"Contas abertas": "Contas abertas"}),
            leads_daily=ld_table.rename(columns={"Contas cadastradas": "Contas cadastradas"}),
        )

        def style_ratio_row(row):
            v = row.get("Percentual_num", 0.0)
            # cor: >=20% azul, <20% vermelho
            if v >= 0.20:
                return [""] * 3 + ["background-color: #dbeafe; color: #1d4ed8; font-weight: 800;"]
            else:
                return [""] * 3 + ["background-color: #fee2e2; color: #b91c1c; font-weight: 800;"]

        st.markdown("##### Visão diária")
        show_daily = ratio.copy()
        # usa styler mantendo Percentual_num internamente
        sty = show_daily.style.apply(style_ratio_row, axis=1)
        st.dataframe(sty.hide(axis="index"), use_container_width=True)

        st.markdown("##### Visão mensal")
        mtab = monthly_ratio_table(ratio)
        def style_month_row(row):
            v = row.get("Percentual_num", 0.0)
            if v >= 0.20:
                return [""] * 3 + ["background-color: #dbeafe; color: #1d4ed8; font-weight: 800;"]
            else:
                return [""] * 3 + ["background-color: #fee2e2; color: #b91c1c; font-weight: 800;"]

        msty = mtab.style.apply(style_month_row, axis=1)
        st.dataframe(msty.hide(axis="index"), use_container_width=True)

# ---- TAB 5: Remuneração
with tab5:
    st.markdown("#### Remuneração incremental (por CNPJ) — a partir de Novembro/2025")
    st.caption("Regra: nível = MAIOR critério (1..4) dentro do texto. Incremental = max(0, valor do mês - maior valor já pago anteriormente por CNPJ).")

    if not files_month:
        st.info("Envie os arquivos mensais (ex.: NOVEMBRO2025.xlsx, DEZEMBRO2025.xlsx...) para calcular.")
    else:
        month_dfs = []
        for f in files_month:
            dfm = _read_excel(f.getvalue())
            # garante colunas essenciais
            dfm = _ensure_cols(dfm, [COL_CNPJ, COL_CRITERIOS])
            mkey = month_key_from_filename(f.name)
            month_dfs.append((mkey, dfm))

        summary = calc_monthly_incremental(month_dfs)

        # formata pra exibição
        show = summary.copy()
        show["Receita cheia (mês)"] = show["Receita cheia (mês)"].apply(br_money)
        show["Já recebido (acumulado até mês anterior)"] = show["Já recebido (acumulado até mês anterior)"].apply(br_money)
        show["Receita do mês (incremental)"] = show["Receita do mês (incremental)"].apply(br_money)

        st.markdown("##### Resumo mensal (cheio x já recebido x incremental)")
        st.dataframe(show, hide_index=True, use_container_width=True)

        # opcional: salvar histórico local da última execução
        try:
            raw = summary.copy()
            raw.to_csv(HIST_MONTHLY_PAY, index=False)
        except:
            pass

st.caption("Obs.: o histórico é salvo localmente no servidor. Se a aplicação for redeployada, pode reiniciar. Se quiser histórico permanente, depois ligamos em um Google Sheet/Drive ou banco.")
