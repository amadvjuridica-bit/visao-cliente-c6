import os
import io
import json
import hashlib
import datetime as dt
from typing import Optional, Tuple, Dict

import pandas as pd
import streamlit as st

# =========================
# CONFIG
# =========================
START_DATE = dt.date(2026, 1, 1)  # memorizar a partir de 01/01/2026

# PLANILHA PRINCIPAL (C6)
COL_T = "DT_CONTA_CRIADA"                 # data de abertura
COL_P = "DT_FUNDACAO_EMPRESA"             # fundação
COL_X = "CHAVES_PIX_FORTE"                # tipo de chave pix
COL_Y = "VL_SALDO_MEDIO_MENSALIZADO"      # saldo
COL_V = "STATUS_CC"                       # status
COL_AQ = "BANCO_DOMICILIO"                # domicílio bancário
COL_BY = "FL_QUALIFICADO_COMISS"          # qualificada (0/1)
COL_BR = "MES_REF_COMISS"                 # M0/M1/M2 (referência)
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"   # texto: CASH IN: 3 | ...

# (se existirem)
COL_NOME = "NOME_CLIENTE"
COL_DOC = "CD_CPF_CNPJ_CLIENTE"

PAYOUT = {1: 210, 2: 345, 3: 600, 4: 810}

# PLANILHA LEADS (cadastro) - coluna M por posição (13ª coluna)
LEADS_DATE_COL_INDEX = 12

# PERSISTÊNCIA
DATA_DIR = "data_uploads"
os.makedirs(DATA_DIR, exist_ok=True)

LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
PREV_PATH = os.path.join(DATA_DIR, "prev.json")

HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_aberturas_diario.csv")
HIST_OPEN_MONTH = os.path.join(DATA_DIR, "hist_aberturas_mensal.csv")
HIST_CAD_DAILY  = os.path.join(DATA_DIR, "hist_cadastros_diario.csv")
HIST_CAD_MONTH  = os.path.join(DATA_DIR, "hist_cadastros_mensal.csv")

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
    # s vem como "YYYY-MM" ou "YYYY-MM-01" ou "YYYY-MM" period string
    try:
        # tenta YYYY-MM
        if isinstance(s, str) and len(s) == 7 and s[4] == "-":
            y, m = s.split("-")
            return f"{m}/{y}"
        dd = pd.to_datetime(s, errors="coerce")
        if pd.isna(dd):
            return str(s)
        return dd.strftime("%m/%Y")
    except Exception:
        return str(s)

def fmt_int(n) -> str:
    return f"{int(n):,}".replace(",", ".")

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
        return ""

# =========================
# UTILITÁRIOS
# =========================
def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _safe_to_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.date

