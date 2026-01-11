import os
import io
import re
import json
import hashlib
import datetime as dt
from typing import Optional, Dict, Tuple, List

import pandas as pd
import streamlit as st


# =========================
# APP / MARCA
# =========================
APP_TITLE = "Assis & Mollerke"
LOGO_PATH = "LOGO CORRETA.png"

LOGIN_USER = "admin"
LOGIN_PASS = "123456"


# =========================
# REGRAS DE REMUNERAÇÃO
# =========================
# Faixas por quantidade de qualificadas no mês (contagem do MÊS)
# <=49: 1.0
# 50..149: 1.1
# 150..349: 1.25
# >=350: 1.5
# Valores por nível (1..4) conforme faixa:
TIER_TABLE = [
    # (min_qty, max_qty, name, {level: value})
    (0, 49, "Até 49 qualificadas", {1: 140.00, 2: 230.00, 3: 400.00, 4: 540.00}),
    (50, 149, "50 a 149 qualificadas", {1: 154.00, 2: 253.00, 3: 440.00, 4: 594.00}),
    (150, 349, "150 a 349 qualificadas", {1: 175.00, 2: 287.50, 3: 500.00, 4: 675.00}),
    (350, 10**9, "350+ qualificadas", {1: 210.00, 2: 345.00, 3: 600.00, 4: 810.00}),
]

START_MONTH = "2025-11"  # você pediu a partir de Novembro/25


# =========================
# COLUNAS POSSÍVEIS (variam conforme planilha)
# =========================
COL_CNPJ_CANDIDATES = ["CD_CPF_CNPJ_CLIENTE", "CNPJ", "CPF_CNPJ", "CPF/CNPJ"]
COL_NOME_CANDIDATES = ["NOME_CLIENTE", "CLIENTE", "NOME", "RAZAO_SOCIAL"]
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"
COL_BY = "FL_QUALIFICADO_COMISS"  # se existir, filtra BY=1; se não existir, assume que já é base mensal
COL_REF_DATE_CANDIDATES = ["REFERENCIA", "DT_REFERENCIA", "MES_REFERENCIA", "DATA_REFERENCIA"]
COL_REF_TEXT_CANDIDATES = ["MES_REF_COMISS", "MES_REF", "MES"]  # às vezes vem M0/M1 etc — não é mês calendário


# =========================
# PERSISTÊNCIA
# =========================
DATA_DIR = "data_uploads"
os.makedirs(DATA_DIR, exist_ok=True)

# histórico por CNPJ: maior valor já pago (FULL) até hoje
HIST_MAXPAID = os.path.join(DATA_DIR, "hist_max_pago_por_cnpj.csv")

# resumo mensal calculado
HIST_MONTHLY = os.path.join(DATA_DIR, "hist_remuneracao_mensal.csv")

# detalhe do último processamento (para download / auditoria)
LAST_DETAIL = os.path.join(DATA_DIR, "last_remuneracao_detalhe.csv")


