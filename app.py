import os
import io
import json
import hashlib
import datetime as dt
from typing import Optional, Tuple, Dict

import pandas as pd
import streamlit as st

# =========================
# CONFIG - PLANILHA PRINCIPAL (C6)
# =========================
COL_T = "DT_CONTA_CRIADA"                 # data de abertura da conta
COL_P = "DT_FUNDACAO_EMPRESA"             # fundação
COL_X = "CHAVES_PIX_FORTE"                # tipo de chave pix (CNPJ/EMAIL/PHONE/-)
COL_Y = "VL_SALDO_MEDIO_MENSALIZADO"      # saldo
COL_V = "STATUS_CC"                       # status
COL_AQ = "BANCO_DOMICILIO"                # banco domicílio
COL_BY = "FL_QUALIFICADO_COMISS"          # qualificada (0/1)
COL_BR = "MES_REF_COMISS"                 # M0/M1/M2
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"   # texto: CASH IN: 3 | ...

# Colunas opcionais (se existirem)
COL_NOME = "NOME_CLIENTE"
COL_DOC = "CD_CPF_CNPJ_CLIENTE"

# Pagamentos por nível
PAYOUT = {1: 210, 2: 345, 3: 600, 4: 810}

# =========================
# CONFIG - PLANILHA LEADS (CADASTRO)
# =========================
# Você disse: coluna M tem a data do cadastro (M = 13ª coluna => índice 12)
LEADS_DATE_COL_INDEX = 12

# =========================
# PERSISTÊNCIA (ontem vs hoje)
# =========================
DATA_DIR = "data_uploads"
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
os.makedirs(DATA_DIR, exist_ok=True)

# =========================
# Formatação
# =========================
def fmt_date_br(d) -> str:
    if pd.isna(d) or d is None:
        return ""
    try:
        if isinstance(d, dt.date):
            return d.strftime("%d/%m/%Y")
        dd = pd.to_datetime(d, errors="coerce")
        if pd.isna(dd):
            return ""
        return dd.strftime("%d/%m/%Y")
    except Exception:
        return ""

def fmt_month_br(d) -> str:
    if pd.isna(d) or d is None:
        return ""
    dd = pd.to_datetime(d, errors="coerce")
    if pd.isna(dd):
        return ""
    return dd.strftime("%m/%Y")

def fmt_int(n: int) -> str:
    return f"{int(n):,}".replace(",", ".")

def fmt_money(v: float) -> str:
    try:
        s = f"{float(v):,.2f}"
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {s}"
    except Exception:
        return "R$ 0,00"