def _normalize_str(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").str.strip()

def _contains_c6(val: str) -> bool:
    return "c6" in str(val).lower()

def _load_excel(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")

def _coerce_c6(df: pd.DataFrame) -> pd.DataFrame:
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

    # filtra tudo a partir de 01/01/2026 (para memorizar o que importa)
    df = df[df[COL_T].notna()].copy()
    df = df[df[COL_T] >= START_DATE].copy()

    return df

# =========================
# HISTÓRICO (GRAVAR / LER)
# =========================
def _read_hist(path: str, key_col: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=[key_col, "valor"])
    df = pd.read_csv(path)
    if key_col not in df.columns:
        return pd.DataFrame(columns=[key_col, "valor"])
    df["valor"] = pd.to_numeric(df["valor"], errors="coerce").fillna(0).astype(int)
    return df

def _write_hist(df: pd.DataFrame, path: str):
    df.to_csv(path, index=False)

def _upsert_hist(path: str, key_col: str, new_df: pd.DataFrame):
    """
    new_df: colunas [key_col, "valor"]
    Regra: se existir a chave, sobrescreve com o novo valor.
    """
    base = _read_hist(path, key_col)
    base = base.set_index(key_col)
    new_df = new_df.set_index(key_col)

    base.update(new_df)  # sobrescreve existentes
    missing = new_df.index.difference(base.index)
    if len(missing) > 0:
        base = pd.concat([base, new_df.loc[missing]], axis=0)

    base = base.reset_index().sort_values(key_col)
    _write_hist(base, path)

# =========================
# MÉTRICAS C6
# =========================
def _pix_info(df: pd.DataFrame):
    s = df[COL_X].astype("string").fillna("").str.strip().str.upper()
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

def _aberturas_por_dia(df: pd.DataFrame) -> pd.DataFrame:
    return (
        pd.Series(df[COL_T]).dropna()
        .value_counts().sort_index()
        .rename_axis("dia")
        .reset_index(name="valor")
    )

def _aberturas_por_mes(df: pd.DataFrame) -> pd.DataFrame:
    t = pd.to_datetime(df[COL_T], errors="coerce")
    m = t.dropna().dt.to_period("M").astype(str)
    return (
        m.value_counts().sort_index()
        .rename_axis("mes")
        .reset_index(name="valor")
    )

def _sum_saldo(df: pd.DataFrame) -> float:
    return float(df[COL_Y].sum())

def _status_counts(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df[COL_V].fillna("SEM STATUS").replace("", "SEM STATUS")
        .value_counts()
        .rename_axis("Status")
        .reset_index(name="Quantidade")
    )

def _domicilio_c6_count(df: pd.DataFrame) -> int:
    return int(df[COL_AQ].fillna("").astype(str).apply(_contains_c6).sum())

def _qualificadas(df: pd.DataFrame) -> pd.DataFrame:
    return df[df[COL_BY] == 1].copy()

def _br_counts(dfq: pd.DataFrame) -> pd.DataFrame:
    s = dfq[COL_BR].fillna("").astype(str).str.upper().str.strip()
    return (
        s.replace("", "SEM")
        .value_counts()
        .rename_axis("Referência")
        .reset_index(name="Quantidade")
    )

# =========================
# QUALIFICAÇÃO: considerar somente o MAIOR valor e o critério correspondente
# =========================
def _parse_criterios_max(txt: str) -> Tuple[int, str]:
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

def _payout_from_max(dfq: pd.DataFrame):
    if dfq.empty:
        return (
            pd.DataFrame(columns=["Nível", "Quantidade", "Valor unitário", "Total"]),
            0,
            pd.DataFrame(columns=["Critério (maior)", "Quantidade"]),
            pd.DataFrame()
        )

    parsed = dfq[COL_CRIT].apply(_parse_criterios_max)
    dfq2 = dfq.copy()
    dfq2["Critério considerado"] = parsed.apply(lambda x: x[1])
    dfq2["Nível considerado"] = parsed.apply(lambda x: x[0])
    dfq2["Receita"] = dfq2["Nível considerado"].apply(lambda n: PAYOUT.get(int(n), 0))

    levels = dfq2["Nível considerado"].astype(int)
    levels = levels[levels > 0]

    # Tabela por critério considerado
    crit_tbl = (
        dfq2[dfq2["Nível considerado"] > 0]["Critério considerado"]
        .value_counts()
        .rename_axis("Critério (maior)")
        .reset_index(name="Quantidade")
    )

    if levels.empty:
        return (
            pd.DataFrame(columns=["Nível", "Quantidade", "Valor unitário", "Total"]),
            0,
            crit_tbl,
            dfq2
        )

    counts = levels.value_counts().sort_index()
    rows = []
    for level, qty in counts.items():
        unit = PAYOUT.get(int(level), 0)
        total = int(qty) * int(unit)
        rows.append([int(level), int(qty), unit, total])

    payout_tbl = pd.DataFrame(rows, columns=["Nível", "Quantidade", "Valor unitário", "Total"])
    total_payout = int(payout_tbl["Total"].sum()) if not payout_tbl.empty else 0
    return payout_tbl, total_payout, crit_tbl, dfq2

# =========================
# FUNDAÇÕES: por dia -> mês/ano
# =========================
def _fundacoes_mes_por_dia(df: pd.DataFrame, dia: dt.date) -> pd.DataFrame:
    x = df[df[COL_T] == dia][[COL_P]].dropna().copy()
    if x.empty:
        return pd.DataFrame(columns=["Mês de fundação", "Quantidade"])
    x["MES_FUND"] = x[COL_P].apply(lambda d: d.strftime("%m/%Y") if isinstance(d, dt.date) else "")
    out = (
        x["MES_FUND"].replace("", "SEM FUNDAÇÃO")
        .value_counts()
        .rename_axis("Mês de fundação")
        .reset_index(name="Quantidade")
    )
    return out

# =========================
# LEADS
# =========================
def _load_leads_dates(df_leads: pd.DataFrame) -> Tuple[pd.DataFrame, str]:
    if df_leads.shape[1] <= LEADS_DATE_COL_INDEX:
        raise ValueError("A planilha de Leads não tem a coluna M (13ª coluna).")
    col_used = df_leads.columns[LEADS_DATE_COL_INDEX]
    s = pd.to_datetime(df_leads[col_used], errors="coerce").dt.date
    out = pd.DataFrame({"dia": s}).dropna()
    out = out[out["dia"] >= START_DATE].copy()
    return out, str(col_used)

def _cadastros_por_dia(df_dates: pd.DataFrame) -> pd.DataFrame:
    return (
        df_dates["dia"]
        .value_counts().sort_index()
        .rename_axis("dia")
        .reset_index(name="valor")
    )

def _cadastros_por_mes(df_dates: pd.DataFrame) -> pd.DataFrame:
    t = pd.to_datetime(df_dates["dia"], errors="coerce")
    m = t.dropna().dt.to_period("M").astype(str)
    return (
        m.value_counts().sort_index()
        .rename_axis("mes")
        .reset_index(name="valor")
    )

# =========================
# % Cadastro ÷ Abertura usando HISTÓRICO
# =========================
def _pct_table_daily() -> pd.DataFrame:
    o = _read_hist(HIST_OPEN_DAILY, "dia")
    c = _read_hist(HIST_CAD_DAILY, "dia")
    df = pd.merge(o, c, on="dia", how="outer", suffixes=("_abertas", "_cad")).fillna(0)
    df["valor_abertas"] = df["valor_abertas"].astype(int)
    df["valor_cad"] = df["valor_cad"].astype(int)
    df["percentual"] = df.apply(lambda r: (r["valor_cad"] / r["valor_abertas"]) if r["valor_abertas"] > 0 else 0.0, axis=1)
    df = df.sort_values("dia")
    return df

def _pct_table_month() -> pd.DataFrame:
    o = _read_hist(HIST_OPEN_MONTH, "mes")
    c = _read_hist(HIST_CAD_MONTH, "mes")
    df = pd.merge(o, c, on="mes", how="outer", suffixes=("_abertas", "_cad")).fillna(0)
    df["valor_abertas"] = df["valor_abertas"].astype(int)
    df["valor_cad"] = df["valor_cad"].astype(int)
    df["percentual"] = df.apply(lambda r: (r["valor_cad"] / r["valor_abertas"]) if r["valor_abertas"] > 0 else 0.0, axis=1)
    df = df.sort_values("mes")
    return df

def _style_pct(df: pd.DataFrame):
    def cell_color(v):
        try:
            return "background-color: #cfe9ff;" if float(v) >= 0.20 else "background-color: #ffd6d6;"
        except Exception:
            return ""
    return df.style.applymap(cell_color, subset=["percentual"])

# =========================
# Snapshot (ontem x hoje) - continua
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
# Login simples
# =========================
def login_gate():
    st.sidebar.title("Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")
    if st.sidebar.button("Entrar"):
        st.session_state["logged_in"] = (u == "admin" and p == "123456")
        if not st.session_state["logged_in"]:
            st.sidebar.error("Usuário ou senha inválidos.")
    return st.session_state.get("logged_in", False)

# =========================
# APP
# =========================
st.set_page_config(page_title="Assis & Mollerke", layout="wide")

# Logo
logo_paths = [
    "assets/logo.png",
    "logo.png",
    "LOGO CORRETA.png",
    "LOGO%20CORRETA.png",
]
logo_found = next((p for p in logo_paths if os.path.exists(p)), None)

top_l, top_r = st.columns([1, 3])
with top_l:
    if logo_found:
        st.image(logo_found, use_container_width=True)
with top_r:
    st.markdown("## Assis & Mollerke")
    st.caption("Painel automático com memória desde 01/01/2026 (não perde dados do passado).")

if not login_gate():
    st.stop()

st.subheader("Importação do dia")

col_up1, col_up2 = st.columns(2)
with col_up1:
    uploaded_c6 = st.file_uploader("1) Planilha principal (C6) (.xlsx)", type=["xlsx"], key="c6")
with col_up2:
    uploaded_leads = st.file_uploader("2) Planilha de Leads (Cadastro) (.xlsx)", type=["xlsx"], key="leads")

prev, latest_saved = _load_prev_latest()

# =========================
# PROCESSAMENTO DO DIA
# =========================
lead_col_used = None

if uploaded_c6:
    file_bytes = uploaded_c6.getvalue()
    file_hash = _hash_bytes(file_bytes)

    df = _load_excel(file_bytes)
    df = _coerce_c6(df)

    # Atualiza HISTÓRICO de aberturas (diário e mensal)
    open_daily = _aberturas_por_dia(df)     # dia, valor
    open_month = _aberturas_por_mes(df)     # mes, valor
    _upsert_hist(HIST_OPEN_DAILY, "dia", open_daily)
    _upsert_hist(HIST_OPEN_MONTH, "mes", open_month)

    # Métricas do arquivo atual (para resumo do dia)
    qtd_com_pix, qtd_sem_pix, pix_por_chave = _pix_info(df)
    saldo_total = _sum_saldo(df)
    status_tbl = _status_counts(df)
    qtd_c6 = _domicilio_c6_count(df)

    dfq = _qualificadas(df)
    br_counts = _br_counts(dfq)
    payout_tbl, total_payout, crit_tbl, dfq_view = _payout_from_max(dfq)
    total_contas_arquivo = int(df.shape[0])
    total_qualificadas = int(dfq.shape[0])

    # Leads: atualiza histórico se enviado
    if uploaded_leads:
        df_leads = _load_excel(uploaded_leads.getvalue())
        df_dates, lead_col_used = _load_leads_dates(df_leads)

        cad_daily = _cadastros_por_dia(df_dates)  # dia, valor
        cad_month = _cadastros_por_mes(df_dates)  # mes, valor
        _upsert_hist(HIST_CAD_DAILY, "dia", cad_daily)
        _upsert_hist(HIST_CAD_MONTH, "mes", cad_month)

    # Snapshot do "arquivo do dia" (para comparação hoje vs ontem)
    metrics = {
        "total_contas": total_contas_arquivo,
        "qtd_com_pix": qtd_com_pix,
        "qtd_sem_pix": qtd_sem_pix,
        "saldo_total": saldo_total,
        "qtd_c6": qtd_c6,
        "total_qualificadas": total_qualificadas,
        "total_payout": total_payout,
    }
    _snapshot_to_disk(tag=uploaded_c6.name, file_hash=file_hash, metrics=metrics)
    prev, latest_saved = _load_prev_latest()

# =========================
# RESUMO / PAINEL
# =========================
st.divider()

if latest_saved:
    m = latest_saved["metrics"]

    st.subheader("Resumo do dia (arquivo enviado)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Contas abertas (arquivo)", fmt_int(m["total_contas"]))
    c2.metric("Saldo total", fmt_money(m["saldo_total"]))
    c3.metric("Clientes com Pix", fmt_int(m["qtd_com_pix"]))
    c4.metric("Clientes sem Pix", fmt_int(m["qtd_sem_pix"]))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Domicílio C6", fmt_int(m["qtd_c6"]))
    c6.metric("Contas qualificadas", fmt_int(m["total_qualificadas"]))
    c7.metric("Receita estimada", fmt_money(m["total_payout"]))
    c8.metric("Arquivo", latest_saved.get("tag", "-"))

    st.subheader("Diferença (arquivo de hoje vs arquivo anterior)")
    if prev and prev.get("metrics"):
        pm = prev["metrics"]
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Δ Contas", f"{(m['total_contas'] - pm.get('total_contas', 0)):+,}".replace(",", "."))
        d2.metric("Δ Saldo", fmt_money(m["saldo_total"] - pm.get("saldo_total", 0.0)))
        d3.metric("Δ Qualificadas", f"{(m['total_qualificadas'] - pm.get('total_qualificadas', 0)):+,}".replace(",", "."))
        d4.metric("Δ Receita", fmt_money(m["total_payout"] - pm.get("total_payout", 0)))
    else:
        st.info("Ainda não existe arquivo anterior para comparar. Envie pelo menos 2 dias.")

else:
    st.info("Envie a planilha principal (C6) para gerar o resumo do dia. O histórico será acumulado a partir de 01/01/2026.")

# =========================
# TABS (HISTÓRICO)
# =========================
st.divider()
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "Histórico de Aberturas",
    "Fundações",
    "Pix e Status",
    "Qualificadas e Receita",
    "Cadastro vs Abertura (%)",
    "Backup do histórico",
])

