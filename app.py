import os
import io
import json
import hashlib
import datetime as dt
from typing import Optional, Tuple, Dict, List

import pandas as pd
import streamlit as st

# ---------------------------
# CONFIG: nomes das colunas (da sua planilha)
# ---------------------------
COL_T = "DT_CONTA_CRIADA"                 # criação da conta
COL_P = "DT_FUNDACAO_EMPRESA"             # fundação
COL_X = "CHAVES_PIX_FORTE"                # tipo de chave pix (CNPJ/EMAIL/PHONE/-)
COL_Y = "VL_SALDO_MEDIO_MENSALIZADO"      # saldo médio mensalizado
COL_V = "STATUS_CC"                       # status
COL_AQ = "BANCO_DOMICILIO"                # banco domicílio
COL_BY = "FL_QUALIFICADO_COMISS"          # qualificada (0/1)
COL_BR = "MES_REF_COMISS"                 # M0/M1/M2
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"   # texto dos critérios (CASH IN / DOM / etc)

# Colunas opcionais (se existirem na sua planilha, o app mostra)
COL_NOME = "NOME_CLIENTE"
COL_DOC = "CD_CPF_CNPJ_CLIENTE"

# Pagamentos por nível (1..4)
PAYOUT = {1: 210, 2: 345, 3: 600, 4: 810}

# Onde o app guarda os uploads (para comparar hoje vs ontem)
DATA_DIR = "data_uploads"
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------
# Helpers de formatação
# ---------------------------
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
    # retorna "MM/YYYY"
    if pd.isna(d) or d is None:
        return ""
    dd = pd.to_datetime(d, errors="coerce")
    if pd.isna(dd):
        return ""
    return dd.strftime("%m/%Y")

def fmt_int(n: int) -> str:
    return f"{int(n):,}".replace(",", ".")

def fmt_money(v: float) -> str:
    # "R$ 1.234,56"
    try:
        s = f"{float(v):,.2f}"
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {s}"
    except Exception:
        return "R$ 0,00"

