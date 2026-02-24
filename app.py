import os
import io
import json
import re
import hashlib
import datetime as dt
from typing import Dict, Tuple, Optional, List

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
COL_CNPJ = "CD_CPF_CNPJ_CLIENTE"
COL_ABERTURA = "DT_CONTA_CRIADA"
COL_FUNDACAO = "DT_FUNDACAO_EMPRESA"
COL_PIX = "CHAVES_PIX_FORTE"
COL_SALDO = "VL_SALDO_MEDIO_MENSALIZADO"
COL_CASHIN_MTD = "VL_CASH_IN_MTD"  # ✅ usado como "Saldo total (snapshot)"
COL_STATUS = "STATUS_CC"
COL_DOMICILIO = "BANCO_DOMICILIO"
COL_BY = "FL_QUALIFICADO_COMISS"
COL_BR = "MES_REF_COMISS"
COL_CRIT = "CRITERIOS_ATINGIDOS_COMISS"

COL_LEADS_DATA = "DATA_CADASTRO"
COL_DATA_BASE = "DATA_BASE"

POSSIVEIS_COL_DATA_BASE = [
    "DATA_BASE", "DT_BASE", "DATA_REFERENCIA", "DT_REFERENCIA",
    "DATA_RELATORIO", "DT_RELATORIO", "DATA_ATUALIZACAO", "DT_ATUALIZACAO"
]

ALVO_CONVERSAO = 0.20
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
HIST_COMPARE_DAILY = os.path.join(DATA_DIR, "hist_comparativo_diario.json")


# =========================================================
# HELPERS
# =========================================================
def safe_json_load(path: str, default):
    if "firebase" in st.secrets:
        return _fs_load_payload(_fs_doc_id_from_path(path), default)

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def safe_json_save(path: str, obj):
    if "firebase" in st.secrets:
        _fs_save_payload(_fs_doc_id_from_path(path), obj)
        return

    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def safe_json_delete(path: str):
    if "firebase" in st.secrets:
        _fs_delete_doc(_fs_doc_id_from_path(path))
    if os.path.exists(path):
        os.remove(path)


def file_md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


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
    if not isinstance(txt, str) or not txt.strip():
        return 0
    nums = [int(n) for n in re.findall(r":\s*(\d+)", txt)]
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
    if not isinstance(txt, str) or not txt.strip():
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
# HISTÓRICO DIÁRIO
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
    if q.empty:
        store[mkey] = store.get(mkey, {}) or {}
        safe_json_save(HIST_MONTH_LEVELS, store)
        return

    by_cnpj = q.groupby("_cnpj")["_nivel"].max().reset_index()

    month_map: Dict[str, int] = store.get(mkey, {}) or {}
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
    if q.empty:
        store[mkey] = month_map
        safe_json_save(HIST_MONTH_LEVELS, store)
        return

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
def login_gate() -> bool:
    st.sidebar.markdown("### Acesso")
    u = st.sidebar.text_input("Usuário", value="")
    p = st.sidebar.text_input("Senha", value="", type="password")
    if st.sidebar.button("Entrar"):
        st.session_state["logged_in"] = (u == "admin" and p == "123456")
        if not st.session_state["logged_in"]:
            st.sidebar.error("Usuário ou senha inválidos.")
    return st.session_state.get("logged_in", False)


def apply_theme():
    st.markdown(
        """
        <style>
            section[data-testid="stSidebar"]{ background:#0f1b3a; }
            section[data-testid="stSidebar"] * { color:#ffffff !important; }

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
        </style>
        """,
        unsafe_allow_html=True,
    )


def show_logo_and_title():
    here = os.getcwd()
    logo_path = os.path.join(here, "LOGO CORRETA.png")

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


def reset_all_data():
    for p in [
        HIST_OPEN_DAILY, HIST_LEADS_DAILY, HIST_MONTH_LEVELS,
        HIST_PAGO_POR_CNPJ, HIST_RESUMO_MENSAL, HIST_SNAPSHOT_MENSAL,
        HIST_COMPARE_DAILY,
        os.path.join(DATA_DIR, "meta_c6_summary.json"),
        os.path.join(DATA_DIR, "leads_status_daily_q.json"),
    ]:
        safe_json_delete(p)


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

tab_painel, tab_meta, tab_leads_status = st.tabs(
    ["📊 Painel C6", "📢 Campanhas Meta – C6", "🧾 Leads – Status Diário"]
)

# =========================================================
# TAB 1 — PAINEL C6
# =========================================================
with tab_painel:

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
        receber = float(info.get("receber_mes", 0.0))

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Mês", mes_atual)
        m2.metric("Faixa", faixa)
        m3.metric("Qualificadas", br_int(qual))
        m4.metric("A receber (mês)", br_money(receber))
    else:
        st.info("Ainda não há histórico de remuneração. Importe os diários (Jan/26 em diante) e/ou Nov/25 e Dez/25.")

