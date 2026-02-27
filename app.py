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
# CONFIGURAÇÕES (COLUNAS) - NÃO ALTERADO
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

# Data base (mês/dia do relatório)
COL_DATA_BASE = "DATA_BASE"

# Possíveis colunas para detectar o "mês do relatório" (mês do arquivo)
POSSIVEIS_COL_DATA_BASE = [
    "DATA_BASE", "DT_BASE", "DATA_REFERENCIA", "DT_REFERENCIA",
    "DATA_RELATORIO", "DT_RELATORIO", "DATA_ATUALIZACAO", "DT_ATUALIZACAO"
]

# Conversão
ALVO_CONVERSAO = 0.20

# A partir de Jan/26 salvar histórico diário
HIST_START = dt.date(2026, 1, 1)

# =========================================================
# REMUNERAÇÃO (FAIXAS) - NÃO ALTERADO
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

# ✅ histórico comparativo diário (por DATA_BASE)
HIST_COMPARE_DAILY = os.path.join(DATA_DIR, "hist_comparativo_diario.json")   # dd/mm/aaaa -> métricas do dia

# Leads diários (Status diário - coluna Q)
LEADS_STATUS_DAILY_PATH = os.path.join(DATA_DIR, "leads_status_daily_q.json")
LEADS_CONTROL_PATH = os.path.join(DATA_DIR, "leads_control.json")


# =========================================================
# HELPERS - EXATAMENTE COMO ESTAVAM
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


def safe_json_delete(path: str):
    """
    Remove somente o doc/arquivo daquele relatório.
    """
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
# ✅ EXTRA: helpers mínimos (Leads) — sem mexer no resto
# =========================================================
_FORBIDDEN = r"[./\*\[\]/]"

def firestore_safe_key(s: str, max_len: int = 120) -> str:
    if s is None:
        s = ""
    s = str(s).strip()
    s = re.sub(_FORBIDDEN, " ", s)
    s = s.replace("'", "").replace('"', "").replace("`", "")
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        s = "SEM_STATUS"
    if s.startswith("_"):
        s = f"K{s}"
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s