def fmt_money_signed(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return sign + " " + fmt_money(abs(v))

# ---------------------------
# Funções utilitárias
# ---------------------------
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

    # Datas
    df[COL_T] = _safe_to_date(df[COL_T])
    df[COL_P] = _safe_to_date(df[COL_P])

    # Texto
    df[COL_X] = _normalize_str(df[COL_X])
    df[COL_V] = _normalize_str(df[COL_V])
    df[COL_AQ] = _normalize_str(df[COL_AQ])
    df[COL_BR] = _normalize_str(df[COL_BR])
    df[COL_CRIT] = _normalize_str(df[COL_CRIT])

    # BY como inteiro 0/1
    df[COL_BY] = pd.to_numeric(df[COL_BY], errors="coerce").fillna(0).astype(int)

    # Saldo
    df[COL_Y] = pd.to_numeric(df[COL_Y], errors="coerce").fillna(0.0)

    return df

def _pix_info(df: pd.DataFrame) -> Tuple[int, int, pd.DataFrame]:
    # Conta Pix tratando "-" como sem Pix
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

def _contas_criadas(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, int, List[dt.date]]:
    por_dia = (
        pd.Series(df[COL_T])
        .dropna()
        .value_counts()
        .sort_index()
        .rename_axis("Dia")
        .reset_index(name="Contas criadas")
    )

    t = pd.to_datetime(df[COL_T], errors="coerce")
    por_mes = (
        t.dropna()
         .dt.to_period("M")
         .astype(str)
         .value_counts()
         .sort_index()
         .rename_axis("Mês")
         .reset_index(name="Contas criadas")
    )

    total = int(pd.Series(df[COL_T]).dropna().shape[0])
    dias_list = sorted(pd.Series(df[COL_T]).dropna().unique().tolist())
    return por_dia, por_mes, total, dias_list

def _fundacoes_mes_por_dia(df: pd.DataFrame, dia: dt.date) -> pd.DataFrame:
    # filtra contas criadas no dia e resume fundação por mês/ano
    x = df[df[COL_T] == dia][[COL_T, COL_P]].dropna()
    if x.empty:
        return pd.DataFrame(columns=["Mês de fundação", "Quantidade"])

    x = x.copy()
    x["MES_FUNDACAO"] = x[COL_P].apply(fmt_month_br)
    out = (
        x["MES_FUNDACAO"]
        .replace("", "SEM FUNDAÇÃO")
        .value_counts()
        .rename_axis("Mês de fundação")
        .reset_index(name="Quantidade")
        .sort_values("Mês de fundação")
    )
    return out

def _status_counts(df: pd.DataFrame) -> pd.DataFrame:
    out = (
        df[COL_V]
        .fillna("SEM STATUS")
        .replace("", "SEM STATUS")
        .value_counts()
        .rename_axis("Status")
        .reset_index(name="Quantidade")
    )
    return out

def _domicilio_c6_count(df: pd.DataFrame) -> int:
    s = df[COL_AQ].fillna("").astype(str)
    return int(s.apply(_contains_c6).sum())

def _qualificadas(df: pd.DataFrame) -> pd.DataFrame:
    return df[df[COL_BY] == 1].copy()

def _br_counts(dfq: pd.DataFrame) -> pd.DataFrame:
    s = dfq[COL_BR].fillna("").astype(str).str.upper().str.strip()
    out = (
        s.replace("", "SEM")
         .value_counts()
         .rename_axis("Referência")
         .reset_index(name="Quantidade")
    )
    return out

def _parse_criterios_max(txt: str) -> Tuple[int, str]:
    """
    Regra: considerar SOMENTE o MAIOR valor dentre os critérios.
    Ex: CASH IN: 3 | ... | SALDO MEDIO: 4 | ... => nível=4 e critério="SALDO MEDIO"
    """
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

    # limita 0..4
    best_val = max(0, min(best_val, 4))
    return best_val, best_name

def _payout_from_max(dfq: pd.DataFrame) -> Tuple[pd.DataFrame, int, pd.DataFrame]:
    """
    Retorna:
      - tabela de pagamento por nível (1..4) usando o MAIOR critério
      - total a receber
      - tabela “critério vencedor” (qual critério foi o maior)
    """
    if dfq.empty:
        empty_tbl = pd.DataFrame(columns=["Nível", "Quantidade", "Valor unitário", "Total"])
        empty_crit = pd.DataFrame(columns=["Critério (maior)", "Quantidade"])
        return empty_tbl, 0, empty_crit

    parsed = dfq[COL_CRIT].apply(_parse_criterios_max)
    dfq2 = dfq.copy()
    dfq2["NIVEL_MAX"] = parsed.apply(lambda x: x[0])
    dfq2["CRITERIO_MAX"] = parsed.apply(lambda x: x[1])

    # só níveis 1..4 remunerados
    levels = dfq2["NIVEL_MAX"].astype(int)
    levels = levels[levels > 0]

    if levels.empty:
        empty_tbl = pd.DataFrame(columns=["Nível", "Quantidade", "Valor unitário", "Total"])
        crit_tbl = (
            dfq2["CRITERIO_MAX"]
            .replace("", "N/A")
            .value_counts()
            .rename_axis("Critério (maior)")
            .reset_index(name="Quantidade")
        )
        return empty_tbl, 0, crit_tbl

    counts = levels.value_counts().sort_index()

    rows = []
    for level, qty in counts.items():
        unit = PAYOUT.get(int(level), 0)
        total = int(qty) * int(unit)
        rows.append([int(level), int(qty), int(unit), int(total)])

    payout_tbl = pd.DataFrame(rows, columns=["Nível", "Quantidade", "Valor unitário", "Total"])

    total_payout = int(payout_tbl["Total"].sum()) if not payout_tbl.empty else 0

    crit_tbl = (
        dfq2[dfq2["NIVEL_MAX"] > 0]["CRITERIO_MAX"]
        .replace("", "N/A")
        .value_counts()
        .rename_axis("Critério (maior)")
        .reset_index(name="Quantidade")
    )

    return payout_tbl, total_payout, crit_tbl

def _sum_saldo(df: pd.DataFrame) -> float:
    return float(df[COL_Y].sum())

def _snapshot_to_disk(tag: str, file_hash: str, metrics: Dict):
    payload = {
        "tag": tag,
        "file_hash": file_hash,
        "saved_at": dt.datetime.now().isoformat(),
        "metrics": metrics,
    }

    prev_path = os.path.join(DATA_DIR, "prev.json")

    # move latest -> prev
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

# ---------------------------
# Login simples
# ---------------------------
def login_gate():
    st.sidebar.title("Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")

    if st.sidebar.button("Entrar"):
        st.session_state["logged_in"] = (u == "admin" and p == "123456")
        if not st.session_state["logged_in"]:
            st.sidebar.error("Usuário ou senha inválidos.")

    return st.session_state.get("logged_in", False)

# ---------------------------
# App
# ---------------------------
st.set_page_config(page_title="Assis & Mollerke | Visão Cliente C6", layout="wide")

# Logo + cabeçalho
logo_paths = ["assets/logo.png", "logo.png"]
logo_found = None
for p in logo_paths:
    if os.path.exists(p):
        logo_found = p
        break

top_l, top_r = st.columns([1, 3])
with top_l:
    if logo_found:
        st.image(logo_found, use_container_width=True)
with top_r:
    st.markdown("## Assis & Mollerke")
    st.caption("Visão Cliente - C6 | Envie o Excel do dia para gerar o painel e a diferença vs. ontem.")

if not login_gate():
    st.stop()

uploaded = st.file_uploader("Enviar planilha Excel (.xlsx)", type=["xlsx"])

prev, latest_saved = _load_prev_latest()

# -------------------------------------------------------
# Processa arquivo quando enviado
# -------------------------------------------------------
if uploaded:
    file_bytes = uploaded.getvalue()
    file_hash = _hash_bytes(file_bytes)

    df = _load_excel(file_bytes)
    df = _coerce_columns(df)

    # Métricas principais
    por_dia, por_mes, total_contas, dias_list = _contas_criadas(df)
    qtd_com_pix, qtd_sem_pix, pix_por_chave = _pix_info(df)
    saldo_total = _sum_saldo(df)
    status_tbl = _status_counts(df)
    qtd_c6 = _domicilio_c6_count(df)

    dfq = _qualificadas(df)
    br_counts = _br_counts(dfq)

    payout_tbl, total_payout, crit_tbl = _payout_from_max(dfq)
    total_qualificadas = int(dfq.shape[0])

    metrics = {
        "total_contas": total_contas,
        "qtd_com_pix": qtd_com_pix,
        "qtd_sem_pix": qtd_sem_pix,
        "saldo_total": saldo_total,
        "qtd_c6": qtd_c6,
        "total_qualificadas": total_qualificadas,
        "total_payout": total_payout,
    }

    _snapshot_to_disk(tag=uploaded.name, file_hash=file_hash, metrics=metrics)
    prev, latest_saved = _load_prev_latest()

# -------------------------------------------------------
# Exibe resumo com base no último salvo
# -------------------------------------------------------
if latest_saved:
    m = latest_saved["metrics"]

    st.subheader("Resumo do dia")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Contas criadas (total)", fmt_int(m["total_contas"]))
    c2.metric("Saldo total", fmt_money(m["saldo_total"]))
    c3.metric("Clientes com Pix", fmt_int(m["qtd_com_pix"]))
    c4.metric("Clientes sem Pix", fmt_int(m["qtd_sem_pix"]))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Clientes com domicílio C6", fmt_int(m["qtd_c6"]))
    c6.metric("Contas qualificadas", fmt_int(m["total_qualificadas"]))
    c7.metric("Receita estimada", fmt_money(m["total_payout"]))
    c8.metric("Arquivo", latest_saved.get("tag", "-"))

    st.subheader("Diferença vs. ontem")
    if prev and prev.get("metrics"):
        pm = prev["metrics"]
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Δ Contas criadas", f"{(m['total_contas'] - pm.get('total_contas', 0)):+d}")
        d2.metric("Δ Saldo total", fmt_money_signed(m["saldo_total"] - pm.get("saldo_total", 0.0)))
        d3.metric("Δ Contas qualificadas", f"{(m['total_qualificadas'] - pm.get('total_qualificadas', 0)):+d}")
        d4.metric("Δ Receita estimada", fmt_money_signed(m["total_payout"] - pm.get("total_payout", 0)))
        st.caption(f"Comparando: {latest_saved.get('tag')} vs. {prev.get('tag')}")
    else:
        st.info("Ainda não existe 'ontem'. Envie a planilha de dois dias diferentes para o app calcular a diferença.")

    st.divider()
    st.subheader("Relatórios detalhados")

    if not uploaded:
        st.warning("Para ver os relatórios detalhados, envie a planilha novamente nesta sessão.")
    else:
        tab1, tab2, tab3, tab4 = st.tabs([
            "Contas criadas",
            "Fundações (por dia)",
            "Pix e Status",
            "Qualificadas e Receita"
        ])

        with tab1:
            st.markdown("### Contas criadas por dia")
            tmp = por_dia.copy()
            tmp["Dia"] = tmp["Dia"].apply(fmt_date_br)
            st.dataframe(tmp, use_container_width=True, hide_index=True)

            st.markdown("### Contas criadas por mês")
            st.dataframe(por_mes, use_container_width=True, hide_index=True)

        with tab2:
            st.markdown("### Fundações por dia (somente mês/ano da fundação)")
            st.caption("Selecione um dia de abertura para ver a distribuição do mês de fundação das empresas abertas naquele dia.")

            # lista de dias disponíveis
            dias = sorted(pd.Series(df[COL_T]).dropna().unique().tolist())
            if not dias:
                st.info("Não há datas de criação para exibir.")
            else:
                dia_sel = st.selectbox(
                    "Selecione o dia de abertura",
                    options=dias,
                    format_func=lambda d: fmt_date_br(d)
                )
                fund_mes = _fundacoes_mes_por_dia(df, dia_sel)
                st.dataframe(fund_mes, use_container_width=True, hide_index=True)

        with tab3:
            st.markdown("### Pix")
            st.caption("Clientes com Pix vs. sem Pix + distribuição por tipo de chave.")

            col_a, col_b = st.columns(2)
            with col_a:
                st.metric("Com Pix", fmt_int(qtd_com_pix))
            with col_b:
                st.metric("Sem Pix", fmt_int(qtd_sem_pix))

            st.markdown("#### Distribuição por tipo de chave (somente quem tem Pix)")
            st.dataframe(pix_por_chave, use_container_width=True, hide_index=True)

            st.markdown("### Status")
            st.dataframe(status_tbl, use_container_width=True, hide_index=True)

        with tab4:
            st.markdown("### Contas qualificadas e receita")
            st.metric("Total de contas qualificadas", fmt_int(total_qualificadas))

            st.markdown("#### Referência (M0/M1/M2)")
            st.dataframe(br_counts, use_container_width=True, hide_index=True)

            st.markdown("#### Critério que gerou o nível (maior valor)")
            st.caption("Aqui você enxerga qual critério foi o maior (ex.: SALDO MEDIO) e a quantidade de vezes que ele foi o determinante.")
            st.dataframe(crit_tbl, use_container_width=True, hide_index=True)

            st.markdown("#### Tabela de receita por nível (baseada no MAIOR nível por cliente)")
            payout_show = payout_tbl.copy()
            if not payout_show.empty:
                payout_show["Valor unitário"] = payout_show["Valor unitário"].apply(lambda x: fmt_money(x))
                payout_show["Total"] = payout_show["Total"].apply(lambda x: fmt_money(x))
            st.dataframe(payout_show, use_container_width=True, hide_index=True)

            st.success(f"Receita estimada total: {fmt_money(total_payout)}")

            # visão por cliente (opcional, se colunas existirem)
            st.markdown("#### Visualização por cliente (para entender o resultado)")
            st.caption("Mostra qual foi o critério vencedor e o nível que ele atingiu para cada cliente qualificado (considerando apenas o maior valor).")

            dfq_view = dfq.copy()
            parsed = dfq_view[COL_CRIT].apply(_parse_criterios_max)
            dfq_view["Nível considerado"] = parsed.apply(lambda x: x[0])
            dfq_view["Critério considerado"] = parsed.apply(lambda x: x[1])
            dfq_view["Receita (R$)"] = dfq_view["Nível considerado"].apply(lambda n: PAYOUT.get(int(n), 0))

            cols_show = []
            if COL_DOC in dfq_view.columns:
                cols_show.append(COL_DOC)
            if COL_NOME in dfq_view.columns:
                cols_show.append(COL_NOME)

            cols_show += ["Critério considerado", "Nível considerado", "Receita (R$)", COL_CRIT]

            view = dfq_view[cols_show].copy()
            if "Receita (R$)" in view.columns:
                view["Receita (R$)"] = view["Receita (R$)"].apply(lambda x: fmt_money(x))
            st.dataframe(view, use_container_width=True, hide_index=True)

else:
    st.info("Envie a planilha Excel do dia para gerar o painel.")