# =========================================================
# TAB 2 — CAMPANHAS META – C6
# =========================================================
with tab_meta:

    st.subheader("📢 Campanhas Meta – C6")
    META_SUMMARY_PATH = os.path.join(DATA_DIR, "meta_c6_summary.json")

    # ✅ Grupos (inclui os 3 novos)
    META_GROUPS = {
        "VAREJO": ["americ", "bigloj", "links", "varejo", "carioc"],
        "FUNDAÇÃO FFM": ["fundacao", "ffmedi"],
        "FIEB": ["fieb", "sesi", "senai", "iel", "cieb", "csenai", "casesi", "caiell"],
        "EXPONENCIAL": ["exponencial", "copel", "embasa", "bnb"],
        "I9": ["i9"],
        "JUNTA COMERCIAL": ["junta", "jacom"],
    }
    META_GROUP_ORDER = ["VAREJO", "FUNDAÇÃO FFM", "FIEB", "EXPONENCIAL", "I9", "JUNTA COMERCIAL"]

    def _classify_group(desc: str) -> Optional[str]:
        if desc is None or pd.isna(desc):
            return None
        s = str(desc).strip().lower()
        for g in META_GROUP_ORDER:
            for k in META_GROUPS.get(g, []):
                if k in s:
                    return g
        return None

    def _norm_col(c: str) -> str:
        c = str(c).strip().lower()
        c = c.replace("\ufeff", "")
        c = c.replace(" ", "_").replace("-", "_")
        c = re.sub(r"_+", "_", c)
        return c

    def _detect_delimiter(sample_text: str) -> str:
        candidates = [";", ",", "\t", "|"]
        counts = {sep: sample_text.count(sep) for sep in candidates}
        best = max(counts, key=counts.get)
        return best if counts[best] > 0 else ","

    def _read_meta_file(name: str, raw_bytes: bytes) -> pd.DataFrame:
        name = name.lower()
        if name.endswith(".csv"):
            head = raw_bytes[:200_000]
            sample = head.decode("utf-8-sig", errors="replace")
            sep = _detect_delimiter(sample)
            return pd.read_csv(
                io.BytesIO(raw_bytes),
                engine="python",
                sep=sep,
                on_bad_lines="skip",
                encoding="utf-8-sig",
            )
        return pd.read_excel(io.BytesIO(raw_bytes))

    def _auto_rename_to_required(df: pd.DataFrame) -> pd.DataFrame:
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

    def _parse_datetime_br_priority(series: pd.Series) -> pd.Series:
        s = series.astype("string").fillna("").str.strip()
        has_slash_ratio = (s.str.contains("/", regex=False, na=False).sum() / max(len(s), 1))

        if has_slash_ratio >= 0.20:
            dt_br = pd.to_datetime(s, errors="coerce", dayfirst=True)
            if int(dt_br.notna().sum()) >= max(1, int(0.80 * len(s))):
                return dt_br
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

    def _records_firestore_safe(recs: list) -> list:
        safe = []
        for r in recs:
            rr = {}
            for k, v in (r or {}).items():
                if isinstance(v, (dt.date, dt.datetime, pd.Timestamp)):
                    rr[k] = pd.to_datetime(v).strftime("%Y-%m-%d")
                    continue
                try:
                    if hasattr(v, "item") and callable(v.item):
                        vv = v.item()
                        if isinstance(vv, (int, float, str, bool)) or vv is None:
                            rr[k] = vv
                            continue
                except Exception:
                    pass

                if isinstance(v, (int, float, str, bool)) or v is None:
                    rr[k] = v
                else:
                    rr[k] = str(v)

            safe.append(rr)
        return safe

    def _load_persisted_summary() -> dict:
        return safe_json_load(META_SUMMARY_PATH, default={}) or {}

    def _save_persisted_summary(summary: dict):
        safe_json_save(META_SUMMARY_PATH, summary)

    def _normalize_existing_tables(summary: dict) -> Tuple[pd.DataFrame, pd.DataFrame]:
        df_monthly = pd.DataFrame(summary.get("monthly", []))
        df_daily = pd.DataFrame(summary.get("daily", []))

        if not df_monthly.empty:
            df_monthly["Mes"] = df_monthly["Mes"].astype(str)
            df_monthly["message_status"] = df_monthly["message_status"].astype(str).str.lower()
            df_monthly["qty"] = pd.to_numeric(df_monthly["qty"], errors="coerce").fillna(0).astype(int)

        if not df_daily.empty:
            df_daily["Mes"] = df_daily["Mes"].astype(str)
            df_daily["message_status"] = df_daily["message_status"].astype(str).str.lower()
            df_daily["qty"] = pd.to_numeric(df_daily["qty"], errors="coerce").fillna(0).astype(int)
            df_daily["Data"] = pd.to_datetime(df_daily["Data"], errors="coerce").dt.date

        return df_monthly, df_daily

    def _normalize_existing_groups(summary: dict) -> dict:
        g = summary.get("groups", {}) or {}
        out = {}
        for grp, payload in g.items():
            p = payload or {}
            dm = pd.DataFrame(p.get("monthly", []))
            dd = pd.DataFrame(p.get("daily", []))

            if not dm.empty:
                dm["Mes"] = dm["Mes"].astype(str)
                dm["message_status"] = dm["message_status"].astype(str).str.lower()
                dm["qty"] = pd.to_numeric(dm["qty"], errors="coerce").fillna(0).astype(int)

            if not dd.empty:
                dd["Mes"] = dd["Mes"].astype(str)
                dd["message_status"] = dd["message_status"].astype(str).str.lower()
                dd["qty"] = pd.to_numeric(dd["qty"], errors="coerce").fillna(0).astype(int)
                dd["Data"] = pd.to_datetime(dd["Data"], errors="coerce").dt.date

            out[grp] = {"monthly_df": dm, "daily_df": dd}
        return out

    def _incremental_upsert_summary(new_df_5cols: pd.DataFrame, new_files_meta: List[dict], imported_hashes: List[str]) -> dict:
        """
        ✅ Importação NORMAL (acumula tudo: global + grupos), sem resetar nada.
        """
        existing = _load_persisted_summary() or {}

        status_set = set(existing.get("status_set", []) or [])
        campaign_set = set(existing.get("campaign_set", []) or [])

        files = existing.get("files", []) or []
        seen_hashes = set(existing.get("file_hashes", []) or [])
        seen_groups_hashes = set(existing.get("groups_file_hashes", []) or [])

        actually_new_hashes = [h for h in imported_hashes if h not in seen_hashes]
        if not actually_new_hashes:
            return existing

        old_monthly, old_daily = _normalize_existing_tables(existing)
        old_groups = _normalize_existing_groups(existing)

        df = new_df_5cols.copy()
        df["message_status"] = df["message_status"].astype(str).str.strip().str.lower()
        df["broadcast_description"] = df["broadcast_description"].astype(str)
        df["Data"] = df["message_date_time"].dt.date
        df["Mes"] = df["message_date_time"].dt.to_period("M").astype(str)

        status_set |= set(df["message_status"].dropna().unique().tolist())
        campaign_set |= set(df["broadcast_description"].dropna().unique().tolist())

        new_monthly = df.groupby(["Mes", "message_status"]).size().reset_index(name="qty")
        new_daily = df.groupby(["Mes", "Data", "message_status"]).size().reset_index(name="qty")

        if old_monthly.empty:
            merged_monthly = new_monthly.copy()
        else:
            merged_monthly = pd.concat([old_monthly, new_monthly], ignore_index=True)
            merged_monthly = merged_monthly.groupby(["Mes", "message_status"], as_index=False)["qty"].sum()

        if old_daily.empty:
            merged_daily = new_daily.copy()
        else:
            merged_daily = pd.concat([old_daily, new_daily], ignore_index=True)
            merged_daily = merged_daily.groupby(["Mes", "Data", "message_status"], as_index=False)["qty"].sum()

        # grupos
        df["_group"] = df["broadcast_description"].apply(_classify_group)
        groups_payload_out = existing.get("groups", {}) or {}

        for grp in META_GROUP_ORDER:
            sub = df[df["_group"] == grp].copy()
            if sub.empty:
                continue

            nm = sub.groupby(["Mes", "message_status"]).size().reset_index(name="qty")
            nd = sub.groupby(["Mes", "Data", "message_status"]).size().reset_index(name="qty")

            old_gm = old_groups.get(grp, {}).get("monthly_df", pd.DataFrame())
            old_gd = old_groups.get(grp, {}).get("daily_df", pd.DataFrame())

            mgm = nm if old_gm.empty else pd.concat([old_gm, nm], ignore_index=True).groupby(["Mes", "message_status"], as_index=False)["qty"].sum()
            mgd = nd if old_gd.empty else pd.concat([old_gd, nd], ignore_index=True).groupby(["Mes", "Data", "message_status"], as_index=False)["qty"].sum()

            groups_payload_out[grp] = {
                "monthly": _records_firestore_safe(mgm.to_dict(orient="records")),
                "daily": _records_firestore_safe(mgd.to_dict(orient="records")),
            }

        global_total = int(merged_monthly["qty"].sum()) if not merged_monthly.empty else 0
        global_enviados = int(
            merged_monthly[merged_monthly["message_status"].isin(["sent", "delivered", "read"])]["qty"].sum()
        ) if not merged_monthly.empty else 0
        dias_unicos = int(merged_daily["Data"].nunique()) if not merged_daily.empty else 0
        status_unicos = int(len(status_set))
        campanhas = int(len(campaign_set))

        for meta in new_files_meta:
            h = meta.get("hash")
            if h and h not in seen_hashes:
                files.append({"name": meta.get("name", ""), "size": int(meta.get("size", 0) or 0), "hash": h})

        seen_hashes |= set(actually_new_hashes)
        seen_groups_hashes |= set(actually_new_hashes)  # ✅ no import normal, grupos também são preenchidos

        summary = {
            "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "files": _records_firestore_safe(files),
            "file_hashes": list(seen_hashes),
            "groups_file_hashes": list(seen_groups_hashes),
            "status_set": sorted(list(status_set)),
            "campaign_set": sorted(list(campaign_set)),
            "global": {
                "total": int(global_total),
                "enviados": int(global_enviados),
                "dias_unicos": int(dias_unicos),
                "campanhas": int(campanhas),
                "status_unicos": int(status_unicos),
            },
            "monthly": _records_firestore_safe(merged_monthly.to_dict(orient="records")),
            "daily": _records_firestore_safe(merged_daily.to_dict(orient="records")),
            "groups": groups_payload_out,
        }

        _save_persisted_summary(summary)
        return summary

    def _upsert_groups_only(df_5cols: pd.DataFrame, file_hashes: List[str], files_meta: List[dict]) -> dict:
        """
        ✅ Reprocessar APENAS segmentação por grupo.
        - NÃO altera monthly/daily/global.
        - Serve para preencher grupos de arquivos já contabilizados anteriormente.
        - Dedup por groups_file_hashes (se já reprocessou, ignora).
        """
        existing = _load_persisted_summary() or {}
        groups_payload_out = existing.get("groups", {}) or {}

        old_groups = _normalize_existing_groups(existing)

        seen_groups_hashes = set(existing.get("groups_file_hashes", []) or [])
        new_hashes = [h for h in file_hashes if h not in seen_groups_hashes]
        if not new_hashes:
            return existing

        df = df_5cols.copy()
        df["message_status"] = df["message_status"].astype(str).str.strip().str.lower()
        df["broadcast_description"] = df["broadcast_description"].astype(str)
        df["Data"] = df["message_date_time"].dt.date
        df["Mes"] = df["message_date_time"].dt.to_period("M").astype(str)

        df["_group"] = df["broadcast_description"].apply(_classify_group)

        for grp in META_GROUP_ORDER:
            sub = df[df["_group"] == grp].copy()
            if sub.empty:
                continue

            nm = sub.groupby(["Mes", "message_status"]).size().reset_index(name="qty")
            nd = sub.groupby(["Mes", "Data", "message_status"]).size().reset_index(name="qty")

            old_gm = old_groups.get(grp, {}).get("monthly_df", pd.DataFrame())
            old_gd = old_groups.get(grp, {}).get("daily_df", pd.DataFrame())

            mgm = nm if old_gm.empty else pd.concat([old_gm, nm], ignore_index=True).groupby(["Mes", "message_status"], as_index=False)["qty"].sum()
            mgd = nd if old_gd.empty else pd.concat([old_gd, nd], ignore_index=True).groupby(["Mes", "Data", "message_status"], as_index=False)["qty"].sum()

            groups_payload_out[grp] = {
                "monthly": _records_firestore_safe(mgm.to_dict(orient="records")),
                "daily": _records_firestore_safe(mgd.to_dict(orient="records")),
            }

        seen_groups_hashes |= set(new_hashes)

        existing["groups"] = groups_payload_out
        existing["groups_file_hashes"] = list(seen_groups_hashes)
        existing["updated_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # (opcional) mantém registro de arquivos reprocessados (não interfere no "files" geral)
        rep = existing.get("groups_reprocess_files", []) or []
        for meta in files_meta:
            h = meta.get("hash")
            if h and h in new_hashes:
                rep.append({"name": meta.get("name", ""), "size": int(meta.get("size", 0) or 0), "hash": h})
        existing["groups_reprocess_files"] = _records_firestore_safe(rep)

        _save_persisted_summary(existing)
        return existing

    if "meta_c6_summary" not in st.session_state:
        st.session_state["meta_c6_summary"] = _load_persisted_summary() or None

    # ✅ Importação principal (NÃO remove, continua aqui)
    with st.expander("Importar arquivos da Meta (CSV ou XLSX)", expanded=True):
        meta_files = st.file_uploader(
            "Envie um ou mais arquivos. O app vai ACUMULANDO o histórico (não substitui).",
            type=["csv", "xlsx"],
            accept_multiple_files=True,
            key="meta_c6_upload"
        )

    if meta_files:
        existing = _load_persisted_summary()
        seen_hashes = set((existing or {}).get("file_hashes", []) or [])

        dfs_meta = []
        files_meta = []
        imported_hashes = []
        skipped = 0

        for f in meta_files:
            raw = f.getvalue()
            h = file_md5(raw)
            files_meta.append({"name": f.name, "size": int(getattr(f, "size", 0) or 0), "hash": h})

            if h in seen_hashes:
                skipped += 1
                continue

            try:
                df_raw = _read_meta_file(f.name, raw)
                dfs_meta.append(df_raw)
                imported_hashes.append(h)
            except Exception as e:
                st.error(f"Erro ao ler {f.name}: {e}")

        if skipped > 0:
            st.info(f"{skipped} arquivo(s) já tinham sido importados antes e foram ignorados (para não duplicar).")

        if dfs_meta:
            df_raw = pd.concat(dfs_meta, ignore_index=True)
            df = _auto_rename_to_required(df_raw)

            required_cols = ["message_id", "message_date_time", "broadcast_description", "message_status", "contact_id"]
            missing = [c for c in required_cols if c not in df.columns]
            if missing:
                st.error(f"Colunas obrigatórias ausentes (após tentativa automática): {missing}")
                st.markdown("**Colunas encontradas no arquivo (para conferência):**")
                st.write(sorted([str(c) for c in df_raw.columns]))
            else:
                df = df[required_cols].copy()
                df["broadcast_description"] = df["broadcast_description"].astype(str)

                df = df[df["broadcast_description"].str.lower().str.contains("c6", na=False)]
                df["message_date_time"] = _parse_datetime_br_priority(df["message_date_time"])
                df = df.dropna(subset=["message_date_time"])

                if df.empty:
                    st.warning("Nenhum registro com 'c6' encontrado nas campanhas após o filtro.")
                else:
                    st.session_state["meta_c6_summary"] = _incremental_upsert_summary(df, files_meta, imported_hashes)
                    st.success("Importação concluída. O histórico foi acumulado com sucesso (inclui grupos).")

    summary = st.session_state.get("meta_c6_summary") or _load_persisted_summary() or None
    st.session_state["meta_c6_summary"] = summary

    if not summary:
        st.info("Importe um ou mais arquivos para gerar os relatórios. (Depois disso, o histórico fica salvo.)")
    else:
        g = (summary.get("global") or {})
        total = int(g.get("total", 0))
        enviados = int(g.get("enviados", 0))
        dias_unicos = int(g.get("dias_unicos", 0))
        campanhas = int(g.get("campanhas", 0))
        status_unicos = int(g.get("status_unicos", 0))

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Registros (C6)", _fmt_int_pt(total))
        c2.metric("Enviados (sent+delivered+read)", _fmt_int_pt(enviados))
        c3.metric("Dias únicos", _fmt_int_pt(dias_unicos))
        c4.metric("Campanhas (contendo C6)", _fmt_int_pt(campanhas))
        c5.metric("Status únicos", _fmt_int_pt(status_unicos))

        df_monthly = pd.DataFrame(summary.get("monthly", []))
        df_daily = pd.DataFrame(summary.get("daily", []))

        if df_monthly.empty or df_daily.empty:
            st.warning("Resumo vazio. Importe arquivos para começar.")
        else:
            df_monthly["Mes"] = df_monthly["Mes"].astype(str)
            df_monthly["message_status"] = df_monthly["message_status"].astype(str).str.lower()
            df_monthly["qty"] = pd.to_numeric(df_monthly["qty"], errors="coerce").fillna(0).astype(int)

            df_daily["Mes"] = df_daily["Mes"].astype(str)
            df_daily["message_status"] = df_daily["message_status"].astype(str).str.lower()
            df_daily["qty"] = pd.to_numeric(df_daily["qty"], errors="coerce").fillna(0).astype(int)
            df_daily["Data"] = pd.to_datetime(df_daily["Data"], errors="coerce").dt.date

            meses = sorted(df_monthly["Mes"].unique())
            meses_lbl = [_month_label(m) for m in meses]

            st.markdown("### Filtros")
            mes_sel_lbl = st.selectbox("Selecione o mês", meses_lbl, index=len(meses_lbl) - 1, key="meta_c6_mes_sel_lbl")
            mes_sel = meses[meses_lbl.index(mes_sel_lbl)]

            st.markdown("### Sintético mensal por status (mês selecionado)")
            mdf = df_monthly[df_monthly["Mes"] == mes_sel].copy().sort_values("qty", ascending=False)
            enviados_mes = int(mdf[mdf["message_status"].isin(["sent", "delivered", "read"])]["qty"].sum())

            a1, a2 = st.columns(2)
            a1.metric("Total no mês", _fmt_int_pt(int(mdf["qty"].sum())))
            a2.metric("Enviados no mês (sent+delivered+read)", _fmt_int_pt(enviados_mes))

            view_m = mdf.rename(columns={"message_status": "Status", "qty": "Quantidade"}).copy()
            view_m["Quantidade"] = view_m["Quantidade"].apply(_fmt_int_pt)
            st.dataframe(view_m, use_container_width=True, hide_index=True)

            st.markdown("### Totais por dia (dentro do mês selecionado)")
            ddf = df_daily[df_daily["Mes"] == mes_sel].copy()
            if ddf.empty:
                st.info("Sem dados diários para este mês.")
            else:
                pivot = (
                    ddf.pivot_table(index="Data", columns="message_status", values="qty", aggfunc="sum")
                    .fillna(0)
                    .astype(int)
                    .sort_index(ascending=False)
                )
                pivot["total_dia"] = pivot.sum(axis=1).astype(int)
                pivot["enviados_dia"] = (pivot.get("sent", 0) + pivot.get("delivered", 0) + pivot.get("read", 0)).astype(int)

                view_d = pivot.copy()
                for col in view_d.columns:
                    view_d[col] = view_d[col].apply(_fmt_int_pt)

                view_d.index = [d.strftime("%d/%m/%Y") if isinstance(d, dt.date) else str(d) for d in view_d.index]
                view_d = view_d.reset_index().rename(columns={"index": "Data"})
                st.dataframe(view_d, use_container_width=True, hide_index=True)

            # ==========================================================
            # ✅ VISUALIZAÇÕES POR GRUPO (ACRÉSCIMO) + REPROCESSAR
            # ==========================================================
            with st.expander("Visualizações por grupo (Varejo / Fundação FFM / FIEB / Exponencial / I9 / Junta Comercial) — clique para abrir", expanded=False):
                st.caption("Baseado em broadcast_description. Não altera os relatórios atuais; é um acréscimo usando consolidados por grupo.")

                # ✅ AQUI está a parte de IMPORTAR para reprocessar APENAS grupos (como você pediu)
                st.markdown("**Opcional: Reprocessar apenas segmentação (grupos)**")
                st.caption("Use isso se você já importou os arquivos antes (totais gerais já contabilizados) e quer só preencher os grupos. Não mexe nos totais gerais.")
                rep_files = st.file_uploader(
                    "Envie CSV/XLSX (os mesmos arquivos, se quiser). O app vai usar apenas broadcast_description para classificar.",
                    type=["csv", "xlsx"],
                    accept_multiple_files=True,
                    key="meta_c6_reprocess_groups"
                )

                if rep_files:
                    existing = _load_persisted_summary() or {}
                    seen_grp_hash = set((existing or {}).get("groups_file_hashes", []) or [])

                    dfs_rep = []
                    rep_meta = []
                    rep_hashes = []
                    rep_skipped = 0

                    for f in rep_files:
                        raw = f.getvalue()
                        h = file_md5(raw)
                        rep_meta.append({"name": f.name, "size": int(getattr(f, "size", 0) or 0), "hash": h})
                        if h in seen_grp_hash:
                            rep_skipped += 1
                            continue
                        try:
                            df_raw = _read_meta_file(f.name, raw)
                            dfs_rep.append(df_raw)
                            rep_hashes.append(h)
                        except Exception as e:
                            st.error(f"Erro ao ler {f.name}: {e}")

                    if rep_skipped:
                        st.info(f"{rep_skipped} arquivo(s) já tinham sido reprocessados para grupos e foram ignorados.")

                    if dfs_rep:
                        df_raw = pd.concat(dfs_rep, ignore_index=True)
                        df = _auto_rename_to_required(df_raw)

                        required_cols = ["message_id", "message_date_time", "broadcast_description", "message_status", "contact_id"]
                        missing = [c for c in required_cols if c not in df.columns]
                        if missing:
                            st.error(f"Colunas obrigatórias ausentes (após tentativa automática): {missing}")
                        else:
                            df = df[required_cols].copy()
                            df["broadcast_description"] = df["broadcast_description"].astype(str)
                            df = df[df["broadcast_description"].str.lower().str.contains("c6", na=False)]

                            df["message_date_time"] = _parse_datetime_br_priority(df["message_date_time"])
                            df = df.dropna(subset=["message_date_time"])

                            if df.empty:
                                st.warning("Nenhum registro com 'c6' encontrado nas campanhas após o filtro.")
                            else:
                                st.session_state["meta_c6_summary"] = _upsert_groups_only(df, rep_hashes, rep_meta)
                                summary = st.session_state["meta_c6_summary"]
                                st.success("Segmentação por grupos atualizada (sem alterar os totais gerais).")

                summary = st.session_state.get("meta_c6_summary") or _load_persisted_summary() or {}
                groups_store = (summary.get("groups", {}) or {})

                grp = st.selectbox("Grupo", META_GROUP_ORDER, key="meta_group_pick")

                if st.button("Processar", type="primary", key="meta_group_process"):
                    st.session_state["meta_group_selected"] = grp

                grp_sel = st.session_state.get("meta_group_selected", None)
                if grp_sel:
                    gp = groups_store.get(grp_sel, {}) or {}
                    gm = pd.DataFrame(gp.get("monthly", []))
                    gd = pd.DataFrame(gp.get("daily", []))

                    if gm.empty or gd.empty:
                        st.info("Ainda não há dados nesse grupo. Reprocesse os grupos (acima) ou importe arquivos novos.")
                    else:
                        gm["Mes"] = gm["Mes"].astype(str)
                        gm["message_status"] = gm["message_status"].astype(str).str.lower()
                        gm["qty"] = pd.to_numeric(gm["qty"], errors="coerce").fillna(0).astype(int)

                        gd["Mes"] = gd["Mes"].astype(str)
                        gd["message_status"] = gd["message_status"].astype(str).str.lower()
                        gd["qty"] = pd.to_numeric(gd["qty"], errors="coerce").fillna(0).astype(int)
                        gd["Data"] = pd.to_datetime(gd["Data"], errors="coerce").dt.date

                        meses_g = sorted(gm["Mes"].unique())
                        meses_g_lbl = [_month_label(m) for m in meses_g]

                        mes_g_lbl = st.selectbox(
                            "Selecione o mês",
                            meses_g_lbl,
                            index=len(meses_g_lbl) - 1,
                            key=f"meta_group_mes_sel_{grp_sel}"
                        )
                        mes_g = meses_g[meses_g_lbl.index(mes_g_lbl)]

                        st.markdown("### Sintético mensal por status (grupo / mês selecionado)")
                        gmf = gm[gm["Mes"] == mes_g].copy().sort_values("qty", ascending=False)
                        view_gm = gmf.rename(columns={"message_status": "Status", "qty": "Quantidade"}).copy()
                        view_gm["Quantidade"] = view_gm["Quantidade"].apply(_fmt_int_pt)
                        st.dataframe(view_gm, use_container_width=True, hide_index=True)

                        st.markdown("### Totais por dia (dentro do mês selecionado)")
                        gdf = gd[gd["Mes"] == mes_g].copy()
                        if gdf.empty:
                            st.info("Sem dados diários para este mês no grupo.")
                        else:
                            pivot_g = (
                                gdf.pivot_table(index="Data", columns="message_status", values="qty", aggfunc="sum")
                                .fillna(0)
                                .astype(int)
                                .sort_index(ascending=False)
                            )
                            pivot_g["total_dia"] = pivot_g.sum(axis=1).astype(int)
                            pivot_g["enviados_dia"] = (pivot_g.get("sent", 0) + pivot_g.get("delivered", 0) + pivot_g.get("read", 0)).astype(int)

                            view_gd = pivot_g.copy()
                            for col in view_gd.columns:
                                view_gd[col] = view_gd[col].apply(_fmt_int_pt)

                            view_gd.index = [d.strftime("%d/%m/%Y") if isinstance(d, dt.date) else str(d) for d in view_gd.index]
                            view_gd = view_gd.reset_index().rename(columns={"index": "Data"})
                            st.dataframe(view_gd, use_container_width=True, hide_index=True)

            st.info(
                "Obs.: aqui o histórico é consolidado (e persistente) por dia/status/mês. "
                "A segmentação por grupos é um acréscimo e não mexe no que já está contabilizado."
            )

# =========================================================
# TAB 3 — LEADS — STATUS DIÁRIO (coluna Q) + indicações
# =========================================================
with tab_leads_status:

    st.subheader("🧾 Leads — Status diário (coluna Q)")
    LEADS_STATUS_DAILY_PATH = os.path.join(DATA_DIR, "leads_status_daily_q.json")

    def _leads_status_load():
        return safe_json_load(LEADS_STATUS_DAILY_PATH, default={}) or {}

    def _leads_status_save(obj):
        safe_json_save(LEADS_STATUS_DAILY_PATH, obj)

    def _leads_status_reset_only():
        safe_json_delete(LEADS_STATUS_DAILY_PATH)
        for k in list(st.session_state.keys()):
            if str(k).startswith("leads_status_"):
                st.session_state.pop(k, None)

    def _detect_delim_for_csv(sample_text: str) -> str:
        candidates = [";", ",", "\t", "|"]
        counts = {sep: sample_text.count(sep) for sep in candidates}
        best = max(counts, key=counts.get)
        return best if counts[best] > 0 else ","

    def _read_any_status_file(name: str, raw_bytes: bytes) -> pd.DataFrame:
        if name.lower().endswith(".csv"):
            sample = raw_bytes[:200_000].decode("utf-8-sig", errors="replace")
            sep = _detect_delim_for_csv(sample)
            return pd.read_csv(io.BytesIO(raw_bytes), engine="python", sep=sep, on_bad_lines="skip", encoding="utf-8-sig")
        return pd.read_excel(io.BytesIO(raw_bytes))

    def _extract_date_base_from_col_b(df: pd.DataFrame) -> Optional[dt.date]:
        if df.shape[1] < 2:
            return None
        s = df.iloc[:, 1]
        d = pd.to_datetime(s, errors="coerce", dayfirst=True).dt.date.dropna()
        if d.empty:
            return None
        m = d.mode()
        if len(m) > 0:
            return m.iloc[0]
        return max(d)

    def _calc_indicacoes_dentro_prazo(df: pd.DataFrame, data_base: dt.date) -> int:
        if df.shape[1] < 13:
            return 0
        s_m = df.iloc[:, 12]  # coluna M
        dt_cad = pd.to_datetime(s_m, errors="coerce", dayfirst=True)
        dt_base = pd.to_datetime(pd.Series([data_base] * len(df)), errors="coerce")
        delta_days = (dt_base - dt_cad).dt.days
        ok = delta_days.le(14) & delta_days.ge(0) & dt_cad.notna()
        return int(ok.sum())

    store = _leads_status_load()
    seen_hashes = set((store.get("_file_hashes", []) or []))

    r1, r2 = st.columns([1, 3])
    with r1:
        if st.button("Resetar somente Leads – Status Diário", use_container_width=True):
            _leads_status_reset_only()
            st.success("Relatório 'Leads – Status Diário' resetado. Os demais relatórios NÃO foram afetados.")

    with st.expander("Importar arquivo(s) diário(s) (status na coluna Q | data base na coluna B)", expanded=True):
        up_status_files = st.file_uploader(
            "Envie XLSX/CSV. O histórico é ACUMULADO (não substitui os outros dias).",
            type=["xlsx", "csv"],
            accept_multiple_files=True,
            key="leads_status_upload_q"
        )

    if up_status_files:
        imported = 0
        skipped = 0
        overwritten_days = []

        for upl in up_status_files:
            raw = upl.getvalue()
            h = file_md5(raw)
            if h in seen_hashes:
                skipped += 1
                continue

            try:
                df_status = _read_any_status_file(upl.name, raw)

                if df_status.shape[1] < 17:
                    st.error(f"{upl.name}: arquivo não possui coluna Q (precisa ter pelo menos 17 colunas).")
                    continue

                data_base = _extract_date_base_from_col_b(df_status)
                if data_base is None:
                    st.error(f"{upl.name}: não consegui ler a DATA BASE na coluna B.")
                    continue

                s = df_status.iloc[:, 16].astype("string").fillna("").str.strip()
                s = s[s != ""]
                if s.empty:
                    st.warning(f"{upl.name}: coluna Q vazia (nenhum status).")
                    continue

                counts = s.value_counts().to_dict()
                day_key = data_base.strftime("%d/%m/%Y")

                indic_ok = _calc_indicacoes_dentro_prazo(df_status, data_base)

                if day_key in store:
                    overwritten_days.append(day_key)

                store[day_key] = {
                    "status": {str(k): int(v) for k, v in counts.items()},
                    "indicacoes_dentro_prazo": int(indic_ok),
                }

                seen_hashes.add(h)
                imported += 1

            except Exception as e:
                st.error(f"Erro ao ler {upl.name}: {e}")

        store["_file_hashes"] = list(seen_hashes)
        _leads_status_save(store)

        if skipped:
            st.info(f"{skipped} arquivo(s) já tinham sido importados e foram ignorados (para não duplicar).")
        if overwritten_days:
            st.warning("Alguns dias já existiam e foram substituídos pelo arquivo mais recente: " + ", ".join(sorted(set(overwritten_days))))
        if imported:
            st.success(f"Importação concluída: {imported} arquivo(s) novos acumulados no histórico.")

    st.info("✅ Leads – Status Diário está OK aqui. (Se você quiser a tabela comparativa com Δ mais detalhada, eu acrescento sem mexer no resto.)")
