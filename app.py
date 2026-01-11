import os
import io
import json
import hashlib
import datetime as dt
from dataclasses import dataclass
from typing import Optional, Tuple, Dict

import pandas as pd
import streamlit as st

# ---------------------------
# CONFIG: nomes das colunas
# ---------------------------
COL_T = "T"     # data criação da conta
COL_P = "P"     # data fundação
COL_X = "X"     # chave pix
COL_Y = "Y"     # saldo
COL_V = "V"     # status
COL_AQ = "AQ"   # domicílio C6
COL_BY = "BY"   # qualificada (0/1)
COL_BR = "BR"   # M0/M1/M2 ou níveis 1..4 (dependendo da sua base)

# Pagamentos por nível (1..4)
PAYOUT = {1: 210, 2: 345, 3: 600, 4: 810}

# Onde o app guarda os uploads (para comparar hoje vs ontem)
DATA_DIR = "data_uploads"
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------
# Funções utilitárias
# ---------------------------
def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _safe_to_datetime(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.date

def _normalize_str(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip()

def _contains_c6(val: str) -> bool:
    if val is None:
        return False
    return "c6" in str(val).lower()

def _load_excel(file_bytes: bytes) -> pd.DataFrame:
    # Lê a primeira planilha do Excel
    df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
    return df

def _coerce_columns(df: pd.DataFrame) -> pd.DataFrame:
    # Garante que as colunas existam (mesmo que vazias)
    for c in [COL_T, COL_P, COL_X, COL_Y, COL_V, COL_AQ, COL_BY, COL_BR]:
        if c not in df.columns:
            df[c] = pd.NA

    # Datas
    df[COL_T] = _safe_to_datetime(df[COL_T])
    df[COL_P] = _safe_to_datetime(df[COL_P])

    # Texto
    df[COL_X] = _normalize_str(df[COL_X])
    df[COL_V] = _normalize_str(df[COL_V])
    df[COL_AQ] = _normalize_str(df[COL_AQ])
    df[COL_BR] = _normalize_str(df[COL_BR])

    # BY como número 0/1
    df[COL_BY] = pd.to_numeric(df[COL_BY], errors="coerce").fillna(0).astype(int)

    # Saldo
    df[COL_Y] = pd.to_numeric(df[COL_Y], errors="coerce").fillna(0.0)

    return df

def _pix_has_key(df: pd.DataFrame) -> Tuple[int, int, pd.DataFrame]:
    has_pix = df[COL_X].notna() & (df[COL_X].astype("string") != "")
    qtd_com = int(has_pix.sum())
    qtd_sem = int((~has_pix).sum())
    por_chave = (
        df.loc[has_pix, COL_X]
          .value_counts(dropna=True)
          .rename_axis("Chave Pix")
          .reset_index(name="Quantidade")
    )
    return qtd_com, qtd_sem, por_chave

def _contas_criadas(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, int]:
    # Por dia (T)
    por_dia = (
        pd.Series(df[COL_T])
        .dropna()
        .value_counts()
        .sort_index()
        .rename_axis("Dia")
        .reset_index(name="Contas criadas")
    )
    # Por mês
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
    return por_dia, por_mes, total

def _fundacoes_por_dia(df: pd.DataFrame) -> pd.DataFrame:
    # “por dia (T), as datas de fundação (P)”
    x = df[[COL_T, COL_P]].dropna()
    if x.empty:
        return pd.DataFrame(columns=["Dia (T)", "Fundação (P)", "Quantidade"])
    out = (
        x.groupby([COL_T, COL_P], dropna=True)
         .size()
         .reset_index(name="Quantidade")
         .rename(columns={COL_T: "Dia (T)", COL_P: "Fundação (P)"})
         .sort_values(["Dia (T)", "Fundação (P)"])
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
    # conta M0/M1/M2 caso existam
    s = dfq[COL_BR].fillna("").astype(str).str.upper().str.strip()
    # se vier "M0/M1/M2"
    if s.str.startswith("M").any():
        out = (
            s.replace("", "SEM_BR")
             .value_counts()
             .rename_axis("BR")
             .reset_index(name="Quantidade")
        )
        return out

    # se vier 1..4 como texto/número
    br_num = pd.to_numeric(dfq[COL_BR], errors="coerce")
    out = (
        br_num.dropna()
              .astype(int)
              .value_counts()
              .sort_index()
              .rename_axis("Nível")
              .reset_index(name="Quantidade")
    )
    return out

def _payout_table(dfq: pd.DataFrame) -> pd.DataFrame:
    br_num = pd.to_numeric(dfq[COL_BR], errors="coerce")
    br_num = br_num.dropna().astype(int)
    if br_num.empty:
        return pd.DataFrame(columns=["Nível", "Quantidade", "Valor unitário", "Total"])

    counts = br_num.value_counts().sort_index()
    rows = []
    for level, qty in counts.items():
        unit = PAYOUT.get(int(level), 0)
        total = int(qty) * int(unit)
        rows.append([int(level), int(qty), int(unit), int(total)])
    out = pd.DataFrame(rows, columns=["Nível", "Quantidade", "Valor unitário", "Total"])
    return out

def _sum_saldo(df: pd.DataFrame) -> float:
    return float(df[COL_Y].sum())

def _snapshot_to_disk(tag: str, file_hash: str, metrics: Dict):
    payload = {
        "tag": tag,
        "file_hash": file_hash,
        "saved_at": dt.datetime.now().isoformat(),
        "metrics": metrics,
    }
    # shift latest -> prev
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

def _diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return a - b

# ---------------------------
# Login simples (usuário/senha)
# ---------------------------
def login_gate():
    st.sidebar.title("Acesso")
    users = st.secrets.get("users", {})
    auth = st.secrets.get("auth", {})

    st.sidebar.caption(auth.get("name", "Login"))

    u = st.sidebar.text_input("Usuário", value="", key="login_user")
    p = st.sidebar.text_input("Senha", value="", type="password", key="login_pass")

    if st.sidebar.button("Entrar"):
        if u in users and str(users[u]) == str(p):
            st.session_state["logged_in"] = True
            st.session_state["user"] = u
        else:
            st.session_state["logged_in"] = False
            st.sidebar.error("Usuário ou senha inválidos.")

    return st.session_state.get("logged_in", False)

# ---------------------------
# App
# ---------------------------
st.set_page_config(page_title="Visão Cliente - C6", layout="wide")

if not login_gate():
    st.stop()

st.title("Visão Cliente - C6 (App Automático)")
st.caption("Envie o Excel do dia e o sistema calcula tudo + diferença vs ontem (último arquivo enviado).")

uploaded = st.file_uploader("Enviar planilha Excel (.xlsx)", type=["xlsx"])

prev, latest_saved = _load_prev_latest()

if uploaded:
    file_bytes = uploaded.getvalue()
    file_hash = _hash_bytes(file_bytes)

    df = _load_excel(file_bytes)
    df = _coerce_columns(df)

    # Métricas
    por_dia, por_mes, total_contas = _contas_criadas(df)
    fundacoes = _fundacoes_por_dia(df)
    qtd_com_pix, qtd_sem_pix, pix_por_chave = _pix_has_key(df)
    saldo_total = _sum_saldo(df)
    status = _status_counts(df)
    qtd_c6 = _domicilio_c6_count(df)

    dfq = _qualificadas(df)
    br_counts = _br_counts(dfq)
    payout_tbl = _payout_table(dfq)
    total_payout = int(payout_tbl["Total"].sum()) if not payout_tbl.empty else 0
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

# Se não enviou ainda, tenta mostrar o último salvo
if latest_saved:
    m = latest_saved["metrics"]
    st.subheader("Resumo (Hoje)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Contas (Total)", f"{m['total_contas']:,}".replace(",", "."))
    c2.metric("Saldo total (Y)", f"{m['saldo_total']:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    c3.metric("Clientes com Pix", f"{m['qtd_com_pix']:,}".replace(",", "."))
    c4.metric("Clientes sem Pix", f"{m['qtd_sem_pix']:,}".replace(",", "."))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Domicílio C6 (AQ)", f"{m['qtd_c6']:,}".replace(",", "."))
    c6.metric("Qualificadas (BY=1)", f"{m['total_qualificadas']:,}".replace(",", "."))
    c7.metric("Total a receber", f"R$ {m['total_payout']:,}".replace(",", "."))
    c8.metric("Arquivo atual", latest_saved.get("tag", "-"))

    # Diferença vs ontem (se houver)
    st.subheader("Diferença (Hoje vs Ontem)")
    if prev and prev.get("metrics"):
        pm = prev["metrics"]
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Δ Contas", f"{_diff(m['total_contas'], pm.get('total_contas')):+,}".replace(",", "."))
        d2.metric("Δ Saldo", f"{_diff(m['saldo_total'], pm.get('saldo_total')):+,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
        d3.metric("Δ Qualificadas", f"{_diff(m['total_qualificadas'], pm.get('total_qualificadas')):+,}".replace(",", "."))
        d4.metric("Δ A receber", f"{_diff(m['total_payout'], pm.get('total_payout')):+,}".replace(",", "."))
        st.caption(f"Comparando '{latest_saved.get('tag')}' vs '{prev.get('tag')}'")
    else:
        st.info("Ainda não existe 'ontem'. Envie a planilha de dois dias diferentes para o app calcular a diferença.")

    st.divider()
    st.subheader("Detalhes e Relatórios")

    tab1, tab2, tab3, tab4 = st.tabs([
        "Contas criadas (T)",
        "Fundações por dia (T x P)",
        "Pix (X) e Status (V)",
        "Qualificadas e Remuneração (BY/BR)"
    ])

    # Recalcula detalhes do último upload salvo apenas se o usuário enviou agora
    # Para simplificar, pedimos novo upload para detalhes completos.
    if not uploaded:
        st.warning("Para ver tabelas detalhadas (gráficos/tabelas), envie a planilha novamente nesta sessão.")
    else:
        with tab1:
            st.write("**Por dia (T):**")
            st.dataframe(por_dia, use_container_width=True)
            st.write("**Por mês (T):**")
            st.dataframe(por_mes, use_container_width=True)

        with tab2:
            st.dataframe(fundacoes, use_container_width=True)

        with tab3:
            st.write("**Pix por chave (X):**")
            st.dataframe(pix_por_chave, use_container_width=True)
            st.write("**Status (V):**")
            st.dataframe(status, use_container_width=True)

        with tab4:
            st.write("**Somente qualificadas (BY=1)**")
            st.write(f"Total qualificadas: **{total_qualificadas}**")
            st.write("**Contagem BR (M0/M1/M2 ou níveis):**")
            st.dataframe(br_counts, use_container_width=True)

            st.write("**Tabela de remuneração (níveis 1..4):**")
            st.dataframe(payout_tbl, use_container_width=True)
            st.success(f"Total a receber: R$ {total_payout:,}".replace(",", "."))
else:
    st.info("Envie a planilha Excel do dia para gerar o painel.")

