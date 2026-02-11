import os
import io
import json
import re
import hashlib
import datetime as dt
from typing import Dict, Tuple, Optional

import pandas as pd
import streamlit as st

# =========================================================
# ✅ FIRESTORE (NUVEM)
# =========================================================
import firebase_admin
from firebase_admin import credentials, firestore


@st.cache_resource
def _get_fs_db():
    """
    Inicializa Firestore usando st.secrets["firebase"] (Streamlit Cloud).
    Cacheado para não reinicializar a cada rerun.
    """
    if "firebase" not in st.secrets:
        return None

    if not firebase_admin._apps:
        cred = credentials.Certificate(dict(st.secrets["firebase"]))
        firebase_admin.initialize_app(cred)

    return firestore.client()


def _fs_doc_id_from_path(path: str) -> str:
    return os.path.basename(path)


def _fs_load_payload(doc_id: str, default):
    db = _get_fs_db()
    if db is None:
        return default
    snap = db.collection("app_store").document(doc_id).get()
    if not snap.exists:
        return default
    data = snap.to_dict() or {}
    return data.get("payload", default)


def _fs_save_payload(doc_id: str, obj):
    db = _get_fs_db()
    if db is None:
        return
    db.collection("app_store").document(doc_id).set(
        {"payload": obj, "updated_at": firestore.SERVER_TIMESTAMP},
        merge=True
    )


def _fs_delete_doc(doc_id: str):
    db = _get_fs_db()
    if db is None:
        return
    db.collection("app_store").document(doc_id).delete()


# =========================================================
# CONFIGURAÇÕES (COLUNAS)
# =========================================================
# C6 (Visão Cliente)
COL_CNPJ = "CD_CPF_CNPJ_CLIENTE"
COL_ABERTURA = "DT_CONTA_CRIADA"
COL_FUNDACAO = "DT_FUNDACAO_EMPRESA"
COL_PIX = "CHAVES_PIX_FORTE"
COL_SALDO = "VL_SALDO_MEDIO_MENSALIZADO"
COL_CASHIN_MTD = "VL_CASH_IN_MTD"  # ✅ usado como "Saldo total (snapshot)"
COL_STATUS = "STATUS_CC"
COL_DOMICILIO = "BANCO_DOMICILIO"
COL_BY = "FL_QUALIFICADO_COMISS"
COL_BR = "MES_REF_COMISS"  # M0/M1/M2
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"

# Leads (cadastros) – coluna M (13ª col) como fallback
COL_LEADS_DATA = "DATA_CADASTRO"

# Possíveis colunas para detectar "data base"
POSSIVEIS_COL_DATA_BASE = [
    "DATA_BASE", "DT_BASE", "DATA_REFERENCIA", "DT_REFERENCIA",
    "DATA_RELATORIO", "DT_RELATORIO", "DATA_ATUALIZACAO", "DT_ATUALIZACAO"
]

# Conversão
ALVO_CONVERSAO = 0.20

# A partir de Jan/26 salvar histórico diário
HIST_START = dt.date(2026, 1, 1)

# =========================================================
# REMUNERAÇÃO (FAIXAS)
# =========================================================
FAIXAS = [
    (0,   "Até 49 (1.0)",   {1: 140.00, 2: 230.00, 3: 400.00, 4: 540.00}),
    (50,  "50+ (1.1)",      {1: 154.00, 2: 253.00, 3: 440.00, 4: 594.00}),
    (150, "150+ (1.25)",    {1: 175.00, 2: 287.50, 3: 500.00, 4: 675.00}),
    (350, "350+ (1.5)",     {1: 210.00, 2: 345.00, 3: 600.00, 4: 810.00}),
]

# =========================================================
# MEMÓRIA / STORAGE
# =========================================================
DATA_DIR = "data_store"
os.makedirs(DATA_DIR, exist_ok=True)

HIST_OPEN_DAILY = os.path.join(DATA_DIR, "hist_aberturas_diario.json")        # dd/mm/aaaa -> aberturas
HIST_LEADS_DAILY = os.path.join(DATA_DIR, "hist_cadastros_diario.json")       # dd/mm/aaaa -> cadastradas
HIST_MONTH_LEVELS = os.path.join(DATA_DIR, "hist_mes_cnpj_nivel.json")        # mm/aaaa -> {cnpj: nivel_max_no_mes}
HIST_PAGO_POR_CNPJ = os.path.join(DATA_DIR, "pago_max_por_cnpj.json")         # cnpj -> max pago acumulado
HIST_RESUMO_MENSAL = os.path.join(DATA_DIR, "resumo_mensal.json")             # mm/aaaa -> resumo calculado
HIST_SNAPSHOT_MENSAL = os.path.join(DATA_DIR, "snapshot_mensal.json")         # mm/aaaa -> estado (saldo/pix/domicilio/qualificadas)
HIST_COMPARE_DAILY = os.path.join(DATA_DIR, "hist_comparativo_diario.json")   # dd/mm/aaaa -> métricas do dia

# ✅ META (persistência incremental só de resumo)
META_STORE_PATH = os.path.join(DATA_DIR, "meta_c6_store.json")
# ✅ LEADS STATUS (coluna Q) (persistência incremental)
LEADS_STATUS_STORE_PATH = os.path.join(DATA_DIR, "leads_status_q_store.json")


# =========================================================
# HELPERS
# =========================================================
def safe_json_load(path: str, default):
    """
    ✅ Se existir st.secrets["firebase"], lê do Firestore.
    Caso contrário, mantém comportamento local.
    """
    if "firebase" in st.secrets:
        return _fs_load_payload(_fs_doc_id_from_path(path), default)

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def safe_json_save(path: str, obj):
    """
    ✅ Se existir st.secrets["firebase"], salva no Firestore.
    Caso contrário, mantém comportamento local.
    """
    if "firebase" in st.secrets:
        _fs_save_payload(_fs_doc_id_from_path(path), obj)
        return

    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def br_money(v: float) -> str:
    s = f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def br_int(n: int) -> str:
    return f"{int(n):,}".replace(",", ".")


def fmt_date(d) -> str:
    if d is None or pd.isna(d):
        return ""
    if isinstance(d, pd.Timestamp):
        d = d.date()
    if isinstance(d, dt.datetime):
        d = d.date()
    if not isinstance(d, dt.date):
        return ""
    return d.strftime("%d/%m/%Y")


def fmt_month(d: dt.date) -> str:
    return d.strftime("%m/%Y")


def month_first(d: dt.date) -> dt.date:
    return dt.date(d.year, d.month, 1)


def month_key_str(m: str) -> int:
    try:
        mm, aa = m.split("/")
        return int(aa) * 100 + int(mm)
    except Exception:
        return 0


def to_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.date