# =========================
# UI / CSS
# =========================
st.set_page_config(page_title=APP_TITLE, layout="wide")
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.0rem; padding-bottom: 2.0rem; }
      section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #1b2440 0%, #11182d 100%);
      }
      section[data-testid="stSidebar"] * { color: #ffffff !important; }
      .am-card {
        border: 1px solid #eef0f6;
        border-radius: 14px;
        padding: 14px 16px;
        background: #ffffff;
        box-shadow: 0 4px 18px rgba(17, 24, 45, 0.06);
      }
      .am-title { font-size: 26px; font-weight: 800; color:#1b2440; margin:0; }
      .am-sub { color:#5b6280; margin-top: 6px; }
      .am-k { font-size: 13px; opacity:.75; margin-bottom:6px; }
      .am-v { font-size: 26px; font-weight: 800; color:#1b2440; margin:0; }
      .chip { display:inline-block; padding:6px 10px; border-radius:999px; font-weight:800; font-size:12px; }
      .ok { background: rgba(30,136,229,.12); color:#1e88e5; border:1px solid rgba(30,136,229,.25); }
      .bad { background: rgba(229,57,53,.10); color:#e53935; border:1px solid rgba(229,57,53,.22); }
    </style>
    """,
    unsafe_allow_html=True
)

def card(k: str, v: str, sub: str = ""):
    st.markdown(
        f"""
        <div class="am-card">
          <div class="am-k">{k}</div>
          <p class="am-v">{v}</p>
          <div style="font-size:12px;opacity:.75;margin-top:6px;">{sub}</div>
        </div>
        """,
        unsafe_allow_html=True
    )

def br_money(v: float) -> str:
    s = f"{float(v):,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"

def br_int(n: int) -> str:
    return f"{int(n):,}".replace(",", ".")

def month_to_br(ym: str) -> str:
    # "YYYY-MM" -> "MM/YYYY"
    try:
        y, m = ym.split("-")
        return f"{m}/{y}"
    except Exception:
        return ym


# =========================
# LOGIN
# =========================
def login_gate() -> bool:
    st.sidebar.markdown("## Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")
    if st.sidebar.button("Entrar"):
        st.session_state["logged"] = (u == LOGIN_USER and p == LOGIN_PASS)
        if not st.session_state["logged"]:
            st.sidebar.error("Usuário ou senha inválidos.")
    return st.session_state.get("logged", False)

if not login_gate():
    st.stop()


# =========================
# HEADER
# =========================
c1, c2 = st.columns([1, 6])
with c1:
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, width=120)
with c2:
    st.markdown(f'<p class="am-title">{APP_TITLE}</p>', unsafe_allow_html=True)
    st.markdown('<p class="am-sub">Remuneração automática por mês (com ajuste incremental por CNPJ).</p>', unsafe_allow_html=True)

st.divider()


# =========================
# HELPERS (detectar colunas)
# =========================
def find_first_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    cols = set(df.columns.astype(str))
    for c in candidates:
        if c in cols:
            return c
    return None

def normalize_cnpj(x) -> str:
    s = str(x) if x is not None else ""
    s = re.sub(r"\D", "", s)
    return s

def parse_month_from_ref(df: pd.DataFrame) -> Optional[str]:
    """
    Tenta descobrir o mês calendário do arquivo.
    Preferência:
    - coluna REFERENCIA (date) se existir: usa YYYY-MM
    - se não tiver, tenta achar qualquer coluna de data em COL_REF_DATE_CANDIDATES
    """
    ref_col = find_first_col(df, COL_REF_DATE_CANDIDATES)
    if ref_col:
        d = pd.to_datetime(df[ref_col], errors="coerce").dropna()
        if not d.empty:
            # pega o primeiro mês
            first = d.iloc[0]
            return f"{first.year:04d}-{first.month:02d}"
    return None

def max_level_from_criteria(txt: str) -> int:
    """
    Retorna o maior número no texto:
    CASH IN: 3 | DOMICILIO: 0 | SALDO MEDIO: 4 ...
    -> 4
    """
    nums = re.findall(r":\s*(\d+)", str(txt))
    if not nums:
        return 0
    vals = [int(n) for n in nums]
    return max(vals) if vals else 0

def tier_for_qty(qty: int) -> Tuple[str, Dict[int, float]]:
    for mn, mx, name, table in TIER_TABLE:
        if mn <= qty <= mx:
            return name, table
    # fallback (nunca deveria cair aqui)
    return TIER_TABLE[0][2], TIER_TABLE[0][3]


# =========================
# HISTÓRICOS
# =========================
def load_maxpaid() -> pd.DataFrame:
    if not os.path.exists(HIST_MAXPAID):
        return pd.DataFrame(columns=["cnpj", "max_pago"])
    df = pd.read_csv(HIST_MAXPAID, dtype={"cnpj": "string"})
    df["cnpj"] = df["cnpj"].astype("string").fillna("").str.strip()
    df["max_pago"] = pd.to_numeric(df["max_pago"], errors="coerce").fillna(0.0)
    df = df[df["cnpj"] != ""].copy()
    return df

def save_maxpaid(df: pd.DataFrame):
    df = df.copy()
    df["cnpj"] = df["cnpj"].astype("string").fillna("").str.strip()
    df["max_pago"] = pd.to_numeric(df["max_pago"], errors="coerce").fillna(0.0)
    df = df[df["cnpj"] != ""].copy()
    df.to_csv(HIST_MAXPAID, index=False)

def load_monthly_hist() -> pd.DataFrame:
    if not os.path.exists(HIST_MONTHLY):
        return pd.DataFrame(columns=[
            "mes", "qualificadas", "faixa", "valor_cheio", "valor_incremental"
        ])
    df = pd.read_csv(HIST_MONTHLY, dtype={"mes": "string"})
    df["mes"] = df["mes"].astype("string").fillna("").str.strip()
    df["qualificadas"] = pd.to_numeric(df["qualificadas"], errors="coerce").fillna(0).astype(int)
    df["valor_cheio"] = pd.to_numeric(df["valor_cheio"], errors="coerce").fillna(0.0)
    df["valor_incremental"] = pd.to_numeric(df["valor_incremental"], errors="coerce").fillna(0.0)
    return df

def upsert_monthly_row(row: Dict):
    base = load_monthly_hist()
    mes = str(row["mes"])
    base = base[base["mes"] != mes].copy()
    base = pd.concat([base, pd.DataFrame([row])], ignore_index=True)
    # ordena por mês
    sort_key = pd.to_datetime(base["mes"] + "-01", errors="coerce")
    base = base.assign(_k=sort_key).sort_values("_k").drop(columns=["_k"])
    base.to_csv(HIST_MONTHLY, index=False)


# =========================
# PROCESSADOR PRINCIPAL (por arquivo)
# =========================
def process_remuneration_file(file_bytes: bytes, filename: str, forced_month: Optional[str] = None):
    df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")

    cnpj_col = find_first_col(df, COL_CNPJ_CANDIDATES)
    if not cnpj_col:
        raise ValueError("Não encontrei a coluna de CNPJ/CPF (ex.: CD_CPF_CNPJ_CLIENTE).")

    name_col = find_first_col(df, COL_NOME_CANDIDATES)

    if COL_CRIT not in df.columns:
        raise ValueError("Não encontrei a coluna CRITERIOS_ATINGIDOS_COMISS no arquivo.")

    # mês calendário
    month = forced_month or parse_month_from_ref(df)
    if not month:
        # fallback: tenta deduzir pelo nome do arquivo (NOVEMBRO2025 etc)
        # se não der, trava para não errar.
        raise ValueError("Não consegui identificar o mês (coluna REFERENCIA não encontrada).")

    # qualificada:
    # - se existir COL_BY: qualificada = BY==1 e nivel>=1
    # - se não existir: assume que o arquivo já é base mensal (mas ainda exige nivel>=1)
    if COL_BY in df.columns:
        by = pd.to_numeric(df[COL_BY], errors="coerce").fillna(0).astype(int)
        df = df[by == 1].copy()

    # nível
    df["nivel"] = df[COL_CRIT].apply(max_level_from_criteria).astype(int)
    df = df[df["nivel"] >= 1].copy()

    # normaliza cnpj
    df["cnpj"] = df[cnpj_col].apply(normalize_cnpj)
    df = df[df["cnpj"] != ""].copy()

    # pega nome
    if name_col:
        df["cliente"] = df[name_col].astype("string").fillna("").str.strip()
    else:
        df["cliente"] = ""

    # contagem do mês define faixa
    qty = int(df.shape[0])
    faixa, tabela = tier_for_qty(qty)

    # valor cheio do mês (por CNPJ) = tabela[nivel]
    df["valor_cheio"] = df["nivel"].map(tabela).fillna(0.0)

    # histórico de max pago por CNPJ (para delta incremental)
    hist = load_maxpaid()
    map_paid = dict(zip(hist["cnpj"].astype(str), hist["max_pago"].astype(float)))

    df["ja_pago_max"] = df["cnpj"].map(map_paid).fillna(0.0)
    df["a_receber"] = (df["valor_cheio"] - df["ja_pago_max"]).clip(lower=0.0)

    # atualiza max pago
    # novo max = max(antigo, valor_cheio)
    new_map = map_paid.copy()
    for c, v in zip(df["cnpj"].tolist(), df["valor_cheio"].tolist()):
        old = float(new_map.get(c, 0.0))
        new_map[c] = max(old, float(v))

    new_hist = pd.DataFrame({"cnpj": list(new_map.keys()), "max_pago": list(new_map.values())})
    save_maxpaid(new_hist)

    # resumo
    valor_cheio_total = float(df["valor_cheio"].sum())
    valor_incremental_total = float(df["a_receber"].sum())

    # distribuição por nível
    dist = (
        df["nivel"].value_counts().sort_index()
        .rename_axis("Nível")
        .reset_index(name="Quantidade")
    )
    dist["Valor unitário (faixa)"] = dist["Nível"].map(tabela).fillna(0.0)
    dist["Total (cheio)"] = dist["Nível"].map(tabela).fillna(0.0) * dist["Quantidade"]

    # salva detalhe do último processamento
    detalhe = df[["cnpj", "cliente", "nivel", "ja_pago_max", "valor_cheio", "a_receber", COL_CRIT]].copy()
    detalhe = detalhe.rename(columns={
        "cnpj": "CNPJ",
        "cliente": "Cliente",
        "nivel": "Nível (maior critério)",
        "ja_pago_max": "Já pago (máx histórico)",
        "valor_cheio": "Valor cheio (mês)",
        "a_receber": "A receber (delta)",
        COL_CRIT: "Critérios"
    })
    detalhe.to_csv(LAST_DETAIL, index=False)

    # grava/atualiza histórico mensal
    upsert_monthly_row({
        "mes": month,
        "qualificadas": qty,
        "faixa": faixa,
        "valor_cheio": valor_cheio_total,
        "valor_incremental": valor_incremental_total,
    })

    return {
        "month": month,
        "filename": filename,
        "qualificadas": qty,
        "faixa": faixa,
        "tabela": tabela,
        "valor_cheio_total": valor_cheio_total,
        "valor_incremental_total": valor_incremental_total,
        "dist": dist,
        "detalhe": detalhe
    }


# =========================
# TELA
# =========================
st.markdown("## Remuneração (Nov/25 em diante)")

st.caption(
    "Regra: (1) conta qualificada entra no mês; (2) nível = maior critério; "
    "(3) valor cheio depende da faixa do mês; (4) a receber = diferença positiva vs. maior valor já pago por CNPJ."
)

u_month = st.text_input("Mês (opcional) para forçar o processamento (formato YYYY-MM). Deixe vazio para usar a coluna REFERENCIA.", value="")

uploads = st.file_uploader(
    "Importe aqui os arquivos mensais (ex.: NOVEMBRO2025.xlsx, DEZEMBRO2025.xlsx). Você pode subir mais de um de uma vez.",
    type=["xlsx"],
    accept_multiple_files=True
)

if uploads:
    for up in uploads:
        try:
            forced = u_month.strip() if u_month.strip() else None
            result = process_remuneration_file(up.getvalue(), up.name, forced_month=forced)

            st.success(f"Processado: {up.name}  •  Mês: {month_to_br(result['month'])}  •  Faixa: {result['faixa']}")

            c1, c2, c3, c4 = st.columns(4)
            with c1:
                card("Qualificadas no mês", br_int(result["qualificadas"]))
            with c2:
                card("Valor cheio do mês", br_money(result["valor_cheio_total"]))
            with c3:
                card("A receber (incremental)", br_money(result["valor_incremental_total"]))
            with c4:
                card("Mês", month_to_br(result["month"]))

            st.markdown("### Distribuição por nível (usando a faixa do mês)")
            show = result["dist"].copy()
            show["Valor unitário (faixa)"] = show["Valor unitário (faixa)"].apply(br_money)
            show["Total (cheio)"] = show["Total (cheio)"].apply(br_money)
            st.dataframe(show, use_container_width=True, hide_index=True)

            st.markdown("### Auditoria por CNPJ (você enxerga o nível vitorioso e o delta)")
            det = result["detalhe"].copy()
            for col in ["Já pago (máx histórico)", "Valor cheio (mês)", "A receber (delta)"]:
                det[col] = det[col].apply(br_money)
            st.dataframe(det, use_container_width=True, hide_index=True)

            if os.path.exists(LAST_DETAIL):
                with open(LAST_DETAIL, "rb") as f:
                    st.download_button(
                        label="Baixar detalhe (CSV)",
                        data=f,
                        file_name=f"remuneracao_detalhe_{result['month']}.csv",
                        mime="text/csv"
                    )

            st.divider()

        except Exception as e:
            st.error(f"Erro ao processar {up.name}: {str(e)}")

st.markdown("## Histórico mensal (calculado)")
hist = load_monthly_hist()
if hist.empty:
    st.info("Ainda não há histórico. Importe NOVEMBRO/25 e DEZEMBRO/25 para começar.")
else:
    showh = hist.copy()
    showh["Mês"] = showh["mes"].apply(month_to_br)
    showh = showh.drop(columns=["mes"])
    showh["valor_cheio"] = showh["valor_cheio"].apply(br_money)
    showh["valor_incremental"] = showh["valor_incremental"].apply(br_money)
    showh = showh.rename(columns={
        "qualificadas": "Qualificadas",
        "faixa": "Faixa",
        "valor_cheio": "Valor cheio (mês)",
        "valor_incremental": "A receber (incremental)",
    })
    st.dataframe(showh[["Mês", "Qualificadas", "Faixa", "Valor cheio (mês)", "A receber (incremental)"]],
                 use_container_width=True, hide_index=True)

st.markdown("## Controle do histórico por CNPJ (máx já pago)")
with st.expander("Ver/baixar histórico por CNPJ (máx pago)"):
    h = load_maxpaid()
    if h.empty:
        st.info("Sem dados ainda.")
    else:
        hh = h.copy()
        hh["max_pago"] = hh["max_pago"].apply(br_money)
        hh = hh.rename(columns={"cnpj": "CNPJ", "max_pago": "Máx já pago"})
        st.dataframe(hh, use_container_width=True, hide_index=True)

        # download
        csv_bytes = hh.to_csv(index=False).encode("utf-8")
        st.download_button("Baixar histórico (CSV)", csv_bytes, "historico_max_pago.csv", "text/csv")
