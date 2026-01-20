# ===== [imports e configs iniciais INALTERADOS] =====
import os
import io
import json
import re
import datetime as dt
from typing import Dict, Tuple, Optional

import pandas as pd
import streamlit as st

# =========================================================
# CONFIGURAÇÕES (COLUNAS)
# =========================================================
COL_CNPJ = "CD_CPF_CNPJ_CLIENTE"
COL_ABERTURA = "DT_CONTA_CRIADA"
COL_FUNDACAO = "DT_FUNDACAO_EMPRESA"
COL_PIX = "CHAVES_PIX_FORTE"
COL_SALDO = "VL_SALDO_MEDIO_MENSALIZADO"
COL_STATUS = "STATUS_CC"
COL_DOMICILIO = "BANCO_DOMICILIO"
COL_BY = "FL_QUALIFICADO_COMISS"
COL_BR = "MES_REF_COMISS"
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"

COL_LEADS_DATA = "DATA_CADASTRO"

POSSIVEIS_COL_DATA_BASE = [
    "DATA_BASE","DT_BASE","DATA_REFERENCIA","DT_REFERENCIA",
    "DATA_RELATORIO","DT_RELATORIO","DATA_ATUALIZACAO","DT_ATUALIZACAO"
]

ALVO_CONVERSAO = 0.20
HIST_START = dt.date(2026, 1, 1)

# =========================================================
# STORAGE
# =========================================================
DATA_DIR = "data_store"
os.makedirs(DATA_DIR, exist_ok=True)

HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_aberturas_diario.json")
HIST_LEADS_DAILY = os.path.join(DATA_DIR, "hist_cadastros_diario.json")
HIST_MONTH_LEVELS = os.path.join(DATA_DIR, "hist_mes_cnpj_nivel.json")
HIST_PAGO_POR_CNPJ = os.path.join(DATA_DIR, "pago_max_por_cnpj.json")
HIST_RESUMO_MENSAL = os.path.join(DATA_DIR, "resumo_mensal.json")
HIST_SNAPSHOT_MENSAL = os.path.join(DATA_DIR, "snapshot_mensal.json")

# =========================================================
# HELPERS
# =========================================================
def fmt_date(d):
    return d.strftime("%d/%m/%Y") if isinstance(d, dt.date) else ""

def read_excel_any(b):
    return pd.read_excel(io.BytesIO(b), engine="openpyxl")

def normalize_str(s):
    return s.astype("string").fillna("").str.strip()

def to_date_series(s):
    return pd.to_datetime(s, errors="coerce").dt.date

def safe_json_load(p, d):
    return json.load(open(p,"r",encoding="utf-8")) if os.path.exists(p) else d

def safe_json_save(p,o):
    json.dump(o,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)

# =========================================================
# LOGIN / TEMA / HEADER
# =========================================================
def login_gate():
    st.sidebar.markdown("### Acesso")
    u = st.sidebar.text_input("Usuário")
    p = st.sidebar.text_input("Senha", type="password")
    if st.sidebar.button("Entrar"):
        st.session_state["ok"] = (u=="admin" and p=="123456")
    return st.session_state.get("ok", False)

def apply_theme():
    st.markdown("""
    <style>
    section[data-testid="stSidebar"]{background:#0f1b3a}
    section[data-testid="stSidebar"] *{color:#fff!important}
    </style>
    """, unsafe_allow_html=True)

def show_logo_and_title():
    here = os.getcwd()   # ✅ FIX STREAMLIT CLOUD
    logo_path = os.path.join(here, "LOGO CORRETA.png")

    c1, c2 = st.columns([1,6])
    with c1:
        if os.path.exists(logo_path):
            st.image(logo_path, width=150)
    with c2:
        st.markdown("""
        <h2>Painel de controle Assis e Mollerke parceiro Banco C6</h2>
        <b>Visão Cliente + Leads + Remuneração</b>
        """, unsafe_allow_html=True)

# =========================================================
# APP
# =========================================================
st.set_page_config(layout="wide")
apply_theme()

if not login_gate():
    st.stop()

show_logo_and_title()
st.divider()

# =========================================================
# UPLOAD
# =========================================================
up_c6 = st.file_uploader("Planilha C6 diária", type=["xlsx"])

df_c6 = None
if up_c6:
    df_c6 = read_excel_any(up_c6.getvalue())
    df_c6[COL_ABERTURA] = to_date_series(df_c6.get(COL_ABERTURA))

# =========================================================
# RELATÓRIOS (DIÁRIO)
# =========================================================
st.subheader("Relatórios (diário)")

if df_c6 is None:
    st.info("Envie a planilha C6.")
else:
    tabs = st.tabs(["Aberturas"])

    with tabs[0]:
        st.markdown("#### Contas abertas por dia (arquivo)")

        por_dia = (
            df_c6[COL_ABERTURA]
            .dropna()
            .value_counts()
            .rename_axis("Dia")
            .reset_index(name="Contas abertas")
        )

        # ✅ ORDENAÇÃO CORRETA: MAIS RECENTE → MAIS ANTIGO
        por_dia = por_dia.sort_values("Dia", ascending=False)

        por_dia["Dia"] = por_dia["Dia"].apply(fmt_date)

        st.bar_chart(
            por_dia.set_index("Dia")["Contas abertas"]
        )

        st.dataframe(
            por_dia,
            use_container_width=True,
            hide_index=True
        )