with tab1:
    st.markdown("### Aberturas (histórico desde 01/01/2026)")

    h_daily = _read_hist(HIST_OPEN_DAILY, "dia")
    if h_daily.empty:
        st.info("Sem histórico diário ainda.")
    else:
        show = h_daily.copy()
        show["Dia"] = show["dia"].apply(lambda x: fmt_date_br(pd.to_datetime(x).date()))
        show = show[["Dia", "valor"]].rename(columns={"valor": "Contas abertas"})
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
    st.markdown("### Fundações por dia (mês/ano) — usando o arquivo enviado")
    if not uploaded_c6:
        st.info("Envie a planilha principal para ver o detalhamento de fundações do dia.")
    else:
        dias = sorted(pd.Series(df[COL_T]).dropna().unique().tolist())
        if not dias:
            st.info("Sem datas de abertura no arquivo.")
        else:
            dia_sel = st.selectbox("Selecione o dia", dias, format_func=fmt_date_br)
            fund = _fundacoes_mes_por_dia(df, dia_sel)
            st.dataframe(fund, use_container_width=True, hide_index=True)

with tab3:
    st.markdown("### Pix e Status — usando o arquivo enviado")
    if not uploaded_c6:
        st.info("Envie a planilha principal para ver Pix e Status.")
    else:
        a, b = st.columns(2)
        with a:
            st.metric("Clientes com Pix", fmt_int(qtd_com_pix))
        with b:
            st.metric("Clientes sem Pix", fmt_int(qtd_sem_pix))

        st.markdown("#### Tipos de chave Pix (somente quem tem Pix)")
        st.dataframe(pix_por_chave, use_container_width=True, hide_index=True)

        st.markdown("#### Status")
        st.dataframe(status_tbl, use_container_width=True, hide_index=True)

