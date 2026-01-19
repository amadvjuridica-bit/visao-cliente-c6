# ============================
# app.py – Painel Assis e Mollerke | Banco C6
# VERSÃO BASE GRAVADA + RECEITA LÍQUIDA RESTAURADA
# ============================

import os
import io
import json
import re
import datetime as dt
from typing import Dict, Tuple, Optional

import pandas as pd
import streamlit as st

# =========================================================
# CONFIGURAÇÕES
# =========================================================
COL_CNPJ = "CD_CPF_CNPJ_CLIENTE"
COL_ABERTURA = "DT_CONTA_CRIADA"
COL_SALDO = "VL_SALDO_MEDIO_MENSALIZADO"
COL_BY = "FL_QUALIFICADO_COMISS"
COL_BR = "MES_REF_COMISS"
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"

DATA_DIR = "data_store"
os.makedirs(DATA_DIR, exist_ok=True)

HIST_RESUMO_MENSAL = os.path.join(DATA_DIR, "resumo_mensal.json")

# =========================================================
# HELPERS
# =========================================================
def br_money(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def br_int(v: int) -> str:
    return f"{v:,}".replace(",", ".")

def safe_json_load(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

# =========================================================
# UI
# =========================================================
st.set_page_config(layout="wide", page_title="Assis e Mollerke | Banco C6")

st.title("Painel de controle Assis e Mollerke parceiro Banco C6")
st.divider()

# =========================================================
# RECEITA LÍQUIDA (RESTAURADA)
# =========================================================
st.subheader("Receita líquida – Assis e Mollerke / H1")

resumo = safe_json_load(HIST_RESUMO_MENSAL, {})

if not resumo:
    st.info("Nenhum histórico mensal disponível.")
else:
    meses = sorted(resumo.keys(), key=lambda x: int(x.split("/")[1]) * 100 + int(x.split("/")[0]))
    mes_sel = st.selectbox("Selecione o mês", meses, index=len(meses) - 1)

    info = resumo[mes_sel]

    valor_bruto = float(info.get("receber_mes", 0.0))

    # DESCONTOS
    nf_h1 = valor_bruto * 0.187
    apos_nf_h1 = valor_bruto - nf_h1

    repasse_h1 = apos_nf_h1 * 0.10
    apos_repasse = apos_nf_h1 - repasse_h1

    nf_am = apos_repasse * 0.14
    liquido_am = apos_repasse - nf_am

    deixamos_ganhar = nf_h1 + repasse_h1

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Receita bruta (mês)", br_money(valor_bruto))
    c2.metric("NF H1 (18,70%)", br_money(nf_h1))
    c3.metric("Repasse H1 (10%)", br_money(repasse_h1))
    c4.metric("Deixamos de ganhar", br_money(deixamos_ganhar))

    st.divider()

    c5, c6, c7 = st.columns(3)
    c5.metric("Base Assis & Mollerke", br_money(apos_repasse))
    c6.metric("NF Assis & Mollerke (14%)", br_money(nf_am))
    c7.metric("Líquido final Assis & Mollerke", br_money(liquido_am))

    st.caption(f"Mês selecionado: {mes_sel}")

# =========================================================
# FIM
# =========================================================