def fmt_money_signed(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return sign + " " + fmt_money(abs(v))

def fmt_pct(p: float) -> str:
    if p is None or pd.isna(p):
        return ""
    return f"{p*100:.1f}%".replace(".", ",")

# =========================
# Utilitários
# =========================
def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _safe_to_date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.date

def _normalize_str(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").str.strip()

def _contains_c6(val: str) -> bool:
    return "c6" in str(val).lower()

def _load_excel(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")

def _coerce_columns(df: pd.DataFrame) -> pd.DataFrame:
    required = [COL_T, COL_P, COL_X, COL_Y, COL_V, COL_AQ, COL_BY, COL_BR, COL_CRIT]
    for c in required:
        if c not in df.columns:
            df[c] = pd.NA

    df[COL_T] = _safe_to_date(df[COL_T])
    df[COL_P] = _safe_to_date(df[COL_P])

    df[COL_X] = _normalize_str(df[COL_X])
    df[COL_V] = _normalize_str(df[COL_V])
    df[COL_AQ] = _normalize_str(df[COL_AQ])
    df[COL_BR] = _normalize_str(df[COL_BR])
    df[COL_CRIT] = _normalize_str(df[COL_CRIT])

    df[COL_BY] = pd.to_numeric(df[COL_BY], errors="coerce").fillna(0).astype(int)
    df[COL_Y] = pd.to_numeric(df[COL_Y], errors="coerce").fillna(0.0)
    return df

# =========================
# Métricas (C6)
# =========================
def _pix_info(df: pd.DataFrame):
    s = df[COL_X].astype("string").fillna("").str.strip().str.upper()
    s = s.str.replace("'", "", regex=False)
    has_pix = ~s.isin(["", "-", "NAN", "NONE", "SEM", "SEM PIX"])

    qtd_com = int(has_pix.sum())
    qtd_sem = int((~has_pix).sum())

    por_chave = (
        s.loc[has_pix]
         .value_counts(dropna=True)
         .rename_axis("Tipo de chave Pix")
         .reset_index(name="Quantidade")
    )
    return qtd_com, qtd_sem, por_chave

def _contas_criadas(df: pd.DataFrame):
    por_dia = (
        pd.Series(df[COL_T]).dropna()
        .value_counts().sort_index()
        .rename_axis("Dia").reset_index(name="Contas abertas")
    )

    t = pd.to_datetime(df[COL_T], errors="coerce")
    por_mes = (
        t.dropna().dt.to_period("M").astype(str)
        .value_counts().sort_index()
        .rename_axis("Mês").reset_index(name="Contas abertas")
    )

    total = int(pd.Series(df[COL_T]).dropna().shape[0])
    dias = sorted(pd.Series(df[COL_T]).dropna().unique().tolist())
    return por_dia, por_mes, total, dias

def _fundacoes_mes_por_dia(df: pd.DataFrame, dia: dt.date) -> pd.DataFrame:
    x = df[df[COL_T] == dia][[COL_T, COL_P]].dropna()
    if x.empty:
        return pd.DataFrame(columns=["Mês de fundação", "Quantidade"])

    x = x.copy()
    x["MES_FUNDACAO"] = x[COL_P].apply(fmt_month_br)
    out = (
        x["MES_FUNDACAO"].replace("", "SEM FUNDAÇÃO")
        .value_counts()
        .rename_axis("Mês de fundação")
        .reset_index(name="Quantidade")
        .sort_values("Mês de fundação")
    )
    return out

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
# Qualificação: considerar SOMENTE o MAIOR valor e o critério correspondente
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
    dfq2["Nível considerado"] = parsed.apply(lambda x: x[0])
    dfq2["Critério considerado"] = parsed.apply(lambda x: x[1])
    dfq2["Receita (R$)"] = dfq2["Nível considerado"].apply(lambda n: PAYOUT.get(int(n), 0))

    levels = dfq2["Nível considerado"].astype(int)
    levels = levels[levels > 0]

    if levels.empty:
        crit_tbl = (
            dfq2["Critério considerado"]
            .value_counts()
            .rename_axis("Critério (maior)")
            .reset_index(name="Quantidade")
        )
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
        rows.append([int(level), int(qty), int(unit), int(total)])

    payout_tbl = pd.DataFrame(rows, columns=["Nível", "Quantidade", "Valor unitário", "Total"])
    total_payout = int(payout_tbl["Total"].sum()) if not payout_tbl.empty else 0

    crit_tbl = (
        dfq2[dfq2["Nível considerado"] > 0]["Critério considerado"]
        .value_counts()
        .rename_axis("Critério (maior)")
        .reset_index(name="Quantidade")
    )

    return payout_tbl, total_payout, crit_tbl, dfq2

def _sum_saldo(df: pd.DataFrame) -> float:
    return float(df[COL_Y].sum())

# =========================
# LEADS: pegar data do cadastro da coluna M (posicional)
# =========================
def _load_leads_dates(df_leads: pd.DataFrame) -> Tuple[pd.DataFrame, str]:
    """
    Retorna:
      - df com coluna DATE_CADASTRO (date)
      - nome da coluna que foi usada (para transparência)
    """
    if df_leads.shape[1] <= LEADS_DATE_COL_INDEX:
        raise ValueError("A planilha de Leads não tem a coluna M (13ª coluna).")

    col_used = df_leads.columns[LEADS_DATE_COL_INDEX]
    s = pd.to_datetime(df_leads[col_used], errors="coerce").dt.date
    out = pd.DataFrame({"DATE_CADASTRO": s}).dropna()
    return out, str(col_used)

def _cadastros_por_dia(df_dates: pd.DataFrame) -> pd.DataFrame:
    return (
        df_dates["DATE_CADASTRO"]
        .value_counts()
        .sort_index()
        .rename_axis("Dia")
        .reset_index(name="Contas cadastradas")
    )

def _cadastros_por_mes(df_dates: pd.DataFrame) -> pd.DataFrame:
    t = pd.to_datetime(df_dates["DATE_CADASTRO"], errors="coerce")
    return (
        t.dropna()
        .dt.to_period("M").astype(str)
        .value_counts().sort_index()
        .rename_axis("Mês")
        .reset_index(name="Contas cadastradas")
    )

def _merge_pct(daily_open: pd.DataFrame, daily_cad: pd.DataFrame) -> pd.DataFrame:
    dfm = pd.merge(daily_open, daily_cad, on="Dia", how="outer").fillna(0)
    dfm["Contas abertas"] = dfm["Contas abertas"].astype(int)
    dfm["Contas cadastradas"] = dfm["Contas cadastradas"].astype(int)
    dfm["Percentual"] = dfm.apply(
        lambda r: (r["Contas cadastradas"] / r["Contas abertas"]) if r["Contas abertas"] > 0 else 0.0,
        axis=1
    )
    dfm = dfm.sort_values("Dia")
    return dfm

def _merge_pct_month(month_open: pd.DataFrame, month_cad: pd.DataFrame) -> pd.DataFrame:
    dfm = pd.merge(month_open, month_cad, on="Mês", how="outer").fillna(0)
    dfm["Contas abertas"] = dfm["Contas abertas"].astype(int)
    dfm["Contas cadastradas"] = dfm["Contas cadastradas"].astype(int)
    dfm["Percentual"] = dfm.apply(
        lambda r: (r["Contas cadastradas"] / r["Contas abertas"]) if r["Contas abertas"] > 0 else 0.0,
        axis=1
    )
    dfm = dfm.sort_values("Mês")
    return dfm

def _style_pct(df: pd.DataFrame):
    # Azul >= 20%, vermelho < 20%
    def color_row(val):
        try:
            return "background-color: #cfe9ff;" if float(val) >= 0.20 else "background-color: #ffd6d6;"
        except Exception:
            return ""

    styled = df.style.applymap(color_row, subset=["Percentual"])
    return styled

# =========================
# Snapshot (ontem x hoje) - mantém como estava
# =========================
def _snapshot_to_disk(tag: str, file_hash: str, metrics: Dict):
    payload = {
        "tag": tag,
        "file_hash": file_hash,
        "saved_at": dt.datetime.now().isoformat(),
        "metrics": metrics,
    }
    prev_path = os.path.join(DATA_DIR, "prev.json")
    if os.path.exists(LATEST_PATH):
        with open(LATEST_PATH, "r", encoding="utf-8") as f:
            old = f.read()
        with open(prev_path, "w", encoding="utf-8") as f:
            f.write(old)

    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def _load_prev_latest() -> Tuple[Optional[dict], Optional[dict]]:
    prev_path = os.path.join(DATA_DIR, "prev.json")
    latest = prev = None
    if os.path.exists(LATEST_PATH):
        with open(LATEST_PATH, "r", encoding="utf-8") as f:
            latest = json.load(f)
    if os.path.exists(prev_path):
        with open(prev_path, "r", encoding="utf-8") as f:
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
st.set_page_config(page_title="Assis & Mollerke | Visão Cliente C6", layout="wide")

# Logo (agora busca também o seu arquivo real)
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
    st.caption("Visão Cliente - C6 | Envie os arquivos do dia para gerar o painel e os percentuais.")

if not login_gate():
    st.stop()

st.subheader("Importação do dia")

col_up1, col_up2 = st.columns(2)
with col_up1:
    uploaded_c6 = st.file_uploader("1) Planilha principal (C6) (.xlsx)", type=["xlsx"], key="c6")
with col_up2:
    uploaded_leads = st.file_uploader("2) Planilha de Leads (Cadastro) (.xlsx)", type=["xlsx"], key="leads")

prev, latest_saved = _load_prev_latest()

# Só calcula tudo se a principal foi enviada
if uploaded_c6:
    file_bytes = uploaded_c6.getvalue()
    file_hash = _hash_bytes(file_bytes)

    df = _load_excel(file_bytes)
    df = _coerce_columns(df)

    # C6 - métricas
    por_dia, por_mes, total_contas, dias_list = _contas_criadas(df)
    qtd_com_pix, qtd_sem_pix, pix_por_chave = _pix_info(df)
    saldo_total = _sum_saldo(df)
    status_tbl = _status_counts(df)
    qtd_c6 = _domicilio_c6_count(df)

    dfq = _qualificadas(df)
    br_counts = _br_counts(dfq)
    payout_tbl, total_payout, crit_tbl, dfq_view = _payout_from_max(dfq)
    total_qualificadas = int(dfq.shape[0])

    # Leads - percentual (se enviar)
    pct_daily_tbl = None
    pct_month_tbl = None
    lead_col_used = None

    if uploaded_leads:
        df_leads = _load_excel(uploaded_leads.getvalue())
        df_dates, lead_col_used = _load_leads_dates(df_leads)

        cad_por_dia = _cadastros_por_dia(df_dates)
        cad_por_mes = _cadastros_por_mes(df_dates)

        # Ajusta nomes pra merge
        open_daily = por_dia.rename(columns={"Contas abertas": "Contas abertas"})
        open_month = por_mes.rename(columns={"Contas abertas": "Contas abertas"})

        pct_daily_tbl = _merge_pct(open_daily, cad_por_dia)
        pct_month_tbl = _merge_pct_month(open_month, cad_por_mes)

    # snapshot (mantém)
    metrics = {
        "total_contas": total_contas,
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
    # RESUMO
    # =========================
    st.divider()
    st.subheader("Resumo do dia")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Contas abertas (total)", fmt_int(total_contas))
    c2.metric("Saldo total", fmt_money(saldo_total))
    c3.metric("Clientes com Pix", fmt_int(qtd_com_pix))
    c4.metric("Clientes sem Pix", fmt_int(qtd_sem_pix))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Clientes com domicílio C6", fmt_int(qtd_c6))
    c6.metric("Contas qualificadas", fmt_int(total_qualificadas))
    c7.metric("Receita estimada", fmt_money(total_payout))
    c8.metric("Arquivo", uploaded_c6.name)

    # =========================
    # TABS
    # =========================
    st.divider()
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "Aberturas",
        "Fundações",
        "Pix e Status",
        "Qualificadas e Receita",
        "Cadastro vs Abertura (%)"
    ])

    with tab1:
        st.markdown("### Contas abertas por dia")
        tmp = por_dia.copy()
        tmp["Dia"] = tmp["Dia"].apply(fmt_date_br)
        st.dataframe(tmp, use_container_width=True, hide_index=True)

        st.markdown("### Contas abertas por mês")
        st.dataframe(por_mes, use_container_width=True, hide_index=True)

    with tab2:
        st.markdown("### Fundações por dia (mês/ano)")
        dias = sorted(pd.Series(df[COL_T]).dropna().unique().tolist())
        if not dias:
            st.info("Não há datas de abertura na planilha.")
        else:
            dia_sel = st.selectbox("Selecione o dia de abertura", dias, format_func=fmt_date_br)
            fund_mes = _fundacoes_mes_por_dia(df, dia_sel)
            st.dataframe(fund_mes, use_container_width=True, hide_index=True)

    with tab3:
        st.markdown("### Pix")
        col_a, col_b = st.columns(2)
        with col_a:
            st.metric("Clientes com Pix", fmt_int(qtd_com_pix))
        with col_b:
            st.metric("Clientes sem Pix", fmt_int(qtd_sem_pix))

        st.markdown("#### Distribuição por tipo de chave (somente quem tem Pix)")
        st.dataframe(pix_por_chave, use_container_width=True, hide_index=True)

        st.markdown("### Status")
        st.dataframe(status_tbl, use_container_width=True, hide_index=True)

    with tab4:
        st.markdown("### Critério considerado (somente o maior valor)")
        st.dataframe(crit_tbl, use_container_width=True, hide_index=True)

        st.markdown("### Receita por nível (base: maior nível por cliente)")
        payout_show = payout_tbl.copy()
        if not payout_show.empty:
            payout_show["Valor unitário"] = payout_show["Valor unitário"].apply(fmt_money)
            payout_show["Total"] = payout_show["Total"].apply(fmt_money)
        st.dataframe(payout_show, use_container_width=True, hide_index=True)

        st.success(f"Receita estimada total: {fmt_money(total_payout)}")

        st.markdown("### Visualização por cliente (para entender)")
        cols_show = []
        if COL_DOC in dfq_view.columns:
            cols_show.append(COL_DOC)
        if COL_NOME in dfq_view.columns:
            cols_show.append(COL_NOME)

        cols_show += ["Critério considerado", "Nível considerado", "Receita (R$)", COL_CRIT]

        view = dfq_view[cols_show].copy()
        view["Receita (R$)"] = view["Receita (R$)"].apply(fmt_money)
        st.dataframe(view, use_container_width=True, hide_index=True)

    with tab5:
        st.markdown("### Percentual: Contas cadastradas ÷ Contas abertas")
        st.caption("Regras: acima de 20% = azul | abaixo de 20% = vermelho.")

        if not uploaded_leads:
            st.warning("Envie também a planilha de Leads para calcular o percentual.")
        else:
            st.caption(f"Coluna usada para 'data do cadastro' na planilha de Leads: **{lead_col_used}** (coluna M).")

            st.markdown("#### Diário")
            pct_daily_show = pct_daily_tbl.copy()
            pct_daily_show["Dia"] = pct_daily_show["Dia"].apply(fmt_date_br)
            pct_daily_show["Percentual"] = pct_daily_show["Percentual"].apply(lambda x: x)
            pct_daily_show_display = pct_daily_show.copy()
            pct_daily_show_display["Percentual"] = pct_daily_show_display["Percentual"].apply(fmt_pct)

            # Mantém a coluna Percentual "numérica" para estilo, e mostra uma versão formatada
            pct_daily_numeric = pct_daily_tbl.copy()
            pct_daily_numeric["Percentual"] = pct_daily_numeric["Percentual"].astype(float)

            st.dataframe(
                _style_pct(pct_daily_numeric)
                .format({"Percentual": lambda x: fmt_pct(x)})
                .hide(axis="index"),
                use_container_width=True
            )

            st.markdown("#### Mensal")
            pct_month_numeric = pct_month_tbl.copy()
            pct_month_numeric["Percentual"] = pct_month_numeric["Percentual"].astype(float)

            st.dataframe(
                _style_pct(pct_month_numeric)
                .format({"Percentual": lambda x: fmt_pct(x)})
                .hide(axis="index"),
                use_container_width=True
            )

else:
    st.info("Envie a planilha principal (C6) para gerar o painel.")