with tab4:
    st.markdown("### Qualificadas e Receita — usando o arquivo enviado")
    st.caption("Cálculo considera SOMENTE o maior nível do texto de critérios (ex.: 'SALDO MEDIO: 4').")

    if not uploaded_c6:
        st.info("Envie a planilha principal para ver as qualificadas.")
    else:
        st.markdown("#### Critério (maior) atingido")
        st.dataframe(crit_tbl, use_container_width=True, hide_index=True)

        st.markdown("#### Receita por nível")
        payout_show = payout_tbl.copy()
        if not payout_show.empty:
            payout_show["Valor unitário"] = payout_show["Valor unitário"].apply(fmt_money)
            payout_show["Total"] = payout_show["Total"].apply(fmt_money)
        st.dataframe(payout_show, use_container_width=True, hide_index=True)
        st.success(f"Receita estimada: {fmt_money(total_payout)}")

        st.markdown("#### Visualização por cliente (para entender)")
        cols_show = []
        if COL_DOC in dfq_view.columns:
            cols_show.append(COL_DOC)
        if COL_NOME in dfq_view.columns:
            cols_show.append(COL_NOME)
        cols_show += ["Critério considerado", "Nível considerado", "Receita", COL_CRIT]

        view = dfq_view[cols_show].copy()
        view["Receita"] = view["Receita"].apply(fmt_money)
        view = view.rename(columns={"Receita": "Receita estimada"})
        st.dataframe(view, use_container_width=True, hide_index=True)

