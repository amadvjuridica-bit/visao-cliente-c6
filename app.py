import os
import io
import re
import json
import hashlib
import datetime as dt
from typing import Optional, Dict, Tuple

import pandas as pd
import streamlit as st


# =========================
# IDENTIDADE (marca)
# =========================
APP_TITLE = "Assis & Mollerke"
APP_SUBTITLE = "Painel executivo — Visão Cliente C6 + Leads"
LOGO_PATH = "LOGO CORRETA.png"  # precisa estar na raiz do repo (como você mostrou no GitHub)


# =========================
# LOGIN (simples)
# =========================
LOGIN_USER = "admin"
LOGIN_PASS = "123456"  # você pode trocar


# =========================
# COLUNAS (Visão Cliente)
# =========================
COL_T  = "DT_CONTA_CRIADA"                 # data de abertura/conta criada
COL_P  = "DT_FUNDACAO_EMPRESA"             # data fundação
COL_X  = "CHAVES_PIX_FORTE"                # tipo de chave pix
COL_Y  = "VL_SALDO_MEDIO_MENSALIZADO"      # saldo
COL_V  = "STATUS_CC"                       # status
COL_AQ = "BANCO_DOMICILIO"                 # banco domicílio
COL_BY = "FL_QUALIFICADO_COMISS"           # qualificada (0/1)
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"    # critérios (texto)

# Pagamentos por nível (maior valor)
PAYOUT = {1: 210, 2: 345, 3: 600, 4: 810}


# =========================
# COLUNAS (Leads)
# =========================
LEADS_CADASTRO = "DATA_HORA_CADASTRO"      # (coluna M do seu contexto)
LEADS_ABERTA   = "DT_CONTA_ABERTA"
LEADS_FLAG     = "FL_CONTA_ABERTA"


# =========================
# PERSISTÊNCIA (histórico)
# =========================
DATA_DIR = "data_uploads"
os.makedirs(DATA_DIR, exist_ok=True)

HIST_OPEN_DAILY   = os.path.join(DATA_DIR, "hist_contas_abertas_diario.csv")
HIST_OPEN_MONTHLY = os.path.join(DATA_DIR, "hist_contas_abertas_mensal.csv")

HIST_LEADS_DAILY   = os.path.join(DATA_DIR, "hist_leads_diario.csv")
HIST_LEADS_MONTHLY = os.path.join(DATA_DIR, "hist_leads_mensal.csv")

LAST_SNAPSHOT = os.path.join(DATA_DIR, "latest_snapshot.json")
PREV_SNAPSHOT = os.path.join(DATA_DIR, "prev_snapshot.json")

START_DATE = dt.date(2026, 1, 1)  # guardar histórico a partir daqui (como você pediu)


# =========================
# ESTILO (visual)
# =========================
def inject_css():
    st.markdown(
        """
        <style>
            /* fundo e tipografia */
            .stApp { background: #ffffff; }
            h1, h2, h3, h4 { color: #1b2440; }
            p, div, span, label { color: #2b2b2b; }

            /* sidebar */
            section[data-testid="stSidebar"] {
                background: linear-gradient(180deg, #1b2440 0%, #11182d 100%);
            }
            section[data-testid="stSidebar"] * {
                color: #ffffff !important;
            }

            /* cards */
            .am-card {
                border: 1px solid #eef0f6;
                border-radius: 14px;
                padding: 14px 16px;
                background: #ffffff;
                box-shadow: 0 4px 18px rgba(17, 24, 45, 0.06);
            }
            .am-card-title {
                font-size: 13px;
                opacity: .75;
                margin-bottom: 4px;
            }
            .am-card-value {
                font-size: 26px;
                font-weight: 700;
                color: #1b2440;
                margin: 0;
            }
            .am-card-sub {
                font-size: 12px;
                opacity: .70;
                margin-top: 6px;
            }

            /* separadores */
            .am-divider {
                height: 1px;
                background: #eef0f6;
                margin: 16px 0;
            }
        </style>
        """,
        unsafe_allow_html=True
    )


def card(title: str, value: str, sub: str = ""):
    st.markdown(
        f"""
        <div class="am-card">
            <div class="am-card-title">{title}</div>
            <p class="am-card-value">{value}</p>
            <div class="am-card-sub">{sub}</div>
        </div>
        """,
        unsafe_allow_html=True
    )