def day_key_store_iso(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def day_key_display_any(k: str) -> str:
    if isinstance(k, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", k):
        try:
            dd = dt.datetime.strptime(k, "%Y-%m-%d").date()
            return dd.strftime("%d/%m/%Y")
        except Exception:
            return k
    return str(k)

def make_unique_columns(cols: List[str]) -> List[str]:
    seen = {}
    out = []
    for c in cols:
        c0 = str(c)
        if c0 not in seen:
            seen[c0] = 1
            out.append(c0)
        else:
            seen[c0] += 1
            out.append(f"{c0} ({seen[c0]})")
    return out


# =========================================================
# CONTROLE DE ARQUIVOS (GENÉRICO) - USADO NO LEADS STATUS
# =========================================================
def get_leads_control() -> dict:
    """Retorna controle de arquivos para Leads"""
    return safe_json_load(LEADS_CONTROL_PATH, default={})


def save_leads_control(control: dict):
    """Salva controle de arquivos para Leads"""
    safe_json_save(LEADS_CONTROL_PATH, control)


def process_meta_files_with_control(
    uploaded_files,
    control_path: str,
    process_func,
) -> Tuple[List, List, int, int]:
    """
    Processa arquivos com controle inteligente.

    Regras:
    - Mesmo nome + mesmo hash → ignora
    - Mesmo nome + hash diferente → substitui
    - Nome novo → adiciona
    """
    control = safe_json_load(control_path, default={})
    files_meta = control.get("files", [])

    # Mapear por nome para busca rápida
    files_by_name = {f["name"]: f for f in files_meta}

    dfs = []
    novos_metadados = []
    qtd_novos = 0
    qtd_substituidos = 0

    for f in uploaded_files:
        raw = f.getvalue()
        h = file_md5(raw)

        # Verificar se já existe arquivo com este nome
        if f.name in files_by_name:
            hash_anterior = files_by_name[f.name]["hash"]

            if hash_anterior == h:
                # Mesmo arquivo, ignora
                continue
            else:
                # Mesmo nome, hash diferente → substituir
                qtd_substituidos += 1
        else:
            # Arquivo novo
            qtd_novos += 1

        try:
            df = process_func(f.name, raw)
            if df is not None and not df.empty:
                dfs.append(df)
                novos_metadados.append({"name": f.name, "hash": h, "size": f.size})
        except Exception as e:
            st.error(f"Erro ao processar {f.name}: {e}")

    # Atualizar controle
    if qtd_novos > 0 or qtd_substituidos > 0:
        # Remover arquivos que foram substituídos
        nomes_substituidos = [
            f.name for f in uploaded_files
            if f.name in files_by_name and file_md5(f.getvalue()) != files_by_name[f.name]["hash"]
        ]

        # Filtrar arquivos existentes removendo os substituídos
        files_meta = [f for f in files_meta if f["name"] not in nomes_substituidos]

        # Adicionar novos metadados
        files_meta.extend(novos_metadados)

        # Salvar controle
        control["files"] = files_meta
        control["file_hashes"] = [f["hash"] for f in files_meta]
        control["updated_at"] = dt.datetime.now().isoformat()
        safe_json_save(control_path, control)

    return dfs, novos_metadados, qtd_novos, qtd_substituidos


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
    """
    Detecta o mês do relatório (mês do arquivo), NÃO o mês de abertura.
    Prioridade:
      1) Colunas tipo DATA_BASE/DT_BASE etc
      2) Fallback: maior data existente em DT_CONTA_CRIADA
    """
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
# QUALIFICAÇÃO (NÍVEL) - NÃO ALTERADO
# =========================================================
def parse_level_from_criterios(txt: str) -> int:
    """
    Ex.: "CASH IN: 3 | DOMICILIO: 0 | SALDO MEDIO: 4 | ..."
    Regra: considerar SOMENTE o maior valor (1..4).
    """
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
    """
    Regra robusta:
    - BY pode vir 0/1/2/3/4 ou texto. Se for número 1..4, considera como nível.
    - CRITERIOS_ATINGIDOS_COMISS também tem níveis por critério -> pega o maior.
    - nível final = max(nível_BY, nível_CRIT)
    """
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
# HISTÓRICO DIÁRIO (SALVA SOMENTE O QUE EXISTE NO ARQUIVO)
# =========================================================
def daily_upsert_many(path: str, counts: Dict[str, int]):
    """
    counts: {"dd/mm/aaaa": qty}
    Salva/atualiza SEM criar datas.
    """
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

            # ✅ qualificadas por BR (M0/M1/M2)
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
    """
    Grava qualificação por MÊS DO RELATÓRIO (mês do arquivo),
    não por DT_CONTA_CRIADA.
    """
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
            section[data-testid="stSidebar"]{
                background: linear-gradient(180deg, #0f1b3a 0%, #1a2b4e 100%);
            }
            section[data-testid="stSidebar"] * {
                color: #ffffff !important;
            }
            section[data-testid="stSidebar"] .stButton button {
                background: rgba(255,255,255,0.1);
                border: 1px solid rgba(255,255,255,0.2);
                color: white !important;
            }
            div[data-testid="stMetric"]{
                background:#ffffff;
                border:1px solid #e9eef7;
                border-radius:14px;
                padding:16px 18px;
                box-shadow:0 4px 12px rgba(15,27,58,0.08);
                transition: all 0.2s ease;
            }
            div[data-testid="stMetric"]:hover {
                box-shadow:0 8px 20px rgba(15,27,58,0.12);
                transform: translateY(-2px);
            }
            h1, h2, h3{
                color: #0f1b3a;
                font-weight: 600;
                letter-spacing: -0.02em;
            }
            .am-badge-ok{
                display:inline-block; padding:6px 16px; border-radius:999px;
                background:rgba(0,122,255,0.12); color:#007AFF;
                font-weight:600; font-size:13px; border:1px solid rgba(0,122,255,0.2);
            }
            .am-badge-bad{
                display:inline-block; padding:6px 16px; border-radius:999px;
                background:rgba(255,59,48,0.12); color:#FF3B30;
                font-weight:600; font-size:13px; border:1px solid rgba(255,59,48,0.2);
            }
            .am-compact-table thead tr th {
                font-size: 13px !important;
                background: #f8fafd !important;
                color: #0f1b3a !important;
                font-weight: 600 !important;
            }
            .am-compact-table tbody tr td {
                font-size: 13px !important;
            }
            .stFileUploader > div {
                border: 2px dashed #e9eef7;
                border-radius: 16px;
                padding: 20px;
                background: #ffffff;
            }
            hr {
                margin: 2rem 0;
                border: none;
                height: 2px;
                background: linear-gradient(90deg, transparent, #e9eef7, transparent);
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
              <div style="font-size:32px;font-weight:900;color:#0f1b3a;margin-bottom:4px;">
                Assis e Mollerke
              </div>
              <div style="color:#5b6b8c;font-weight:600;font-size:16px;">
                Painel de Controle Estratégico
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
        LEADS_STATUS_DAILY_PATH,
        LEADS_CONTROL_PATH,
    ]:
        safe_json_delete(p)


# =========================================================
# APP
# =========================================================
st.set_page_config(page_title="Assis e Mollerke · C6", layout="wide")
apply_theme()

if not login_gate():
    st.stop()

st.sidebar.markdown("---")
if st.sidebar.button("🔄 RESETAR HISTÓRICO (ZERAR TUDO)"):
    reset_all_data()
    st.sidebar.success("Histórico resetado. Reimporte Nov/25 e Dez/25 (se quiser) e depois os diários.")

show_logo_and_title()
st.divider()

tab_painel, tab_leads_status = st.tabs(
    ["📊 Painel C6", "📋 Leads Diários"]
)

# =========================================================
# =====================  TAB 1  ===========================
# ===================== PAINEL C6 ==========================
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

    st.divider()

    st.subheader("Comparativo mensal de remuneração")

    saved = safe_json_load(HIST_RESUMO_MENSAL, default={})
    if not saved:
        st.info("Sem histórico mensal ainda. Importe diários (Jan/26 em diante) e/ou Nov/25 e Dez/25.")
    else:
        rows = []
        for mes, info in saved.items():
            rows.append([
                mes,
                info.get("faixa", ""),
                int(info.get("qualificadas", 0)),
                int(info.get("n1", 0)),
                int(info.get("n2", 0)),
                int(info.get("n3", 0)),
                int(info.get("n4", 0)),
                float(info.get("deveria_receber", 0.0)),
                float(info.get("ja_pago_ref", 0.0)),
                float(info.get("receber_mes", 0.0)),
            ])

        dfm = pd.DataFrame(rows, columns=[
            "Mês", "Faixa", "Qualificadas", "N1", "N2", "N3", "N4",
            "Deveria receber (cheio)", "Já pago (referência)", "A receber no mês"
        ]).sort_values("Mês", key=lambda col: col.map(month_key_str), ascending=True)

        view = dfm.copy()
        view["Qualificadas"] = view["Qualificadas"].apply(br_int)
        view["N1"] = view["N1"].apply(br_int)
        view["N2"] = view["N2"].apply(br_int)
        view["N3"] = view["N3"].apply(br_int)
        view["N4"] = view["N4"].apply(br_int)
        view["Deveria receber (cheio)"] = view["Deveria receber (cheio)"].apply(br_money)
        view["Já pago (referência)"] = view["Já pago (referência)"].apply(br_money)
        view["A receber no mês"] = view["A receber no mês"].apply(br_money)

        st.dataframe(view, use_container_width=True, hide_index=True)

        last = dfm.iloc[-1]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Último mês", str(last["Mês"]))
        c2.metric("Qualificadas", br_int(int(last["Qualificadas"])))
        c3.metric("Receita cheia", br_money(float(last["Deveria receber (cheio)"])))
        c4.metric("A receber", br_money(float(last["A receber no mês"])))


# =========================================================
# =====================  TAB 2  ===========================
# ================ 📋 LEADS DIÁRIOS (CORRIGIDO) ============
# =========================================================
with tab_leads_status:

    st.subheader("📋 Leads Diários (Status por Data Base)")

    # ----- Funções de Persistência -----
    def _leads_status_load():
        return safe_json_load(LEADS_STATUS_DAILY_PATH, default={}) or {}

    def _leads_status_save(obj):
        safe_json_save(LEADS_STATUS_DAILY_PATH, obj)

    def _leads_status_reset_only():
        safe_json_delete(LEADS_STATUS_DAILY_PATH)
        safe_json_delete(LEADS_CONTROL_PATH)
        st.rerun()

    # ----- Funções de Processamento de Arquivo -----
    def _detect_delim_for_csv(sample_text: str) -> str:
        candidates = [";", ",", "\t", "|"]
        counts = {sep: sample_text.count(sep) for sep in candidates}
        best = max(counts, key=counts.get)
        return best if counts[best] > 0 else ","

    def _read_any_status_file(name: str, raw_bytes: bytes) -> Optional[pd.DataFrame]:
        try:
            if name.lower().endswith(".csv"):
                sample = raw_bytes[:200_000].decode("utf-8-sig", errors="replace")
                sep = _detect_delim_for_csv(sample)
                return pd.read_csv(io.BytesIO(raw_bytes), engine="python", sep=sep, on_bad_lines="skip", encoding="utf-8-sig")
            return pd.read_excel(io.BytesIO(raw_bytes))
        except Exception as e:
            st.error(f"Erro ao ler {name}: {e}")
            return None

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

    # ----- ✅ Indicações válidas (≤14d) -----
    def _calcular_validas_14d(df: pd.DataFrame, data_base: dt.date) -> int:
        colunas_cadastro = [c for c in df.columns if 'DATA_HORA_CADASTRO' in str(c).upper()]

        if not colunas_cadastro:
            colunas_cadastro = [c for c in df.columns if 'CADAST' in str(c).upper() and 'DATA' in str(c).upper()]

        if not colunas_cadastro:
            return 0

        nome_coluna_cadastro = colunas_cadastro[0]

        cad_dt = pd.to_datetime(df[nome_coluna_cadastro], errors="coerce", dayfirst=True)

        if cad_dt.isna().all():
            return 0

        base_ts = pd.Timestamp(data_base)

        diff_days = (base_ts - cad_dt).dt.days

        mask = diff_days.notna() & (diff_days >= 0) & (diff_days <= 14)
        return int(mask.sum())

    # ----- Nome status (Firestore-safe) -----
    def limpar_nome_status(status: str) -> str:
        if not isinstance(status, str):
            status = str(status)

        nome = status.strip()
        nome = nome.replace("'", "")
        nome = nome.replace('"', '')
        nome = nome.replace('`', '')
        nome = nome.replace('-', ' ')
        nome = nome.replace('_', ' ')
        nome = ' '.join(nome.split())

        return firestore_safe_key(nome, max_len=120)

    # ----- Encurtar status (visual) -----
    def encurtar_status(status: str) -> str:
        if not isinstance(status, str):
            status = str(status)

        status_limpo = firestore_safe_key(status, max_len=80)
        status_lower = status_limpo.lower()

        if "ainda nao iniciou a abertura de conta" in status_lower:
            return "Ainda nao..."

        if "analise de credito" in status_lower or "análise de crédito" in status_lower:
            return "Em análise"

        if "aprovada aguardando assinatura" in status_lower:
            return "Aprovada"

        if "documentacao pendente" in status_lower or "documentação pendente" in status_lower:
            return "Doc pendente"

        if "desistente" in status_lower:
            return "Desistente"

        if "reprovado" in status_lower or "negado" in status_lower:
            return "Reprovado"

        if "ativo" in status_lower or "ativa" in status_lower:
            return "Ativo"

        if "cancelado" in status_lower:
            return "Cancelado"

        if "orientar" in status_lower:
            return "Orientar"

        if "atualizar" in status_lower:
            return "Atualizar"

        if "desacordo" in status_lower:
            return "Desacordo"

        palavras = status_limpo.split()
        if len(palavras) > 3:
            return ' '.join(palavras[:3]) + "..."

        if len(status_limpo) > 20:
            return status_limpo[:20] + "..."

        return status_limpo

    # ----- Carregar Estado Atual -----
    store = _leads_status_load() or {}

    # Migração sem reset: dd/mm/aaaa -> ISO
    store_migrado = {}
    for k, v in store.items():
        if isinstance(k, str) and re.match(r"^\d{2}/\d{2}/\d{4}$", k):
            try:
                d = dt.datetime.strptime(k, "%d/%m/%Y").date()
                store_migrado[day_key_store_iso(d)] = v
            except Exception:
                store_migrado[k] = v
        else:
            store_migrado[k] = v
    store = store_migrado

    # ----- Importação de Arquivos -----
    with st.expander("📤 Importar arquivo(s) diário(s)", expanded=True):
        st.markdown("""
        **Regras (como você pediu):**
        * Data Base vem da **coluna B**.
        * Status vem da **coluna Q**.
        * **Sempre atualiza (UPSERT)** mesmo se for a mesma data.
        * Não ignora por hash/nome.
        * Indicações Válidas (≤14 dias): **DATA_BASE - DATA_HORA_CADASTRO <= 14**
        """)

        # ✅ FIX refresh: key dinâmica pro uploader
        if "leads_upload_seq" not in st.session_state:
            st.session_state["leads_upload_seq"] = 0
        uploader_key = f"leads_status_upload_q_{st.session_state['leads_upload_seq']}"

        up_status_files = st.file_uploader(
            "Selecione os arquivos (XLSX/CSV). O histórico é ACUMULADO por dia (e o dia é sempre atualizado).",
            type=["xlsx", "csv"],
            accept_multiple_files=True,
            key=uploader_key
        )

        if up_status_files:
            control = safe_json_load(LEADS_CONTROL_PATH, default={}) or {}
            files_meta = control.get("files", []) or []

            processados = 0
            erros = 0

            last_processed_day = None
            last_processed_at = None

            for f in up_status_files:
                raw = f.getvalue()
                df_status = _read_any_status_file(f.name, raw)
                if df_status is None or df_status.empty:
                    erros += 1
                    continue

                if df_status.shape[1] < 17:
                    st.error(f"{f.name}: arquivo não possui coluna Q (precisa ter pelo menos 17 colunas).")
                    erros += 1
                    continue

                data_base = _extract_date_base_from_col_b(df_status)
                if data_base is None:
                    st.error(f"{f.name}: não consegui ler a DATA BASE na coluna B.")
                    erros += 1
                    continue

                day_key_iso = day_key_store_iso(data_base)

                # Status (coluna Q)
                s = df_status.iloc[:, 16].astype("string").fillna("").str.strip()
                s = s[s != ""]
                if s.empty:
                    status_counts = {}
                else:
                    s_limpo = s.apply(limpar_nome_status)
                    status_counts = s_limpo.value_counts().to_dict()
                    status_counts = {str(k): int(v) for k, v in status_counts.items()}

                # Indicações válidas
                validas = _calcular_validas_14d(df_status, data_base)

                # ✅ UPSERT por dia (substitui o dia inteiro pelo arquivo atual)
                payload = dict(status_counts)
                payload["_validas_14d"] = int(validas)
                store[day_key_iso] = payload

                imported_at = dt.datetime.now().isoformat()
                files_meta.append({
                    "name": f.name,
                    "hash": file_md5(raw),
                    "size": getattr(f, "size", None),
                    "day": day_key_iso,
                    "imported_at": imported_at,
                })

                # ✅ marcar último processado (último arquivo do loop)
                last_processed_day = day_key_iso
                last_processed_at = imported_at

                processados += 1

            control["files"] = files_meta[-1000:]
            control["updated_at"] = dt.datetime.now().isoformat()

            # ✅ salva qual foi o último arquivo processado
            if last_processed_day:
                control["last_processed"] = {
                    "day": last_processed_day,
                    "imported_at": last_processed_at,
                }

            safe_json_save(LEADS_CONTROL_PATH, control)

            _leads_status_save(store)

            st.success(f"✅ {processados} arquivo(s) processado(s). (erros: {erros})")

            # ✅ FIX refresh: “limpa” uploader e força rerun visível
            st.session_state["leads_upload_seq"] += 1
            st.rerun()

    # ----- Exibição do Painel -----
    store = _leads_status_load() or {}
    control = safe_json_load(LEADS_CONTROL_PATH, default={}) or {}

    if not store:
        st.info("Ainda não há histórico. Importe o(s) arquivo(s) para começar.")
    else:
        rows = []
        for dkey, payload in store.items():
            if not isinstance(payload, dict):
                continue
            validas = int(payload.get('_validas_14d', 0) or 0)
            data_br = day_key_display_any(dkey)

            for status, qtd in payload.items():
                if status == '_validas_14d':
                    continue
                rows.append({
                    "Data": data_br,
                    "Status": str(status),
                    "Quantidade": int(qtd),
                    "Indicações Válidas (≤14d)": int(validas)
                })

        if not rows:
            st.info("Nenhum dado de status encontrado no histórico.")
        else:
            dfh = pd.DataFrame(rows)
            dfh["_date"] = pd.to_datetime(dfh["Data"], format="%d/%m/%Y", errors="coerce")
            dfh = dfh.dropna(subset=["_date"])

            # ============================
            # ✅ Resumo Geral (AJUSTADO)
            # Total de Leads e Válidas = do ÚLTIMO arquivo processado
            # ============================
            st.markdown("### 📊 Resumo Geral")
            col_metric1, col_metric2 = st.columns(2)
            col_metric3, col_metric4 = st.columns(2)

            dias_unicos = int(dfh["Data"].nunique())
            status_unicos = int(dfh["Status"].nunique())

            # ✅ pega o último arquivo processado
            last_info = control.get("last_processed", {}) or {}
            last_day = last_info.get("day")

            if not last_day:
                # fallback: maior dia ISO no store
                try:
                    last_day = max([k for k in store.keys() if isinstance(k, str)])
                except Exception:
                    last_day = None

            payload_last = store.get(last_day, {}) if last_day else {}
            if not isinstance(payload_last, dict):
                payload_last = {}

            total_geral = sum(int(v) for k, v in payload_last.items() if k != "_validas_14d")
            total_validas = int(payload_last.get("_validas_14d", 0) or 0)

            with col_metric1:
                st.metric("📅 Dias no histórico", br_int(dias_unicos))
            with col_metric2:
                st.metric("🏷️ Status únicos", br_int(status_unicos))
            with col_metric3:
                st.metric("📊 Total de Leads", br_int(total_geral))
            with col_metric4:
                st.metric("✅ Indicações Válidas (≤14d)", br_int(total_validas))

            st.divider()

            # Filtro por mês
            dfh["Mes"] = dfh["_date"].dt.to_period("M").astype(str)
            meses = sorted(dfh["Mes"].unique(), reverse=True)
            meses_lbl = [f"{m.split('-')[1]}/{m.split('-')[0]}" for m in meses]

            st.markdown("### Filtros")
            mes_sel_lbl = st.selectbox(
                "Selecione o mês",
                meses_lbl,
                index=0,
                key="leads_status_mes_sel"
            )
            mes_sel = meses[meses_lbl.index(mes_sel_lbl)]

            # Tabela comparativa
            df_mes = dfh[dfh["Mes"] == mes_sel].copy()

            if not df_mes.empty:
                st.markdown("### 📈 Comparativo Diário (Δ vs dia anterior)")

                pivot = df_mes.pivot_table(
                    index="_date",
                    columns="Status",
                    values="Quantidade",
                    aggfunc="sum",
                    fill_value=0
                ).astype(int)

                pivot.columns = make_unique_columns([encurtar_status(col) for col in pivot.columns])

                pivot = pivot.sort_index(ascending=True)

                for col in pivot.columns:
                    pivot[f"Δ {col}"] = pivot[col].diff().fillna(0)

                pivot["Total"] = pivot[[c for c in pivot.columns if not str(c).startswith("Δ ")]].sum(axis=1)
                pivot["Δ Total"] = pivot["Total"].diff().fillna(0)

                validas_por_dia = df_mes.groupby("_date")["Indicações Válidas (≤14d)"].first().to_dict()
                pivot["Indicações Válidas"] = pivot.index.map(validas_por_dia).fillna(0)
                pivot["Δ Indicações Válidas"] = pivot["Indicações Válidas"].diff().fillna(0)

                pivot = pivot.sort_index(ascending=False)

                view = pivot.reset_index()
                view["Data Base"] = view["_date"].dt.strftime("%d/%m/%Y")

                status_cols = [c for c in view.columns if not c.startswith("Δ ") and c not in ["_date", "Data Base", "Total", "Indicações Válidas"]]
                delta_cols = [f"Δ {c}" for c in status_cols if f"Δ {c}" in view.columns]

                col_order = ["Data Base", "Total", "Δ Total"] + status_cols + delta_cols + ["Indicações Válidas", "Δ Indicações Válidas"]
                view = view[[c for c in col_order if c in view.columns]]

                view_display = view.copy()
                numeric_cols = [c for c in view_display.columns if c != "Data Base"]
                for col in numeric_cols:
                    view_display[col] = view_display[col].apply(br_int)

                def color_delta(val):
                    try:
                        if isinstance(val, str):
                            num = int(val.replace('.', ''))
                        else:
                            num = int(val)
                        if num > 0:
                            return 'color: #0a7d2a; font-weight: 900;'
                        elif num < 0:
                            return 'color: #b00020; font-weight: 900;'
                    except:
                        pass
                    return ''

                styled = view_display.style
                delta_cols_display = [col for col in view_display.columns if col.startswith('Δ ')]
                if delta_cols_display:
                    styled = styled.applymap(color_delta, subset=delta_cols_display)

                if 'Total' in view_display.columns:
                    styled = styled.applymap(lambda x: 'font-weight: 900;', subset=['Total'])

                styled = styled.set_table_styles([
                    {"selector": "th", "props": [
                        ("background-color", "#f3f6fb"),
                        ("color", "#0f1b3a"),
                        ("font-weight", "900"),
                        ("border", "1px solid #e9eef7"),
                        ("padding", "8px")
                    ]},
                    {"selector": "td", "props": [
                        ("border", "1px solid #e9eef7"),
                        ("padding", "8px")
                    ]},
                    {"selector": "tr:nth-of-type(even)", "props": [
                        ("background-color", "#fbfcff")
                    ]},
                ])

                st.dataframe(styled, use_container_width=True, hide_index=True)

    st.divider()
    col_reset1, col_reset2, col_reset3 = st.columns([1, 2, 1])
    with col_reset2:
        if st.button("🧹 Resetar somente Leads – Status Diário", use_container_width=True, type="secondary"):
            _leads_status_reset_only()