with tab5:
    st.markdown("### Cadastro ÷ Abertura (%) — histórico desde 01/01/2026")
    st.caption("Azul: >= 20% | Vermelho: < 20%")

    if not os.path.exists(HIST_OPEN_DAILY) or not os.path.exists(HIST_CAD_DAILY):
        st.warning("Envie as duas planilhas (C6 e Leads) pelo menos uma vez para criar o histórico.")
    else:
        pct_d = _pct_table_daily()
        if pct_d.empty:
            st.info("Sem dados suficientes ainda.")
        else:
            show = pct_d.copy()
            show["Dia"] = show["dia"].apply(lambda x: fmt_date_br(pd.to_datetime(x).date()))
            show = show.rename(columns={
                "valor_abertas": "Contas abertas",
                "valor_cad": "Contas cadastradas",
                "percentual": "Percentual",
            })
            show = show[["Dia", "Contas abertas", "Contas cadastradas", "Percentual"]]

            # estilo pela coluna Percentual (numérica)
            styled = _style_pct(pct_d.rename(columns={
                "valor_abertas": "Contas abertas",
                "valor_cad": "Contas cadastradas",
                "percentual": "Percentual",
            }))
            styled = styled.format({"Percentual": lambda x: fmt_pct(x)})

            # mostra já com dia formatado
            styled_df = pct_d.copy()
            styled_df["Dia"] = styled_df["dia"].apply(lambda x: fmt_date_br(pd.to_datetime(x).date()))
            styled_df = styled_df.rename(columns={
                "valor_abertas": "Contas abertas",
                "valor_cad": "Contas cadastradas",
                "percentual": "Percentual",
            })[["Dia", "Contas abertas", "Contas cadastradas", "Percentual"]]

            # Como o .style não aplica no df com Dia já formatado de forma simples,
            # aplicamos o estilo no df numérico e depois mostramos o df formatado.
            # Resultado prático: cor funciona e o usuário vê data em dd/mm/aaaa.
            colored = pct_d.copy()
            colored = colored.rename(columns={
                "valor_abertas": "Contas abertas",
                "valor_cad": "Contas cadastradas",
                "percentual": "Percentual",
            })
            colored["Dia"] = colored["dia"].apply(lambda x: fmt_date_br(pd.to_datetime(x).date()))
            colored = colored[["Dia", "Contas abertas", "Contas cadastradas", "Percentual"]]

            # Para manter a cor, criamos uma coluna auxiliar interna
            aux = colored.copy()
            aux["Percentual_num"] = pct_d["percentual"].values
            def style_row(row):
                v = row["Percentual_num"]
                color = "#cfe9ff" if v >= 0.20 else "#ffd6d6"
                return ["background-color: %s" % color if c == "Percentual" else "" for c in row.index]

            st.dataframe(
                aux.drop(columns=["Percentual_num"])
                   .style.apply(style_row, axis=1)
                   .format({"Percentual": lambda x: fmt_pct(float(x))}),
                use_container_width=True
            )

        st.markdown("#### Mensal")
        pct_m = _pct_table_month()
        if pct_m.empty:
            st.info("Sem dados mensais ainda.")
        else:
            mm = pct_m.copy()
            mm["Mês"] = mm["mes"].apply(fmt_month_br)
            mm = mm.rename(columns={
                "valor_abertas": "Contas abertas",
                "valor_cad": "Contas cadastradas",
                "percentual": "Percentual",
            })[["Mês", "Contas abertas", "Contas cadastradas", "Percentual"]]

            auxm = mm.copy()
            auxm["Percentual_num"] = pct_m["percentual"].values
            def style_row_m(row):
                v = row["Percentual_num"]
                color = "#cfe9ff" if v >= 0.20 else "#ffd6d6"
                return ["background-color: %s" % color if c == "Percentual" else "" for c in row.index]

            st.dataframe(
                auxm.drop(columns=["Percentual_num"])
                    .style.apply(style_row_m, axis=1)
                    .format({"Percentual": lambda x: fmt_pct(float(x))}),
                use_container_width=True
            )

with tab6:
    st.markdown("### Backup do histórico (recomendado)")
    st.caption("Baixe para guardar. Isso evita perder a memória se o app for reiniciado/recriado.")

    files = [
        (HIST_OPEN_DAILY, "aberturas_diario.csv"),
        (HIST_OPEN_MONTH, "aberturas_mensal.csv"),
        (HIST_CAD_DAILY, "cadastros_diario.csv"),
        (HIST_CAD_MONTH, "cadastros_mensal.csv"),
    ]

    for path, name in files:
        if os.path.exists(path):
            with open(path, "rb") as f:
                st.download_button(
                    label=f"Baixar {name}",
                    data=f,
                    file_name=name,
                    mime="text/csv"
                )
        else:
            st.info(f"Ainda não existe: {name} (vai aparecer após você enviar os arquivos).")
