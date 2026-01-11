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
COL_BY = "FL_QUALIFICADO_COMISS"                  # coluna que pode vir 0/1 OU 0/1/2/3/4 OU texto
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"           # texto "CASH IN: X | ..."

# Planilha "Leads" (cadastros)
COL_LEADS_DATA = "DATA_CADASTRO"                  # vamos mapear a coluna M para este nome

# Regras de cor do indicador de conversão
ALVO_CONVERSAO = 0.20  # 20%

# A partir de qual data você quer "memorizar" histórico diário
HIST_START = dt.date(2026, 1, 1)

# =========================================================
# REGRAS DE REMUNERAÇÃO (POR FAIXA)
# =========================================================
# Faixa definida pela QUANTIDADE TOTAL de qualificadas do mês (contagem de CNPJs qualificados no mês)
# Valores por nível (1..4) são EXATOS conforme você informou.
FAIXAS = [
    # (min_qualificadas, nome, {nivel: valor})
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

HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_aberturas_diario.json")     # aberturas por dia
HIST_LEADS_DAILY = os.path.join(DATA_DIR, "hist_cadastros_diario.json")    # cadastros por dia (leads)
HIST_LAST_SNAPSHOT = os.path.join(DATA_DIR, "snapshot_ultimo.json")        # resumo último upload (diferença vs ontem)

HIST_PAGO_POR_CNPJ = os.path.join(DATA_DIR, "pago_max_por_cnpj.json")      # max pago por CNPJ (incremental)
HIST_RESUMO_MENSAL = os.path.join(DATA_DIR, "resumo_mensal.json")          # resumo mensal incremental


# =========================================================
# HELPERS GERAIS
# =========================================================
def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def br_money(v: float) -> str:
    # formato contábil: R$ 1.234,56
    s = f"{v:,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def br_int(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def fmt_date(d: Optional[dt.date]) -> str:
    if not d or pd.isna(d):
        return ""
    if isinstance(d, pd.Timestamp):
        d = d.date()
    return d.strftime("%d/%m/%Y")


def fmt_month(d: dt.date) -> str:
    return d.strftime("%m/%Y")


def to_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.date


def normalize_str(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").str.strip()


def contains_c6(x) -> bool:
    if x is None or pd.isna(x):
        return False
    return "c6" in str(x).lower()


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
    if m < 0:
        return 0
    if m > 4:
        m = 4
    return m


def parse_level(df: pd.DataFrame) -> pd.Series:
    """
    Regra final (robusta):
    - Se BY tiver número 1..4 => nível = esse número
    - Senão tenta extrair do CRITERIOS_ATINGIDOS_COMISS (maior valor)
    - Qualificada = nível >= 1
    """
    # BY pode vir como texto, número, 0/1, etc.
    by_raw = df.get(COL_BY, pd.Series([None] * len(df)))
    by_num = pd.to_numeric(by_raw, errors="coerce")

    level_by = by_num.fillna(0).astype(int)
    level_by = level_by.where(level_by.between(1, 4), 0)

    # critérios
    crit_raw = df.get(COL_CRIT, pd.Series([""] * len(df))).astype("string").fillna("")
    level_crit = crit_raw.apply(parse_level_from_criterios).astype(int)

    # prioriza BY quando tiver nível; senão usa critérios
    level = level_by.where(level_by > 0, level_crit)
    level = level.fillna(0).astype(int)
    level = level.where(level.between(1, 4), 0)
    return level


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
def upsert_daily_hist(path: str, key: str, series_counts: pd.DataFrame):
    """
    series_counts: DataFrame com colunas [key, "Quantidade"]
    Salva/atualiza no histórico (somando por dia, mantendo o valor mais recente por dia)
    """
    base = safe_json_load(path, default={})  # {"dd/mm/aaaa": qty}
    for _, row in series_counts.iterrows():
        d = row[key]
        q = int(row["Quantidade"])
        if isinstance(d, (dt.date, pd.Timestamp)):
            d = fmt_date(d if isinstance(d, dt.date) else d.date())
        if not d:
            continue
        # substitui pelo valor do arquivo mais recente daquele dia
        base[d] = q
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
    out = pd.DataFrame(rows, columns=["Data", colname])
    return out


# =========================================================
# REMUNERAÇÃO MENSAL INCREMENTAL
# =========================================================
def faixa_por_qtd(qtd_qualificadas: int) -> Tuple[str, Dict[int, float]]:
    """
    Retorna (nome_faixa, tabela_preco_por_nivel)
    """
    chosen_name, chosen_tbl = FAIXAS[0][1], FAIXAS[0][2]
    for min_q, nm, tbl in FAIXAS:
        if qtd_qualificadas >= min_q:
            chosen_name, chosen_tbl = nm, tbl
    return chosen_name, chosen_tbl


def detect_month_from_file(df: pd.DataFrame) -> Optional[dt.date]:
    """
    Tenta pegar o mês do arquivo:
    - se tiver DT_CONTA_CRIADA, pega o mês mais frequente
    - senão tenta por DATA_BASE (se existir)
    - senão None
    """
    if COL_ABERTURA in df.columns:
        d = to_date_series(df[COL_ABERTURA]).dropna()
        if len(d) > 0:
            # mês mais frequente
            m = pd.Series([dt.date(x.year, x.month, 1) for x in d]).mode()
            if len(m) > 0:
                return m.iloc[0]
    # fallback
    if "DATA_BASE" in df.columns:
        d = to_date_series(df["DATA_BASE"]).dropna()
        if len(d) > 0:
            m = pd.Series([dt.date(x.year, x.month, 1) for x in d]).mode()
            if len(m) > 0:
                return m.iloc[0]
    return None


def compute_monthly_incremental(files: List[Tuple[str, bytes]]) -> pd.DataFrame:
    """
    Recebe lista de arquivos mensais (Nov/25 em diante), calcula:
    - qualificados por mês
    - valor cheio do mês (deveria receber)
    - já pago (pela regra incremental: max anterior por CNPJ)
    - a receber no mês (somente diferenças positivas)
    Também atualiza memória pago_max_por_cnpj e resumo_mensal.
    """
    paid_max = safe_json_load(HIST_PAGO_POR_CNPJ, default={})  # {"CNPJ": valor_max_pago}

    resumo_mensal = safe_json_load(HIST_RESUMO_MENSAL, default={})  # {"mm/aaaa": {...}}

    month_rows = []

    # processa em ordem cronológica pelo mês detectado
    parsed = []
    for name, b in files:
        df = read_excel_any(b)
        # normaliza CNPJ
        if COL_CNPJ not in df.columns:
            # se não tiver, tenta achar uma coluna parecida
            # (não para em erro)
            cand = [c for c in df.columns if "CNPJ" in str(c).upper()]
            if cand:
                df[COL_CNPJ] = df[cand[0]]
            else:
                df[COL_CNPJ] = ""

        df[COL_CNPJ] = normalize_str(df[COL_CNPJ]).str.replace(r"\D", "", regex=True)

        df_level = parse_level(df)
        df["_nivel"] = df_level

        # mês
        month = detect_month_from_file(df)
        if month is None:
            # tenta pegar do nome do arquivo: NOVEMBRO2025, DEZEMBRO2025 etc.
            m = re.findall(r"(20\d{2})", name)
            month = dt.date(2025, 11, 1) if "NOVEMBRO" in name.upper() else None
            if month is None and "DEZEMBRO" in name.upper():
                month = dt.date(2025, 12, 1)
            if month is None and m:
                # sem mês exato, ignora
                month = None

        if month is None:
            continue

        parsed.append((month, name, df))

    parsed.sort(key=lambda x: x[0])

    for month, name, df in parsed:
        # qualificados = nível >= 1
        dfq = df[df["_nivel"] >= 1].copy()

        # conta por CNPJ (se repetir, fica o MAIOR nível daquele CNPJ no mês)
        by_cnpj = (
            dfq.groupby(COL_CNPJ)["_nivel"]
            .max()
            .reset_index()
            .rename(columns={"_nivel": "Nível"})
        )
        by_cnpj = by_cnpj[by_cnpj[COL_CNPJ] != ""]  # remove vazios

        qtd_qual = int(by_cnpj.shape[0])

        faixa_nome, precos = faixa_por_qtd(qtd_qual)

        # valor cheio por CNPJ
        by_cnpj["Valor cheio (mês)"] = by_cnpj["Nível"].map(lambda n: float(precos.get(int(n), 0.0)))

        # incremental: pagar somente diferença positiva vs max pago anterior
        ja_pago = []
        a_receber = []
        for _, r in by_cnpj.iterrows():
            cnpj = r[COL_CNPJ]
            cheio = float(r["Valor cheio (mês)"])
            prev_max = float(paid_max.get(cnpj, 0.0))
            diff = cheio - prev_max
            if diff < 0:
                diff = 0.0
            ja_pago.append(prev_max)
            a_receber.append(diff)

        by_cnpj["Já pago antes (CNPJ)"] = ja_pago
        by_cnpj["A receber (mês)"] = a_receber

        total_cheio = float(by_cnpj["Valor cheio (mês)"].sum())
        total_receber = float(by_cnpj["A receber (mês)"].sum())
        total_japago_ref = total_cheio - total_receber  # referência: parte que já estava "coberta" por pagamentos antigos

        # atualiza paid_max
        for _, r in by_cnpj.iterrows():
            cnpj = r[COL_CNPJ]
            cheio = float(r["Valor cheio (mês)"])
            prev = float(paid_max.get(cnpj, 0.0))
            paid_max[cnpj] = max(prev, cheio)

        # salva resumo do mês
        key = fmt_month(month)
        resumo_mensal[key] = {
            "arquivo": name,
            "faixa": faixa_nome,
            "qualificadas": qtd_qual,
            "deveria_receber": total_cheio,
            "ja_pago_ref": total_japago_ref,
            "receber_mes": total_receber,
        }

        month_rows.append([key, name, faixa_nome, qtd_qual, total_cheio, total_japago_ref, total_receber])

    safe_json_save(HIST_PAGO_POR_CNPJ, paid_max)
    safe_json_save(HIST_RESUMO_MENSAL, resumo_mensal)

    out = pd.DataFrame(
        month_rows,
        columns=[
            "Mês",
            "Arquivo",
            "Faixa",
            "Qualificadas",
            "Deveria receber (cheio)",
            "Já pago (referência)",
            "A receber no mês",
        ],
    )
    return out


# =========================================================
# LOGIN SIMPLES
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


# =========================================================
# ESTILO (CORES / LOGO / LAYOUT)
# =========================================================
def apply_theme():
    st.markdown(
        """
        <style>
            /* Sidebar escura (combina com a logo) */
            section[data-testid="stSidebar"]{
                background: #0f1b3a;
            }
            section[data-testid="stSidebar"] * {
                color: #ffffff !important;
            }

            /* Cards mais “corporate” */
            div[data-testid="stMetric"]{
                background: #ffffff;
                border: 1px solid #e9eef7;
                border-radius: 14px;
                padding: 12px 14px;
                box-shadow: 0 2px 10px rgba(15,27,58,0.05);
            }

            /* Títulos */
            h1, h2, h3 {
                color: #0f1b3a;
            }

            /* Barras */
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
    logo_path = os.path.join(here, "LOGO CORRETA.png")  # precisa estar na RAIZ do repositório
    c1, c2 = st.columns([1, 4], vertical_alignment="center")

    with c1:
        if os.path.exists(logo_path):
            st.image(logo_path, width=140)
        else:
            st.info("Logo não encontrada: coloque o arquivo 'LOGO CORRETA.png' na mesma pasta do app.py.")

    with c2:
        st.markdown(
            """
            <div style="line-height:1.1">
              <h1 style="margin-bottom:6px;">Painel de controle Assis e Mollerke parceiro Banco C6</h1>
              <div style="color:#5b6b8c;font-weight:600;">Visão Cliente + Leads + Remuneração incremental (Nov/25 em diante)</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# =========================================================
# DATAFRAMES COM ESTILO (SEM HTML VISÍVEL)
# =========================================================
def style_conversao_table(df: pd.DataFrame) -> "pd.io.formats.style.Styler":
    # precisa existir coluna numérica pra cor
    if "Percentual_num" not in df.columns:
        df["Percentual_num"] = 0.0

    def row_style(row):
        v = float(row.get("Percentual_num", 0.0))
        if v >= ALVO_CONVERSAO:
            return ["background-color: rgba(0,122,255,0.10); color: #0f1b3a; font-weight: 600;"] * len(row)
        else:
            return ["background-color: rgba(255,59,48,0.10); color: #0f1b3a; font-weight: 600;"] * len(row)

    sty = df.style.apply(row_style, axis=1)
    return sty


def hide_index_df(df: pd.DataFrame) -> pd.DataFrame:
    return df.reset_index(drop=True)


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
# IMPORTAÇÃO
# -------------------------------
st.subheader("Importação do dia")

colA, colB = st.columns(2)
with colA:
    up_c6 = st.file_uploader("Planilha C6 (Visão Cliente) — diária (.xlsx)", type=["xlsx"], key="c6")
with colB:
    up_leads = st.file_uploader("Planilha Leads — diária (.xlsx)", type=["xlsx"], key="leads")

st.subheader("Importação mensal (Remuneração incremental)")
up_monthly = st.file_uploader(
    "Envie 1 ou mais arquivos mensais (Nov/25 em diante) — pode enviar vários de uma vez",
    type=["xlsx"],
    accept_multiple_files=True,
    key="monthly",
)

# -------------------------------
# PROCESSAMENTO: C6 (DIÁRIO)
# -------------------------------
daily_ready = False
leads_ready = False

df_c6 = None
df_leads = None

if up_c6:
    b = up_c6.getvalue()
    df_c6 = read_excel_any(b)

    # normalizações importantes
    # CNPJ
    if COL_CNPJ not in df_c6.columns:
        cand = [c for c in df_c6.columns if "CNPJ" in str(c).upper()]
        df_c6[COL_CNPJ] = df_c6[cand[0]] if cand else ""

    df_c6[COL_CNPJ] = normalize_str(df_c6[COL_CNPJ]).str.replace(r"\D", "", regex=True)

    # datas
    if COL_ABERTURA not in df_c6.columns:
        df_c6[COL_ABERTURA] = pd.NA
    if COL_FUNDACAO not in df_c6.columns:
        df_c6[COL_FUNDACAO] = pd.NA

    df_c6[COL_ABERTURA] = to_date_series(df_c6[COL_ABERTURA])
    df_c6[COL_FUNDACAO] = to_date_series(df_c6[COL_FUNDACAO])

    # valores
    if COL_SALDO not in df_c6.columns:
        df_c6[COL_SALDO] = 0.0
    df_c6[COL_SALDO] = pd.to_numeric(df_c6[COL_SALDO], errors="coerce").fillna(0.0)

    # strings
    df_c6[COL_PIX] = normalize_str(df_c6.get(COL_PIX, pd.Series([""] * len(df_c6))))
    df_c6[COL_STATUS] = normalize_str(df_c6.get(COL_STATUS, pd.Series([""] * len(df_c6))))
    df_c6[COL_DOMICILIO] = normalize_str(df_c6.get(COL_DOMICILIO, pd.Series([""] * len(df_c6))))
    df_c6[COL_CRIT] = normalize_str(df_c6.get(COL_CRIT, pd.Series([""] * len(df_c6))))
    df_c6[COL_BY] = df_c6.get(COL_BY, pd.Series([""] * len(df_c6)))

    # métricas diárias
    por_dia_abert = (
        pd.Series(df_c6[COL_ABERTURA]).dropna().value_counts().sort_index()
        .rename_axis("Data")
        .reset_index(name="Quantidade")
    )

    # salva histórico diário (memória)
    # converte para dd/mm/aaaa
    por_dia_abert["Data"] = por_dia_abert["Data"].apply(lambda d: fmt_date(d))
    upsert_daily_hist(HIST_OPEN_DAILY, "Data", por_dia_abert)

    daily_ready = True

if up_leads:
    b = up_leads.getvalue()
    df_leads = read_excel_any(b)

    # A coluna M (no seu excel) precisa virar DATA_CADASTRO.
    # Se já existir DATA_CADASTRO ótimo. Se não existir, tentamos pegar pela posição.
    if COL_LEADS_DATA not in df_leads.columns:
        # tenta achar algo parecido
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

    por_dia_cad = (
        pd.Series(df_leads[COL_LEADS_DATA]).dropna().value_counts().sort_index()
        .rename_axis("Data")
        .reset_index(name="Quantidade")
    )
    por_dia_cad["Data"] = por_dia_cad["Data"].apply(lambda d: fmt_date(d))
    upsert_daily_hist(HIST_LEADS_DAILY, "Data", por_dia_cad)

    leads_ready = True

# -------------------------------
# RESUMO EXECUTIVO DO DIA (C6)
# -------------------------------
if daily_ready:
    # resumo do arquivo diário (apenas do que veio no arquivo)
    qtd_abert_arquivo = int(pd.Series(df_c6[COL_ABERTURA]).dropna().shape[0])
    saldo_total = float(df_c6[COL_SALDO].sum())
    pix_com, pix_sem, pix_por_chave = pix_summary(df_c6)
    domicilio_c6 = int(df_c6[COL_DOMICILIO].apply(contains_c6).sum())

    # qualificação (nível vencedor por linha)
    df_c6["_nivel"] = parse_level(df_c6)
    qualificadas = int((df_c6["_nivel"] >= 1).sum())

    st.subheader("Resumo executivo (dia)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Contas abertas (arquivo)", br_int(qtd_abert_arquivo))
    c2.metric("Saldo total", br_money(saldo_total))
    c3.metric("Clientes com Pix", br_int(pix_com))
    c4.metric("Clientes sem Pix", br_int(pix_sem))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Domicílio C6", br_int(domicilio_c6))
    c6.metric("Contas qualificadas (arquivo)", br_int(qualificadas))
    c7.metric("Arquivo do dia", up_c6.name)
    c8.metric("Mês detectado", fmt_month(detect_month_from_file(df_c6) or dt.date.today()))

    st.divider()

# -------------------------------
# CONVERSÃO: ABERTAS / CADASTRADAS (HISTÓRICO)
# -------------------------------
st.subheader("Conversão (Abertas ÷ Cadastradas)")

hist_open = hist_to_df(HIST_OPEN_DAILY, "Abertas")
hist_leads = hist_to_df(HIST_LEADS_DAILY, "Cadastradas")

if hist_open.empty or hist_leads.empty:
    st.info("Para ver a conversão, envie a planilha diária do C6 e a planilha diária de Leads (ao menos 1 vez).")
else:
    # junta por data
    base = pd.merge(hist_leads, hist_open, on="Data", how="outer").fillna(0)
    base["Cadastradas"] = base["Cadastradas"].astype(int)
    base["Abertas"] = base["Abertas"].astype(int)

    # percent = abertas / cadastradas
    base["Percentual_num"] = base.apply(
        lambda r: (r["Abertas"] / r["Cadastradas"]) if r["Cadastradas"] > 0 else 0.0,
        axis=1
    )
    base["% Abertas/Cadastradas"] = base["Percentual_num"].map(lambda x: f"{x*100:.1f}%".replace(".", ","))

    # indicador (badge)
    base["Indicador"] = base["Percentual_num"].map(
        lambda x: "Dentro do alvo" if x >= ALVO_CONVERSAO else "Abaixo do alvo"
    )

    # filtro por mês
    base["Mes_ref"] = base["Data"].map(lambda d: dt.date(d.year, d.month, 1))
    meses = sorted(base["Mes_ref"].unique())
    meses_lbl = [fmt_month(m) for m in meses]

    colf1, colf2 = st.columns([1, 2])
    with colf1:
        mes_sel_lbl = st.selectbox("Selecione o mês", meses_lbl, index=len(meses_lbl)-1)
    mes_sel = meses[meses_lbl.index(mes_sel_lbl)]

    mes_df = base[base["Mes_ref"] == mes_sel].copy()

    # resumo do mês
    total_ab_mes = int(mes_df["Abertas"].sum())
    total_cad_mes = int(mes_df["Cadastradas"].sum())
    perc_mes = (total_ab_mes / total_cad_mes) if total_cad_mes > 0 else 0.0

    # card do mês com cor
    if perc_mes >= ALVO_CONVERSAO:
        st.markdown(f"<div class='am-badge-ok'>Conversão do mês: {perc_mes*100:.1f}%</div>", unsafe_allow_html=True)
    else:
        st.markdown(f"<div class='am-badge-bad'>Conversão do mês: {perc_mes*100:.1f}%</div>", unsafe_allow_html=True)

    cA, cB, cC = st.columns(3)
    cA.metric("Abertas no mês", br_int(total_ab_mes))
    cB.metric("Cadastradas no mês", br_int(total_cad_mes))
    cC.metric("Mês", mes_sel_lbl)

    # tabela diária estilizada (sem HTML)
    show = mes_df[["Data", "Cadastradas", "Abertas", "% Abertas/Cadastradas", "Indicador", "Percentual_num"]].copy()
    show["Data"] = show["Data"].apply(fmt_date)

    st.caption("Tabela diária do mês selecionado (cor: azul ≥ 20% | vermelho < 20%)")
    st.dataframe(
        style_conversao_table(show).hide(axis="index").format(
            subset=["Cadastradas", "Abertas"],
            formatter="{:,.0f}".format
        ).format(
            subset=["Percentual_num"],
            formatter="{:.4f}".format
        ),
        use_container_width=True
    )

    # gráfico mais bonito
    chart = mes_df.copy()
    chart["Dia"] = chart["Data"].apply(lambda d: d.day)
    chart = chart.sort_values("Dia")
    st.line_chart(chart.set_index("Dia")[["Cadastradas", "Abertas"]])

st.divider()

# -------------------------------
# RELATÓRIOS DIÁRIOS (C6) – ABERTURAS / FUNDAÇÃO / PIX / QUALIFICAÇÃO
# -------------------------------
st.subheader("Relatórios (diário)")

if not daily_ready:
    st.info("Envie a planilha diária do C6 para liberar os relatórios diários.")
else:
    tabs = st.tabs(["Aberturas", "Fundações (por dia)", "Pix + Status", "Qualificação (níveis e critérios)"])

    # Aberturas
    with tabs[0]:
        por_dia = (
            pd.Series(df_c6[COL_ABERTURA]).dropna().value_counts().sort_index()
            .rename_axis("Dia")
            .reset_index(name="Contas abertas")
        )
        por_dia["Dia"] = por_dia["Dia"].apply(fmt_date)

        # por mês
        dts = pd.to_datetime(df_c6[COL_ABERTURA], errors="coerce")
        por_mes = (
            dts.dropna().dt.to_period("M").astype(str)
            .value_counts().sort_index()
            .rename_axis("Mês")
            .reset_index(name="Contas abertas")
        )
        # formata mês para mm/aaaa
        por_mes["Mês"] = por_mes["Mês"].apply(lambda x: f"{x[5:7]}/{x[0:4]}" if isinstance(x, str) and "-" in x else x)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("#### Contas abertas por dia")
            st.bar_chart(por_dia.set_index("Dia")["Contas abertas"])
        with c2:
            st.markdown("#### Contas abertas por mês")
            st.bar_chart(por_mes.set_index("Mês")["Contas abertas"])

        st.markdown("#### Tabela (dia)")
        st.dataframe(hide_index_df(por_dia), use_container_width=True)

        st.markdown("#### Tabela (mês)")
        st.dataframe(hide_index_df(por_mes), use_container_width=True)

    # Fundações (por dia) – manter como você gostou, mas com mês/ano
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
            pivot["Dia"] = pivot["Dia"].apply(fmt_date)

            # filtro por dia (clicável via select)
            dias = sorted(temp[COL_ABERTURA].unique())
            dias_lbl = [fmt_date(d) for d in dias]

            colx, coly = st.columns([1, 2])
            with colx:
                dia_sel_lbl = st.selectbox("Selecione o dia de abertura", dias_lbl, index=len(dias_lbl)-1)
            dia_sel = dias[dias_lbl.index(dia_sel_lbl)]

            dia_df = pivot[pivot["Dia"] == fmt_date(dia_sel)].copy()

            total_dia = int(dia_df["Quantidade"].sum())
            st.markdown(f"**No dia {dia_sel_lbl} foram abertas {br_int(total_dia)} empresas.**")

            st.dataframe(hide_index_df(dia_df), use_container_width=True)
            st.bar_chart(dia_df.set_index("Mês fundação")["Quantidade"])

    # Pix + Status
    with tabs[2]:
        st.markdown("#### Pix")
        pix_com, pix_sem, pix_por_chave = pix_summary(df_c6)
        a, b = st.columns(2)
        a.metric("Clientes com Pix", br_int(pix_com))
        b.metric("Clientes sem Pix", br_int(pix_sem))

        st.dataframe(hide_index_df(pix_por_chave), use_container_width=True)

        st.markdown("#### Status")
        status = (
            df_c6[COL_STATUS]
            .replace("", "SEM STATUS")
            .fillna("SEM STATUS")
            .value_counts()
            .rename_axis("Status")
            .reset_index(name="Quantidade")
        )
        st.dataframe(hide_index_df(status), use_container_width=True)
        st.bar_chart(status.set_index("Status")["Quantidade"])

    # Qualificação
    with tabs[3]:
        st.markdown("#### Qualificação (nível vencedor e critério vencedor)")

        dfq = df_c6.copy()
        dfq["_nivel"] = parse_level(dfq)
        dfq["_qualificada"] = dfq["_nivel"].apply(lambda x: "Sim" if x >= 1 else "Não")

        # critério vencedor (só pra visualização)
        def criterio_vencedor(txt: str) -> str:
            if not isinstance(txt, str) or not txt:
                return ""
            parts = [p.strip() for p in txt.split("|")]
            # pega o que tiver o maior número
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

        # Tabela limpa
        cols_show = []
        if COL_CNPJ in dfq.columns:
            cols_show.append(COL_CNPJ)
        cols_show += [COL_ABERTURA, "_qualificada", "_nivel", "_criterio_vencedor"]

        show = dfq[cols_show].copy()
        show.rename(columns={
            COL_CNPJ: "CNPJ",
            COL_ABERTURA: "Data de abertura",
            "_qualificada": "Qualificada",
            "_nivel": "Nível",
            "_criterio_vencedor": "Critério vencedor"
        }, inplace=True)

        show["Data de abertura"] = show["Data de abertura"].apply(fmt_date)

        # mantém apenas qualificadas pra ficar “executivo”
        show_q = show[show["Qualificada"] == "Sim"].copy()

        st.dataframe(hide_index_df(show_q), use_container_width=True)
        st.bar_chart(pd.DataFrame({
            "Quantidade": [n1, n2, n3, n4]
        }, index=["Nível 1", "Nível 2", "Nível 3", "Nível 4"]))

st.divider()

# -------------------------------
# REMUNERAÇÃO MENSAL INCREMENTAL
# -------------------------------
st.subheader("Remuneração mensal (incremental)")

if up_monthly and len(up_monthly) > 0:
    files = [(f.name, f.getvalue()) for f in up_monthly]
    out = compute_monthly_incremental(files)

    if out.empty:
        st.warning("Não consegui detectar mês/estrutura nos arquivos mensais enviados. Verifique se contêm DT_CONTA_CRIADA ou DATA_BASE.")
    else:
        # formata valores
        view = out.copy()
        view["Qualificadas"] = view["Qualificadas"].apply(lambda x: br_int(int(x)))
        view["Deveria receber (cheio)"] = view["Deveria receber (cheio)"].apply(lambda x: br_money(float(x)))
        view["Já pago (referência)"] = view["Já pago (referência)"].apply(lambda x: br_money(float(x)))
        view["A receber no mês"] = view["A receber no mês"].apply(lambda x: br_money(float(x)))

        st.markdown("#### Resumo por mês (do que você enviou agora)")
        st.dataframe(hide_index_df(view), use_container_width=True)

# mostra o resumo salvo na memória (sempre)
saved = safe_json_load(HIST_RESUMO_MENSAL, default={})
if saved:
    rows = []
    for mes, info in saved.items():
        rows.append([
            mes,
            info.get("arquivo", ""),
            info.get("faixa", ""),
            int(info.get("qualificadas", 0)),
            float(info.get("deveria_receber", 0.0)),
            float(info.get("ja_pago_ref", 0.0)),
            float(info.get("receber_mes", 0.0)),
        ])

    dfm = pd.DataFrame(rows, columns=[
        "Mês", "Arquivo", "Faixa", "Qualificadas", "Deveria receber (cheio)", "Já pago (referência)", "A receber no mês"
    ])

    # ordena por mês (mm/aaaa)
    def month_key(s):
        try:
            mm, aa = s.split("/")
            return int(aa)*100 + int(mm)
        except:
            return 0

    dfm = dfm.sort_values("Mês", key=lambda col: col.map(month_key))

    st.markdown("#### Histórico mensal consolidado (salvo na memória)")
    dfm_view = dfm.copy()
    dfm_view["Qualificadas"] = dfm_view["Qualificadas"].apply(lambda x: br_int(int(x)))
    dfm_view["Deveria receber (cheio)"] = dfm_view["Deveria receber (cheio)"].apply(lambda x: br_money(float(x)))
    dfm_view["Já pago (referência)"] = dfm_view["Já pago (referência)"].apply(lambda x: br_money(float(x)))
    dfm_view["A receber no mês"] = dfm_view["A receber no mês"].apply(lambda x: br_money(float(x)))

    st.dataframe(hide_index_df(dfm_view), use_container_width=True)

    # cartões do último mês
    last = dfm.iloc[-1]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Mês atual (histórico)", str(last["Mês"]))
    c2.metric("Qualificadas (mês)", br_int(int(last["Qualificadas"])))
    c3.metric("Receita cheia (mês)", br_money(float(last["Deveria receber (cheio)"])))
    c4.metric("A receber (mês)", br_money(float(last["A receber no mês"])))
else:
    st.info("Envie os arquivos mensais (Nov/25 em diante) para o sistema montar o histórico incremental.")