# =========================
# UTILS
# =========================
def br_date(d: Optional[dt.date]) -> str:
    if not d or pd.isna(d):
        return ""
    if isinstance(d, dt.datetime):
        d = d.date()
    return d.strftime("%d/%m/%Y")


def br_month(d: Optional[dt.date]) -> str:
    if not d or pd.isna(d):
        return ""
    if isinstance(d, dt.datetime):
        d = d.date()
    return d.strftime("%m/%Y")


def br_money(v: float) -> str:
    # formato "R$ 1.234.567,89"
    s = f"{v:,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def safe_read_excel(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")


def safe_to_date(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.date


def normalize_str(series: pd.Series) -> pd.Series:
    return series.astype("string").fillna("").str.strip()


def contains_c6(val: str) -> bool:
    return "c6" in str(val).lower()


def ensure_columns(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    return df


# =========================
# REGRAS PRINCIPAIS
# =========================
def parse_max_level(criteria_text: str) -> int:
    """
    Regra B (sua):
    - pega o MAIOR valor entre CASH IN / DOMICILIO / SALDO MEDIO / SPENDING / CONTA GLOBAL
    - se o maior for 4, nível=4 (vitorioso 4)
    """
    nums = re.findall(r":\s*(\d+)", str(criteria_text))
    if not nums:
        return 0
    vals = [int(x) for x in nums]
    return max(vals) if vals else 0


def compute_qualificadas(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, int]:
    """
    - Qualificada = BY == 1
    - Nível = maior valor do texto de critérios (1..4)
    - Receita = PAYOUT[nivel]
    """
    dfq = df[df[COL_BY] == 1].copy()

    dfq["Nivel"] = dfq[COL_CRIT].apply(parse_max_level).astype(int)
    dfq["Receita"] = dfq["Nivel"].map(PAYOUT).fillna(0).astype(int)

    # distribuição e total
    dist = (
        dfq["Nivel"]
        .value_counts()
        .sort_index()
        .rename_axis("Nível")
        .reset_index(name="Quantidade")
    )

    total_receita = int(dfq["Receita"].sum())
    return dfq, dist, total_receita


# =========================
# VISÃO CLIENTE — métricas
# =========================
def contas_criadas(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, int]:
    s = safe_to_date(df[COL_T])
    s = s.dropna()
    s = s[s >= START_DATE]

    por_dia = (
        s.value_counts()
        .sort_index()
        .rename_axis("Dia")
        .reset_index(name="Contas abertas")
    )

    por_mes = (
        pd.to_datetime(s)
        .dt.to_period("M")
        .astype(str)
        .value_counts()
        .sort_index()
        .rename_axis("Mês")
        .reset_index(name="Contas abertas")
    )

    total = int(s.shape[0])
    return por_dia, por_mes, total


def fundacoes_por_dia_mes(df: pd.DataFrame) -> pd.DataFrame:
    t = safe_to_date(df[COL_T])
    p = safe_to_date(df[COL_P])

    aux = pd.DataFrame({"Dia": t, "Fundacao": p}).dropna()
    aux = aux[aux["Dia"] >= START_DATE]

    aux["Fundação (mês/ano)"] = aux["Fundacao"].apply(br_month)

    out = (
        aux.groupby(["Dia", "Fundação (mês/ano)"])
        .size()
        .reset_index(name="Quantidade")
        .sort_values(["Dia", "Fundação (mês/ano)"])
    )
    return out


def pix_stats(df: pd.DataFrame) -> Tuple[int, int, pd.DataFrame]:
    s = normalize_str(df[COL_X]).str.upper()
    s = s.str.replace("'", "", regex=False)

    sem_pix_tokens = {"", "-", "NAN", "NONE", "SEM", "SEM PIX"}
    has_pix = ~s.isin(sem_pix_tokens)

    qtd_com = int(has_pix.sum())
    qtd_sem = int((~has_pix).sum())

    por_tipo = (
        s[has_pix]
        .value_counts()
        .rename_axis("Tipo de chave Pix")
        .reset_index(name="Quantidade")
    )
    return qtd_com, qtd_sem, por_tipo


def saldo_total(df: pd.DataFrame) -> float:
    v = pd.to_numeric(df[COL_Y], errors="coerce").fillna(0.0)
    return float(v.sum())


def status_counts(df: pd.DataFrame) -> pd.DataFrame:
    s = normalize_str(df[COL_V])
    s = s.replace("", "SEM STATUS")
    out = (
        s.value_counts()
        .rename_axis("Status")
        .reset_index(name="Quantidade")
    )
    return out


def domicilio_c6_count(df: pd.DataFrame) -> int:
    s = normalize_str(df[COL_AQ])
    return int(s.apply(contains_c6).sum())


# =========================
# LEADS — cadastro x abertura
# =========================
def leads_kpis(df_leads: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Percentual = (abertas / cadastradas) * 100
    - diário
    - mensal
    """
    df_leads = ensure_columns(df_leads, [LEADS_CADASTRO, LEADS_ABERTA, LEADS_FLAG])

    cad = pd.to_datetime(df_leads[LEADS_CADASTRO], errors="coerce").dt.date
    aberta = pd.to_datetime(df_leads[LEADS_ABERTA], errors="coerce").dt.date

    # cadastradas por dia (cadastro)
    cad_s = pd.Series(cad).dropna()
    cad_s = cad_s[cad_s >= START_DATE]
    cad_daily = cad_s.value_counts().sort_index()

    # abertas por dia (data de abertura)
    aberta_s = pd.Series(aberta).dropna()
    aberta_s = aberta_s[aberta_s >= START_DATE]
    aberta_daily = aberta_s.value_counts().sort_index()

    # junta num dataframe diário
    idx = cad_daily.index.union(aberta_daily.index)
    daily = pd.DataFrame({
        "Dia": idx,
        "Cadastradas": [int(cad_daily.get(d, 0)) for d in idx],
        "Abertas": [int(aberta_daily.get(d, 0)) for d in idx],
    })

    daily["Percentual_num"] = daily.apply(
        lambda r: (r["Abertas"] / r["Cadastradas"] * 100) if r["Cadastradas"] > 0 else 0.0,
        axis=1
    )
    daily["Percentual"] = daily["Percentual_num"].map(lambda x: f"{x:.2f}%".replace(".", ","))

    # mensal
    daily["Mes"] = daily["Dia"].map(lambda d: f"{d.year:04d}-{d.month:02d}")
    monthly = (
        daily.groupby("Mes")[["Cadastradas", "Abertas"]]
        .sum()
        .reset_index()
    )
    monthly["Percentual_num"] = monthly.apply(
        lambda r: (r["Abertas"] / r["Cadastradas"] * 100) if r["Cadastradas"] > 0 else 0.0,
        axis=1
    )
    monthly["Percentual"] = monthly["Percentual_num"].map(lambda x: f"{x:.2f}%".replace(".", ","))

    # formata datas
    daily["Dia"] = daily["Dia"].map(br_date)

    # mensal para "MM/AAAA"
    def mes_to_br(m: str) -> str:
        # "YYYY-MM"
        y, mm = m.split("-")
        return f"{mm}/{y}"
    monthly["Mês"] = monthly["Mes"].map(mes_to_br)
    monthly = monthly.drop(columns=["Mes"])

    return daily, monthly


def style_percentual(df_show: pd.DataFrame, threshold: float = 20.0) -> "pd.io.formats.style.Styler":
    """
    Pinta linha (azul) se >= 20% e (vermelho) se < 20%.
    Sem texto explicando a regra na tela.
    """
    def row_style(row):
        v = float(row.get("Percentual_num", 0.0))
        if v >= threshold:
            return ["background-color: rgba(33, 150, 243, 0.10)"] * len(row)
        return ["background-color: rgba(244, 67, 54, 0.10)"] * len(row)

    return df_show.style.apply(row_style, axis=1)


# =========================
# HISTÓRICO (upsert)
# =========================
def upsert_hist(path: str, key_col: str, new_df: pd.DataFrame) -> pd.DataFrame:
    """
    Salva histórico somando/atualizando por chave (ex.: Dia ou Mês).
    Se já existe a chave, substitui pelos valores do dia (último upload).
    """
    if os.path.exists(path):
        base = pd.read_csv(path)
    else:
        base = pd.DataFrame(columns=new_df.columns)

    # garante colunas
    for c in new_df.columns:
        if c not in base.columns:
            base[c] = pd.NA

    base = base.copy()
    base[key_col] = base[key_col].astype(str)
    new_df = new_df.copy()
    new_df[key_col] = new_df[key_col].astype(str)

    # remove chaves existentes e concatena
    base = base[~base[key_col].isin(set(new_df[key_col].tolist()))]
    out = pd.concat([base, new_df], ignore_index=True)

    # ordena pela coluna chave (como string dd/mm/yyyy pode ordenar errado; então converte se for Dia)
    if key_col.lower().startswith("dia"):
        parsed = pd.to_datetime(out[key_col], format="%d/%m/%Y", errors="coerce")
        out = out.assign(_sort=parsed).sort_values("_sort").drop(columns=["_sort"])
    else:
        # Mês "MM/AAAA"
        parsed = pd.to_datetime("01/" + out[key_col], format="%d/%m/%Y", errors="coerce")
        out = out.assign(_sort=parsed).sort_values("_sort").drop(columns=["_sort"])

    out.to_csv(path, index=False)
    return out


def save_snapshot(metrics: Dict, tag: str, file_hash: str):
    snap = {
        "saved_at": dt.datetime.now().isoformat(),
        "tag": tag,
        "file_hash": file_hash,
        "metrics": metrics,
    }
    # shift last -> prev
    if os.path.exists(LAST_SNAPSHOT):
        with open(LAST_SNAPSHOT, "r", encoding="utf-8") as f:
            old = f.read()
        with open(PREV_SNAPSHOT, "w", encoding="utf-8") as f:
            f.write(old)

    with open(LAST_SNAPSHOT, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)


def load_prev_latest() -> Tuple[Optional[dict], Optional[dict]]:
    prev = latest = None
    if os.path.exists(PREV_SNAPSHOT):
        with open(PREV_SNAPSHOT, "r", encoding="utf-8") as f:
            prev = json.load(f)
    if os.path.exists(LAST_SNAPSHOT):
        with open(LAST_SNAPSHOT, "r", encoding="utf-8") as f:
            latest = json.load(f)
    return prev, latest


def diff(a, b):
    try:
        if a is None or b is None:
            return None
        return a - b
    except Exception:
        return None


# =========================
# LOGIN UI
# =========================
def login_gate() -> bool:
    st.sidebar.markdown(f"## Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")

    if st.sidebar.button("Entrar"):
        st.session_state["logged"] = bool(u == LOGIN_USER and p == LOGIN_PASS)
        if not st.session_state["logged"]:
            st.sidebar.error("Usuário ou senha inválidos.")

    return st.session_state.get("logged", False)


# =========================
# APP
# =========================
st.set_page_config(page_title=APP_TITLE, layout="wide")
inject_css()

if not login_gate():
    st.stop()

# Cabeçalho com logo
top_left, top_right = st.columns([1, 6])
with top_left:
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, width=120)
with top_right:
    st.markdown(f"# {APP_TITLE}")
    st.caption(APP_SUBTITLE)

st.markdown('<div class="am-divider"></div>', unsafe_allow_html=True)

# Uploads
u1, u2 = st.columns(2)
with u1:
    up_clientes = st.file_uploader("📌 Importar Visão Cliente (xlsx)", type=["xlsx"], key="clientes")
with u2:
    up_leads = st.file_uploader("📌 Importar Leads (xlsx)", type=["xlsx"], key="leads")

prev_snap, latest_snap = load_prev_latest()

# =========================
# PROCESSA UPLOADS
# =========================
df_clientes = None
df_leads = None

if up_clientes:
    b = up_clientes.getvalue()
    df_clientes = safe_read_excel(b)
    file_hash = sha256_bytes(b)

    # garante colunas
    df_clientes = ensure_columns(df_clientes, [COL_T, COL_P, COL_X, COL_Y, COL_V, COL_AQ, COL_BY, COL_CRIT])

    # tipagens
    df_clientes[COL_T] = safe_to_date(df_clientes[COL_T])
    df_clientes[COL_P] = safe_to_date(df_clientes[COL_P])
    df_clientes[COL_X] = normalize_str(df_clientes[COL_X])
    df_clientes[COL_V] = normalize_str(df_clientes[COL_V])
    df_clientes[COL_AQ] = normalize_str(df_clientes[COL_AQ])
    df_clientes[COL_BY] = pd.to_numeric(df_clientes[COL_BY], errors="coerce").fillna(0).astype(int)
    df_clientes[COL_Y] = pd.to_numeric(df_clientes[COL_Y], errors="coerce").fillna(0.0)
    df_clientes[COL_CRIT] = df_clientes[COL_CRIT].astype("string").fillna("")

    # métricas visão cliente
    open_daily, open_monthly, open_total = contas_criadas(df_clientes)
    fund_by_day_month = fundacoes_por_dia_mes(df_clientes)
    com_pix, sem_pix, pix_by_type = pix_stats(df_clientes)
    saldo = saldo_total(df_clientes)
    st_status = status_counts(df_clientes)
    c6 = domicilio_c6_count(df_clientes)
    dfq, nivel_dist, receita_total = compute_qualificadas(df_clientes)
    qtd_qualificadas = int(dfq.shape[0])

    # histórico (abertas)
    open_daily_show = open_daily.copy()
    open_daily_show["Dia"] = open_daily_show["Dia"].map(br_date)
    open_monthly_show = open_monthly.copy()
    # open_monthly tem "YYYY-MM"
    def yyyymm_to_br(s: str) -> str:
        y, m = s.split("-")
        return f"{m}/{y}"
    open_monthly_show["Mês"] = open_monthly_show["Mês"].map(yyyymm_to_br)

    upsert_hist(HIST_OPEN_DAILY, "Dia", open_daily_show)
    upsert_hist(HIST_OPEN_MONTHLY, "Mês", open_monthly_show)

    metrics = {
        "contas_abertas_total": open_total,
        "saldo_total": float(saldo),
        "clientes_com_pix": int(com_pix),
        "clientes_sem_pix": int(sem_pix),
        "domicilio_c6": int(c6),
        "qualificadas": int(qtd_qualificadas),
        "receita_estimada": int(receita_total),
        "arquivo_clientes": up_clientes.name,
        "hash_clientes": file_hash
    }
    save_snapshot(metrics=metrics, tag=up_clientes.name, file_hash=file_hash)
    prev_snap, latest_snap = load_prev_latest()

if up_leads:
    b2 = up_leads.getvalue()
    df_leads = safe_read_excel(b2)

    # kpis
    daily_leads, monthly_leads = leads_kpis(df_leads)

    # salva histórico
    daily_for_hist = daily_leads.drop(columns=["Percentual"], errors="ignore").copy()
    monthly_for_hist = monthly_leads.drop(columns=["Percentual"], errors="ignore").copy()

    # daily tem Percentual_num escondido, guardamos também (para pintar depois)
    upsert_hist(HIST_LEADS_DAILY, "Dia", daily_for_hist)
    upsert_hist(HIST_LEADS_MONTHLY, "Mês", monthly_for_hist)


# =========================
# RESUMO DO DIA (último snapshot)
# =========================
if latest_snap and latest_snap.get("metrics"):
    m = latest_snap["metrics"]

    st.markdown("## Resumo executivo")
    r1, r2, r3, r4 = st.columns(4)
    with r1:
        card("Contas abertas (no arquivo)", f"{m['contas_abertas_total']:,}".replace(",", "."))
    with r2:
        card("Saldo total", br_money(m["saldo_total"]))
    with r3:
        card("Clientes com Pix", f"{m['clientes_com_pix']:,}".replace(",", "."))
    with r4:
        card("Clientes sem Pix", f"{m['clientes_sem_pix']:,}".replace(",", "."))

    r5, r6, r7, r8 = st.columns(4)
    with r5:
        card("Domicílio C6", f"{m['domicilio_c6']:,}".replace(",", "."))
    with r6:
        card("Contas qualificadas", f"{m['qualificadas']:,}".replace(",", "."))
    with r7:
        card("Receita estimada", br_money(m["receita_estimada"]))
    with r8:
        card("Arquivo", m.get("arquivo_clientes", "-"))

    st.markdown('<div class="am-divider"></div>', unsafe_allow_html=True)

    st.markdown("## Variação vs. último arquivo")
    if prev_snap and prev_snap.get("metrics"):
        pm = prev_snap["metrics"]
        d1, d2, d3, d4 = st.columns(4)
        with d1:
            v = diff(m.get("contas_abertas_total"), pm.get("contas_abertas_total"))
            card("Δ Contas abertas", f"{int(v):+d}" if v is not None else "—")
        with d2:
            v = diff(m.get("saldo_total"), pm.get("saldo_total"))
            card("Δ Saldo total", br_money(v) if v is not None else "—")
        with d3:
            v = diff(m.get("qualificadas"), pm.get("qualificadas"))
            card("Δ Qualificadas", f"{int(v):+d}" if v is not None else "—")
        with d4:
            v = diff(m.get("receita_estimada"), pm.get("receita_estimada"))
            card("Δ Receita estimada", br_money(v) if v is not None else "—")
    else:
        st.info("Assim que você importar dois dias diferentes, o painel já compara automaticamente.")

    st.markdown('<div class="am-divider"></div>', unsafe_allow_html=True)
else:
    st.info("Importe o arquivo de Visão Cliente para gerar o resumo executivo.")


# =========================
# DETALHES (tabs)
# =========================
st.markdown("## Relatórios")

tabs = st.tabs([
    "Contas abertas (Histórico)",
    "Fundações (por dia)",
    "Pix & Status",
    "Qualificadas & Receita",
    "Cadastro x Abertura (Leads)"
])

# ---- TAB 1: Histórico de aberturas
with tabs[0]:
    st.markdown("### Evolução (a partir de 01/01/2026)")

    if os.path.exists(HIST_OPEN_DAILY):
        hist_daily = pd.read_csv(HIST_OPEN_DAILY)
        hist_month = pd.read_csv(HIST_OPEN_MONTHLY)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Diário**")
            st.dataframe(hist_daily, hide_index=True, use_container_width=True)
        with c2:
            st.markdown("**Mensal**")
            st.dataframe(hist_month, hide_index=True, use_container_width=True)
    else:
        st.info("Importe o arquivo Visão Cliente pelo menos uma vez para começar a gravar o histórico.")

# ---- TAB 2: Fundações por dia (drilldown)
with tabs[1]:
    st.markdown("### Fundações por dia (mês/ano)")

    if df_clientes is None:
        st.info("Importe o arquivo Visão Cliente para ver este relatório.")
    else:
        fund = fundacoes_por_dia_mes(df_clientes).copy()
        if fund.empty:
            st.warning("Não encontrei datas de abertura/fundação válidas no arquivo.")
        else:
            # seletor de dia (drill)
            dias = sorted(fund["Dia"].unique())
            dias_br = [br_date(d) for d in dias]
            sel = st.selectbox("Escolha o dia para detalhar", dias_br)

            # filtra
            sel_date = pd.to_datetime(sel, format="%d/%m/%Y").date()
            show = fund[fund["Dia"] == sel_date].copy()
            show = show.drop(columns=["Dia"])
            show = show.sort_values("Quantidade", ascending=False)

            # resumo
            total_dia = int(show["Quantidade"].sum())
            card("Aberturas no dia selecionado", f"{total_dia:,}".replace(",", "."), sub=f"Dia: {sel}")

            st.dataframe(show, hide_index=True, use_container_width=True)

# ---- TAB 3: Pix & Status
with tabs[2]:
    st.markdown("### Pix & Status")

    if df_clientes is None:
        st.info("Importe o arquivo Visão Cliente para ver este relatório.")
    else:
        com_pix, sem_pix, pix_by_type = pix_stats(df_clientes)
        st1, st2 = st.columns(2)
        with st1:
            card("Clientes com Pix", f"{com_pix:,}".replace(",", "."))
            st.dataframe(pix_by_type, hide_index=True, use_container_width=True)
        with st2:
            card("Clientes sem Pix", f"{sem_pix:,}".replace(",", "."))
            st.dataframe(status_counts(df_clientes), hide_index=True, use_container_width=True)

# ---- TAB 4: Qualificadas & Receita
with tabs[3]:
    st.markdown("### Qualificadas & Receita")

    if df_clientes is None:
        st.info("Importe o arquivo Visão Cliente para ver este relatório.")
    else:
        dfq, nivel_dist, receita_total = compute_qualificadas(df_clientes)

        a, b = st.columns([1, 2])
        with a:
            card("Total qualificadas", f"{dfq.shape[0]:,}".replace(",", "."))
            card("Receita estimada", br_money(receita_total))
        with b:
            st.markdown("**Distribuição por nível (maior critério)**")
            st.dataframe(nivel_dist, hide_index=True, use_container_width=True)

        st.markdown("#### Lista (para auditoria e entendimento)")
        cols_show = []
        for c in ["CD_CPF_CNPJ_CLIENTE", "NOME_CLIENTE", COL_T, COL_CRIT]:
            if c in dfq.columns:
                cols_show.append(c)

        audit = dfq[cols_show].copy()
        if COL_T in audit.columns:
            audit["Data de abertura"] = audit[COL_T].map(br_date)
            audit = audit.drop(columns=[COL_T])
        audit["Nível"] = dfq["Nivel"]
        audit["Receita"] = dfq["Receita"].map(lambda x: br_money(float(x)))

        # Renomeia colunas para ficar “bonito”
        rename_map = {
            "CD_CPF_CNPJ_CLIENTE": "CPF/CNPJ",
            "NOME_CLIENTE": "Cliente",
            COL_CRIT: "Critérios atingidos",
        }
        audit = audit.rename(columns=rename_map)

        st.dataframe(audit, hide_index=True, use_container_width=True)

# ---- TAB 5: Leads — Cadastro x Abertura
with tabs[4]:
    st.markdown("### Cadastro x Abertura (Leads)")

    if not os.path.exists(HIST_LEADS_DAILY):
        st.info("Importe a planilha de Leads pelo menos uma vez para iniciar o histórico.")
    else:
        # carrega histórico
        hist_d = pd.read_csv(HIST_LEADS_DAILY)
        hist_m = pd.read_csv(HIST_LEADS_MONTHLY)

        # reconstrói o percentual para exibição (e pinta)
        # diário
        aux_d = hist_d.copy()
        aux_d["Cadastradas"] = pd.to_numeric(aux_d.get("Cadastradas", 0), errors="coerce").fillna(0).astype(int)
        aux_d["Abertas"] = pd.to_numeric(aux_d.get("Abertas", 0), errors="coerce").fillna(0).astype(int)
        aux_d["Percentual_num"] = aux_d.apply(
            lambda r: (r["Abertas"] / r["Cadastradas"] * 100) if r["Cadastradas"] > 0 else 0.0,
            axis=1
        )
        aux_d["Percentual"] = aux_d["Percentual_num"].map(lambda x: f"{x:.2f}%".replace(".", ","))

        # mensal
        aux_m = hist_m.copy()
        aux_m["Cadastradas"] = pd.to_numeric(aux_m.get("Cadastradas", 0), errors="coerce").fillna(0).astype(int)
        aux_m["Abertas"] = pd.to_numeric(aux_m.get("Abertas", 0), errors="coerce").fillna(0).astype(int)
        aux_m["Percentual_num"] = aux_m.apply(
            lambda r: (r["Abertas"] / r["Cadastradas"] * 100) if r["Cadastradas"] > 0 else 0.0,
            axis=1
        )
        aux_m["Percentual"] = aux_m["Percentual_num"].map(lambda x: f"{x:.2f}%".replace(".", ","))

        # mostra kpis do mês corrente do histórico (última linha)
        if not aux_m.empty:
            last = aux_m.iloc[-1]
            k1, k2, k3 = st.columns(3)
            with k1:
                card("Mês (histórico)", str(last.get("Mês", "")))
            with k2:
                card("Cadastradas (mês)", f"{int(last.get('Cadastradas', 0)):,}".replace(",", "."))
            with k3:
                card("Abertas (mês)", f"{int(last.get('Abertas', 0)):,}".replace(",", "."))

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Diário**")
            show_d = aux_d[["Dia", "Cadastradas", "Abertas", "Percentual", "Percentual_num"]].copy()
            st.dataframe(
                style_percentual(show_d, threshold=20.0).hide(axis="index").format(na_rep=""),
                use_container_width=True
            )
        with c2:
            st.markdown("**Mensal**")
            show_m = aux_m[["Mês", "Cadastradas", "Abertas", "Percentual", "Percentual_num"]].copy()
            st.dataframe(
                style_percentual(show_m, threshold=20.0).hide(axis="index").format(na_rep=""),
                use_container_width=True
            )