def normalize_str(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").str.strip()


def read_excel_any(file_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")


def contains_c6(x) -> bool:
    if x is None or pd.isna(x):
        return False
    return "c6" in str(x).lower()


# =========================================================
# ✅ DETECTAR DATA_BASE (DIA) DO ARQUIVO
# =========================================================
def detect_report_day_from_df(df: pd.DataFrame) -> Optional[dt.date]:
    """
    Prioridade:
      1) DATA_BASE (ou equivalentes)
      2) fallback: maior data em DT_CONTA_CRIADA (se existir)
    Retorna dt.date (dia).
    """
    for c in POSSIVEIS_COL_DATA_BASE:
        if c in df.columns:
            d = to_date_series(df[c]).dropna()
            if len(d) > 0:
                m = d.mode()
                if len(m) > 0:
                    return m.iloc[0]
                return max(d)

    if COL_ABERTURA in df.columns:
        d = to_date_series(df[COL_ABERTURA]).dropna()
        if len(d) > 0:
            return max(d)

    return None


# =========================================================
# DETECÇÃO DO MÊS DO RELATÓRIO (mês do arquivo)
# =========================================================
def detect_report_month_from_df(df: pd.DataFrame) -> Optional[dt.date]:
    for c in POSSIVEIS_COL_DATA_BASE:
        if c in df.columns:
            d = to_date_series(df[c]).dropna()
            if len(d) > 0:
                m = pd.Series([dt.date(x.year, x.month, 1) for x in d]).mode()
                if len(m) > 0:
                    return m.iloc[0]

    if COL_ABERTURA in df.columns:
        d = to_date_series(df[COL_ABERTURA]).dropna()
        if len(d) > 0:
            mx = max(d)
            return dt.date(mx.year, mx.month, 1)

    return None


# =========================================================
# QUALIFICAÇÃO (NÍVEL)
# =========================================================
def parse_level_from_criterios(txt: str) -> int:
    nums = [int(n) for n in re.findall(r":\s*(\d+)", str(txt or ""))]
    if not nums:
        return 0
    m = max(nums)
    if m < 1:
        return 0
    return min(m, 4)


def parse_level(df: pd.DataFrame) -> pd.Series:
    by_raw = df.get(COL_BY, pd.Series([None] * len(df)))
    by_num = pd.to_numeric(by_raw, errors="coerce").fillna(0).astype(int)
    level_by = by_num.where(by_num.between(1, 4), 0)

    crit_raw = df.get(COL_CRIT, pd.Series([""] * len(df))).astype("string").fillna("")
    level_crit = crit_raw.apply(parse_level_from_criterios).astype(int)

    lvl = pd.concat([level_by, level_crit], axis=1).max(axis=1).astype(int)
    return lvl.where(lvl.between(1, 4), 0)


def criterio_vencedor(txt: str) -> str:
    txt = str(txt or "").strip()
    if not txt:
        return ""
    parts = [p.strip() for p in txt.split("|")]
    best_name, best_val = "", 0
    for p in parts:
        m = re.search(r"(.+):\s*(\d+)", p)
        if m:
            nome = m.group(1).strip()
            val = int(m.group(2))
            if val > best_val:
                best_name, best_val = nome, val
    if best_val <= 0:
        return ""
    return f"{best_name} ({best_val})"


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
# HISTÓRICO DIÁRIO (SALVA SOMENTE O QUE EXISTE NO ARQUIVO)
# =========================================================
def daily_upsert_many(path: str, counts: Dict[str, int]):
    base = safe_json_load(path, default={})
    for k, v in counts.items():
        base[k] = int(v)
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
    return pd.DataFrame(rows, columns=["Data", colname])


# =========================================================
# HISTÓRICO COMPARATIVO (UPsert + TABELA COM Δ)
# =========================================================
def compare_daily_upsert(day_key: str, payload: dict):
    base = safe_json_load(HIST_COMPARE_DAILY, default={})
    base[day_key] = payload
    safe_json_save(HIST_COMPARE_DAILY, base)


def compare_daily_df() -> pd.DataFrame:
    base = safe_json_load(HIST_COMPARE_DAILY, default={})
    rows = []
    for k, v in base.items():
        try:
            d = dt.datetime.strptime(k, "%d/%m/%Y").date()
        except Exception:
            continue
        if d < HIST_START:
            continue
        v = v or {}
        rows.append({
            "_date": d,
            "Data base": k,
            "Contas (C6) total": int(v.get("c6_total", 0)),
            "Leads total": int(v.get("leads_total", 0)),
            "Qualificadas total": int(v.get("qual_total", 0)),
            "Qualificadas M0": int(v.get("qual_m0", 0)),
            "Qualificadas M1": int(v.get("qual_m1", 0)),
            "Qualificadas M2": int(v.get("qual_m2", 0)),
            "Chaves Pix total": int(v.get("pix_total", 0)),
            "Saldo total (VL_CASH_IN_MTD)": float(v.get("cashin_total", 0.0)),
            "Base (A receber no mês)": float(v.get("base_receber_mes", 0.0)),
            "_mes_ref": v.get("mes_ref", "")
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values("_date", ascending=True).reset_index(drop=True)

    for col in [
        "Contas (C6) total",
        "Leads total",
        "Qualificadas total",
        "Qualificadas M0",
        "Qualificadas M1",
        "Qualificadas M2",
        "Chaves Pix total",
        "Saldo total (VL_CASH_IN_MTD)",
        "Base (A receber no mês)",
    ]:
        df[f"Δ {col}"] = df[col].diff().fillna(0)

    df = df.sort_values("_date", ascending=False).reset_index(drop=True)

    df["Saldo total (VL_CASH_IN_MTD)"] = df["Saldo total (VL_CASH_IN_MTD)"].apply(br_money)
    df["Δ Saldo total (VL_CASH_IN_MTD)"] = df["Δ Saldo total (VL_CASH_IN_MTD)"].apply(br_money)

    df["Base (A receber no mês)"] = df["Base (A receber no mês)"].apply(br_money)
    df["Δ Base (A receber no mês)"] = df["Δ Base (A receber no mês)"].apply(br_money)

    for c in [
        "Contas (C6) total",
        "Leads total",
        "Qualificadas total",
        "Qualificadas M0",
        "Qualificadas M1",
        "Qualificadas M2",
        "Chaves Pix total",
        "Δ Contas (C6) total",
        "Δ Leads total",
        "Δ Qualificadas total",
        "Δ Qualificadas M0",
        "Δ Qualificadas M1",
        "Δ Qualificadas M2",
        "Δ Chaves Pix total",
    ]:
        df[c] = df[c].apply(br_int)

    df = df[[
        "Data base", "_mes_ref",
        "Contas (C6) total", "Δ Contas (C6) total",
        "Leads total", "Δ Leads total",
        "Qualificadas total", "Δ Qualificadas total",
        "Qualificadas M0", "Δ Qualificadas M0",
        "Qualificadas M1", "Δ Qualificadas M1",
        "Qualificadas M2", "Δ Qualificadas M2",
        "Chaves Pix total", "Δ Chaves Pix total",
        "Saldo total (VL_CASH_IN_MTD)", "Δ Saldo total (VL_CASH_IN_MTD)",
        "Base (A receber no mês)", "Δ Base (A receber no mês)"
    ]].rename(columns={"_mes_ref": "Mês ref (remuneração)"})

    return df


# =========================================================
# MENSAL POR CNPJ (NÍVEL MÁXIMO NO MÊS) - A PARTIR DO DIÁRIO
# =========================================================
def month_levels_upsert_from_daily_df(df_c6: pd.DataFrame):
    store = safe_json_load(HIST_MONTH_LEVELS, default={})

    mes_rel = detect_report_month_from_df(df_c6)
    if mes_rel is None:
        return

    if mes_rel < dt.date(2026, 1, 1):
        return

    mkey = fmt_month(mes_rel)

    df = df_c6.copy()

    if COL_CNPJ not in df.columns:
        cand = [c for c in df.columns if "CNPJ" in str(c).upper()]
        df[COL_CNPJ] = df[cand[0]] if cand else ""

    df["_cnpj"] = normalize_str(df[COL_CNPJ]).str.replace(r"\D", "", regex=True)
    df["_nivel"] = parse_level(df)

    q = df[(df["_cnpj"] != "") & (df["_nivel"] >= 1)].copy()
    month_map: Dict[str, int] = store.get(mkey, {}) or {}

    if not q.empty:
        by_cnpj = q.groupby("_cnpj")["_nivel"].max().reset_index()
        for _, r in by_cnpj.iterrows():
            cnpj = str(r["_cnpj"])
            lvl = int(r["_nivel"])
            prev = int(month_map.get(cnpj, 0))
            if lvl > prev:
                month_map[cnpj] = lvl

    store[mkey] = month_map
    safe_json_save(HIST_MONTH_LEVELS, store)


# =========================================================
# IMPORTAÇÃO MENSAL (EXCEÇÃO NOV/25 e DEZ/25) - SEED
# =========================================================
def detect_month_from_filename(name: str) -> Optional[dt.date]:
    n = name.upper()
    if "NOVEMBRO2025" in n or "NOV/2025" in n or "NOV_2025" in n or "NOV-2025" in n:
        return dt.date(2025, 11, 1)
    if "DEZEMBRO2025" in n or "DEZ/2025" in n or "DEZ_2025" in n or "DEZ-2025" in n:
        return dt.date(2025, 12, 1)
    return None


def month_levels_upsert_from_monthly_file(file_name: str, file_bytes: bytes):
    df = read_excel_any(file_bytes)

    m = None
    if COL_ABERTURA in df.columns:
        d = to_date_series(df[COL_ABERTURA]).dropna()
        if len(d) > 0:
            mm = pd.Series([dt.date(x.year, x.month, 1) for x in d]).mode()
            if len(mm) > 0:
                m = mm.iloc[0]
    if m is None:
        m = detect_month_from_filename(file_name)
    if m is None:
        return

    store = safe_json_load(HIST_MONTH_LEVELS, default={})
    mkey = fmt_month(m)
    month_map: Dict[str, int] = store.get(mkey, {}) or {}

    if COL_CNPJ not in df.columns:
        cand = [c for c in df.columns if "CNPJ" in str(c).upper()]
        df[COL_CNPJ] = df[cand[0]] if cand else ""

    df["_cnpj"] = normalize_str(df[COL_CNPJ]).str.replace(r"\D", "", regex=True)
    df["_nivel"] = parse_level(df)

    q = df[(df["_cnpj"] != "") & (df["_nivel"] >= 1)].copy()
    if not q.empty:
        by_cnpj = q.groupby("_cnpj")["_nivel"].max().reset_index()
        for _, r in by_cnpj.iterrows():
            cnpj = str(r["_cnpj"])
            lvl = int(r["_nivel"])
            prev = int(month_map.get(cnpj, 0))
            if lvl > prev:
                month_map[cnpj] = lvl

    store[mkey] = month_map
    safe_json_save(HIST_MONTH_LEVELS, store)


# =========================================================
# FAIXA
# =========================================================
def faixa_por_qtd(qtd_qualificadas: int) -> Tuple[str, Dict[int, float]]:
    chosen_name, chosen_tbl = FAIXAS[0][1], FAIXAS[0][2]
    for min_q, nm, tbl in FAIXAS:
        if qtd_qualificadas >= min_q:
            chosen_name, chosen_tbl = nm, tbl
    return chosen_name, chosen_tbl


def faixa_tbl_por_nome(nome: str) -> Dict[int, float]:
    for _, nm, tbl in FAIXAS:
        if nm == nome:
            return tbl
    return FAIXAS[0][2]


# =========================================================
# RECOMPUTE INCREMENTAL (SEM CRIAR MESES)
# =========================================================
def recompute_incremental() -> pd.DataFrame:
    month_levels = safe_json_load(HIST_MONTH_LEVELS, default={})
    months = sorted(list(month_levels.keys()), key=month_key_str)

    paid_max: Dict[str, float] = {}
    resumo: Dict[str, dict] = {}

    rows = []
    for mkey in months:
        cmap: Dict[str, int] = month_levels.get(mkey, {}) or {}
        cmap = {k: int(v) for k, v in cmap.items() if str(k).strip() != ""}

        qtd_qual = len(cmap)

        if mkey == "12/2025":
            faixa_nome, precos = FAIXAS[-1][1], FAIXAS[-1][2]
        else:
            faixa_nome, precos = faixa_por_qtd(qtd_qual)

        lvl_counts = {1: 0, 2: 0, 3: 0, 4: 0}
        for _, lvl in cmap.items():
            if lvl in lvl_counts:
                lvl_counts[lvl] += 1

        total_cheio = 0.0
        total_receber = 0.0

        for cnpj, lvl in cmap.items():
            cheio = float(precos.get(int(lvl), 0.0))
            prev = float(paid_max.get(cnpj, 0.0))
            diff = cheio - prev
            if diff < 0:
                diff = 0.0

            total_cheio += cheio
            total_receber += diff
            paid_max[cnpj] = max(prev, cheio)

        ja_pago_ref = total_cheio - total_receber

        resumo[mkey] = {
            "faixa": faixa_nome,
            "qualificadas": qtd_qual,
            "n1": lvl_counts[1],
            "n2": lvl_counts[2],
            "n3": lvl_counts[3],
            "n4": lvl_counts[4],
            "deveria_receber": total_cheio,
            "ja_pago_ref": ja_pago_ref,
            "receber_mes": total_receber,
        }

        rows.append([
            mkey, faixa_nome, qtd_qual,
            lvl_counts[1], lvl_counts[2], lvl_counts[3], lvl_counts[4],
            total_cheio, ja_pago_ref, total_receber
        ])

    safe_json_save(HIST_PAGO_POR_CNPJ, paid_max)
    safe_json_save(HIST_RESUMO_MENSAL, resumo)

    return pd.DataFrame(
        rows,
        columns=[
            "Mês", "Faixa", "Qualificadas",
            "Nível 1", "Nível 2", "Nível 3", "Nível 4",
            "Deveria receber (cheio)", "Já pago (referência)", "A receber no mês"
        ],
    )


# =========================================================
# LOGIN / TEMA / HEADER
# =========================================================
def apply_theme():
    st.markdown(
        """
        <style>
            section[data-testid="stSidebar"]{ background:#0f1b3a; }
            section[data-testid="stSidebar"] * { color:#ffffff !important; }

            /* ✅ Botões do sidebar: visíveis (não ficam "brancos") */
            section[data-testid="stSidebar"] div.stButton > button{
                width:100%;
                border-radius:12px;
                border:1px solid rgba(255,255,255,0.28);
                background: rgba(255,255,255,0.14);
                color:#ffffff !important;
                font-weight:900;
                padding:10px 12px;
            }
            section[data-testid="stSidebar"] div.stButton > button:hover{
                background:#007AFF !important;
                border-color:#007AFF !important;
                color:#ffffff !important;
            }

            div[data-testid="stMetric"]{
                background:#ffffff;
                border:1px solid #e9eef7;
                border-radius:14px;
                padding:12px 14px;
                box-shadow:0 2px 10px rgba(15,27,58,0.05);
            }

            h1,h2,h3{ color:#0f1b3a; }

            .am-badge-ok{
                display:inline-block; padding:4px 10px; border-radius:999px;
                background:rgba(0,122,255,0.12); color:#007AFF;
                font-weight:900; font-size:12px;
            }
            .am-badge-bad{
                display:inline-block; padding:4px 10px; border-radius:999px;
                background:rgba(255,59,48,0.12); color:#FF3B30;
                font-weight:900; font-size:12px;
            }

            .am-small-note{ color:#5b6b8c; font-weight:700; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _logo_path() -> str:
    here = os.getcwd()
    return os.path.join(here, "LOGO CORRETA.png")


def login_gate() -> bool:
    # ✅ Logo grande antes de acessar
    lp = _logo_path()
    if os.path.exists(lp):
        st.image(lp, width=420)
        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

    st.sidebar.markdown("### Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")
    if st.sidebar.button("Entrar"):
        st.session_state["logged_in"] = (u == "admin" and p == "123456")
        if not st.session_state["logged_in"]:
            st.sidebar.error("Usuário ou senha inválidos.")
    return st.session_state.get("logged_in", False)


def show_logo_and_title():
    logo_path = _logo_path()

    c1, c2 = st.columns([1, 6], vertical_alignment="center")
    with c1:
        if os.path.exists(logo_path):
            st.image(logo_path, width=160)
        else:
            st.warning("Logo não encontrada. Coloque 'LOGO CORRETA.png' na raiz do projeto.")
    with c2:
        st.markdown(
            """
            <div style="line-height:1.1">
              <div style="font-size:28px;font-weight:900;color:#0f1b3a;margin-bottom:4px;">
                Painel de controle Assis e Mollerke parceiro Banco C6
              </div>
              <div style="color:#5b6b8c;font-weight:700;">
                Visão Cliente + Leads + Remuneração
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# =========================================================
# RESETS
# (✅ Mantém só o que já existia: reset total no sidebar
#  e reset específico dentro do Leads Status Diário)
# =========================================================
def reset_all_data():
    for p in [
        HIST_OPEN_DAILY, HIST_LEADS_DAILY, HIST_MONTH_LEVELS,
        HIST_PAGO_POR_CNPJ, HIST_RESUMO_MENSAL, HIST_SNAPSHOT_MENSAL,
        HIST_COMPARE_DAILY,
        META_STORE_PATH,
        LEADS_STATUS_STORE_PATH,
    ]:
        if "firebase" in st.secrets:
            _fs_delete_doc(_fs_doc_id_from_path(p))
        if os.path.exists(p):
            os.remove(p)


def reset_only_leads_status():
    if "firebase" in st.secrets:
        _fs_delete_doc(_fs_doc_id_from_path(LEADS_STATUS_STORE_PATH))
    if os.path.exists(LEADS_STATUS_STORE_PATH):
        os.remove(LEADS_STATUS_STORE_PATH)


# =========================================================
# APP
# =========================================================
st.set_page_config(page_title="Assis e Mollerke | Banco C6", layout="wide")
apply_theme()

if not login_gate():
    st.stop()

st.sidebar.markdown("---")
if st.sidebar.button("RESETAR HISTÓRICO (ZERAR TUDO)"):
    reset_all_data()
    st.sidebar.success("Histórico resetado. Reimporte Nov/25 e Dez/25 (se quiser) e depois os diários.")

show_logo_and_title()
st.divider()

# =========================================================
# ✅ 3 PÁGINAS
# =========================================================
page = st.tabs(["📊 Painel C6", "📢 Campanhas Meta – C6", "🧾 Leads — Status diário (coluna Q)"])

# =========================================================
# ======================== PÁGINA 1 =======================
# =========================================================
with page[0]:
    st.subheader("Importação diária (Janeiro/26 em diante)")

    colA, colB = st.columns(2)
    with colA:
        up_c6 = st.file_uploader("Planilha C6 (Visão Cliente) — diária (.xlsx)", type=["xlsx"], key="c6")
    with colB:
        up_leads = st.file_uploader("Planilha Leads — diária (.xlsx)", type=["xlsx"], key="leads")

    st.subheader("Importação mensal (exceção: Nov/25 e Dez/25)")
    up_monthly = st.file_uploader(
        "Envie Nov/25 e Dez/25 (apenas se precisar iniciar histórico antigo)",
        type=["xlsx"],
        accept_multiple_files=True,
        key="monthly",
    )

    if up_monthly and len(up_monthly) > 0:
        for f in up_monthly:
            month_levels_upsert_from_monthly_file(f.name, f.getvalue())

    df_c6 = None
    df_leads = None

    _cmp_day: Optional[dt.date] = None
    _cmp_mes_ref: str = ""
    _cmp_c6_total = None
    _cmp_leads_total = None
    _cmp_qual_total = None

    _cmp_qual_m0 = None
    _cmp_qual_m1 = None
    _cmp_qual_m2 = None

    _cmp_pix_total = None
    _cmp_cashin_total = None

    if up_c6:
        df_c6 = read_excel_any(up_c6.getvalue())

        if COL_ABERTURA not in df_c6.columns:
            df_c6[COL_ABERTURA] = pd.NA
        if COL_FUNDACAO not in df_c6.columns:
            df_c6[COL_FUNDACAO] = pd.NA
        if COL_SALDO not in df_c6.columns:
            df_c6[COL_SALDO] = 0.0
        if COL_CASHIN_MTD not in df_c6.columns:
            df_c6[COL_CASHIN_MTD] = 0.0
        if COL_BR not in df_c6.columns:
            df_c6[COL_BR] = ""
        if COL_CRIT not in df_c6.columns:
            df_c6[COL_CRIT] = ""
        if COL_BY not in df_c6.columns:
            df_c6[COL_BY] = ""

        df_c6[COL_ABERTURA] = to_date_series(df_c6[COL_ABERTURA])
        df_c6[COL_FUNDACAO] = to_date_series(df_c6[COL_FUNDACAO])
        df_c6[COL_SALDO] = pd.to_numeric(df_c6[COL_SALDO], errors="coerce").fillna(0.0)
        df_c6[COL_CASHIN_MTD] = pd.to_numeric(df_c6[COL_CASHIN_MTD], errors="coerce").fillna(0.0)
        df_c6[COL_BR] = normalize_str(df_c6[COL_BR]).str.upper()
        df_c6[COL_CRIT] = normalize_str(df_c6[COL_CRIT])

        opened_counts = (
            df_c6[df_c6[COL_ABERTURA].notna()]
            .assign(_d=df_c6[COL_ABERTURA])
            .query("_d >= @HIST_START")
            .groupby("_d")
            .size()
            .to_dict()
        )
        opened_counts = {fmt_date(k): int(v) for k, v in opened_counts.items()}
        if opened_counts:
            daily_upsert_many(HIST_OPEN_DAILY, opened_counts)

        month_levels_upsert_from_daily_df(df_c6)

        mes_rel = detect_report_month_from_df(df_c6)
        if mes_rel and mes_rel >= dt.date(2026, 1, 1):
            mkey = fmt_month(mes_rel)

            df_tmp = df_c6.copy()
            df_tmp["_nivel"] = parse_level(df_tmp)

            pix_com, pix_sem, _ = pix_summary(df_tmp)
            domicilio_c6 = int(df_tmp.get(COL_DOMICILIO, pd.Series([""] * len(df_tmp))).apply(contains_c6).sum())
            qualificadas = int((df_tmp["_nivel"] >= 1).sum())

            saldo_total = float(df_tmp[COL_CASHIN_MTD].sum())

            snap = safe_json_load(HIST_SNAPSHOT_MENSAL, default={})
            snap[mkey] = {
                "saldo_total": saldo_total,
                "pix_com": pix_com,
                "pix_sem": pix_sem,
                "domicilio_c6": domicilio_c6,
                "qualificadas_arquivo": qualificadas,
                "arquivo_c6": up_c6.name if up_c6 else "",
            }
            safe_json_save(HIST_SNAPSHOT_MENSAL, snap)

        _cmp_day = detect_report_day_from_df(df_c6)
        _cmp_mes_ref = fmt_month(mes_rel) if mes_rel else ""
        _cmp_c6_total = int(len(df_c6))

        dfq_tmp = df_c6.copy()
        dfq_tmp["_nivel"] = parse_level(dfq_tmp)
        _cmp_qual_total = int((dfq_tmp["_nivel"] >= 1).sum())

        br_tmp = normalize_str(dfq_tmp.get(COL_BR, pd.Series([""] * len(dfq_tmp)))).str.upper()
        qmask = dfq_tmp["_nivel"] >= 1
        _cmp_qual_m0 = int((qmask & (br_tmp == "M0")).sum())
        _cmp_qual_m1 = int((qmask & (br_tmp == "M1")).sum())
        _cmp_qual_m2 = int((qmask & (br_tmp == "M2")).sum())

        s_pix = normalize_str(df_c6.get(COL_PIX, pd.Series([""] * len(df_c6)))).str.upper()
        s_pix = s_pix.str.replace("'", "", regex=False)
        has_pix = ~s_pix.isin(["", "-", "NAN", "NONE", "SEM", "SEM PIX"])
        _cmp_pix_total = int(has_pix.sum())

        _cmp_cashin_total = float(df_c6[COL_CASHIN_MTD].sum())

    if up_leads:
        df_leads = read_excel_any(up_leads.getvalue())

        if COL_LEADS_DATA not in df_leads.columns:
            cand = [c for c in df_leads.columns if "CADAST" in str(c).upper() and "DATA" in str(c).upper()]
            if cand:
                df_leads[COL_LEADS_DATA] = df_leads[cand[0]]
            else:
                if len(df_leads.columns) >= 13:
                    df_leads[COL_LEADS_DATA] = df_leads.iloc[:, 12]
                else:
                    df_leads[COL_LEADS_DATA] = pd.NA

        df_leads[COL_LEADS_DATA] = to_date_series(df_leads[COL_LEADS_DATA])

        leads_counts = (
            df_leads[df_leads[COL_LEADS_DATA].notna()]
            .assign(_d=df_leads[COL_LEADS_DATA])
            .query("_d >= @HIST_START")
            .groupby("_d")
            .size()
            .to_dict()
        )
        leads_counts = {fmt_date(k): int(v) for k, v in leads_counts.items()}
        if leads_counts:
            daily_upsert_many(HIST_LEADS_DAILY, leads_counts)

        _cmp_leads_total = int(len(df_leads))
        if _cmp_day is None:
            _cmp_day = detect_report_day_from_df(df_leads)

    st.divider()

    _ = recompute_incremental()
    saved_resumo = safe_json_load(HIST_RESUMO_MENSAL, default={})

    if _cmp_day and _cmp_day >= HIST_START:
        day_key = fmt_date(_cmp_day)

        base_receber_mes = 0.0
        if _cmp_mes_ref and saved_resumo:
            base_receber_mes = float(saved_resumo.get(_cmp_mes_ref, {}).get("receber_mes", 0.0))

        compare_daily_upsert(day_key, {
            "mes_ref": _cmp_mes_ref,
            "c6_total": int(_cmp_c6_total or 0),
            "leads_total": int(_cmp_leads_total or 0),
            "qual_total": int(_cmp_qual_total or 0),
            "qual_m0": int(_cmp_qual_m0 or 0),
            "qual_m1": int(_cmp_qual_m1 or 0),
            "qual_m2": int(_cmp_qual_m2 or 0),
            "pix_total": int(_cmp_pix_total or 0),
            "cashin_total": float(_cmp_cashin_total or 0.0),
            "base_receber_mes": float(base_receber_mes),
        })

    st.subheader("Comparativo diário (diferenças vs dia anterior)")

    df_cmp = compare_daily_df()
    if df_cmp.empty:
        st.info("Importe C6 e/ou Leads com DATA_BASE para começar o comparativo diário.")
    else:
        st.dataframe(df_cmp, use_container_width=True, hide_index=True)

    st.divider()

    st.subheader("Resumo executivo (mês)")

    hist_open = hist_to_df(HIST_OPEN_DAILY, "Abertas")
    hist_leads = hist_to_df(HIST_LEADS_DAILY, "Cadastradas")

    if hist_open.empty or hist_leads.empty:
        st.info("Importe C6 + Leads (diário) para montar o mês.")
    else:
        base = pd.merge(hist_leads, hist_open, on="Data", how="outer").fillna(0)
        base["Abertas"] = base["Abertas"].astype(int)
        base["Cadastradas"] = base["Cadastradas"].astype(int)
        base["Mes_ref"] = base["Data"].map(month_first)

        meses = sorted(base["Mes_ref"].unique())
        mes_atual = meses[-1]
        mes_lbl = fmt_month(mes_atual)

        mes_df = base[base["Mes_ref"] == mes_atual].copy()
        total_ab_mes = int(mes_df["Abertas"].sum())
        total_cad_mes = int(mes_df["Cadastradas"].sum())
        perc_mes = (total_ab_mes / total_cad_mes) if total_cad_mes > 0 else 0.0

        badge = "am-badge-ok" if perc_mes >= ALVO_CONVERSAO else "am-badge-bad"
        st.markdown(
            f"<div class='{badge}'>Conversão do mês: {str(round(perc_mes*100,1)).replace('.',',')}%</div>",
            unsafe_allow_html=True
        )

        snap = safe_json_load(HIST_SNAPSHOT_MENSAL, default={})
        s = snap.get(mes_lbl, {})

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Mês", mes_lbl)
        c2.metric("Cadastradas (mês)", br_int(total_cad_mes))
        c3.metric("Abertas (mês)", br_int(total_ab_mes))
        c4.metric("% geral (mês)", f"{str(round(perc_mes*100,1)).replace('.',',')}%")

        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Saldo total (snapshot)", br_money(float(s.get("saldo_total", 0.0))))
        c6.metric("Pix (snapshot)", f'{br_int(int(s.get("pix_com",0)))} com | {br_int(int(s.get("pix_sem",0)))} sem')
        c7.metric("Domicílio C6 (snapshot)", br_int(int(s.get("domicilio_c6", 0))))
        c8.metric("Qualificadas (arquivo)", br_int(int(s.get("qualificadas_arquivo", 0))))

    st.divider()

    st.subheader("Remuneração do mês atual (incremental)")

    if saved_resumo:
        months_sorted = sorted(saved_resumo.keys(), key=month_key_str)
        mes_atual = months_sorted[-1]
        info = saved_resumo.get(mes_atual, {})

        faixa = info.get("faixa", "-")
        qual = int(info.get("qualificadas", 0))
        n1 = int(info.get("n1", 0))
        n2 = int(info.get("n2", 0))
        n3 = int(info.get("n3", 0))
        n4 = int(info.get("n4", 0))
        cheio = float(info.get("deveria_receber", 0.0))
        japago = float(info.get("ja_pago_ref", 0.0))
        receber = float(info.get("receber_mes", 0.0))

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Mês", mes_atual)
        m2.metric("Faixa", faixa)
        m3.metric("Qualificadas", br_int(qual))
        m4.metric("A receber (mês)", br_money(receber))

        m5, m6, m7 = st.columns(3)
        m5.metric("Receita cheia (mês)", br_money(cheio))
        m6.metric("Já pago (referência)", br_money(japago))
        m7.metric("Níveis (1/2/3/4)", f"{br_int(n1)} / {br_int(n2)} / {br_int(n3)} / {br_int(n4)}")
    else:
        st.info("Ainda não há histórico de remuneração. Importe os diários (Jan/26 em diante) e/ou Nov/25 e Dez/25.")

    st.divider()

    st.subheader("Receita líquida (H1 + Assis e Mollerke)")

    saved = safe_json_load(HIST_RESUMO_MENSAL, default={})
    if not saved:
        st.info("Sem histórico mensal ainda. Importe os diários (Jan/26 em diante) e/ou Nov/25 e Dez/25.")
    else:
        months_sorted = sorted(saved.keys(), key=month_key_str)
        mes_sel = st.selectbox("Selecione o mês para ver o líquido", months_sorted, index=len(months_sorted) - 1)

        info = saved.get(mes_sel, {})
        base_receber = float(info.get("receber_mes", 0.0))

        nf_h1 = base_receber * 0.187
        apos_nf_h1 = base_receber - nf_h1
        repasse_h1 = apos_nf_h1 * 0.10
        apos_repasse = apos_nf_h1 - repasse_h1
        nf_am = apos_repasse * 0.14
        liquido_am = apos_repasse - nf_am
        deixamos_de_ganhar = nf_h1 + repasse_h1

        l1, l2, l3, l4 = st.columns(4)
        l1.metric("Mês", mes_sel)
        l2.metric("Base (A receber no mês)", br_money(base_receber))
        l3.metric("Deixamos de ganhar (NF H1 + Repasse)", br_money(deixamos_de_ganhar))
        l4.metric("Líquido Assis e Mollerke", br_money(liquido_am))

        df_liq = pd.DataFrame(
            [
                ["Base (A receber no mês)", base_receber],
                ["NF H1 (18,70%)", -nf_h1],
                ["Subtotal após NF H1", apos_nf_h1],
                ["Repasse H1 (10%)", -repasse_h1],
                ["Subtotal após Repasse H1", apos_repasse],
                ["NF Assis e Mollerke (14%)", -nf_am],
                ["Líquido Assis e Mollerke", liquido_am],
            ],
            columns=["Etapa", "Valor"],
        )
        df_liq["Valor"] = df_liq["Valor"].apply(br_money)
        st.dataframe(df_liq, use_container_width=True, hide_index=True)

# =========================================================
# ======================== PÁGINA 2 =======================
# =========================================================
with page[1]:
    st.subheader("📢 Campanhas Meta – C6")

    def _detect_delimiter(sample_text: str) -> str:
        candidates = [";", ",", "\t", "|"]
        counts = {sep: sample_text.count(sep) for sep in candidates}
        best = max(counts, key=counts.get)
        return best if counts[best] > 0 else ","

    def _read_meta_file(file_obj) -> pd.DataFrame:
        name = (file_obj.name or "").lower()
        if name.endswith(".csv"):
            raw = file_obj.getvalue()
            head = raw[:200_000]
            try:
                sample = head.decode("utf-8-sig", errors="replace")
            except Exception:
                sample = head.decode(errors="replace")
            sep = _detect_delimiter(sample)
            return pd.read_csv(
                io.BytesIO(raw),
                engine="python",
                sep=sep,
                on_bad_lines="skip",
                encoding="utf-8-sig",
            )
        return pd.read_excel(file_obj)

    def _norm_col(c: str) -> str:
        c = str(c).strip().lower()
        c = c.replace("\ufeff", "")
        c = c.replace(" ", "_").replace("-", "_")
        c = re.sub(r"_+", "_", c)
        return c

    def _auto_rename_meta_cols(df: pd.DataFrame) -> pd.DataFrame:
        norm_map = {_norm_col(c): c for c in df.columns}
        candidates = {
            "message_id": ["message_id", "messageid", "message id", "id_message", "id_mensagem"],
            "message_date_time": [
                "message_date_time", "message_datetime", "message_date", "message_time",
                "message_date_time_utc", "message_date_time_(utc)", "datetime", "timestamp",
                "created_time", "created_at"
            ],
            "broadcast_description": [
                "broadcast_description", "broadcast_desc", "broadcast", "broadcast_name",
                "campaign", "campaign_name", "description"
            ],
            "message_status": ["message_status", "status", "delivery_status", "message_delivery_status"],
            "contact_id": ["contact_id", "contactid", "wa_id", "whatsapp_id", "recipient_id"],
        }
        candidates = {k: [_norm_col(x) for x in v] for k, v in candidates.items()}
        rename = {}
        for target, cand_list in candidates.items():
            found = None
            if _norm_col(target) in norm_map:
                found = norm_map[_norm_col(target)]
            else:
                for cand in cand_list:
                    if cand in norm_map:
                        found = norm_map[cand]
                        break
            if found:
                rename[found] = target
        return df.rename(columns=rename).copy()

    def _parse_datetime(series: pd.Series) -> pd.Series:
        s = series.astype("string").fillna("").str.strip()
        has_slash_ratio = (s.str.contains("/", regex=False, na=False).sum() / max(len(s), 1))
        if has_slash_ratio >= 0.20:
            dt_br = pd.to_datetime(s, errors="coerce", dayfirst=True)
            dt_us = pd.to_datetime(s, errors="coerce", dayfirst=False)
            return dt_br if int(dt_br.notna().sum()) >= int(dt_us.notna().sum()) else dt_us
        dt1 = pd.to_datetime(s, errors="coerce", dayfirst=True)
        dt2 = pd.to_datetime(s, errors="coerce", dayfirst=False)
        return dt1 if int(dt1.notna().sum()) >= int(dt2.notna().sum()) else dt2

    def _fmt_int_pt(n: int) -> str:
        return f"{int(n):,}".replace(",", ".")

    def _month_label(period_str: str) -> str:
        try:
            y, m = period_str.split("-")
            return f"{m}/{y}"
        except Exception:
            return period_str

    def _meta_store_load() -> dict:
        return safe_json_load(META_STORE_PATH, default={"files": {}, "daily": {}}) or {"files": {}, "daily": {}}

    def _meta_store_save(store: dict):
        safe_json_save(META_STORE_PATH, store)

    def _file_sig_hash(file_obj) -> str:
        raw = file_obj.getvalue()
        h = hashlib.sha1()
        h.update(raw[:1_000_000])
        if len(raw) > 1_000_000:
            h.update(raw[-1_000_000:])
        h.update(str(len(raw)).encode("utf-8"))
        h.update((file_obj.name or "").encode("utf-8", errors="ignore"))
        return h.hexdigest()

    with st.expander("Importar arquivos da Meta (CSV ou XLSX)", expanded=True):
        meta_files = st.file_uploader(
            "Envie um ou mais arquivos. O histórico é acumulado e fica salvo.",
            type=["csv", "xlsx"],
            accept_multiple_files=True,
            key="meta_c6_upload"
        )

    store = _meta_store_load()

    if meta_files:
        added_any = False
        required_cols = ["message_id", "message_date_time", "broadcast_description", "message_status", "contact_id"]

        for f in meta_files:
            try:
                sig = _file_sig_hash(f)
                if sig in (store.get("files") or {}):
                    continue  # já contabilizado
                df_raw = _read_meta_file(f)
                df = _auto_rename_meta_cols(df_raw)

                missing = [c for c in required_cols if c not in df.columns]
                if missing:
                    st.error(f"{f.name}: colunas obrigatórias ausentes após tentativa automática: {missing}")
                    continue

                df = df[required_cols].copy()
                df["broadcast_description"] = df["broadcast_description"].astype(str)
                df = df[df["broadcast_description"].str.lower().str.contains("c6", na=False)]
                if df.empty:
                    store["files"][sig] = {"name": f.name, "rows_c6": 0}
                    added_any = True
                    continue

                df["message_status"] = df["message_status"].astype(str).str.strip().str.lower()
                df["message_date_time"] = _parse_datetime(df["message_date_time"])
                df = df.dropna(subset=["message_date_time"])
                if df.empty:
                    store["files"][sig] = {"name": f.name, "rows_c6": 0}
                    added_any = True
                    continue

                df["day"] = df["message_date_time"].dt.date.astype(str)  # YYYY-MM-DD
                g = df.groupby(["day", "message_status"]).size().reset_index(name="qty")

                daily_store = store.get("daily", {}) or {}
                for _, r in g.iterrows():
                    day = str(r["day"])
                    status = str(r["message_status"])
                    qty = int(r["qty"])
                    if day not in daily_store:
                        daily_store[day] = {}
                    daily_store[day][status] = int(daily_store[day].get(status, 0)) + qty

                store["daily"] = daily_store
                store["files"][sig] = {"name": f.name, "rows_c6": int(len(df))}
                added_any = True

            except Exception as e:
                st.error(f"Erro ao processar {f.name}: {e}")

        if added_any:
            _meta_store_save(store)
            st.success("Arquivos contabilizados e histórico acumulado com sucesso.")

    daily_store = store.get("daily", {}) or {}
    if not daily_store:
        st.info("Importe arquivos da Meta para gerar o histórico.")
    else:
        rows = []
        for day, status_map in daily_store.items():
            try:
                d = dt.datetime.strptime(day, "%Y-%m-%d").date()
            except Exception:
                continue
            for stt, qty in (status_map or {}).items():
                rows.append({"Data": d, "Status": str(stt), "Quantidade": int(qty)})

        df_all = pd.DataFrame(rows)
        if df_all.empty:
            st.info("Sem dados válidos após os filtros.")
        else:
            df_all["Mes"] = df_all["Data"].apply(lambda x: f"{x.year:04d}-{x.month:02d}")

            total_reg = int(df_all["Quantidade"].sum())
            dias_unicos = int(df_all["Data"].nunique())
            status_unicos = int(df_all["Status"].nunique())

            c1, c2, c3 = st.columns(3)
            c1.metric("Registros (C6)", _fmt_int_pt(total_reg))
            c2.metric("Dias únicos", _fmt_int_pt(dias_unicos))
            c3.metric("Status únicos", _fmt_int_pt(status_unicos))

            meses = sorted(df_all["Mes"].unique())
            meses_lbl = [_month_label(m) for m in meses]
            st.markdown("### Filtros")
            mes_sel_lbl = st.selectbox("Selecione o mês", meses_lbl, index=len(meses_lbl) - 1, key="meta_mes_sel")
            mes_sel = meses[meses_lbl.index(mes_sel_lbl)]

            st.markdown("### Totais por dia (no mês)")
            ddf = df_all[df_all["Mes"] == mes_sel].copy()
            pivot = (
                ddf.pivot_table(index="Data", columns="Status", values="Quantidade", aggfunc="sum")
                .fillna(0).astype(int)
                .sort_index(ascending=False)
            )
            pivot["total_dia"] = pivot.sum(axis=1).astype(int)

            view_d = pivot.copy()
            for col in view_d.columns:
                view_d[col] = view_d[col].apply(_fmt_int_pt)
            view_d.index = [d.strftime("%d/%m/%Y") for d in view_d.index]
            view_d = view_d.reset_index().rename(columns={"index": "Data"})
            st.dataframe(view_d, use_container_width=True, hide_index=True)

# =========================================================
# ======================== PÁGINA 3 =======================
# =========================================================
with page[2]:
    st.subheader("🧾 Leads — Status diário (coluna Q)")
    st.markdown("<div class='am-small-note'>DATA BASE: coluna B | STATUS: coluna Q | Indicações válidas: (Data base - Data/hora cadastro (coluna M)) ≤ 14 dias.</div>", unsafe_allow_html=True)

    # ✅ botão de reset só aqui (como estava OK)
    if st.button("Resetar somente Leads — Status diário (coluna Q)"):
        reset_only_leads_status()
        st.success("Leads — Status diário (coluna Q) resetado.")

    def _detect_delimiter(sample_text: str) -> str:
        candidates = [";", ",", "\t", "|"]
        counts = {sep: sample_text.count(sep) for sep in candidates}
        best = max(counts, key=counts.get)
        return best if counts[best] > 0 else ","

    def _read_any_csv_xlsx(file_obj) -> pd.DataFrame:
        name = (file_obj.name or "").lower()
        if name.endswith(".csv"):
            raw = file_obj.getvalue()
            head = raw[:200_000]
            try:
                sample = head.decode("utf-8-sig", errors="replace")
            except Exception:
                sample = head.decode(errors="replace")
            sep = _detect_delimiter(sample)
            return pd.read_csv(
                io.BytesIO(raw),
                engine="python",
                sep=sep,
                on_bad_lines="skip",
                encoding="utf-8-sig",
            )
        return read_excel_any(file_obj.getvalue())

    def _leads_status_load() -> dict:
        return safe_json_load(LEADS_STATUS_STORE_PATH, default={}) or {}

    def _leads_status_save(store: dict):
        safe_json_save(LEADS_STATUS_STORE_PATH, store)

    def _extract_date_base_from_col_b(df: pd.DataFrame) -> Optional[dt.date]:
        if df.shape[1] < 2:
            return None
        s = df.iloc[:, 1]
        d = pd.to_datetime(s, errors="coerce", dayfirst=True).dt.date.dropna()
        if len(d) == 0:
            return None
        try:
            mode = pd.Series(list(d)).mode()
            if len(mode) > 0:
                return mode.iloc[0]
        except Exception:
            pass
        return max(d)

    def _extract_status_col_q(df: pd.DataFrame) -> pd.Series:
        if df.shape[1] < 17:
            return pd.Series([], dtype="string")
        return df.iloc[:, 16].astype("string").fillna("").str.strip()

    def _count_valid_indicacoes(df: pd.DataFrame, data_base: dt.date) -> int:
        """
        Indicações válidas = (data_base - data_hora_cadastro) <= 14 dias
        data_base: coluna B
        data_hora_cadastro: coluna M
        """
        if df.shape[1] < 13:
            return 0
        cad = df.iloc[:, 12]  # Coluna M
        cad_dt = pd.to_datetime(cad, errors="coerce", dayfirst=True)
        if cad_dt.isna().all():
            return 0
        base_dt = pd.to_datetime(data_base)
        diff_days = (base_dt - cad_dt).dt.days
        valid = diff_days.notna() & (diff_days >= 0) & (diff_days <= 14)
        return int(valid.sum())

    def _fmt_int_pt(n: int) -> str:
        return f"{int(n):,}".replace(",", ".")

    def _month_label(period_str: str) -> str:
        try:
            y, m = period_str.split("-")
            return f"{m}/{y}"
        except Exception:
            return period_str

    with st.expander("Importar arquivo diário (status na coluna Q)", expanded=True):
        up = st.file_uploader("Envie XLSX ou CSV (o STATUS deve estar na coluna Q).", type=["xlsx", "csv"], key="leads_status_q_upload")

    store = _leads_status_load()

    if up:
        df_raw = _read_any_csv_xlsx(up)
        data_base = _extract_date_base_from_col_b(df_raw)
        if data_base is None:
            st.error("Não consegui identificar a DATA BASE na coluna B.")
        else:
            status_s = _extract_status_col_q(df_raw)
            status_s = status_s[status_s.astype(str).str.strip() != ""]
            if status_s.empty:
                st.warning("Não encontrei valores de STATUS na coluna Q.")
            else:
                counts = status_s.value_counts().to_dict()
                day_key = data_base.strftime("%d/%m/%Y")
                validas = _count_valid_indicacoes(df_raw, data_base)

                # ✅ guarda status + validas (apenas o necessário)
                store[day_key] = {
                    "_status": {str(k): int(v) for k, v in counts.items()},
                    "_validas": int(validas),
                }
                _leads_status_save(store)
                st.success(f"Importado e salvo: {day_key} ({_fmt_int_pt(int(status_s.shape[0]))} linhas com status).")

    # build dataset
    rows = []
    validas_rows = []
    for dkey, m in (store or {}).items():
        if not isinstance(m, dict):
            continue
        smap = m.get("_status", {}) or {}
        v = int(m.get("_validas", 0) or 0)
        for stt, qty in smap.items():
            rows.append({"Data": dkey, "Status": str(stt), "Quantidade": int(qty)})
        validas_rows.append({"Data": dkey, "Validas": v})

    if not rows:
        st.info("Importe arquivos para montar o histórico.")
    else:
        df_all = pd.DataFrame(rows)
        df_all["_date"] = pd.to_datetime(df_all["Data"], format="%d/%m/%Y", errors="coerce")
        df_all = df_all.dropna(subset=["_date"]).copy()
        df_all["_date"] = df_all["_date"].dt.date
        df_all["Mes"] = df_all["_date"].apply(lambda x: f"{x.year:04d}-{x.month:02d}")

        dfv = pd.DataFrame(validas_rows)
        dfv["_date"] = pd.to_datetime(dfv["Data"], format="%d/%m/%Y", errors="coerce")
        dfv = dfv.dropna(subset=["_date"]).copy()
        dfv["_date"] = dfv["_date"].dt.date
        dfv["Mes"] = dfv["_date"].apply(lambda x: f"{x.year:04d}-{x.month:02d}")
        dfv["Validas"] = pd.to_numeric(dfv["Validas"], errors="coerce").fillna(0).astype(int)

        dias_hist = int(df_all["_date"].nunique())
        status_unicos = int(df_all["Status"].nunique())
        total_sum = int(df_all["Quantidade"].sum())
        total_validas = int(dfv["Validas"].sum()) if not dfv.empty else 0

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Dias no histórico", _fmt_int_pt(dias_hist))
        m2.metric("Status únicos", _fmt_int_pt(status_unicos))
        m3.metric("Total (somatório)", _fmt_int_pt(total_sum))
        m4.metric("Indicações válidas (somatório)", _fmt_int_pt(total_validas))

        meses = sorted(df_all["Mes"].unique())
        meses_lbl = [_month_label(m) for m in meses]
        st.markdown("### Filtros")
        mes_sel_lbl = st.selectbox("Selecione o mês", meses_lbl, index=len(meses_lbl) - 1, key="leads_q_mes_sel")
        mes_sel = meses[meses_lbl.index(mes_sel_lbl)]

        # ✅ Comparativo diário (Δ vs dia anterior) — dentro do mês (inclui Válidas)
        st.markdown("### Comparativo diário (Δ vs dia anterior) — dentro do mês")

        dfm = df_all[df_all["Mes"] == mes_sel].copy()
        if dfm.empty:
            st.info("Sem dados para esse mês.")
        else:
            pivot = (
                dfm.pivot_table(index="_date", columns="Status", values="Quantidade", aggfunc="sum")
                .fillna(0).astype(int)
                .sort_index(ascending=True)
            )
            pivot["Total"] = pivot.sum(axis=1).astype(int)

            dfv_m = dfv[dfv["Mes"] == mes_sel].copy()
            vmap = dfv_m.set_index("_date")["Validas"].to_dict() if not dfv_m.empty else {}
            pivot["Válidas"] = pd.Series([int(vmap.get(d, 0)) for d in pivot.index], index=pivot.index).astype(int)

            delta = pivot.diff().fillna(0).astype(int)

            view = pd.DataFrame(index=pivot.index)
            view["Data"] = [d.strftime("%d/%m/%Y") for d in pivot.index]
            view["Total"] = pivot["Total"].astype(int)
            view["Δ Total"] = delta["Total"].astype(int)
            view["Válidas"] = pivot["Válidas"].astype(int)
            view["Δ Válidas"] = delta["Válidas"].astype(int)

            for c in ["Total", "Δ Total", "Válidas", "Δ Válidas"]:
                view[c] = view[c].apply(_fmt_int_pt)

            def _style_delta(val: str):
                try:
                    v = int(str(val).replace(".", "").replace(",", ""))
                except Exception:
                    return ""
                if v > 0:
                    return "color:#007AFF; font-weight:900;"
                if v < 0:
                    return "color:#FF3B30; font-weight:900;"
                return "color:#5b6b8c; font-weight:700;"

            styled = view.style.applymap(_style_delta, subset=["Δ Total", "Δ Válidas"])
            st.dataframe(styled, use_container_width=True, hide_index=True)

            t_mes = int(pivot["Total"].sum())
            v_mes = int(pivot["Válidas"].sum())
            cA, cB = st.columns(2)
            cA.metric("Total no mês", _fmt_int_pt(t_mes))
            cB.metric("Válidas no mês (≤14 dias)", _fmt_int_pt(v_mes))
